# SQLite/WAL LocalJournal与Persistence Writer

## 1. 运行结构

每个Runtime实例只管理一个逻辑账户，并在`storage.root/data/local_journal.db`打开该账户的
LocalJournal。`PersistenceWriter`创建专用线程，在该线程内打开、使用和关闭唯一SQLite连接；
Decision Loop、QMT回调、Scheduler、同步Worker和本地API只提交有界异步请求，不直接接触连接。

默认Writer队列容量为1024，另保留一个内部停止槽位。队列满立即返回
`PERSISTENCE_QUEUE_FULL`，不会阻塞QMT回调或静默丢请求。已进入队列的提交即使调用协程被取消，
Writer也会先得到确定的提交结果再完成取消，防止订单意图持久化结果处于调用方未知状态。

## 2. 数据库归属和耐久参数

启动时创建父目录，并固定、回读以下参数：

| 参数 | 值 |
|---|---:|
| `journal_mode` | `WAL` |
| `synchronous` | `FULL` |
| `busy_timeout` | `3000 ms` |
| `wal_autocheckpoint` | `1000 pages` |
| `foreign_keys` | `ON` |
| `trusted_schema` | `OFF` |

数据库同时保存固定`application_id`、`user_version`、随机`journal_id`和逻辑`account_id`。非空未知
数据库、其他应用数据库、未来Schema、账户不匹配、迁移校验和不匹配、缺表/缺列/缺索引均拒绝
启动。一个数据库不会被另一账户接管；Checkpoint因此是该账户Runtime的库级状态。

## 3. Schema V1

Schema V1包含：

- `schema_migrations`：迁移版本、固定校验和和应用时间；
- `journal_meta`：Journal/账户身份及全局提交序号；
- `committed_transactions`：事务请求hash、原提交结果和幂等判定；
- `boot_sequences`：各`boot_id`最后事件序号；
- `trade_plans`、`runtime_commands`：计划及中心命令缓存；
- `order_intents`、`broker_order_links`、`broker_trades`：报单前意图和QMT标识链；
- `domain_events`、`outbox`：本地事件事实及原子补传队列；
- `local_controls`、`checkpoints`：本地控制证据和恢复游标。

价格和金额保存为规范十进制文本，数量/序号保存为64位整数，日期和时间保存为规范wire文本，
文档保存为不可变UTF-8规范JSON字节。Schema不存在Tick表；完整Tick仍进入独立Parquet/ZSTD
Recorder，不与恢复关键事务争抢SQLite Writer。

## 4. 原子提交与幂等

每次`commit()`执行以下固定流程：

1. 对全部强类型操作做规范编码并计算事务请求hash；
2. `BEGIN IMMEDIATE`获取短写事务；
3. 相同事务ID、相同hash返回原提交序号和事件，结果标记`DUPLICATE`；
4. 相同事务ID、不同hash返回永久幂等冲突；
5. 分配全局`commit_sequence`，按调用顺序应用1至500个操作；
6. 保存事务结果、推进元数据并`COMMIT`。

任一操作失败会完整回滚，提交序号和事件序号不被消耗。若SQLite在`COMMIT`阶段返回错误，连接
立即失败关闭并返回`JOURNAL_COMMIT_OUTCOME_UNKNOWN`；重启后使用原事务ID重试，由事务记录判断
已提交或未提交，禁止根据超时猜测结果。

不可变记录完全相同可幂等重放，不同内容返回`JOURNAL_IDEMPOTENCY_CONFLICT`。Broker订单关联只
允许从空值补齐，已知标识不可改写，`updated_at`保留较新值；remark必须与已持久化意图一致，
成交账户必须与意图账户一致。Checkpoint只允许revision前进。

## 5. 事件与Outbox

Writer按`boot_id`在事务内分配严格递增`local_seq`，以
`max(commit_clock, occurred_at)`生成`recorded_at`，校验完整事件语义后计算`event_hash`。
Event Journal和`DOMAIN_EVENT` Outbox项在同一SQLite事务写入；相同事件只能得到一个Outbox项和
一个提交结果记录。

Outbox内部状态为`PENDING/ACKED/DEAD_LETTER`。ACK保留终态记录而不物理删除，并同时校验
`outbox_id + content_hash`。重试会持久化严格增加的`attempts`、不倒退的`available_at`和稳定原因
码；JRN-004在此基础上实现网络批次与退避策略。

## 6. 恢复、健康与维护

`load_recovery()`只接受数据库绑定账户，按交易日返回计划、命令、订单意图、QMT关联和成交；
本地控制按`created_at, control_id`返回账户全部历史，由状态机重放有效状态；Checkpoint返回当前
库级游标。命令高水位按账户全部已存命令计算。

`health()`执行短读取和可回滚写探针，分别报告主库字节、WAL字节、剩余磁盘、Outbox积压和最后
提交序号。磁盘低水位、WAL超阈值、Outbox超阈值或临时Busy进入`DEGRADED`；只读、I/O、磁盘满、
损坏或存储不变量失败进入`UNAVAILABLE`并关闭连接。

维护能力固定为：

- `CHECKPOINT`：执行`wal_checkpoint(TRUNCATE)`，Busy时明确返回未完成，并报告已处理页字节数；
- `BACKUP`：用SQLite Backup API写同目录唯一临时文件，对备份执行`quick_check`后原子发布，拒绝
  覆盖已有目标以及源库、WAL或SHM路径；
- `INTEGRITY_CHECK`：执行`quick_check`，失败立即使Journal不可用。

15:20 Checkpoint与15:30 Backup仍由Runtime内部Scheduler发布控制事件，并由同一个
Persistence Writer串行执行，不创建第二SQLite写入者。

## 7. 错误与停止语义

Busy/Locked是可重试错误，Journal保持打开；业务幂等冲突、错误账户、错误文档和错误Outbox回执
是永久请求错误，但不会污染连接。磁盘满、只读、I/O、损坏、解码不变量、序号耗尽和提交结果
未知均失败关闭。对外错误只包含稳定code和通用说明，不暴露SQL、数据库路径或业务数据。

Writer正常停止时先停止接收请求，再按FIFO处理已接受请求，最后关闭Journal。启动或停止协程被
取消时仍会向线程发送停止信号；停止等待被取消后可再次等待同一关闭过程。关闭超时进入
`FAILED`，后台线程为daemon，不能阻止进程在故障处置时退出。
