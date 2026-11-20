"""通用线程与会话查询路由（行业中立的脚手架能力）。

服务端是线程 / 消息的唯一权威源：本模块提供前端渲染所需的读取接口，
使前端本地存储可以降级为"离线兜底快照"而非主存储。

提供五组读取：
- GET /api/v1/threads：当前用户可见的线程（会话 / 工单条目）列表
- GET /api/v1/sessions：按 session_id 聚合的可见会话列表
- GET /api/v1/threads/{thread_id}：线程只读详情（元信息 + 最新卡片快照）
- GET /api/v1/threads/{thread_id}/messages：单线程的消息与卡片快照
- GET /api/v1/sessions/{session_id}/messages：整个会话的消息（跨线程全序）

可见性规则收口在 core.workflow.thread_access；不可见与不存在统一 404，
不泄露资源存在性。本模块不含任何业务词汇。
"""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.security import get_current_user
from app.core.workflow import (
    MessageRecorder,
    ThreadMappingStore,
    assert_thread_visible,
)

thread_router = APIRouter(tags=["Universal-Threads"])

# 列表单页上限
MAX_LIST_LIMIT = 200
# 消息单页上限
MAX_MESSAGE_LIMIT = 500
# 允许的线程类别
_ALLOWED_KINDS = ("all", "task")


def _now_iso() -> str:
    """当前时刻的 ISO 字符串（供客户端做时钟偏移校正）。"""
    return datetime.now(timezone.utc).isoformat()


@thread_router.get("/threads")
async def list_threads(
    kind: str = Query("all", description="线程类别：all=全部；task=排除对话线程"),
    session_id: str | None = Query(None, description="限定某一会话"),
    include_archived: bool = Query(False, description="是否包含已归档线程"),
    q: str | None = Query(None, description="标题关键字（大小写不敏感的子串匹配）"),
    limit: int = Query(100, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """当前用户可见的线程列表。

    可见性由 core 统一判定（发起人 / 实际处理人 / 待办责任人三命中其一），
    无权即不可见 —— 不返回 403，避免暴露"存在但无权"这一事实。

    q 非空时按展示标题过滤（大小写不敏感，子串匹配）。

    total 为过滤后的可见条目总数。本页为空时窗口函数无行可取，total 取 0；
    该情形下 has_more 同样为 false，客户端据此判定"已到末页"。
    """
    if kind not in _ALLOWED_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"kind 仅支持 {'/'.join(_ALLOWED_KINDS)}",
        )
    user_id = current_user["sub"]
    user_role = current_user.get("role") or ""
    # 多取一条用于判断 has_more
    rows = await ThreadMappingStore.list_visible(
        user_id,
        user_role,
        kind=kind,
        session_id=session_id,
        include_archived=include_archived,
        q=q,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(rows) > limit
    # total_count 由 SQL 窗口函数给出，各行取值一致，取首行即可
    total = int(rows[0].get("total_count") or 0) if rows else 0
    # total_count 是 SQL 层产物，不进对外契约（总数由顶层 total 承载）
    items = []
    for row in rows[:limit]:
        item = dict(row)
        item.pop("total_count", None)
        items.append(item)
    return {
        "items": items,
        "has_more": has_more,
        "total": total,
        "server_time": _now_iso(),
    }


@thread_router.get("/sessions")
async def list_sessions(
    q: str | None = Query(
        None, description="标题关键字（命中会话内任一可见线程标题）"
    ),
    limit: int = Query(100, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """当前用户可见的会话列表（按 session_id 聚合）。

    一个会话框内可产生多条线程（对话线程 + 若干业务线程），本端点按会话
    聚合为一行，避免同一会话在列表里重复出现。

    代表态 status 为业务线程优先：组内存在任务线程时取其中最近活跃者的
    状态，否则取对话线程的状态。related_count 为组内任务线程数，
    thread_count 为组内线程总数。q 命中组内任一可见线程标题即算命中。

    可见性仍逐线程判定，会话可见 = 组内至少一条可见线程。

    分页落在聚合之后：total 为会话总数。本页为空时窗口函数无行可取，
    total 取 0。
    """
    user_id = current_user["sub"]
    user_role = current_user.get("role") or ""
    # 多取一条用于判断 has_more
    rows = await ThreadMappingStore.list_sessions(
        user_id,
        user_role,
        q=q,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(rows) > limit
    total = int(rows[0].get("total_count") or 0) if rows else 0
    items = []
    for row in rows[:limit]:
        item = dict(row)
        item.pop("total_count", None)
        items.append(item)
    return {
        "items": items,
        "has_more": has_more,
        "total": total,
        "server_time": _now_iso(),
    }


@thread_router.get("/threads/{thread_id}")
async def get_thread_detail(
    thread_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """线程只读详情（线程元信息 + 卡片快照）。

    可见性由 core 统一判定，不可见与不存在统一 404。
    本端点严格只读：不写库、不改状态、不触发任何外部系统。

    card 为线程内最新一张卡片（不限类型）；cards 为线程内每种卡片类型的
    最新一张 —— 只读检视需要一次取全不同类型的卡片信息。
    """
    mapping = await assert_thread_visible(
        thread_id,
        current_user["sub"],
        current_user.get("role") or "",
    )
    card = await MessageRecorder.get_latest_card(thread_id)
    cards = await MessageRecorder.list_latest_cards(thread_id)
    return {
        "thread": mapping,
        "card": card,
        "cards": cards,
        "server_time": _now_iso(),
    }


@thread_router.get("/threads/{thread_id}/messages")
async def list_thread_messages(
    thread_id: str,
    limit: int = Query(200, ge=1, le=MAX_MESSAGE_LIMIT),
    offset: int = Query(0, ge=0),
    order: str = Query("asc", description="排序方向：asc / desc"),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """单线程的消息与卡片快照（按 seq 升序）。"""
    if order not in ("asc", "desc"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="order 仅支持 asc / desc",
        )
    await assert_thread_visible(
        thread_id,
        current_user["sub"],
        current_user.get("role") or "",
    )
    rows = await MessageRecorder.list_by_thread(thread_id)
    total = len(rows)
    page = rows[offset : offset + limit]
    if order == "desc":
        page = list(reversed(page))
    return {
        "thread_id": thread_id,
        "items": page,
        "total": total,
        "has_more": offset + limit < total,
        "server_time": _now_iso(),
    }


@thread_router.get("/sessions/{session_id}/messages")
async def list_session_messages(
    session_id: str,
    limit: int = Query(300, ge=1, le=MAX_MESSAGE_LIMIT),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """整个会话的消息（跨线程，按 seq 全序）。

    一个会话可同时包含"对话线程"与"业务工单线程"，其消息在时间轴上交错；
    seq 是全局单调序号，保证多次拉取的顺序完全一致。
    只返回当前用户可见线程的消息。
    """
    user_id = current_user["sub"]
    user_role = current_user.get("role") or ""
    threads = await ThreadMappingStore.list_visible(
        user_id,
        user_role,
        session_id=session_id,
        limit=500,
    )
    thread_ids = [item["thread_id"] for item in threads]
    if not thread_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="资源不存在",
        )
    rows = await MessageRecorder.list_by_threads(
        thread_ids,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(rows) > limit
    return {
        "session_id": session_id,
        "thread_ids": thread_ids,
        "items": rows[:limit],
        "has_more": has_more,
        "server_time": _now_iso(),
    }


__all__ = [
    "thread_router",
]
