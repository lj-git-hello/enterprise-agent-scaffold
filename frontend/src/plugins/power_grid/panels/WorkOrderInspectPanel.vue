<script setup lang="ts">
/**
 * WorkOrderInspectPanel —— 检修工单只读详情检视面板（业务插件）。
 *
 * 纯只读：无审批 / 提交 / 重试按钮，只呈现服务端权威数据。
 * 数据源为通用只读端点 GET /api/v1/threads/{thread_id}（线程元信息 + 卡片快照），
 * core 不知道任何工单字段 —— 字段语义全部落在本插件。
 *
 * 实时同步：
 * - 跨端推送（工作流事件节拍）→ 重新拉取；
 * - 本端操作导致卡片原地改写（card_version 递增）→ 重新拉取。
 *
 * 缺字段如实显示「—」，不静默显示空值；拉取失败保留旧数据并显式提示。
 */
import { computed, ref, watch } from 'vue'

import type { CardSnapshot, ThreadDetail } from '@/core/a2ui/types'
import { fetchThreadDetail } from '@/core/api/threads'
import { formatDateTime } from '@/core/utils/datetime'
import { useWorkbenchStore } from '@/core/workbench/useWorkbenchStore'

const props = defineProps<{
  /** 被检视的线程标识 */
  threadId: string
}>()

/** 卡片类型（后端 card_type） */
const CARD_TYPE_AUDIT = 'dispatcher_audit'
const CARD_TYPE_PERMIT = 'permit_result'

/** 缺字段占位符 */
const PLACEHOLDER = '—'

const workbench = useWorkbenchStore()
const { state: workbenchState } = workbench

const detail = ref<ThreadDetail | null>(null)
const cards = ref<CardSnapshot[]>([])
const loading = ref(false)
const syncError = ref<string | null>(null)

/** 把任意字段转为展示文本；缺省一律显示占位符。 */
function text(value: unknown): string {
  if (value === null || value === undefined) return PLACEHOLDER
  const raw = String(value).trim()
  return raw ? raw : PLACEHOLDER
}

function findCard(cardType: string): CardSnapshot | null {
  return cards.value.find((card) => card.card_type === cardType) ?? null
}

const auditCard = computed(() => findCard(CARD_TYPE_AUDIT))
const permitCard = computed(() => findCard(CARD_TYPE_PERMIT))

function payloadOf(card: CardSnapshot | null): Record<string, unknown> {
  const payload = card?.card_payload
  return payload && typeof payload === 'object'
    ? (payload as Record<string, unknown>)
    : {}
}

const auditPayload = computed(() => payloadOf(auditCard.value))
const permitPayload = computed(() => payloadOf(permitCard.value))

const permitInfo = computed<Record<string, unknown>>(() => {
  const info = permitPayload.value.permit_info
  return info && typeof info === 'object'
    ? (info as Record<string, unknown>)
    : {}
})

/** 合规校验结果（违规项列表） */
const violations = computed<string[]>(() => {
  const raw = auditPayload.value.violations
  if (!Array.isArray(raw)) return []
  return raw.map((item) => String(item)).filter((item) => item.trim().length > 0)
})

/** 状态：线程状态与卡片状态同源，以服务端线程状态为准 */
const status = computed(() => text(detail.value?.status))

/** 自动签发（无人工核签环节） */
const autoIssued = computed(() => permitPayload.value.auto_issued === true)

interface DetailRow {
  label: string
  value: string
}

/** 工单主体信息 */
const workOrderRows = computed<DetailRow[]>(() => [
  { label: '工单号', value: text(auditPayload.value.biz_order_id) },
  { label: '变电站', value: text(auditPayload.value.substation) },
  { label: '电压等级', value: text(auditPayload.value.voltage_level) },
  { label: '作业内容', value: text(auditPayload.value.operation_task) },
])

/** 流程与人员信息 */
const processRows = computed<DetailRow[]>(() => [
  { label: '状态', value: status.value },
  { label: '核签人', value: text(auditPayload.value.decided_by) },
  { label: '核签时间', value: text(auditPayload.value.decided_at) },
  { label: '核签意见', value: text(auditPayload.value.opinion) },
  { label: '提报人', value: text(detail.value?.creator_id) },
  { label: '提报时间', value: formatDateTime(detail.value?.created_at) },
  { label: '流程节点', value: text(detail.value?.current_node) },
])

/** 许可信息（人工核签签发或定级后自动签发） */
const permitRows = computed<DetailRow[]>(() => [
  { label: '调度令编号', value: text(permitInfo.value.permit_no) },
  { label: '许可时间', value: text(permitInfo.value.issued_at) },
  { label: '签发方式', value: autoIssued.value ? '自动签发' : '人工核签' },
])

/**
 * 拉取线程详情。
 *
 * 请求期间线程被切换（props 变化）时丢弃旧响应，避免详情与标题错位。
 */
async function load(): Promise<void> {
  const threadId = props.threadId
  if (!threadId) {
    detail.value = null
    cards.value = []
    return
  }
  loading.value = true
  try {
    const resp = await fetchThreadDetail(threadId)
    if (props.threadId !== threadId) return
    detail.value = resp.thread
    cards.value = resp.cards
    syncError.value = null
  } catch (error) {
    if (props.threadId !== threadId) return
    // 保留旧数据：宁可显示上一次拉取的内容并提示同步失败，也不清空成空白
    const status = (error as { status?: number }).status
    syncError.value = status
      ? `详情同步失败（HTTP ${status}），当前显示上一次拉取的内容`
      : '详情同步失败，当前显示上一次拉取的内容'
  } finally {
    if (props.threadId === threadId) {
      loading.value = false
    }
  }
}

// 线程切换 → 重新拉取
watch(
  () => props.threadId,
  () => {
    void load()
  },
  { immediate: true },
)

// 跨端工作流事件 → 重新拉取（终态即时呈现）
watch(
  () => workbenchState.workflowEventTick,
  () => {
    void load()
  },
)

/**
 * 中栏同线程卡片的版本签名。
 *
 * 卡片被原地改写（审批结论落卡）时 card_version 递增 —— 签名变化即需重拉，
 * 否则本端操作后的详情会停留在旧值。
 */
const cardVersionSignature = computed(() =>
  workbenchState.activeMessages
    .filter(
      (message) =>
        message.kind === 'card' && message.thread_id === props.threadId,
    )
    .map(
      (message) =>
        `${message.card_type ?? ''}:${message.card_version ?? 0}`,
    )
    .join('|'),
)

watch(cardVersionSignature, () => {
  void load()
})
</script>

<template>
  <div class="flex flex-col gap-3">
    <!-- 同步状态 -->
    <p v-if="syncError" class="rounded-md bg-amber-50 px-2 py-1 text-[11px] text-amber-700">
      {{ syncError }}
    </p>
    <p v-else-if="loading" class="text-[11px] text-gray-400">
      正在同步详情…
    </p>

    <p v-if="!detail" class="text-[11px] text-gray-400">
      暂无详情
    </p>

    <template v-else>
      <!-- 工单主体 -->
      <dl class="flex flex-col gap-1 text-xs text-gray-700">
        <div v-for="row in workOrderRows" :key="row.label" class="flex gap-2">
          <dt class="w-20 shrink-0 text-gray-400">{{ row.label }}</dt>
          <dd class="break-all">{{ row.value }}</dd>
        </div>
      </dl>

      <!-- 合规校验结果 -->
      <div class="rounded-md bg-amber-50 p-2">
        <p class="text-[11px] font-medium text-amber-700">合规校验结果</p>
        <ul v-if="violations.length" class="mt-1 list-disc pl-4 text-[11px] text-amber-700">
          <li v-for="item in violations" :key="item">{{ item }}</li>
        </ul>
        <p v-else class="mt-1 text-[11px] text-green-700">
          安措齐备，未发现违规项
        </p>
      </div>

      <!-- 流程与人员 -->
      <dl class="flex flex-col gap-1 text-xs text-gray-700">
        <div v-for="row in processRows" :key="row.label" class="flex gap-2">
          <dt class="w-20 shrink-0 text-gray-400">{{ row.label }}</dt>
          <dd class="break-all">{{ row.value }}</dd>
        </div>
      </dl>

      <!-- 许可信息 -->
      <div class="rounded-md border border-gray-200 bg-white p-2">
        <p class="mb-1 text-[11px] font-medium text-gray-700">调度许可</p>
        <dl class="flex flex-col gap-1 text-xs text-gray-700">
          <div v-for="row in permitRows" :key="row.label" class="flex gap-2">
            <dt class="w-20 shrink-0 text-gray-400">{{ row.label }}</dt>
            <dd class="break-all">{{ row.value }}</dd>
          </div>
        </dl>
      </div>
    </template>
  </div>
</template>
