<script setup lang="ts">
/**
 * TaskHistoryList —— 提报员历史时间线（通用）。
 *
 * 只渲染外部传入的 props.messages：消息来源由工作台 store 统一裁决
 * （服务端优先，本地快照仅作离线兜底），本视图**不再自行从 IndexedDB
 * 恢复**——否则会用本地数据覆盖服务端权威流。
 *
 * 角色标识：用户消息右置 + 用户头像，助手消息左置 + 智能体头像，
 * 使对话双方归属一目了然；头像为纯装饰，不承载任何业务判定。
 */
import { Bot, Loader2, User } from 'lucide-vue-next'

import MessageCardHost from '../a2ui/MessageCardHost.vue'
import type { CardActionContext, ChatMessageRecord } from '../a2ui/types'
import { renderMarkdown } from '@/core/utils/markdown'

const props = defineProps<{
  sessionId: string
  currentUserId: string
  /** 外部响应式消息列表（store 持有） */
  messages: ChatMessageRecord[]
  /** 正在被只读检视的线程标识（用于卡片高亮；空串表示无） */
  inspectedThreadId?: string
  /** 正在被只读检视的卡片类型（用于卡片高亮；空串表示无） */
  inspectedCardType?: string
  /**
   * 等待首个 token 的助手消息 ID（空串表示无）。
   *
   * 由 store 登记与清除：请求发出时登记，收到正文 / 流结束 / 出错时清空。
   * 本视图据此在机器人头像旁渲染"思考中"状态胶囊。
   */
  pendingReplyId?: string
}>()

/** 卡片动作事件上抛（宿主据此刷新左栏 / 剔除失效项） */
const emit = defineEmits<{
  decided: [permitted: boolean]
  conflict: []
  /** 用户点击卡片 → 请求打开只读详情检视 */
  inspect: [message: ChatMessageRecord]
}>()

/**
 * 等待提示与正文之间的交接动效。
 *
 * 直接复用 Tailwind 工具类作为过渡类，无需额外 <style> 块，
 * 保持组件样式单一来源。
 */
const FADE_TRANSITION = {
  enterActiveClass: 'transition-opacity duration-200 ease-out',
  enterFromClass: 'opacity-0',
  leaveActiveClass: 'transition-opacity duration-150 ease-in',
  leaveToClass: 'opacity-0',
}

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

/** 是否已收到正文（未收到时不渲染空气泡）。 */
function hasContent(message: ChatMessageRecord): boolean {
  return (message.content ?? '').length > 0
}

/**
 * 该消息是否处于「首字等待」状态。
 *
 * 判据：它就是本轮登记的等待对象，且尚未收到任何正文。
 * 首个 token 落地 / 流结束 / 出错，等待态即被 store 清空，这里随之恢复。
 */
function isAwaitingReply(message: ChatMessageRecord): boolean {
  if (!props.pendingReplyId) return false
  if (message.id !== props.pendingReplyId) return false
  return !hasContent(message)
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
      class="flex items-start gap-2"
      :class="message.role === 'user' ? 'justify-end' : 'justify-start'"
    >
      <!-- 助手头像：左置徽章，与助手消息（纯文本 / A2UI 卡片）同侧 -->
      <div
        v-if="message.role !== 'user'"
        class="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-slate-200/80 bg-slate-100 text-indigo-600"
        title="智能体"
      >
        <Bot class="h-4 w-4" />
      </div>

      <!-- 消息体：卡片与文本共用同一宽度上限容器，头像不挤压正文排版 -->
      <div class="min-w-0 max-w-md">
        <!-- 卡片类型 -->
        <div
          v-if="message.kind === 'card'"
          class="w-full rounded-lg border bg-white p-3 shadow-sm transition"
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

        <!-- 文本气泡：收到正文才渲染，避免留下无内容的空气泡 -->
        <Transition v-bind="FADE_TRANSITION">
          <div
            v-if="message.kind !== 'card' && hasContent(message)"
            class="rounded-lg px-3 py-2 text-sm"
            :class="
              message.role === 'user'
                ? 'bg-blue-600 text-white'
                : 'bg-gray-100 text-gray-800'
            "
            v-html="textHtml(message)"
          />
        </Transition>

        <!-- 首字等待提示：请求已发出、首个 token 尚未落地 -->
        <Transition v-bind="FADE_TRANSITION">
          <div
            v-if="message.kind !== 'card' && isAwaitingReply(message)"
            class="flex w-fit items-center gap-2 rounded-full bg-gray-100 px-3 py-1.5 text-xs text-gray-500"
          >
            <Loader2 class="h-3.5 w-3.5 shrink-0 animate-spin text-indigo-500" />
            <span>正在检索知识库并生成回答，请稍候...</span>
          </div>
        </Transition>
      </div>

      <!-- 用户头像：右置徽章，与右侧消息气泡对齐 -->
      <div
        v-if="message.role === 'user'"
        class="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-indigo-100 text-indigo-700"
        title="用户"
      >
        <User class="h-4 w-4" />
      </div>
    </div>
  </div>
</template>
