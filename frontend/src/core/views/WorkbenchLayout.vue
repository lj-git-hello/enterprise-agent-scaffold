<script setup lang="ts">
/**
 * WorkbenchLayout —— 通用三栏沉浸式工作台外壳。
 *
 * Header：标题 / SSE 状态灯 / 身份切换 / 退出登录
 * 左栏：新建会话、视图 Tab、列表、来源徽章、本地缓存运维
 * 中栏：当前会话标示、离线兜底提示、历史/审批视图、通用输入框
 * 右栏：折叠、业务表单 slot、Trace 面板
 *
 * 权威源已在服务端：左栏与中栏优先读服务端，本地 IndexedDB 仅为
 * 离线兜底快照；界面必须如实标注当前数据来源，不得伪装成最新。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Activity,
  ClipboardList,
  LogOut,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-vue-next'

import TaskApprovalPool from './TaskApprovalPool.vue'
import TaskHistoryList from './TaskHistoryList.vue'
import type { ChatMessageRecord, SessionSummary } from '../a2ui/types'
import { chatStream, type StreamStatus } from '../api/agent'
import { clearToken, http, setToken } from '../api/http'
import { subscribeWorkflowEvents } from '../api/workflow'
import { useCurrentUser } from '../composables/useCurrentUser'
import { RenderTicker } from '../engine/render_ticker'
import {
  clearUserPartition,
  probeHealth,
  storageHealth,
} from '../engine/storage'
import { PluginRegistry, type PluginIdentity } from '../plugins/registry'
import { formatDateTime } from '../utils/datetime'
import {
  type ApplyResult,
  useWorkbenchStore,
} from '../workbench/useWorkbenchStore'

// 通用工作台共享状态
const workbench = useWorkbenchStore()
const { state: workbenchState } = workbench
const { setCurrentUser } = useCurrentUser()

/** 插件未声明演示身份时的占位（不参与任何业务判定） */
const EMPTY_IDENTITY: PluginIdentity = {
  id: '',
  label: '未配置身份',
  role: '',
}

// ---- 当前身份（身份清单来自业务插件，底座不写死任何角色） ----
const identities = computed<PluginIdentity[]>(
  () => PluginRegistry.activeIdentities.value,
)
const identityId = ref<string>('')
const currentIdentity = computed<PluginIdentity>(
  () =>
    identities.value.find((item) => item.id === identityId.value) ??
    identities.value[0] ??
    EMPTY_IDENTITY,
)

/** 是否已退出登录（退出后不再自动建立会话） */
const loggedOut = ref(false)

/**
 * 身份激活代次：并发切换时只认最后一次激活的结果。
 *
 * 快速连切身份时，先发后到的旧响应不得写入过期令牌，也不得继续收尾。
 */
let activationEpoch = 0

// 调后端登录端点换取真实 JWT（后端只认合规 JWT，不能写 simulated:*）
async function loginAs(userId: string, epoch: number) {
  if (!userId) {
    return
  }
  const { data } = await http.post<{ access_token: string }>('/v1/auth/login', {
    user_id: userId,
  })
  // 已被更新的切换取代：过期令牌绝不能写回
  if (epoch !== activationEpoch) {
    return
  }
  setToken(data.access_token)
}

/** 已激活身份（避免重复登录；登录失败后清空以允许重试） */
let activatedId = ''

/**
 * 当前生效身份：凭据真正对应的用户。
 *
 * 与下拉框选中值可能短暂不一致（切换失败、或切换进行中）：
 * 取数一律以生效身份为准，避免用旧凭据取到别的身份的数据。
 */
function effectiveIdentityId(): string {
  return workbenchState.activeOwnerId || currentIdentity.value.id
}

// ---- 工作流事件订阅（跨端状态变更实时同步） ----
// 订阅通道的身份与受众由 JWT 判定，切换身份必须重建订阅，
// 否则旧身份的订阅会一直收不到属于新身份的事件。
let workflowAbort: AbortController | null = null

function stopWorkflowSubscription(): void {
  workflowAbort?.abort()
  workflowAbort = null
}

function startWorkflowSubscription(userId: string): void {
  stopWorkflowSubscription()
  const controller = new AbortController()
  workflowAbort = controller
  void subscribeWorkflowEvents(
    {
      onStatusChanged: (payload) => {
        // 事件按"创建该订阅的身份"归因，不在回调时反查当前身份：
        // 旧订阅的迟到事件因此不会记到新身份名下。
        void workbench.handleWorkflowEvent(payload, userId)
      },
    },
    controller.signal,
  ).catch((error) => {
    // 订阅中断不阻断主流程：对话流与手动刷新仍是权威兜底
    if (controller.signal.aborted) return
    console.warn('工作流事件订阅中断：', error)
  })
}

/** 身份切换失败提示（界面给出重试入口，不静默失败）。 */
const identityError = ref<string | null>(null)

/**
 * 身份切换导致的中止标记。
 *
 * 切换身份会主动中断在途对话流 —— 属预期行为，不得当作"连接失败"提示用户。
 */
let identitySwitchAbort = false

async function activateIdentity(userId: string): Promise<void> {
  if (!userId || userId === activatedId) {
    return
  }
  const previousId = activatedId
  activatedId = userId
  const epoch = ++activationEpoch
  identityError.value = null
  try {
    // 切换身份：在途对话流属于上一身份，先中止，
    // 否则其 token / 卡片会被写进新身份的中栏。
    if (previousId && previousId !== userId) {
      // 仅在确有在途流时置中断标记：否则会误吞下一轮流的错误提示
      if (abortController) {
        identitySwitchAbort = true
        abortController.abort()
      }
      ticker?.clear()
      isStreaming.value = false
      // 先重置内存态并解除落盘归属，再清分区：
      // 顺序颠倒会让排队中的防抖落盘在清理完成后把分区重新写回来。
      workbench.resetAll()
      // 清理前一身份在本机的缓存分区（共享终端安全）。
      // 本地缓存清理失败不得阻断身份切换，故单独兜住异常。
      try {
        await clearUserPartition(previousId)
      } catch (error) {
        console.error('清理本地缓存分区失败：', error)
      }
      // 换人：搜索条件一并清空（store 侧亦会重置查询与分页位点）
      searchInput.value = ''
      if (searchTimer) {
        clearTimeout(searchTimer)
        searchTimer = null
      }
      // 期间又发生了新的切换：本次激活作废
      if (epoch !== activationEpoch) return
    }
    await loginAs(userId, epoch)
    // 已被更新的切换取代：放弃本次收尾（令牌亦未写入）
    if (epoch !== activationEpoch) return
    // 同步身份供深层卡片做权限判定（角色取自目标身份本身）
    const target = identities.value.find((item) => item.id === userId)
    setCurrentUser(userId, target?.role ?? currentIdentity.value.role)
    // 订阅工作流事件（JWT 已更新，受众随身份生效）
    startWorkflowSubscription(userId)
    // 登出后重新登录 / 切换身份后：会话 ID 已被清空，必须重建一个，
    // 否则新一轮对话会以空 thread_id 提交到服务端。
    if (!workbenchState.activeSessionId) {
      workbench.setSession(newSessionId())
    }
    // 切换身份后从服务端加载该用户的会话集合（审批人本地通常为空）
    await workbench.refreshConversations(userId)
    if (epoch !== activationEpoch) return
    // 切换身份后自动打开新身份的首个会话：
    // 中栏必须只呈现新身份自己的内容，绝不残留上一身份的对话。
    if (previousId && previousId !== userId) {
      const first = workbenchState.conversationList[0]
      if (first) {
        await workbench.openConversation(first)
      }
    }
  } catch (error) {
    console.error('身份激活失败：', error)
    // 已被更新的切换取代：本次失败作废，不影响更新的那次
    if (epoch !== activationEpoch) return
    activatedId = ''
    identityError.value = previousId
      ? '身份切换失败：无法获取新的登录凭据，已返回上一身份。'
      : '身份激活失败：无法获取登录凭据，请重试。'
    // 回滚：下拉框回到上一身份，由既有 watch(identityId) 触发重新激活。
    // （activatedId 已清空，不会双激活，也不会无限递归）
    if (previousId) {
      identityId.value = previousId
    }
  }
}

/** 重试身份激活（首次激活失败时无可回滚目标，必须提供显式重试入口）。 */
function retryIdentity(): void {
  identityError.value = null
  void activateIdentity(identityId.value)
}

async function switchIdentity(event: Event) {
  identityId.value = (event.target as HTMLSelectElement).value
}

// 身份清单由插件声明：清单到达后自动选中首个有效身份
watch(
  identities,
  (list) => {
    if (list.length && !list.some((item) => item.id === identityId.value)) {
      identityId.value = list[0].id
    }
  },
  { immediate: true, deep: true },
)

// 身份变化 → 重新登录并同步角色
watch(identityId, (userId) => {
  void activateIdentity(userId)
})

/** 退出登录：清 JWT + 清本机该用户缓存分区 + 重置内存态。 */
async function logout() {
  const userId = currentIdentity.value.id
  abortController?.abort()
  stopWorkflowSubscription()
  ticker?.clear()
  ticker = null
  isStreaming.value = false
  // 先重置内存态并解除落盘归属，再清分区：
  // 顺序颠倒会让排队中的防抖落盘在清理完成后把分区重新写回来。
  workbench.resetAll()
  try {
    await clearUserPartition(userId)
  } catch (error) {
    console.error('清理本地缓存分区失败：', error)
  }
  clearToken()
  activatedId = ''
  loggedOut.value = true
}

/** 重新登录（退出后的显式恢复动作）。 */
async function relogin() {
  loggedOut.value = false
  await activateIdentity(currentIdentity.value.id)
}

// ---- SSE 灯状态（由通用对话流的连接生命周期驱动） ----
// 注意：这盏灯反映的是"对话流"而非"网络连通性"——登录本身不建立流，
// 空闲期无流可查属于正常状态，不可用 Offline 这类断网措辞误导用户。
const sseStatus = ref<StreamStatus>('idle')
const sseMeta = {
  idle: { dot: 'bg-gray-400', text: 'Idle' },
  connected: { dot: 'bg-green-500', text: 'Connected' },
  reconnecting: { dot: 'bg-amber-500', text: 'Reconnecting' },
  offline: { dot: 'bg-red-500', text: 'Disconnected' },
} as const
/** 状态灯悬浮说明：明确它反映的是对话流，而非网络连通性 */
const sseStatusHint =
  '对话流（SSE）状态：Idle = 当前没有进行中的对话流，不代表后端离线'

// ---- 当前会话（ID 与消息由共享 store 持有） ----
function newSessionId(): string {
  return crypto.randomUUID()
}
// 初始化一个本地会话（延迟落库，不发请求）
workbench.setSession(newSessionId())
const sessionId = computed(() => workbenchState.activeSessionId)

const activeTab = ref<'history' | 'approval'>('history')
const draftText = ref('')

function createNewSession() {
  // 仅重置当前会话与中栏消息；严禁删除/覆盖会话集合中已有记录
  workbench.setSession(newSessionId())
  draftText.value = ''
  activeTab.value = 'history'
}

/** 点击会话项：切换到该会话并恢复其中栏消息（服务端优先）。 */
async function selectConversation(summary: SessionSummary) {
  await workbench.openConversation(summary)
  activeTab.value = 'history'
}

/**
 * 中栏卡片点击 → 打开右栏只读检视面板。
 *
 * 检视态是唯一的高亮来源：面板与中栏边框都由 store 的检视态派生，
 * 不存在第二份需要同步的"高亮状态"。
 */
function handleInspect(message: ChatMessageRecord): void {
  workbench.openInspection(message.thread_id, message.card_type)
  // 检视面板在右栏：右栏处于折叠态时自动展开，否则用户看不到任何反馈
  workbench.setRightDrawer(true)
}

/** 是否处于只读检视态。 */
const inspectionOpen = computed(() =>
  Boolean(workbenchState.inspectedThreadId),
)

/**
 * 当前检视对象的只读面板组件（插件注册，core 不解释其内部字段）。
 *
 * 未注册对应卡片类型的检视视图时为空 —— 右栏如实提示"未注册"，
 * 绝不渲染空白面板假装成功。
 */
const inspectorComponent = computed(() =>
  PluginRegistry.getInspector(workbenchState.inspectedCardType),
)

/** 右栏表单分区标题（插件提供；未声明时回落中性默认名）。 */
const formSectionTitle = computed(() => PluginRegistry.activeFormSectionTitle.value)

/** 左栏关联计数的单位文案（插件提供；未声明时回落中性默认名）。 */
const sessionCountLabel = computed(() => PluginRegistry.activeSessionCountLabel.value)

/** 表单提交成功后的统一收口（供 App 经组件 ref 调用）。 */
async function handleApplied(result: ApplyResult) {
  workbench.applyResult(result)
  // 与服务端对齐（apply 的卡片服务端已落库），并恢复本地快照写入
  await workbench.refreshMessages()
}

/** 手动刷新左栏（服务端权威列表）。 */
async function refreshList() {
  await workbench.refreshConversations(effectiveIdentityId())
}

// ---- 左栏会话列表：标题搜索 + 分页折叠 ----

/** 搜索框的即时值（与 store 中已生效的查询条件相互独立） */
const searchInput = ref<string>('')

/** 搜索防抖时长（毫秒）：避免逐字符触发后端查询 */
const SEARCH_DEBOUNCE_MS = 300

let searchTimer: ReturnType<typeof setTimeout> | null = null

/** 输入即搜（防抖）；清空输入同样触发查询，回到完整列表 */
function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    searchTimer = null
    void workbench.searchConversations(searchInput.value)
  }, SEARCH_DEBOUNCE_MS)
}

/** 回车立即提交当前关键字（跳过剩余防抖时间） */
function submitSearch() {
  if (searchTimer) {
    clearTimeout(searchTimer)
    searchTimer = null
  }
  void workbench.searchConversations(searchInput.value)
}

/** 左栏计数文案：过滤后显示匹配数，未过滤显示总数 */
const conversationCountText = computed(() => {
  const total = workbenchState.conversationTotal
  if (workbenchState.conversationQuery) return `匹配 ${total} 条`
  return `共 ${total} 条`
})

/** "显示更多"按钮上的剩余条数 */
const remainingCount = computed(() =>
  Math.max(
    0,
    workbenchState.conversationTotal - workbenchState.conversationFetched,
  ),
)

// 会话集合被标记陈旧（审批完成 / 提报成功）→ 与服务端对齐一次
watch(
  () => workbenchState.conversationsStale,
  () => {
    void workbench.refreshConversations(effectiveIdentityId())
  },
)

/**
 * 清除本机缓存分区。
 *
 * 仅清理本机离线快照，服务端数据不受影响；结果如实反映到指示灯。
 */
async function wipeLocalSnapshot() {
  try {
    await clearUserPartition(currentIdentity.value.id)
    await probeHealth()
  } catch (error) {
    console.error('清除本地缓存失败：', error)
  }
}

/** 本地缓存状态文案（语义：缓存可用性，而非主存储健康度） */
const cacheStatusText = computed(() => {
  if (!storageHealth.writable) return '写入失败'
  if (workbenchState.conversationSource === 'cache') return '离线快照'
  if (workbenchState.conversationSource === 'none') return '无本地缓存'
  return '正常'
})

/** 左栏来源徽章：离线兜底时标注快照时间，避免伪装成最新数据 */
const conversationSourceBadge = computed(() => {
  if (workbenchState.conversationSource !== 'cache') return ''
  const at = workbenchState.lastSyncedAt
  if (!at) return '离线快照'
  const time = new Date(at).toLocaleTimeString('zh-CN', { hour12: false })
  return `离线快照 · ${time}`
})

/** 中栏离线兜底提示 */
const messageOfflineHint = computed(() =>
  workbenchState.messageSource === 'cache'
    ? '当前显示的是本地离线快照，服务端暂不可达；恢复网络后将自动回源。'
    : '',
)

/**
 * 网络恢复 → 自动回源（服务端重新成为权威源）。
 *
 * 离线期间界面显示的是本地快照，恢复连通后必须主动重新拉取，
 * 否则用户会一直看着陈旧数据而不自知。
 */
async function handleOnline() {
  await workbench.refreshConversations(effectiveIdentityId())
  await workbench.refreshMessages()
}

window.addEventListener('online', handleOnline)

// 右栏开合由 store 统一持有（提交成功后自动折叠）
const rightOpen = computed(() => workbenchState.rightDrawerOpen)

// ---- 通用对话流（跨业务：端点与卡片协议均来自底座） ----
const isStreaming = ref(false)
const streamError = ref<string | null>(null)
let abortController: AbortController | null = null
let ticker: RenderTicker | null = null

/** 发送当前输入：走通用对话网关，token 经打字机逐字渲染。 */
async function sendDraft() {
  const text = draftText.value.trim()
  if (!text || isStreaming.value) {
    return
  }

  draftText.value = ''
  streamError.value = null

  const threadId = sessionId.value
  const createdAt = new Date().toISOString()
  // 客户端幂等键：同一轮唯一，服务端据此识别整轮重放
  const clientMsgId = crypto.randomUUID()

  // 用户消息立即入列
  workbench.appendMessage({
    id: crypto.randomUUID(),
    thread_id: threadId,
    role: 'user',
    kind: 'text',
    content: text,
    card_type: null,
    card_payload: null,
    retry_payload: null,
    created_at: createdAt,
  })

  // 助手占位消息：打字机逐字写入
  const answerId = crypto.randomUUID()
  workbench.appendMessage({
    id: answerId,
    thread_id: threadId,
    role: 'assistant',
    kind: 'text',
    content: '',
    card_type: null,
    card_payload: null,
    retry_payload: null,
    created_at: createdAt,
  })

  ticker?.clear()
  ticker = new RenderTicker((consumed) => {
    workbench.appendMessageText(answerId, consumed)
  })

  isStreaming.value = true
  abortController = new AbortController()
  // 实时内容尚未经服务端确认 → 暂不落本地快照
  workbench.beginLiveTurn()

  try {
    await chatStream(
      {
        thread_id: threadId,
        session_id: threadId,
        message: text,
        client_msg_id: clientMsgId,
      },
      {
        onToken: (content) => ticker?.enqueue(content),
        onCard: (cardType, data) => {
          workbench.appendMessage({
            id: crypto.randomUUID(),
            thread_id: threadId,
            role: 'assistant',
            kind: 'card',
            content: null,
            card_type: cardType,
            card_payload: { card_type: cardType, ...data },
            retry_payload: null,
            created_at: new Date().toISOString(),
          })
        },
        onError: (message) => {
          streamError.value = message
        },
        onDone: () => {
          // 立即排空打字机缓冲区，避免尾字滞留导致落盘内容不全
          ticker?.flush()
          ticker = null
          isStreaming.value = false
        },
        onReplay: () => {
          streamError.value = '本轮此前已提交，服务端未重复执行；已按服务端记录刷新。'
        },
        onStatus: (status) => {
          sseStatus.value = status
        },
      },
      abortController.signal,
    )
  } catch (error) {
    // 身份切换主动中断：属预期行为，不作为故障提示
    if (identitySwitchAbort) {
      identitySwitchAbort = false
      return
    }
    streamError.value =
      (error as { message?: string }).message ?? '对话连接失败，请稍后重试。'
  } finally {
    isStreaming.value = false
    abortController = null
    // 本轮结束：中断标记复位（异常路径已在 catch 中先行消费）
    identitySwitchAbort = false
    // 与服务端对齐权威副本（并恢复本地快照写入）
    void workbench.refreshMessages()
    // 本轮新建了聊天线程 → 左栏与服务端对齐一次（标题 / 状态以服务端为准）
    workbench.markConversationsStale()
  }
}

// 组件卸载时中止在途流，避免回调写入已销毁的状态
onBeforeUnmount(() => {
  abortController?.abort()
  stopWorkflowSubscription()
  ticker?.clear()
  ticker = null
  if (searchTimer) {
    clearTimeout(searchTimer)
    searchTimer = null
  }
  window.removeEventListener('online', handleOnline)
})

// 暴露给装配层（App）调用
defineExpose({ handleApplied })

// 挂载时探测本地缓存可用性
onMounted(async () => {
  // 启动时为当前身份确保一个有效 JWT（旧 simulated:* / 空值会被覆盖）。
  // 用下拉框的选中身份：此刻身份清单可能尚未到达，为空则跳过（清单到达后由 watch 触发）。
  await activateIdentity(identityId.value)
  await probeHealth()
})
</script>

<template>
  <div class="flex h-full flex-col bg-white text-gray-800">
    <!-- Header -->
    <header class="flex h-14 shrink-0 items-center justify-between border-b bg-white px-4">
      <div class="flex items-center gap-2">
        <Activity class="h-5 w-5 text-blue-600" />
        <span class="text-sm font-semibold">企业级 AI Agent 应用平台</span>
      </div>

      <div class="flex items-center gap-3">
        <!-- SSE 状态灯 -->
        <div class="flex items-center gap-1.5">
          <span class="h-2 w-2 rounded-full" :class="sseMeta[sseStatus].dot" />
          <span class="text-xs text-gray-500" :title="sseStatusHint">
            {{ sseMeta[sseStatus].text }}
          </span>
        </div>

        <!-- 身份切换（身份清单来自业务插件） -->
        <select class="rounded-md border border-gray-300 px-2 py-1 text-xs disabled:opacity-50" :value="identityId"
          :disabled="identities.length === 0 || loggedOut" @change="switchIdentity">
          <option v-for="item in identities" :key="item.id" :value="item.id">
            {{ item.label }} ({{ item.role }})
          </option>
        </select>

        <button v-if="loggedOut" type="button"
          class="rounded-md bg-blue-600 px-2 py-1 text-xs text-white" @click="relogin">
          重新登录
        </button>
        <button v-else type="button" title="退出登录（清除本机缓存分区）"
          class="flex items-center gap-1 rounded-md border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-50"
          @click="logout">
          <LogOut class="h-3 w-3" />
          退出
        </button>
      </div>
    </header>

    <!-- 已退出登录提示 -->
    <div v-if="loggedOut" class="border-b bg-amber-50 px-4 py-2 text-xs text-amber-700">
      已退出登录：JWT 与本机缓存分区均已清除，服务端数据不受影响。
    </div>

    <!-- 身份切换失败提示：明确反馈并给出重试入口，不静默失败 -->
    <div v-if="identityError" class="flex items-center gap-2 border-b bg-red-50 px-4 py-2 text-xs text-red-700">
      <span>{{ identityError }}</span>
      <button type="button" class="underline" @click="retryIdentity">重试</button>
    </div>

    <!-- 主体三栏 -->
    <div class="flex flex-1 overflow-hidden">
      <!-- 左栏 -->
      <aside class="flex w-72 shrink-0 flex-col border-r bg-gray-50">
        <div class="p-3">
          <button type="button"
            class="flex w-full items-center justify-center gap-1.5 rounded-md bg-blue-600 px-3 py-2 text-sm text-white"
            @click="createNewSession">
            <Plus class="h-4 w-4" />
            新建会话
          </button>
        </div>

        <!-- Tab -->
        <div class="flex gap-1 px-3">
          <button type="button" class="rounded-md px-3 py-1 text-xs" :class="activeTab === 'history'
            ? 'bg-blue-100 text-blue-700'
            : 'text-gray-500 hover:bg-gray-100'
            " @click="activeTab = 'history'">
            我的会话
          </button>
          <button type="button" class="rounded-md px-3 py-1 text-xs" :class="activeTab === 'approval'
            ? 'bg-blue-100 text-blue-700'
            : 'text-gray-500 hover:bg-gray-100'
            " @click="activeTab = 'approval'">
            待办审批池
          </button>
        </div>

        <!-- 标题搜索：置于滚动容器之外，列表滚动时保持可见 -->
        <div v-if="activeTab === 'history'" class="border-b px-3 py-2">
          <input v-model="searchInput" type="search" placeholder="搜索会话标题"
            class="w-full rounded-md border border-gray-200 px-2 py-1 text-xs outline-none focus:border-blue-400"
            @input="onSearchInput" @keyup.enter="submitSearch" />
        </div>

        <!-- 列表树：Tab1 渲染【我的会话】集合 -->
        <div class="flex-1 overflow-y-auto p-3">
          <template v-if="activeTab === 'history'">
            <div class="mb-2 flex items-center justify-between">
              <span class="text-[10px] text-gray-400">
                {{ conversationSourceBadge || '服务端权威数据' }}
              </span>
              <div class="flex items-center gap-2">
                <span class="text-[10px] text-gray-400">{{ conversationCountText }}</span>
                <button type="button" title="从服务端刷新列表" class="text-gray-400 hover:text-blue-600"
                  @click="refreshList">
                  <RefreshCw class="h-3 w-3" />
                </button>
              </div>
            </div>

            <p v-if="workbenchState.conversationList.length === 0" class="text-xs text-gray-400">
              <template v-if="searchInput.trim()">未匹配到「{{ searchInput.trim() }}」</template>
              <template v-else>暂无会话，可在右侧填报发起</template>
            </p>

            <button v-for="item in workbenchState.conversationList" :key="item.session_id" type="button"
              class="mb-2 w-full rounded-md border p-2 text-left transition hover:bg-white" :class="sessionId === item.session_id
                ? 'border-blue-500 bg-blue-50'
                : 'border-gray-200 bg-white'
                " @click="selectConversation(item)">
              <div class="flex items-center gap-1.5">
                <ClipboardList class="h-3.5 w-3.5 shrink-0 text-gray-400" />
                <span class="truncate text-xs font-medium text-gray-800">
                  {{ item.title || item.session_id }}
                </span>
              </div>
              <div class="mt-1 flex items-center gap-1.5">
                <span class="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] text-amber-700">
                  {{ item.status }}
                </span>
                <span v-if="item.related_count > 0"
                  class="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] text-blue-700">
                  {{ sessionCountLabel }} {{ item.related_count }} 条
                </span>
                <span class="ml-auto text-[10px] text-gray-400">
                  {{ formatDateTime(item.last_activity_at) }}
                </span>
              </div>
            </button>

            <!-- 显示更多：服务端仍有未加载条目时按页追加 -->
            <button v-if="workbenchState.conversationHasMore && workbenchState.conversationSource === 'server'"
              type="button"
              class="mt-1 w-full rounded-md border border-dashed border-gray-300 py-1.5 text-xs text-gray-500 transition hover:bg-gray-50"
              @click="workbench.loadMoreConversations()">
              显示更多（还有 {{ remainingCount }} 条）
            </button>
          </template>

          <!-- Tab2：待办审批池由中栏独立拉取，此处不渲染其数据 -->
          <p v-else class="text-xs text-gray-400">
            待办审批池见中栏
          </p>
        </div>

        <!-- 本地缓存运维（服务端为权威源，此处仅为离线兜底快照） -->
        <div class="border-t p-3">
          <p class="text-[10px] text-gray-500">
            本地缓存：{{ cacheStatusText }}
          </p>
          <button type="button" title="仅清理本机缓存，服务端数据不受影响"
            class="mt-1 flex items-center gap-1 text-[10px] text-gray-500 hover:text-red-600"
            @click="wipeLocalSnapshot">
            <Trash2 class="h-3 w-3" />
            清除本地缓存
          </button>
        </div>
      </aside>

      <!-- 中栏 -->
      <main class="flex flex-1 flex-col overflow-hidden bg-white">
        <div class="border-b px-4 py-2 text-xs text-gray-500">
          session_id: {{ sessionId }}
        </div>

        <!-- 离线兜底提示：如实告知当前显示的不是权威数据 -->
        <p v-if="messageOfflineHint" class="border-b bg-amber-50 px-4 py-1.5 text-xs text-amber-700">
          {{ messageOfflineHint }}
        </p>

        <div class="flex-1 overflow-hidden">
          <TaskHistoryList v-if="activeTab === 'history'" :session-id="sessionId" :current-user-id="currentIdentity.id"
            :messages="workbenchState.activeMessages" :inspected-thread-id="workbenchState.inspectedThreadId"
            :inspected-card-type="workbenchState.inspectedCardType" @inspect="handleInspect" />
          <TaskApprovalPool v-else :current-user-id="currentIdentity.id" :current-role="currentIdentity.role" />
        </div>

        <!-- 通用输入 -->
        <div class="border-t p-3">
          <div class="flex items-center gap-2">
            <input v-model="draftText" type="text"
              class="flex-1 rounded-md border border-gray-300 px-3 py-1.5 text-sm disabled:bg-gray-50"
              placeholder="输入消息…" :disabled="isStreaming || loggedOut" @keyup.enter="sendDraft" />
            <button type="button" class="rounded-md bg-blue-600 px-3 py-1.5 text-sm text-white disabled:opacity-50"
              :disabled="isStreaming || !draftText.trim() || loggedOut" @click="sendDraft">
              {{ isStreaming ? '生成中…' : '发送' }}
            </button>
            <button type="button" class="rounded-md border border-gray-300 px-2 py-1.5 text-xs hover:bg-gray-50"
              title="展开右栏业务表单" @click="workbench.setRightDrawer(true)">
              📋 快速填报
            </button>
          </div>

          <!-- 流异常提示：明确反馈，不静默失败 -->
          <p v-if="streamError" class="mt-1.5 text-xs text-red-600">
            {{ streamError }}
          </p>
        </div>
      </main>

      <!-- 右栏 -->
      <!-- 折叠态保留 w-10 把手轨道：absolute 按钮需相对本 aside 定位，
           若容器宽度归零则把手仍会溢出视口外，导致右栏无法重新展开。 -->
      <aside class="relative flex flex-col border-l bg-gray-50/50 transition-all duration-300"
        :class="rightOpen ? (inspectionOpen ? 'w-96' : 'w-80') : 'w-10'">
        <!-- v-show 而非 v-if：折叠时不销毁业务表单，已填数据得以保留 -->
        <div v-show="rightOpen" class="flex h-full flex-col">
          <div class="flex items-center justify-between p-3">
            <span class="text-xs font-medium">工作台侧栏</span>
            <button type="button" class="text-xs text-gray-500" @click="workbench.setRightDrawer(false)">
              收起
            </button>
          </div>

          <!-- 区1：只读详情检视（与下方表单区之间以分割线明确分区） -->
          <div v-if="inspectionOpen" class="border-b border-gray-200 px-3 pb-3">
            <div class="mb-2 flex items-center justify-between">
              <span class="text-xs font-medium">详情检视</span>
              <button type="button" class="text-[11px] text-gray-500 hover:text-gray-700"
                @click="workbench.closeInspection()">
                关闭
              </button>
            </div>
            <component :is="inspectorComponent" v-if="inspectorComponent"
              :thread-id="workbenchState.inspectedThreadId" />
            <p v-else class="text-[11px] text-gray-400">
              该卡片类型未注册只读检视视图
            </p>
          </div>

          <!-- 区2：业务表单（v-show 保活，草稿不因折叠 / 检视而丢失） -->
          <div class="flex-1 overflow-y-auto p-3">
            <p class="mb-2 text-xs font-medium">
              {{ formSectionTitle }}
            </p>
            <slot name="business-form" :session-id="sessionId">
              <p class="text-xs text-gray-400">
                业务插件表单位（后续挂载）
              </p>
            </slot>
          </div>

          <!-- Trace -->
          <div class="border-t p-3">
            <p class="mb-1 text-xs font-medium">运行追踪</p>
            <p class="text-[10px] text-gray-400">
              latency_ms / token 消耗（暂无数据）
            </p>
          </div>
        </div>

        <!-- 折叠态重新展开把手：相对 aside 定位（left-0），不再逃逸出 overflow-hidden -->
        <button v-if="!rightOpen" type="button"
          class="absolute left-0 top-2 z-10 h-8 w-6 rounded-l-md border border-r-0 border-gray-300 bg-white text-sm leading-none text-gray-500 shadow-sm hover:bg-gray-50 hover:text-gray-700"
          title="展开右栏" @click="workbench.setRightDrawer(true)">
          ›
        </button>
      </aside>
    </div>
  </div>
</template>
