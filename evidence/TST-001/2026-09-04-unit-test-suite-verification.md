# EV-TST-001 自动交易单元测试总集验证

## 结论

TST-001 已完成。项目已经形成一个可重复执行、失败关闭的统一质量门，覆盖 Runtime 状态机、
双向签名、金额数量精度、交易日期与时钟、计划风险、Windows Hard Safety、报单前最终门、
SQLite/WAL 恢复、Outbox 幂等和本地危险控制。测试、静态检查、前端构建、Python构建及全新环境
安装全部通过。

## 测试总集

测试分为 `tests/unit`、`tests/contract`、`tests/integration` 和前端 Vitest 四层。能力到测试文件的
稳定映射已写入 `docs/test-suite.md`；新增交易能力必须同时提供正常、边界、拒绝和恢复用例。

`scripts/check.ps1` 统一执行：

1. Python 3.11/3.12 与 Node.js 20.19.0以上版本门；
2. Windows任务部署脚本真实冒烟验证；
3. pnpm锁文件安装、ESLint、Prettier、TypeScript、Vitest覆盖率及Vite生产构建；
4. secret仓库策略、compileall、Ruff、Mypy、Pytest；
5. 隔离构建sdist和wheel。

开发机默认Node.js 18.20.2会在测试开始前得到明确拒绝；使用工作区Node.js 24.19.0后完整质量门
通过。CI继续固定Node.js 22和Python 3.11。新增政策测试防止package、质量脚本和CI版本要求
发生静默漂移。

## 验证结果

### Python

- Python：3.12.14 x64；
- 收集并通过：1281项；
- 总覆盖率：90.62%，满足90%语句与分支综合门；
- Ruff：通过；
- Ruff格式：178个文件通过；
- Mypy：178个源文件无问题；
- compileall：通过；
- secret仓库策略：通过。

### 前端

- Vitest：7个测试文件、24项测试全部通过；
- 语句覆盖率85.95%、分支80%、函数83.33%、行89.71%；
- ESLint、Prettier、TypeScript全部通过；
- Vite生产构建转换3280个模块，静态资源写入wheel包含路径。

### 构建与全新安装

- 构建 `auto_trading_system-0.1.0.dev28.tar.gz`；
- 构建 `auto_trading_system-0.1.0.dev28-py3-none-any.whl`；
- 在全新虚拟环境按 `runtime.lock` 的哈希安装全部运行依赖；
- 安装wheel后 `auto-trading-runtime --version` 和 `auto-trading-watchdog --version` 均返回
  `0.1.0.dev28`；
- 安装包中的本地操作台 `static/index.html` 和Windows任务部署脚本均可读取。

## 关键文件

- `scripts/check.ps1`
- `docs/test-suite.md`
- `tests/unit/test_quality_gate_policy.py`
- `pyproject.toml`
- `.github/workflows/quality.yml`
- `frontend/operator-console/package.json`

## 验收判断

TST-001 完成标准中的状态机、签名、精度、交易日、时间窗和风控边界均有直接行为测试及统一
质量门证据。QMT客户端自身行为不在单元测试中伪造为现场结论，继续由QMT-001至QMT-010和
TST-006验收。下一项进入TST-002双项目契约测试。
