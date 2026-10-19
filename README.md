# HarborRelay｜校园心理支持与风险协同平台

HarborRelay 在原 MindBridge 项目基础上改名。现有数据库名、Redis Key、Chroma collection、MCP 工具名和 Ollama 模型文件名暂时保留原标识，以兼容已有数据与部署；新安装也可通过环境变量逐步迁移。

## 核心能力

* 学生端 SSE 流式聊天，前端可展示打字机式输出。
* Basic Auth 与 Argon2id 密码验证，兼容旧 SHA-256 成功登录后升级；个案操作使用认证主体与角色/归属检查。
* LangGraph 确定性安全主流程 + 转介工具反馈子图；默认规则规划，单模型规划为实验选项；保留 custom runtime 兜底。
* 所有意图先做风险预检与独立评估；普通聊天跳过 RAG，高风险优先安全模板，一般咨询按需检索。
* Chroma 向量 RAG 知识库：支持 OpenAI 或 Ollama Embeddings；当前 Docker 默认使用 `nomic-embed-text:latest`，与 BM25 关键词召回融合后进入本地 reranker；向量不可用时保留 BM25 + `hybrid_score` 降级链路。
* 风险保护：规则预检、LLM JSON 评估、保守异常兜底；结果用于支持流程，不作为临床诊断。
* 后台报告：记录情绪标签、情绪分数、风险等级、置信度和摘要，但学生端不展示后台评估结果。
* 风险个案处理：管理员可接手、填写跟进记录并关闭个案，处理过程保留操作人和时间。
* Agent 执行详情：后台按单轮展示路由、Agent 步骤和检索来源。
* 报告与 ToolJob 在同一事务提交，先于 SSE；worker 执行台账/个案/告警，失败重试与死信可追踪。默认 log 告警不发送邮件。
* 本地模拟资源转介：根据工具结果补充查询、调整允许渠道、澄清或安全退出，输出来源与可编辑草稿，不执行预约或人工交接。
* 本地微调模型接入：支持通过 Ollama 加载 `mindbridge-qwen2.5-7b-ft-q4_k_m.gguf`。
* OpenAI-compatible API 接入：也可切换到云端模型。
* MCP 保留为可选本地 stdio 适配器；默认工具队列直接调用业务服务，未经认证的个案写操作拒绝执行。
* RAG 评测：Recall@K、Precision@K、MRR、NDCG@K、HitRate。

## 管理端处理流程

中高风险报告生成个案后，管理员在“风险个案闭环”中接手、添加跟进记录，再填写处理结果关闭个案。接手和关闭均会写入 `case_notes`；关闭后的个案只读。管理端“单轮执行记录”保留原字段；Trace JSON 增加任务、产物引用、工具观察状态、耗时和停止原因，新 Trace 不复制完整对话和 prompt。

## 技术栈

```text
语言：Python
Web 框架：FastAPI
服务运行：Uvicorn / ASGI
数据库：MySQL，SQLAlchemy ORM，PyMySQL 驱动
短期记忆：Redis
配置管理：pydantic-settings，.env
AI 接入：Ollama，本地微调 GGUF 模型，OpenAI-compatible API，Mock Provider
Agent 编排：LangGraph 安全主流程与单转介子图，custom runtime 兼容兜底
RAG：本地知识库切块、OpenAI/Ollama Embeddings、Chroma 向量库、BM25、分数融合、本地 reranker、上下文扩展
流式输出：Server-Sent Events
文档解析：pypdf
Excel 台账：openpyxl
邮件预警：SMTP / smtplib
前端：原生 HTML / CSS / JavaScript
认证：Basic Auth、Argon2id、服务端角色与个案归属校验
工具协议：可选 MCP stdio 适配器；默认内部 Service 调用
```

入口为 `app/agents/langgraph_runtime.py`，`app/agents/runtime.py` 提供相同安全契约的 custom 兜底。RAG 保留 Chroma + BM25 融合与本地重排；当配置的向量服务不可用且 `KNOWLEDGE_VECTOR_REQUIRED=false` 时回退本地检索。OpenAI embedding 需要 API Key，Ollama embedding 使用本地模型。

## 目录结构

```text
app/
├── agents/          # 安全主流程、强类型契约、转介反馈子图与 custom 兜底
├── evaluation/      # 隔离对照评测、合成数据与故障 Fixture
├── fixtures/        # 明确标记 simulated 的本地资源目录
├── api/             # FastAPI 路由
├── core/            # 配置、数据库、安全、启动初始化
├── knowledge/       # 内置校园心理知识库
├── mcp_tools/       # MCP 工具服务
├── models/          # SQLAlchemy 实体
├── rag_eval/        # RAG 评测脚本和数据集
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

```text
规则快速预检 → 必要上下文 → 强制风险评估 → 受约束的意图路由
  高风险：固定安全回复路径，跳过可选 RAG/Skill/自由生成
  资源转介：PLAN → TOOL_CALL → OBSERVE → REPLAN / ASK_USER / FINISH / SAFE_EXIT
  一般咨询：RAG + Skills → 回复 prompt
  普通聊天：回复 prompt
报告与 outbox 同事务提交 → SSE meta → 审核后模板 / 验证后的资源结果 / 普通模型 stream
工具 worker → 策略与审计 → ToolOrchestrationService
```

Memory、意图分类、RAG 和风险检查是主流程组件。转介默认 `REFERRAL_PLANNER=rules`，
按工具观察确定性选择下一步，不调用模型规划；这是带反馈的规则工作流，不宣称自主决策。
`REFERRAL_PLANNER=model` 显式启用单模型规划实验，两种策略共用 LangGraph 和安全执行器。
每次工具观察进入下一轮规划，隐私、时间、白名单和预算由执行层保护。
演示目录固定为模拟数据，未知可用性不描述为真实名额。

实验模式下 Ollama 规划默认使用原生 JSON Schema（`REFERRAL_ACTION_FORMAT=schema`），根据已观察
证据限制动作参数；工具白名单、隐私/时间限制、风险升级与完成校验仍在执行层实施。
`prompt` 模式可兼容回退，两者都会校验 Action。普通聊天不强制 JSON 格式。

真实 7B 模型目前仍会重复查询、错误退出或耗尽默认 15 秒预算。转介是实验性模拟能力，
不能把 Mock 全通过或真实模型的 JSON 合规率当作可靠规划的证明。2026-10-18 已完成
首轮本机 Ollama 测试和独立 MySQL 8/8 项验证，详见
[真实依赖验证与重现命令](docs/validation-2026-10-18/README.md)。
实际使用当前微调 7B 时保留默认 `REFERRAL_PLANNER=rules`；单模型规划尚未验证可靠。
`REFERRAL_ENABLED=false` 可完全关闭模拟转介。规则规划不调用模型不等于整轮聊天不调用模型，主流程风险与意图评估保留。

SSE 保留 meta/token/done/error。meta.outputMode 区分 `model_stream`、`safety_template`
和 `referral_result`；后两种一次发送已准备的文本，明确区别于实时模型生成。
普通模型 stream 没有整段发送前安全审核承诺；高风险最终文本审核未通过时不发送候选。

`REFERRAL_ENABLED=false` 可回退到原咨询路径；安全检查、授权与 outbox 不可关闭。
完整状态契约、流程图、投递语义、兼容和回滚方案见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 架构升级审查与复现

- [实施前代码审查](docs/ARCHITECTURE_REVIEW.md)
- [P0 → P1 → P2 → P4 分阶段记录](docs/IMPLEMENTATION_LOG.md)
- [真实运行的合成评测报告](docs/evaluation/report.md)
- [规则与单模型的配对对照](docs/planner-comparison-2026-10-18/README.md)
- [真实 Redis / Chroma / Ollama embedding 验证](docs/infrastructure-validation-2026-10-18/README.md)
- [P0/P1/P2/P4 最终验收、缓存一致性与忙写快照](docs/final-acceptance-2026-10-18/README.md)

```powershell
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-test.txt
$env:PYTHONIOENCODING = 'utf-8'
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe -m app.harness.runner --suite all --json
.venv/Scripts/python.exe -m app.evaluation.runner --baseline-ref f172dd8 --repeat 3
```

上述入口使用隔离 SQLite、合成账户、Mock AI、本地 Fixture 和 log 告警；SMTP 专项单测
会启动短命的 loopback 接收器，只接收 `.invalid` 合成地址且不转发。无需已有
MySQL/Redis/Ollama/SMTP。`requirements-test.txt` 不安装可选 Chroma，真实向量库未被
这组评测验证。评测自动将本地旧提交导出到临时目录，使用相同输入/运行次数对照，
输出 `target/evaluation/report.json`、Markdown、逐例结果与 `failures.json`。

最终验收：126/126 单测、6/6 Harness、Mock 对话 21/21、故障场景 45/45、7 项安全门槛通过。
真实依赖另用隔离容器验证：Redis/Chroma 16/16、MySQL 9/9，包含缓存丢写/乱序后的完整
消息窗口校验，以及并发写入时 300 条向量的逻辑快照和新进程恢复。执行：

```powershell
.venv/Scripts/python.exe -m app.evaluation.infrastructure_runner
.venv/Scripts/python.exe -m app.evaluation.mysql_runner
```

需要缓存的 Docker 镜像与本机已安装的 nomic 模型；临时环境会安装当前 requirements。
不复用业务 Redis/数据库或现有数据卷。实测 Chroma 范围为 Linux Python 3.12，详细依赖、
失败记录和最终验收边界见上方报告。真实向量自匹配不代表完整 RAG 质量复评。

Redis 读取先查询数据库可见的最近消息 ID 窗口，只有整个窗口一致才使用缓存正文；漏写、
重复、乱序和旧缓存回填都作为 miss，回读持久消息并脱敏。缓存字段增加 `messageId`，
旧数据自动失效重填，无需改 Redis Key 或迁移表。此保证以现有追加式消息 API 为前提，
不承诺并发聊天请求具有串行对话语义。

Chroma 新快照为当前 provider/model 集合的逻辑导出（JSONL + SHA-256 清单），发布前由
应用锁隔离同进程写入。恢复校验格式、维度、数量、摘要和集合身份，只能写入新目录，
不会自动切换正在使用的索引。示例：

```powershell
.venv/Scripts/python.exe -m app.services.vector_snapshots --snapshot data/chroma-snapshots/logical-<id> --destination data/chroma-restored-new
```

需完整 requirements 和匹配的 provider/model/collection 配置。旧版目录复制快照不由新恢复
入口读取，也不自动删除；先离线验证，或由数据库重建索引。嵌入式 Chroma 限单写进程，
不支持多个 Uvicorn worker 共写该目录。逻辑快照不包含业务数据库，不代替灾难恢复备份。

Mock 通过不代表真实模型规划质量、临床风险检出率或生产可用性。P3（额外模型/独立
Agent）缺少消融收益证据，P5（跨客户端 MCP）缺少真实第二客户端需求，均暂不实施。

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
CREATE DATABASE mindbridge DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'mindbridge'@'%' IDENTIFIED BY 'mindbridge';
GRANT ALL PRIVILEGES ON mindbridge.* TO 'mindbridge'@'%';
FLUSH PRIVILEGES;
```

`.env` 中配置连接：

```env
DATABASE_URL=mysql+pymysql://mindbridge:mindbridge@127.0.0.1:3306/mindbridge?charset=utf8mb4
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_MEMORY_TTL_SECONDS=86400
REDIS_MEMORY_MAX_MESSAGES=40
```

完整聊天记录写入 MySQL 的 `chat_sessions`、`chat_messages` 等表。Redis 只保存每个会话最近 `REDIS_MEMORY_MAX_MESSAGES` 条短期上下文，并通过 `REDIS_MEMORY_TTL_SECONDS` 自动过期。

## 快速启动 Mock 演示

Mock 模式不需要 Ollama 或模型文件，但仍需要 MySQL 和 Redis，适合先演示完整业务流程。

```bash
cd mindbridge-py
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
AI_PROVIDER=mock uvicorn app.main:app --host 127.0.0.1 --port 8080
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

报告和 ToolJob 在同一数据库事务提交，并先于首个 SSE 事件；用户断连或模型流失败
不会取消已提交的任务。普通咨询写台账，中高风险创建个案，高风险安排预警：

```text
EXCEL_REPORT
CASE_CREATE → ALERT_SEND
```

告警依赖个案创建，不依赖 Excel 成功。worker 在执行前验证持久化风险策略，记录允许/
拒绝/成功/失败，按限额重试并进入死信。重复入队按 report_id/kind 去重，DEAD 不会被
悄悄重建。Excel 按报告 ID 防止文件已写、DB 未提交后的重复追加。

```env
TOOL_QUEUE_ENABLED=true
TOOL_QUEUE_MAX_ATTEMPTS=3
TOOL_QUEUE_STALE_SECONDS=300
TOOL_QUEUE_EXCEL_WORKERS=1
TOOL_QUEUE_EMAIL_WORKERS=2
ALERT_EMAIL_RATE_LIMIT_PER_MINUTE=30
ALERT_EMAIL_DELIVERY_MODE=log
```

`TOOL_QUEUE_ENABLED=false` 暂停 worker，任务仍持久化等待恢复，不会改走 MCP。
默认本地单 worker；MySQL 入队竞争、条件认领与进程退出窗口已有隔离验证，多进程共享
Excel/SMTP 的分布式投递仍不承诺。SMTP 至少一次执行；部分收件人被拒收时记录失败，
发送确认丢失或重试时仍可能重复。log 模式只记录而不发送。

## 邮件预警配置

高风险消息会触发心理报告与 outbox，由工具 worker 直接调用业务服务完成台账、个案与告警。发送邮件前需要在 `.env` 中配置 SMTP：

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=your-account@example.com
SMTP_PASSWORD=your-smtp-password
SMTP_USE_TLS=true
SMTP_USE_SSL=false
ALERT_EMAIL_DELIVERY_MODE=smtp
ALERT_EMAIL_FROM=your-account@example.com
ALERT_EMAIL_TO=counselor@example.com,admin@example.com
ALERT_EMAIL_SUBJECT_PREFIX=[HarborRelay 高风险预警]
```

未配置 SMTP 或收件人时，系统不会中断聊天流程，但会在 `alert_records` 中写入 `FAILED` 记录，提示缺少的配置项。

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
models/mindbridge-qwen2.5-7b-ft/mindbridge-qwen2.5-7b-ft-q4_k_m.gguf
```

如果本机已经有其他位置的 GGUF 模型文件，可以通过 `UPSTREAM_GGUF` 指定路径并建立软链接：

```bash
UPSTREAM_GGUF=/path/to/mindbridge-qwen2.5-7b-ft-q4_k_m.gguf ./scripts/create-finetuned-model.sh
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
AI_PROVIDER=ollama ./scripts/run-dev.sh
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
AI_PROVIDER=openai \\\\
OPENAI_API_KEY=你的_API_Key \\\\
OPENAI_MODEL=gpt-4o-mini \\\\
OPENAI_EMBEDDING_MODEL=text-embedding-3-small \\\\
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

知识库向量检索也使用同一个 `OPENAI_API_KEY` 调用 embeddings API。相关配置：

```env
KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
KNOWLEDGE_CANDIDATE_K=16
KNOWLEDGE_HYBRID_VECTOR_WEIGHT=0.65
KNOWLEDGE_HYBRID_BM25_WEIGHT=0.35
KNOWLEDGE_RERANK_ENABLED=true
CHROMA_PERSIST_DIR=data/chroma
CHROMA_COLLECTION_NAME=mindbridge_knowledge
```

当 `KNOWLEDGE_VECTOR_REQUIRED=false` 时，缺少 API key 或 Chroma 不可用不会阻断聊天，系统会回退到本地 BM25 + `hybrid_score` reranker。若交付验收要求必须走 Chroma 向量检索，可设置 `KNOWLEDGE_VECTOR_REQUIRED=true`。

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
AI_PROVIDER=mock python -m app.rag_eval.runner
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

对话通过 `MindBridgeAgentHarness` 组织一次 run。Harness 保留 LangGraph / custom runtime 的安全主流程和转介子图，在外层统一管理：

* 输入脱敏和 session 解析。
* Memory / Supervisor / Knowledge / RiskGuardian / Response Agent 调用。
* 心理报告落库和工具计划生成。
* 学生与助手消息持久化。
* Agent steps、知识召回、风险结果等 trace 数据输出。

因此 HTTP 层只负责认证和 SSE 流式输出，Agent 后处理逻辑集中在 runtime harness 内。

## Engineering Harness

项目提供一键工程 harness，用 mock AI、临时 SQLite、内存短期记忆和本地输出验证核心链路：

* Risk Safety Harness：高风险识别、报告生成、后台元数据不外显、工具队列入队。
* Agent Routing Harness：通过 `MindBridgeAgentHarness` 验证 CHAT / CONSULT / RISK 路由和组件步骤。
* Standard Skills Harness：验证 `skills/*/SKILL.md` 标准 Skill 加载、选择逻辑和交接摘要模板渲染。
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

## MCP 使用边界

默认路径是 `Queue → ToolOrchestrationService`，MCP 不参与内部后台任务。
保留的 `app/mcp_tools/server.py` 是本地 stdio 适配层，依赖限制在 MCP 1.x。
报告工具需要后端子进程提供单报告作用范围；自由 actor 的接手/添加跟进工具拒绝执行，
应使用现有认证 API。这个本地 scope 标记不是远程 MCP 身份认证，不能据此开放外部访问。

`mindbridge_excel_report`、`mindbridge_case_create`、`mindbridge_alert_send`、
`mindbridge_alert_notify` 保留为内部适配能力；`mindbridge_alert_ack` 与
`mindbridge_case_note_add` 在缺少认证主体的协议入口明确拒绝。投递失败报告工具错误。

目前没有真实第二 AI 客户端，跨客户端服务暂不实施。未来需要先完成认证、角色与个案
归属、审计和独立客户端安全验证，再增加受限只读接口与显式授权写操作。

内置标准 Skills 位于 `skills/*/SKILL.md`，运行时由 `MindBridgeSkillRegistry` 加载：

* `supportive_response_baseline`：心理咨询与风险回复的基础共情、边界和学生端表达规则。
* `high_risk_safety_plan`：高风险时引导模型优先完成短期安全计划。
* `anxiety_grounding_support`：焦虑、惊恐、崩溃场景的稳定化和 grounding 指引。
* `sleep_routine_support`：失眠、睡眠节律紊乱场景的安全睡眠建议。
* `academic_stress_planning`：考试、作业、论文、绩点压力的下一步拆解。
* `referral_resource_guidance`：校内心理中心、辅导员、可信任支持人和紧急资源转介。
* `counselor_handoff_summary`：生成给辅导员/管理员看的个案交接摘要模板。



下次启动

1\.启动ollama和docker

2\.打开 Docker Desktop 软件界面。



在左侧菜单栏点击 Containers。



在列表中找到你的项目组（通常以 mindbridge-py 命名，里面包含 app、mysql、redis 三个子项）。



点击该项目组右侧的 “Start” (播放键图标)，等待它们的状态变为 Running / Healthy。
