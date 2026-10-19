# 分阶段实施记录

环境与基线见 [实施前审查](ARCHITECTURE_REVIEW.md)。P0—P4 的首轮记录来自 2026-10-16，
使用合成账户、隔离 SQLite、Mock AI、本地 Fixture 和 log 告警。2026-10-18 的本机 Ollama
与隔离 MySQL 验证另记在本文末尾；历史结论保留原测试范围。没有推送或修改生产环境。

## P0：安全与可靠性

- 修改：`agents/runtime.py`、`langgraph_runtime.py` 强制规则预检/风险评估前置，
  高风险跳过可选 RAG，普通回复不能重置风险；`services/assessment.py` 校验分数并保守回退。
- 修改：`agents/harness.py` 在报告事务中写 ToolJob；`services/chat.py` 发送 meta 前已完成提交，
  高风险只发送审核通过的最终模板，低风险仍用真实 provider stream。
- 修改：`services/tool_queue.py`、`tools.py`、`tool_governance.py` 的执行路径：报告锁定、
  条件 claim、过期恢复、依赖失败、策略审计、Excel 文件崩溃窗口去重；保持内部 service 路径。
- 修改：`core/security.py`、`api/routes.py`、`services/report.py`、`tools.py` 实施认证主体/
  角色/个案归属；管理员保留全局权限。现有 MCP 的自由 actor 操作拒绝执行，报告适配器
  仅允许 backend scoped stdio，不宣称提供外部认证。`mcp_client.py` 传递单报告作用范围。
- 独立兼容补丁：Argon2id 新密码与成功登录后旧 SHA-256 升级，字段长度无需迁移；
  requirements 增加 argon2-cffi，将原来无上界 MCP 限制为 1.x（本地安装原解析成 2.x）。
- 兼容修复：`services/skills.py` 的公开路径统一为 POSIX 风格；更新原 harness 的安全
  断言：所有意图都必须检查风险，高风险不要求 RAG，保留咨询检索要求。
- 测试：`python -m unittest discover -s tests -q`，38/38；
  `python -m app.harness.runner --suite all --json`，6/6；RAG 60 条指标与基线一致。
  原始记录见 `docs/evaluation/p0-harness.json`。
- 风险/限制：MySQL 行锁并发、真实 SMTP/Redis/Chroma 未验证；SMTP 为至少一次投递。
  `TOOL_QUEUE_ENABLED=false` 会暂停默认 worker，outbox 仍可靠保留，恢复 worker 后再处理。
  不把 log 告警描述为已发送邮件。需保障 worker 持续运行及监控积压。
- 回滚：代码补丁可回退；无 DDL。Argon2 散列生成后，旧版仅支持 SHA-256 的认证代码
  不能直接回滚，必须保留兼容 verifier。强制安全门禁不提供关闭开关。

## P1：结构化 State / Artifact / Trace

- 新增 `agents/contracts.py`：TurnState、TaskSpec、ToolObservation、SafetyReview 和
  按 kind 校验 payload 的 Artifact；来源引用必填。
- `agents/runtime.py` 复用原业务函数，在节点内部创建隔离快照，返回显式字段更新；
  `langgraph_runtime.py` 和 custom runtime 共用转换/路由/结果逻辑。图中只保留用户/会话 ID，
  不保存 ORM 对象或数据库 Session；没有启用持久化 checkpoint，不宣称跨进程恢复 Agent。
- `services/trace.py` 复用现有 JSON 字段记录产物引用、最终回复摘要哈希、真正针对最终
  文本的审核结果；不再将完整原文、记忆和 prompt 复制到新 trace。历史 trace 未改动。
- `services/ai.py` 记录 provider/model/latency/error/字符估算 token；估算明确标记，
  不冒充 provider 实际 usage。`services/chat.py` 记录输出模式，常规流不声称整段发送前审核。
- 测试：42/42 单测，含输入状态不变、Schema 拒绝、两 runtime 安全/Artifact 一致性、
  trace 不复制完整敏感对话；现有 6/6 harness 通过。证据 `p1-harness.json`。
- 回归风险：状态内仍有本轮必要上下文（仅运行内存）；业务聊天/报告依然按原策略持久化，
  本轮改变的是诊断 trace 的复制范围。没有表结构变更，回退可选择 custom runtime。

## P2：单 Referral Agent 与真实工具反馈循环

- 新增 `agents/referral.py` 的 PLAN→TOOL_CALL→OBSERVE 子图；每次模型收到更新后的
  observation。JSON Action 校验后才执行白名单工具，限制步数、次数、耗时和重复调用。
- 新增 `services/referral_tools.py` 与 `fixtures/support_resources.json`：仅本地模拟查询、
  可用性 Fixture 和脱敏草稿；没有 DB、网络、预约或通知依赖。隐私/明确渠道/时间限制
  在执行边界复查，模型不能自行放松；未知状态不作成功处理。
- 修改 `runtime/langgraph_runtime/harness/chat` 接入转介结果与 Artifact；缺时间可追问，
  下一轮补时间可继续匹配；高风险在探索前或工具观察后升级，使用 P0 安全输出路径。
- 修改 `services/ai.py` 接收本轮剩余 HTTP 超时并记录调用；Mock 是显式的 observation
  驱动策略。无效 JSON/模型异常进入标注的 deterministic_fallback，不宣称真实模型已验证。
- `api/routes.py` 在 SSE 头发出前拒绝跨用户 session；`tests/test_api_authorization.py`
  通过真实 FastAPI/Basic Auth 请求验证角色、归属及 actor 推导。
- 测试：67/67 单测，6/6 现有 harness；同一目录候选不可用时 5 次工具调用并改查远程，
  候选可用时 3 次调用直接准备草稿。缺时间 0 次；持续超时 2 次后退出；同时测试无结果、
  隐私冲突、未知状态、假成功、重复动作、风险中断、未授权副作用和 custom 子图兜底。
- 回归/限制：本地 Fixture 的计划能力不能代表 Ollama 模型可靠性。耗时边界在各节点前后
  检查，阻塞的同步调用无法强制撤销；当前工具全部为有界本地函数，未来外部适配器需独立
  超时和取消设计。time window 提取仅支持明确的星期/上午下午晚上，不假装理解任意日期。
- 回滚：`REFERRAL_ENABLED=false` 回到原咨询回复/RAG 路径，强制风险检查仍有效。
  没有新表、外部副作用或额外 Agent。P3/P5 未满足收益/真实第二客户端门槛，暂不实施。

## P4：固定数据集、对照评测与最终复核

- 新增 `app/evaluation/{runner,probe,scenarios}.py` 和 `cases.json`。runner 按指定本地
  commit 用 git archive 构造隔离快照；旧版和当前版本使用同一合成输入、Mock 配置、
  LangGraph 与运行次数，无远端操作。Windows 下在临时目录清理前显式释放 SQLite engine。
- 对照：7 类对话各运行 3 次；旧版 9/21，当前 21/21。差异涉及新增转介能力和已修复
  的明确风险规则，不能解读为真实模型智能提升。旧版没有转介工具循环，故障对照标记
  not_supported，不填造旧版工具恢复数据。
- 当前 15 类转介场景 × 3 = 45/45：证据正确、瞬时超时恢复各为 1.0；6 次刻意注入的
  越权决策全部拒绝，越权执行成功 0。persistent timeout 等场景按正确安全退出算通过，
  不把安全退出误记为成功转介。
- 7 项合成安全硬门槛当前全部通过；旧版 7 项失败。分别覆盖风险前置、明确风险检出、
  断连 outbox、流失败 outbox、未审核高风险输出、伪造 actor、队列策略拒绝。
  这是指定回归用例的结果，不是临床风险漏检率或全面安全证明。
- RAG 60 条：Recall/HitRate 0.966667、Precision 0.6375、MRR 0.911111、NDCG 0.903041，
  与旧代码快照一致；同进程总耗时与首 token 的均值/P50/P95 原值保存在 report.json。
  首 token 含冷启动/图执行/提交，未测网络或真实模型耗时，不据此宣称生产性能提升。
- 最终复核补丁：`runtime/harness` 延迟创建可选 RAG/缓存，高风险绕过 Skill/prompt
  组装，outbox 提交后才尝试缓存写入；Redis 不可用时普通路径仍执行风险评估。
  `ai.py` 规则归一化 Unicode/零宽空格，保留确定性升级；`chat/trace` 记录审核不可用、
  流失败与断连。队列过期恢复改为条件更新，避免覆盖已完成任务；MCP 投递失败抛工具错误。
- 新增最终回归：可选组件全故障、高风险审核超时、Excel 文件/DB 崩溃窗口、依赖死亡、
  活跃任务不被恢复、DEAD 不被重建，以及常规 SSE 在生成结束前增量返回 provider chunk。
- 最终实际命令：
  `python -m unittest discover -s tests -v`：78/78；
  `python -m app.harness.runner --suite all --json`：6/6；
  `python -m app.evaluation.runner --baseline-ref f172dd8 --repeat 3`：passed=true，失败索引为空；
  `uv pip check`：依赖兼容；`python -m compileall -q app tests`：通过；
  新增模块的 Ruff F 检查（target py311）和 `git diff --check`：通过。
- 证据：`docs/evaluation/{report.json,report.md,failures.json,current.json,baseline.json,
  referral-scenarios.json,final-tests.txt,final-harness.json,environment.txt}`。
  report.json 保存数据集及应用源代码 SHA-256；归档时已校验源代码摘要一致。
- 文档：新增根目录 `ARCHITECTURE.md`，更新 README、`.env.example` 与
  `requirements-test.txt`，保留生产 requirements 的 Chroma 依赖。P3/P5 明确暂不实施。
- 剩余工作：真实 Ollama JSON 决策、MySQL 并发/崩溃恢复、Redis/Chroma、SMTP 沙箱验证；
  多进程共享副作用需先设计分布式幂等与租约，当前只验证本地执行。未操作真实数据、生产环境，
  未推送远程，未执行 schema 迁移。所有修改保留在工作区供审阅。

## P4 追加：2026-10-18 真实 Ollama 与独立 MySQL

- 实际修改 `services/ai.py`、`agents/referral.py`、`core/config.py`、`.env.example`：
  Ollama 可传原生 Action Schema，按已观察候选/可用性/草稿约束解码；返回实际 usage 时
  单独标记 provider 计数。仅 prompt 的兼容开关保留，执行器安全校验不变，默认预算仍为 15 秒。
- 新增 `evaluation/ollama_runner.py`、`mysql_runner.py` 和三组测试文件；`requirements-test.txt`
  声明 jsonschema，更新 `evaluation/runner.py` 的范围描述。只使用 loopback 已安装模型和
  自建隔离 MySQL，未连接其他项目的数据库；本轮告警仍为 log。
- Ollama 0.40.5，现有微调 7B Q4_K_M：15 秒 × 4 场景 × 3 轮，符合预期 3/12，均为规则追问；
  模型规划任务完成 0/9。26 次请求中收到 24 个合规 Action、2 次 ReadTimeout，回退 2/26；
  瞬时工具故障实际触发 3 次，恢复 0 次。没有把无推理用例或回退记成模型成功。
- 120 秒诊断组 6/8 符合预期，其中模型依赖路径 4/6；正常可用路径约 16.9 秒完成，
  改查渠道/瞬时故障恢复仍失败。同一当前提示的 prompt 组 1/3 达到预期，11/11 Action
  合规，不能把格式或耗时差异单独归因于原生 Schema。完整失败与原始 Action 均归档。
- MySQL 8.0.46 / REPEATABLE-READ：8/8 通过，覆盖 8 线程同报告 24 次入队仅 3 个任务、
  3 个 dispatcher 无重复 claim、业务执行审计、两个真实应用子进程退出窗口、策略拒绝与
  死依赖收敛。临时 tmpfs 容器清理成功。未验证 DB 宕机、机器断电或分布式副作用。
- 回归：91/91 单测、6/6 Harness；同旧提交重跑的 Mock 对照当前 21/21，故障场景 45/45，
  7 项安全门槛全通过，RAG 60 条指标不变。pip check、compileall、Ruff F/py311、diff 检查通过。
- 已更新 README/架构文档，证据见 [验证记录](validation-2026-10-18/README.md)。
  新报告记录当前应用摘要，未覆盖 2026-10-16 的历史 Mock 证据。
- 判断：真实模型尚未验证可用，建议该模型下关闭实验转介；先对比规则与单个通用指令模型，
  无收益则保留规则，不把简单目录流程强行交给 LLM。现有失败不足以证明独立上下文/职责
  拆分必要，P3 暂缓；P5 无真实第二客户端，暂缓。Redis/Chroma、SMTP 沙箱仍待验证。
  未推送、未操作生产或真实敏感数据，修改保留在工作区。

## P4 继续：规则基线、配对对照与默认策略收敛

- 修改 `agents/referral.py`、`core/config.py`、`.env.example`、`agents/runtime.py`：
  显式 rules/model 规划入口，默认 `REFERRAL_PLANNER=rules`。直接复用已有规则策略，
  不调用模型，也不通过 Mock 冒充规则成本；两策略共用 LangGraph、风险、授权和完成校验。
  `api/routes.py` 状态增加 planner，保留 API 的旧组件名；Trace 区分主规则、模型回退与 Mock。
- 新增 `evaluation/planner_comparison.py`，扩展 `ollama_runner.py`：固定 TaskSpec ID，
  每次克隆 Fixture、记录/核对输入摘要、单独预热、保存候选失败。只测试本机已有 7B 与
  R1，未下载模型或加入在线路由；未提取私有 thinking，带旧式标记的原始输出不落盘。
- 九场景各三轮的完整对照：规则 27/27、7B 10/27、R1 7/27；模型无回退路径分别 7/24、
  0/24。两个模型瞬时故障恢复均 0/3，规则 3/3。R1 结果含回退，不冒充模型能力。
- 对照发现旧重复检查将标准化当前参数与原始历史参数比较，省略默认值可漏检重复。
  已统一参数校验后的调用签名，重试例外必须匹配刚超时的同一个调用；预算与授权不放宽。
  新增默认参数绕过和另一参数超时借用重试资格的回归测试。超时提示不再声称一定已重试。
- 修复后三策略各一轮：规则 9/9、7B 3/9、R1 2/9；模型恢复均 0/1，规则 1/1。
  三轮与单轮按应用摘要分开归档，保留修复差异，并验证旧代码摘要可还原。
- 最终回归：101/101 单测、6/6 Harness、Mock 21/21、故障 45/45、7 项安全门槛通过，
  RAG 60 条不变；Ruff F/py311、compileall、pip check、diff 检查通过。
- README/ARCHITECTURE 已按真实默认行为更新，证据见
  [配对对照](planner-comparison-2026-10-18/README.md)。当前简单目录任务没有 LLM 收益证据，
  保留规则是有数据支持的选择；模型规划显式实验开启，不扩展独立 Agent 或 MCP。
  Redis/Chroma 隔离验证仍待完成。没有推送、生产/真实敏感数据操作或真实通知。

## P4 继续：真实 Redis / Chroma 与故障恢复边界

- 新增 `evaluation/infrastructure_runner.py`、`infrastructure_probe.py`，复用本地 Docker
  校验，创建拥有者标签明确的 Redis/测试容器。只读挂载源码/技能/requirements，在 tmpfs
  中创建数据和临时 venv，补齐完整认证与当前依赖；没有复用实际业务实例或数据卷。
- 环境探索的导入失败、noexec 动态库问题、初始探针参数错误和 embedding 冷加载超时单独
  记录，不计为正式基线。预热后基线 7/13 通过，定位 6 个实际边界缺陷；修复后原有检查
  均通过，增加错误 HTTP embedding 响应，并扩展高风险双依赖故障，最终 14/14。
- 修改 `services/memory.py`：追加/替换的事务边界，畸形条目/角色白名单，初始化失败不
  永久关闭客户端，同 store 短暂重试冷却；现有 key 与合法消息格式保留。真实 MONITOR
  看到 MULTI→RPUSH→LTRIM→EXPIRE→EXEC；32 次并发追加、真实 TTL 与数据库回填通过。
- 修改 `services/knowledge.py`、`vector_store.py`：status 恢复同步、必需模式不能静默禁用，
  provider/cache 的非有限浮点与 JSON 序列化校验。`requirements.txt` 为 Chroma 0.5.23
  固定兼容旧接口的 PostHog 5.4.0，匿名遥测仍关闭，未改工具审计。
- 实测 Redis 7.2.14、Chroma 0.5.23、Linux Python 3.12.13、NumPy 2.5.1；10 次真实本机
  nomic embedding 请求，768 维，3 条合成数据。新进程重开、静止快照恢复、写后应用
  子进程 os._exit(75) 重开及模型集合隔离通过；不是忙写/断电或 Redis/Chroma 宕机验证。
- 新增 `tests/test_infrastructure_contracts.py`。最终 107/107 单测、6/6 Harness、Mock
  21/21、故障 45/45、7 项安全门槛通过，RAG 60 条原指标不变；Ruff F/py311、compileall、
  主环境和临时 Linux 环境的 pip check、diff 检查通过。
- 新报告与最终应用摘要一致，基线/探索/最终结果分开归档，见
  [依赖验证](infrastructure-validation-2026-10-18/README.md)。README/ARCHITECTURE 已更新。
  缓存新鲜度、写入失败后的版本一致性、忙写快照与真实 SMTP 沙箱仍待验证；P3/P5 暂缓。
  临时容器全部清理，未推送、未修改生产、未操作真实敏感数据或发送真实通知。

## 最终收尾：缓存窗口、忙写快照与 SMTP 沙箱（P3/P5 排除）

- `services/memory.py` 增加提交后消息 ID 与完整窗口校验；`agents/runtime.py` 必要上下文
  先读有界 DB ID，匹配才信任缓存正文。漏中间写、重复、乱序、旧回填和旧格式均回读数据库。
  `agents/harness.py` 传持久 ID，`harness/runner.py` 同步适配；不增加表或版本协议框架。
- 新增 `services/vector_snapshots.py`，`vector_store.py` 改为 JSONL + SHA-256 清单的逻辑
  导出与新目录恢复；共享目录锁保护同进程写入/快照，`knowledge.py` 把来源替换/重建也
  纳入锁。失败导出不发布，失败恢复带不可用标记；旧文件快照不删除，不自动切换活动索引。
- `services/tools.py` 修复部分收件人拒收被误报成功；新增 `evaluation/smtp_sandbox.py`
  与真实 loopback TCP 用例，覆盖成功去重、全/部分/DATA 拒绝、TLS/AUTH 拒绝、确认丢失
  可重复及已有队列的失败/成功审计。没有真实邮件发送，不改变 TLS 默认配置。
- 新增 `test_memory_coherence.py`（8）、`test_vector_snapshots.py`（6）、
  `test_smtp_sandbox.py`（5）。初次实现发现缓存构造异常处理回归，修复后最终 **126/126**
  单测通过；**6/6** Harness、Mock **21/21**、故障 **45/45**、7 项安全门槛通过，RAG 60
  条指标不变。本次修改的 Python 文件 Ruff F/py311、compileall、uv pip check、diff 检查通过。
- 真实 Redis/Chroma **16/16**（run `31fe0161f52e`），两 store 实例、300 条跨分页向量
  在并发写期间导出、新进程恢复完整单一 generation；真实 Redis 超时漏写/旧回填被检测。
  MySQL **9/9**（run `9527cfc9ebf1`），新增较小 ID 较晚提交不逃过窗口校验的实际事务用例。
  两组临时容器均清理成功。应用摘要与最新 Mock/容器报告一致。
- 已更新 README/ARCHITECTURE，并以 [最终验收](final-acceptance-2026-10-18/README.md)
  逐项闭环原计划。P2 模型质量门槛仍不通过，默认规则是根据既有对照的明确调整；P3/P5
  不实施。缓存/忙写快照/SMTP 沙箱不再列为待做，分布式/断电/真实邮件/临床能力保留边界。
  未提交或推送、未修改生产、未操作真实敏感数据，所有改动留在工作区。
