# HarborRelay 架构升级代码审查（实施前）

审查日期：2026-10-16（Asia/Shanghai）。实际计划文件位于工作区上一级
`HarborRelay_Architecture_Upgrade_Plan.md`；Git 根目录是 `mindbridge-py/`。
本地 `main`，HEAD `f172dd833377ceda268d4a481f558be32dff9942`，审查开始时工作区干净。
未拉取或推送远端，未连接业务数据库、Redis、SMTP 或真实校园资源。

本文保留实施前判断。2026-10-18 的真实 Ollama 与临时独立 MySQL 验证结果见
[追加验证](validation-2026-10-18/README.md)，不覆盖原始基线记录。
P0/P1/P2/P4 的最终闭环、后续缓存/快照/SMTP 修复和剩余边界见
[最终验收](final-acceptance-2026-10-18/README.md)。

## 代码证据与必须修复的问题

|优先级|当前代码证据（实施前）|影响与结论|
|---|---|---|
|P0|`agents/runtime.py:137` supervisor 将 CHAT 的 risk_assessed 置真；`:172` companion 设置 LOW|独立风险检查可被分类绕过；风险评估应前置，风险只能升级|
|P0|`services/chat.py:25` 生成器在完整流之后 dispatch_tools|断连/生成异常时可能完全没有告警任务；报告和任务须原子提交，并发生在第一个 SSE 事件之前|
|P0|`services/tool_queue.py:181` 直接执行 service；tool_governance 仅被 import|允许/拒绝策略没有进入默认执行路径；补审计和拒绝执行|
|P0|`tool_queue.py:134` 非原子 claim，重启恢复所有 RUNNING；依赖失败永久等待|并发重复执行、活任务被重领、依赖死亡不收敛；补条件更新与过期恢复|
|P0|`services/tools.py:82/99/113` 接受自由 actor；`mcp_tools/server.py:64/78` 原样转发|API 从认证用户取 username，但 service/MCP 未认证，不能形成统一权限边界|
|P0|`core/security.py:15` 普通 SHA-256，无慢哈希|独立兼容补丁：新密码 Argon2id，旧散列仅在成功登录后升级，无批量账户迁移|
|P0|实际自然语言由 `ChatService.ai.stream` 在 runtime 后生成|候选 prompt 审查无法审核最终文本；高风险改用受控模板并审核最终文本，常规分支保留真实模型流|
|P1|`langgraph_runtime.py:13` State 仅含可变 AgentContext；节点原地修改|显式状态更新，移出 ORM 对象；继续用原有操作函数和结果边界，避免建立新事件框架|
|P1|`services/trace.py:40` 默认保存完整原文、prompt、证据正文|改用脱敏元数据和引用；旧数据库字段保留，避免 DDL；业务聊天记录与诊断 trace 区分|
|P2|`KnowledgeAgent` 检索一次；`AiClient.complete` 仅返回文本，无工具协议|需要有界 JSON Action 规划、真实 observation 回传、参数/白名单验证及可测试回退|
|P5|`agents/harness.py:100` 默认入队，worker 本地执行；MCP 仅关闭队列后使用|保留内部 service 路径；不为证明 MCP 强制绕行；未认证个案写工具默认拒绝|

API/SSE：保留 `/api/chat/stream` 和 meta/token/done/error 事件；允许 meta 增加输出模式说明。
普通 CHAT 仍无需生成心理报告，但必须经过风险评估。高风险不等待 RAG。
管理员原有全局权限保留；增加的辅导员范围仅允许自身/未分配个案，不扩大普通用户权限。

RAG：`services/knowledge.py` 已有混合召回、本地重排、上下文扩展；`vector_store.py`
已有 provider/model 维度隔离和故障兜底；保留，不改检索算法。原评测中 Recall@K
实际按“存在相关结果”计为 1，应保留指标定义并说明，不能当作完整相关文档召回率。

数据库：create_all 不会迁移既有表；本轮不新增/删除表或字段。队列幂等使用报告行锁
和既有 report_id/kind 查询；MySQL 上的竞争行为仍需独立集成验证。SMTP 为至少一次
投递语义，崩溃窗口内可能重复，不宣称 exactly-once。

## 计划逐项裁决

|项|裁决|实施方案/依赖|
|---|---|---|
|第0阶段|接受|先读调用链、跑基线，本文在实现前建立|
|P0-A|接受|规则预检→必要记忆→风险评估→意图路由；异常保守回退，禁止降级|
|P0-B|接受，调整|复用 ToolJob 作 outbox；与报告同事务，不新建消息总线；默认 worker 执行|
|P0-C|接受，拆补丁|service 检查持久化认证用户；MCP 无认证写操作拒绝；Argon2 单独兼容补丁|
|P0-D|接受，收敛|高风险固定安全文本且发送前审核；不增加不能证明有效的模型相互投票|
|P1-1/2|接受|强类型最小 State/Artifact；显式更新和共享执行函数；没有 checkpoint 时不宣称耐久恢复|
|P1-3/4|接受，调整|只创建有消费者的产物；保留 AgentRunResult、现有 DTO 与 UI 字段|
|P1-5/6|接受|复用 trace JSON 字段保存扩展元数据；验证 custom/LangGraph 同一安全契约|
|P2|接受|单 Referral Agent，三个本地只读/草稿工具，LangGraph 子图；模拟目录和来源明确|
|P3|暂不实施|无单 Agent 上下文失败及双 Agent 消融收益证据；记录模型调用元数据即可|
|P4|接受，限定结论|合成数据+Mock 对照，记录实际延迟/调用数/故障恢复；真实模型泛化能力不在已验证范围|
|P5|暂不实施|没有真实第二 AI 客户端或授权需求；只加固已有适配器并纠正 README|

参考仓库按提交 `810d19bef54c5baf575470259d5355cffb58212e` 只读检查
events/registry/coordinator/autonomous/event_driven_runtime；不复制任务认领竞争。
其 `SafetyAgent._review_response` 拼接 prompt messages，未审核自然语言结果；
`EventDrivenAgentRuntimeService._to_result` 无 accepted 时回退生成 prompt，不能作为安全门禁参考。
来源：[参考代码](https://github.com/h-yyang/MindBridge/tree/810d19bef54c5baf575470259d5355cffb58212e/app/agents)、
[LangGraph 状态更新](https://docs.langchain.com/oss/python/langgraph/graph-api)、
[密码存储建议](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)。

## 已运行基线

- 系统 Python 3.13 缺依赖：最初 6 个测试模块导入失败，未计作通过。
- 建立项目 `.venv`，Python 3.11.16；安装 requirements 中除 chromadb 外的依赖。
  Chroma 真实向量库没有在此环境验证；embedding HTTP 接口使用 mocks 测试。
- `.venv/Scripts/python.exe -m unittest discover -s tests -q`：23/23 通过。
- `.venv/Scripts/python.exe -m app.harness.runner --suite all --json`：4/6 通过。
  失败为 Skills/API 两套检查的 Windows 路径分隔符（期待 `/SKILL.md`，返回 `\\SKILL.md`），
  属已有跨平台缺陷，修正输出为 POSIX 风格，保留检查强度。
- RAG 60 条：Recall/HitRate 0.966667，Precision 0.6375，MRR 0.911111，NDCG 0.903041。
- 断连探针：拿到高风险 meta 后关闭生成器，新增 ToolJob=0，复现告警调度缺口。
- 单次 Mock 同进程首 token：chat 18.31ms、consult 48.71ms、risk 40.96ms、referral 42.12ms。
  仅作本机观察值，不是网络端到端/真实模型性能；原转介没有结构化资源证据或工具循环。
  故障恢复和越权成功率无旧基线测量，记为未测，不填造 0。
- 原始输出：`docs/evaluation/baseline-harness.json`、`baseline-metrics.json`。

## 开工结论与范围

P0 可以安全开始。未发现需要操作真实数据、不可逆迁移或外部集成的阻塞。
先修改 runtime/langgraph_runtime、harness/chat、tool_queue/governance/tools、security/routes、
MCP 边界与相关测试；再改结构化状态/trace；最后添加转介子图、评测脚本及架构文档。
各阶段记录实际结果；未验证的真实模型、MySQL 并发、Redis 和 Chroma 如实列为限制。
回滚以代码补丁和可选转介功能开关为界；不提供可关闭强制风险/授权检查的开关。
