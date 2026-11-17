# 企业级通用 RAG 架构设计与脚手架融合指南

## 一、 宏观认知：工业级 RAG 架构的本质与职责边界

在工业生产级系统中，RAG 绝不是单纯在 Web 视图函数中调用框架接口；低劣的 RAG 实现不仅会导致严重的幻觉扩散，更会因为知识库未建立数据生命周期、版本混乱、解析穿透而破坏下游业务流的合法性与系统稳定性。   

### 1. 核心架构拓扑图与数据流转

Plaintext

```
┌────────────────────────────────────────────────────────────────────────┐
│                   业务调用端 (FastAPI 路由 / LangGraph 节点)             │
│    • 通用问答 (POST /api/v1/knowledge/ask - 统一鉴权与出处回显)           │
│    • 多维检索 (POST /api/v1/knowledge/search - 元数据前置过滤)          │
│    • 状态机节点核验 (LangGraph: safety_check_node / compliance_node)    │
└───────────────────────────────────▲────────────────────────────────────┘
                                    │ 依赖注入 (Depends / VectorStoreService)
┌───────────────────────────────────▼────────────────────────────────────┐
│                    【通用脚手架 RAG 底座】(app/core/rag/)               │
│                                                                        │
│  ┌───────────────────────┐          ┌───────────────────────────────┐  │
│  │ 工业级摄入引擎 (Ingest)│          │ 复合多维检索引擎 (Retrieval)  │  │
│  │ • 目录深度递归遍历器  │          │ • 原生距离操作符 (<=> HNSW)   │  │
│  │ • 惰性流式解析器工厂  │          │ • 元数据 B-Tree 前置物理过滤  │  │
│  │ • 同节滑动语义分块器  │          │ • 软删除与版本过滤 (is_active)│  │
│  │ • MD5 指纹 CAS 防重   │          │ • 真实向量调用 + 伪向量降级   │  │
│  └───────────┬───────────┘          └───────────────▲───────────────┘  │
└──────────────┼──────────────────────────────────────┼──────────────────┘
               │ 异步批处理写入 (满 50 条 commit)       │ pgvector 亚毫秒检索
┌──────────────▼──────────────────────────────────────┴──────────────────┐
│                   AI 专属数据资产层 (PostgreSQL 16)                    │
│   • ai_knowledge_base: 切片正文、doc_code、biz_tag、Embedding (1536)   │
│   • HNSW 专用索引 (vector_cosine_ops, m=16, ef_construction=64)       │
│   • 业务标签 B-Tree 索引 (idx_knowledge_biz_tag, idx_knowledge_is_active)│
└────────────────────────────────────────────────────────────────────────┘
```

### 2. 必须坚守的五大工业底线

1. **全链路零物化（Zero Materialization）**： 从磁盘读取、文档解析、语义分块到向量批量入库，所有组件之间只传递 Python 生成器（`Iterator`），禁止在管道中间调用 `list()` 全量驻留内存。
2. **内容指纹防重（Deduplication by Content Hash）**： 切片入库前必须根据 `doc_code + chunk_index + md5(content)` 比对去重，实现天然幂等。避免因运维人员重复执行导入脚本导致向量库切片翻倍膨胀、挤占大模型上下文窗口（Context Window）。   
3. **元数据前置过滤法则（Metadata Pre-filtering）**： 严禁全库暴力向量计算。利用 `idx_knowledge_biz_tag` 与 `idx_knowledge_is_active` B-Tree 索引，在执行高维向量空间余弦距离计算前，先在 SQL 引擎层滤除无关业务范围（如按电压等级 `220kV` 过滤），确保亚毫秒级低延迟响应。   
4. **软删除与版本废止生命周期（Soft Deletion & Versioning）**： 新版规程（如从 `v1.0` 升级到 `v2.0`）发布时，老规程绝不物理 `DELETE`（以防审计与历史 Trace 无法复现），统一通过 `is_active = FALSE` 软删除。   
5. **摄入与查询物理隔离**： 知识摄入属于高 CPU/网络 I/O 密集型任务，必须通过独立的 CLI 运维脚本在后台离线执行；在线 Web 接口只负责查询，保持极简、低延迟、零阻塞。   

## 二、 工业上的必要性与企业应用实况

### 1. 传统 Toy RAG（简单做法）在企业中的四大致命缺陷

- **格式穿透与解析乱码**：直接用基础文件读取或简易工具处理企业资产。真实资产包含混杂表格的 `.xlsx`、多级嵌套的 `.docx`、不同编码（UTF-8/GBK）的 `.md`/`.txt`。简单工具遇到 Word 表格直接变成一团乱麻，导致向量化语义完全丢失。   
- **内存膨胀雪崩（OOM）**：直接把几十兆甚至上百兆的 PDF/DOCX 一次性加载进内存转化成巨型字符串，并发或处理大文档时直接触发容器 OOM 杀死进程。
- **缺乏工业级检索维度**：高维向量在精确数字和编码（如规范条文编号 `DL-408-2021 §4.2`）上极易失焦，工业系统必须依赖元数据前置过滤与向量混合召回。   
- **重复执行导致数据腐败**：缺少入库幂等机制，同一目录多次导入产生海量同质冗余向量，使得模型检索阶段发生“复读机”或召回同质切片。   

### 2. 企业级落地优缺点权衡

| **架构维度**     | **引入工业级通用 RAG 底座   企业级通用 RAG 架构设计与脚手架融合指南.md+ 1** | **玩具级简单 RAG（硬编码写入）   企业级通用 RAG 架构设计与脚手架融合指南.md** |
| ---------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| **通用可复用性** | **极高**。底盘负责解析、分块、pgvector 存取，换到金融/制造只需换文档目录。 | **无**。解析和向量库操作写死在业务代码中，完全不可拆卸。     |
| **内存表现**     | **平稳**。全链路流式生成器消费，同批仅驻留 50 条切片，内存占用恒定。 | **极度危险**。一次性 `read()` 巨型文档，极易造成堆内存爆破（OOM）。 |
| **多源异构解析** | **健全**。工厂模式覆盖 `.pdf`, `.docx`, `.xlsx`, `.csv`, `.md`, `.txt`。 | **脆弱**。只能读简单纯文本，遇到工业图表、Excel 直接报编码异常崩溃。 |
| **幂等与健壮性** | **强**。唯一指纹防重、软删除版本控制、单文件失败隔离跳过并打标记录。 | **极弱**。脚本中断导致半入库状态，重复运行产生海量垃圾冗余向量。 |
| **开发与维护**   | **结构清晰**。摄入走独立 CLI 脚本，状态机与 API 只需按契约调用 Service。 | **混乱**。初始化脚本混在业务逻辑里，接口耗时不可控。         |

## 三、 微观操作与通用脚手架落地规范

### 1. 完整目录结构设计

Plaintext

```
backend/
├── app/
│   ├── core/
│   │   ├── database.py                       # 双连接池基础设施 (SQLAlchemy 异步池 + psycopg 原生池)[cite: 8, 9]
│   │   ├── security.py                       # 通用安全鉴权 (get_current_user 拦截)
│   │   └── rag/                              # 【通用 RAG 框架底座】(100% 跨业务复用)
│   │       ├── __init__.py                   # 导出 ParsedDocument, Chunker, Service 等核心类
│   │       ├── embeddings.py                 # 统一向量化（真实 API + 确定性归一化伪向量降级）
│   │       ├── parsers/                      # 多格式工业流式解析器工厂
│   │       │   ├── __init__.py               # 工厂统一导出
│   │       │   ├── base.py                   # ParsedDocument 与 BaseParser (抽象主方法: iter_parse)
│   │       │   ├── text_parser.py            # .txt, .md 流式解析 (嗅探编码 UTF-8/GBK 自适应)
│   │       │   ├── docx_parser.py            # .docx 解析 (段落与表格转 Markdown 顺序流)
│   │       │   ├── pdf_parser.py             # .pdf 逐页惰性解析 (携带 page 元数据)
│   │       │   ├── excel_parser.py           # .xlsx (openpyxl 流) + .csv (标准库)，表头语义短句化
│   │       │   └── factory.py                # DocumentParserFactory (单例/路由，提供 iter_file)[cite: 7]
│   │       ├── chunking.py                   # 工业语义分块器 (层级标点递归 + 同节滑动窗口)[cite: 7]
│   │       └── vector_service.py             # 基于 ai_knowledge_base 的高性能检索/入库中枢
│   │
│   ├── models/
│   │   └── ai_schema.py                      # "3+N" 数据库模型定义 (包含 AIKnowledgeBase)
│   │
│   ├── api/v1/                               # 【业务 API】[cite: 7]
│   │   └── knowledge.py                      # 工业级通用检索与多维问答端点[cite: 7]
│   │
│   └── plugins/power_grid/                   # 【垂直业务插件层 (隔离非通用数据与逻辑)】[cite: 7]
│       └── documents/                        # 真实规程文档 (DL-408-2021 等)[cite: 7]
│           └── DL-408-2021_safety_rules.md
│
├── scripts/                                  # 【工业级离线运维 CLI 工具】[cite: 7]
│   ├── rag_ingest_all.py                     # 脚本 1：全量递归扫描入库 (幂等跳过已有)[cite: 7]
│   └── rag_ingest_file.py                    # 脚本 2：指定单文件/多文件增量/覆盖热更新[cite: 7]
│
└── tests/                                    # 【自动化测试规范】
    ├── test_rag_chunking.py                  # 分块器与哈希指纹单测
    ├── test_rag_vector.py                    # 向量存储中枢增删改查单测
    └── test_rag_api.py                       # FastAPI 接口端到端集成测试
```

### 2. 微观核心代码设计与逐行解析

#### ① 多格式解析器工厂与流式契约（`app/core/rag/parsers/`）

- **基类抽象契约 (`app/core/rag/parsers/base.py`)**： 将 `iter_parse` 确立为主抽象方法，强制所有子类解析器输出生成器，`parse()` 作为物化便捷方法兼容旧调用[cite: 7]：

Python

```
from abc import ABC, abstractmethod
from typing import Iterator
from pydantic import BaseModel, Field

class ParsedDocument(BaseModel):
    title: str = Field(..., description="文档标题或章节名")
    content: str = Field(..., description="解析抽取的纯文本")
    source_path: str = Field(..., description="物理文件路径")
    doc_type: str = Field(..., description="文件格式枚举 (pdf/docx/md/xlsx/csv/txt)")
    metadata: dict = Field(default_factory=dict, description="扩展元数据 (如行号、页码)")

class BaseParser(ABC):
    @abstractmethod
    def iter_parse(self, file_path: str) -> Iterator[ParsedDocument]:
        """
        核心抽象方法：以生成器形式流式逐段产出解析结果，禁止一次性内存驻留。
        子类必须实现此生成器逻辑。
        """
        pass

    def parse(self, file_path: str) -> list[ParsedDocument]:
        """便捷物化方法，用于小文件或需一次性获取全量列表的场景"""
        return list(self.iter_parse(file_path))
```

- **纯文本与 Markdown 解析器 (`text_parser.py`)**： 先嗅探前 4096 字节探测编码（UTF-8/GBK），再逐行流式读取，按行批次输出，彻底避免全量加载与乱码：

Python

```
import chardet
from typing import Iterator
from app.core.rag.parsers.base import BaseParser, ParsedDocument

class TextParser(BaseParser):
    def __init__(self, batch_lines: int = 200):
        self.batch_lines = batch_lines

    def iter_parse(self, file_path: str) -> Iterator[ParsedDocument]:
        # 1. 嗅探前缀判定编码，避免中途解码失败导致重复产出已读行
        with open(file_path, "rb") as f:
            raw_sample = f.read(4096)
            detected = chardet.detect(raw_sample)
            encoding = detected.get("encoding") or "utf-8"

        # 2. 惰性逐行读取，按固定行数聚合分批 yield
        with open(file_path, "r", encoding=encoding, errors="replace") as f:
            buffer = []
            start_line = 1
            for current_line, line in enumerate(f, 1):
                buffer.append(line)
                if len(buffer) >= self.batch_lines:
                    yield ParsedDocument(
                        title=f"行 {start_line}-{current_line}",
                        content="".join(buffer),
                        source_path=file_path,
                        doc_type="text",
                        metadata={"start_line": start_line, "end_line": current_line}
                    )
                    buffer.clear()
                    start_line = current_line + 1
            if buffer:
                yield ParsedDocument(
                    title=f"行 {start_line}-{current_line}",
                    content="".join(buffer),
                    source_path=file_path,
                    doc_type="text",
                    metadata={"start_line": start_line, "end_line": current_line}
                )
```

- **表格类统一解析器 (`excel_parser.py`)**： **工程决策**：`openpyxl` 底层仅支持 Office Open XML 压缩包（`.xlsx`），若强行打开 `.csv` 会抛出 `InvalidFileException`。因此在同一个解析器门面下，`.xlsx` 采用 `openpyxl(read_only=True)` 流式读取，`.csv` 调用标准库 `csv.reader`，二者统一产出“表头语义短句”：

Python

```
import csv
import openpyxl
from typing import Iterator
from app.core.rag.parsers.base import BaseParser, ParsedDocument

class ExcelParser(BaseParser):
    def iter_parse(self, file_path: str) -> Iterator[ParsedDocument]:
        if file_path.lower().endswith(".csv"):
            yield from self._parse_csv(file_path)
        else:
            yield from self._parse_xlsx(file_path)

    def _parse_csv(self, file_path: str) -> Iterator[ParsedDocument]:
        # 统一以标准库按行读取，保留列名语义映射
        with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f)
            headers = next(reader, None)
            if not headers:
                return
            for row_idx, row in enumerate(reader, start=2):
                parts = [f"{headers[i]}: {val.strip()}" for i, val in enumerate(row) if i < len(headers) and val.strip()]
                if parts:
                    yield ParsedDocument(
                        title=f"第 {row_idx} 行数据",
                        content=", ".join(parts),
                        source_path=file_path,
                        doc_type="csv",
                        metadata={"row_index": row_idx}
                    )

    def _parse_xlsx(self, file_path: str) -> Iterator[ParsedDocument]:
        # 使用 read_only=True 启用底层生成器流，防止大表耗尽内存
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        try:
            for sheet in wb.worksheets:
                rows_gen = sheet.iter_rows(values_only=True)
                headers = next(rows_gen, None)
                if not headers:
                    continue
                headers = [str(h) if h is not None else f"列{i+1}" for i, h in enumerate(headers)]
                for row_idx, row in enumerate(rows_gen, start=2):
                    parts = [f"{headers[i]}: {str(val).strip()}" for i, val in enumerate(row) if i < len(headers) and val is not None and str(val).strip()]
                    if parts:
                        yield ParsedDocument(
                            title=f"{sheet.title}-行{row_idx}",
                            content=", ".join(parts),
                            source_path=file_path,
                            doc_type="xlsx",
                            metadata={"sheet": sheet.title, "row_index": row_idx}
                        )
        finally:
            wb.close()
```

- **PDF 与 DOCX 流式注意点**：
  - **PDF (`pdf_parser.py`)**：使用 `pypdf.PdfReader` 逐页提取文本并 `yield`，元数据携带 `{"page": page_num}`。
  - **DOCX (`docx_parser.py`)**：因 `python-docx` 底层必须解析完整 XML 树，无法实现磁盘层物理惰性，但本设计实现了“内容驻留层流式”——段落与表格按原文档自然顺序遍历，表格实时转为 Markdown Table 字符串并逐块产出，防止拼接生成几十兆的大文本变量。

#### ② 工业语义分块器（`app/core/rag/chunking.py`）

实现**层级标点递归切分**、**同节滑动重叠（Overlap）\**以及\**MD5 内容指纹自动派生**[cite: 7]：

Python

```
import hashlib
from typing import Iterator, Iterable
from pydantic import BaseModel, Field
from app.core.rag.parsers.base import ParsedDocument

class DocumentChunk(BaseModel):
    doc_code: str
    biz_tag: str
    section_title: str | None = None
    content: str
    chunk_index: int
    content_hash: str = ""

    def model_post_init(self, __context):
        # 自动派生不可逆内容指纹：doc_code + 归一化正文的 MD5
        if not self.content_hash:
            raw = f"{self.doc_code}:{self.content.strip()}"
            self.content_hash = hashlib.md5(raw.encode("utf-8")).hexdigest()

class RecursiveSemanticChunker:
    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        # 工业层级切分标点优先级：段落 ➔ 换行 ➔ 句末标点 ➔ 分号
        self.separators = ["\n\n", "\n", "；", "。", "！", "？", ";", ". "]

    def iter_split_sections(
        self, 
        sections: Iterable[ParsedDocument], 
        doc_code: str, 
        biz_tag: str
    ) -> Iterator[DocumentChunk]:
        """
        逐 Section 消费生成器，绝不在分块前全量 list(sections)。
        重要工程准则：重叠窗口 (Overlap) 仅在同一 section 内部滑动，
        严禁跨章节、跨页重叠，防止语义上下文污染。
        """
        chunk_counter = 0
        for section in sections:
            raw_text = section.content.strip()
            if not raw_text:
                continue

            sub_chunks = self._recursive_split(raw_text, self.separators)
            for text in sub_chunks:
                if not text.strip():
                    continue
                chunk_counter += 1
                yield DocumentChunk(
                    doc_code=doc_code,
                    biz_tag=biz_tag,
                    section_title=section.title,
                    content=text.strip(),
                    chunk_index=chunk_counter
                )

    def _recursive_split(self, text: str, separators: list[str]) -> list[str]:
        if len(text) <= self.chunk_size or not separators:
            return [text]

        sep = separators[0]
        remaining_seps = separators[1:]
        splits = text.split(sep)
        result = []
        current_buffer = ""

        for s in splits:
            item = s if not current_buffer else sep + s
            if len(current_buffer) + len(item) <= self.chunk_size:
                current_buffer += item
            else:
                if current_buffer:
                    result.append(current_buffer)
                    # 保留同 Section 内部重叠步长
                    overlap_len = min(len(current_buffer), self.chunk_overlap)
                    current_buffer = current_buffer[-overlap_len:] + item
                else:
                    # 单段过长，降级进入下一层标点切分
                    result.extend(self._recursive_split(item, remaining_seps))
                    current_buffer = ""

        if current_buffer:
            result.append(current_buffer)
        return result
```

#### ③ 向量中枢与索引保护检索（`app/core/rag/vector_service.py`）

- **向量服务降级与高维正交认知 (`embeddings.py`)**： 优先调用配置的模型 API；在离线或单测无网络环境下，基于文本 MD5 派生 1536 维伪向量并做 L2 归一化（$\Vert{}v\Vert{}_2 = 1$）。

  > **关键排查认知**：在 1536 维的高维数学空间中，伪随机向量彼此几乎完全正交，不同文字之间的余弦相似度极低（通常仅在 0.01~0.03 波动）。进行纯离线单测时，若设置 `min_similarity=0.5` 会被 SQL 直接过滤；测试离线检索链路时应进行**原句精确检索**（相似度为 1.0）或将测试阈值配置为 `min_similarity=0.0`。

- **向量中枢业务服务 (`vector_service.py`)**： 实现分批聚合写入、内容比对幂等过滤、HNSW 原生路径保护检索与软废止：   

Python

```
import uuid
from typing import Iterable
from sqlalchemy import select, text
from app.core.database import AsyncSessionLocal
from app.models.ai_schema import AIKnowledgeBase
from app.core.rag.chunking import DocumentChunk
from app.core.rag.embeddings import EmbeddingService

class VectorStoreService:
    def __init__(self):
        self.embedding_service = EmbeddingService()

    async def batch_upsert_chunks(self, chunks: Iterable[DocumentChunk], batch_size: int = 50) -> int:
        """
        分批迭代消费生成器，满 batch_size (默认 50) 即触发一次 flush 并提交，
        外层同时间只驻留一个批次内存，随写随释放。
        """
        inserted_total = 0
        buffer = []
        for chunk in chunks:
            buffer.append(chunk)
            if len(buffer) >= batch_size:
                inserted_total += await self._flush_batch(buffer)
                buffer.clear()
        if buffer:
            inserted_total += await self._flush_batch(buffer)
        return inserted_total

    async def _flush_batch(self, batch: list[DocumentChunk]) -> int:
        async with AsyncSessionLocal() as session:
            # 1. 幂等排重查询：查出当前批次内已存在的 (doc_code, content)
            doc_codes = list({c.doc_code for c in batch})
            stmt = select(AIKnowledgeBase.doc_code, AIKnowledgeBase.content).where(
                AIKnowledgeBase.doc_code.in_(doc_codes),
                AIKnowledgeBase.is_active == True
            )
            result = await session.execute(stmt)
            existing_set = set(result.all())

            # 2. 内存集合差集计算，剔除重复切片
            to_insert = [
                c for c in batch if (c.doc_code, c.content.strip()) not in existing_set
            ]
            if not to_insert:
                return 0

            # 3. 批量生成向量
            contents = [c.content for c in to_insert]
            vectors = await self.embedding_service.get_embeddings(contents)

            # 4. 组装入库实体
            for chunk, vec in zip(to_insert, vectors):
                record = AIKnowledgeBase(
                    id=str(uuid.uuid4()),
                    doc_code=chunk.doc_code,
                    biz_tag=chunk.biz_tag,
                    section_title=chunk.section_title,
                    content=chunk.content.strip(),
                    embedding=vec,
                    is_active=True,
                    doc_version="v1.0"
                )
                session.add(record)
            
            await session.commit()
            return len(to_insert)

    async def search_advanced(
        self,
        query: str,
        biz_tag: str | None = None,
        doc_code: str | None = None,
        top_k: int = 3,
        min_similarity: float = 0.5
    ) -> list[dict]:
        """
        HNSW 索引保护检索：
        排序必须直接写原生距离表达式 ORDER BY embedding <=> CAST(:vec_str AS vector)，
        禁止按别名 similarity 排序，避免 PostgreSQL 执行计划放弃 HNSW 索引降级为全表扫。
        """
        query_vectors = await self.embedding_service.get_embeddings([query])
        vec_str = str(query_vectors[0])

        conditions = ["is_active = TRUE"]
        params = {"vec_str": vec_str, "top_k": top_k, "min_sim": min_similarity}

        if biz_tag:
            conditions.append("biz_tag = :biz_tag")
            params["biz_tag"] = biz_tag
        if doc_code:
            conditions.append("doc_code = :doc_code")
            params["doc_code"] = doc_code

        where_clause = " AND ".join(conditions)

        sql = text(f"""
            SELECT doc_code, biz_tag, section_title, content,
                   (1 - (embedding <=> CAST(:vec_str AS vector))) AS similarity
            FROM ai_knowledge_base
            WHERE {where_clause}
              AND (1 - (embedding <=> CAST(:vec_str AS vector))) >= :min_sim
            ORDER BY embedding <=> CAST(:vec_str AS vector) ASC
            LIMIT :top_k;
        """)

        async with AsyncSessionLocal() as session:
            res = await session.execute(sql, params)
            return [dict(row._mapping) for row in res.fetchall()]

    async def soft_delete_document(self, doc_code: str) -> int:
        """软废止规程切片，供热更新覆盖与生命周期维护"""
        async with AsyncSessionLocal() as session:
            stmt = text("UPDATE ai_knowledge_base SET is_active = FALSE WHERE doc_code = :doc_code AND is_active = TRUE")
            res = await session.execute(stmt, {"doc_code": doc_code})
            await session.commit()
            return res.rowcount
```

## 四、 两个独立离线运维脚本与批处理设计

按照脚手架设计原则，大批量摄入任务必须通过独立的 CLI 工具与在线 Web 服务物理隔离[cite: 7]。

### 1. 脚本一：全量目录递归扫描入库（`scripts/rag_ingest_all.py`）

- **运行命令**：

  Bash

  ```
  python scripts/rag_ingest_all.py --dir ./plugins/power_grid/documents --biz-tag 220kV --doc-code DL-408-2021
  ```

- **企业级工程实现规范**：

  - **跨平台循环策略**：开头显式判断 `sys.platform == "win32"`，配置 `WindowsSelectorEventLoopPolicy`，防止 Windows 下异步 I/O 报错。   
  - **递归与异常隔离**：使用 `os.walk` 遍历子目录，遇到破损文件打印 `Warning` 并继续，避免由于单个文件损坏中断整个目录的扫描入库。
  - **大屏报表回显**：最终输出格式化报表，包含 `扫描文件数`、`生成切片数`、`实际插入数`、`幂等跳过数` 和 `耗时`。

Python

```
# backend/scripts/rag_ingest_all.py 核心片段
import os
import sys
import time
import asyncio
import argparse

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.core.rag.parsers.factory import DocumentParserFactory
from app.core.rag.chunking import RecursiveSemanticChunker
from app.core.rag.vector_service import VectorStoreService

async def main():
    parser = argparse.ArgumentParser(description="全量知识库目录递归流式摄入工具")
    parser.add_argument("--dir", required=True, help="规程文档根目录")
    parser.add_argument("--biz-tag", required=True, help="业务分类标签")
    parser.add_argument("--doc-code", default=None, help="统一规程编号 (缺省使用文件名)")
    args = parser.parse_args()

    start_time = time.time()
    chunker = RecursiveSemanticChunker()
    service = VectorStoreService()

    total_files, total_chunks, total_inserted = 0, 0, 0

    for root, _, files in os.walk(args.dir):
        for file in files:
            file_path = os.path.join(root, file)
            code = args.doc_code or os.path.splitext(file)[0]
            try:
                # 串联流式管道：解析 -> 分块 -> 批量提交
                sections_gen = DocumentParserFactory.iter_file(file_path)
                chunks_gen = chunker.iter_split_sections(sections_gen, doc_code=code, biz_tag=args.biz_tag)
                
                # 统计切片数并分批消费入库
                chunk_list = list(chunks_gen)  # 单文件内部切片生成
                if not chunk_list:
                    continue
                
                total_files += 1
                total_chunks += len(chunk_list)
                inserted = await service.batch_upsert_chunks(chunk_list)
                total_inserted += inserted
            except Exception as e:
                print(f"[WARN] 文件解析跳过: {file_path}, 原因: {str(e)}")

    elapsed = round(time.time() - start_time, 2)
    print("\n========== RAG 入库统计报表 ==========")
    print(f"扫描文件数 : {total_files}")
    print(f"生成切片数 : {total_chunks}")
    print(f"实际插入数 : {total_inserted}")
    print(f"幂等跳过数 : {total_chunks - total_inserted}")
    print(f"耗时(秒)   : {elapsed}")
    print("======================================")

if __name__ == "__main__":
    asyncio.run(main())
```

### 2. 脚本二：指定文件精准/覆盖入库（`scripts/rag_ingest_file.py`）

- **运行命令**：

  Bash

  ```
  python scripts/rag_ingest_file.py --files ./plugins/power_grid/documents/DL-408-2021_safety_rules.md --biz-tag 220kV --doc-code DL-408-2021 --doc-version v2.0 --overwrite
  ```

- **执行机理**： 支持传入单个或多个文件路径。若附带 `--overwrite` 开关，在切片入库前原子调用 `soft_delete_document(doc_code)` 将该规程的历史切片置为 `is_active = FALSE`，随后以新版本号灌入新切片，实现热更新与审计回溯。   

## 五、 在线服务端点：FastAPI 通用检索与防幻觉问答暴露

在线端点挂载于 `backend/app/api/v1/knowledge.py`，保持**100% 工业通用底座中立性**，严禁在后端路由或响应中硬编码任何特定垂直行业的私有词汇[cite: 7]。

### 1. 契约模型与路由挂载

Python

```
# backend/app/api/v1/knowledge.py
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from app.core.security import get_current_user
from app.core.rag.vector_service import VectorStoreService

knowledge_router = APIRouter(prefix="/knowledge", tags=["Universal-RAG"])

class SearchRequest(BaseModel):
    query: str
    biz_tag: str | None = None
    doc_code: str | None = None
    top_k: int = 3
    min_similarity: float = 0.5

class SearchResultItem(BaseModel):
    doc_code: str
    biz_tag: str
    section_title: str | None
    content: str
    similarity: float

class AskRequest(BaseModel):
    query: str
    biz_tag: str | None = None
    doc_code: str | None = None

class AskResponse(BaseModel):
    question: str
    answer: str
    citations: list[SearchResultItem]

@knowledge_router.post("/search", response_model=list[SearchResultItem])
async def search_knowledge(req: SearchRequest, current_user: dict = Depends(get_current_user)):
    """多维精准向量检索端点 (受 JWT 鉴权保护)"""
    service = VectorStoreService()
    results = await service.search_advanced(
        query=req.query,
        biz_tag=req.biz_tag,
        doc_code=req.doc_code,
        top_k=req.top_k,
        min_similarity=req.min_similarity
    )
    return results

@knowledge_router.post("/ask", response_model=AskResponse)
async def ask_knowledge(req: AskRequest, current_user: dict = Depends(get_current_user)):
    """带规范引文出处的通用问答端点 (防幻觉设计)"""
    service = VectorStoreService()
    citations_data = await service.search_advanced(
        query=req.query,
        biz_tag=req.biz_tag,
        doc_code=req.doc_code,
        top_k=3,
        min_similarity=0.3
    )

    citations = [SearchResultItem(**item) for item in citations_data]

    # 防御性降级设计：未检索到依据时明确拒绝，防止模型胡乱编造
    if not citations:
        return AskResponse(
            question=req.query,
            answer="知识库中暂未检索到与该问题直接相关的规范依据，请核实问题或联系系统管理员确认。",
            citations=[]
        )

    # 离线环境/未配置 API Key 时的规范引文自拼接格式
    body_lines = [f"- [{c.doc_code} {c.section_title or ''}] {c.content}" for c in citations]
    answer_text = "依据知识库规范，相关条款要求如下：\n" + "\n".join(body_lines)

    return AskResponse(
        question=req.query,
        answer=answer_text,
        citations=citations
    )
```

在 `backend/main.py` 中挂载此路由：

Python

```
from app.api.v1.knowledge import knowledge_router
app.include_router(knowledge_router, prefix="/api/v1")
```

## 六、 避坑与现场排查排错红宝书（真实实战总结）

本节记录此前在 Windows、VSCode、SQLAlchemy 与 pgvector 实战中出现的各类环境与逻辑陷阱，新人实施时遇错可直接在此对照解决：

### 1. `ModuleNotFoundError: No module named 'app'`

- **现象**：在 VSCode 终端执行 `python .\tests\test_xxx.py` 时直接抛出包丢失错误。

- **原因**：直接运行子目录脚本时，Python 会将脚本所在文件夹（`backend/tests`）而非工程根目录（`backend`）作为根搜索路径。

- **标准解决方案**： 在 `backend` 目录下统一使用模块模式运行单测：

  Bash

  ```
  python -m tests.test_rag_api
  ```

  或者在单测文件最顶部动态注入模块搜索路径：

  Python

  ```
  import sys
  from pathlib import Path
  sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
  ```

### 2. PowerShell 粘贴多行脚本卡死在 `>>`

- **现象**：向 VSCode 的 PowerShell 终端粘贴包含跨行引号的 `python -c "..."` 代码时，终端一直显示 `>>` 并不执行。
- **原因**：PowerShell 对跨行多重双引号的闭合存在转义解析限制。
- **标准解决方案**：严禁在 PowerShell 中直接粘贴包含复杂缩进的多行命令。应创建独立的 `.py` 脚本文件执行，或者使用 PowerShell 原生 Here-String 格式：`@'...'@ | Out-File -Encoding utf8 temp.py; python temp.py; Remove-Item temp.py`。

### 3. 单测 `assert inserted > 0` 抛出 `AssertionError: 首次入库行数应大于 0`

- **现象**：执行向量入库单测时，代码没有任何语法错误，但在断言新增行数时崩溃。

- **原因**：上一次运行测试时未物理清理数据，或者前序步骤已经成功写入过相同的 `doc_code` 和内容，底层的**幂等防重机制拦截了重复写入**，返回实际插入数为 `0`。

- **标准解决方案**： 在自动化单测中，使用带微秒时间戳的动态编码生成切片，确保单测数据空间独立：

  Python

  ```
  unique_code = f"TEST-DOC-{int(time.time() * 1000)}"
  ```

### 4. 离线伪向量单测召回数为 0（`len(results) == 0`）

- **现象**：入库成功且事务已 `COMMIT`，但随后执行 `search_advanced(query="...")` 却召回空列表。
- **原因**：本地未配置真实 API Key 时使用的是 MD5 伪向量。在 1536 维空间中，伪向量之间近乎正交（余弦相似度仅 0.02 左右）。SQL 中强制指定的 `AND similarity >= 0.5` 导致目标记录被直接过滤。
- **标准解决方案**： 在未连外网模型的离线单测模式下，应使用**原文精确检索**（自身匹配自身相似度约为 1.0），或将单测请求参数中的过滤阈值调低为 `min_similarity = 0.0` 验证 SQL 链路通畅性。

### 5. `openpyxl` 处理 `.csv` 崩溃

- **现象**：`InvalidFileException: openpyxl does not support .csv file_format, please use the csv module`。
- **原因**：`openpyxl` 物理上不支持逗号分隔的纯文本 CSV。
- **标准解决方案**：在 `ExcelParser` 内部根据文件后缀严格做门面路由：`.csv` 使用内置 `csv` 模块处理，`.xlsx` 走 `openpyxl(read_only=True)`，上层保持透明统一。

## 七、 新人实施落地检查核对表（Checklist）

新人完成 RAG 底座施工后，必须对照本清单逐项勾选验收，全部满足方可合入主分支：

| **验证阶段** | **检查项目**       | **工业级验收指标与判定标准**                                 | **状态** |
| ------------ | ------------------ | ------------------------------------------------------------ | -------- |
| **底层存储** | 向量扩展与维度     | PostgreSQL 成功安装 `vector` 扩展；`ai_knowledge_base` 表向量字段锁定为 `vector(1536)`；HNSW 索引通过 `vector_cosine_ops` 建立[cite: 7, 8]。 | [ ]      |
| **解析工厂** | 流式零物化契约     | `BaseParser.iter_parse()` 为生成器抽象主方法；所有解析器无一次性全量大变量驻留；CSV 独立走标准库[cite: 7]。 | [ ]      |
| **语义切片** | 同节滑动与指纹     | 层级标点递归切分生效；滑动重叠（Overlap）仅限段落内部，绝不跨页/跨章污染；自动填充 MD5 `content_hash`[cite: 7]。 | [ ]      |
| **存储中枢** | 批处理事务控制     | `batch_upsert_chunks` 按 50 条切片执行批处理 `flush` 与 `commit`，内存随写随释放[cite: 7]。 | [ ]      |
| **幂等机制** | 物理级重复阻断     | 对同一规程文件连续执行两次入库操作，第二次入库实际插入数必须为 0，终端明确回显幂等跳过[cite: 7]。 | [ ]      |
| **检索性能** | HNSW 索引执行路径  | SQL 排序直接使用 `ORDER BY embedding <=> CAST(:vec_str AS vector)` 原生操作符，绝不按别名排序[cite: 7]。 | [ ]      |
| **生命周期** | 规程热更新与软废止 | CLI 工具支持 `--overwrite` 参数；执行后旧切片 `is_active` 置为 `FALSE`，新切片以 `v2.0` 入库，历史记录完整保留可供审计[cite: 7, 8]。 | [ ]      |
| **服务接口** | FastAPI 路由安全   | 匿名未携带 Token 请求 `/api/v1/knowledge/search` 报 401 拦截；携带有效 Token 正常返回结构化切片数组。 | [ ]      |
| **防幻设计** | 规范引文与拒答策略 | 当 `/api/v1/knowledge/ask` 检索未命中有效依据时，返回标准中立兜底拒绝提示，命中时返回完整出处 `citations`。 |          |