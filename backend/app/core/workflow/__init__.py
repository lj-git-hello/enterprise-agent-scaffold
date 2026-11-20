"""通用工作流底座（跨业务可复用）。

对外暴露：
- BaseWorkflowState：通用运行态基类
- GraphRegistry：工作流图注册中心（依赖反转）
- HITLController：CAS 并发 / 待办 / 终态
- MessageRecorder：卡片 / 消息持久化
- TraceRecorder：执行留痕写入（视图投影改写前的真相留存）
- ThreadMappingStore：线程映射建档 / 归档 / 可见列表
- EventBus / WORKFLOW_EVENT_BUS：进程内状态变更事件总线（跨端推送）
- assert_thread_visible / can_view：通用线程可见性策略
"""

from app.core.workflow.base_state import BaseWorkflowState
from app.core.workflow.event_bus import (
    EVENT_STATUS_CHANGED,
    WORKFLOW_EVENT_BUS,
    EventBus,
    WorkflowEvent,
)
from app.core.workflow.graph_registry import (
    DEFAULT_GRAPH_NAME,
    GraphRegistry,
)
from app.core.workflow.hitl import HITLController
from app.core.workflow.message_recorder import MessageRecorder
from app.core.workflow.thread_access import assert_thread_visible, can_view
from app.core.workflow.thread_mapping_store import (
    CHAT_THREAD_PREFIX,
    ThreadMappingStore,
)
from app.core.workflow.trace_recorder import TraceRecorder

__all__ = [
    "EVENT_STATUS_CHANGED",
    "WORKFLOW_EVENT_BUS",
    "BaseWorkflowState",
    "CHAT_THREAD_PREFIX",
    "EventBus",
    "GraphRegistry",
    "DEFAULT_GRAPH_NAME",
    "HITLController",
    "MessageRecorder",
    "ThreadMappingStore",
    "TraceRecorder",
    "WorkflowEvent",
    "assert_thread_visible",
    "can_view",
]