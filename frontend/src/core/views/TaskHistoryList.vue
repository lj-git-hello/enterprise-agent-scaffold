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
}>()

/** 卡片动作事件上抛（宿主据此刷新左栏 / 剔除失效项） */
const emit = defineEmits<{
  decided: [permitted: boolean]
  conflict: []
}>()

// 卡片动作上下文（dispatch 占位，业务动作后续注入）
function makeActionContext(message: ChatMessageRecord): CardActionContext {
  return {
    threadId: message.thread_id,
    retryPayload: message.retry_payload,
    dispatch: () => undefined,
  }
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
        class="w-full max-w-md rounded-lg border border-gray-200 bg-white p-3 shadow-sm"
      >
        <MessageCardHost
          :message="message"
          :action-context="makeActionContext(message)"
          @decided="(permitted: boolean) => emit('decided', permitted)"
          @conflict="emit('conflict')"
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
