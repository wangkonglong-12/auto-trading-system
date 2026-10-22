# 订单意图持久化门

## 1. 目标与位置

`OrderIntentPersistenceGate`是Windows交易节点唯一允许调用Broker `submit_order`的业务
边界。它运行在`DecisionEventLoop`所有者任务内，通过`owner_guard`拒绝其他线程或任务
发起报单，并通过`OrderIntentCommitter`和`BrokerOrderSubmitter`两个最小端口隔离SQLite、
QMT及测试替身。

生产组合根把`PersistenceWriter`和唯一`BrokerGateway`注入该组件。架构测试扫描生产
源码中的`submit_order(...)`调用，除该持久化门外不允许出现第二条调用路径。

## 2. 固定执行时序

一次首次报单严格执行以下顺序：

1. 校验调用者是决策循环所有者，校验限价乘数量不超过意图的`max_notional`，并按当前
   Runtime状态与Windows Hard Safety执行第一次报单权限检查；
2. 由`OrderIntentRecord`构造Broker无关的`SubmitOrder`，避免调用者分别维护两份订单参数；
3. 在同一LocalJournal事务中提交`PutOrderIntent`与`ORDER_INTENT_CREATED`领域事件；
4. 只有收到事务自身的全新`COMMITTED`结果并确认预期事件已持久化后，立即重新读取Runtime
   状态和Hard Safety事实；第二次完整权限检查仍通过，才调用一次Broker `submit_order`；
5. Broker返回`SubmissionReceipt`后，把`request_seq`、`client_order_id`和24字符
   `remark_token`关联在第二个LocalJournal事务中；
6. 两次提交均明确完成后，才返回`SUBMITTED`。

意图事件由LocalJournal同时写入领域事件表和Durable Outbox，因此中心服务断网不影响
本地报单门，恢复联网后由JRN-004补传。

## 3. 稳定身份与幂等

调用者必须在第一次尝试前确定并保留以下不可变身份：

| 身份 | 用途 |
|---|---|
| `intent_id` | 唯一订单意图及意图事务身份 |
| `client_order_id` | Windows节点内部订单主键和Broker查询键 |
| `event_id` | `ORDER_INTENT_CREATED`事件及Outbox幂等键 |
| `remark_token` | 最长24字符的QMT委托/成交追溯标识 |
| `boot_id` | 本次Runtime进程及本地事件序列范围 |
| `correlation_id` | 贯穿计划、风控、意图和订单的关联ID |
| `causation_id` | 触发本意图的上游决定；没有明确上游时为`None` |

意图事务ID按`account_id + intent_id`生成稳定SHA-256摘要；回执事务ID按
`account_id + client_order_id + request_seq`生成。日志层同时校验事务ID和完整操作内容，
同ID同内容返回幂等结果，同ID不同内容报告冲突。

## 4. 返回与错误语义

| 情况 | 返回/错误 | Broker行为 | Runtime动作 |
|---|---|---|---|
| 新意图与回执关联均提交成功 | `SUBMITTED` | 恰好调用一次 | 等待Broker回调并进入订单状态机 |
| 首次权限检查不允许执行 | `ORDER_EXECUTION_NOT_ALLOWED` | 不调用 | 保持当前安全模式，不写入新意图 |
| 意图等待提交期间权限被收回 | `ORDER_EXECUTION_PERMISSION_REVOKED` | 不调用 | 意图已持久化，进入对账且禁止普通路径重报 |
| 意图事务返回`DUPLICATE` | `PERSISTED_RECONCILIATION_REQUIRED` | 不调用 | 保持/进入安全暂停，查询Broker事实后恢复 |
| 意图提交失败或调用被取消 | 原持久化错误/取消 | 不调用 | 按存储健康策略暂停或重试 |
| 意图提交结果与事务/事件不匹配 | `OrderIntentGateError` | 不调用 | 失败关闭并对账 |
| Broker调用抛错或返回非法回执 | `OrderSubmissionUnresolvedError`，阶段为`BROKER_SUBMISSION` | 已进入调用边界 | 禁止重报，先查询Broker |
| 回执关联提交失败或被取消 | `OrderSubmissionUnresolvedError`，阶段为`RECEIPT_PERSISTENCE` | 已取得Broker回执 | 安全暂停并用client/remark/seq对账 |

`BrokerTimeoutError.outcome_unknown`被保留到未决错误；即使Broker明确表示本次未应用，
已持久化意图也不会由普通重试路径自动重报，必须由恢复流程核对Broker事实并作出显式
决定。这一规则用可用性换取不重复下单。

## 5. 崩溃窗口与恢复

本地事务与券商系统之间不存在跨系统原子提交，必须明确处理三个崩溃窗口：

- 意图提交前崩溃：本地没有意图，Broker未被调用；
- 意图提交后、Broker调用前崩溃：恢复时看到意图但没有券商关联，查询订单/委托并使用
  `client_order_id`和`remark_token`判定，不自动重报；
- Broker调用后、回执关联提交前崩溃：Broker可能已有委托，恢复时必须以券商事实为准，
  补齐关联后才能继续。

Runtime重启仍按ADR-006停在`READY_PAUSED`。恢复和对账完成且取得明确恢复许可前，
持久化门不会把历史意图转换为新Broker调用。

## 6. 已验证边界

自动化测试覆盖：

- COMMIT失败、任务取消、返回结果错配和缺少事件时Broker调用次数为零；
- 买单仅在`AUTO`允许，卖单仅在`AUTO/PAUSE_BUY/SELL_ONLY`允许；权限读取返回非法状态时
  失败关闭；
- Writer阻塞期间Runtime进入HALT后，即使提交最终成功，Broker调用次数仍为零；
- 新事务只报一次，重复事务进入对账而不重报；
- Broker未启动、超时前未受理、超时后结果不明、适配器异常和非法回执；
- Broker已返回后，回执关联写入失败或取消不会被报告为成功；
- 真实SQLite/WAL、唯一Writer与Mock Broker组合下的意图、事件、Outbox、request_seq关联、
  重启恢复和Schema验证；
- 真实SQLite竞争写锁导致`JOURNAL_BUSY`时，Broker中保持零订单且数据库没有半写记录。
