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
  SessionSummary,
  TaskSummary,
} from '../a2ui/types'
import { fetchSessionMessages, fetchSessions } from '../api/threads'
import type { WorkflowEventPayload } from '../api/workflow'
import {
  debounceSaveConversations,
  debounceSaveMessages,
  loadConversations,
  loadMessages,
} from '../engine/storage'
import { PluginRegistry } from '../plugins/registry'
import { generateUUID } from '../utils/uuid'

/** 数据来源标记 */
export type DataSource = 'server' | 'cache' | 'live' | 'none'

/** 左栏会话列表单页条数（折叠显示，剩余条目经"显示更多"按页追加） */
export const CONVERSATION_PAGE_SIZE = 10

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
  /**
   * 左栏会话集合（会话级聚合视图：一次会话框内的多次对话 / 多个工单只占一行）。
   */
  conversationList: SessionSummary[]
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
  /** 服务端仍有未加载的条目（可继续"显示更多"） */
  conversationHasMore: boolean
  /** 服务端过滤后的可见条目总数 */
  conversationTotal: number
  /** 当前生效的标题关键字（已提交到服务端的查询条件） */
  conversationQuery: string
  /**
   * 已从服务端取回的条目数（服务端分页位点）。
   *
   * 必须独立于 conversationList.length：去重会丢弃重复条目，
   * apply 后列表还会临时多出一条，用列表长度当 offset 会错位翻页。
   */
  conversationFetched: number
  /**
   * 工作流事件节拍：每收到一条状态变更事件即递增。
   *
   * 视图据此 watch 并自行决定是否重拉（core 不替视图决定刷新范围）。
   */
  workflowEventTick: number
  /**
   * 已生效身份：当前凭据真正对应的用户。
   *
   * 与"下拉框选中值"不同 —— 选中只是切换意图，凭据才是事实。
   * 本值只在凭据换好之后才变化，视图据此判断"界面身份 == 生效身份"。
   */
  activeOwnerId: string
  /**
   * 正在被只读检视的线程标识（无则为空串）。
   *
   * 高亮与检视面板都由它派生：不存在第二份"高亮状态"，杜绝两者失配。
   */
  inspectedThreadId: string
  /** 正在被检视的卡片类型（无则为空串） */
  inspectedCardType: string
  /**
   * 等待首个 token 的助手消息 ID（无则为空串）。
   *
   * 从本轮请求发出、到首个 token 落地之间，界面必须给出明确反馈：
   * 否则用户面对的是一段没有任何提示的静默等待，无法区分"还在思考"与"已经卡住"。
   * 该值同时承担"流在进行中"与"等待对象是谁"两项语义：
   * 收到正文 / 流结束 / 出错 / 切换会话，任一情形都会清空它。
   */
  pendingReplyId: string
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
  conversationHasMore: false,
  conversationTotal: 0,
  conversationQuery: '',
  conversationFetched: 0,
  workflowEventTick: 0,
  activeOwnerId: '',
  inspectedThreadId: '',
  inspectedCardType: '',
  pendingReplyId: '',
})

/** 会话集合归属用户（切换身份时随之切换并重新拉取）。 */
let conversationOwnerId: string | null = null

/**
 * 会话打开代次：并发打开会话时作废被抢占的旧请求。
 *
 * 用户连点两个会话时，先发后到的旧响应不得覆盖新会话的中栏内容。
 */
let openSessionSeq = 0

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

/**
 * 归属校验：发起请求时记下的归属是否仍是当前归属。
 *
 * 所有跨异步边界的拉取统一用它复核：归属已变（切换身份 / 登出）则丢弃响应，
 * 旧身份的数据绝不允许落到新身份的中栏、左栏或本地分区。
 */
function isOwnerCurrent(ownerId: string | null): boolean {
  return ownerId !== null && ownerId === conversationOwnerId
}

/** 切换当前会话：替换会话 ID 与消息列表。 */
function setSession(sessionId: string, messages: ChatMessageRecord[] = []) {
  messagesSessionId = sessionId
  state.activeSessionId = sessionId
  state.activeMessages.splice(0, state.activeMessages.length, ...messages)
  // 注意：此处**不**清空 pendingReplyId ——
  // setSession 同时被服务端回读（refreshMessages）复用，而回读与下一轮发送
  // 可能重叠；在回读里清等待态会误杀正在进行中的那轮反馈。
  // 真正的会话切换路径（openConversation / 新建会话 / resetAll）各自清理。
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
  // 首个 token 落地即结束等待态：中栏的"思考中"提示在此与正文无缝衔接。
  // 判据用"写入后是否有正文"而非"本次分片是否非空" —— 打字机可能先吐空分片。
  if (state.pendingReplyId === id && (target.content ?? '').length > 0) {
    state.pendingReplyId = ''
  }
}

/** 整体替换左栏列表（服务端回读 / 缓存兜底）。 */
function setConversations(list: SessionSummary[]) {
  state.conversationList.splice(0, state.conversationList.length, ...list)
}

/**
 * 拉取当前用户可见的会话集合（服务端优先，失败降级到本地快照）。
 *
 * 服务端是唯一权威源；本地快照仅在服务端不可达时兜底，
 * 且通过 conversationSource 如实告知界面。
 *
 * 列表按页取回（默认 CONVERSATION_PAGE_SIZE 条）。自动刷新时按已取条数
 * 请求，保留用户已展开的深度 —— 审批完成 / 提报成功后的服务端对齐
 * 不应把第 2 页折叠掉。
 */
async function refreshConversations(userId: string): Promise<void> {
  // 归属已绑定给别的身份时不得改绑：
  // 切换失败等场景下"下拉框值"与"生效身份"可能短暂不一致，
  // 改绑会让旧凭据取到的数据挂到新身份名下。
  if (conversationOwnerId !== null && conversationOwnerId !== userId) {
    return
  }
  // 身份变更检测必须先于赋值：conversationOwnerId 记录的是归属身份
  const ownerChanged = conversationOwnerId !== userId
  if (ownerChanged) {
    // 换人：旧的查询条件与分页位点对新身份无意义，一并重置
    state.conversationQuery = ''
    state.conversationFetched = 0
    state.conversationHasMore = false
    state.conversationTotal = 0
  }
  conversationOwnerId = userId
  // 凭据已生效，此后才允许视图按"生效身份"重新取数
  state.activeOwnerId = userId
  try {
    const resp = await fetchSessions({
      limit: Math.max(CONVERSATION_PAGE_SIZE, state.conversationFetched),
      offset: 0,
      q: state.conversationQuery || undefined,
    })
    // 请求期间归属已变（切换身份）→ 丢弃旧身份的响应
    if (!isOwnerCurrent(userId)) return
    setConversations(resp.items)
    state.conversationFetched = resp.items.length
    state.conversationHasMore = resp.has_more
    state.conversationTotal = resp.total ?? resp.items.length
    state.conversationSource = 'server'
    state.lastSyncedAt = Date.now()
    state.syncError = null
  } catch (error) {
    const cached = await loadConversations<SessionSummary>(userId)
    if (cached) {
      setConversations(cached.data)
      state.conversationSource = 'cache'
      state.lastSyncedAt = cached.updatedAt
    } else {
      setConversations([])
      state.conversationSource = 'none'
    }
    // 离线：无法继续翻页，本地快照也不含服务端总数
    state.conversationHasMore = false
    state.conversationTotal = state.conversationList.length
    state.syncError = describeError(error)
  }
}

/**
 * 追加下一页会话条目（"显示更多"）。
 *
 * 追加时按 session_id 去重：列表按"最近活跃"排序，条目会随新消息上浮，
 * offset 分页因此天然可能与已加载区间重叠。
 */
async function loadMoreConversations(): Promise<void> {
  const ownerId = conversationOwnerId
  if (!ownerId || !state.conversationHasMore) return
  // 离线快照无法翻页
  if (state.conversationSource !== 'server') return
  try {
    const resp = await fetchSessions({
      limit: CONVERSATION_PAGE_SIZE,
      offset: state.conversationFetched,
      q: state.conversationQuery || undefined,
    })
    // 请求期间归属已变（切换身份）→ 丢弃旧身份的响应
    if (!isOwnerCurrent(ownerId)) return
    const known = new Set(
      state.conversationList.map((item) => item.session_id),
    )
    const fresh = resp.items.filter((item) => !known.has(item.session_id))
    state.conversationList.push(...fresh)
    state.conversationFetched += resp.items.length
    state.conversationHasMore = resp.has_more
    // 本页为空时服务端无法给出总数（窗口函数无行可取），保留已知总数
    if (resp.items.length > 0) {
      state.conversationTotal = resp.total ?? state.conversationTotal
    }
  } catch (error) {
    state.syncError = describeError(error)
  }
}

/**
 * 按标题关键字过滤会话列表（重置到第一页）。
 *
 * 过滤条件变化后旧的 offset 位点失效，故分页状态一并归零。
 */
async function searchConversations(query: string): Promise<void> {
  state.conversationQuery = query.trim()
  state.conversationFetched = 0
  state.conversationHasMore = false
  state.conversationTotal = 0
  if (conversationOwnerId) {
    await refreshConversations(conversationOwnerId)
  }
}

/**
 * 打开某条会话：服务端拉取消息，失败降级到本地快照。
 */
async function openConversation(summary: SessionSummary): Promise<void> {
  const ownerId = conversationOwnerId
  const seq = ++openSessionSeq
  if (!ownerId) return
  // 切换会话即关闭检视：被检视的卡片属于上一个会话，留着只会看到错位的详情
  closeInspection()
  // 切换会话即作废上一轮的等待态：等待提示只对当前会话的助手消息有意义
  state.pendingReplyId = ''
  try {
    const resp = await fetchSessionMessages(summary.session_id, { limit: 300 })
    // 归属已变（切换身份）或已被更新的打开请求抢占 → 丢弃响应
    if (!isOwnerCurrent(ownerId) || seq !== openSessionSeq) return
    setSession(summary.session_id, resp.items)
    state.messageSource = 'server'
    state.syncError = null
  } catch (error) {
    if (!isOwnerCurrent(ownerId) || seq !== openSessionSeq) return
    // 404 = 服务端权威判定该会话暂无可见线程（空态），不是故障
    if (isEmptySession(error)) {
      setSession(summary.session_id, [])
      state.messageSource = 'server'
      state.syncError = null
      return
    }
    // 兜底快照必须按"发起时"的归属读取：回调时的归属可能已换成别人
    const cached = await loadMessages<ChatMessageRecord>(ownerId, summary.session_id)
    if (!isOwnerCurrent(ownerId) || seq !== openSessionSeq) return
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
  const ownerId = conversationOwnerId
  if (!sessionId || !ownerId) return
  try {
    const resp = await fetchSessionMessages(sessionId, { limit: 300 })
    // 会话或归属在请求期间已变（切换身份）→ 丢弃：
    // 否则旧身份的消息会在切换完成后回填进新身份的中栏
    if (sessionId !== state.activeSessionId || !isOwnerCurrent(ownerId)) return
    setSession(sessionId, resp.items)
    state.messageSource = 'server'
    state.syncError = null
    suppressMessageWrite = false
    persistMessagesSnapshot()
  } catch (error) {
    suppressMessageWrite = false
    // 同上：迟到的响应不得改写当前会话号（404 分支会把旧会话号写回）
    if (sessionId !== state.activeSessionId || !isOwnerCurrent(ownerId)) return
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

/**
 * 标记实时流开始：实时内容暂不落本地快照。
 *
 * @param replyId 本轮等待首个 token 的助手消息 ID（空串表示不标记等待态）
 */
function beginLiveTurn(replyId: string = ''): void {
  suppressMessageWrite = true
  state.pendingReplyId = replyId
}

/**
 * 清除首字等待态。
 *
 * 触发时机：收到正文（由 appendMessageText 自动完成）、流正常结束、
 * 流报错、整轮被放弃（重放 / 中止）。任一情形下都不应继续显示"思考中"。
 */
function clearPendingReply(): void {
  state.pendingReplyId = ''
}

/** 标记会话集合陈旧（递增计数，视图据此触发一次服务端对齐）。 */
function markConversationsStale(): void {
  state.conversationsStale += 1
}

/**
 * 处理服务端广播的工作流状态变更事件（跨端同步入口）。
 *
 * 事件语义对 core 是不透明的，core 只做两件事：
 * 1. 把"有变更发生"这一事实广播出去（workflowEventTick 递增），
 *    由各视图自行决定是否需要重拉；
 * 2. 若变更命中的线程属于当前会话，则重拉中栏消息（终态即时呈现）。
 *
 * @param payload 事件负载（业务中立）
 * @param selfId 本端操作者标识：自己触发的变更本端已即时更新，跳过
 */
async function handleWorkflowEvent(
  payload: WorkflowEventPayload,
  selfId: string,
): Promise<void> {
  if (payload.actor_id && payload.actor_id === selfId) {
    // 自己触发的变更：本端已即时更新，跳过以免重复拉取
    return
  }
  state.workflowEventTick += 1
  // 左栏状态 / 锁版本一律以服务端为准：任何状态变更都要对齐一次
  markConversationsStale()
  // 命中当前会话 → 重拉中栏（终态卡片即时呈现）
  // 命中判定以 session_id 为准：会话级列表项不再持有 thread_id，
  // 而"这条会话里发生了什么"才是中栏需要响应的语义。
  if (payload.session_id && payload.session_id === state.activeSessionId) {
    await refreshMessages()
  }
}

/** 把后端 apply 返回结果收口为：中栏消息 + 会话集合 upsert + 折叠右栏。 */
function applyResult(result: ApplyResult) {
  const card = result.card ?? {}
  const cardType = String(result.card_type ?? card.card_type ?? '')
  const text = result.message ?? ''

  // 中栏：卡片快照消息（MessageCardHost 据 card_type 动态渲染）；
  // 后端返回文本结果时（如流程自动终止）退化为纯文本消息，避免空卡片。
  const message: ChatMessageRecord = {
    id: generateUUID(),
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

  // 左栏不在本地拼装列表项：左栏是**会话级**聚合视图，前端既拿不准
  // 关联计数，也捏不出准确代表态 —— 一律走服务端对齐（markConversationsStale）。
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
 * 打开只读检视（点中栏某张卡片）。
 *
 * 检视态是**唯一**的高亮来源：中栏卡片的选中边框由它派生，
 * 不另存第二份"高亮状态"，杜绝面板与高亮失配。
 */
function openInspection(threadId: string, cardType: string | null): void {
  state.inspectedThreadId = threadId
  state.inspectedCardType = cardType ?? ''
}

/** 关闭只读检视（同步取消中栏卡片高亮）。 */
function closeInspection(): void {
  state.inspectedThreadId = ''
  state.inspectedCardType = ''
}

/**
 * 重置内存态（登出 / 切换身份时调用）。
 *
 * 同时解除落盘归属（conversationOwnerId 置空）：此后任何响应式变更都不会
 * 再触发本地写盘，否则清理完成后，排队中的落盘会把分区重新写回来。
 *
 * 切换身份时复用本函数（而非另写一套重置）：两套逻辑必然漂移，共用是唯一
 * 正确选择。切换分支必须在清本机分区**之前**调用，否则旧消息会被写进新身份
 * 的本地分区。
 */
function resetAll(): void {
  conversationOwnerId = null
  messagesSessionId = ''
  suppressMessageWrite = false
  // 作废在途的会话打开请求
  openSessionSeq += 1
  setConversations([])
  state.activeMessages.splice(0, state.activeMessages.length)
  state.activeSessionId = ''
  state.conversationSource = 'none'
  state.messageSource = 'none'
  state.lastSyncedAt = 0
  state.syncError = null
  state.conversationHasMore = false
  state.conversationTotal = 0
  state.conversationQuery = ''
  state.conversationFetched = 0
  state.activeOwnerId = ''
  // 检视态必须随身份一起清空：否则检视面板会残留上一身份的数据
  state.inspectedThreadId = ''
  state.inspectedCardType = ''
  // 等待态同理：换人后上一轮的在途等待不得继续显示
  state.pendingReplyId = ''
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
      // 会话号必须与当前会话一致：身份切换后残留的旧会话号若与新归属搭配，
      // 会把旧身份的消息写进新身份的本地分区（共享终端越权残留）
      if (messagesSessionId !== state.activeSessionId) return
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
    loadMoreConversations,
    searchConversations,
    refreshMessages,
    beginLiveTurn,
    clearPendingReply,
    markConversationsStale,
    handleWorkflowEvent,
    applyResult,
    setConversations,
    setRightDrawer,
    openInspection,
    closeInspection,
    resetAll,
  }
}
