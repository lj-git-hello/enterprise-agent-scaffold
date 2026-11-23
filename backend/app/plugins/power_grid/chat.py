"""电网安规问答对话处理器（垂直插件）。

复用通用 RAG 检索（core.rag）与通用 LLM 配置口径（core.llm 的环境变量键），
输出统一的扁平 A2UI 事件：

- token：LLM 增量文本（逐字推流）
- ui_card：规程引文卡片（card_type=power_grid_citations）

SSE 分帧、消息落库与执行留痕全部由通用对话网关负责，本模块只管语义。
大模型未配置 / 超时 / 异常时走确定性降级（规程条文分片推流），绝不卡死、
绝不向网关抛异常（网关异常路径不会补发 done 帧）。
"""

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import openai
from openai import AsyncOpenAI

from app.core.llm import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MAX_RETRIES,
    ENV_TIMEOUT,
    resolve_model_name,
)
from app.core.plugin.manifest import ChatContext
from app.core.rag.vector_service import VectorStoreService
from app.plugins.power_grid.constants import (
    POWER_CARD_TYPE_CITATIONS,
    POWER_RAG_BIZ_TAG,
)
from app.schemas.a2ui import A2UIEventType, AgentChatEvent, AgentChatRequest

# ---- 检索参数 ----
RAG_TOP_K = 3
RAG_MIN_SIMILARITY = 0.3

# ---- 模型参数 ----
QA_TEMPERATURE = 0.1
# Trace 节点名（通用留痕口径，与业务图节点名无关）
CHAT_TRACE_NODE = "chat.stream"

# ---- 降级推流参数 ----
# 每帧字符数与帧间隔：把规程条文切成小片逐帧下发，模拟打字机观感。
# 前端在任何网络环境下都能看到丝滑流式，而不是整段蹦出。
DEGRADED_CHUNK_SIZE = 6
DEGRADED_CHUNK_DELAY = 0.02

# ---- 固定文案 ----
NO_RESULT_ANSWER = (
    "知识库中暂未检索到与该问题直接相关的规程依据，"
    "请核实问题或联系管理员确认。"
)
EMPTY_QUESTION_MESSAGE = "消息内容为空，请输入需要咨询的安规问题。"
DEGRADED_ANSWER_HEADER = "依据规程要求，相关条款如下："
LLM_NOT_CONFIGURED = "大模型未配置（缺少 API Key）"
STREAM_INTERRUPTED_MESSAGE = "回答生成中断，已补充规程原文。"

# 通用问答系统提示（仅依据召回条目作答，强制标注出处，无依据则拒答）
QA_SYSTEM_PROMPT = (
    "你是一个严谨的电力安规知识问答助手。只能且必须依据用户提供的【参考条目】"
    "回答问题，不得使用参考条目之外的信息，不得自行推断。"
    "严禁编造规程外的数字、条款、编号或数值；若某项依据在参考条目中没有出现，"
    "就必须明确说明没有该依据。"
    "回答中需标注所引用条目的出处编号与章节。"
    "若参考条目不足以回答问题，请明确说明现有资料中没有相关依据，不要编造。"
)


def _build_citations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把检索结果整理为引文条目（结构化，可直接下发为卡片负载）。"""
    citations: list[dict[str, Any]] = []
    for row in rows:
        citations.append(
            {
                "doc_code": row.get("doc_code"),
                "section_title": row.get("section_title"),
                "content": row.get("content"),
                "similarity": round(float(row.get("similarity") or 0.0), 4),
            }
        )
    return citations


def _build_context(citations: list[dict[str, Any]]) -> str:
    """把引文组装为提供给模型的参考文本。"""
    blocks = []
    for index, item in enumerate(citations, start=1):
        title = item.get("section_title")
        blocks.append(
            f"[条目{index}] 出处：{item.get('doc_code')}"
            f"{' / ' + title if title else ''}\n"
            f"内容：{item.get('content')}"
        )
    return "\n\n".join(blocks)


def _build_degraded_answer(citations: list[dict[str, Any]]) -> str:
    """大模型不可用 / 调用失败时的确定性带引文答复。"""
    lines = [DEGRADED_ANSWER_HEADER]
    for item in citations:
        title = item.get("section_title")
        source = f"【{item.get('doc_code')}{(' / ' + title) if title else ''}】"
        lines.append(f"{source}\n{item.get('content')}")
    return "\n".join(lines)


def _citation_event(citations: list[dict[str, Any]]) -> AgentChatEvent:
    """构造引文卡片事件。"""
    return AgentChatEvent(
        type=A2UIEventType.UI_CARD,
        card_type=POWER_CARD_TYPE_CITATIONS,
        data={"citations": citations},
    )


def _build_openai_client() -> AsyncOpenAI | None:
    """构建 OpenAI 兼容客户端；未配置 API Key 时返回 None（走确定性降级）。

    环境变量名沿用 core.llm 的权威常量（OPENAI_API_KEY / OPENAI_BASE_URL /
    LLM_TIMEOUT / LLM_MAX_RETRIES），密钥只来自环境变量，严禁硬编码。
    """
    api_key = os.getenv(ENV_API_KEY, "").strip()
    if not api_key:
        return None

    timeout = float(os.getenv(ENV_TIMEOUT, "") or DEFAULT_TIMEOUT_SECONDS)
    max_retries = int(os.getenv(ENV_MAX_RETRIES, "") or DEFAULT_MAX_RETRIES)

    return AsyncOpenAI(
        api_key=api_key,
        base_url=os.getenv(ENV_BASE_URL, "").strip() or None,
        timeout=timeout,
        max_retries=max_retries,
    )


def _build_messages(
    question: str,
    citations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """组装原生 OpenAI 消息体：系统提示严格限定依据召回条目作答。"""
    return [
        {"role": "system", "content": QA_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"问题：{question}\n\n参考条目：\n{_build_context(citations)}",
        },
    ]


async def _stream_model_answer(
    client: AsyncOpenAI,
    messages: list[dict[str, str]],
    stats: dict[str, int],
) -> AsyncIterator[str]:
    """调用大模型产出增量文本。

    供应商兼容：部分 OpenAI 兼容网关不识别 `stream_options`（会返回 400），
    此时自动去掉该参数重试一次 —— 观测参数不得反噬可用性。
    """
    last_error: Exception | None = None

    for extra_kwargs in (
        {"stream_options": {"include_usage": True}},
        {},
    ):
        try:
            stream = await client.chat.completions.create(
                model=resolve_model_name(),
                messages=messages,
                temperature=QA_TEMPERATURE,
                stream=True,
                **extra_kwargs,
            )
        except openai.BadRequestError as exc:
            # 仅当是参数不兼容才换参重试；其余 4xx 直接上抛
            last_error = exc
            continue

        async for chunk in stream:
            # usage 帧只带计量信息，choices 可能为空
            if chunk.choices:
                delta = chunk.choices[0].delta
                text = delta.content if delta is not None else None
                if text:
                    yield text
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                stats["prompt_tokens"] = max(
                    stats["prompt_tokens"], int(usage.prompt_tokens or 0)
                )
                stats["completion_tokens"] = max(
                    stats["completion_tokens"], int(usage.completion_tokens or 0)
                )
        return

    raise last_error if last_error is not None else RuntimeError("流式创建失败")


async def _iter_degraded_chunks(text: str) -> AsyncIterator[str]:
    """把确定性降级文本按固定字数切片逐帧产出（模拟打字机观感）。

    每帧之间留出固定间隔：即使上游完全不可用，前端依然是逐字浮现，
    而不是一次性整段蹦出。
    """
    for index in range(0, len(text), DEGRADED_CHUNK_SIZE):
        await asyncio.sleep(DEGRADED_CHUNK_DELAY)
        yield text[index : index + DEGRADED_CHUNK_SIZE]


async def power_grid_chat_handler(
    context: ChatContext,
    request: AgentChatRequest,
) -> AsyncIterator[AgentChatEvent]:
    """安规 RAG 问答：召回规程 → 流式生成 → 附引文卡片。"""
    question = (request.message or "").strip()
    if not question:
        yield AgentChatEvent(
            type=A2UIEventType.ERROR,
            message=EMPTY_QUESTION_MESSAGE,
        )
        return

    # ---- 1. 召回规程条款（检索异常不阻断，转确定性降级） ----
    try:
        rows = await VectorStoreService().search_advanced(
            query=question,
            biz_tag=POWER_RAG_BIZ_TAG,
            top_k=RAG_TOP_K,
            min_similarity=RAG_MIN_SIMILARITY,
        )
    except Exception:  # noqa: BLE001 —— 检索不可用时降级为"无依据"答复
        rows = []

    if not rows:
        yield AgentChatEvent(type=A2UIEventType.TOKEN, content=NO_RESULT_ANSWER)
        return

    citations = _build_citations(rows)

    # ---- 2. 大模型流式生成（未配置 / 异常一律确定性降级） ----
    started = time.perf_counter()
    stats = {"prompt_tokens": 0, "completion_tokens": 0}
    produced = False
    error_text: str | None = None

    client: AsyncOpenAI | None = None
    try:
        client = _build_openai_client()
    except Exception as exc:  # noqa: BLE001 —— 客户端构造本身也可能失败（证书/代理/依赖）
        client = None
        error_text = f"客户端初始化失败：{type(exc).__name__}: {exc}"

    if client is None:
        error_text = error_text or LLM_NOT_CONFIGURED
    else:
        try:
            async for text in _stream_model_answer(
                client,
                _build_messages(question, citations),
                stats,
            ):
                produced = True
                yield AgentChatEvent(type=A2UIEventType.TOKEN, content=text)
        except Exception as exc:  # noqa: BLE001 —— 超时/限流/参数不兼容统一降级
            error_text = f"{type(exc).__name__}: {exc}"
        finally:
            # 释放 HTTP 连接；关闭异常不得污染已产出的正文
            with contextlib.suppress(Exception):
                await client.close()

    # ---- 3. 确定性降级兜底：规程条文按固定字数切片逐帧推流 ----
    # 覆盖三种情形：未配置模型、调用失败、以及中途断流（已产出部分正文）。
    # 中途断流时保留已输出内容，降级全文续推，保证用户始终看到完整依据。
    if error_text is not None or not produced:
        error_text = error_text or "大模型未返回任何内容"
        async for chunk in _iter_degraded_chunks(_build_degraded_answer(citations)):
            yield AgentChatEvent(type=A2UIEventType.TOKEN, content=chunk)

    latency_ms = int((time.perf_counter() - started) * 1000)

    # ---- 4. 执行留痕（写入由通用层完成，本模块只上报观测数据） ----
    if context.trace_sink is not None:
        await context.trace_sink.record(
            node_name=CHAT_TRACE_NODE,
            model_name=resolve_model_name(),
            prompt_tokens=stats["prompt_tokens"],
            completion_tokens=stats["completion_tokens"],
            latency_ms=latency_ms,
            error_log=error_text,
        )

    # 已产出正文后中断：明确告知用户，同时说明已补充规程原文
    if error_text is not None and produced:
        yield AgentChatEvent(
            type=A2UIEventType.ERROR,
            message=STREAM_INTERRUPTED_MESSAGE,
        )

    yield _citation_event(citations)


__all__ = ["CHAT_TRACE_NODE", "power_grid_chat_handler"]
