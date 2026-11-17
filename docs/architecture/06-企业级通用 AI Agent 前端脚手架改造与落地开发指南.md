## 企业级通用 AI Agent 前端脚手架改造与落地开发指南

### 一、 宏观架构设计与解耦模型

#### 1. 前后端对称解耦体系

后端通过 `GraphRegistry` 实现状态机与垂直业务的依赖反转，前端则基于 **`CardRegistry` 协议驱动 + 业务插槽路由 + 无业务流渲染调度引擎** 实现彻底的物理剥离。通用底座代码不包含任何具体行业字段（如“变电站”、“主变停电”、“安规条款”等），所有垂直业务均收拢至 `plugins/` 目录。

Plaintext

```
┌────────────────────────────────────────────────────────────────────────┐
│               垂直业务插件层 (plugins/power_grid/)                      │
│   • SafetyDiffCard.vue (安规对比卡片)                                  │
│   • PermitResultCard.vue (防伪签名二维码卡片)                          │
│   • ApplyTaskForm.vue (变电检修申报表单)                               │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ 动态注册 (Card & Component Plugins)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│             通用前端工程底座 (src/core/) —— 100% 跨业务通用             │
│                                                                        │
│  ┌───────────────────────────┐      ┌───────────────────────────────┐  │
│  │ A2UI 动态卡片分发容器     │      │ 60Hz 恒帧打字机渲染调度引擎   │  │
│  │ (CardRegistry / CardHost) │      │ (render_ticker.ts - rAF 缓冲) │  │
│  └───────────────────────────┘      └───────────────────────────────┘  │
│  ┌───────────────────────────┐      ┌───────────────────────────────┐  │
│  │ 通用 SSE 保活通信客户端   │      │ 本地持久化与防抖归档中枢      │  │
│  │ (:ping 过滤 / 退避重连)   │      │ (localforage - IndexedDB)     │  │
│  └───────────────────────────┘      └───────────────────────────────┘  │
│  ┌───────────────────────────┐      ┌───────────────────────────────┐  │
│  │ 通用 CAS 409 拦截控制器   │      │ 双角色插槽看板工作台骨架      │  │
│  │ (useWorkflowAction)       │      │ (提报历史流 vs 审批待办池)    │  │
│  └───────────────────────────┘      └───────────────────────────────┘  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ SSE / RESTful (带 JWT)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│           后端通用 FastAPI 底座 (ai_thread_mappings / CAS 乐观锁)       │
└────────────────────────────────────────────────────────────────────────┘
```

#### 2. 三大核心协议规约

1. **A2UI 数据分发协议**：前端消费 `ai_chat_messages` 表中的 `kind='card'` 记录，依据 `card_type` 字符串匹配插件注册的组件，直接将 `card_payload` 注入 Props。   
2. **CAS 乐观锁回传协议**：审批卡片提交恢复（Resume）请求时，强制携带当前卡片所绑定的 `lock_version`。若后端返回 `409 Conflict`，通用底座自动拦截并触发轻提示，刷新看板消除脏待办。   
3. **单会话多工单隔离协议**：前端窗口绑定唯一 `session_id`，后端在此会话下为每笔申报派生独立的 `thread_id`，保证界面保持多任务独立流转。   

### 二、 目标工程目录树规范

Plaintext

```
frontend/
├── src/
│   ├── core/                          # 【通用底座：严禁出现任何电力/业务代码】
│   │   ├── a2ui/
│   │   │   ├── CardRegistry.ts        # 卡片单例注册中心
│   │   │   ├── MessageCardHost.vue    # 通用卡片挂载容器组件
│   │   │   └── types.ts               # A2UI 核心数据结构与契约定义
│   │   ├── engine/
│   │   │   ├── render_ticker.ts       # 60Hz 恒帧打字机缓冲池 (requestAnimationFrame)
│   │   │   └── storage.ts             # IndexedDB 本地持久化封装 (localforage)
│   │   ├── api/
│   │   │   ├── sse_client.ts          # 通用 SSE 传输客户端 (心跳保活与重连)
│   │   │   └── http.ts                # Axios 封装 (自动携带 Bearer Token 与 409 拦截)
│   │   ├── composables/
│   │   │   └── useWorkflowAction.ts   # 状态机唤醒/审批通用 hook (处理 CAS 乐观锁)
│   │   └── views/
│   │       ├── WorkbenchLayout.vue    # 双角色通用工作台外壳
│   │       ├── TaskHistoryList.vue    # 提报员历史流组件
│   │       └── TaskApprovalPool.vue   # 审批人待办池组件
│   ├── plugins/                       # 【业务插件目录：实现垂直场景】
│   │   └── power_grid/                # 电力业务插件模块
│   │       ├── cards/
│   │       │   ├── SafetyDiffCard.vue     # 安规核验对比卡
│   │       │   └── PermitResultCard.vue   # 调度令签发防伪二维码卡
│   │       ├── forms/
│   │       │   └── SubstationApplyForm.vue# 变电申报业务表单
│   │       └── index.ts               # 电力插件安装入口 (注册卡片与表单插槽)
│   ├── App.vue                        # 顶层入口
│   └── main.ts                        # 引导脚本：装配通用核心 + 加载业务插件
├── package.json
├── tsconfig.json
├── tailwind.config.js
└── vite.config.ts
```

### 三、 微观操作步骤（逐文件代码与保姆级实施指南）

#### 步骤 1：工程初始化与依赖安装

在项目根目录下初始化前端工程，并安装必要依赖：

Bash

```
# 1. 创建基于 Vite + Vue3 + TS 的前端工程
npm create vite@latest frontend -- --template vue-ts
cd frontend

# 2. 安装核心通用依赖
npm install axios localforage qrcode.vue lucide-vue-next
npm install -D tailwindcss postcss autoprefixer @types/node

# 3. 初始化 TailwindCSS
npx tailwindcss init -p
```

配置 `vite.config.ts` 别名解析：

TypeScript

```
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import path from 'path';

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});
```

#### 步骤 2：通用渲染引擎与存储底层建设

##### 2.1 封装 60Hz 恒帧打字机缓冲池

在 `src/core/engine/render_ticker.ts` 中实现调度逻辑。接收 SSE 字符流时不直接操作响应式变量，而是入队通过 `requestAnimationFrame` 平滑消费：   

TypeScript

```
/**
 * 60Hz 恒帧打字机引擎
 * 利用 requestAnimationFrame 调度字符消费，消除长文本高频重排导致的掉帧
 */
export class RenderTicker {
  private bufferQueue: string[] = [];
  private isTicking: boolean = false;
  private onConsume: (char: string) => void;
  private onComplete?: () => void;
  private charsPerFrame: number = 2; // 默认每帧消费字符基准速率

  constructor(onConsume: (char: string) => void, onComplete?: () => void) {
    this.onConsume = onConsume;
    this.onComplete = onComplete;
  }

  public push(text: string): void {
    for (const char of text) {
      this.bufferQueue.push(char);
    }
    // 动态调节消费速率：积压过多时自动加速消费，防止界面明显滞后
    if (this.bufferQueue.length > 100) {
      this.charsPerFrame = 8;
    } else if (this.bufferQueue.length > 30) {
      this.charsPerFrame = 4;
    } else {
      this.charsPerFrame = 2;
    }

    if (!this.isTicking) {
      this.isTicking = true;
      requestAnimationFrame(this.tick.bind(this));
    }
  }

  private tick(): void {
    if (this.bufferQueue.length === 0) {
      this.isTicking = false;
      if (this.onComplete) {
        this.onComplete();
      }
      return;
    }

    const count = Math.min(this.charsPerFrame, this.bufferQueue.length);
    for (let i = 0; i < count; i++) {
      const char = this.bufferQueue.shift();
      if (char) {
        this.onConsume(char);
      }
    }

    requestAnimationFrame(this.tick.bind(this));
  }

  public flush(): void {
    while (this.bufferQueue.length > 0) {
      const char = this.bufferQueue.shift();
      if (char) this.onConsume(char);
    }
    this.isTicking = false;
    if (this.onComplete) this.onComplete();
  }
}
```

##### 2.2 封装 IndexedDB 离线持久化中枢

在 `src/core/engine/storage.ts` 中封装 `localforage`，并在流式传输结束后提供 1000ms 防抖入库：   

TypeScript

```
import localforage from 'localforage';
import type { ChatMessageRecord } from '../a2ui/types';

localforage.config({
  name: 'EnterpriseAgentChassis',
  storeName: 'chat_session_history',
  description: 'AI Agent 会话与卡片离线快照存储',
});

let debounceTimer: ReturnType<typeof setTimeout> | null = null;

export const StorageEngine = {
  async saveMessages(sessionId: string, messages: ChatMessageRecord[]): Promise<void> {
    await localforage.setItem(`messages_${sessionId}`, JSON.parse(JSON.stringify(messages)));
  },

  async loadMessages(sessionId: string): Promise<ChatMessageRecord[]> {
    const data = await localforage.getItem<ChatMessageRecord[]>(`messages_${sessionId}`);
    return data || [];
  },

  /**
   * 推流结束防抖持久化（1000ms），避免高频 I/O 阻塞主线程[cite: 14]
   */
  debounceSaveMessages(sessionId: string, messages: ChatMessageRecord[], delay = 1000): void {
    if (debounceTimer) {
      clearTimeout(debounceTimer);
    }
    debounceTimer = setTimeout(async () => {
      await this.saveMessages(sessionId, messages);
      debounceTimer = null;
    }, delay);
  },

  async clearSession(sessionId: string): Promise<void> {
    await localforage.removeItem(`messages_${sessionId}`);
  },
};
```

#### 步骤 3：A2UI 核心卡片动态分发系统

##### 3.1 定义通用契约类型

在 `src/core/a2ui/types.ts` 中定义与后端对齐的结构：

TypeScript

```
export interface ChatMessageRecord {
  id: string;
  thread_id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  kind: 'text' | 'card';
  content?: string | null;
  card_type?: string | null;
  card_payload?: Record<string, any> | null;
  retry_payload?: Record<string, any> | null;
  created_at: string;
}

export interface CardActionContext {
  threadId: string;
  lockVersion: number;
  action: string;
  payload?: Record<string, any>;
}
```

##### 3.2 实现单例卡片注册中枢

在 `src/core/a2ui/CardRegistry.ts` 中管理所有卡片组件的映射注册：

TypeScript

```
import { type Component, shallowRef } from 'vue';

class CardRegistry {
  private static registry = new Map<string, Component>();

  /**
   * 业务插件向底座挂载卡片
   */
  public static register(cardType: string, component: Component): void {
    if (this.registry.has(cardType)) {
      console.warn(`[CardRegistry] Warning: card_type '${cardType}' 已被覆盖注册。`);
    }
    this.registry.set(cardType, component);
  }

  public static get(cardType: string): Component | undefined {
    return this.registry.get(cardType);
  }

  public static getAllTypes(): string[] {
    return Array.from(this.registry.keys());
  }
}

export default CardRegistry;
```

##### 3.3 实现通用卡片挂载容器

在 `src/core/a2ui/MessageCardHost.vue` 中完成动态组件渲染与事件路由：

代码段

```
<template>
  <div class="a2ui-card-container my-3 transition-all duration-300">
    <!-- 动态解析渲染对应的业务卡片 -->
    <component
      :is="targetComponent"
      v-if="targetComponent"
      :thread-id="message.thread_id"
      :payload="message.card_payload || {}"
      :retry-payload="message.retry_payload || {}"
      @execute-action="onCardAction"
    />

    <!-- 未注册卡片时的防御兜底展示 -->
    <div v-else class="p-4 border border-dashed border-gray-300 rounded-lg bg-gray-50 text-xs text-gray-500">
      <p class="font-bold">⚠️ 未识别的 A2UI 卡片: {{ message.card_type }}</p>
      <pre class="mt-2 overflow-x-auto">{{ JSON.stringify(message.card_payload, null, 2) }}</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import CardRegistry from './CardRegistry';
import type { ChatMessageRecord, CardActionContext } from './types';

const props = defineProps<{
  message: ChatMessageRecord;
}>();

const emit = defineEmits<{
  (e: 'action', context: CardActionContext): void;
}>();

const targetComponent = computed(() => {
  if (!props.message.card_type) return null;
  return CardRegistry.get(props.message.card_type);
});

function onCardAction(context: CardActionContext) {
  emit('action', context);
}
</script>
```

#### 步骤 4：通用 CAS 乐观锁防抢审 Composable 封装

在 `src/core/composables/useWorkflowAction.ts` 中实现统一的状态机恢复调用及 409 拦截处理：   

TypeScript

```
import { ref } from 'vue';
import axios, { AxiosError } from 'axios';
import type { CardActionContext } from '../a2ui/types';

export function useWorkflowAction() {
  const isSubmitting = ref(false);
  const errorMessage = ref<string | null>(null);

  /**
   * 通用状态机唤醒/审批恢复函数
   */
  async function resumeWorkflow(
    url: string,
    context: CardActionContext,
    onSuccess?: (resData: any) => void,
    onConflict?: () => void
  ) {
    isSubmitting.value = true;
    errorMessage.value = null;

    try {
      const response = await axios.post(url, {
        thread_id: context.threadId,
        lock_version: context.lockVersion, // 核心：传递 CAS 版本号
        is_permitted: context.action === 'APPROVE',
        opinion: context.payload?.opinion || '',
        extra_data: context.payload || {},
      });

      if (response.status === 200) {
        if (onSuccess) onSuccess(response.data);
      }
    } catch (err: any) {
      if (axios.isAxiosError(err)) {
        const axiosError = err as AxiosError<{ detail: string }>;
        if (axiosError.response?.status === 409) {
          // 409 CAS 冲突拦截：给出明确轻提示
          const conflictMsg = axiosError.response.data?.detail || '该工单已被其他操作人处理或版本已失效！';
          alert(`【冲突拦截】${conflictMsg}`);
          if (onConflict) onConflict();
          return;
        }
      }
      errorMessage.value = err.message || '系统通信异常';
      alert(`审批操作失败: ${errorMessage.value}`);
    } finally {
      isSubmitting.value = false;
    }
  }

  return {
    isSubmitting,
    errorMessage,
    resumeWorkflow,
  };
}
```

#### 步骤 5：实现垂直业务插件（电力场景落地）

##### 5.1 实现安规核验对比卡片（`SafetyDiffCard.vue`）

在 `src/plugins/power_grid/cards/SafetyDiffCard.vue` 中编写电力安规卡片，直接消费 `payload`：

代码段

```
<template>
  <div class="bg-white border-2 border-indigo-100 rounded-xl shadow-sm p-5 max-w-xl">
    <div class="flex justify-between items-center pb-3 border-b border-gray-100">
      <span class="text-xs font-semibold px-2.5 py-0.5 rounded bg-indigo-50 text-indigo-700">安规初审意见单</span>
      <span class="text-xs text-gray-400">CAS 版本: v{{ payload.lock_version || 1 }}</span>
    </div>

    <div class="mt-4 space-y-2 text-sm text-gray-700">
      <div><span class="text-gray-400">变电站：</span><span class="font-medium">{{ payload.substation }}</span></div>
      <div><span class="text-gray-400">电压等级：</span><span class="font-medium">{{ payload.voltage_level }}</span></div>
      <div><span class="text-gray-400">检修任务：</span><span class="font-medium">{{ payload.operation_task }}</span></div>
    </div>

    <!-- 违章对比列表 -->
    <div class="mt-4 bg-red-50/60 rounded-lg p-3 border border-red-100" v-if="payload.violations?.length">
      <div class="text-xs font-semibold text-red-700 mb-1 flex items-center gap-1">⚠️ 发现安全隐患项：</div>
      <ul class="list-disc list-inside text-xs text-red-600 space-y-1">
        <li v-for="(item, idx) in payload.violations" :key="idx">{{ item }}</li>
      </ul>
    </div>
    <div class="mt-4 bg-emerald-50 rounded-lg p-3 text-xs text-emerald-700 border border-emerald-100" v-else>
      ✅ 现场安全措施完备，符合规程要求。
    </div>

    <!-- 调度长操作核签栏 -->
    <div class="mt-5 flex gap-3 justify-end">
      <button
        @click="emitAction('REJECT')"
        class="px-4 py-1.5 text-xs font-medium text-gray-600 bg-gray-100 hover:bg-gray-200 rounded-lg"
      >
        驳回申请
      </button>
      <button
        @click="emitAction('APPROVE')"
        class="px-4 py-1.5 text-xs font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg shadow-sm"
      >
        核准许可 (唤醒状态机)
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
const props = defineProps<{
  threadId: string;
  payload: Record<string, any>;
  retryPayload: Record<string, any>;
}>();

const emit = defineEmits(['execute-action']);

function emitAction(type: 'APPROVE' | 'REJECT') {
  emit('execute-action', {
    threadId: props.threadId,
    lockVersion: props.payload.lock_version || 1, // 传入当前版本进行 CAS 校验[cite: 13, 14]
    action: type,
    payload: {
      opinion: type === 'APPROVE' ? '现场安措完备，准予许可' : '安措不全，请补充后重报',
    },
  });
}
</script>
```

##### 5.2 实现防伪二维码调度令卡片（`PermitResultCard.vue`）

在 `src/plugins/power_grid/cards/PermitResultCard.vue` 中利用 `qrcode.vue` 渲染数字签名[cite: 14]：

代码段

```
<template>
  <div class="bg-gradient-to-br from-emerald-500 to-teal-700 text-white rounded-xl p-5 shadow-lg max-w-md">
    <div class="flex justify-between items-start">
      <div>
        <p class="text-xs uppercase tracking-wider opacity-80">Official Permit</p>
        <h3 class="text-lg font-bold mt-0.5 tracking-tight">正式电力调度令凭单</h3>
      </div>
      <div class="bg-white/20 px-2 py-0.5 rounded text-[10px] font-mono">DISPATCH APPROVED</div>
    </div>

    <div class="my-4 bg-white/10 rounded-lg p-3 space-y-1 font-mono text-xs">
      <div>凭单单号: <span class="font-bold select-all">{{ payload.permit_no }}</span></div>
      <div>状态标识: <span>{{ payload.status || 'PERMITTED' }}</span></div>
      <div class="truncate text-[10px] opacity-75">防伪摘要: {{ payload.ca_signature_hash }}</div>
    </div>

    <!-- 现场移动 PDA 扫码验真二维码 -->
    <div class="bg-white p-3 rounded-lg flex items-center justify-between gap-4">
      <div class="text-gray-800 text-xs leading-relaxed">
        <p class="font-semibold text-gray-900">SCADA 数字证书验真</p>
        <p class="text-[11px] text-gray-500 mt-0.5">支持现场巡检终端扫码校验 CA 签名防伪链</p>
      </div>
      <qrcode-vue
        :value="qrCodeData"
        :size="80"
        level="M"
        class="border border-gray-200 rounded p-1"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import QrcodeVue from 'qrcode.vue';

const props = defineProps<{
  threadId: string;
  payload: Record<string, any>;
}>();

// 二维码载荷：拼接防伪摘要供移动端校验
const qrCodeData = computed(() => {
  return JSON.stringify({
    permit_no: props.payload.permit_no,
    hash: props.payload.ca_signature_hash,
    thread_id: props.threadId,
    timestamp: new Date().toISOString(),
  });
});
</script>
```

##### 5.3 插件安装与注册入口

在 `src/plugins/power_grid/index.ts` 中向通用底座统一注册卡片：

TypeScript

```
import CardRegistry from '@/core/a2ui/CardRegistry';
import SafetyDiffCard from './cards/SafetyDiffCard.vue';
import PermitResultCard from './cards/PermitResultCard.vue';

/**
 * 电力业务插件装配入口
 */
export function setupPowerGridPlugin(): void {
  // 注册电力场景专属卡片
  CardRegistry.register('dispatcher_audit', SafetyDiffCard);
  CardRegistry.register('permit_result', PermitResultCard);

  console.info('[PowerGridPlugin] 电力业务插件安装成功，A2UI 卡片组件注册完成。');
}
```

#### 步骤 6：入口装配与测试验证（`main.ts`）

在 `src/main.ts` 中引入通用核心并安装业务插件：

TypeScript

```
import { createApp } from 'vue';
import App from './App.vue';
import './style.css'; // 导入 TailwindCSS

// 1. 导入业务插件入口
import { setupPowerGridPlugin } from './plugins/power_grid';

const app = createApp(App);

// 2. 装配业务插件（实现依赖注入）
setupPowerGridPlugin();

app.mount('#app');
```

### 四、 端到端联调与闭环验证核对清单

完成上述代码编写后，按照以下清单在本地进行端到端联调验证：

| **验证项**              | **测试操作**                                                 | **预期通过标准**                                             |
| ----------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| **1. 插件动态卡片加载** | 前端接收到一条 `card_type: 'dispatcher_audit'` 的消息        | 界面动态渲染出 `SafetyDiffCard.vue`，正确显示变电站名称与红绿安规隐患标签，无控制台报错[cite: 13, 14]。 |
| **2. 60Hz 恒帧推流**    | 通过通用 SSE 客户端接收 2000 字以上的流式文本[cite: 14]      | 文本通过 `RenderTicker` 平滑逐字显示，长文本传输期间无丢帧和 DOM 顿挫[cite: 14]。 |
| **3. 本地离线持久化**   | 流式接收完毕后刷新浏览器页面                                 | `localforage` 从 IndexedDB 快速反序列化历史气泡与卡片[cite: 14]，实现会话离线秒开。 |
| **4. CAS 并发冲突拦截** | 调度长 A 和调度长 B 同时打开待办卡片，A 点击核准后，B 再点击核准 | 调度长 A 的卡片流转成功，调度长 B 的界面弹出轻提示“该工单已被其他操作人处理或版本已失效”[cite: 13]，底座拦截 409 且无界面崩溃。 |
| **5. 数字防伪核验**     | 审批通过后生成 `permit_result` 卡片                          | 界面呈现包含绿色调度令与二维码的凭证[cite: 14]，使用手机或扫码器扫描二维码可正确解析出 `ca_signature_hash`[cite: 14]。 |