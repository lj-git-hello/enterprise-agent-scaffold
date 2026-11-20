"""通用人机协同（HITL）并发控制器。

集中收口三类跨业务操作：
- resume_with_cas：CAS 乐观锁抢占 + Command(resume) 唤醒工作流
- get_pending_tasks：按角色查询待办
- finalize_status：流程结束后回写终态 / 签名

垂直业务 Router 只做编排，不写任何底层 SQL。
"""

from typing import Any

from fastapi import HTTPException, status
from langgraph.types import Command
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.workflow.graph_registry import DEFAULT_GRAPH_NAME, GraphRegistry
from app.core.workflow.thread_mapping_store import (
    IN_FLIGHT_STATUSES,
    STATUS_RUNNING,
    STATUS_WAITING_HUMAN,
)
from app.core.workflow.trace_recorder import TraceRecorder

# 通用状态常量（禁止魔法值）：STATUS_RUNNING 由 thread_mapping_store 统一定义，
# 此处仅做再导出，供历史引用方保持兼容。

# 图恢复失败时的留痕节点名（业务中立措辞：core 层零业务词汇要求）
TRACE_NODE_RESUME_FAILED = "workflow.resume_failed"


class HITLController:
    """无状态 HITL 并发控制器（全部静态方法）。"""

    @staticmethod
    async def resume_with_cas(
        thread_id: str,
        lock_version: int,
        resume_payload: dict[str, Any],
        operator_id: str,
        audit_opinion: str | None = None,
        graph_name: str = DEFAULT_GRAPH_NAME,
    ) -> None:
        """CAS 抢占成功后，用 Command(resume) 唤醒对应工作流图。

        抢占失败（已被处理 / 版本失效）抛 409。
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "UPDATE ai_thread_mappings "
                    "SET status = :running, "
                    "lock_version = lock_version + 1, "
                    "operator_id = :uid, "
                    "audit_opinion = :opinion, "
                    "last_activity_at = now() "
                    "WHERE thread_id = :tid "
                    "AND lock_version = :ver "
                    "AND status = :waiting"
                ),
                {
                    "running": STATUS_RUNNING,
                    "uid": operator_id,
                    "opinion": audit_opinion,
                    "tid": thread_id,
                    "ver": lock_version,
                    "waiting": STATUS_WAITING_HUMAN,
                },
            )
            if result.rowcount == 0:
                await session.rollback()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="任务已被其他操作人处理或版本已失效",
                )
            await session.commit()

        # 依赖反转：从注册中心取当前业务图，底座不感知具体业务
        graph = GraphRegistry.get_graph(graph_name)
        config = {"configurable": {"thread_id": thread_id}}
        try:
            await graph.ainvoke(Command(resume=resume_payload), config=config)
        except Exception as exc:  # noqa: BLE001 —— 失败必须留痕并上抛
            # CAS 与图恢复分属两个事务：图恢复失败时线程已被置为 RUNNING，
            # 若就此返回将永久滞留中间态。底座只负责"留痕 + 上抛"——
            # 是否仍可保持终态由业务插件按自身语义判定（插件可能已产出
            # 不可逆的终态产物），底座无权代为决定，故不擅自改写状态。
            await TraceRecorder.record(
                thread_id,
                TRACE_NODE_RESUME_FAILED,
                error_log=f"{type(exc).__name__}: {exc}",
            )
            raise

    @staticmethod
    async def get_pending_tasks(assigned_role: str) -> list[dict[str, Any]]:
        """查询指定角色的待办记录（WAITING_HUMAN 且未归档）。

        归档过滤是必需的：待办池是"只增不减"的工作队列，一旦缺少生命周期过滤，
        历史遗留的挂起记录会永久沉在审批人池里（无人核准即永不消失）。
        """
        async with AsyncSessionLocal() as session:
            rows = await session.execute(
                text(
                    "SELECT thread_id, session_id, creator_id, creator_dept, "
                    "status, assigned_role, lock_version, audit_opinion, "
                    "created_at "
                    "FROM ai_thread_mappings "
                    "WHERE status = :status AND assigned_role = :role "
                    "AND is_archived = false "
                    "ORDER BY created_at"
                ),
                {
                    "status": STATUS_WAITING_HUMAN,
                    "role": assigned_role,
                },
            )
            items = [dict(row._mapping) for row in rows]

        for item in items:
            if item.get("created_at") is not None:
                item["created_at"] = item["created_at"].isoformat()
        return items

    @staticmethod
    async def finalize_status(
        thread_id: str,
        final_status: str,
        signature_hash: str | None = None,
        operator_id: str | None = None,
        audit_opinion: str | None = None,
    ) -> bool:
        """流程结束后回写映射终态；返回是否真正落库。

        【终态守卫】只有仍在途的线程才接受终态回写：当前状态属于通用在途
        集合（DRAFT / ACTIVE / IN_PROGRESS / RUNNING / WAITING_HUMAN），
        或已等于目标终态（幂等重写）时才写入。已被他人终结的线程不再被
        覆盖 —— 首次终态写入生效，迟到的请求无法把已定的结论改回去。

        仅回写传入的字段：未传者保持原值不变（避免覆盖 CAS 阶段已写入的内容）。
        """
        # 赋值片段全部来自本模块字面量，不含外部输入，无注入风险
        # 状态推进属实质活动，同步提升线程最近活跃时间
        assignments = ["status = :status", "last_activity_at = now()"]
        params: dict[str, Any] = {"status": final_status, "tid": thread_id}
        if signature_hash is not None:
            assignments.append("signature_hash = :sig")
            params["sig"] = signature_hash
        if operator_id is not None:
            assignments.append("operator_id = :uid")
            params["uid"] = operator_id
        if audit_opinion is not None:
            assignments.append("audit_opinion = :opinion")
            params["opinion"] = audit_opinion

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    f"UPDATE ai_thread_mappings SET {', '.join(assignments)} "
                    "WHERE thread_id = :tid "
                    "AND (status = :status "
                    "     OR status = ANY(CAST(:in_flight AS text[])))"
                ),
                {**params, "in_flight": list(IN_FLIGHT_STATUSES)},
            )
            await session.commit()
            return result.rowcount > 0


__all__ = [
    "STATUS_RUNNING",
    "TRACE_NODE_RESUME_FAILED",
    "HITLController",
]