"""通用线程映射中枢（建档 / 归档 / 幂等键）。

ai_thread_mappings 是通用层的"调度中枢表"，其写入逻辑必须收口在
core 层：业务插件只描述"建一条什么样的映射"，不直接触碰 ORM。
与 MessageRecorder 保持同一范式：通用层封装持久化，插件只做编排。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError

from app.core.database import AsyncSessionLocal
from app.models.ai_schema import AIThreadMapping

# 通用初始状态（业务插件可覆盖为自身语义）
STATUS_DRAFT = "DRAFT"
# 通用活跃状态（无状态机的纯对话线程）
STATUS_ACTIVE = "ACTIVE"
# 通用挂起状态（等待人工介入）
STATUS_WAITING_HUMAN = "WAITING_HUMAN"
# 通用进行中状态（流程已启动、尚未落到挂起态或终态）
STATUS_IN_PROGRESS = "IN_PROGRESS"
# 通用运行中状态（CAS 抢占成功、状态机恢复执行期间）
STATUS_RUNNING = "RUNNING"
# 通用失败终态（流程异常终止，核心层兜底用）
STATUS_FAILED = "FAILED"
# 通用兜底角色（无状态机的纯对话线程）
ROLE_SYSTEM = "SYSTEM"

# 在途状态集合：线程处于这些状态说明流程仍在进行，终态回写应当生效。
# 其它状态一律视为"已被他人终结"，终态回写不再覆盖 —— 首次终态写入生效，
# 杜绝迟到的请求把已定的结论改回去。
IN_FLIGHT_STATUSES = (
    STATUS_DRAFT,
    STATUS_ACTIVE,
    STATUS_IN_PROGRESS,
    STATUS_RUNNING,
    STATUS_WAITING_HUMAN,
)

# 聊天线程命名空间前缀（通用层约定：与业务工单线程天然隔离）。
# 本模块是 thread_id 语义的权威定义处，其它模块一律从此导入，勿重复定义。
CHAT_THREAD_PREFIX = "chat_"

# 线程类别（由命名空间前缀推导，不新增业务语义列）
THREAD_KIND_CHAT = "chat"
THREAD_KIND_TASK = "task"


class ThreadMappingStore:
    """无状态线程映射仓储（全部静态方法）。"""

    @staticmethod
    async def create_mapping(
        thread_id: str,
        session_id: str,
        creator_id: str,
        creator_role: str,
        assigned_role: str,
        creator_dept: str | None = None,
        status: str = STATUS_DRAFT,
        idempotency_key: str | None = None,
        title: str | None = None,
    ) -> AIThreadMapping:
        """建立线程映射（幂等）：已存在则原样返回，不重复插入。

        title 为线程展示标题：业务语义由插件注入，核心层只当不透明字符串存取。
        """
        async with AsyncSessionLocal() as session:
            existing = await session.get(AIThreadMapping, thread_id)
            if existing is not None:
                return existing

            mapping = AIThreadMapping(
                thread_id=thread_id,
                session_id=session_id,
                creator_id=creator_id,
                creator_role=creator_role,
                creator_dept=creator_dept,
                assigned_role=assigned_role,
                status=status,
                lock_version=1,
                idempotency_key=idempotency_key,
                title=title,
            )
            session.add(mapping)
            try:
                await session.commit()
            except IntegrityError:
                # 并发重复建档：回滚后取已存在记录
                await session.rollback()
                existing = await session.get(AIThreadMapping, thread_id)
                if existing is not None:
                    return existing
                raise
            return mapping

    @staticmethod
    async def update_status(thread_id: str, status: str) -> bool:
        """回写线程状态；返回是否命中记录。

        status 语义由业务插件定义，核心层只当不透明字符串存取。
        状态推进属于实质活动，同步提升线程最近活跃时间。
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "UPDATE ai_thread_mappings SET status = :status, "
                    "last_activity_at = now() "
                    "WHERE thread_id = :tid"
                ),
                {"status": status, "tid": thread_id},
            )
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    async def update_current_node(thread_id: str, node_name: str | None) -> bool:
        """回写状态机当前节点指针；返回是否命中记录。

        node_name 为 None 表示清空指针（流程已终结，不再停留在任何节点上）。
        节点名语义由业务插件定义，核心层只当不透明字符串存取。
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "UPDATE ai_thread_mappings SET current_node = :node "
                    "WHERE thread_id = :tid"
                ),
                {"node": node_name, "tid": thread_id},
            )
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    async def update_biz_order_id(thread_id: str, biz_order_id: str | None) -> bool:
        """回写外部业务单号（弱引用）；返回是否命中记录。

        biz_order_id 是外部系统的单据标识，核心层只当不透明字符串存取。
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "UPDATE ai_thread_mappings SET biz_order_id = :biz "
                    "WHERE thread_id = :tid"
                ),
                {"biz": biz_order_id, "tid": thread_id},
            )
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    async def archive(thread_id: str) -> bool:
        """归档线程映射（软删除）；返回是否命中记录。

        归档后该线程不再出现在待办池（见 HITLController.get_pending_tasks），
        但记录本身保留以维持审计链路。
        """
        async with AsyncSessionLocal() as session:
            mapping = await session.get(AIThreadMapping, thread_id)
            if mapping is None:
                return False
            mapping.is_archived = True
            mapping.archived_at = func.now()
            await session.commit()
            return True

    @staticmethod
    async def archive_many(thread_ids: list[str]) -> list[str]:
        """批量归档线程映射；返回实际命中并置为归档的 thread_id 列表。

        已归档的记录不重复处理（幂等）。
        """
        if not thread_ids:
            return []
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "UPDATE ai_thread_mappings "
                    "SET is_archived = true, archived_at = now() "
                    "WHERE thread_id = ANY(:ids) AND is_archived = false "
                    "RETURNING thread_id"
                ),
                {"ids": list(thread_ids)},
            )
            archived = [row[0] for row in result]
            await session.commit()
        return archived

    @staticmethod
    async def list_pending(
        before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """列出未归档的挂起记录（供归档脚本预览 / 运维巡检）。

        - before 为 None 时返回全部未归档的 WAITING_HUMAN 记录；
        - before 不为 None 时只返回 created_at 早于该时刻的记录。
        """
        base_sql = (
            "SELECT thread_id, session_id, creator_id, creator_role, "
            "assigned_role, status, lock_version, created_at "
            "FROM ai_thread_mappings "
            "WHERE status = :status AND is_archived = false "
        )
        params: dict[str, Any] = {"status": STATUS_WAITING_HUMAN}
        if before is None:
            sql = base_sql + "ORDER BY created_at"
        else:
            sql = base_sql + "AND created_at < :before ORDER BY created_at"
            params["before"] = before

        async with AsyncSessionLocal() as session:
            rows = await session.execute(text(sql), params)
            items = [dict(row._mapping) for row in rows]
        for item in items:
            if item.get("created_at") is not None:
                item["created_at"] = item["created_at"].isoformat()
        return items

    @staticmethod
    async def list_visible(
        user_id: str,
        user_role: str,
        *,
        kind: str = "all",
        session_id: str | None = None,
        include_archived: bool = False,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """列出当前用户可见的线程（会话 / 工单条目）。

        可见性：creator_id / operator_id / assigned_role 三命中其一
        （assigned_role 命中需排除系统占位角色，避免"人人可见"）。

        thread_kind 由命名空间前缀推导（chat_ 前缀为对话线程），不新增业务语义列。
        title 为解析后的展示标题：title 列 → 首条文本消息前 60 字 → thread_id。
        last_activity_at 取线程最近活跃时间（物化列，由写消息 / 状态推进维护），
        供列表按"最近活跃"排序。

        q 非空时按**展示标题**做大小写不敏感的子串过滤（匹配展示标题而非
        title 列，保证两条来源的标题都可被检索到）。使用 strpos 而非 ILIKE：
        strpos 无通配符语义，用户输入的 % _ \ 一律按字面量处理，无需转义。

        返回的每一行附带 total_count：过滤后的可见条目总数（窗口函数，
        在 WHERE 之后求值）。offset 越界导致空结果页时无行可取，total_count
        亦随之缺失，调用方按缺省 0 处理。
        """
        sql = text(
            # 内层 CTE 统一解析展示标题与最近活跃时间，外层只做过滤与分页，
            # 避免同一套标题表达式在 WHERE 中重复书写。
            "WITH visible AS ( "
            "  SELECT t.thread_id, t.session_id, t.creator_id, t.creator_role, "
            "         t.assigned_role, t.status, t.lock_version, t.current_node, "
            "         t.created_at, t.updated_at, "
            "         CASE WHEN t.thread_id LIKE 'chat\\_%' THEN 'chat' "
            "              ELSE 'task' END AS thread_kind, "
            "         COALESCE( "
            "           NULLIF(t.title, ''), "
            "           NULLIF((SELECT left(m.content, 60) "
            "                   FROM ai_chat_messages m "
            "                   WHERE m.thread_id = t.thread_id "
            "                     AND m.kind = 'text' "
            "                     AND m.content IS NOT NULL AND m.content <> '' "
            "                   ORDER BY m.seq LIMIT 1), ''), "
            "           t.thread_id "
            "         ) AS title, "
            "         COALESCE(t.last_activity_at, t.created_at) AS last_activity_at "
            "  FROM ai_thread_mappings t "
            "  WHERE (CAST(:include_archived AS boolean) OR t.is_archived = false) "
            "    AND (t.creator_id = :uid "
            "         OR t.operator_id = :uid "
            "         OR (t.assigned_role <> :system_role "
            "             AND t.assigned_role = :role)) "
            "    AND (CAST(:session_id AS text) IS NULL "
            "         OR t.session_id = :session_id) "
            "    AND (CAST(:kind AS text) <> 'task' "
            "         OR t.thread_id NOT LIKE 'chat\\_%') "
            ") "
            "SELECT *, count(*) OVER () AS total_count "
            "FROM visible "
            "WHERE (CAST(:q AS text) IS NULL "
            "       OR strpos(lower(title), lower(CAST(:q AS text))) > 0) "
            "ORDER BY last_activity_at DESC, thread_id DESC "
            "LIMIT CAST(:limit AS integer) OFFSET CAST(:offset AS integer)"
        )
        params: dict[str, Any] = {
            "uid": user_id,
            "role": user_role or "",
            "system_role": ROLE_SYSTEM,
            "kind": kind,
            "session_id": session_id,
            "include_archived": include_archived,
            "q": (q or "").strip() or None,
            "limit": max(1, min(limit, 500)),
            "offset": max(0, offset),
        }
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sql, params)
            items = [dict(row._mapping) for row in rows]
        for item in items:
            for key in ("created_at", "updated_at", "last_activity_at"):
                value = item.get(key)
                if value is not None:
                    item[key] = value.isoformat()
        return items

    @staticmethod
    async def list_sessions(
        user_id: str,
        user_role: str,
        *,
        q: str | None = None,
        include_archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按 session_id 聚合列出当前用户可见的会话。

        一个会话框内可产生多条线程（对话线程 + 若干业务线程），左栏按会话
        聚合为一行，避免同一会话重复出现。

        可见性仍逐线程判定：组内至少一条可见线程即视为会话可见；不上提到
        会话级，避免泄露他人会话的存在与条数。

        代表态 status：业务线程优先 —— 组内存在任务线程时取其中最近活跃者
        的状态，否则取对话线程的状态（core 只认 thread_kind，不解释业务语义）。
        title：组内最早创建的对话线程标题（无对话线程时回落最早线程）。
        related_count：组内任务线程数；thread_count：组内线程总数。

        分页必须落在聚合之后：窗口函数置于 GROUP BY 外层，total_count 为
        会话总数而非线程数；q 匹配组内任一可见线程的标题。
        """
        sql = text(
            # 内层 CTE 逐线程判定可见性并解析展示标题；外层按 session_id 聚合，
            # 代表态与标题均用有序 array_agg 取首条，聚合完成后才做分页。
            "WITH visible AS ( "
            "  SELECT t.thread_id, t.session_id, t.status, t.created_at, "
            "         t.updated_at, "
            "         CASE WHEN t.thread_id LIKE 'chat\\_%' THEN 'chat' "
            "              ELSE 'task' END AS thread_kind, "
            "         COALESCE( "
            "           NULLIF(t.title, ''), "
            "           NULLIF((SELECT left(m.content, 60) "
            "                   FROM ai_chat_messages m "
            "                   WHERE m.thread_id = t.thread_id "
            "                     AND m.kind = 'text' "
            "                     AND m.content IS NOT NULL AND m.content <> '' "
            "                   ORDER BY m.seq LIMIT 1), ''), "
            "           t.thread_id "
            "         ) AS title, "
            "         COALESCE(t.last_activity_at, t.created_at) AS last_activity_at "
            "  FROM ai_thread_mappings t "
            "  WHERE (CAST(:include_archived AS boolean) OR t.is_archived = false) "
            "    AND (t.creator_id = :uid "
            "         OR t.operator_id = :uid "
            "         OR (t.assigned_role <> :system_role "
            "             AND t.assigned_role = :role)) "
            "), agg AS ( "
            "  SELECT session_id, "
            "         count(*) AS thread_count, "
            "         count(*) FILTER (WHERE thread_kind = 'task') AS related_count, "
            "         min(created_at) AS created_at, "
            "         max(updated_at) AS updated_at, "
            "         max(last_activity_at) AS last_activity_at, "
            # 标题：对话线程优先（最早创建的对话标题），再按创建时间取首条
            "         (array_agg(title "
            "           ORDER BY (thread_kind <> 'chat'), created_at, thread_id))[1] "
            "           AS title, "
            # 代表态：任务线程优先（最近活跃者），再按活跃时间取首条
            "         (array_agg(status "
            "           ORDER BY (thread_kind <> 'task'), "
            "                    last_activity_at DESC, thread_id DESC))[1] "
            "           AS status, "
            # 关键字：命中组内任一可见线程标题即算命中
            "         bool_or(CASE WHEN CAST(:q AS text) IS NULL THEN false "
            "                      ELSE strpos(lower(title), "
            "                                  lower(CAST(:q AS text))) > 0 END) "
            "           AS q_hit "
            "  FROM visible "
            "  GROUP BY session_id "
            ") "
            "SELECT session_id, title, status, related_count, thread_count, "
            "       created_at, updated_at, last_activity_at, "
            "       count(*) OVER () AS total_count "
            "FROM agg "
            "WHERE (CAST(:q AS text) IS NULL OR q_hit) "
            "ORDER BY last_activity_at DESC, session_id DESC "
            "LIMIT CAST(:limit AS integer) OFFSET CAST(:offset AS integer)"
        )
        params: dict[str, Any] = {
            "uid": user_id,
            "role": user_role or "",
            "system_role": ROLE_SYSTEM,
            "include_archived": include_archived,
            "q": (q or "").strip() or None,
            "limit": max(1, min(limit, 500)),
            "offset": max(0, offset),
        }
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sql, params)
            items = [dict(row._mapping) for row in rows]
        for item in items:
            for key in ("created_at", "updated_at", "last_activity_at"):
                value = item.get(key)
                if value is not None:
                    item[key] = value.isoformat()
        return items

    @staticmethod
    async def get(thread_id: str) -> dict[str, Any] | None:
        """读取线程映射快照；不存在返回 None。"""
        async with AsyncSessionLocal() as session:
            mapping = await session.get(AIThreadMapping, thread_id)
            if mapping is None:
                return None
            return {
                "thread_id": mapping.thread_id,
                "session_id": mapping.session_id,
                "creator_id": mapping.creator_id,
                "creator_role": mapping.creator_role,
                "assigned_role": mapping.assigned_role,
                "operator_id": mapping.operator_id,
                "status": mapping.status,
                "lock_version": mapping.lock_version,
                "current_node": mapping.current_node,
                "biz_order_id": mapping.biz_order_id,
                "title": mapping.title,
                "is_archived": mapping.is_archived,
                "created_at": (
                    mapping.created_at.isoformat()
                    if mapping.created_at is not None
                    else None
                ),
                "updated_at": (
                    mapping.updated_at.isoformat()
                    if mapping.updated_at is not None
                    else None
                ),
                "last_activity_at": (
                    mapping.last_activity_at.isoformat()
                    if mapping.last_activity_at is not None
                    else None
                ),
            }


__all__ = [
    "CHAT_THREAD_PREFIX",
    "IN_FLIGHT_STATUSES",
    "ROLE_SYSTEM",
    "STATUS_ACTIVE",
    "STATUS_DRAFT",
    "STATUS_FAILED",
    "STATUS_IN_PROGRESS",
    "STATUS_RUNNING",
    "STATUS_WAITING_HUMAN",
    "THREAD_KIND_CHAT",
    "THREAD_KIND_TASK",
    "ThreadMappingStore",
]
