/**
 * useWorkbenchStore —— 通用三栏工作台共享响应式状态（跨业务、零业务词）。
 *
 * 权威源收敛到服务端：
 * - conversationList：当前用户【我的会话】集合，**服务端为唯一真相源**，
 *   本地 IndexedDB 仅为离线兜底快照；
 * - activeMessages：当前会话的消息与卡片快照，同样服务端优先；
 * - 待办审批池：由审批池视图在 Tab 激活时独立请求后端，不进入本 store。
 *
 * 每个集合都带"来源"标记（server / cache / live / none），界面据此如实
 * 告知用户当前看到的是权威数据还是离线兜底，绝不伪装。
 */

import { reactive, watch } from 'vue'

import type {
  ChatMessageRecord,
  TaskSummary,
} from '../a2ui/types'
import { fetchSessionMessages, fetchThreads } from '../api/threads'
import {
  debounceSaveConversations,
  debounceSaveMessages,
  loadConversations,
  loadMessages,
} from '../engine/storage'
import { PluginRegistry } from '../plugins/registry'

/** 数据来源标记 */
export type DataSource = 'server' | 'cache' | 'live' | 'none'

/** apply 端点返回结构（本 store 只依赖通用形态） */
export interface ApplyResult {
  thread_id: string
  status: string
  /** 卡片快照；流程在提报阶段即终止时可能为空 */
  card?: Record<string, unknown>
  /** 文本结果（与 card 二选一）：如流程自动终止时的说明 */
  message?: string
  /** 会话 ID（服务端权威） */
  session_id?: string
  /** 卡片类型（服务端权威） */
  card_type?: string | null
  /** 线程摘要（服务端权威；左栏字段一律以它为准） */
  thread?: Partial<TaskSummary> | null
}

interface WorkbenchState {
  activeSessionId: string
  activeMessages: ChatMessageRecord[]
  conversationList: TaskSummary[]
  rightDrawerOpen: boolean
  /** 左栏数据来源 */
  conversationSource: DataSource
  /** 中栏数据来源 */
  messageSource: DataSource
  /** 最近一次成功同步时间戳 */
  lastSyncedAt: number
  /** 最近一次同步错误（显式暴露，不静默） */
  syncError: string | null
  /** 会话集合陈旧计数（递增即触发一次服务端对齐） */
  conversationsStale: number
}

const state = reactive<WorkbenchState>({
  activeSessionId: '',
  activeMessages: [],
  conversationList: [],
  rightDrawerOpen: true,
  conversationSource: 'none',
  messageSource: 'none',
  lastSyncedAt: 0,
  syncError: null,
  conversationsStale: 0,
})

/** 会话集合归属用户（切换身份时随之切换并重新拉取）。 */
let conversationOwnerId: string | null = null

/** 当前 activeMessages 归属的会话 ID（与消息内容严格对应）。 */
let messagesSessionId = ''

/**
 * 中栏消息的本地写入门控。
 *
 * 为真时不自动落盘：实时流产生的内容尚未经服务端确认，
 * 避免本地缓存出现"服务端还没有的卡片"。
 */
let suppressMessageWrite = false

/** 归一化错误描述（用于界面提示）。 */
function describeError(error: unknown): string {
  if (error && typeof error === 'object') {
    const detail = error as { status?: number; message?: string }
    if (typeof detail.status === 'number' && detail.status > 0) {
      return `服务端请求失败（HTTP ${detail.status}）`
    }
    if (detail.message) return detail.message
  }
  return '未知错误'
}

/**
 * 是否为"服务端可达但该会话暂无可见线程"的 404。
 *
 * 新建会话尚未产生任何服务端线程时接口即返回 404 —— 这是正常的空态，
 * 不是故障：此时应展示权威的空列表，而不是把它当成同步失败去回退本地快照。
 */
function isEmptySession(error: unknown): boolean {
  if (error && typeof error === 'object') {
    const detail = error as { status?: number }
    return detail.status === 404
  }
  return false
}

/**
 * 卡片载荷脱敏：插件级优先（业务知识留在插件层），核心级兜底。
 *
 * 脱敏只作用于**本地快照**，实时视图与服务端权威数据保持原样。
 */
function sanitizeMessage(message: ChatMessageRecord): ChatMessageRecord {
  if (message.kind !== 'card' || !message.card_type || !message.card_payload) {
    return message
  }
  const sanitizer = PluginRegistry.getCardSanitizer(message.card_type)
  if (!sanitizer) return message
  return { ...message, card_payload: sanitizer(message.card_payload) }
}

/** 构造落盘用的消息副本（逐条过插件脱敏器）。 */
function buildSnapshot(messages: ChatMessageRecord[]): ChatMessageRecord[] {
  return messages.map(sanitizeMessage)
}

/** 切换当前会话：替换会话 ID 与消息列表。 */
function setSession(sessionId: string, messages: ChatMessageRecord[] = []) {
  messagesSessionId = sessionId
  state.activeSessionId = sessionId
  state.activeMessages.splice(0, state.activeMessages.length, ...messages)
}

/** 直接追加一条消息（实时流 / 文本回执均可）。 */
function appendMessage(message: ChatMessageRecord) {
  state.activeMessages.push(message)
}

/**
 * 向指定消息追加文本（流式打字机逐字写入）。
 *
 * 通过响应式数组取回消息代理后再改写，确保触发视图更新；
 * 消息不存在时静默忽略（流已结束或会话已切换）。
 */
function appendMessageText(id: string, chunk: string) {
  const target = state.activeMessages.find((item) => item.id === id)
  if (!target) return
  target.content = (target.content ?? '') + chunk
}

/** 整体替换左栏列表（服务端回读 / 缓存兜底）。 */
function setConversations(list: TaskSummary[]) {
  state.conversationList.splice(0, state.conversationList.length, ...list)
}

/** 左栏 upsert（按 thread_id 去重覆盖）。 */
function upsertConversation(summary: TaskSummary) {
  const existingIndex = state.conversationList.findIndex(
    (item) => item.thread_id === summary.thread_id,
  )
  if (existingIndex >= 0) {
    state.conversationList.splice(existingIndex, 1, summary)
  } else {
    state.conversationList.unshift(summary)
  }
}

/**
 * 拉取当前用户可见的会话集合（服务端优先，失败降级到本地快照）。
 *
 * 服务端是唯一权威源；本地快照仅在服务端不可达时兜底，
 * 且通过 conversationSource 如实告知界面。
 */
async function refreshConversations(userId: string): Promise<void> {
  conversationOwnerId = userId
  try {
    const resp = await fetchThreads({ kind: 'all', limit: 100 })
    setConversations(resp.items)
    state.conversationSource = 'server'
    state.lastSyncedAt = Date.now()
    state.syncError = null
  } catch (error) {
    const cached = await loadConversations<TaskSummary>(userId)
    if (cached) {
      setConversations(cached.data)
      state.conversationSource = 'cache'
      state.lastSyncedAt = cached.updatedAt
    } else {
      setConversations([])
      state.conversationSource = 'none'
    }
    state.syncError = describeError(error)
  }
}

/**
 * 打开某条会话：服务端拉取消息，失败降级到本地快照。
 */
async function openConversation(summary: TaskSummary): Promise<void> {
  try {
    const resp = await fetchSessionMessages(summary.session_id, { limit: 300 })
    setSession(summary.session_id, resp.items)
    state.messageSource = 'server'
    state.syncError = null
  } catch (error) {
    // 404 = 服务端权威判定该会话暂无可见线程（空态），不是故障
    if (isEmptySession(error)) {
      setSession(summary.session_id, [])
      state.messageSource = 'server'
      state.syncError = null
      return
    }
    const cached = await loadMessages<ChatMessageRecord>(
      conversationOwnerId ?? '',
      summary.session_id,
    )
    setSession(summary.session_id, cached?.data ?? [])
    state.messageSource = cached ? 'cache' : 'none'
    state.syncError = describeError(error)
  }
}

/**
 * 从服务端回读当前会话消息（实时流结束后对齐权威副本）。
 *
 * 回读成功后主动落一次本地快照：落的是**服务端确认过的副本**，
 * 而非实时流中的中间态。
 */
async function refreshMessages(): Promise<void> {
  const sessionId = state.activeSessionId
  if (!sessionId || !conversationOwnerId) return
  try {
    const resp = await fetchSessionMessages(sessionId, { limit: 300 })
    setSession(sessionId, resp.items)
    state.messageSource = 'server'
    state.syncError = null
    suppressMessageWrite = false
    persistMessagesSnapshot()
  } catch (error) {
    suppressMessageWrite = false
    // 404 = 服务端权威判定该会话暂无可见线程（空态），不是故障
    if (isEmptySession(error)) {
      setSession(sessionId, [])
      state.messageSource = 'server'
      state.syncError = null
      return
    }
    state.syncError = describeError(error)
  }
}

/** 显式落一次中栏本地快照（服务端回读后调用）。 */
function persistMessagesSnapshot(): void {
  if (!conversationOwnerId || !messagesSessionId) return
  debounceSaveMessages(
    conversationOwnerId,
    messagesSessionId,
    buildSnapshot([...state.activeMessages]),
  )
}

/** 标记实时流开始：实时内容暂不落本地快照。 */
function beginLiveTurn(): void {
  suppressMessageWrite = true
}

/** 标记会话集合陈旧（递增计数，视图据此触发一次服务端对齐）。 */
function markConversationsStale(): void {
  state.conversationsStale += 1
}

/** 把后端 apply 返回结果收口为：中栏消息 + 会话集合 upsert + 折叠右栏。 */
function applyResult(result: ApplyResult) {
  const card = result.card ?? {}
  const cardType = String(result.card_type ?? card.card_type ?? '')
  const text = result.message ?? ''

  // 中栏：卡片快照消息（MessageCardHost 据 card_type 动态渲染）；
  // 后端返回文本结果时（如流程自动终止）退化为纯文本消息，避免空卡片。
  const message: ChatMessageRecord = {
    id: crypto.randomUUID(),
    thread_id: result.thread_id,
    role: 'assistant',
    kind: text ? 'text' : 'card',
    content: text || null,
    card_type: text ? null : cardType,
    card_payload: text ? null : card,
    retry_payload: null,
    created_at: new Date().toISOString(),
  }
  state.activeMessages.push(message)

  // 左栏：服务端返回的线程摘要为唯一真相源（标题 / 状态 / 锁版本一律以它为准）
  const serverThread = result.thread ?? {}
  const summary: TaskSummary = {
    thread_id: result.thread_id,
    session_id: result.session_id ?? state.activeSessionId,
    creator_id: String(serverThread.creator_id ?? ''),
    assigned_role: String(serverThread.assigned_role ?? ''),
    status: String(serverThread.status ?? result.status ?? 'WAITING_HUMAN'),
    lock_version: Number(
      serverThread.lock_version ?? card.lock_version ?? 1,
    ),
    current_node: serverThread.current_node ?? null,
    title:
      typeof serverThread.title === 'string' && serverThread.title
        ? serverThread.title
        : undefined,
  }
  upsertConversation(summary)

  // 中栏新增内容尚未经服务端确认 → 本轮不写本地快照
  suppressMessageWrite = true
  // 左栏随后与服务端对齐一次（标题 / 状态以服务端为准）
  markConversationsStale()
  state.rightDrawerOpen = false
}

/** 设置右栏抽屉开合。 */
function setRightDrawer(open: boolean) {
  state.rightDrawerOpen = open
}

/**
 * 重置内存态（登出时调用）。
 *
 * 同时解除落盘归属（conversationOwnerId 置空）：此后任何响应式变更都不会
 * 再触发本地写盘，否则登出清理完成后，排队中的落盘会把分区重新写回来。
 */
function resetAll(): void {
  conversationOwnerId = null
  messagesSessionId = ''
  suppressMessageWrite = false
  setConversations([])
  state.activeMessages.splice(0, state.activeMessages.length)
  state.activeSessionId = ''
  state.conversationSource = 'none'
  state.messageSource = 'none'
  state.lastSyncedAt = 0
  state.syncError = null
}

// 会话集合变更 → 按归属用户防抖落盘（服务端副本的本地缓存）
watch(
  () => state.conversationList,
  (list) => {
    if (conversationOwnerId) {
      debounceSaveConversations(conversationOwnerId, [...list])
    }
  },
  { deep: true },
)

// 当前会话消息变更 → 按会话防抖落盘（脱敏后的副本）
// 落盘责任统一收敛在 store：视图层不做持久化，避免切换 Tab 时丢失记录。
watch(
  () => state.activeMessages,
  (list) => {
    if (suppressMessageWrite) return
    if (conversationOwnerId && messagesSessionId) {
      debounceSaveMessages(
        conversationOwnerId,
        messagesSessionId,
        buildSnapshot([...list]),
      )
    }
  },
  { deep: true },
)

export function useWorkbenchStore() {
  return {
    state,
    setSession,
    appendMessage,
    appendMessageText,
    openConversation,
    refreshConversations,
    refreshMessages,
    beginLiveTurn,
    markConversationsStale,
    applyResult,
    setConversations,
    setRightDrawer,
    resetAll,
  }
}
