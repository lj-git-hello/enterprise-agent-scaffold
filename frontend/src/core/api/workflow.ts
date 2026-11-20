/**
 * workflow —— 通用工作流事件订阅客户端（跨业务、零业务词汇）。
 *
 * 采用 @microsoft/fetch-event-source（与 agent.ts 同款成熟事件流驱动库）：
 * 库内置事件分帧、TCP Buffer 拼接与断网自动重连；
 * EventSourceMessage.event 透传 SSE 的 event: 字段，可直接用于事件分派。
 *
 * 事件契约与后端 app/core/workflow/event_bus.py 对齐：
 * - ready                   ：订阅就绪（连接建立即下发）
 * - workflow.status_changed ：工作流状态变更（业务中立事件名，全链路唯一写法）
 *
 * 载荷字段一律业务中立：core 只按 thread_id 关联当前会话，
 * 不理解"状态"的具体业务含义。
 */

import { fetchEventSource } from '@microsoft/fetch-event-source'

import type { StreamStatus } from './agent'
import { API_BASE, TOKEN_STORAGE_KEY } from './http'

/** 通用工作流事件端点（相对 /api） */
export const WORKFLOW_EVENTS_PATH = '/v1/workflow/events'

/** 状态变更事件名（业务中立） */
export const EVENT_STATUS_CHANGED = 'workflow.status_changed'

/** 订阅就绪事件名 */
export const EVENT_READY = 'ready'

/** 断线重连间隔（毫秒） */
export const RECONNECT_INTERVAL_MS = 2000

/** 最大重连次数（超过则停止重试，避免无限打服务端） */
export const MAX_RECONNECT_ATTEMPTS = 3

/** 工作流事件负载（字段一律业务中立） */
export interface WorkflowEventPayload {
  /** 事件名（workflow.status_changed 等） */
  event: string
  /** 关联线程标识 */
  thread_id: string
  /** 变更后的状态（业务语义由发布方定义，core 不解释） */
  status: string
  /** 触发变更的操作者标识 */
  actor_id: string
  /** 发生时间（服务端 ISO 串） */
  occurred_at: string
  /** 受众标识集合（user_id 或 role） */
  audience: string[]
}

/** 事件回调集合 */
export interface WorkflowEventHandlers {
  /** 状态变更事件 */
  onStatusChanged?: (payload: WorkflowEventPayload) => void
  /** 订阅连接状态 */
  onStatus?: (status: StreamStatus) => void
}

/** 带 fatal 标记的错误：立即终止重连 */
interface FatalStreamError extends Error {
  fatal?: boolean
}

function isFatal(error: unknown): boolean {
  return (error as FatalStreamError | null)?.fatal === true
}

/**
 * 订阅工作流状态变更事件。
 *
 * 长连接：服务端持续推流，客户端仅在断线时按间隔重连。
 * 与对话流不同：此处**不做自动重放语义**（订阅是幂等只读通道，
 * 重连后由服务端重新推送即可），因此重连不携带任何业务负载。
 *
 * @param handlers 事件回调
 * @param signal 外部取消信号（卸载 / 登出时中止）
 */
export async function subscribeWorkflowEvents(
  handlers: WorkflowEventHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const token = localStorage.getItem(TOKEN_STORAGE_KEY) ?? ''
  let retryCount = 0

  await fetchEventSource(`${API_BASE}${WORKFLOW_EVENTS_PATH}`, {
    method: 'GET',
    headers: {
      Accept: 'text/event-stream',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    signal,
    openWhenHidden: true,
    onopen: async (response) => {
      if (!response.ok) {
        const error: FatalStreamError = new Error(
          `事件订阅建立失败（HTTP ${response.status}）`,
        )
        error.fatal = true
        throw error
      }
      const contentType = response.headers.get('content-type') ?? ''
      if (!contentType.startsWith('text/event-stream')) {
        const error: FatalStreamError = new Error(
          `响应不是 SSE 流（Content-Type=${contentType}）`,
        )
        error.fatal = true
        throw error
      }
      retryCount = 0
      handlers.onStatus?.('connected')
    },
    onmessage: (message) => {
      if (!message.data) {
        return
      }
      let payload: WorkflowEventPayload
      try {
        payload = JSON.parse(message.data) as WorkflowEventPayload
      } catch {
        // 非 JSON 负载（如心跳注释）直接忽略，不中断流
        return
      }
      if (payload.event === EVENT_STATUS_CHANGED) {
        handlers.onStatusChanged?.(payload)
      }
    },
    onerror: (error) => {
      retryCount += 1
      if (isFatal(error) || retryCount > MAX_RECONNECT_ATTEMPTS) {
        handlers.onStatus?.('offline')
        // 抛出以终止库内置重连（返回 false 不会停止重试）
        throw error
      }
      handlers.onStatus?.('reconnecting')
      return RECONNECT_INTERVAL_MS
    },
    onclose: () => {
      // 服务端主动收口（正常情况下仅在服务重启时发生）：判定为断开
      handlers.onStatus?.('offline')
    },
  })
}
