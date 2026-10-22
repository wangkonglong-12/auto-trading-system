# TEST-RT-001 Runtime故障注入验收证据

## 验收结论

TEST-RT-001完成。Runtime已建立统一失败关闭故障策略，覆盖Journal低磁盘/不可写、
Persistence Writer队列饱和/停滞/失败、Runtime组件崩溃和本地控制审计不明。存储和关键
运行故障进入HALT，普通队列事件丢失进入PAUSE_BUY，关键事件丢失进入HALT；任何故障恢复
都不会自动授予AUTO。

本项同时修复了“订单意图等待Writer期间Runtime已HALT，Writer恢复后仍可能调用Broker”的
竞态。订单持久化门现在于意图写入前及Broker调用前分别读取实时权限；第二次检查失败时保留
已持久化意图并强制进入对账，Broker调用次数为零。

## 故障矩阵

| 场景 | 注入边界 | 验收结果 |
|---|---|---|
| Writer当前提交阻塞 | 可控Journal驱动真实专用Writer线程 | `accepted-completed=1`且`pending=0`仍识别停滞；HALT后释放阻塞不报单 |
| SQLite低磁盘阈值 | 真实SQLite/WAL健康探针 | 产生`JOURNAL_DISK_PRESSURE`并HALT；不写新意图，不调用Broker |
| Writer队列饱和 | 容量1，当前请求阻塞且后续请求排队 | 第三个请求立即返回`PERSISTENCE_QUEUE_FULL`，Runtime进入HALT |
| Writer失败 | 状态快照为`FAILED` | 产生一次`PERSISTENCE_FAILED`并由Watchdog authority进入HALT |
| 进程崩溃 | 子进程提交WAL后强制退出 | 重开后事务、Outbox和序号恢复一致，Runtime默认不恢复AUTO |
| SQLite竞争写锁下急停 | 第二连接持有写锁 | HALT先于审计写入生效，重启恢复仍保持HALT |
| 普通/关键入口溢出 | 状态机故障报告 | 普通丢失进入PAUSE_BUY；关键丢失进入HALT |

## 自动化验证

2026-09-04在Windows、Python 3.12.14执行：

- 编译、Ruff、格式检查和mypy严格检查全部通过；
- 1020项测试全部通过，总覆盖率94.11%；
- 56项订单门、运行时故障、SQLite崩溃恢复专项测试通过；
- 真实Writer阻塞场景确认Runtime HALT后Broker订单数为零；
- 真实SQLite健康探针确认低磁盘阈值在实际磁盘写满前触发HALT；
- Writer容量场景确认队列满立即失败，不阻塞调用线程；
- sdist与dev17 wheel从隔离构建环境生成成功。

## 独立安装验证

在全新虚拟环境中从dev17 wheel安装声明的运行依赖并验证：

- 包版本为`0.1.0.dev17`；
- 可从安装包导入`RuntimeFaultPolicy`、`PersistenceStallDetector`、
  `JournalHealthFaultDetector`和`RuntimeOrderExecutionGuard`；
- 安装包不依赖源码目录完成上述导入。

## 产物校验和

| 产物 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev17-py3-none-any.whl` | `606ef0663f67df640fa3a519ef03b8fa8ef5d132d7400b6f9d5fefa144f69002` |
| `src/auto_trading_system/runtime/faults.py` | `2dc645caada76be4f4141d1f444e6cd5a4ddaba94abdbde4b374ca62b2be596a` |
| `src/auto_trading_system/monitoring/persistence.py` | `b8960d6b84737ea1179284257008a9f709833239228732cf4ceb6351556f8619` |
| `src/auto_trading_system/monitoring/journal_health.py` | `d57cb54decb38f220e2abfeae2afbc41d2ca0393aabd25a5d52873ee5d8f02ac` |
| `src/auto_trading_system/execution/permission.py` | `209c98aead3f60c6f25a0c5e7171281d26477136abba9f052981ad469cd71682` |
| `src/auto_trading_system/execution/order_intent_gate.py` | `4ab0497088fbc2a8f29c279ef6c08d8ba5e914b47244dd2a4583310330f29b71` |
| `tests/integration/test_runtime_fault_matrix.py` | `a2a437b7465dc7544049bbc3e8a3837eb4a42fbb363e7f69aa0ccb696589c333` |
| `docs/runtime-fault-injection.md` | `8fa1d66861ac37a2b850cfc5d29bfe528575801c27610c9c7b99e83b8a059582` |

## 后续工作

P2阶段本地可靠性工作全部完成。P3真实QMT Gateway仍等待GOV-001和目标QMT环境；按依赖图继续
实施当前环境可独立验收的RSK-001计划级策略风险检查，并保留P3现场验收门。
