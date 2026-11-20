"""通用工作流事件总线（进程内发布订阅）。

底座只提供"事件如何流动"的能力，不理解任何业务语义：
- 事件载荷字段一律业务中立：event / thread_id / status / actor_id /
  occurred_at / audience / session_id；
- 订阅者以 user_id 与 role 标识，受众命中规则为纯字符串比较；
- 受众为空表示不限受众（广播）。

⚠️ 进程内广播仅在单进程内有效：多 worker 部署时各进程互不可见。
本项目以单进程运行为准；跨进程一致性由"服务端回读 + 前端重拉"兜底。
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 状态变更事件名：全链路唯一写法（业务中立，底座与插件共用同一常量）
EVENT_STATUS_CHANGED = "workflow.status_changed"

# 心跳间隔（秒）：超过该时长无事件即下发注释帧保活
HEARTBEAT_INTERVAL_SECONDS = 15


@dataclass(frozen=True)
class WorkflowEvent:
    """工作流事件（业务中立载荷，只读不可变）。"""

    event: str
    thread_id: str
    status: str
    actor_id: str
    occurred_at: str
    """受众标识集合（user_id 或 role）；为空表示不限受众。"""
    audience: tuple[str, ...] = ()
    """会话标识（线程的会话归属）；为空表示未知。
    会话级列表（按 session_id 聚合）据此把事件定位到具体会话，
    无此字段则前端无法命中会话，只能退化为整表刷新。"""
    session_id: str = ""

    def to_payload(self) -> dict[str, Any]:
        """转为可 JSON 序列化的载荷。"""
        return {
            "event": self.event,
            "thread_id": self.thread_id,
            "status": self.status,
            "actor_id": self.actor_id,
            "occurred_at": self.occurred_at,
            "audience": list(self.audience),
            "session_id": self.session_id,
        }


class EventBus:
    """进程内事件总线（发布方与订阅方均不感知业务语义）。"""

    def __init__(self) -> None:
        # 订阅标识 → (事件队列, 订阅者身份集合)
        self._subscribers: dict[
            int, tuple[asyncio.Queue[WorkflowEvent], set[str]]
        ] = {}
        self._next_id = 1

    def subscribe(
        self, user_id: str, role: str
    ) -> tuple[int, asyncio.Queue[WorkflowEvent]]:
        """注册订阅者；返回 (订阅标识, 事件队列)。"""
        queue: asyncio.Queue[WorkflowEvent] = asyncio.Queue()
        sub_id = self._next_id
        self._next_id += 1
        self._subscribers[sub_id] = (
            queue,
            {item for item in (user_id, role) if item},
        )
        return sub_id, queue

    def unsubscribe(self, sub_id: int) -> None:
        """注销订阅者（幂等）。"""
        self._subscribers.pop(sub_id, None)

    def publish(self, event: WorkflowEvent) -> int:
        """广播事件；返回实际投递的订阅者数量。

        受众为空 = 不限受众，投递给全部订阅者；
        受众非空 = 仅投递给 user_id 或 role 命中者。
        """
        audience = set(event.audience)
        delivered = 0
        for queue, identities in list(self._subscribers.values()):
            if audience and not (identities & audience):
                continue
            try:
                queue.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:  # pragma: no cover - 队列默认无界
                logger.warning(
                    "[事件总线] 订阅者队列已满，丢弃事件 %s", event.event
                )
        return delivered

    @property
    def subscriber_count(self) -> int:
        """当前订阅者数量（运维观测用）。"""
        return len(self._subscribers)


# 进程级单例
WORKFLOW_EVENT_BUS = EventBus()


__all__ = [
    "EVENT_STATUS_CHANGED",
    "HEARTBEAT_INTERVAL_SECONDS",
    "WORKFLOW_EVENT_BUS",
    "EventBus",
    "WorkflowEvent",
]
