"""通用线程可见性策略（跨业务、零业务语义）。

规则（与具体业务角色无关，只认"谁与这条线程有关系"）：
1. creator_id 命中  → 发起人可见
2. operator_id 命中 → 实际处理人可见
3. assigned_role 命中当前用户角色，且 assigned_role 不是系统占位角色
   → 待办责任人可见

其余不可见。**不可见与不存在统一按 404 处理**，避免泄露资源存在性。

本模块只做通用关系判定；各业务插件自行解释角色名的业务含义。
"""

from typing import Any

from fastapi import HTTPException, status

from app.core.workflow.thread_mapping_store import ROLE_SYSTEM, ThreadMappingStore


def can_view(mapping: dict[str, Any] | None, user_id: str, user_role: str) -> bool:
    """判断当前用户是否可见该线程映射。"""
    if not mapping:
        return False
    if mapping.get("creator_id") == user_id:
        return True
    operator_id = mapping.get("operator_id")
    if operator_id and operator_id == user_id:
        return True
    assigned_role = mapping.get("assigned_role") or ""
    if (
        assigned_role
        and assigned_role != ROLE_SYSTEM
        and assigned_role == (user_role or "")
    ):
        return True
    return False


async def assert_thread_visible(
    thread_id: str,
    user_id: str,
    user_role: str,
) -> dict[str, Any]:
    """读取线程映射并校验可见性；不可见或不存在统一抛 404。

    返回可见时的映射快照，供调用方直接使用。
    """
    mapping = await ThreadMappingStore.get(thread_id)
    if not can_view(mapping, user_id, user_role):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="资源不存在",
        )
    assert mapping is not None  # can_view 已保证非空
    return mapping


__all__ = [
    "assert_thread_visible",
    "can_view",
]
