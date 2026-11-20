<script setup lang="ts">
/**
 * MessageCardHost —— 卡片宿主。
 *
 * 根据 message.card_type 从 CardRegistry 动态解析卡片组件；
 * 未注册时渲染 JSON 兜底卡片，保证消息始终可读、不白屏。
 */
import { computed } from 'vue'

import { CardRegistry } from './CardRegistry'
import type { CardActionContext, ChatMessageRecord } from './types'

const props = defineProps<{
  message: ChatMessageRecord
  actionContext?: CardActionContext
}>()

/**
 * 卡片动作事件透传。
 *
 * 卡片组件内部自行提交后端（依赖反转），宿主的职责只是把结果上报给
 * 上层：decided = 审批完成（需刷新左栏），conflict = CAS 冲突（项已失效），
 * inspect = 用户点击卡片，请求打开只读详情检视。
 */
const emit = defineEmits<{
  decided: [permitted: boolean]
  conflict: []
  inspect: [message: ChatMessageRecord]
}>()

/**
 * 卡片点击 → 上抛只读检视请求。
 *
 * 交互控件（按钮 / 链接 / 输入控件）上的点击一律忽略：
 * 用户点"核准/驳回"时不该顺手打开只读面板，否则审批动作会被误读成"查看详情"。
 *
 * core 不判断卡片类型是否可检视 —— 是否存在对应检视视图由插件注册表决定。
 */
function handleCardClick(event: MouseEvent): void {
  const target = event.target as HTMLElement | null
  if (target?.closest('button, a, input, textarea, select')) {
    return
  }
  emit('inspect', props.message)
}

// 动态组件（未注册时为 undefined → 走兜底）
const resolved = computed(() =>
  props.message.card_type ? CardRegistry.get(props.message.card_type) : undefined,
)

// 兜底展示的 JSON 文本
const fallbackJson = computed(() =>
  JSON.stringify(props.message.card_payload ?? {}, null, 2),
)
</script>

<template>
  <div class="w-full" @click="handleCardClick">
    <component
      :is="resolved"
      v-if="resolved"
      :message="message"
      :action-context="actionContext"
      @decided="(permitted: boolean) => emit('decided', permitted)"
      @conflict="emit('conflict')"
    />

    <!-- 未注册卡片：JSON 兜底 -->
    <div
      v-else
      class="rounded-md border border-dashed border-gray-300 bg-gray-50 p-3"
    >
      <p class="mb-2 text-xs text-gray-500">
        未注册卡片类型：{{ message.card_type ?? '（空）' }}
      </p>
      <pre
        class="overflow-x-auto whitespace-pre-wrap break-all text-xs text-gray-700">{{ fallbackJson }}</pre>
    </div>
  </div>
</template>
