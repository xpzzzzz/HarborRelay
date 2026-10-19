# HarborRelay｜校园心理支持与风险协同平台

HarborRelay 在原 MindBridge 项目基础上改名。现有数据库名、Redis Key、Chroma collection、MCP 工具名和 Ollama 模型文件名暂时保留原标识，以兼容已有数据与部署；新安装也可通过环境变量逐步迁移。

## 核心能力

* 学生端 SSE 流式聊天，前端可展示打字机式输出。
* Basic Auth 登录，支持学生和管理员角色隔离。
* LangGraph 多 Agent 工作流：Memory、Supervisor、Knowledge、RiskGuardian、Companion、Counselor，未安装 LangGraph 时自动回退到自研有限循环 runtime。
* 动态路由 RAG：先判断 `CHAT / CONSULT / RISK`，普通问题不查知识库，咨询和风险场景才进入检索增强。
* Chroma 向量 RAG 知识库：支持 OpenAI 或 Ollama Embeddings；当前 Docker 默认使用 `nomic-embed-text:latest`，与 BM25 关键词召回融合后进入本地 reranker；向量不可用时保留 BM25 + `hybrid_score` 降级链路。
* 心理风险评估：高风险词典优先、LLM JSON 评估、关键词兜底。
* 后台报告：记录情绪标签、情绪分数、风险等级、置信度和摘要，但学生端不展示后台评估结果。
* 风险个案处理：管理员可接手、填写跟进记录并关闭个案，处理过程保留操作人和时间。
* Agent 执行详情：后台按单轮展示路由、Agent 步骤和检索来源。
* 数据闭环：咨询/风险消息完整写入 MySQL，短期上下文写入 Redis，高风险消息写入 Excel 台账并通过邮件发送预警。
* 本地微调模型接入：支持通过 Ollama 加载 `mindbridge-qwen2.5-7b-ft-q4\\\_k\\\_m.gguf`。
* OpenAI-compatible API 接入：也可切换到云端模型。
* MCP 工具服务：暴露 Excel 报告写入和风险通知工具，后端高风险后处理通过 MCP client 调用这些工具。
* RAG 评测：Recall@K、Precision@K、MRR、NDCG@K、HitRate。

## 管理端处理流程

中高风险报告生成个案后，管理员在“风险个案闭环”中接手、添加跟进记录，再填写处理结果关闭个案。接手和关闭均会写入 `case_notes`；关闭后的个案只读。管理端“单轮执行记录”展示已有 `agent_run_traces` 中的路由、Agent 步骤和知识来源，不包含新采集的耗时指标。

## 技术栈

```text
语言：Python
Web 框架：FastAPI
服务运行：Uvicorn / ASGI
数据库：MySQL，SQLAlchemy ORM，PyMySQL 驱动
短期记忆：Redis
配置管理：pydantic-settings，.env
AI 接入：Ollama，本地微调 GGUF 模型，OpenAI-compatible API，Mock Provider
Agent 编排：LangGraph bounded agent loop，自研 runtime 兜底
RAG：本地知识库切块、OpenAI/Ollama Embeddings、Chroma 向量库、BM25、分数融合、本地 reranker、上下文扩展
流式输出：Server-Sent Events
文档解析：pypdf
Excel 台账：openpyxl
邮件预警：SMTP / smtplib
前端：原生 HTML / CSS / JavaScript
认证：Basic Auth
工具协议：MCP
```

说明：当前 Python 版已经提供 LangGraph runtime，入口在 `app/agents/langgraph\\\_runtime.py`；同时保留 `app/agents/runtime.py` 作为无框架兜底。RAG 默认使用 Chroma 本地持久化向量库做语义召回，同时用 BM25 做关键词召回，再融合并本地 rerank；未安装 Chroma、未配置 `OPENAI\\\_API\\\_KEY` 或向量服务异常时，会自动回退到本地 BM25 + `hybrid\\\_score` reranker，避免演示环境中断。

## 目录结构

```text
app/
├── agents/          # LangGraph 多 Agent 编排和自研 runtime 兜底
├── api/             # FastAPI 路由
├── core/            # 配置、数据库、安全、启动初始化
├── knowledge/       # 内置校园心理知识库
├── mcp\\\_tools/       # MCP 工具服务
├── models/          # SQLAlchemy 实体
├── rag\\\_eval/        # RAG 评测脚本和数据集
├── schemas/         # Pydantic DTO
├── services/        # AI、聊天、知识库、评估、报告、工具服务
└── static/          # 原生前端页面

models/mindbridge-qwen2.5-7b-ft/
├── Modelfile        # Ollama 模型定义
└── README.md        # GGUF 模型放置说明

scripts/
├── run-dev.sh
├── start-ollama.sh
├── create-finetuned-model.sh
└── package-release.sh
```

## Agent loop

每轮对话进入一个 LangGraph bounded agent loop，由 controller 根据共享状态自主选择下一个 Agent，最多执行 8 个 Agent 步，防止心理安全场景出现无限自主循环：

```text
controller
-> MemoryAgent / SupervisorAgent / KnowledgeAgent / RiskGuardianAgent / CompanionAgent / CounselorAgent
-> controller
-> END 或 max\\\_steps=8
-> SSE 流式输出
```

各 Agent 分工：

* `MemoryAgent`：优先从 Redis 读取本会话短期记忆；Redis 为空时从 MySQL 最近消息回填，并生成本轮记忆摘要。
* `SupervisorAgent`：判断 `CHAT / CONSULT / RISK`，决定是否进入心理支持链路。
* `KnowledgeAgent`：将学生输入改写为知识库查询词，执行 RAG 检索。
* `RiskGuardianAgent`：执行后台心理状态评估，同时保留高风险词库硬兜底。
* `CompanionAgent`：处理普通学习、编程、校园事务和闲聊。
* `CounselorAgent`：结合记忆、RAG 和风险评估，生成心理支持回复 prompt。

## 安装依赖

```bash
pip install -r requirements.txt
```

`requirements.txt` 已包含：

```text
langchain-core
langgraph
chromadb
pymysql
redis
```

如果交付环境暂时无法安装 LangGraph，系统仍会自动回退到自研 runtime，不影响 Mock 演示和基本功能。

## MySQL 和 Redis 配置

系统默认使用 MySQL 保存完整业务数据和完整聊天消息，使用 Redis 保存短期对话记忆。启动服务前先创建数据库：

```sql
CREATE DATABASE mindbridge DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4\\\_unicode\\\_ci;
CREATE USER 'mindbridge'@'%' IDENTIFIED BY 'mindbridge';
GRANT ALL PRIVILEGES ON mindbridge.\\\* TO 'mindbridge'@'%';
FLUSH PRIVILEGES;
```

`.env` 中配置连接：

```env
DATABASE\\\_URL=mysql+pymysql://mindbridge:mindbridge@127.0.0.1:3306/mindbridge?charset=utf8mb4
REDIS\\\_URL=redis://127.0.0.1:6379/0
REDIS\\\_MEMORY\\\_TTL\\\_SECONDS=86400
REDIS\\\_MEMORY\\\_MAX\\\_MESSAGES=40
```

完整聊天记录写入 MySQL 的 `chat\\\_sessions`、`chat\\\_messages` 等表。Redis 只保存每个会话最近 `REDIS\\\_MEMORY\\\_MAX\\\_MESSAGES` 条短期上下文，并通过 `REDIS\\\_MEMORY\\\_TTL\\\_SECONDS` 自动过期。

## 快速启动 Mock 演示

Mock 模式不需要 Ollama 或模型文件，但仍需要 MySQL 和 Redis，适合先演示完整业务流程。

```bash
cd mindbridge-py
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
AI\\\_PROVIDER=mock uvicorn app.main:app --host 127.0.0.1 --port 8080
```

浏览器打开：

```text
http://127.0.0.1:8080
```

默认账号：

```text
student / student123
admin / admin123
```

## Docker Compose 一键启动

仓库提供 `Dockerfile` 和 `docker-compose.yml`，会启动：

* `mysql`：MySQL 8.4，容器内端口 `3306`，宿主机映射 `13306`
* `redis`：Redis 7，容器内端口 `6379`，宿主机映射 `16379`
* `app`：HarborRelay FastAPI 服务，宿主机端口 `8080`

默认配置会让应用容器访问宿主机 Ollama：

```bash
docker compose up -d --build
```

如果 Ollama 已经有下列模型，容器即可使用真实本地聊天模型链路：

```text
mindbridge-qwen2.5-7b-ft:latest
```

## Chroma 向量库与快照

应用启动时会同步 `app/knowledge/*.md` 内置默认知识库到数据库。默认知识覆盖校园心理支持、风险等级、焦虑恐慌、情绪低落、睡眠作息、学业压力、考试季、人际关系、新生适应、咨询转介和隐私边界等主题。

知识库支持两个 embedding provider：

```text
KNOWLEDGE_EMBEDDING_PROVIDER=ollama
→ OLLAMA_BASE_URL
→ OLLAMA_EMBEDDING_MODEL=nomic-embed-text:latest
→ Ollama /api/embed

KNOWLEDGE_EMBEDDING_PROVIDER=openai
→ OPENAI_BASE_URL
→ OPENAI_API_KEY
→ OPENAI_EMBEDDING_MODEL=text-embedding-3-small
→ OpenAI-compatible /embeddings
```

Docker 部署推荐配置：

```env
KNOWLEDGE_EMBEDDING_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_EMBEDDING_MODEL=nomic-embed-text:latest

OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=
OPENAI_EMBEDDING_MODEL=text-embedding-3-small

KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
KNOWLEDGE_CANDIDATE_K=16
KNOWLEDGE_HYBRID_VECTOR_WEIGHT=0.65
KNOWLEDGE_HYBRID_BM25_WEIGHT=0.35
KNOWLEDGE_RERANK_ENABLED=true

CHROMA_PERSIST_DIR=data/chroma
CHROMA_COLLECTION_NAME=mindbridge_knowledge
CHROMA_SNAPSHOT_DIR=data/chroma-snapshots
CHROMA_SNAPSHOT_KEEP=5
EMBEDDING_TIMEOUT_SECONDS=120
```

主检索链路：

```text
Ollama/OpenAI embedding
→ Chroma 向量召回
+ BM25 关键词召回
→ 配置权重融合
→ 本地 reranker
→ 相邻知识分块扩展
```

当 `KNOWLEDGE_VECTOR_REQUIRED=false` 时，如果 Chroma 或当前 embedding provider 不可用，系统自动降级到：

```text
BM25 + hybrid_score reranker
```

设为 `true` 时，向量初始化或调用失败会直接暴露错误。

不同 provider 和模型使用独立 Chroma collection。collection identity 包含 provider 和模型哈希，数据库 `embedding_json` 同时记录 provider、model 和 dimension，因此不会把 OpenAI 与 Ollama 向量混合。显式执行重建接口时，会使用当前 provider 重新生成全部知识向量。

管理员接口：

```bash
curl -u admin:admin123 http://127.0.0.1:8080/api/admin/knowledge/status
curl -u admin:admin123 -X POST http://127.0.0.1:8080/api/admin/knowledge/rebuild-vector
curl -u admin:admin123 -X POST http://127.0.0.1:8080/api/admin/knowledge/backup
```

正常 Ollama 状态应包含：

```text
vectorAvailable: true
embeddingProvider: ollama
embeddingModel: nomic-embed-text:latest
embeddingDimension: 768
vectorChunks: 与 databaseChunks 一致
vectorError: 空
```

## 工具队列、限流与死信

心理报告生成后，工具链不会阻塞学生端流式回复，而是写入 `tool\\\_jobs` 队列表：

```text
EXCEL\\\_REPORT
CASE\\\_CREATE -> ALERT\\\_SEND
```

Excel 写入使用进程内锁串行化，个案创建保持幂等；预警发送使用独立线程池并支持每分钟限流。失败任务会按延迟重试，超过 `TOOL\\\_QUEUE\\\_MAX\\\_ATTEMPTS` 后进入 `dead\\\_letter\\\_records`。

```env
TOOL\\\_QUEUE\\\_ENABLED=true
TOOL\\\_QUEUE\\\_EXCEL\\\_WORKERS=1
TOOL\\\_QUEUE\\\_EMAIL\\\_WORKERS=2
ALERT\\\_EMAIL\\\_RATE\\\_LIMIT\\\_PER\\\_MINUTE=30
ALERT\\\_EMAIL\\\_DELIVERY\\\_MODE=log
```

`ALERT\\\_EMAIL\\\_DELIVERY\\\_MODE=log` 适合本地演示；生产发邮件时改为 `smtp` 并配置 SMTP。

## 邮件预警配置

高风险消息会触发心理报告，并由后端通过 MCP 工具调用完成 Excel 台账写入和邮件预警。发送邮件前需要在 `.env` 中配置 SMTP：

```env
SMTP\\\_HOST=smtp.example.com
SMTP\\\_PORT=587
SMTP\\\_USERNAME=your-account@example.com
SMTP\\\_PASSWORD=your-smtp-password
SMTP\\\_USE\\\_TLS=true
SMTP\\\_USE\\\_SSL=false
ALERT\\\_EMAIL\\\_FROM=your-account@example.com
ALERT\\\_EMAIL\\\_TO=counselor@example.com,admin@example.com
ALERT\\\_EMAIL\\\_SUBJECT\\\_PREFIX=\\\[HarborRelay 高风险预警]
```

未配置 SMTP 或收件人时，系统不会中断聊天流程，但会在 `alert\\\_records` 中写入 `FAILED` 记录，提示缺少的配置项。

## 接入本地微调 GGUF 模型

Python 版默认预留本地模型名：

```text
mindbridge-qwen2.5-7b-ft:latest
```

模型目录：

```text
models/mindbridge-qwen2.5-7b-ft/
```

需要放入的 GGUF 权重：

```text
models/mindbridge-qwen2.5-7b-ft/mindbridge-qwen2.5-7b-ft-q4\\\_k\\\_m.gguf
```

如果本机已经有其他位置的 GGUF 模型文件，可以通过 `UPSTREAM\\\_GGUF` 指定路径并建立软链接：

```bash
UPSTREAM\\\_GGUF=/path/to/mindbridge-qwen2.5-7b-ft-q4\\\_k\\\_m.gguf ./scripts/create-finetuned-model.sh
```

创建 Ollama 模型：

```bash
./scripts/create-finetuned-model.sh
```

启动 Ollama：

```bash
./scripts/start-ollama.sh
```

启动 Python 服务：

```bash
AI\\\_PROVIDER=ollama ./scripts/run-dev.sh
```

查看模型接入状态：

```bash
curl -u student:student123 http://127.0.0.1:8080/api/agent/status
```

返回结果中的 `finetunedModel.ggufExists` 和 `finetunedModel.modelfileExists` 会显示模型资产是否就绪。
同时 `agentFramework.active` 会显示当前实际使用的 Agent 编排框架：

```text
langgraph
custom
```

## 接入 OpenAI-compatible API

```bash
AI\\\_PROVIDER=openai \\\\
OPENAI\\\_API\\\_KEY=你的\\\_API\\\_Key \\\\
OPENAI\\\_MODEL=gpt-4o-mini \\\\
OPENAI\\\_EMBEDDING\\\_MODEL=text-embedding-3-small \\\\
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

知识库向量检索也使用同一个 `OPENAI\\\_API\\\_KEY` 调用 embeddings API。相关配置：

```env
KNOWLEDGE\\\_VECTOR\\\_ENABLED=true
KNOWLEDGE\\\_VECTOR\\\_REQUIRED=false
OPENAI\\\_EMBEDDING\\\_MODEL=text-embedding-3-small
KNOWLEDGE\\\_CANDIDATE\\\_K=16
KNOWLEDGE\\\_HYBRID\\\_VECTOR\\\_WEIGHT=0.65
KNOWLEDGE\\\_HYBRID\\\_BM25\\\_WEIGHT=0.35
KNOWLEDGE\\\_RERANK\\\_ENABLED=true
CHROMA\\\_PERSIST\\\_DIR=data/chroma
CHROMA\\\_COLLECTION\\\_NAME=mindbridge\\\_knowledge
```

当 `KNOWLEDGE\\\_VECTOR\\\_REQUIRED=false` 时，缺少 API key 或 Chroma 不可用不会阻断聊天，系统会回退到本地 BM25 + `hybrid\\\_score` reranker。若交付验收要求必须走 Chroma 向量检索，可设置 `KNOWLEDGE\\\_VECTOR\\\_REQUIRED=true`。

## 调用示例

学生流式聊天：

```bash
curl -N -u student:student123 \\\\
  -H 'Content-Type: application/json' \\\\
  -d '{"message":"我最近很焦虑，晚上总是睡不着"}' \\\\
  http://127.0.0.1:8080/api/chat/stream
```

高风险示例，会触发心理报告、风险个案创建和预警工具计划；Excel 保留为台账输出，邮件/log 是预警通道之一：

```bash
curl -N -u student:student123 \\\\
  -H 'Content-Type: application/json' \\\\
  -d '{"message":"我不想活了，感觉撑不下去了"}' \\\\
  http://127.0.0.1:8080/api/chat/stream
```

管理员查看报告：

```bash
curl -u admin:admin123 http://127.0.0.1:8080/api/admin/reports
```

管理员追加知识库：

```bash
curl -u admin:admin123 \\\\
  -H 'Content-Type: application/json' \\\\
  -d '{"source":"sleep-guide","content":"失眠时可先固定起床时间，减少睡前屏幕刺激，必要时联系校心理中心。"}' \\\\
  http://127.0.0.1:8080/api/admin/knowledge
```

追加知识库时，系统会同步写入 MySQL 分块和 Chroma 向量库；已有分块会在首次向量检索时自动补建 Chroma 索引。

## RAG 评测

```bash
AI\\\_PROVIDER=mock python -m app.rag\\\_eval.runner
```

评测报告输出到：

```text
target/rag-eval-report.json
```

## 单元测试

当前 `tests/` 里的基础回归用例使用 Python 标准库 `unittest`，不依赖 `pytest`：

```bash
python -m unittest discover -s tests
```

## Agent Runtime Harness

线上对话通过 `MindBridgeAgentHarness` 组织一次 Agent run。Harness 不改变 LangGraph / custom runtime 内部的多 Agent 协作顺序，而是在外层统一管理：

* 输入脱敏和 session 解析。
* Memory / Supervisor / Knowledge / RiskGuardian / Response Agent 调用。
* 心理报告落库和工具计划生成。
* 学生与助手消息持久化。
* Agent steps、知识召回、风险结果等 trace 数据输出。

因此 HTTP 层只负责认证和 SSE 流式输出，Agent 后处理逻辑集中在 runtime harness 内。

## Engineering Harness

项目提供一键工程 harness，用 mock AI、临时 SQLite、内存短期记忆和本地输出验证核心链路：

* Risk Safety Harness：高风险识别、报告生成、后台元数据不外显、工具队列入队。
* Agent Routing Harness：通过 `MindBridgeAgentHarness` 验证 CHAT / CONSULT / RISK 路由和多 Agent 步骤。
* Standard Skills Harness：验证 `skills/\\\*/SKILL.md` 标准 Skill 加载、选择逻辑和交接摘要模板渲染。
* RAG Harness：基于内置评测集验证 Recall@K、MRR、NDCG 和 HitRate。
* API Harness：健康检查、认证授权、SSE 聊天、管理员知识库接口。
* Tool Queue Harness：Excel / case / alert 依赖、幂等、限流和 dead letter。

```bash
python3 -m app.harness.runner
```

报告输出到：

```text
target/harness/harness-report.json
target/harness/rag-eval-report.json
```

## MCP 工具服务

MCP Python 包建议使用 Python 3.10 或 3.11 安装运行。

```bash
python -m app.mcp\\\_tools.server
```

业务后端触发报告后处理时，默认通过异步工具队列复用同一套工具实现；关闭队列后会作为 MCP client 通过 stdio 启动同一个 MCP server。

暴露工具：

* `mindbridge\\\_excel\\\_report`
* `mindbridge\\\_case\\\_create`
* `mindbridge\\\_alert\\\_send`
* `mindbridge\\\_alert\\\_ack`
* `mindbridge\\\_case\\\_note\\\_add`
* `mindbridge\\\_alert\\\_notify`

内置标准 Skills 位于 `skills/\\\*/SKILL.md`，运行时由 `MindBridgeSkillRegistry` 加载：

* `supportive\\\_response\\\_baseline`：心理咨询与风险回复的基础共情、边界和学生端表达规则。
* `high\\\_risk\\\_safety\\\_plan`：高风险时引导模型优先完成短期安全计划。
* `anxiety\\\_grounding\\\_support`：焦虑、惊恐、崩溃场景的稳定化和 grounding 指引。
* `sleep\\\_routine\\\_support`：失眠、睡眠节律紊乱场景的安全睡眠建议。
* `academic\\\_stress\\\_planning`：考试、作业、论文、绩点压力的下一步拆解。
* `referral\\\_resource\\\_guidance`：校内心理中心、辅导员、可信任支持人和紧急资源转介。
* `counselor\\\_handoff\\\_summary`：生成给辅导员/管理员看的个案交接摘要模板。



下次启动

1\.启动ollama和docker

2\.打开 Docker Desktop 软件界面。



在左侧菜单栏点击 Containers。



在列表中找到你的项目组（通常以 mindbridge-py 命名，里面包含 app、mysql、redis 三个子项）。



点击该项目组右侧的 “Start” (播放键图标)，等待它们的状态变为 Running / Healthy。
