<script setup lang="ts">
/**
 * PermitResultCard —— 终态调度许可凭证卡片（业务插件）。
 *
 * 展示调度令编号、许可时间、CA 签名摘要，并将签名渲染为现场核验二维码。
 *
 * 离线兜底快照会剔除签名摘要与调度令编号（见插件脱敏器），
 * 此时必须优雅降级：缺字段不抛错、不渲染残缺二维码。
 */
import { computed } from 'vue'
import QrcodeVue from 'qrcode.vue'

import type { ChatMessageRecord } from '@/core/a2ui/types'

interface PermitInfo {
  permit_no?: string
  ca_signature_hash?: string
  issued_at?: string
}

const props = defineProps<{
  message: ChatMessageRecord
}>()

const payload = computed<Record<string, unknown>>(
  () => props.message.card_payload ?? {},
)

const permitInfo = computed<PermitInfo | null>(
  () => (payload.value.permit_info as PermitInfo | undefined) ?? null,
)

const status = computed(() => String(payload.value.status ?? ''))

/** 是否为定级后自动签发（无人工核签环节） */
const autoIssued = computed(() => payload.value.auto_issued === true)

/** 签发说明（自动签发时给出定级理由；人工核签时为核签意见） */
const opinion = computed(() => String(payload.value.opinion ?? ''))

const permitNo = computed(() => permitInfo.value?.permit_no ?? '')

const issuedAt = computed(() => permitInfo.value?.issued_at ?? '')

/** 签名摘要（脱敏后可能缺省） */
const signatureDigest = computed(() => {
  const hash = permitInfo.value?.ca_signature_hash
  return hash ? `${hash.slice(0, 24)}…` : ''
})

/**
 * 二维码内容：签名摘要 + 编号。
 *
 * 任一要素缺失即不渲染二维码：残缺二维码会让现场核验误判。
 */
const qrValue = computed(() => {
  const info = permitInfo.value
  if (!info?.ca_signature_hash || !info.permit_no) return ''
  return `${info.permit_no}|${info.ca_signature_hash}`
})
</script>

<template>
  <div class="flex flex-col gap-3">
    <div class="flex items-center justify-between">
      <p class="text-sm font-medium text-gray-800">调度许可凭证</p>
      <div class="flex items-center gap-1.5">
        <span
          v-if="autoIssued"
          class="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] text-blue-700"
        >自动签发</span>
        <span
          class="rounded-full px-2 py-0.5 text-[10px]"
          :class="
            status === 'PERMITTED'
              ? 'bg-green-100 text-green-700'
              : 'bg-gray-100 text-gray-600'
          ">{{ status }}</span>
      </div>
    </div>

    <p
      v-if="opinion"
      class="rounded-md bg-gray-50 px-2 py-1.5 text-[11px] leading-relaxed text-gray-600"
    >{{ opinion }}</p>

    <template v-if="permitInfo">
      <dl class="flex flex-col gap-1 text-xs text-gray-600">
        <div class="flex gap-2">
          <dt class="shrink-0 text-gray-400">调度令编号：</dt>
          <dd class="break-all">{{ permitNo || '—' }}</dd>
        </div>
        <div class="flex gap-2">
          <dt class="shrink-0 text-gray-400">许可时间：</dt>
          <dd>{{ issuedAt || '—' }}</dd>
        </div>
        <div class="flex gap-2">
          <dt class="shrink-0 text-gray-400">CA 签名：</dt>
          <dd class="break-all font-mono text-[10px]">
            {{ signatureDigest || '本地快照未缓存签名摘要' }}
          </dd>
        </div>
      </dl>

      <div v-if="qrValue" class="flex justify-center rounded-md border border-gray-200 bg-white p-2">
        <QrcodeVue
          :value="qrValue"
          :size="120"
          level="M"
          render-as="svg"
        />
      </div>
      <p v-if="qrValue" class="text-center text-[10px] text-gray-400">现场扫码核验凭证</p>
    </template>

    <p v-else class="text-xs text-gray-400">暂无许可信息</p>
  </div>
</template>
