# Windows节点与量化服务同步契约

## 1. 责任边界

Windows长期运行的Sync Worker是跨项目通信的唯一发起方。它拉取计划和命令，上传事件、
资产/持仓快照、对账结果、命令结果与心跳；QMT回调线程和交易决策循环不直接发起HTTP。

同步分成两类：

- 业务事实：事件、资产、持仓、对账和命令结果先持久化到Durable Outbox，再至少一次上传；
- 临时健康：心跳只表达最新状态，发送失败不进入Outbox，由下一次心跳覆盖。

## 2. 快照设计

资产快照保留QMT的`cash/frozen_cash/market_value/total_asset`，并兼容已验证版本可能提供的
`fetch_balance/current_balance`。持仓快照保留证券代码、总量、可用、冻结、在途、昨日数量、
平均价和市值。业务层只使用统一字段，不接触XtQuant对象。

`query_status`决定载荷形态：

| query_status | 数据字段 | error | 含义 |
|---|---|---|---|
| `AVAILABLE` | 必须存在；持仓允许空数组 | 禁止 | 查询明确成功 |
| `QUERY_FAILED` | 禁止 | 必须存在 | 调用明确失败 |
| `UNKNOWN` | 禁止 | 必须存在 | `None`或环境状态导致结果有歧义 |

同一`snapshot_id`重传时Body必须保持不变。中心发现同ID不同内容时拒绝覆盖并创建Incident。

## 3. 对账设计

对账报告由一次完整的资产、持仓、当日委托和当日成交查询形成。四类输入各出现一次，并记录
查询时点、状态和快照/查询引用。只要任何一类不可知，整个报告就是`INDETERMINATE`；系统进入
`READY_PAUSED/PAUSE_BUY`，继续重试或等待人工处理。

差异明细独立编号，按资产、持仓、委托和成交分类，保留本地值、券商值、严重级别与处置状态。
中心保存每次报告，不用新报告覆盖旧证据。券商事实优先；差异处置通过新事件和新对账报告闭环。

## 4. 心跳设计

交易时段每5秒发送一次，非交易时段降低频率。心跳包含：

- Runtime状态、执行环境、boot和递增序号；
- QMT、券商账户、中心API和行情状态；
- 决策队列、Writer队列、Journal、Outbox、活动订单、订阅数、WAL和磁盘；
- 时钟偏差、运行版本、配置hash及QMT/XtQuant/Python三元组；
- 最近一次对账和当前降级原因。

中心按接收时间计算失联，但页面同时展示节点`sent_at`。心跳乱序到达时保留原始记录，只用同一
`boot_id`内最大的`heartbeat_seq`更新当前视图。

## 5. 命令设计

中心为节点/账户维护单调递增`command_seq`。Windows使用已处理高水位拉取命令，先把命令写入
LocalJournal，再由单一事件循环按顺序应用，最终结果经Outbox回传。

命令作用域受类型限制：撤单只能指向订单，冻结/解冻只能指向证券，对账可以指向节点、账户、
证券或订单，模式命令只能指向节点或账户。每条命令必须有签发人、原因、签发时间和过期时间。

最终结果固定为`APPLIED/REJECTED/NOOP/DUPLICATE/FAILED`。`REJECTED/FAILED`携带结构化错误；
成功结果不可标为可重试。中心在收到最终结果前展示“已下发/待应用”，不得显示为“已生效”。

## 6. 本地优先级

命令优先级为：本地Emergency Halt和Hard Safety > 券商/QMT事实限制 > 中心收紧命令 > 策略状态。
远程`REQUEST_AUTO_RESUME`仅触发本地前置检查和人工批准流程，不能自动清除HALT、持仓差异、
持久化故障、行情陈旧或未知订单状态。

## 7. 重试与幂等

- HTTP传输不做隐藏的内部重试。每次Scheduler重试生成新的`request_id`、Nonce、发送时间和签名，
  但原业务ID、内容hash和业务载荷保持不变。
- 资产/持仓用`snapshot_id`幂等，对账用`reconciliation_id`幂等，命令与结果用`command_id`
  幂等。
- HTTP超时、验签失败和5xx不删除Outbox；永久Schema/身份错误进入死信并产生CRITICAL告警。
- 心跳不进入Outbox；下一次心跳携带当前Outbox积压和最老记录时间，供中心监控。

Domain Event批次每次最多50条。响应必须先通过HMAC认证及请求ID/Nonce绑定校验，再按事件ID和
事件hash逐条应用`ACCEPTED/DUPLICATE/REJECTED`结果。无可信完整回执时整批指数退避；永久死信
只接受认证响应中的不可重试拒绝。详细运行规则见`durable-outbox.md`。
