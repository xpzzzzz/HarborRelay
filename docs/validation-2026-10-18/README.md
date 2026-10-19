# 2026-10-18：真实模型与隔离 MySQL 验证

本文保留第一轮采样与当时建议。后续已加入显式规则策略并改为默认，最新选择依据见
[规则/模型配对对照](../planner-comparison-2026-10-18/README.md)。下述源代码摘要对应本轮采样时版本。

结论：MySQL 的 8 项隔离探针通过；本机微调 7B 模型的转介规划尚未通过默认 15 秒预算下的功能验证。
Schema 能约束格式与证据范围，不能补足规划、改查渠道与故障恢复能力。没有增加独立 Agent、模型路由或外部 MCP 客户端。

## 实际修改与取舍

|文件|变化与边界|
|---|---|
|`app/services/ai.py`|Ollama 非流式请求可携带原生 `format` Schema；有 provider usage 时记录实际 token/加载时间。普通回复及 OpenAI-compatible 请求保留原行为|
|`app/agents/referral.py`|压缩 Action 提示，禁止嵌套工具名；按已观察候选、可用性和草稿生成解码 Schema。执行器仍独立检查白名单、隐私、时间、风险、来源与完成证据|
|`app/core/config.py`、`.env.example`|新增 `REFERRAL_ACTION_FORMAT=schema/prompt`，原生格式为默认；保留 15 秒预算和原工具/步数上限|
|`app/evaluation/ollama_runner.py`|只访问 loopback 已安装模型；合成 Fixture，无数据库/预约/通知。分别统计 JSON、Action、解码 Schema、任务结果、规则处理、回退、实际注入故障是否恢复|
|`app/evaluation/mysql_runner.py`|只创建本地、自有标签、tmpfs 的新 MySQL 容器；随机测试库与合成账户，退出时校验所有权后清理|
|`app/evaluation/runner.py`|明确 Mock 报告的测试范围，与真实依赖探针区分|
|`tests/test_structured_actions.py`、`test_mysql_probe_safety.py`、`test_ollama_probe.py`|验证原生参数/usage、异常回退、执行层仍拒绝越权、本地测试隔离和不把规则/回退计作模型成功|
|`requirements-test.txt`|显式声明评测用 `jsonschema`，不把它引入在线规划校验链路|
|`README.md`、`ARCHITECTURE.md`、实施记录/审查文档|更新真实验证范围、失败证据、回退方式与后续门槛|

原生 `format` 接口依据 [Ollama Structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
和 [Chat API](https://docs.ollama.com/api/chat)。解码约束不承担授权职责；即使模型或服务忽略它，执行器仍会拒绝未经授权的工具操作。

## 真实 Ollama 结果

本机 Ollama **0.40.5**，模型 `mindbridge-qwen2.5-7b-ft:latest`，Q4_K_M；模型 digest：
`6f5a0ea99a06070f1623185d302f3d9a0c350c1662557513c00281e1e8e7c88c`。
温度 0.35，最大输出 512 token；没有下载、修改模型或使用第二模型。正式采样使用已加载的模型，冷加载探索另记。

|评测|符合预期|模型依赖路径符合预期，且无回退|收到的 Action 合规|模型请求错误|故障恢复|
|---|---:|---:|---:|---:|---:|
|原生 Schema，15 秒，4 场景 × 3|3/12|0/9|24/24（26 次请求）|2 次 ReadTimeout|0/3|
|原生 Schema，120 秒，8 场景 × 1|6/8|4/6|16/16|0|0/1|
|仅 prompt，同一当前提示与 Fixture，120 秒，3 场景 × 1|1/3|1/3|11/11|0|0/1|

“符合预期”包括正确追问、安全退出和风险中断，不全是完成转介。15 秒组通过的 3 次均为缺时间规则追问，0 次模型推理；
120 秒原生组有 2 次为缺时间/风险规则处理，不计作模型成功。仅 prompt 组有 1 个 Action 含额外的 `stop_reason:null`，
Pydantic Action 接受该兼容字段，但严格解码 Schema 判为不合规（10/11）；这是两项不同指标。

默认预算组的失败：

- `available`：2/3 次已完成 3 个工具调用，但最终确认请求读取超时；另 1 次重复查目录被执行器停止。
- `replan`：3/3 次在观察到线下候选 unavailable 后，误选 `availability_unknown`，未改查允许的远程渠道。
- `timeout_once`：3/3 次在第一次工具超时后直接退出；合成故障确实触发，不能把未重试算恢复成功。

120 秒原生诊断组中，`available` 约 **16.9 秒**完成；`replan` 和 `timeout_once` 依然失败。
仅 prompt 组中，`available` 约 **21.6 秒**完成；另两项因重复动作失败。探索性单例也曾成功完成，约 26.7 秒，
不能据此覆盖重复采样中的失败。初始旧提示曾返回错误的嵌套工具参数，见 `exploratory/`。

当前 prompt 本身也能产生合规 Action，因此这些小样本**不能把格式改善或延迟差异单独归因于原生 Schema**，
更不能声称它提升了语义规划能力。模式之间未固定随机 seed，TaskSpec ID 不同，缓存/本机负载也会影响耗时。
原生约束的保留理由是可执行协议与证据参数范围；实际授权仍由确定性执行层负责。

完整记录见 [默认预算报告](ollama-schema-15/report.md)、[诊断报告](ollama-schema-120/report.md)、
[prompt 兼容报告](ollama-prompt-120/report.md)，每份 JSON 保存合成输入、原始 Action、步骤、模式、usage 与模型 digest。
失败索引见 [failures.json](failures.json)。`unauthorized_successes=0` 只反映这些指定用例，不能当作全面安全证明。
超时请求没有 provider usage，token 总量只覆盖收到的响应。HTTP 超时与节点预算检查也不是严格的墙钟取消机制。

## MySQL 8/8 项验证

MySQL **8.0.46**，隔离级别 **REPEATABLE-READ**，缓存镜像 ID：
`sha256:7dcddc01f13bab2f15cde676d44d01f61fc9f99fe7785e86196dfc07d358ae2b`。
此次 run ID `7fc75080f69e`，报告 `cleanup_succeeded=true`，临时容器已删除，未复用其他项目容器。

|检查|实际证据|
|---|---|
|报告与 outbox 原子回滚|报告及任务均不存在|
|同报告并发入队|8 线程 × 3 轮，24 次请求始终仅 3 个任务|
|并发条件认领|3 个 dispatcher，共 3 次提交，重复 claim=0|
|受控业务执行|3 个 SUCCESS、3 个 SUCCESS 审计；台账/个案/log 告警各 1|
|进程退出后 outbox 与恢复|子进程提交后 `os._exit(73)`；父进程验证记录仍在，过期任务恢复并成功|
|Excel 写后、DB 提交前进程退出|子进程 `os._exit(74)`；DB 无成功标记，重试后同报告仅 1 行|
|策略拒绝|LOW 报告被注入 ALERT_SEND 后进入 DEAD；0 告警且有 BLOCKED 审计|
|死依赖收敛|依赖死亡的任务进入 DEAD 与死信|

原始记录：[mysql-report.json](mysql-report.json)。并发认领用记录 executor 隔离 DB claim，业务执行单独串行验证；
未验证多进程同时写 Excel、SMTP exactly-once、租约心跳、操作系统断电、MySQL 宕机/重启恢复或分布式水平扩容。
本次容器数据在 tmpfs，进程退出测试仅针对应用子进程，不是持久磁盘灾难恢复测试。没有真实邮件投递。

## 回归与版本

- **91/91** 单测，**6/6** 工程 Harness，见 [tests.txt](tests.txt)、[harness.json](harness.json)。
- 最新 Mock 对照：旧快照 9/21，当前 21/21；转介故障场景 45/45；7 项合成安全门槛全通过，见 [Mock 报告](mock/report.md)。
- RAG 60 条指标不变：Recall/HitRate 0.966667、Precision 0.6375、MRR 0.911111、NDCG 0.903041；向量服务未实测。
- pip 依赖检查、compileall、变更 Python 模块的 Ruff F（py311）与 diff 空白检查通过。
- 本轮采样时应用源代码 SHA-256：`b37379eb920d010fbc6d0a994f109a8be29edc2ab829f2dc417ded921db2b7d7`，与本目录正式 Ollama、Mock 报告一致。
  MySQL 报告采集于同日更早，之后未改动被测队列/业务执行代码；探针仅作格式整理。环境见 [environment.json](environment.json)。

## 重现命令与后续建议

在仓库根目录、Python 3.11 与 `requirements-test.txt` 环境中运行：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe -m app.harness.runner --suite all --json
.venv/Scripts/python.exe -m app.evaluation.runner --baseline-ref f172dd8 --repeat 3
.venv/Scripts/python.exe -m app.evaluation.ollama_runner --budget-seconds 15 --repeat 3 --output-dir target/real-validation/schema-15-repeat3 --skip-warmup
.venv/Scripts/python.exe -m app.evaluation.ollama_runner --budget-seconds 120 --cases available replan missing_time timeout_once empty privacy unknown risk_before --output-dir target/real-validation/schema-120 --skip-warmup
.venv/Scripts/python.exe -m app.evaluation.ollama_runner --format prompt --budget-seconds 120 --cases available replan timeout_once --output-dir target/real-validation/prompt-120 --skip-warmup
.venv/Scripts/python.exe -m app.evaluation.mysql_runner
```

`--skip-warmup` 对应此次正式采样前已加载模型的情况；冷启动时可去掉该参数，runner 会单独记录预热且不计入场景耗时。
没有已有模型或缓存 MySQL 镜像时会失败，不自动下载或连接现有数据库。Ollama 功能失败时 runner 返回非零，仍保存 JSON/Markdown；
不要把它改成成功退出以掩盖模型问题。随机生成不同 TaskSpec ID、温度采样和机器负载意味着复现步骤，不保证逐 token 或耗时相同。

当前微调模型用于真实交互时建议 `REFERRAL_ENABLED=false`，待规划评测通过后再启用实验转介。
下一步先做规则基线与单个通用指令模型的同任务对照，区分模型能力、提示与执行成本；没有收益时，这种简单目录任务可以继续用规则。
若需要展示自主规划，应选择确实需要在观察后权衡多个候选和约束的任务，并先定义验收集，避免把规则步骤交给 LLM 后称为“更智能”。
现有失败不说明独立上下文或职责拆分不可维护，故未满足 P3；没有真实第二客户端，故未满足 P5。
Redis/Chroma、真实 SMTP 沙箱仍未验证。本轮未推送、未修改生产环境、未操作真实敏感数据。
