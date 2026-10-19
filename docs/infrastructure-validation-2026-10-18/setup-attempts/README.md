# 探针开发与环境探索记录

以下尝试不计作正式的修复前/后通过率；均保留清理结果。

- [missing-argon2](missing-argon2/runner.json)：缓存镜像缺少 Argon2，导入失败，尚未执行兼容检查。 清理成功：True。
- [noexec-venv](noexec-venv/runner.json)：临时 venv 的动态库不能在 noexec tmpfs 映射，探针未进入功能检查。 清理成功：True。
- [cold-embedding](cold-embedding/runner.json)：探针 MONITOR 的 PING 参数写法错误，以及未预热 embedding 超时，导致后续部分检查缺少索引；随后修正探针并重新采集正式基线。 清理成功：True。
