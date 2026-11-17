"""通用 A2UI 对话网关（行业中立的脚手架能力）。

提供 POST /agent/chat-stream：以 SSE 单向推流扁平事件。

职责收口（全部在通用层，业务插件只提供语义）：
1. 请求解析与鉴权；
2. 线程命名空间隔离（聊天线程统一加 chat_ 前缀，与业务工单线程天然区分）；
3. 取活跃插件的 chat_handler 并驱动其事件流；
4. 逐事件 SSE 分帧推送；
5. 消息与卡片落库（MessageRecorder；卡片"先落库再推帧"，保证推给用户的即已持久化）；
6. 执行留痕（AIExecutionTrace —— 此前有表无写入）；
7. 始终以 done（正常）或 error（异常/未接入）收口，绝不悬挂；
8. 整轮幂等：客户端幂等键命中即识别为重放，不再重复执行业务处理器。

本模块不包含任何业务词汇；业务语义完全由插件的 chat_handler 决定。
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.core.database import AsyncSessionLocal
from app.core.plugin.loader import PluginLoader
from app.core.plugin.manifest import ChatContext
from app.core.security import get_current_user
from app.core.workflow import (
    CHAT_THREAD_PREFIX,
    MessageRecorder,
    ThreadMappingStore,
)
from app.core.workflow.thread_mapping_store import (
    ROLE_SYSTEM,
    STATUS_ACTIVE,
)
from app.models.ai_schema import AIExecutionTrace
from app.schemas.a2ui import (
    A2UIEventType,
    AgentChatEvent,
    AgentChatRequest,
)

logger = logging.getLogger(__name__)

agent_router = APIRouter(prefix="/agent", tags=["Universal-Agent"])

# 幂等键前缀
CHAT_IDEMPOTENCY_PREFIX = "chat:"

# 终结码：整轮对话此前已提交，本次请求为重放
TURN_ALREADY_SUBMITTED = "TURN_ALREADY_SUBMITTED"

# 消息角色
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

# 固定文案
NO_HANDLER_MESSAGE = "当前业务插件未接入对话能力，请改用业务表单提交。"
EMPTY_INPUT_MESSAGE = "消息内容为空，请输入内容后再发送。"

# 线程展示标题的最大长度
TITLE_MAX_LENGTH = 60

# SSE 响应头（禁用 Nginx 缓冲，保证逐字推流）
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def build_chat_thread_id(thread_id: str) -> str:
    """聊天线程统一加命名空间前缀（幂等）。"""
    if thread_id.startswith(CHAT_THREAD_PREFIX):
        return thread_id
    return f"{CHAT_THREAD_PREFIX}{thread_id}"


class TraceWriter:
    """执行留痕写入器（通用层实现 ai_execution_traces 落库）。"""

    def __init__(self, thread_id: str) -> None:
        self._thread_id = thread_id

    async def record(
        self,
        node_name: str,
        model_name: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
        total_cost: float = 0.0,
        tool_calls: dict[str, Any] | None = None,
        error_log: str | None = None,
    ) -> None:
        """写入一行节点级留痕；写失败仅告警，不影响主对话链路。"""
        trace = AIExecutionTrace(
            thread_id=self._thread_id,
            node_name=node_name,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_cost=total_cost,
            latency_ms=latency_ms,
            tool_calls=tool_calls,
            error_log=error_log,
        )
        try:
            async with AsyncSessionLocal() as session:
                session.add(trace)
                await session.commit()
        except Exception:  # noqa: BLE001 —— 留痕属旁路审计，失败不阻断对话
            logger.exception("[对话网关] 执行留痕写入失败 node=%s", node_name)


def _sse_frame(event_name: str, payload: dict[str, Any]) -> str:
    """渲染一帧 SSE（event 名 + data JSON）。"""
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_name}\ndata: {body}\n\n"


def _sse_event(event: AgentChatEvent) -> str:
    """把扁平事件渲染为 SSE 帧。"""
    payload = event.model_dump(mode="json", exclude_none=True)
    return _sse_frame(event.type.value, payload)


def _derive_title(message: str) -> str | None:
    """从首条用户消息推导线程展示标题（通用语义：取首行并截断）。

    核心层只做"截断首行"这一普适动作，不理解任何业务语义。
    """
    lines = (message or "").strip().splitlines()
    first = lines[0].strip() if lines else ""
    if not first:
        return None
    return first[:TITLE_MAX_LENGTH]


async def _record_card_safely(
    thread_id: str, card_type: str, data: dict[str, Any]
) -> None:
    """卡片快照落库（旁路语义：失败仅告警，绝不阻断对话流）。

    card_payload 内保留 card_type 键，与前端实时构造的形态逐字段一致，
    保证"实时态"与"回读态"的卡片载荷完全相同（刷新后卡片不变形）。
    """
    try:
        await MessageRecorder.record_card(
            thread_id=thread_id,
            role=ROLE_ASSISTANT,
            card_type=card_type,
            card_payload={**data, "card_type": card_type},
        )
    except Exception:  # noqa: BLE001 —— 落库属旁路，失败不中断流
        logger.exception("[对话网关] 卡片快照落库失败 card_type=%s", card_type)


@agent_router.post("/chat-stream")
async def chat_stream(
    req: AgentChatRequest,
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """A2UI 流式对话入口（SSE）。

    - 插件未提供 chat_handler → 以 error 事件明确告知（不 500）。
    - 事件序列：token* → (ui_card)? → done；异常时以 error 收口。
    """
    manifest = PluginLoader.active()
    handler = manifest.chat_handler

    user_id = current_user["sub"]
    user_role = current_user.get("role") or ROLE_SYSTEM
    session_id = req.session_id or req.thread_id
    physical_thread_id = build_chat_thread_id(req.thread_id)

    # 建档（通用层封装，幂等）
    await ThreadMappingStore.create_mapping(
        thread_id=physical_thread_id,
        session_id=session_id,
        creator_id=user_id,
        creator_role=user_role,
        assigned_role=ROLE_SYSTEM,
        status=STATUS_ACTIVE,
        idempotency_key=f"{CHAT_IDEMPOTENCY_PREFIX}{physical_thread_id}",
        title=_derive_title(req.message),
    )

    # 用户消息落库（带客户端幂等键：命中唯一索引说明该轮此前已提交）
    replayed = False
    if req.message.strip():
        persisted = await MessageRecorder.record_text(
            thread_id=physical_thread_id,
            role=ROLE_USER,
            content=req.message,
            client_msg_id=req.client_msg_id,
        )
        if persisted is None and req.client_msg_id:
            replayed = True

    async def event_stream() -> AsyncIterator[str]:
        # 整轮重放：该轮此前已提交，不再执行业务处理器
        # （避免重复消耗 LLM 与重复产出内容），仅以终结码收口
        if replayed:
            yield _sse_frame(
                A2UIEventType.DONE.value,
                {"type": "done", "code": TURN_ALREADY_SUBMITTED},
            )
            return

        if handler is None:
            yield _sse_frame(
                A2UIEventType.ERROR.value, {"message": NO_HANDLER_MESSAGE}
            )
            yield _sse_frame(A2UIEventType.DONE.value, {"type": "done"})
            return

        context = ChatContext(
            thread_id=physical_thread_id,
            session_id=session_id,
            user_id=user_id,
            user_role=user_role,
            trace_sink=TraceWriter(thread_id=physical_thread_id),
        )

        # 分段缓冲：文本先缓冲，遇卡片先 flush 文本再落卡片，
        # 保证服务端 seq 顺序 == 用户实时所见顺序
        pending_text: list[str] = []

        async def flush_text() -> None:
            """把缓冲的助手文本落库（落库失败仅告警，不中断流）。"""
            if not pending_text:
                return
            answer = "".join(pending_text)
            pending_text.clear()
            if not answer:
                return
            try:
                await MessageRecorder.record_text(
                    thread_id=physical_thread_id,
                    role=ROLE_ASSISTANT,
                    content=answer,
                )
            except Exception:  # noqa: BLE001 —— 落库失败不得中断流
                logger.exception("[对话网关] 助手消息落库失败")

        failure: str | None = None
        try:
            async for event in handler(context, req):
                if event.type == A2UIEventType.TOKEN and event.content:
                    pending_text.append(event.content)
                elif event.type == A2UIEventType.UI_CARD and event.card_type:
                    # 先落文本再落卡片：顺序即用户所见顺序
                    await flush_text()
                    await _record_card_safely(
                        physical_thread_id, event.card_type, event.data or {}
                    )
                yield _sse_event(event)
        except Exception as exc:  # noqa: BLE001 —— 异常统一降级为 error 事件
            logger.exception("[对话网关] 插件对话处理器异常")
            failure = f"对话执行异常：{type(exc).__name__}"
        finally:
            # 断流 / 异常路径同样尽量保留已产出内容
            await flush_text()

        if failure is not None:
            yield _sse_frame(A2UIEventType.ERROR.value, {"message": failure})
            return
        yield _sse_frame(A2UIEventType.DONE.value, {"type": "done"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


__all__ = [
    "CHAT_THREAD_PREFIX",
    "TURN_ALREADY_SUBMITTED",
    "TraceWriter",
    "agent_router",
    "build_chat_thread_id",
]
