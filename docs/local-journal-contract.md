# LocalJournal端口与恢复数据模型

## 1. 职责边界

`LocalJournal`保存交易节点崩溃恢复、幂等执行、事实追踪和中心补传所需的关键数据。交易核心依赖
`journal.port.LocalJournal`及不可变事务命令；SQLite/WAL代码集中在`journal/sqlite.py`，
通过`journal/writer.py`的有界Persistence Writer接入，完整实现见`sqlite-local-journal.md`。

节点数据按用途落在两个存储面：

| 数据 | 存储组件 | 用途 |
|---|---|---|
| TradePlan缓存、中心命令 | LocalJournal | 断网期间使用已冻结计划，命令按序幂等应用 |
| OrderIntent、QMT标识链、成交去重 | LocalJournal | 报单前持久化门、提交不明恢复、订单双向追踪 |
| DomainEvent、资产/持仓快照、对账、命令结果 | LocalJournal + Durable Outbox | 本地事实与中心至少一次补传 |
| 本地Override、Emergency Halt、检查点 | LocalJournal | 本地优先级恢复和审计 |
| 完整Tick流 | Parquet/ZSTD Tick Recorder | 研究、回放和20个交易日保留 |
| 60秒决策行情窗口 | Runtime内存 | 新鲜度判断和执行决策 |

SQLite承载低频、恢复关键的交易事实；完整Tick的高吞吐写入由独立Recorder队列和分区文件承载，
从资源上隔离交易事务。

## 2. 单Writer调用模型

`LocalJournal`是同步阻塞端口，只由Runtime内一个Persistence Writer拥有和调用。决策循环把
`JournalTransaction`发送给Writer并等待异步结果事件；QMT回调、Scheduler、Sync Worker和业务
Handler均通过该Writer排队。

这一模型把连接、事务、迁移、维护和关闭收敛到一个线程，配合RT-002有界队列提供确定背压。
Journal健康从`HEALTHY`降为`DEGRADED/UNAVAILABLE`时，Writer发布关键结果事件，RT-003收紧状态。

## 3. 不可变记录

| 记录 | 唯一键/关键字段 | 恢复用途 |
|---|---|---|
| `CanonicalJsonDocument` | `document_id + content_hash` | 保存完整规范JSON字节，重复内容可比较 |
| `OrderIntentRecord` | `intent_id`、`client_order_id` | 报单方向、数量、限价、上限金额、remark和策略版本 |
| `BrokerOrderLink` | `client_order_id` | 关联request seq、order id、order sysid和24字符remark |
| `BrokerTradeRecord` | `account_id + trade_id` | 成交回调/查询去重并关联内部订单 |
| `EventDraft` | `event_id` | Writer分配序号、记录时间、事件hash并构造完整DomainEvent |
| `LocalControlRecord` | `control_id` | 本地接管、急停和恢复批准证据 |
| `CheckpointRecord` | `namespace + key + revision` | 命令高水位、恢复进度和组件检查点 |
| `OutboxItem` | 自增`outbox_id`、`document_id + hash` | 重试、逐条回执和死信处理 |

JSON文档使用UTF-8规范编码：键排序、无额外空白、非ASCII字符原样编码、最大4 MiB。端口保存
`bytes`而非可变字典，读取时每次解码为新对象。所有ID、hash、证券代码、Decimal金额/价格、整数
数量、交易日和上海时间均在进入Writer前完成强类型校验。

## 4. 原子事务命令

一个`JournalTransaction`具有稳定`transaction_id`和1至500个操作：

- `CacheTradePlan`：保存验签、Schema和语义校验通过的冻结计划；
- `StoreCommand`：保存中心命令及单调`command_seq`；
- `PutOrderIntent`：创建唯一订单意图；
- `UpsertBrokerOrderLink`：逐步补齐QMT请求和券商标识；
- `RecordBrokerTrade`：按成交ID保存去重事实；
- `AppendDomainEvent`：写Event Journal并在同一事务创建Domain Event Outbox项；
- `EnqueueOutbox`：保存资产、持仓、对账或命令结果；
- `AcknowledgeOutbox`：按`outbox_id + content_hash`确认已接收项；
- `RescheduleOutbox`：推进重试次数、下次可用时间和最近失败原因；
- `DeadLetterOutbox`：保存中心返回的永久拒绝及原因；
- `PutLocalControl`：保存本地控制证据；
- `PutCheckpoint`：按revision推进恢复检查点。

Domain Event统一走`AppendDomainEvent`，保证事件Journal和Outbox共存亡。Writer在事务内按`boot_id`
分配严格递增`local_seq`，设置`recorded_at`、计算`event_hash`，并把最终
`PersistedEvent`放入`JournalCommitResult`返回决策循环。

## 5. 提交和幂等语义

- 首次成功事务返回`COMMITTED`和严格递增`commit_sequence`；
- 相同`transaction_id`与相同操作重试返回原提交的`DUPLICATE`结果；
- 同一业务ID出现不同不可变内容返回`JOURNAL_IDEMPOTENCY_CONFLICT`，属于永久冲突；
- 任一操作失败时整个事务回滚，订单意图持久化门据此阻断Broker调用；
- 对外错误只包含稳定code和retryable，底层SQL、路径和数据细节进入受保护诊断；
- Outbox确认同时校验content hash，迟到回执无法删除同ID的其他内容。

## 6. 启动恢复快照

`load_recovery(account_id, trade_date)`一次返回账户/交易日范围内的：

- 有效计划和已接收命令；
- 订单意图与完整券商标识链；
- 当日已知成交；
- 仍然生效的本地控制；
- 命令高水位和组件检查点。

返回集合全部为tuple，记录账户和交易日必须与查询范围一致。Runtime用该快照重建内存后，再以
QMT资产、持仓、委托和成交四类查询做启动对账；LocalJournal是本地意图/证据，券商查询是最终
交易事实。

## 7. Outbox读取

`fetch_outbox(limit, available_at)`按自增`outbox_id`读取到期`PENDING`项。持久化消息类型固定为
Domain Event、资产快照、持仓快照、对账报告和命令结果。心跳由最新Runtime快照直接发送，下一次
覆盖上一次状态。

JRN-003已在此端口上实现订单意图/事件原子提交、Broker调用门和request seq关联。
JRN-004在此端口上实现批次、指数退避、逐条ACK、永久拒绝死信和高水位；网络超时或响应验签失败
保留PENDING内容及原业务ID/hash。

## 8. 健康与维护

`health()`统一返回状态、可写性、Schema版本、主数据库/WAL字节数、剩余磁盘、Outbox积压、
最后提交序号和稳定原因码。`HEALTHY`同时表示可写且无降级原因，`UNAVAILABLE`同时表示写入许可
关闭。

`maintain()`提供三项存储中立能力：

- `CHECKPOINT`：收敛增量日志并返回完成结果；
- `BACKUP`：写入调用方指定的绝对目标路径；
- `INTEGRITY_CHECK`：执行一致性检查并返回稳定detail code。

SQLite适配器将其分别映射为WAL Checkpoint、SQLite Backup API和`quick_check`，所有维护仍由
Persistence Writer串行执行。

## 9. 生命周期

RT-004在取得账户Global Mutex后调用`open()`，完成Schema版本检查、迁移和写健康验证，再启动
Decision Loop与QMT。停止时先结束QMT和排空决策事件，再由Persistence Phase调用`close()`，
最后释放账户锁。

端口状态为`NEW → OPEN → CLOSED`，存储故障进入`FAILED`。同一实例关闭后由新Runtime重新创建，
并通过恢复快照和券商对账进入`READY_PAUSED`。
