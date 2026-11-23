# enterprise-agent-scaffold

> **一句话定位**：**企业级通用 AI Agent 协同工作台与执行中枢脚手架**（FastAPI + LangGraph + Vue 3）—— Core 层与 Plugin 层物理隔离，支持双连接池隔离、CAS 乐观锁、人机协同（HITL）、流式 RAG 与 Core+Plugin 插件化业务扩展；接入新业务只需新增一个插件目录，底座零改动。仓库内置 `power_grid` 电网检修调度插件作为完整落地样例。

## 特性速览

- **Core + Plugin 依赖反转**：底座只认 `PluginManifest` 契约，不认业务语义；前后端插件自动发现装载。
- **HITL + CAS 乐观锁**：人工审批挂起/恢复以数据库 CAS 抢占为准，杜绝重复核签与迟到请求覆写终态。
- **「3+N」三轨分离 + 双连接池**：调度中枢 / 交互载荷 / 运行审计三轨解耦，LangGraph 检查点独立连接池规避死锁。
- **工业级流式 RAG**：惰性解析 + 惰性分块 + 幂等入库 + HNSW 余弦检索，全链路零全量物化。
- **三栏 A2UI 工作台**：左会话 / 中对话卡片 / 右检视表单，插槽式插件装载，离线兜底不丢草稿。
- **可执行的隔离门禁**：正则黑名单静态扫描，底座出现一个业务词即门禁失败。

---

## 1. 项目简介与核心定位

### 1.1 Core + Plugin 双层物理隔离

`enterprise-agent-scaffold` 是一个**企业级通用 AI Agent 协同工作台与执行中枢脚手架**（FastAPI 应用标题即 `企业级 AI Agent 通用脚手架`）。仓库刻意采用「双层物理隔离」结构，把「可复用的 Agent 中台能力」与「具体的垂直业务」彻底分开：

```
backend/app/core/           ← 通用底座：零业务词汇
backend/app/plugins/<biz>/  ← 业务插件：唯一允许出现业务语义的地方

frontend/src/core/          ← 通用底座：零业务词汇
frontend/src/plugins/<biz>/ ← 业务插件：唯一允许出现业务语义的地方
```

隔离不是靠口头约定，而是靠**门禁脚本强制执行**的（见 [第 6 节](#6-质量与隔离门禁)）。这带来一个直接的工程收益：**换业务 = 新增一个插件目录，底座一行不改**。

仓库内置唯一一个业务插件 `power_grid`（电网检修调度），作为这套架构的完整落地样例：它演示了状态图编排、人工核签断点、风险定级策略、防腐层适配器、A2UI 卡片与检视面板、业务表单插槽的全链路形态。**它是一个示例，不属于底座能力。**

### 1.2 关键架构决策与取舍

| 决策 | 取舍说明 |
| --- | --- |
| **插件清单自描述**<br>`PluginManifest`（frozen dataclass） | 插件自报 `name / version / router / router_prefix / graph_factory / graph_name / chat_handler / endpoint_paths / identities`。底座在装配期做契约强校验：`router_prefix` 必须以 `/api` 开头；声明了 `graph_factory` 就必须给 `graph_name`。**违规即启动失败**，而不是运行期才炸。 |
| **多插件必须显式指定** | 发现到多个插件却未设 `ACTIVE_PLUGIN` 时，启动即抛 `PluginContractError` 并列出候选。宁可启动失败，也绝不隐式猜测当前业务。 |
| **端点以服务端为唯一真相源** | 前端 `PluginDefinition.endpoints` 只是本地默认值；启动时拉 `GET /api/v1/plugin/manifest`，按插件 `name` 匹配后覆盖。版本不一致时 `console.warn` + 顶部提示条。 |
| **状态图按名注册**<br>`GraphRegistry` | 类级 `_graphs` 字典，暴露 `register_graph / get_graph / clear`。业务图注册名**不占用 `DEFAULT` 槽位** —— 业务图漏传 `graph_name` 应当快速失败，而不是静默唤醒另一个业务的图。 |
| **接口用 PEP 544 Protocol 固化** | `RiskPolicyProtocol`、`IAMAdapterProtocol` / `OrderAdapterProtocol` / `CredentialAdapterProtocol`、`TraceSink` / `ChatHandler` 全部标注 `@runtime_checkable`。实现方无需显式继承，签名一致即视为实现；外部系统接入时可整体替换实现而不动调用方。 |
| **前端插件编译期自动装载** | 自动装载写在 **`frontend/src/main.ts`**：`import.meta.glob('./plugins/*/plugin.ts', { eager: true })`。注意**不在** `vite.config.ts` 里 —— 后者只有 `@` → `./src` 别名与 `/api` → `http://127.0.0.1:8000` 代理。 |

---

## 2. 核心架构特性

### 2.1 Core + Plugin 依赖反转

**后端装载链路**：`PluginLoader.discover()` 用 `pkgutil.iter_modules` 扫描 `app/plugins/*/plugin.py`，读取每个插件导出的 `PLUGIN` 清单。子包没有 `plugin.py` 视为非插件子包跳过；插件模块导入抛异常则 fail-fast 上抛，绝不静默跳过。

**防腐层 Protocol 一览**（全部位于底座，插件只负责实现）：

| Protocol | 定义位置 | 用途 |
| --- | --- | --- |
| `RiskPolicyProtocol` | `backend/app/core/policy/protocols.py` | 风险定级契约（只定义 `RiskLevel` 与 `RiskClassification`，**不定义任何业务阈值**） |
| `IAMAdapterProtocol` | `backend/app/services/acl/protocols.py` | 外部身份 / IAM 对接 |
| `OrderAdapterProtocol` | `backend/app/services/acl/protocols.py` | 业务单据 / 工单系统对接 |
| `CredentialAdapterProtocol` | `backend/app/services/acl/protocols.py` | 凭据与许可凭证下发 |
| `TraceSink` / `ChatHandler` | `backend/app/core/plugin/manifest.py` | 留痕落库 / 对话处理扩展点 |

**前端 `PluginDefinition`**（定义于 `frontend/src/core/plugins/registry.ts`）：

| 字段 | 说明 |
| --- | --- |
| `name` / `version` | 插件唯一名与语义化版本，须与后端 manifest 比对 |
| `endpoints` | 工作流端点（相对 `/api`），启动时被服务端 manifest 覆盖 |
| `cards?` | `card_type` → **可交互**卡片组件 |
| `inspectors?` | `card_type` → **只读**详情检视面板（无审批 / 提交动作） |
| `cardSanitizers?` | 卡片脱敏器，**仅作用于本地离线快照** |
| `formComponent?` / `formSectionTitle?` | 右栏业务表单组件与分区标题 |
| `sessionCountLabel?` | 左栏关联计数单位文案 |
| `identities?` | 演示身份清单（`id` / `label` / `role`） |

其中 `cards` 与 `inspectors` 是**两套同构但语义不同**的映射：前者承载用户动作，后者只呈现服务端权威数据。

### 2.2 人机协同（HITL）与 CAS 乐观锁

人工审批链路的三个关键设计：

**1）显式中断，而非静态断点。** `interrupt()` 在 `backend/app/plugins/power_grid/graph.py` 的 `human_audit_node` 节点内显式调用；**不使用静态 `interrupt_before`**。原因：静态断点无法表达「本节点是否需要人工」的业务判断，也无法携带审批所需上下文。

**2）恢复走 CAS 抢占。** `HITLController.resume_with_cas` 先执行乐观锁更新，再唤醒图：

```sql
UPDATE ai_thread_mappings
   SET status = :running, lock_version = lock_version + 1,
       operator_id = :uid, audit_opinion = :opinion, last_activity_at = now()
 WHERE thread_id = :tid AND lock_version = :ver AND status = :waiting
```

`rowcount == 0` → 抛 `HTTPException(409, "任务已被其他操作人处理或版本已失效")`。抢占成功后才执行：

```python
await graph.ainvoke(
    Command(resume={"is_permitted": ..., "opinion": ..., "reviewer_id": ...}),
    config={"configurable": {"thread_id": thread_id}},
)
```

这保证「同一工单不会被两个审批人同时核签」。

**3）终态守卫。** `HITLController.finalize_status` 的 WHERE 条件含 `status = ANY(:in_flight)`，在途集合为 `DRAFT / ACTIVE / IN_PROGRESS / RUNNING / WAITING_HUMAN`。已被他人终结的线程不会被迟到请求覆盖 —— 首次终态写入生效。

**跨端实时同步**：`WORKFLOW_EVENT_BUS` 是进程内发布订阅，事件名 `workflow.status_changed`，心跳 15 秒；前端通过 `GET /api/v1/workflow/events`（SSE）接收。前端收到自己触发的变更（`actor_id` 相同）会跳过，避免自我回环。

**两层失败兜底**：图恢复失败时，底座写 `workflow.resume_failed` trace 并上抛；插件侧判断是否已产出终态产物 —— 已产出则保持终态仅补记 trace，未产出则置失败终态。**底座无权代业务决定**，这是「留痕 + 上抛」而非「擅自改写状态」的原因。

**卡片级第二层乐观锁**：`ai_chat_messages.card_version`。卡片终态是原地投影（`MessageRecorder.update_card_payload` 改写 `card_payload` 并递增 `card_version`，旧载荷写入 `ai_execution_traces`）；版本不匹配时只记 warning，不抛 409。

### 2.3 三轨分离与双连接池

**「3+N」资产表**（`backend/app/models/ai_schema.py`，**恰好 4 张 `ai_` 表**）：

| 表 | 职责 |
| --- | --- |
| `ai_thread_mappings` | 调度中枢：线程 ↔ 会话映射、状态指针、CAS `lock_version`、`title`、`last_activity_at` |
| `ai_chat_messages` | 交互载荷与卡片快照：`card_version`、`seq`、`client_msg_id` 幂等键 |
| `ai_execution_traces` | append-only 运行审计：节点级 Token / 成本 / 时延 / 工具调用 |
| `ai_knowledge_base` | 向量知识：`Vector(1024)` + `is_active` 软废止 |

**双连接池**（`backend/app/core/database.py`）：

| 用途 | 变量 / 实现 | 连接串与参数 |
| --- | --- | --- |
| 业务 CRUD + 向量检索 | SQLAlchemy `async_engine` | `postgresql+asyncpg://`；`pool_size=10`、`max_overflow=20`、`pool_recycle=3600`、`pool_pre_ping=True` |
| 状态机检查点 | `psycopg_pool.AsyncConnectionPool` → `AsyncPostgresSaver` | 原生 `postgresql://`；`min_size=2`、`max_size=10`、`autocommit=True` |

**为什么要分池**：LangGraph 检查点写入与业务大事务 / 向量索引查询共用一个池时，长事务持锁会与 ANN 建索引、向量写入互相等待，形成死锁。拆池是成本最低、侵入最小的隔离手段。

**生命周期**：`lifespan` 启动时 `init_pool()`（`saver.setup()` 幂等建 `checkpoints` / `checkpoint_blobs` / `checkpoint_writes`）；关闭时**先关检查点池**再 `async_engine.dispose()`。

**迁移边界**：Alembic **明确不纳管 LangGraph 原生表** —— `migrations/env.py` 的 `LANGGRAPH_ENGINE_TABLES` 配合 `include_object` 返回 `False`，避免 `alembic upgrade head` 误改引擎内部表。

### 2.4 工业级流式 RAG

| 环节 | 设计 |
| --- | --- |
| **解析** | 解析器注册表（`core/rag/parsers/factory.py`）：txt/md → `TextParser`、docx → `DocxParser`、pdf → `PdfParser`、xlsx/csv → `ExcelParser`。主契约 `BaseParser.iter_parse` 是**惰性生成器**；`DocumentParserFactory.iter_file` 为大文件场景推荐入口。 |
| **分块** | `RecursiveSemanticChunker`，`SEMANTIC_SEPARATORS` 按层级标点（`\n\n` → `\n` → `；` → `。` → 英文句读）递归退化，全部分隔符失效才硬切。`chunk_size=500` / `chunk_overlap=50`。对外主契约 `iter_split_sections` 是惰性生成器，`split_sections` 才是物化版本。 |
| **指纹** | `DocumentChunk.content_hash` = `md5(f"{doc_code}:{content.strip()}")`，由 pydantic `model_validator` 自动填充。 |
| **嵌入** | `EmbeddingService`，`EMBEDDING_DIM=1024`、`EMBEDDING_BATCH_SIZE=8`，走 OpenAI 兼容协议；网络异常 / 未配 Key 时降级为确定性伪向量 `_pseudo_embedding`，保证离线链路可跑。 |
| **幂等入库** | `VectorStoreService.batch_upsert_chunks` 按 `(doc_code, content)` 且 **`is_active = TRUE`** 去重 —— 软废止行只作历史留痕，不阻止同内容新版本重灌；`soft_delete_document` 执行软废止。 |
| **检索** | `VectorStoreService.search_advanced`，`ORDER BY embedding <=> CAST(:vec AS vector) LIMIT :top_k`，相似度换算 `1 - (embedding <=> CAST(:vec AS vector))`，`DEFAULT_TOP_K=3`、`DEFAULT_MIN_SIMILARITY=0.5`；元数据预过滤 + 阈值过滤双重收口。 |
| **索引** | `idx_knowledge_embedding_hnsw`（`m=16`、`ef_construction=64`、`vector_cosine_ops`）+ 复合 B-Tree `idx_knowledge_tag_active(biz_tag, is_active)`。 |

**四个必须知道的工程约束**（都是踩过的坑）：

1. **分块预算是「字符」不是「token」**。实现全程使用 `len(text)`，不经过 tokenizer。
2. **`content_hash` 只存在于内存 Pydantic 模型，库里没有这一列**。入库幂等实际靠 `(doc_code, content)` 正文比对，语义等价但实现路径不同。
3. **`EMBEDDING_BATCH_SIZE=8` 是硬约束**。批量 ≥16 时部分供应商的 embeddings 会落入不同表示空间（与小批量余弦≈0、完全正交），导致检索全失效 —— 未重新验证前不要调大。
4. **`EMBEDDING_SUPPORTS_DIMENSIONS` 保持关闭**。向不支持 Matryoshka 降维的模型下发 `dimensions` 参数会直接返回 HTTP 400。

当前实配为硅基流动 `BAAI/bge-m3`（1024 维原生），全部通过 `EMBEDDING_*` 环境变量可替换。

### 2.5 三栏沉浸式 A2UI 工作台

`frontend/src/core/views/WorkbenchLayout.vue` 的布局：

| 栏 | 结构 | 内容 |
| --- | --- | --- |
| **左栏** `w-72` | `border-r` | 新建会话、我的会话 / 待办审批池 Tab、标题搜索、会话列表、显示更多、底部本地缓存运维区 |
| **中栏** `flex-1` | 消息流 | `session_id` 栏、离线兜底提示条、`TaskHistoryList` / `TaskApprovalPool` 按 Tab 切换、通用输入框 |
| **右栏** `relative flex-col border-l` | 三分区 | 顶部**只读详情检视区**、中部**业务表单插槽** `<slot name="business-form" :session-id="sessionId">`、底部运行追踪（latency / token 占位） |

右栏宽度 `rightOpen ? (inspectionOpen ? 'w-96' : 'w-80') : 'w-10'`；检视区与表单区之间有 `border-b border-gray-200` 视觉分割线。

**折叠不销毁组件**：右栏内容根节点用 `v-show="rightOpen"` 而非 `v-if` —— 用户填了一半的表单草稿不会因折叠或查看详情而丢失。

**会话聚合在服务端**：左栏按 `session_id` 聚合的会话列表来自 `GET /api/v1/sessions`（后端 `api/v1/threads.py` 的 `list_sessions`），返回 `SessionSummary[]`（`session_id` / `title` / `status` / `related_count` / `thread_count` / `last_activity_at` 等）。前端只做去重与渲染，不做权威聚合。

**动态卡片**：`core/a2ui/MessageCardHost.vue` 按 `message.card_type` 从 `CardRegistry` 动态解析 `<component :is>`；未注册类型渲染 JSON 兜底。点击卡片触发 `inspect`（忽略 button / a / input 等交互元素上的点击，避免审批动作被误读为查看详情）。`core/a2ui/types.ts` 中 **card_type 是宽松 `string`，没有枚举** —— core 层零业务词汇；业务卡片类型清单在插件与后端 constants。

**状态管理**：`core/workbench/useWorkbenchStore.ts` 的**模块级 `reactive()` 单例**（`const state = reactive<WorkbenchState>({...})`），导出 `useWorkbenchStore()`；字段含 `activeSessionId` / `activeMessages` / `conversationList` / `rightDrawerOpen` / `workflowEventTick` / `inspectedThreadId` / `inspectedCardType` / `pendingReplyId` / `activeOwnerId` 等；每个集合都带来源标记 `DataSource = 'server' | 'cache' | 'live' | 'none'`，界面如实告知用户当前看到的是权威数据还是离线兜底。

**离线兜底**：`core/engine/storage.ts` 用 localforage（实例 `agent-scaffold`）。读侧 `loadConversations(userId)` / `loadMessages(userId, sessionId)`，写侧 `saveConversations` / `saveMessages` + 1000ms 防抖版本；`SCHEMA_VERSION=3`、`SNAPSHOT_TTL_MS=7 天`、`MAX_SESSIONS_PER_USER=30`、`MAX_MESSAGES_PER_SESSION=300`；敏感键正则清洗 `/(signat|signature|token|secret|passwo?rd|credential|api[_-]?key|private[_-]?key|nonce|salt|certificate)/i`。业务特有的敏感字段判定规则归插件脱敏器（`cardSanitizers`），core 只负责调用。

**SSE 对话流**：`core/api/agent.ts` 用 **`@microsoft/fetch-event-source`** 的 `fetchEventSource`，POST `POST /api/v1/agent/chat-stream`，事件 type 为 `token | ui_card | error | done`；`RECONNECT_INTERVAL_MS=2000`、`MAX_RECONNECT_ATTEMPTS=3`；**连接一旦建立（`opened=true`）不再自动重放**，避免重复消费 LLM 与重复落库；`StreamStatus = 'idle' | 'connected' | 'reconnecting' | 'offline'`。

**409 冲突的前端处理**：`core/composables/useWorkflowAction.ts` 捕获 `status === 409` → 琥珀色轻提示（非阻断弹窗）+ 重拉待办池与详情。

---

## 3. 技术栈

### 后端（Python 3.11）

| 类别 | 依赖 |
| --- | --- |
| Web 框架 | `fastapi==0.162.0`、`uvicorn==0.54.0`、`httpx`、`tenacity`、`python-dotenv` |
| ORM / 迁移 | `SQLAlchemy==2.1.8`、`alembic==1.20.1` |
| 驱动 / 连接池 | `asyncpg==0.32.0`、`psycopg==3.3.6`、`psycopg-pool==3.3.3`、`pgvector==0.5.1` |
| 编排 / LLM | `langgraph==1.2.18`、`langgraph-checkpoint-postgres==3.2.0`、`langchain-openai==1.7.3` |
| 模型 / 安全 | `pydantic==2.14.1`、`PyJWT==2.15.1`、`tiktoken==0.14.0` |
| 文档解析 | `pypdf`、`python-docx`、`openpyxl`、`lxml` |
| 开发依赖 | `pytest==9.2.0`、`pytest-asyncio==1.4.1.post1` |

### 前端

| 类别 | 依赖 |
| --- | --- |
| 框架 / 构建 | `vue ^3.5.44`、`vite ^8.3.5`、`typescript ~6.0.3`、`vue-tsc ^3.3.13`、`@vitejs/plugin-vue ^6.0.10` |
| 样式 | `tailwindcss ^3.4.19`（v3，非 v4）、`postcss`、`autoprefixer` |
| 流式 / 存储 | `@microsoft/fetch-event-source ^2.0.1`、`localforage ^1.10.0` |
| 内容渲染 | `markdown-it ^15`、`katex`、`highlight.js`、`dompurify` |
| 其他 | `lucide-vue-next`、`axios`、`qrcode.vue` |

**无 pinia、无 vue-router**：全局状态由 `core/workbench/useWorkbenchStore.ts` 的模块级 `reactive()` 单例承担；视图切换由工作台自身的 Tab 状态管理。

### 数据库

PostgreSQL 16 + pgvector。服务端 `vector` 扩展版本 **0.8.7**（由 `pgvector/pgvector:pg16` 镜像提供），Python 侧适配包 `pgvector==0.5.1` —— 两者是独立版本，不要混淆。

---

## 4. 仓库目录结构

```
enterprise-agent-scaffold/
├── CLAUDE.md                                    # 项目级工程规范（目录拓扑 / 红线 / 门禁）
├── PROJECT_ARCHITECTURE_AND_TEST_GUIDE.md       # 系统设计说明书 + 手工测试验证指南（3777 行）
├── 企业级通用 AI Agent 通用脚手架 · 落地开发与架构全景指南.md   # v2.0 生产级实施规约（2687 行）
├── docker-compose.yml                           # 仅含一个数据库服务
├── docs/architecture/                           # 00–09 系列架构与测试文档
├── project-docs/overall-structure.md
│
├── backend/
│   ├── main.py                                  # 唯一入口，FastAPI title="企业级 AI Agent 通用脚手架"
│   ├── alembic.ini                              # script_location = %(here)s/migrations；url 为占位符
│   ├── conftest.py / pytest.ini
│   ├── requirements.txt / requirements-dev.txt
│   ├── migrations/
│   │   ├── env.py                               # 动态注入 ASYNC_DB_URL；排除 LangGraph 原生表
│   │   └── versions/                            # 5 个线性迁移，head = d4e5f6a7b8c9
│   ├── scripts/
│   │   ├── check_plugin_isolation.py            # 隔离门禁
│   │   ├── rag_ingest_all.py / rag_ingest_file.py
│   │   ├── archive_stale_pending.py
│   │   └── init_db.sql                          # CREATE EXTENSION vector / uuid-ossp
│   ├── plugins/power_grid/documents/            # 业务知识文档（DL-408、GB26860 等）
│   ├── tests/                                   # 本地测试目录（内容不入库，仅保留 .gitkeep）
│   └── app/
│       ├── core/                                # ★ 通用底座：零业务词汇
│       │   ├── database.py                      # 双连接池
│       │   ├── llm.py / security.py
│       │   ├── workflow/
│       │   │   ├── graph_registry.py            # 状态图注册中心
│       │   │   ├── hitl.py                      # HITL + CAS 控制器
│       │   │   ├── message_recorder.py          # 消息 / 卡片投影
│       │   │   ├── thread_mapping_store.py      # 中枢表读写 + 会话聚合
│       │   │   ├── event_bus.py                 # 进程内事件总线
│       │   │   ├── trace_recorder.py
│       │   │   └── thread_access.py / base_state.py
│       │   ├── plugin/                          # manifest.py / loader.py
│       │   ├── policy/                          # protocols.py（只定义契约，不定义阈值）
│       │   └── rag/
│       │       ├── chunking.py / embeddings.py / vector_service.py
│       │       └── parsers/ base / factory / pdf / docx / excel / text
│       ├── plugins/power_grid/                  # ★ 唯一业务插件
│       │   ├── plugin.py                        # __all__ = ["PLUGIN"]
│       │   ├── router.py / graph.py / policy.py / chat.py
│       │   ├── constants.py / states.py / schemas.py
│       │   └── adapters/ oms_adapter.py / scada_adapter.py
│       ├── api/v1/                              # agent / auth / knowledge / plugin / threads / workflow
│       ├── models/ai_schema.py                  # 「3+N」四张 ai_ 表
│       ├── schemas/a2ui.py                      # 通用 A2UI 事件契约
│       └── services/acl/                        # iam_adapter.py / protocols.py
│
└── frontend/
    ├── src/
    │   ├── main.ts                              # import.meta.glob 自动装载插件
    │   ├── core/                                # ★ 通用底座：零业务词汇
    │   │   ├── a2ui/ types.ts / CardRegistry.ts / MessageCardHost.vue
    │   │   ├── api/ http.ts / agent.ts / threads.ts / workflow.ts / plugin.ts
    │   │   ├── composables/ useCurrentUser.ts / useWorkflowAction.ts
    │   │   ├── engine/ storage.ts / render_ticker.ts
    │   │   ├── plugins/registry.ts              # PluginDefinition 接口定义处
    │   │   ├── utils/ datetime.ts / markdown.ts
    │   │   ├── views/ WorkbenchLayout.vue / TaskApprovalPool.vue / TaskHistoryList.vue
    │   │   └── workbench/useWorkbenchStore.ts
    │   └── plugins/power_grid/                  # ★ 业务插件
    │       ├── plugin.ts
    │       ├── cards/ SafetyDiffCard / PermitResultCard / CitationsCard
    │       ├── forms/ SubstationApplyForm.vue
    │       └── panels/ WorkOrderInspectPanel.vue
    ├── vite.config.ts / tailwind.config.js / postcss.config.js
    └── tsconfig.json / tsconfig.app.json / tsconfig.node.json
```

### 职责对照表

| 目录 | 职责 | 可出现业务语义吗 |
| --- | --- | --- |
| `backend/app/core/` | 通用能力：状态图注册、HITL、存储、RAG 框架、插件装载 | ❌ 禁止 |
| `backend/app/plugins/<biz>/` | 业务状态图、路由、风险策略、外部系统适配器、业务常量 | ✅ 唯一合法位置 |
| `backend/app/api/v1/` | 通用网关：对话流、RAG、插件清单、线程 / 会话读取、工作流事件 | ❌ 禁止 |
| `backend/app/services/acl/` | 防腐层通用契约（PEP 544 Protocol） | ❌ 禁止 |
| `frontend/src/core/` | 通用工作台、卡片宿主、状态管理、离线存储 | ❌ 禁止 |
| `frontend/src/plugins/<biz>/` | 业务卡片、检视面板、表单组件、脱敏器 | ✅ 唯一合法位置 |

---

## 5. 快速启动

> 以下命令均为 **Windows PowerShell**。

### 5.1 前置要求

- Python **3.11**（示例环境为 conda `power-agent`）
- Node.js + npm
- Docker / Docker Compose（用于 PostgreSQL 16 + pgvector）
- 仓库内**没有 `.env.example`**，需自行创建环境变量文件（见 5.2 / 5.3）

### 5.2 数据库

`docker-compose.yml` **只含一个服务** `ai-postgres`（image `pgvector/pgvector:pg16`，container_name `power_ai_postgres`，端口 `5432:5432`，卷 `ai_pg_data`，挂载 `./backend/scripts/init_db.sql` 到 `/docker-entrypoint-initdb.d/init_db.sql`，healthcheck `pg_isready`）。容器调优：`max_connections=200` / `shared_buffers=512MB` / `work_mem=32MB` / `maintenance_work_mem=128MB`。

口令来自**项目根目录** `.env` 的 `DB_NAME` / `DB_USER` / `DB_PASSWORD`。**`DB_PASSWORD` 未配置时容器启动失败**（compose 中用 `${DB_PASSWORD:?…}` 做 fail-fast 校验，无硬编码口令）：

```powershell
# 在项目根目录准备 .env（可从 backend/.env 复制 DB_* 各项）
#   DB_NAME / DB_USER / DB_PASSWORD
docker-compose up -d

# 若不想复制到根目录，可直接指定 env 文件：
# docker compose --env-file backend/.env up -d
```

> **注意**：Compose 默认只读**项目根目录**的 `.env`，而本仓库的 env 文件位于 `backend/.env`。两者路径不同，裸跑 `docker-compose up -d` 会因取不到 `DB_PASSWORD` 而失败。

### 5.3 后端

```powershell
cd backend

# 安装依赖（测试用 requirements-dev.txt）
pip install -r requirements.txt

# 创建 backend/.env（仓库内无 .env.example，需自行创建）
#   DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD
#   OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL_NAME
#   LLM_TIMEOUT / LLM_MAX_RETRIES
#   EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL_NAME
#   EMBEDDING_DIM / EMBEDDING_SUPPORTS_DIMENSIONS
#   JWT_SECRET_KEY
#   ACTIVE_PLUGIN（可选，多插件共存时必填）

# 数据库迁移（必须在 backend/ 目录执行）
alembic upgrade head

# 启动（开发期带 --reload）
conda activate power-agent
python -m uvicorn main:app --reload --port 8000
```

**几点说明**：

- `alembic.ini` 的 `sqlalchemy.url` 是**占位符**，由 `migrations/env.py` 动态注入 `ASYNC_DB_URL`（`env.py` 读 `backend/.env`），无需手工改写。当前迁移 head 为 `d4e5f6a7b8c9`。
- **Windows 下必须带 `--reload`**（`psycopg` 与 `ProactorEventLoop` 不兼容）。`main.py` 顶部已内置 `asyncio.WindowsSelectorEventLoopPolicy` 防御，但 reload 模式仍是最稳的本地开发路径。做手工测试时若 uvicorn 的文件监听反复触发重启（pytest 写 `.pyc` 会命中），可去掉 `--reload` 改为 `python -m uvicorn main:app --port 8000`。
- `DB_USER` / `DB_PASSWORD` / `DB_NAME` 缺失会直接 `RuntimeError` fail-fast；`JWT_SECRET_KEY` 缺失同样拒绝启动（拒绝无密钥签发）。

### 5.4 前端

```powershell
cd frontend
npm install
npm run dev
```

Vite 默认 http://localhost:5173，`/api` 代理到 http://127.0.0.1:8000（开发期避免跨域）。前端 scripts 只有 `dev`(vite) / `build`(`vue-tsc -b && vite build`) / `preview`(vite preview)，**没有独立的 typecheck 脚本**。

> **部署形态**：后端**不挂载前端静态产物**（无 `StaticFiles` / `app.mount`），前后端独立交付，开发期靠 Vite 代理打通。

### 5.5 验证

```powershell
# 健康检查：网关存活 + 状态机挂载状态
curl.exe http://127.0.0.1:8000/api/v1/health
```

返回形如：

```json
{"code":200,"status":"ok","plugin":"power_grid","graph_ready":true}
```

`graph_ready: true` 是「后端完全就绪」的一站式断言。

### 5.6 通用 API 端点

全部前缀 `/api/v1`：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/auth/login` | 以预置 `user_id` 换取 Token |
| GET | `/auth/me` | 当前登录用户身份元数据 |
| POST | `/knowledge/search` | 向量检索 |
| POST | `/knowledge/ask` | RAG 问答（携带来源引用，未命中时明确拒答） |
| POST | `/agent/chat-stream` | SSE 流式对话（`token` / `ui_card` / `error` / `done`） |
| GET | `/plugin/manifest` | 插件清单（endpoints / identities 下发） |
| GET | `/threads` | 线程列表（支持标题搜索） |
| GET | `/sessions` | **会话级聚合列表**（左栏权威数据源） |
| GET | `/threads/{thread_id}` | 只读线程详情 |
| GET | `/threads/{thread_id}/messages` | 线程消息 |
| GET | `/sessions/{session_id}/messages` | 会话消息 |
| GET | `/workflow/events` | 工作流事件 SSE |
| GET | `/health` | 健康检查 |

业务插件路由按 manifest 的 `router_prefix` 挂载。`power_grid` 插件的前缀为 `/api/v1/power-dispatch`，端点为 `POST /apply`、`GET /tasks`、`GET /messages`、`POST /audit`。

---

## 6. 质量与隔离门禁

### 6.1 隔离门禁（后端）

`backend/scripts/check_plugin_isolation.py` 扫描以下目标：

- `backend/app/core/`
- `backend/app/schemas/a2ui.py`
- `backend/app/api/v1/{agent,plugin,workflow,threads}.py`
- `backend/app/services/acl/`
- `frontend/src/core/`

正则黑名单包含 `power[_-]grid`、`power[_-]dispatch`、`\bOMS\b`、`\bSCADA\b`、`dispatcher_audit`、`permit_result`、`power_grid_citations`、`变电站`、`调度令`、`检修`、`安规`、`停电`、`核签`、`许可令`、`运检`、`值班长`、`电压等级`、`\bkV\b`、`\bDISPATCHER\b`、`\bAPPLICANT\b` 等。命中即判违规，打印「文件:行号 命中「词」」并给出整改要求；注释中的说明性豁免需带 `@isolation-exempt` 标记。

```powershell
cd backend
python scripts/check_plugin_isolation.py    # 退出码 0 = 通过
```

### 6.2 类型门禁（前端）

```powershell
cd frontend
npx vue-tsc -b --force                      # 必须 exit 0
```

### 6.3 门禁的设计意图

这两个脚本是**架构约束的可执行形式**。没有它们，「底座零业务词汇」只是一句口号 —— 半年后没人记得哪些字段是业务专有的。有了它们，破坏隔离的改动会在提交前就被拦下，且报错信息直接指向「文件:行号 命中「词」」，整改路径明确（把词下沉到插件目录）。

---

## 7. 业务插件扩展指南

四步接入一个新业务，**底座零改动**。

### 第 1 步：建插件目录与清单

新建 `backend/app/plugins/<biz>/`，其中 `plugin.py` 导出 `PLUGIN = PluginManifest(...)`（`__all__ = ["PLUGIN"]`），声明：

- `router` —— 插件自身 `APIRouter`（不带前缀，由通用层按 `router_prefix` 挂载）
- `router_prefix` —— **必须以 `/api` 开头**（如 `/api/v1/sample-biz`），否则 `__post_init__` 抛 `PluginContractError`
- `graph_factory` + `graph_name` —— 声明 `graph_factory` 就**必须**同时给 `graph_name`
- `chat_handler` —— 可缺省
- `endpoint_paths` —— 相对前缀的端点路径映射（如 `{"tasks": "/tasks"}`）
- `identities` —— 演示身份（可选）

### 第 2 步：实现业务侧能力

在插件目录内实现：

| 文件 | 职责 |
| --- | --- |
| `graph.py` | 状态图工厂函数 + 节点实现 |
| `router.py` | 业务路由 |
| `policy.py` | 业务策略 —— **阈值常量放插件内**，实现 core 的 `RiskPolicyProtocol` |
| `adapters/` | 防腐层适配器，实现 core / services 定义的 Protocol（外部系统对接） |
| `constants.py` | 业务常量（插件名、图注册名、路由前缀、卡片类型） |
| `states.py` | 状态定义（继承 `BaseWorkflowState`） |

> **阈值归属原则**：`core/policy/protocols.py` 只定义 `RiskLevel = Literal["LOW","HIGH","UNKNOWN"]` 与 `RiskClassification` / `RiskPolicyProtocol` 契约，**不定义任何业务阈值**。以 `power_grid` 为例，`POWER_HUMAN_REVIEW_KV_MIN = 110`、`POWER_VOLTAGE_KV_MAX = 1000` 均为插件私有常量。

### 第 3 步：实现前端插件

新建 `frontend/src/plugins/<biz>/plugin.ts`，导出 `PluginDefinition`：

- `cards` —— 卡片映射（`card_type` → 组件）
- `inspectors` —— 只读检视面板
- `formComponent` + `formSectionTitle` —— 右栏业务表单与分区标题
- `cardSanitizers` —— 卡片脱敏器（业务特有敏感字段判定留在这里，core 只负责调用）
- `endpoints` / `identities` —— 端点默认值与演示身份（启动时被服务端 manifest 覆盖）

新建卡片 / 表单 / 面板组件。`main.ts` 的 `import.meta.glob` 会**自动装载**，无需改底座。

### 第 4 步：跑门禁并验证

```powershell
# 1) 隔离门禁：确认底座零业务词汇泄漏（必须 exit 0）
cd backend && python scripts/check_plugin_isolation.py

# 2) 前端类型门禁（必须 exit 0）
cd frontend && npx vue-tsc -b --force

# 3) 多插件共存时，在 backend/.env 指定生效插件
#    ACTIVE_PLUGIN=<biz>

# 4) 数据库迁移（若有新表）
cd backend && alembic upgrade head

# 5) 启动验证
python -m uvicorn main:app --reload --port 8000
```

### 参考样例：`power_grid` 的业务语义

作为「插件层承载业务」的完整示例（**这些规则属于插件，不属于底座**）：

- **状态图**：`safety_check → oms_draft → risk_classify → (human_audit | issue_permit) → END`；节点常量 `NODE_SAFETY_CHECK` / `NODE_OMS_DRAFT` / `NODE_RISK_CLASSIFY` / `NODE_HUMAN_AUDIT` / `NODE_ISSUE_PERMIT`。
- **电压定级**（二值判定，无灰区）：`≥ 110kV` → `HIGH` → **强制人工核签**；`< 110kV` → `LOW` → **自动签发调度许可令**；越界（`0 < kv ≤ 1000` 之外）或无法识别 → `UNKNOWN` → **转人工**（fail-closed 保守兜底）。`risk_classify` 节点必须无副作用 —— 状态机 `resume` 时会重放节点体。
- **路由前缀**：`/api/v1/power-dispatch`；端点 `POST /apply`、`GET /tasks`、`GET /messages`、`POST /audit`。
- **演示身份**：`PluginIdentity` 由插件在清单中声明，`PluginLoader` 启动时登记进 Mock IAM 账号目录，前端拉取 manifest 渲染身份切换器。

---

## 8. 文档索引

### 架构与设计文档（`docs/architecture/`）

| 文件 | 内容 |
| --- | --- |
| `00-全生命周期生产问题.md` | 生产问题总集：运行时环境、联调权限、架构边界 |
| `01-电力项目改造记录.md` | 从零部署 PG + pgvector 底座到系统级改造的落地清单 |
| `02-数据库架构与库表设计原则及快速落地指南.md` | 资产边界法则、三轨分离法则、「3+N」库表规范与落地 SOP |
| `03-Alembic 究竟是什么它的核心作用是什么.md` | 迁移工具的定位、边界与 `env.py` 逐段拆解 |
| `04-防腐层（Anti-Corruption Layer, ACL）架构指南.md` | ACL 拓扑、落地规范与接入新业务系统的实施流水线 |
| `05-企业级通用 RAG 架构设计与脚手架融合指南.md` | RAG 总设计：零物化、元数据前置过滤、软删除版本废止 |
| `06-企业级通用 AI Agent 前端脚手架改造与落地开发指南.md` | 前端 `CardRegistry` 协议驱动 + 业务插槽路由的解耦模型 |
| `07-企业级通用 AI Agent 通用脚手架 · 落地开发与架构全景指南.md` | v2.0 生产级实施规约（与根目录同名文件内容一致） |
| `08-项目架构与手工测试验证指南.md` | 系统设计说明书全文 + 端到端测试手册（较新版本） |
| `09-手动测试记录.md` | 手动测试记录（当前为占位，仅有收尾语） |

### 根级长文档

| 文件 | 内容 |
| --- | --- |
| `PROJECT_ARCHITECTURE_AND_TEST_GUIDE.md` | 技术栈全景清单、底座六大模块详细设计、三大核心动线全链路穿透、手把手端到端测试、现有问题与技术债清单、后续路线图（3777 行） |
| `企业级通用 AI Agent 通用脚手架 · 落地开发与架构全景指南.md` | v2.0 生产级实施规约（2687 行） |
| `CLAUDE.md` | 项目级工程规范：目录拓扑、接口契约、业务红线、本地运行门禁 |
| `project-docs/overall-structure.md` | 整体结构概览 |

### 运维脚本

| 脚本 | 用途 |
| --- | --- |
| `scripts/check_plugin_isolation.py` | 底座业务词汇隔离门禁（退出码 0 = 通过） |
| `scripts/rag_ingest_all.py --dir --biz-tag --doc-code --chunk-size --chunk-overlap` | 递归扫描目录批量入库 |
| `scripts/rag_ingest_file.py --files --biz-tag --doc-code --doc-version --overwrite` | 指定文件精确入库（`--overwrite` 先软废止旧版切片） |
| `scripts/archive_stale_pending.py --list --before --thread-id --apply` | 归档历史挂起记录（缺省为 dry-run 预览） |

---

**维护者备注**：`backend/tests/` 与 `frontend/tests/` 的内容已在 `.gitignore` 中排除（仓库内仅保留 `.gitkeep`），本地测试资产不入库；`backend/.env` 同样不入库，需按 [5.3 节](#53-后端) 自行创建。仓库当前未附带 LICENSE 文件，开源分发前请自行补充。
