# Durable Outbox投递设计

## 1. 目标与边界

业务事实先随LocalJournal事务持久化，再由Windows节点的Sync Worker主动上传量化服务。投递采用
“本地至少一次、中心按业务ID和内容hash幂等”的语义：网络中断、进程退出和响应认证失败都不会
删除本地事实；只有经过HMAC验证且逐条身份匹配的中心回执才能改变Outbox状态。

Scheduler的`event-upload`任务每次只调用一次`DurableOutboxWorker.run_once()`。Worker不创建计时
线程、不连接QMT，也不在决策回调中执行HTTP。Persistence Writer仍是SQLite的唯一调用者。

## 2. 一次投递流程

1. 读取Journal健康和PENDING数量；Journal不可写时停止，中心调用次数为零。
2. 按`outbox_id`读取最多50条到期记录；批次必须是不可变tuple、项目必须为PENDING且身份唯一。
3. 按消息类型路由。Domain Event由`SignedEventBatchHttpTransport`发送到
   `POST /api/v1/trading/events/batch`。
4. 每次HTTP尝试生成新的`request_id`、Nonce和签名；重试时事件ID、事件hash和业务内容保持不变。
5. 先验证响应签名、请求ID和请求Nonce绑定，再解析HTTP状态和逐条回执。
6. ACK、重新调度和死信操作合并成一个Journal事务；提交结果不匹配时不得报告投递成功。

Worker禁止重叠执行。任务取消发生在HTTP过程中时不提交本地回执，原记录仍保持PENDING，由下次
Scheduler tick接管。

## 3. 回执和保留规则

| 中心结果 | 本地动作 | 说明 |
|---|---|---|
| `ACCEPTED` | ACK | 中心已持久化该事件 |
| `DUPLICATE` | ACK | 相同业务ID与hash已存在，达到幂等收敛 |
| `REJECTED + retryable=true` | RESCHEDULE | 保留原内容，推进重试计数和下次时间 |
| `REJECTED + retryable=false` | DEAD_LETTER | 保存中心给出的永久拒绝原因，供人工处置 |
| 超时、断网、非200、响应验签或结构失败 | 整批RESCHEDULE | 没有可信逐条结果，禁止部分ACK |

指数退避为`min(base × 2^(attempts-1), max)`，默认2秒起步、300秒封顶。重试计数使用32位
正整数边界；达到边界时失败关闭并保留PENDING，不能静默转死信。死信只接受经过认证的中心永久
拒绝，不能由本地次数阈值自动产生。

## 4. 高水位与交易安全

默认50,000条进入`HIGH`，100,000条进入`CRITICAL`。Worker在启动观察和级别变化时向
`DecisionEventLoop`关键通道发布`OUTBOX_PRESSURE_CHANGED`；信号包含当前数量和两个阈值。
`HIGH/CRITICAL`均表达“阻断新增买入”，状态机Handler据此收紧到`PAUSE_BUY`或更严格模式。
积压恢复为`NORMAL`只发布恢复事实，不授予AUTO许可；恢复仍需健康、对账和本地审批条件。
生产处理器固定使用`OutboxPressureDecisionHandler`：AUTO收到HIGH进入`PAUSE_BUY`，任意可运行态
收到CRITICAL进入`HALT`，NORMAL对当前限制执行NOOP。

SQLite适配器的`maximum_pending_outbox`绑定CRITICAL阈值。达到容量保护或关键事件入口拒绝压力
信号时，组件返回稳定错误并失败关闭，避免在无法留存审计证据时继续扩大交易风险。

## 5. 传输安全

- URL必须为HTTPS且路径固定为`/api/v1/trading`；禁止重定向并启用系统TLS验证；
- HTTP连接池、连接超时和读写超时来自版本化Runtime配置；
- HMAC secret只以`bytes`进入传输对象，`repr`固定脱敏；
- 发送前验证事件Schema语义、节点/账户作用域、`event_id`和`event_hash`；
- 响应在解释状态码和Body前完成HMAC、时钟窗口及请求绑定校验；
- 回执必须完整覆盖批次且身份不可重复，迟到或错配回执不能ACK其他记录。

## 6. 重启恢复与可观测结果

SQLite/WAL提交保证PENDING记录随进程崩溃保留。Runtime重启并完成Journal恢复后，Scheduler从到期
记录继续投递。中心若已收到但节点未记录ACK，会返回`DUPLICATE`，随后本地安全ACK。

每次运行返回抓取数、ACK数、重试数、死信数、投递前后积压、压力级别、Journal提交序号和稳定
错误码。日志和监控只记录这些结构化字段，不记录HMAC secret、完整响应或账户凭据。
