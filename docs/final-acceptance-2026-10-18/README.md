# P0 / P1 / P2 / P4 最终验收

2026-10-18，在本地 `main` / `f172dd833377ceda268d4a481f558be32dff9942` 上完成本轮工程收尾。
验收时修改尚未提交或推送；该状态描述验收时点，发布状态以 Git 历史为准。
P3 和 P5 按用户要求排除；没有新建独立 Agent、在线模型路由或远程 MCP。
本文是最终范围说明；此前各阶段报告保留当时的代码摘要和失败记录。

**已交付的工程范围全部通过指定验收。** 模型规划的可靠性门槛仍未通过，已据配对实验将默认
转介收敛为规则反馈，保留模型实验开关。这是对原计划的有证据调整，不能宣传为可靠自主规划，
也不能把下面的 Mock 成功率解释为真实模型、临床识别率或生产 SLA。

## 原计划逐项闭环

|计划项|最终交付与取舍|验证入口/证据|
|---|---|---|
|代码审查与基线|以实际调用链识别共享可变状态、风险绕过、流后调度、身份信任与 Trace 泄露；保留参考代码缺陷作为反例|[审查报告](../ARCHITECTURE_REVIEW.md)、[原基线](../evaluation/baseline-metrics.json)|
|P0-A 风险前置|所有意图先预检/独立评估，分类不能降级；规则命中跳过可选依赖；补缓存漏写后的历史风险用例|`test_safety_boundaries.py`、`test_memory_coherence.py`、Mock 7 项硬门槛|
|P0-B 可靠调度|既有 ToolJob 作 outbox，与报告同事务、先于 SSE；并发条件 claim、过期恢复、依赖死亡与死信；保留至少一次语义|MySQL 9/9、断连/生成失败用例、两个真实应用子进程退出窗口|
|P0-C 认证/治理|认证主体、角色/归属、默认 worker 策略审计；Argon2id 与成功登录旧哈希升级；未认证 MCP 写入拒绝|`test_api_authorization.py`、`test_case_workflow.py`、`test_tool_governance.py`、MySQL 拒绝审计|
|P0-D 最终回复门禁|高风险固定模板，最终文本审核后发送；拒绝/异常不回放候选；普通模型流仍是真实 stream|`test_safety_boundaries.py`、SSE outputMode 与未批准文本门槛|
|P1 状态/产物/Trace|强类型 State 与有消费者的 Artifact，节点返回更新；无 ORM 进入图状态；custom/LangGraph 共用契约；Trace 限元数据/引用|`test_state_contracts.py`、6/6 Harness、既有 API 回归|
|P2 反馈循环|单转介子图与 3 个本地模拟工具；Observation 决定重试、改查、追问、结束；授权/隐私/时间/证据/预算独立校验|45/45 场景、`test_referral_loop.py`、`test_structured_actions.py`|
|P2 模型兼容调整|Ollama Action Schema + 运行时校验；实际模型重复/耗时无收益，默认 rules；模型 planner 显式实验启用|[真实模型实验](../validation-2026-10-18/README.md)、[规则/7B/R1 配对对照](../planner-comparison-2026-10-18/README.md)|
|P4 合成评测|相同输入/Fixture/轮次，旧 Workflow vs 当前默认；保留失败索引、工具成本、恢复、耗时、SSE 首输出与 RAG 指标|[最终 Mock 报告](mock/report.md)、[逐例失败索引](mock/failures.json)|
|P4 真实依赖与故障|隔离 MySQL、Redis、Chroma、本机已有 nomic；缓存丢写/逆序提交、忙写快照恢复、SMTP loopback 沙箱|[MySQL](mysql/report.json)、[Redis/Chroma](infrastructure/probe.json)、[单测](tests.txt)|
|P3 / P5|本轮明确不实施；既无独立 Agent 消融收益，也无真实第二客户端需求|不新增技术组件来填补标签|

## 本次收尾实际修改

|文件|变更|
|---|---|
|`app/services/memory.py`|缓存携带 messageId；完整持久消息窗口验证；缺失/重复/乱序/旧格式回读数据库，正文脱敏；主键排序|
|`app/agents/runtime.py`|必要历史接入窗口校验；缓存构造/读取失败仍用数据库；高风险直接命中保持跳过可选读取|
|`app/agents/harness.py`|保存消息返回持久实体，将提交后的 ID 传给可选缓存；outbox 顺序保留|
|`app/harness/runner.py`|内存测试适配器支持同一 ID 契约，保持 Mock 与实际读取语义一致|
|`app/services/vector_snapshots.py`（新增）|逻辑导出/校验/恢复、共享锁、原子发布、受限保留、失败恢复标记、仅恢复到新目录的 CLI|
|`app/services/vector_store.py`|写入/查询/快照按持久目录加锁，替换复制目录快照，暴露新目录恢复入口；空集合同步也导出删除后的状态|
|`app/services/knowledge.py`|来源替换与重建在同一应用锁内完成，避免快照落在删除/重建的中间状态|
|`app/services/tools.py`|检查 smtplib 返回的部分拒收结果，记录失败并进入已有重试/审计路径|
|`app/evaluation/infrastructure_probe.py`|真实 Redis 丢写/旧回填/重复；两 store、300 条、跨分页忙写快照，新进程恢复和损坏/覆盖拒绝|
|`app/evaluation/mysql_runner.py`|新增两个事务按反向提交顺序写消息的校验；真实 MySQL 验收由 8 项扩至 9 项|
|`app/evaluation/smtp_sandbox.py`（新增）|仅 loopback、`.invalid` 地址、只存内存不转发的 SMTP 故障接收器|
|`tests/test_memory_coherence.py`（新增，8 方法）|漏中间写、旧回填、乱序/重复/旧格式、同时间戳、读写故障、角色、两 Runtime 历史风险升级|
|`tests/test_vector_snapshots.py`（新增，6 方法）|摘要篡改、导出失败不发布、恢复中断标记、目录边界、共享锁和保留兼容|
|`tests/test_smtp_sandbox.py`（新增，5 方法）|真实 TCP 成功去重、全/部分/DATA 拒收、TLS/AUTH 拒绝、确认丢失可重复、治理队列重试|
|`README.md`、`ARCHITECTURE.md`、`docs/IMPLEMENTATION_LOG.md`|最终流程、兼容/回滚、精确测试范围与复现入口；历史报告增加后续阶段链接|

## 最终实测结果

|检查|结果|原始证据|
|---|---|---|
|Python 3.11 单测|**126/126**|[tests.txt](tests.txt)|
|Harness|**6/6**|[harness.json](harness.json)|
|Mock 对话，7 场景 × 3|**21/21**；旧版 9/21|[report.json](mock/report.json)|
|转介/故障，15 场景 × 3|**45/45**，完成证据正确率 1.0；指定瞬时故障恢复率 1.0|[referral-scenarios.json](mock/referral-scenarios.json)|
|指定安全门槛|**7/7**；当前未授权成功数 0|[report.json](mock/report.json)|
|原 RAG 60 条|Recall/HitRate 0.966667、Precision 0.6375、MRR 0.911111、NDCG 0.903041，与旧基线一致|[harness.json](harness.json)|
|真实 Redis/Chroma|**16/16**，run `31fe0161f52e`，cleanup=true|[runner.json](infrastructure/runner.json)、[probe.json](infrastructure/probe.json)|
|真实 MySQL 8.0.46|**9/9**，run `9527cfc9ebf1`，REPEATABLE-READ，cleanup=true|[report.json](mysql/report.json)|
|静态/依赖检查|本次修改的 Python 文件 Ruff F/py311、compileall、uv pip check、diff 检查通过|[quality-checks.json](quality-checks.json)|

Mock 首次输出中位数旧版 59.22ms、当前 50.17ms；整轮中位数旧版 71.93ms、当前 55.94ms。
这些值在进程内生成器测量，包含模板/已准备的转介结果，非公网延迟、非模型实时首 token，
也不是具有统计显著性的性能结论。原报告键 `single_referral_agent` 是历史组件名，当前默认
planner 为 rules，不代表实际模型推理。

最终应用源码 SHA-256：`89c647acb9e68bda4e310d88d934d4c6036c6dde082005c30c6a7dde9d863644`，
与容器探针、Mock 报告相同。逐文件摘要、运行环境和阶段结果见 [acceptance.json](acceptance.json)。
单测首次实现曾发现缓存构造异常不再被捕获的回归，修正后全量通过；未删除或放宽安全断言。
全仓 Ruff 另发现未修改的旧 `app/services/model_assets.py:20` 存在 F541，HEAD 已包含该问题；
本轮不因格式问题更改无关模块，也不把全仓 lint 宣称为通过。

## 三个关键取舍

**缓存不是安全事实来源。** 仅比最后一个 ID 不足：中间丢写后新消息成功、较小 ID 更晚提交、
缓存旧窗口回填都可能保留相同最大 ID。完整窗口比较能覆盖这些情况。命中时仍付出一个有界
数据库 ID 查询，换取正文缓存加速与可解释的一致性，避免增加持久版本列和分布式事务。
这是追加式消息与当前数据库事务可见性的保证，不提供并发请求的串行聊天锁，也不覆盖未来
同 ID 正文编辑；若增加编辑能力，必须扩展版本协议。

**逻辑集合快照与业务备份分开。** 不复制活跃 SQLite/HNSW 文件，不依赖 Chroma 私有 flush API。
导出/写入在同一进程共享锁内完成；300 条分页用例证明并发 writer 等待，恢复的是 generation-0，
活动集合随后已变为 generation-1。恢复验证整个记录集、数量、维度、摘要和身份，只创建新目录，
不覆盖活动路径。SHA-256 用于损坏检测，不代替可信备份来源或访问控制。
旧目录快照不自动迁移/删除，新恢复入口遇到旧格式会失败。失败导出保留 `.partial-*`，失败导入
保留不可用标记；管理员应离线核查这些目录，程序不会自动切换到未完成索引。

**SMTP 不虚构 exactly-once。** Python smtplib 在部分收件人被拒时正常返回拒收字典，必须显式检查；
本轮修复了将其报告为整体成功的问题。沙箱实际接收 DATA 后断开、重试再接收的用例显示 2 封，
证明确认丢失仍可能重复。该重复是公开的至少一次限制，不用“去重测试通过”掩盖崩溃窗口。
接口依据：[Python smtplib](https://docs.python.org/3/library/smtplib.html)。

嵌入式 Chroma 的边界依据：[Chroma 系统约束](https://cookbook.chromadb.dev/core/system_constraints/)、
[实际使用版本的 get/upsert 接口](https://github.com/chroma-core/chroma/blob/0.5.23/chromadb/api/models/Collection.py)。
本地模式仅支持一个 writer 进程；本轮的锁只协调应用服务线程，不保护多个进程或直接绕开封装的写入。
保留这一部署边界比加入无法证明安全的伪分布式锁更合理；需要扩容时应单独评估服务化存储。

## 复现

实际 Git 根目录 `mindbridge-py`，Python 3.11 测试环境：

```powershell
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-test.txt
$env:PYTHONIOENCODING = 'utf-8'
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe -m app.harness.runner --suite all --json
.venv/Scripts/python.exe -m app.evaluation.runner --baseline-ref f172dd8 --repeat 3
.venv/Scripts/python.exe -m app.evaluation.mysql_runner
.venv/Scripts/python.exe -m app.evaluation.infrastructure_runner
```

前 3 个验证入口无需现有外部服务；SMTP 单测启动自己的短命 loopback TCP sink。
MySQL 运行器需要已缓存 `mysql:8.0`，不会拉镜像或连接业务实例；基础设施运行器需要已缓存
`redis:7.2-alpine` 与 `mindbridge-py-app:latest`，并在 tmpfs 临时 venv 安装当前完整 requirements。
本机 Ollama 需已有 nomic 模型；不会下载或修改模型。实际 Chroma 环境为 Linux Python 3.12.13，
NumPy 2.5.1、onnxruntime 1.28.0，完整版本见 [pip-freeze](infrastructure/pip-freeze.txt)。
隔离方式与镜像要求详见 [前阶段复现](../infrastructure-validation-2026-10-18/README.md)。

逻辑快照恢复示例（需完整 requirements 及匹配的 provider/model/collection 配置）：

```powershell
.venv/Scripts/python.exe -m app.services.vector_snapshots --snapshot "data/chroma-snapshots/logical-<id>" --destination data/chroma-restored-new
```

将示例路径换成实际已完成的导出。目标目录必须不存在；命令不会修改 CHROMA_PERSIST_DIR，
不会覆盖旧索引。先离线检查记录/检索，再由部署流程决定是否切换配置。本轮仅在自建 tmpfs 数据上
调用恢复；没有访问现有索引。

## 完成范围与保留限制

本轮计划内的 P0/P1/P2/P4 工程交付和上述依赖收尾已闭环，不再把缓存校验、忙写快照或 SMTP
沙箱列为待做事项。以下是明确边界，不表示已经验证：

- 当前模型 planner 无可靠性收益，默认规则；没有泛化到真实校园资源，不执行预约/交接。
- 普通低风险模型流没有整段发送前审核；高风险用审核模板。安全评测是合成硬门槛，不是临床结论。
- Redis 窗口以事务可见的追加式消息为准；DB 必要持久化失败时仍无法保证 outbox 提交。
- Chroma 快照是单集合/单写进程的逻辑导出，不是 MySQL/Chroma 原子联合备份、断电/磁盘损坏
  恢复、多进程/分布式共享写入或 Windows 原生 Chroma 兼容性证明。
- 自动快照为全集合导出，尚无大规模性能评测；失败 partial 需离线检查，不自动删除。
- SMTP 沙箱不验证公网邮件到达、真实证书、成功 TLS 握手或真实账号；至少一次重试可能重复。
- 多进程共享 Excel/SMTP 的分布式副作用、机器断电和长期容量不属于本轮生产验证。

没有新表或 DDL 迁移，Redis Key/合法旧字段、API/SSE 事件名与 RAG 排序算法保留。
关闭实验转介可用 `REFERRAL_ENABLED=false`；默认 `rules` 即可靠的演示基线。回滚时保留
强制风险、身份/治理、安全门禁与 Argon2 verifier；新 Redis ID 字段对旧读取代码是附加字段。
旧/新快照不可互相冒充，回滚备份代码前保留导出文件并明确格式。

所有数据为合成；告警只进入本地内存 sink 或 log，临时容器均已清理，未推送、未改生产、
未操作真实敏感数据或向真实收件人发送邮件。
