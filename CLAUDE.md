# 项目级工程规范：企业级 AI Agent 通用底座与电网业务插件 (power-dispatch-agent)

> 本文件仅记录本仓库特有的目录拓扑、接口契约、业务红线与本地运行门禁；通用编码、并发安全与提速纪律遵循用户级 `~/.claude/CLAUDE.md`。

## 1. 仓库拓扑与物理隔离边界（Core + Plugin 双层隔离）
- **核心设计哲学**：`backend/app/core/` 与 `frontend/src/core/` 零硬编码任何垂直业务词汇（严禁出现 `DISPATCHER`、`APPLICANT`、`检修`、`调度令`、`kV` 等）；新增业务 = 新增独立插件目录，底座零改动。
- **前端架构 (`frontend/`)**：
  - Vue 3 + TypeScript + Vite + `<script setup lang="ts">`，**无 `vue-router`、无 `pinia`**，全局状态为 `src/core/workbench/useWorkbenchStore.ts` 的模块级 `reactive()` 单例，离线兜底走 `localforage`。
  - **三栏工作台插槽契约 (`WorkbenchLayout.vue`)**：
    - 左栏：按 `session_id` 聚合的会话列表（调用 `GET /api/v1/sessions`）。
    - 中栏：对话流与 `MessageCardHost.vue` 动态卡片宿主（点击卡片触发 `inspect` 事件）。
    - 右栏：顶部插件只读详情检视区（通过 `PluginDefinition.inspectors` 注册）+ 视觉分割线 + 底部 `#business-form` 业务表单插槽（`v-show` 保活防草稿丢失，标题由 `PluginDefinition.formSectionTitle` 注入）。
- **通用底座 (`backend/app/core/`)**：
  - `workflow/`：`GraphRegistry`、`HITLController`、`MessageRecorder`、`ThreadMappingStore`（含 `list_sessions` 会话聚合查询）、`TraceRecorder`、`WORKFLOW_EVENT_BUS`（事件载荷含 `session_id`）。
  - `policy/`：通用风险定级契约（`RiskClassification` / `RiskPolicyProtocol`），**不定义任何业务阈值**。
  - `plugin/`：`PluginManifest`、`PluginIdentity` 与自动发现加载器。
  - `api/v1/`：`threads.py`（`GET /sessions` 会话分页聚合、`GET /threads` 线程列表、`GET /threads/{thread_id}` 只读详情）、`workflow.py`（通用 SSE 事件端点 `workflow.status_changed`）。
- **电网业务插件 (`backend/app/plugins/power_grid/` & `frontend/src/plugins/power_grid/`)**：
  - 唯一后端出口 `plugin.py`（`__all__ = ["PLUGIN"]`）；阈值常量 `POWER_HUMAN_REVIEW_KV_MIN = 110` 属插件私有；外部 OMS / SCADA 交互封装于 `adapters/`（PEP 544 `Protocol`）。
  - 前端插件 (`plugin.ts`) 注册卡片映射（`cards`）、只读检视面板（`inspectors`）与表单分区标题（`formSectionTitle`）。
- **存储底座**：PostgreSQL 16 + `psycopg` 异步驱动 + `pgvector` + Alembic。核心表：`ai_thread_mappings`（含 `lock_version`）、`ai_chat_messages`、`ai_execution_traces`、`ai_knowledge_base`（含 `is_active` 软废止）。

## 2. 电力业务红线与状态机编排规约
- **电压等级定级红线（二值判定，无灰区档）**：
  - `>= 110kV` → 定级 `HIGH` → **强制人工核签**，进入调度长待办池。
  - `< 110kV` → 定级 `LOW` → **自动签发调度许可令**，当场生成许可凭证卡。
  - 无法识别 / 越界（空串、非数值、`0 < kv > 1000` 之外）→ 定级 `UNKNOWN` → **转人工**（fail-closed 保守兜底）。
  - 定级节点 `risk_classify` **必须无副作用**（状态机 `resume` 时会重放节点）。
- **双轨入口与会话派生**：
  - 自然语言对话（`chat.py`）：固定线程 `chat_{session_id}`，安规问答仅检索 `is_active=TRUE` 切片并标注出处。
  - 业务提报（`POST /apply`）：派生独立线程 `{session_id}_{uuid12}`；图执行后必须检查 `bool(snapshot.interrupts)`，若未挂起直接结束（安规初审驳回），必须立即置终态驳回并调用 `ThreadMappingStore.archive()`，严禁废单滞留待办池。
- **HITL 状态机护栏**：
  - 人工断点必须在节点内显式调用 `interrupt()`，**严禁使用静态 `interrupt_before`**。
  - 终态守卫：仅对在途状态（`DRAFT / ACTIVE / IN_PROGRESS / RUNNING / WAITING_HUMAN`）生效；图恢复异常时，若已产出终态产物则保持终态仅补记留痕，未产出则置失败终态。
  - 审核卡为可变视图投影，原地改写 `card_payload` 并递增 `card_version`，旧载荷写入 `ai_execution_traces`。
- **环境隔离**：`PluginIdentity` 演示免密切换仅限 `dev` / `demo` 环境，生产环境（`prod`）强制由外部 IAM 接管。

## 3. 本仓库常用命令与验证门禁（Windows PowerShell）
- **启动后端（必须带 `--reload`，否则 Windows 下 `psycopg` 报 `ProactorEventLoop` 错误）**：
  ```powershell
  cd backend
  conda activate power-agent
  python -m uvicorn main:app --reload --port 8000
  ```
- **数据库迁移（位于 `backend/` 目录）**：
  ```powershell
  alembic upgrade head
  ```
- **静态隔离与类型门禁（修改对应端代码后必跑）**：
  ```powershell
  # 后端核心/插件零业务词隔离门禁（必须 exit 0）
  cd backend && python scripts/check_plugin_isolation.py

  # 前端强类型检查（必须 exit 0）
  cd frontend && npx vue-tsc -b --force
  ```
- **全量回归（仅当用户明确要求「准备提交 / 全量回归」时运行）**：
  ```powershell
  cd backend && pytest -q
  cd frontend && npm run build
  ```