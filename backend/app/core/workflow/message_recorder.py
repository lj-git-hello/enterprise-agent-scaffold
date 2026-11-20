"""通用 A2UI 卡片 / 消息快照持久化服务。"""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.database import AsyncSessionLocal
from app.models.ai_schema import AIChatMessage

# 卡片消息类型
KIND_CARD = "card"
KIND_TEXT = "text"


class MessageRecorder:
    """无状态消息记录器（静态方法）。"""

    @staticmethod
    async def record_card(
        thread_id: str,
        role: str,
        card_type: str,
        card_payload: dict[str, Any],
        retry_payload: dict[str, Any] | None = None,
        content: str | None = None,
        *,
        client_msg_id: str | None = None,
    ) -> AIChatMessage | None:
        """向 ai_chat_messages 写入结构化卡片与自包含重试载荷并提交。

        client_msg_id 为客户端幂等键：命中部分唯一索引时视为重复投递，
        跳过写入并返回 None（调用方据此识别重放）。
        """
        message = AIChatMessage(
            thread_id=thread_id,
            role=role,
            kind=KIND_CARD,
            content=content,
            card_type=card_type,
            card_payload=card_payload,
            retry_payload=retry_payload,
            client_msg_id=client_msg_id,
        )
        async with AsyncSessionLocal() as session:
            session.add(message)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await _client_msg_exists(session, thread_id, client_msg_id):
                    return None
                raise
        return message

    @staticmethod
    async def record_text(
        thread_id: str,
        role: str,
        content: str,
        *,
        client_msg_id: str | None = None,
    ) -> AIChatMessage | None:
        """写入普通文本消息并提交。

        client_msg_id 幂等语义同 record_card：重复投递返回 None。
        """
        message = AIChatMessage(
            thread_id=thread_id,
            role=role,
            kind=KIND_TEXT,
            content=content,
            client_msg_id=client_msg_id,
        )
        async with AsyncSessionLocal() as session:
            session.add(message)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await _client_msg_exists(session, thread_id, client_msg_id):
                    return None
                raise
        return message

    @staticmethod
    async def exists_card(thread_id: str, card_type: str) -> bool:
        """判断线程内是否已存在指定类型的卡片（用于落卡防重）。

        请求重试或流程重放时，调用方据此跳过重复落卡。
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT 1 FROM ai_chat_messages "
                    "WHERE thread_id = :thread_id AND card_type = :card_type "
                    "LIMIT 1"
                ),
                {"thread_id": thread_id, "card_type": card_type},
            )
            return result.first() is not None

    @staticmethod
    async def update_card_payload(
        thread_id: str,
        card_type: str,
        patch: dict[str, Any],
        *,
        expected_card_version: int | None = None,
    ) -> bool:
        """原地改写线程内最后一条同类型卡片的载荷；返回是否命中并更新。

        定位规则：``ORDER BY seq DESC NULLS LAST, created_at DESC LIMIT 1``。
        seq 可能为 NULL 的存量行必须排到最后，不得被误选为"最新一张"。

        合并语义：card_payload 顶层浅合并（patch 同名键覆盖），其余键保留；
        改写后 card_version 递增 1，updated_at 置为当前时刻。

        expected_card_version 不为 None 时启用乐观锁：仅当库内版本与期望
        一致才更新，否则不更新并返回 False（调用方据此识别并发冲突）。
        """
        sql = text(
            "UPDATE ai_chat_messages SET "
            "  card_payload = COALESCE(card_payload, '{}'::jsonb) "
            "                || CAST(:patch AS jsonb), "
            "  card_version = card_version + 1, "
            "  updated_at = now() "
            "WHERE id = ("
            "  SELECT id FROM ai_chat_messages "
            "  WHERE thread_id = :thread_id AND card_type = :card_type "
            "  ORDER BY seq DESC NULLS LAST, created_at DESC "
            "  LIMIT 1"
            ") "
            "AND (CAST(:expected_version AS integer) IS NULL "
            "     OR card_version = CAST(:expected_version AS integer))"
        )
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                sql,
                {
                    "thread_id": thread_id,
                    "card_type": card_type,
                    "patch": json.dumps(patch, ensure_ascii=False),
                    "expected_version": expected_card_version,
                },
            )
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    async def get_latest_card(
        thread_id: str,
        card_type: str,
    ) -> dict[str, Any] | None:
        """读取线程内最后一条同类型卡片；无记录返回 None。

        定位规则与 update_card_payload 一致：``ORDER BY seq DESC NULLS LAST,
        created_at DESC LIMIT 1``，seq 为 NULL 的存量行排到最后。
        """
        sql = text(
            "SELECT id, seq, card_type, card_payload, card_version, "
            "created_at, updated_at "
            "FROM ai_chat_messages "
            "WHERE thread_id = :thread_id AND card_type = :card_type "
            "ORDER BY seq DESC NULLS LAST, created_at DESC "
            "LIMIT 1"
        )
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    sql, {"thread_id": thread_id, "card_type": card_type}
                )
            ).first()
        if row is None:
            return None
        return _to_message_dict(row)

    @staticmethod
    async def list_by_thread(thread_id: str) -> list[dict[str, Any]]:
        """按 thread_id 读取消息 / 卡片快照，按 seq 升序；无记录返回 []。

        JSONB 列由驱动还原为 dict；时间转 ISO 字符串，保证可 JSON 序列化。
        """
        sql = text(
            "SELECT id, seq, thread_id, role, kind, content, card_type, "
            "card_payload, retry_payload, card_version, created_at, updated_at "
            "FROM ai_chat_messages "
            "WHERE thread_id = :thread_id "
            "ORDER BY seq ASC"
        )
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sql, {"thread_id": thread_id})

        return [_to_message_dict(row) for row in rows]

    @staticmethod
    async def list_by_threads(
        thread_ids: list[str],
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按线程集合读取消息，按 seq 升序（跨线程稳定全序）；无记录返回 []。

        seq 是全局单调序号：created_at 为 transaction timestamp、同值行序
        不确定，跨线程交错排序必须依赖 seq。
        """
        if not thread_ids:
            return []
        sql = text(
            "SELECT id, seq, thread_id, role, kind, content, card_type, "
            "card_payload, retry_payload, card_version, created_at, updated_at "
            "FROM ai_chat_messages "
            "WHERE thread_id = ANY(:tids) "
            "ORDER BY seq ASC "
            "LIMIT CAST(:limit AS integer) OFFSET CAST(:offset AS integer)"
        )
        async with AsyncSessionLocal() as session:
            rows = await session.execute(
                sql,
                {
                    "tids": list(thread_ids),
                    "limit": max(1, min(limit, 500)),
                    "offset": max(0, offset),
                },
            )

        return [_to_message_dict(row) for row in rows]


async def _client_msg_exists(
    session: Any,
    thread_id: str,
    client_msg_id: str | None,
) -> bool:
    """判断是否因客户端幂等键冲突而失败（用于区分幂等命中与真实约束错误）。"""
    if not client_msg_id:
        return False
    result = await session.execute(
        text(
            "SELECT 1 FROM ai_chat_messages "
            "WHERE thread_id = :thread_id AND client_msg_id = :cmid "
            "LIMIT 1"
        ),
        {"thread_id": thread_id, "cmid": client_msg_id},
    )
    return result.first() is not None


def _to_message_dict(row: Any) -> dict[str, Any]:
    """把查询行转成可 JSON 序列化的消息字典（时间转 ISO 字符串）。"""
    item = dict(row._mapping)
    created_at = item.get("created_at")
    if created_at is not None:
        item["created_at"] = created_at.isoformat()
    updated_at = item.get("updated_at")
    if updated_at is not None:
        item["updated_at"] = updated_at.isoformat()
    return item


__all__ = [
    "KIND_CARD",
    "KIND_TEXT",
    "MessageRecorder",
]
