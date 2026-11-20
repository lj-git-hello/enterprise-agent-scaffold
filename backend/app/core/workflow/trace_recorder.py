"""通用执行留痕写入器（ai_execution_traces）。

留痕表是 append-only 的真相来源：卡片等视图投影一旦被原地改写，旧载荷即
从 ai_chat_messages 中消失，但审计链路要求"改写前是什么"可回溯，因此
所有不可逆的视图变更都必须在此落一条留痕。

写失败仅告警，不阻断主流程（留痕属旁路审计）。
"""

import logging
from typing import Any

from app.core.database import AsyncSessionLocal
from app.models.ai_schema import AIExecutionTrace

logger = logging.getLogger(__name__)

# 非模型驱动的流程节点（人工审批、状态流转）统一使用的 model_name 占位
TRACE_MODEL_NON_LLM = "n/a"


class TraceRecorder:
    """无状态执行留痕记录器（静态方法）。"""

    @staticmethod
    async def record(
        thread_id: str,
        node_name: str,
        *,
        model_name: str = TRACE_MODEL_NON_LLM,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
        total_cost: float = 0.0,
        tool_calls: dict[str, Any] | None = None,
        error_log: str | None = None,
    ) -> None:
        """写入一行节点级留痕；写失败仅告警，不影响主链路。"""
        trace = AIExecutionTrace(
            thread_id=thread_id,
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
        except Exception:  # noqa: BLE001 —— 留痕属旁路审计，失败不阻断主流程
            logger.exception("[工作流] 执行留痕写入失败 node=%s", node_name)


__all__ = [
    "TRACE_MODEL_NON_LLM",
    "TraceRecorder",
]