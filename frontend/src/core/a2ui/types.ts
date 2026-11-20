/**
 * a2ui 类型 —— 与后端 ai_chat_messages 记录结构对齐。
 */

/** 消息种类：文本 / 卡片 */
export type MessageKind = 'text' | 'card'

/** 消息记录（对应后端 AIChatMessage） */
export interface ChatMessageRecord {
  id: string
  thread_id: string
  role: string
  kind: MessageKind
  content: string | null
  card_type: string | null
  card_payload: Record<string, unknown> | null
  retry_payload: Record<string, unknown> | null
  created_at: string | null
  /** 全局单调序号（服务端权威排序依据；本地新建消息可缺省） */
  seq?: number
  /** 卡片视图投影版本：卡片被原地改写时递增，是卡片级乐观锁的依据 */
  card_version?: number
  /** 最近一次改写时间（服务端 ISO 串；未改写时等于写入时间） */
  updated_at?: string | null
}

/** 卡片交互上下文：卡片动作回调可据此触发重试/上报 */
export interface CardActionContext {
  threadId: string
  /** 自包含重试载荷（来自后端 retry_payload） */
  retryPayload: Record<string, unknown> | null
  /** 触发某个动作（由宿主/业务注入具体实现） */
  dispatch: (action: string, payload?: Record<string, unknown>) => void
}

/** 卡片组件接收的 props（各业务卡片统一契约） */
export interface CardComponentProps {
  message: ChatMessageRecord
  actionContext: CardActionContext
}

/**
 * ResumeActionContext —— CAS 恢复动作上下文。
 * 提交给工作流恢复端点，用于乐观锁版本校验。
 */
export interface ResumeActionContext {
  thread_id: string
  lock_version: number
  action: string
  payload: Record<string, unknown> | null
}

/** 任务 / 会话摘要：列表与看板使用的通用结构 */
export interface TaskSummary {
  thread_id: string
  session_id: string
  creator_id: string
  assigned_role: string
  status: string
  lock_version: number
  current_node: string | null
  /** 列表展示标题（可缺省，回退展示 thread_id） */
  title?: string
  /** 线程类别：chat=对话线程；task=业务工单线程 */
  thread_kind?: string
  /** 创建时间（服务端 ISO 串） */
  created_at?: string | null
  /** 更新时间（服务端 ISO 串） */
  updated_at?: string | null
  /** 最近活跃时间（服务端 ISO 串） */
  last_activity_at?: string | null
}

/**
 * 会话摘要：左栏按 session_id 聚合后的一行。
 *
 * 一个会话内可产生多条线程（对话线程 + 若干业务线程），本结构是这些线程的
 * 聚合视图：会话可见 = 组内至少一条线程对当前用户可见。
 */
export interface SessionSummary {
  /** 会话标识 */
  session_id: string
  /** 会话展示标题（组内最早对话线程标题；无对话线程时回落最早线程） */
  title: string
  /** 代表态状态（业务语义由发布方定义，core 不解释） */
  status: string
  /** 组内关联线程数（不含对话线程） */
  related_count: number
  /** 组内线程总数 */
  thread_count: number
  /** 创建时间（服务端 ISO 串） */
  created_at?: string | null
  /** 更新时间（服务端 ISO 串） */
  updated_at?: string | null
  /** 最近活跃时间（服务端 ISO 串） */
  last_activity_at?: string | null
}

/** 线程详情（只读端点返回的线程元信息） */
export interface ThreadDetail {
  thread_id: string
  session_id: string
  creator_id: string
  creator_role: string
  assigned_role: string
  operator_id: string | null
  status: string
  lock_version: number
  current_node: string | null
  /** 外部单号弱引用（core 只当不透明字符串） */
  biz_order_id: string | null
  title: string | null
  is_archived: boolean
  created_at: string | null
  updated_at: string | null
  last_activity_at: string | null
}

/** 卡片快照（只读端点返回的卡片载荷） */
export interface CardSnapshot {
  id: number
  seq: number | null
  card_type: string | null
  card_payload: Record<string, unknown> | null
  /** 卡片视图投影版本（原地改写时递增） */
  card_version: number
  created_at: string | null
  updated_at: string | null
}

/** 只读检视目标：被点开的卡片（线程 + 卡片类型） */
export interface InspectionTarget {
  thread_id: string
  card_type: string | null
}

/**
 * WorkflowEndpoints —— 工作流端点配置（依赖反转）。
 * 由各业务插件提供具体 URL，core 视图不写死任何业务路径。
 *
 * 路径一律相对 /api（与 http baseURL 对齐）；后端 manifest 为唯一真相源。
 */
export interface WorkflowEndpoints {
  /** 待办列表端点 */
  tasks: string
  /** 某线程消息端点 */
  messages: string
  /** 恢复/审批端点 */
  resume: string
  /** 业务提报端点（可缺省：非提报型业务插件无需提供） */
  apply?: string
}
