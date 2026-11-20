"""通用工作流事件订阅端点（SSE）。

GET /api/v1/workflow/events —— 业务中立的状态变更推送通道。

本文件属通用底座：只负责"把事件送到订阅者手上"，
不理解任何业务语义（不知道谁被决策、决策成什么业务状态）。
事件的受众与含义均由发布方（业务插件）填充。

推送时序约定：发布方必须在数据库 commit() 之后才广播，
否则订阅者可能收到一条尚未落库、回读不到的状态。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.core.security import get_current_user
from app.core.workflow.event_bus import (
    HEARTBEAT_INTERVAL_SECONDS,
    WORKFLOW_EVENT_BUS,
)

logger = logging.getLogger(__name__)

workflow_router = APIRouter(tags=["Universal-Workflow"])

# 订阅就绪事件名（业务中立）：连接建立即下发，供前端确认通道已建立
EVENT_READY = "ready"

# 心跳注释帧：纯注释行，不触发前端 onmessage，仅用于保活
HEARTBEAT_COMMENT = ": ping"

# SSE 响应头：禁用缓冲并保持长连接，避免中间代理提前断开
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _sse_frame(event_name: str, payload: dict[str, Any]) -> str:
    """渲染一帧 SSE（event 名 + data JSON）。"""
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_name}\ndata: {body}\n\n"


@workflow_router.get("/workflow/events")
async def workflow_events(
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """订阅工作流状态变更事件（进程内广播）。

    - 连接建立即下发 `event: ready`；
    - 超过心跳间隔无事件则下发注释帧保活（防中间代理断连）；
    - 客户端断开时自动注销订阅，不留悬挂队列。
    """
    user_id = str(current_user.get("sub") or "")
    role = str(current_user.get("role") or "")

    async def event_stream() -> AsyncIterator[str]:
        sub_id, queue = WORKFLOW_EVENT_BUS.subscribe(user_id, role)
        try:
            yield _sse_frame(
                EVENT_READY,
                {"event": EVENT_READY, "subscriber_id": sub_id},
            )
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_INTERVAL_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield f"{HEARTBEAT_COMMENT}\n\n"
                    continue
                yield _sse_frame(event.event, event.to_payload())
        except asyncio.CancelledError:
            # 客户端断连：交由 finally 注销订阅
            raise
        finally:
            WORKFLOW_EVENT_BUS.unsubscribe(sub_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


__all__ = ["EVENT_READY", "workflow_router"]
