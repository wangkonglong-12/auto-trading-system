# JRN-002 SQLite/WAL单Writer与迁移验收证据

## 验收结论

JRN-002完成。账户级SQLite数据库由唯一`PersistenceWriter`线程独占，启用WAL与
`synchronous=FULL`，所有业务写入使用短事务原子提交。Schema迁移、数据库身份与
账户绑定、事务幂等、Checkpoint、Outbox状态、健康探针、在线备份、完整性检查以及
异常后的失败关闭均已实现并通过测试。

完整Tick不进入SQLite；后续由MKT-002使用Parquet/ZSTD分区记录。

## 实现边界

- `journal/sqlite.py`是唯一允许直接依赖`sqlite3`的模块；领域、决策循环和Broker
  Gateway只依赖存储中立的LocalJournal契约。
- `PersistenceWriter`使用容量有界的专属线程队列，SQLite连接的打开、访问和关闭
  均发生在同一线程。
- 每个数据库记录固定`application_id`、Schema版本、迁移校验和、`journal_id`和
  `account_id`；陌生、篡改、未来版本或错账户数据库拒绝打开。
- 订单意图、订单标识、成交、领域事件、Outbox、控制记录和Checkpoint可在一个
  `BEGIN IMMEDIATE`事务内提交；失败完整回滚，提交结果按事务哈希幂等返回。
- 健康检查包含短写探针、数据库/WAL大小、磁盘余量、Outbox积压与提交水位；维护
  支持WAL Checkpoint、SQLite Backup API加快速完整性校验和完整性检查。

## 自动化验证

2026-09-04在Windows、Python 3.12.13执行全仓质量门：

- 编译、Ruff检查与格式检查通过；
- mypy严格检查通过，覆盖90个源文件；
- 888项测试全部通过；
- 总覆盖率97.06%，高于90%质量门；
- 包含独立子进程在已提交事务后`os._exit`的真实WAL崩溃恢复，以及未提交事务回滚、
  重启恢复和`quick_check`验证；
- sdist与wheel从隔离构建环境成功生成。

专项SQLite、Writer和崩溃恢复测试共50项，覆盖队列背压、生命周期取消、写入故障、
并发锁、迁移篡改、账户错配、序列上限、不可变冲突、Outbox状态和恢复校验。

## 独立安装验证

新建空白虚拟环境，严格按`requirements/runtime.lock`的hash安装运行依赖，再以
`--no-deps`安装构建wheel。验证结果：

- `auto-trading-runtime --version`输出`0.1.0.dev13`；
- 通过wheel创建SQLite/WAL数据库并启动唯一Writer；
- 原子提交Checkpoint，健康探针确认可写与提交水位；
- Backup API生成可读备份；
- 停止并重新打开Writer后成功恢复Checkpoint；
- 冒烟结果：`WHEEL_SQLITE_WAL_WRITER_SMOKE=PASS`；
- 临时验证环境已安全清理。

## 产物校验和

| 产物 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev13-py3-none-any.whl` | `1179f325968bd3b77c4a66bc59c108eb330a93e3764d1b32c06d87dd56fa1559` |
| `src/auto_trading_system/journal/sqlite.py` | `2d7dca3961113c91f2e69da82c8dc10dc8d33ecc4c8700c3e1ab4392b468c6ed` |
| `src/auto_trading_system/journal/writer.py` | `10e73698a0bc57b989f759bb15d3dbcbc8361d16378a080d8fb8287206f3455c` |
| `src/auto_trading_system/apps/trade_runtime/bootstrap.py` | `1f206fac5fbf30832ffce4a9db155f6ba0673bc47da94bd5ea3fe4860a5682ba` |
| `docs/sqlite-local-journal.md` | `eee38ef5a38750342ba32a4646219cda9df1bcd2ba8e71cfb1493ed017a8023d` |

## 后续工作

JRN-003直接在该事务边界上实现订单意图持久化门：只有明确收到全新事务的
`COMMITTED`结果后才允许首次调用Broker Gateway；提交失败和结果不明均阻断报单。
