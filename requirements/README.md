# 依赖锁管理

- `runtime.lock`固定Windows交易节点运行依赖。
- `dev.lock`在运行依赖基础上固定测试、静态检查和构建工具。
- 两份锁文件都包含包哈希，安装时必须使用`--require-hashes`。
- `pyproject.toml`是直接依赖的唯一声明源，锁文件由`pip-compile`生成，不手工维护传递依赖。

重新生成锁文件：

```powershell
python -m piptools compile pyproject.toml --strip-extras --generate-hashes `
  --index-url https://pypi.org/simple --output-file requirements/runtime.lock
python -m piptools compile pyproject.toml --extra dev --strip-extras --generate-hashes `
  --allow-unsafe --index-url https://pypi.org/simple --output-file requirements/dev.lock
```

当前锁文件在独立的CPython 3.11.16 x64开发环境生成，APSW固定3.53.4.0，
实际SQLite为3.53.4。使用本项目隔离环境进行安装与质量验证，量化后台依赖保持原样。
生产发布前仍须在GOV-001目标机冻结Python补丁、券商SDK及安装包hash并复验。
