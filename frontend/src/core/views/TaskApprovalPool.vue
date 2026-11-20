<script setup lang="ts">
/**
 * TaskApprovalPool —— 通用待办审批看板。
 *
 * 拉取 WAITING_HUMAN 且 assigned_role 匹配的任务；展示锁版本徽章，
 * 点击展开详情（卡片经 MessageCardHost）；核准/驳回走 useWorkflowAction，
 * 409 冲突时自动刷新剔除脏任务。
 */
import { computed, onMounted, ref, watch } from 'vue'

import MessageCardHost from '../a2ui/MessageCardHost.vue'
import type {
  CardActionContext,
  ChatMessageRecord,
  TaskSummary,
} from '../a2ui/types'
import { http } from '../api/http'
import { useWorkflowAction } from '../composables/useWorkflowAction'
import { PluginRegistry } from '../plugins/registry'
import { useWorkbenchStore } from '../workbench/useWorkbenchStore'

const props = defineProps<{
  currentUserId: string
  currentRole: string
}>()

const workbench = useWorkbenchStore()

// 端点由插件注册表下发（后端 manifest 为唯一真相源）；core 不写死任何业务路径
const endpoints = computed(() => PluginRegistry.activeEndpoints.value)

/** 端点缺失（插件未注册 / 清单未同步）时的显式配置错误 */
const configError = ref<string | null>(null)

const tasks = ref<TaskSummary[]>([])
const selectedThreadId = ref<string | null>(null)
/**
 * 打开详情时的任务快照。
 * 审批后该任务会离开待办池（列表里再也查不到），
 * 但详情仍需展示终态卡片，且快照持有提交审批所需的锁版本。
 */
const selectedTask = ref<TaskSummary | null>(null)
const detailMessages = ref<ChatMessageRecord[]>([])

/**
 * 选中任务是否仍在待办池中。
 * 判据必须业务中立：core 层不得知道任何业务卡片类型，
 * 只能用「是否仍在待审批队列」这一通用事实驱动底部操作条显隐。
 */
const stillPending = computed(() =>
  tasks.value.some((t) => t.thread_id === selectedThreadId.value),
)

const { isSubmitting, submitAudit, notice } = useWorkflowAction()

let loaded = false

/**
 * 上次拉取时的工作流事件节拍。
 *
 * 用于判定"离开 Tab 期间是否发生过状态变更"：发生过则列表可能陈旧，
 * 重新进入时必须重拉；没发生过则无需重复请求。
 */
let fetchedAtTick = -1

async function fetchTasks(force = false) {
  const tasksUrl = endpoints.value?.tasks
  if (!tasksUrl) {
    configError.value =
      '工作流端点未配置：业务插件未注册或后端清单未同步，请检查插件装载状态。'
    return
  }
  configError.value = null

  // 发起时的生效身份：响应回来时身份若已变，这份数据属于旧身份，必须丢弃
  const ownerId = workbench.state.activeOwnerId
  // 角色由后端按 JWT 判定，前端不传任何角色参数
  const { data } = await http.get<{ items: TaskSummary[] }>(tasksUrl)
  if (ownerId !== workbench.state.activeOwnerId) return
  tasks.value = data.items ?? []
  loaded = true
  fetchedAtTick = workbench.state.workflowEventTick
  void force
}

/** Tab 激活时调用：首次进入懒加载；期间发生过状态变更则重拉。 */
async function activate() {
  if (!loaded || fetchedAtTick !== workbench.state.workflowEventTick) {
    await fetchTasks()
  }
}

/**
 * 按 thread_id 读取该线程的消息 / 卡片快照。
 *
 * 只依赖 thread_id，不依赖任务是否仍在待办池 —— 审批后任务离池，
 * 但详情仍须能重拉并展示终态卡片（审批结论写在原卡上）。
 */
async function loadDetail(threadId: string) {
  const messagesUrl = endpoints.value?.messages
  if (!messagesUrl) {
    configError.value =
      '工作流端点未配置：业务插件未注册或后端清单未同步，请检查插件装载状态。'
    return
  }
  const ownerId = workbench.state.activeOwnerId
  try {
    const { data } = await http.get<{ items: ChatMessageRecord[] }>(
      messagesUrl,
      { params: { thread_id: threadId } },
    )
    // 身份在请求期间已变 → 丢弃旧身份的详情
    if (ownerId !== workbench.state.activeOwnerId) return
    detailMessages.value = data.items ?? []
  } catch {
    if (ownerId !== workbench.state.activeOwnerId) return
    detailMessages.value = []
  }
}

async function openDetail(task: TaskSummary) {
  selectedThreadId.value = task.thread_id
  selectedTask.value = task
  await loadDetail(task.thread_id)
}

async function decide(approved: boolean) {
  const resumeUrl = endpoints.value?.resume
  if (!resumeUrl) {
    configError.value =
      '工作流端点未配置：业务插件未注册或后端清单未同步，请检查插件装载状态。'
    return
  }
  const threadId = selectedThreadId.value
  const task = selectedTask.value
  if (!threadId || !task) {
    return
  }
  await submitAudit(
    resumeUrl,
    {
      thread_id: threadId,
      lock_version: task.lock_version,
      approved,
      opinion: approved ? '核准许可' : '驳回修改',
    },
    // 成功：刷新待办与详情，并标记左栏会话集合陈旧
    // （终态与锁版本以服务端为准，左栏必须重新对齐，否则显示陈旧态）
    async () => {
      await fetchTasks()
      workbench.markConversationsStale()
      // 详情不清空：重拉后展示终态卡片（审批结论就写在原审核卡上）
      await loadDetail(threadId)
    },
    // 409 冲突：任务已被处理，刷新剔除并重拉详情（此时已是终态）
    async () => {
      await fetchTasks()
      await loadDetail(threadId)
    },
  )
}

function makeActionContext(message: ChatMessageRecord): CardActionContext {
  return {
    threadId: message.thread_id,
    retryPayload: message.retry_payload,
    dispatch: () => undefined,
  }
}

/**
 * 卡片内直接审批（通过 / 驳回）后刷新待办池。
 *
 * 终态任务必须从待办池剔除，否则用户会看到已处理的单据仍在列表里；
 * 但详情不清空 —— 重拉后展示终态卡片，让操作者看到审批结果。
 */
async function onCardDecided() {
  await fetchTasks()
  if (selectedThreadId.value) {
    await loadDetail(selectedThreadId.value)
  }
}

/** 409 冲突：任务已被他人处理，刷新剔除并重拉详情（此时已是终态） */
async function onCardConflict() {
  await fetchTasks()
  if (selectedThreadId.value) {
    await loadDetail(selectedThreadId.value)
  }
}

// 工作流状态变更事件（跨端推送）→ 待办池实时对齐，无需人工刷新
watch(
  () => workbench.state.workflowEventTick,
  () => {
    void fetchTasks()
  },
)

/**
 * 生效身份变化 → 旧身份的待办与详情立即失效，并按新身份重拉。
 *
 * 判据用"生效身份"（凭据真正对应的用户）而非下拉框选中值：
 * 选中值先变、凭据后换，用选中值会在新令牌尚未生效时就去取数。
 */
watch(
  () => workbench.state.activeOwnerId,
  async (ownerId) => {
    // 先清空：旧身份的待办与详情不得留在界面上
    tasks.value = []
    selectedThreadId.value = null
    selectedTask.value = null
    detailMessages.value = []
    loaded = false
    fetchedAtTick = -1
    if (!ownerId) return
    await activate()
  },
)

// 组件仅在切到审批池 Tab 时挂载，挂载即懒加载（首次点击才发请求）
onMounted(activate)

defineExpose({ refresh: fetchTasks, activate })
</script>

<template>
  <div class="flex h-full overflow-hidden">
    <!-- 待办列表 -->
    <div class="w-64 shrink-0 overflow-y-auto border-r border-gray-200 p-3">
      <p
        v-if="configError"
        class="mb-2 rounded-md bg-red-50 p-2 text-xs text-red-600"
      >
        {{ configError }}
      </p>

      <p v-if="tasks.length === 0" class="text-sm text-gray-400">
        暂无待办任务
      </p>

      <button
        v-for="task in tasks"
        :key="task.thread_id"
        type="button"
        class="mb-2 w-full rounded-md border p-2 text-left transition hover:bg-gray-50"
        :class="
          selectedThreadId === task.thread_id
            ? 'border-blue-500 bg-blue-50'
            : 'border-gray-200'
        "
        @click="openDetail(task)"
      >
        <p class="truncate text-xs font-medium text-gray-800">
          {{ task.thread_id }}
        </p>
        <div class="mt-1 flex items-center gap-2">
          <span
            class="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] text-amber-700">{{ task.status }}</span>
          <span class="text-[10px] text-gray-500">
            Lock: v{{ task.lock_version }}
          </span>
        </div>
      </button>
    </div>

    <!-- 详情主区 -->
    <div class="flex flex-1 flex-col overflow-hidden">
      <div class="flex-1 overflow-y-auto p-4">
        <p v-if="!selectedThreadId" class="text-sm text-gray-400">
          请选择左侧待办查看详情
        </p>

        <div
          v-for="message in detailMessages"
          :key="message.id"
          class="mb-3"
        >
          <MessageCardHost
            v-if="message.kind === 'card'"
            :message="message"
            :action-context="makeActionContext(message)"
            @decided="onCardDecided"
            @conflict="onCardConflict"
          />
          <p v-else class="text-sm text-gray-700">{{ message.content }}</p>
        </div>
      </div>

      <!-- 核准 / 驳回：仅当选中任务仍在待办池中才可操作 -->
      <div
        v-if="selectedThreadId && stillPending"
        class="flex items-center gap-2 border-t border-gray-200 p-3"
      >
        <button
          type="button"
          class="rounded-md bg-green-600 px-3 py-1.5 text-sm text-white disabled:opacity-50"
          :disabled="isSubmitting"
          @click="decide(true)"
        >
          核准
        </button>
        <button
          type="button"
          class="rounded-md bg-gray-200 px-3 py-1.5 text-sm text-gray-700 disabled:opacity-50"
          :disabled="isSubmitting"
          @click="decide(false)"
        >
          驳回
        </button>
        <p v-if="notice" class="text-xs text-amber-600">
          {{ notice.message }}
        </p>
      </div>
    </div>
  </div>
</template>
