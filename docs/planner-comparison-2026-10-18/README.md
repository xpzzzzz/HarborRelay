# 规则与单模型规划的配对对照

本文保留规划策略选择阶段的记录，源码摘要对应该阶段版本。后续缓存/向量验证与补丁见
[真实依赖验证](../infrastructure-validation-2026-10-18/README.md)。

当前模拟目录转介默认采用 **`REFERRAL_PLANNER=rules`**。它保留工具反馈和 LangGraph，
根据观察确定性选择下一步，不宣称模型自主规划。**`REFERRAL_PLANNER=model`** 保留为单模型实验选项。
在当前任务、模型和预算下，没有测得用模型替代规则的收益；这个判断不推广到其他任务或模型。

## 对照方法

三种策略共用 ReferralAgent 子图、工具、授权/来源/完成校验和预算：显式规则、现有微调 7B、已有 DeepSeek-R1 1.5B。
没有下载模型；R1 是推理模型，并非本机已经具备通用指令模型的证明。它只作为离线候选，不加入在线模型路由。

每个策略使用相同的固定 TaskSpec ID、约束、资源数据、15 秒预算、8 次规划/6 次工具调用上限。
工具 Fixture 每次重新创建。每个案例保存 TaskSpec 与资源摘要，`verify_pairs` 在输入、覆盖范围或 Fixture 不同时拒绝比较。
模型都使用温度 0.35、512 token 上限和原生 Schema；分别预热，预热不计入场景耗时，依次执行以避免两个模型互相排队。
没有固定 seed，缓存和本机负载仍影响采样，不把小样本延迟差异当作一般性能结论。

|场景|预期|
|---|---|
|available|线下候选可用，核验并准备草稿|
|replan|线下不可用，改查用户未禁止的远程渠道|
|timeout_once|第一次工具调用超时，有限重试后完成|
|missing_time|规则追问，0 次模型调用|
|strict_channel|明确只接受线下，候选不可用时退出，不放松约束|
|remote_only|明确远程，直接查询远程可用候选|
|risk_after|工具观察发出 HIGH 信号，立即中断探索|
|renamed|改名并倒置资源顺序，仍依据观察中的 ID 与来源处理|
|unknown|未知可用性不能当作可用|

“符合预期”包括正确追问、退出和风险中断，不全是完成转介。`risk_after` 是注入的结构化信号，
不是临床风险检出测试。规划器只接收 TaskSpec；任意自然语言日期/偏好理解不在此处验证。

## 三轮完整对照：发现重复检查缺陷前

完整原始结果见 [三轮报告](before-guard-fix/report.md)，应用摘要
`930c25958ce782101fd3bad68e7e10b3794b3245e80ef6310155e64ac8096978`。

|策略|符合预期|模型路径符合预期且无回退|模型请求|回退规划|瞬时故障恢复|P50 / P95（毫秒）|
|---|---:|---:|---:|---:|---:|---:|
|显式规则|27/27|不适用|0|0|3/3|12.90 / 14.45|
|微调 7B|10/27|7/24|64|0|0/3|9813.04 / 10767.56|
|R1 1.5B|7/27|0/24|63|41|0/3|11372.37 / 15060.21|

每个模型有 3 次缺时间规则处理，不计作模型成功。7B 的 64 个 Action 全部合规，却仍不能稳定改查渠道、
遵循严格渠道退出原因或恢复瞬时超时。R1 的 7 次预期结果含规则和回退，没有无回退模型成功；
63 次请求中 22 次 Action 合规、10 次 HTTP 错误、31 次空/无效输出，不能把回退结果归功于模型。
R1 响应中观察到 `done_reason=length` 与空的最终 content；输出预算与思考行为的兼容性仍是限制，不能据此评价它的一般推理能力。

这些时间只测转介子图，不含整轮聊天、RAG、风险/意图模型、持久化或网络 SSE。
规则组的首次图导入/编译也计入样本，均值受该冷启动影响，不能把 P95 当作最大耗时。
工具调用数量较少可能是失败早退，不能单独当作成本优化。

## 实际发现并修复的执行器缺陷

R1 输出会省略工具默认参数，例如 `{ "private": true }` 与显式
`{ "channel": "in_person", "private": true }`。旧重复检查将校验后的当前参数与原始历史参数比较，
可能放过语义相同的重复请求；预算保护仍有效，但重复保护不完整。

现在所有调用签名都基于同一个参数 Schema 校验、补全默认值后的结果。一次重试例外只适用于
**上一条刚超时的同一个调用**，其他资源/参数不能借此获得重复执行机会。没有放宽白名单、隐私、风险或预算。
新增完整子图测试验证省略默认值不能绕过检查，以及同名工具的另一参数超时不能授权重复旧调用。
超时的用户提示也改成仅说明本次未完成，避免模型未重试时宣称已经重试。

修复后的三策略单轮回归见 [修复后报告](after-guard-fix/report.md)，与三轮数据分开保存，不拼接统计。
修复后规则 **9/9**、7B **3/9**（无回退模型路径 2/8）、R1 **2/9**（无回退模型路径 1/8）；
两个模型的瞬时故障恢复均为 0/1，规则为 1/1。相同任务/资源摘要校验通过，两候选都未通过全场景无回退门槛。
应用摘要见 [source-versions.json](source-versions.json)，功能修复差异见 [repeat-guard-fix.diff](repeat-guard-fix.diff)。
最终扩展 lint 发现 runtime 原有未使用的 MessageRole 导入，已删除，差异另记
[final-import-cleanup.diff](final-import-cleanup.diff)；规划器与执行器源文件未再改变。
已验证还原这两处差异能恢复三轮报告的应用摘要；修复前后结果不冒充同一版本测量。

## 实际文件与兼容性

|文件|修改|
|---|---|
|`app/agents/referral.py`|规则/model 两种规划入口，复用已有规则函数；统一执行器；规范化重复签名和准确的重试例外|
|`app/core/config.py`、`.env.example`|增加 `REFERRAL_PLANNER`，默认 rules，其他预算不变|
|`app/agents/runtime.py`|将所选策略传入子图，模型与规则共用 Artifact、Trace、SSE 结果边界|
|`app/api/routes.py`|状态返回 planner，历史 ReferralAgent 名称保持兼容，描述区分规则与实验模型|
|`app/evaluation/ollama_runner.py`|固定 TaskSpec/Fixture 摘要，新增约束/改名场景，支持显式规则评测，思考标记输出不落盘|
|`app/evaluation/planner_comparison.py`|配对离线比较、独立预热、失败索引、JSON/Markdown；候选失败仍非零退出|
|`tests/test_planner_strategies.py`、`test_referral_loop.py`、`test_ollama_probe.py`|规则不调用 AI、共用授权/风险/预算、runtime 策略选择、重复防绕过、配对不一致拒绝及不保存思考内容|
|README、ARCHITECTURE、实施记录|真实默认行为、实验限制与收益门槛|

规则策略记录 `deterministic_policy`；模型调用失败后的规则回退记录 `deterministic_fallback`；
Mock 模型接口策略仍记 `mock_policy`。规则策略不创建 Schema 或调用 AiClient，但仍产生同一种 Action 经执行器校验。
没有用 Mock 冒充规则成本，也没有把回退算作模型独立成功。主流程风险与意图模型仍会调用，0 次只指转介规划阶段。
`REFERRAL_ENABLED=false` 仍能关闭全部模拟转介；安全检查、认证、outbox、API/SSE 事件和数据库结构不变。

最终回归：**101/101** 单测、**6/6** 工程 Harness、Mock 对话 **21/21**、转介故障 **45/45**、
7 项合成安全门槛全通过；RAG 60 条指标保持原值。见 [tests.txt](tests.txt)、[harness.json](harness.json)、
[Mock 报告](mock/report.md)。Mock 报告的历史键 `single_referral_agent` 指组件，当前转介默认策略为 rules，
不表示已经验证模型自治。Ruff F/py311、compileall、pip check 和 diff 检查通过。
修复后真实模型采样版本为 `2da35dcaae19d44d8f7b475f79b5b49eb4f3a4929e226e379990664a045328cb`。
删除未使用导入后重跑完整单测、Harness 与 Mock；最终应用摘要见 [environment.json](environment.json)，
与最新 Mock 报告一致。没有为这次无行为变化的导入清理重跑真实模型性能采样。

模型最终答案与 thinking 字段的协议依据见 [Ollama Thinking](https://docs.ollama.com/capabilities/thinking)
与 [Chat API](https://docs.ollama.com/api/chat)。评测不提取/存储 thinking；若最终 content 混入旧式 think 标记，
省略原始记录且不剥离后伪装成有效 Action。模型原样输出仍接受正常校验，失败明确回退。

## 重现与后续门槛

```powershell
$env:PYTHONIOENCODING = 'utf-8'
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe -m app.harness.runner --suite all --json
.venv/Scripts/python.exe -m app.evaluation.runner --baseline-ref f172dd8 --repeat 3
# 本机已有的两个候选，各自为单规划器实验，不启用线上路由
.venv/Scripts/python.exe -m app.evaluation.planner_comparison --repeat 3
# 离线规则基线，不访问 Ollama
.venv/Scripts/python.exe -m app.evaluation.planner_comparison --models --repeat 3 --output-dir target/rules-only
```

使用 Python 3.11 和 `requirements-test.txt`。runner 只接受 loopback 已有 completion 模型；不自动下载。
完整对照使用修复前版本；当前命令复现修复后代码，不保证逐 token 或时间相同。候选没有达到所有预期或触发回退时，
进程返回非零并保存失败，不能改为成功退出来掩盖问题。单轮回归可加 `--repeat 1`，用于验证修复，没有三轮统计的样本量。

后续优先补真实 Redis/Chroma 的隔离兼容验证。若业务出现确实需要不确定工具结果、多约束权衡和澄清的任务，
应先定义验收与规则基线，再评估单模型收益。目录匹配可以继续使用规则，不为展示自治而虚构复杂度。
P3 缺少独立上下文/职责拆分必要性及收益证据，P5 缺少真实第二客户端，继续暂缓。
本轮未推送、未修改生产、未操作真实敏感数据或发送真实通知。
