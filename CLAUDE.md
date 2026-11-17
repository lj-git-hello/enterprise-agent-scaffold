# 项目级工程规范：企业级 AI Agent 通用底座与电网业务插件 (power-dispatch-agent)

## 一、 架构全景与物理边界（Core + Plugin 双层隔离）
- **核心设计哲学**：**通用底座（`app/core/`）只认契约与状态，零硬编码业务细节；新增垂直业务 = 新增独立插件目录（含 `plugin.py` 暴露 `PLUGIN` 清单），底座零改动。**
- **工程目录拓扑**：
  - **前端 (`frontend/`)**：Vue 3 + TypeScript + Vite + Pinia + `<script setup lang="ts">`，三栏响应式工作台（对话流 / A2UI 动态卡片渲染 / 角色审批待办池），本地 `localforage` 离线兜底快照（服务端为权威源）。
  - **通用底座 (`backend/app/core/`)**：
    - `workflow/`：全局图注册中心（`GraphRegistry`）、HITL 审批控制器（`HITLController`）、消息快照落库器（`MessageRecorder`）、工单映射存储（`ThreadMappingStore`）。
    - `plugin/`：插件契约定义（`PluginManifest`、`PluginIdentity`）与插件自动发现加载器（`loader.py`）。
    - `database.py` / `llm.py` / `security.py`：PostgreSQL 16 双连接池物理隔离、统一 LLM / Embedding 适配、JWT 签发与外部 IAM 验签边界。
  - **垂直业务插件 (`backend/app/plugins/power_grid/`)**：
    - `plugin.py`：唯一公开出口（`__all__ = ["PLUGIN"]`），声明路由前缀、角色常量、演示身份及状态机注册。
    - `graph.py` / `states.py` / `router.py` / `chat.py` / `schemas.py` / `constants.py`：电网检修调度状态机、安规 RAG 对话处理器、接口 DTO 模型。
    - `adapters/`：传统业务防腐层（Anti-Corruption Layer），通过 PEP 544 `Protocol` 隔离外部 OMS 工单系统与 SCADA 调度许可系统。
  - **存储底座**：PostgreSQL 16 + `psycopg` 异步驱动 + `pgvector` (HNSW 索引) + Alembic 异步迁移。核心业务表：`ai_thread_mappings`（含 `lock_version` 乐观锁）、`ai_chat_messages`（JSONB 卡片快照）、`ai_execution_traces`（全链路审计留痕）、`ai_knowledge_base`（`VECTOR(1024)` + `is_active` 软废止标记）。

---

## 二、 电力业务红线与状态机编排规约（Plugin 层规范）
- **双轨入口解耦**：
  - **自然语言对话入口 (`chat.py`)**：处理日常安规问答与意图识别；涉及《电力安全工作规程》时优先检索 `ai_knowledge_base`（仅限 `is_active=TRUE` 的有效切片），严格依据条款作答并标注出处，严禁无依据脑补。
  - **结构化业务提报入口 (`router.py` -> `POST /apply`)**：派生独立 `thread_id`（`{session_id}_{uuid12}`），写入 `ai_thread_mappings` 建档后驱动 `build_dispatch_graph` 执行。
- **状态机节点与人机协同（HITL）规则**：
  - **节点内动态挂起**：人工审批断点**必须在节点函数内部显式调用 `interrupt()`** 表达，**严禁使用静态 `interrupt_before`**。
  - **防滞留回滚兜底**：`POST /apply` 驱动图执行后必须探查 `bool(snapshot.interrupts)`；若安规初审未通过导致图直接流转至 `END`（未挂起），必须立即调用 `HITLController.finalize_status` 置为终态驳回并执行 `ThreadMappingStore.archive()`，严禁废单滞留在调度长待办池。
  - **并发防重与唤醒**：调度长核签（`AuditRequest`）必须校验 `lock_version`（CAS 乐观锁），通过 `Command(resume=...)` 携带相同 `thread_id` 唤醒状态机流转至 `issue_permit`（签发 SCADA 许可令）或 `END`。
- **外部系统防腐隔离**：插件内部严禁直接耦合外部电网数据库或第三方私有 SDK，所有外部交互（IAM 鉴权、OMS 建单、SCADA 许可）必须通过 `adapters/` 下的 `Protocol` 接口调用。

---

## 三、 安全底线、生产边界与高危操作拦截
- **生产与演示环境严格隔离**：`PluginIdentity` 演示角色切换器仅限 `dev` / `demo` 环境启用；生产环境（`prod`）必须禁用免密切换接口，强制由外部统一身份认证中心（IAM）接管鉴权。
- **严禁硬编码敏感信息**：绝对禁止在代码中硬编码 API Key、数据库连接串、JWT Secret 或模型参数。大模型与向量服务配置统一从 `backend/.env` 读取（如 `EMBEDDING_MODEL`、`EMBEDDING_SUPPORTS_DIMENSIONS`）。
- **向量空间一致性铁律**：切换 Embedding 模型后，严禁将新旧向量混用检索；必须通过 `is_active=FALSE` 软废止旧向量记录，再重新切块灌库。
- **Git 忽略与资产保护**：确保 `.env`、`node_modules/`、`__pycache__/`、临时测试脚本写入 `.gitignore`。严禁未经二次确认执行 `rm -rf`、数据库物理 `DELETE` / `DROP` 或删除用户手写业务文件。

---

## 四、 后端开发规范 (Python 3.11+ & FastAPI & LangGraph)
- **全链路纯异步优先 (Async Only)**：
  - 所有数据库操作（`AsyncSession`）、状态机交互（`ainvoke`、`astream_events(version="v2")`、`aget_state`）及外部 HTTP/LLM 请求必须全链路异步，严禁在异步事件循环中调用阻塞式同步方法。
- **契约与不可变边界**：
  - 跨层传递的配置、清单与凭证优先使用 `@dataclass(frozen=True)` 保证只读不可变；模块对外暴露统一使用 `__all__` 显式声明白名单。
- **大模型调用与降级兜底**：
  - 合规审查与结构化输出节点强制低温（`temperature=0.0` 或 `0.1`），统一包裹 `try-except` 超时与异常捕获；LLM 调用失败时触发安全兜底策略，严禁静默写入伪造向量或未捕获异常崩溃。

---

## 五、 前端开发规范 (Vue 3 + TypeScript)
- **流式输出与协议规范 (Streaming First)**：
  - 面向用户的打字机输出统一采用 **SSE（HTTP POST）** 协议，默认使用成熟事件流驱动库（如 `@microsoft/fetch-event-source`）处理事件分帧、TCP Buffer 拼接与断线重连；若使用原生 `fetch` + `ReadableStream`，必须显式编写严谨的 `buffer` 边界切分逻辑。
- **A2UI 动态卡片与类型安全**：
  - 全局开启严格 TypeScript，禁用 `any`；组件 Props、接口入参/响应载荷及 A2UI 卡片结构（`card_payload`）均需定义严谨 `interface` / `type`。
  - 统一采用 `<script setup lang="ts">` 语法，模块引用使用路径别名（`@/...`），状态与角色枚举统一抽取为常量，禁止魔法值。

---

## 六、 Agent 执行反馈与常用命令 (Windows PowerShell)
- **执行透明化规范（防黑盒干等）**：
  - 调用 Bash、Read 或 Grep 工具前，先用 1 句话简短说明“当前准备检查/执行什么”；单次最多并发读取 3 个核心文件，严禁扫描 `node_modules`、`.venv`、`dist` 等无关目录。
- **环境与启动命令**：
  - 环境要求：Conda 虚拟环境 `(power-agent)`，Node.js 18+，PostgreSQL 16 服务运行中。
  ```powershell
  # 1. 启动后端服务（位于 backend/ 目录下）
  conda activate power-agent
  uvicorn app.main:app --reload --port 8000

  # 2. 数据库迁移（位于 backend/ 目录下）
  alembic upgrade head