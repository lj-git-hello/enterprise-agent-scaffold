<script setup lang="ts">
/**
 * TaskHistoryList —— 提报员历史时间线（通用）。
 *
 * 只渲染外部传入的 props.messages：消息来源由工作台 store 统一裁决
 * （服务端优先，本地快照仅作离线兜底），本视图**不再自行从 IndexedDB
 * 恢复**——否则会用本地数据覆盖服务端权威流。
 */
import MessageCardHost from '../a2ui/MessageCardHost.vue'
import type { CardActionContext, ChatMessageRecord } from '../a2ui/types'
import { renderMarkdown } from '@/core/utils/markdown'

defineProps<{
  sessionId: string
  currentUserId: string
  /** 外部响应式消息列表（store 持有） */
  messages: ChatMessageRecord[]
  /** 正在被只读检视的线程标识（用于卡片高亮；空串表示无） */
  inspectedThreadId?: string
  /** 正在被只读检视的卡片类型（用于卡片高亮；空串表示无） */
  inspectedCardType?: string
}>()

/** 卡片动作事件上抛（宿主据此刷新左栏 / 剔除失效项） */
const emit = defineEmits<{
  decided: [permitted: boolean]
  conflict: []
  /** 用户点击卡片 → 请求打开只读详情检视 */
  inspect: [message: ChatMessageRecord]
}>()

// 卡片动作上下文（dispatch 已接上检视通道；其余业务动作后续注入）
function makeActionContext(message: ChatMessageRecord): CardActionContext {
  return {
    threadId: message.thread_id,
    retryPayload: message.retry_payload,
    dispatch: (action: string) => {
      if (action === 'inspect') {
        emit('inspect', message)
      }
    },
  }
}

/**
 * 判断某张卡片是否正处于检视态（决定是否加高亮边框）。
 *
 * 匹配键为「线程 + 卡片类型」：同一线程内可能并存多种卡片，
 * 只按线程匹配会把多张卡片一起点亮。
 */
function isInspected(
  message: ChatMessageRecord,
  inspectedThreadId: string | undefined,
  inspectedCardType: string | undefined,
): boolean {
  if (!inspectedThreadId) return false
  if (message.thread_id !== inspectedThreadId) return false
  return (message.card_type ?? '') === (inspectedCardType ?? '')
}

function textHtml(message: ChatMessageRecord): string {
  return renderMarkdown(message.content ?? '')
}
</script>

<template>
  <div class="flex h-full flex-col gap-3 overflow-y-auto p-4">
    <p v-if="messages.length === 0" class="text-sm text-gray-400">
      暂无历史消息
    </p>

    <div
      v-for="message in messages"
      :key="message.id"
      class="flex"
      :class="message.role === 'user' ? 'justify-end' : 'justify-start'"
    >
      <!-- 卡片类型 -->
      <div
        v-if="message.kind === 'card'"
        class="w-full max-w-md rounded-lg border bg-white p-3 shadow-sm transition"
        :class="isInspected(message, inspectedThreadId, inspectedCardType)
          ? 'border-blue-500 ring-2 ring-blue-500'
          : 'border-gray-200'"
      >
        <MessageCardHost
          :message="message"
          :action-context="makeActionContext(message)"
          @decided="(permitted: boolean) => emit('decided', permitted)"
          @conflict="emit('conflict')"
          @inspect="(target: ChatMessageRecord) => emit('inspect', target)"
        />
      </div>

      <!-- 文本气泡 -->
      <div
        v-else
        class="max-w-md rounded-lg px-3 py-2 text-sm"
        :class="
          message.role === 'user'
            ? 'bg-blue-600 text-white'
            : 'bg-gray-100 text-gray-800'
        "
        v-html="textHtml(message)"
      />
    </div>
  </div>
</template>
