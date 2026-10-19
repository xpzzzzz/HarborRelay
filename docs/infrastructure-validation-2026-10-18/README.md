# Redis / Chroma 隔离兼容验证

本文保留本阶段 14 项检查及当时的源码摘要。后续已完成缓存窗口一致性、忙写逻辑快照与
SMTP loopback 沙箱，最新 16 项基础设施与 9 项 MySQL 验收见
[最终验收](../final-acceptance-2026-10-18/README.md)；下文待验证项为历史范围，不代表当前遗留任务。

本阶段在真实 Redis、Chroma 0.5.23 和本机 Ollama embedding 上完成 **14/14** 项指定检查，
并修复缓存恢复、缓存内容校验、追加事务、向量必需模式与状态/数值校验问题。
这是依赖兼容和故障边界的验证，不是生产容量、临床风险检出或 RAG 泛化质量证明。

## 环境与隔离

- Redis **7.2.14**，redis-py **5.2.1**；Chroma **0.5.23**，Python **3.12.13 / Linux**。
- NumPy **2.5.1**，onnxruntime **1.28.0**，PostHog **5.4.0**，MCP **1.30.0**，Argon2 **23.1.0**。
- 实际模型 `nomic-embed-text:latest`，768 维，digest
  `0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f`，没有下载或修改模型。
- 新建两个带本次所有权标签的容器。Redis 只绑定其命名空间的 loopback，不发布主机端口；
  测试进程共享该命名空间。Chroma、SQLite 与虚拟环境在临时 tmpfs；只读挂载当前 app/skills/requirements，
  不挂载 `.env`、业务数据目录或现有数据库卷。仅证据目录可写回工作区。
- embedding 故障网关将正常请求转发到本机 Ollama，实际发出 **10** 次合成文本 embedding 请求；
  故障模式在同一 HTTP 边界注入 503、读取超时及错误向量响应，未用 Mock 向量冒充真实索引。
- cached image、完整依赖冻结、pip check 与清理结果均归档。最终 run ID `0771dd08c5ec`，
  `cleanup_succeeded=true`，两个临时容器均已删除，没有复用或停止其他项目实例。

基础镜像缓存早于 Argon2 补丁，首次运行在导入阶段停止，未计为兼容性通过。
之后在容器内创建继承系统包的临时虚拟环境，安装当前 requirements，保留完整认证逻辑；
首次 tmpfs 不允许动态库映射的问题也在测试运行器中修正，没有更改生产环境或基础镜像。
这些环境失败记录见 [setup-attempts](setup-attempts/README.md)。

## 基线与修复结果

预热后的修复前基线 **7/13** 通过，[基线原始记录](before/probe.json)保留实际失败。
最终原有 13 项全部通过，另增加错误 HTTP embedding 响应检查，共 **14/14**；
高风险用例还扩展为 Redis 与 embedding 同时故障。不能把不同覆盖范围当作严格的同样本性能对比。

|检查|实际验证|
|---|---|
|Redis 存取/限长/脱敏|7 次顺序追加和 32 次并发追加后最多 4 条；手机号/邮箱不以原值进入缓存；替换与清空有效|
|Redis 追加事务|服务端 MONITOR 从原来的 RPUSH/LTRIM/EXPIRE 分散命令变为 MULTI→RPUSH→LTRIM→EXPIRE→EXEC|
|畸形缓存与权限角色|坏 JSON、非对象、错误内容形状与 system 角色跳过，保留合法的 user/assistant 数据|
|启动失败后恢复|CLIENT PAUSE 造成初始化 PING 超时；同一 store 在服务恢复、短暂冷却后能重新存取|
|真实过期|TTL=1 秒后数据消失|
|缓存未命中|从独立 SQLite 的持久消息取历史并脱敏，重填真实 Redis；代码原有 source 标签 mysql_seeded 保留|
|真实向量/混合检索|3 条合成文档真实生成 768 维向量并写 Chroma，精确文本查询命中对应来源|
|503/超时/恢复|可选模式使用 BM25；必需模式抛错；同一服务恢复后状态和查询恢复|
|必需且禁用向量|明确抛 VectorStoreUnavailable，不静默回退|
|非有限浮点|拒绝 provider 与缓存中的 NaN/Inf，不改变已知维度|
|错误 HTTP 向量|数量、维度、NaN 响应在写入前拒绝，已有 3 条索引保持有效|
|进程与快照|新进程重开推断维度；静止目录快照恢复可查；写后 os._exit(75) 的应用子进程数据可重开；快照保留 2 份|
|模型隔离与同步|另一模型集合独立为空；删除来源后重建清理旧向量；同来源重入库不增加条数|
|高风险故障隔离|真实 Redis 暂停且 embedding 网关离线，确定性高风险模板路径不调用向量服务，3 个 outbox 任务在 SSE meta 前可见|

最终 [runner.json](final/runner.json)、[probe.json](final/probe.json)、[stdout](final/stdout.txt)、
[stderr](final/stderr.txt)保存检查细节。高风险场景使用 Mock 对话模型与明确规则信号，不能据此宣称真实模型风险识别率。

## 实际代码修改

|文件|修改与兼容边界|
|---|---|
|`app/services/memory.py`|RPUSH/裁剪/TTL 同一 Redis 事务；replace 显式事务并正确释放 pipeline；初始化失败保留可重连 client，用现有 socket timeout 作单 store 短暂冷却；只接受会话数据角色和字符串内容|
|`app/services/vector_store.py`|必需向量不能通过 enabled=false 绕过；拒绝非有限 embedding；保留现有 provider/model 集合隔离和存储格式|
|`app/services/knowledge.py`|健康检查恢复后同步刷新 available/error；拒绝非有限缓存向量，序列化不输出 NaN/Inf|
|`requirements.txt`|为 Chroma 0.5.23 固定兼容的 PostHog 5.4.0；匿名遥测仍关闭，不影响工具执行审计|
|`app/evaluation/infrastructure_runner.py`|复用本地 Docker 上下文检查；拥有者标签校验、临时容器/虚拟环境、完整依赖检查与清理|
|`app/evaluation/infrastructure_probe.py`|真实服务、可控 HTTP 故障、新进程重开/退出、隔离 SQLite/outbox 与来源断言|
|`tests/test_infrastructure_contracts.py`|6 项无需容器的回归方法，覆盖恢复冷却、缓存角色/形状、严格向量模式、状态恢复、非有限值与隔离拒绝|

生产代码差异见 [changes.diff](changes.diff)。Redis Key 前缀、消息 JSON 的既有字段、API/SSE 事件、
DB 表结构、RAG 排序算法和默认可选降级配置均保留。变化有两点需要明确：历史缓存中的 system/tool 角色
不再作为会话消息信任；`KNOWLEDGE_VECTOR_ENABLED=false` 与 `KNOWLEDGE_VECTOR_REQUIRED=true`
的冲突配置现在明确失败。正常默认 required=false 不受该配置变化影响。

原生接口依据：[redis-py 事务](https://redis.io/docs/latest/develop/clients/redis-py/transpipe/)、
[Chroma 0.5.23 客户端](https://github.com/chroma-core/chroma/blob/0.5.23/chromadb/api/client.py)。
PostHog 兼容性依据：[Chroma 的旧 capture 调用](https://github.com/chroma-core/chroma/blob/0.5.23/chromadb/telemetry/product/posthog.py)、
[PostHog 5.4.0 签名](https://github.com/PostHog/posthog-python/blob/v5.4.0/posthog/__init__.py)。
初次实际导入的新版 API 曾产生参数数量错误；固定版本后正常创建/查询不再出现该错误。

## 可复现命令

仓库根目录，主测试环境为 Python 3.11 与 `requirements-test.txt`：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe -m app.harness.runner --suite all --json
.venv/Scripts/python.exe -m app.evaluation.runner --baseline-ref f172dd8 --repeat 3
.venv/Scripts/python.exe -m app.evaluation.infrastructure_runner
```

需要本地 Docker、已缓存 `redis:7.2-alpine` 与含 Python/pip 的 `mindbridge-py-app:latest`，
本机 Ollama 在 11434 提供已安装的 nomic 模型。运行器不会拉取镜像，但临时环境补齐 requirements
时需要包下载访问。Linux 测试进程使用 Docker Desktop 的 host.docker.internal 访问本机模型；
其他平台的模型网络映射未在此验证。若没有应用镜像，可以先用当前 Dockerfile 在本地构建专用标签，
再用 `--app-image <标签>` 传入；不需要启动 compose 业务服务。

结果写入 `target/infrastructure-validation/report.json` 与独立 run ID 子目录。
任意检查、依赖安装或清理失败都返回非零，保留失败证据。embedding 冷加载单独预热并记录：
早期未预热的 30 秒请求曾超时，不能据最终预热测试宣称冷启动满足生产耗时要求。
没有更改生产 embedding timeout 或模型预算。

## 回归与尚未验证的范围

最终 **107/107** 单测、**6/6** Harness、Mock 对话 **21/21**、转介故障 **45/45** 和 7 项合成安全门槛通过；
RAG 60 条原指标不变。见 [tests.txt](tests.txt)、[harness.json](harness.json)、[Mock 报告](mock/report.md)。
真实向量检查是 3 条合成数据的精确文本匹配，不是这 60 条数据的真实 embedding 质量复评。
Windows 主测试环境未安装 Chroma；本阶段真实 Chroma 结论限于记录的 Linux/Python/依赖组合。

完整依赖见 [pip-freeze](final/pip-freeze.txt) 与 [pip-check](final/pip-check.txt)。Ruff F/py311、compileall、
主环境 pip check、diff 空白检查通过。最终源码摘要与容器探针、最新 Mock 报告一致，见 [environment.json](environment.json)。

Redis 仍是可丢失的尽力缓存：本阶段证明缓存未命中/不可用时读持久历史及恢复，不证明跨写入失败、并发会话的
强一致版本校验，也不保证重复消息去重。后续应单独设计缓存新鲜度/写后失效和相应故障验收，不能把缓存当作权限或风险事实来源。
Redis 事务保证命令执行期间不被其他客户端交错，不保证运行时命令错误回滚或持久化灾难恢复。

Chroma `snapshot()` 仍复制文件目录，本次只验证无并发写时可恢复。
未验证忙写一致快照、磁盘损坏、主机断电、Redis/Chroma 进程崩溃或分布式多写者；tmpfs 在容器删除后消失，
应用子进程退出用例不是这些灾难恢复的替代。高风险路径仍依赖必要数据库能提交。
SMTP 真实投递/沙箱未验证；P3/P5 门槛未改变。未推送、未修改生产配置、未读取真实敏感数据或发送真实通知。
