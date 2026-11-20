<script setup lang="ts">
/**
 * SafetyDiffCard —— 安全合规差异审核卡片（业务插件）。
 *
 * 展示电压等级、变电站、违规隐患；核准/驳回经 useWorkflowAction.submitAudit
 * 直接提交后端字段（is_permitted/opinion），携带 thread_id/lock_version；
 * 409 冲突时轻提示并通知父级剔除失效项。
 *
 * 卡片终态由服务端 payload.status 驱动（WAITING_HUMAN / PERMITTED / REJECTED）：
 * 已决策的单不再显示审批按钮，核签人 / 核签时间 / 核签意见直接呈现在卡面上。
 */
import { computed } from 'vue'

import { useWorkflowAction } from '@/core/composables/useWorkflowAction'
import { useCurrentUser } from '@/core/composables/useCurrentUser'
import type { ChatMessageRecord } from '@/core/a2ui/types'
import { PluginRegistry } from '@/core/plugins/registry'
import { useWorkbenchStore } from '@/core/workbench/useWorkbenchStore'

const props = defineProps<{
  message: ChatMessageRecord
}>()

const emit = defineEmits<{
  conflict: []
  decided: [permitted: boolean]
}>()

const payload = computed<Record<string, unknown>>(
  () => props.message.card_payload ?? {},
)

const threadId = computed(() => String(payload.value.thread_id ?? ''))
const lockVersion = computed(
  () => Number(payload.value.lock_version ?? 0),
)
const substation = computed(() => String(payload.value.substation ?? ''))
const voltageLevel = computed(
  () => String(payload.value.voltage_level ?? ''),
)
const violations = computed<string[]>(
  () => (payload.value.violations as string[] | undefined) ?? [],
)

// ---- 卡片终态（与后端状态机同源，不另立词表）----
const STATUS_PERMITTED = 'PERMITTED'
const STATUS_REJECTED = 'REJECTED'

const cardStatus = computed(() => String(payload.value.status ?? ''))
const decidedBy = computed(() => String(payload.value.decided_by ?? ''))
const decidedAt = computed(() => String(payload.value.decided_at ?? ''))
const opinion = computed(() => String(payload.value.opinion ?? ''))

/** 已核准 */
const isApproved = computed(() => cardStatus.value === STATUS_PERMITTED)
/** 已驳回 */
const isRejected = computed(() => cardStatus.value === STATUS_REJECTED)
/**
 * 仍待核签。
 * 存量卡片（本次改造前写入、payload 无 status）按待核签处理，
 * 避免历史工单因缺少状态字段而无法核签；CAS 才是最终并发裁决。
 */
const isPending = computed(() => !isApproved.value && !isRejected.value)

// 端点由插件注册表下发（后端 manifest 为唯一真相源），此处不硬编码业务路径
const auditUrl = computed(
  () => PluginRegistry.getActive()?.endpoints.resume ?? '',
)
const ROLE_DISPATCHER = 'DISPATCHER'

const { isSubmitting, notice, submitAudit } = useWorkflowAction()
const { hasAnyRole } = useCurrentUser()
const workbench = useWorkbenchStore()

/** 仅「调度长角色 + 待核签」可核签；已决策的卡片一律只读。 */
const canAudit = computed(
  () => hasAnyRole(ROLE_DISPATCHER) && isPending.value,
)

async function decide(permitted: boolean) {
  if (!auditUrl.value) {
    return
  }
  await submitAudit(
    auditUrl.value,
    {
      thread_id: threadId.value,
      lock_version: lockVersion.value,
      approved: permitted,
      opinion: permitted ? '核准许可' : '驳回修改',
      // 卡片级乐观锁期望版本：服务端据此拒绝覆盖他人结论
      card_version: props.message.card_version ?? 1,
    },
    // 成功：通知父级 + 中栏即时重拉（终态以服务端回读为准）
    // + 标记左栏陈旧（终态与锁版本一律以服务端为准）
    () => {
      emit('decided', permitted)
      workbench.markConversationsStale()
      void workbench.refreshMessages()
    },
    // 409：轻提示并让父级移除该失效项
    () => emit('conflict'),
  )
}
</script>

<template>
  <div class="flex flex-col gap-3">
    <div class="text-sm">
      <p class="font-medium text-gray-800">{{ substation }}</p>
      <p class="mt-0.5 text-xs text-gray-500">
        电压等级：{{ voltageLevel }}
      </p>
    </div>

    <!-- 风险隐患 -->
    <div class="rounded-md bg-amber-50 p-2">
      <p class="text-xs font-medium text-amber-700">合规校验结果</p>
      <ul
        v-if="violations.length"
        class="mt-1 list-disc pl-4 text-xs text-amber-700"
      >
        <li v-for="item in violations" :key="item">{{ item }}</li>
      </ul>
      <p v-else class="mt-1 text-xs text-green-700">
        安措齐备，未发现违规项
      </p>
    </div>

    <!-- CAS 冲突提示 -->
    <p v-if="notice?.type === 'conflict'" class="text-xs text-amber-600">
      【冲突拦截】该工单已被其他操作人处理或版本已失效
    </p>

    <!-- 终态①：已核准 -->
    <div v-if="isApproved" class="rounded-md bg-green-50 p-2">
      <p class="text-xs font-medium text-green-700">已核准</p>
      <p class="mt-1 text-xs text-green-700">
        核签人：{{ decidedBy || '—' }}
        <span v-if="decidedAt">（{{ decidedAt }}）</span>
      </p>
      <p v-if="opinion" class="mt-1 text-xs text-green-700">
        核签意见：{{ opinion }}
      </p>
    </div>

    <!-- 终态②：已驳回 -->
    <div v-else-if="isRejected" class="rounded-md bg-red-50 p-2">
      <p class="text-xs font-medium text-red-700">已驳回</p>
      <p class="mt-1 text-xs text-red-700">
        核签人：{{ decidedBy || '—' }}
        <span v-if="decidedAt">（{{ decidedAt }}）</span>
      </p>
      <p v-if="opinion" class="mt-1 text-xs text-red-700">
        驳回意见：{{ opinion }}
      </p>
    </div>

    <!-- 待核签：提报员只读等待提示 -->
    <p v-else-if="!canAudit" class="rounded-md bg-blue-50 px-2 py-1.5 text-xs text-blue-700">
      工单已挂起并流转至调度长审批池，等待核签中...
    </p>

    <!-- 待核签：调度长可操作 -->
    <div v-else class="flex gap-2">
      <button
        type="button"
        class="flex-1 rounded-md bg-green-600 px-3 py-1.5 text-sm text-white disabled:opacity-50"
        :disabled="isSubmitting"
        @click="decide(true)"
      >
        核准许可
      </button>
      <button
        type="button"
        class="flex-1 rounded-md bg-gray-200 px-2 py-1.5 text-sm text-gray-700 disabled:opacity-50"
        :disabled="isSubmitting"
        @click="decide(false)"
      >
        驳回修改
      </button>
    </div>
  </div>
</template>
