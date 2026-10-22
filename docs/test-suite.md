# 自动交易测试总集

## 1. 统一入口

`scripts/check.ps1` 是本项目唯一完整质量门。它先验证 Python、Node.js 和 Windows 部署脚本，
然后依次执行前端依赖锁安装、Lint、格式、TypeScript、覆盖率测试、生产构建，以及 Python
仓库策略、编译、Ruff、格式、Mypy、Pytest 和 sdist/wheel 构建。

支持的工具链为：

- Python 3.11 或 3.12；
- Node.js 20.19.0 及以上，CI 固定 Node.js 22；
- pnpm 11.19.0，按 `packageManager` 和锁文件安装。

```powershell
.\scripts\check.ps1 -Python .\.venv\Scripts\python.exe
```

本机默认 Node.js 不满足版本要求时，显式指定 Node.js 安装目录：

```powershell
.\scripts\check.ps1 `
  -Python .\.venv-dev\Scripts\python.exe `
  -NodeBin C:\path\to\node
```

工具链版本不满足要求时必须在运行测试前失败，不能把运行时不兼容误报成产品测试失败。

## 2. 测试分层

| 层级 | 目录 | 责任 |
|---|---|---|
| 单元测试 | `tests/unit` | 值对象、状态机、策略、协议算法、端口和适配器的确定性边界 |
| 契约测试 | `tests/contract` | JSON Schema、示例、固定哈希、HMAC向量及跨项目协议语义 |
| 集成测试 | `tests/integration` | SQLite/WAL、线程、HTTP、进程、打包资源和故障恢复的真实组合 |
| 前端测试 | `frontend/operator-console/src` | 路由、API、控制授权、状态展示和浏览器侧交互 |

单元和契约测试不访问 QMT、外部网络或系统墙上时钟。需要时间推进、网络故障、Broker回调、
磁盘异常或锁竞争时，使用可控 Clock、Mock Gateway、Mock Central API、临时数据库和真实子进程。

## 3. TST-001 必要覆盖矩阵

| 验收能力 | 权威测试集合 | 必须证明的边界 |
|---|---|---|
| Runtime状态机 | `test_runtime_state_machine.py`、`test_runtime_faults.py` | 启动、恢复、权限优先级、非法迁移、队列溢出、HALT和终态 |
| 请求/响应签名 | `test_protocol_signing.py`、`test_hmac_vectors.py` | 原始Body、时间窗边界、Nonce、篡改、未知Key、响应请求绑定和失败关闭 |
| 金额与数量精度 | `test_domain_decimal_values.py`、`test_domain_quantity.py` | Decimal精度、范围、非有限值、整数/Bool隔离、lot和上下溢出 |
| 交易日期与时钟 | `test_domain_trading_time.py`、`test_runtime_scheduler.py` | 严格日期、上海时区、单调时钟、盘中窗口、迟到和休市NOOP事件边界 |
| 计划风险 | `test_plan_risk.py`、`test_plan_risk_event.py` | 账户/日期/暴露/标的数/参与率/策略版本及拒绝证据 |
| Windows Hard Safety | `test_hard_safety.py`、`test_hard_safety_guard.py` | 本地上限不可放宽、买卖分层、活动单预占、重复ID和所有数值边界 |
| 报单前最终门 | `test_hard_safety_order_gate.py` | 意图提交后事实变化必须再次拦截，Broker调用次数保持为零 |
| 持久化与恢复 | `test_sqlite_journal.py`、`test_sqlite_wal_recovery.py` | 单Writer、事务原子性、迁移、崩溃、备份、锁竞争和恢复快照 |
| 同步与幂等 | `test_outbox_worker.py`、`test_durable_outbox_delivery.py` | 逐条ACK、重试、死信、重启补传、响应验签和高水位 |
| 本地危险控制 | `test_local_control_authorization.py`、`test_local_control_sqlite.py` | 短会话、CSRF、影响预览、逐字确认、一次性授权和双审计 |

新增交易能力必须先归入上述矩阵或新增明确的能力行，并同时具有正常、边界、拒绝和恢复用例。
仅验证“正常成功”不满足测试总集完成标准。

## 4. 覆盖率和通过标准

- Python 使用语句与分支覆盖，最低总覆盖率 90%；
- 前端使用语句、分支、函数和行覆盖率，阈值由 Vitest 配置强制；
- Ruff、Mypy、TypeScript、ESLint、Prettier 任一失败均阻断；
- wheel 和 sdist 必须在同一质量门构建成功；
- 失败测试不得通过重跑、睡眠、扩大超时或删除断言隐藏；
- QMT现场行为不属于单元测试结论，继续由 QMT-001 至 QMT-010 和 TST-006 验收。

## 5. 可追溯性

每个工作项证据至少记录：执行命令、测试数量、覆盖率、构建产物、解释器版本、已知限制和下一个
依赖。TST-001 的总体验收记录位于
`evidence/TST-001/2026-09-04-unit-test-suite-verification.md`。
