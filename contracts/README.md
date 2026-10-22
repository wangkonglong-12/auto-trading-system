# 跨项目契约

## 当前版本入口（V3.1 / 协议V2）

`v2/openapi.json`列出9个已实现的节点接口；`v2/schemas`包含16份Schema，`v2/examples`保留公共测试向量。V2计划采用Ed25519签名，节点HTTPS采用HMAC双向认证，金融记录使用严格字段定义。账单完整载荷与结算API仍待交付。

以下保留旧V1协议说明，仅供兼容路径使用。当前实现状态与V1/V2切换边界见`docs/design/v3.1/续开发实施报告_20260920.md`；公共测试密钥用于测试，生产发布器使用独立受控签名密钥。

## V1兼容基线

`contracts/v1`是协议1.x的机器可读基线。量化服务和Windows交易节点使用同一份Schema、
样例和测试向量完成契约测试。

`contracts/v1/manifest.sha256.json`列出完整协议包中每个文件的规范相对路径和SHA-256。两个项目
各自在CI中校验本地文件集合和摘要，并可通过`scripts/verify-cross-project-contracts.ps1`运行
双实现验证。协议变更必须同步更新两端文件和清单，禁止只比较现有文件交集。

## TradePlan哈希

1. 复制TradePlan顶层对象并移除`plan_hash`字段。
2. 使用UTF-8编码JSON。
3. 对象键按Unicode码点升序排列。
4. 分隔符固定为`,`和`:`，不输出额外空白。
5. 保留Unicode字符，拒绝NaN和Infinity。
6. 金额、价格、比例和评分使用十进制字符串。
7. 对最终字节执行SHA-256，结果格式为`sha256:<64位小写十六进制>`。

Python参考实现位于`auto_trading_system.protocol.canonical_json`。

## DomainEvent与Outbox上传

`domain-event.schema.json`定义Windows节点产生的不可变领域事件；
`event-batch-request.schema.json`和`event-batch-result.schema.json`定义
`POST /api/v1/trading/events/batch`的请求及逐条回执。单批1至50条。

事件封套固定包含：

- `event_id`：事件业务幂等键，首次本地提交前生成，所有重试保持不变；
- `event_hash`：移除自身后按规范JSON计算SHA-256，用于检测同ID不同内容；
- `boot_id`：Runtime每次进程启动生成一次，同一次启动内保持不变；
- `local_seq`：LocalJournal Writer在一个`boot_id`内从1严格递增分配；
- `correlation_id/causation_id`：串联计划、风险、订单、成交、控制和告警因果链；
- `aggregate_type/aggregate_id`：确定中心投影目标；
- `occurred_at/recorded_at`：事实发生时间与本地持久化时间，均显式使用`+08:00`；
- `source/severity/event_type/payload`：来源、级别、类型及对应的严格载荷。

事件哈希算法与TradePlan一致，但移除的字段为`event_hash`。Python参考实现为
`calculate_event_hash()`和`verify_event_hash()`。

### 事件类型

| 事件类型 | 聚合 | 用途 |
|---|---|---|
| `RUNTIME_STATE_CHANGED` | Runtime | 记录启动、对账、暂停、AUTO、只卖和HALT状态变化 |
| `PLAN_STATUS_CHANGED` | Plan | 记录计划接收、校验、ACK、运行、完成或拒绝 |
| `ORDER_INTENT_CREATED` | Order | 记录持久化门前确定的唯一订单意图 |
| `ORDER_STATUS_CHANGED` | Order | 记录QMT回调/查询归一化后的订单状态和累计数量 |
| `TRADE_RECORDED` | Trade | 按券商成交编号记录不可变成交事实 |
| `RISK_DECISION` | Risk | 记录策略风控和本机Hard Safety逐规则结果 |
| `CONTROL_COMMAND_APPLIED` | Control | 记录本地或中心命令的执行、拒绝或NOOP结果 |
| `RECONCILIATION_DISCREPANCY` | Reconciliation | 记录本地事实与券商事实差异 |
| `ALERT_RAISED` | Alert | 记录可去重、可追踪的运行告警 |

### 幂等与逐条回执

中心服务先在同一事务中登记不可变原始事件；投影由后续独立工作项消费这些原始事实，按以下规则返回：

| 中心已有状态 | 回执 | retryable | 处理 |
|---|---|---:|---|
| 不存在`event_id` | `ACCEPTED` | false | 保存原始事件并标记等待投影 |
| 相同`event_id + event_hash` | `DUPLICATE` | false | 不重复投影，视为上传成功 |
| 相同`event_id`、不同`event_hash` | `REJECTED/EVENT_ID_CONFLICT` | false | 不覆盖原事件，进入人工处置 |
| 相同`boot_id + local_seq`、不同事件 | `REJECTED/LOCAL_SEQUENCE_CONFLICT` | false | 不覆盖原序列，进入人工处置 |
| 临时存储不可用 | 签名HTTP `503` | true | 整批事务回滚，Windows保留Outbox重试 |

Windows仅在收到与请求`event_id + event_hash`完全一致的`ACCEPTED`或`DUPLICATE`
回执后删除Outbox项。`REJECTED`且`retryable=true`继续退避重试；非重试错误转入死信并产生
CRITICAL告警。HTTP超时或响应验签失败时整批保留并重试。

传输层不执行隐藏重试；每次Scheduler补传生成新的`request_id`、时间戳、Nonce和签名，事件
业务ID、事件hash和业务内容保持不变。服务端允许跨批、跨启动和补传导致的乱序，不能因
`local_seq`低于当前高水位而拒绝；`boot_id + local_seq`只用于唯一性、缺口检测和诊断。

批量结果同时返回`accepted/duplicates/rejected/high_watermark`。前三项必须与逐事件回执
精确一致；`high_watermark`是本次请求中`ACCEPTED`或`DUPLICATE`事件的最大`local_seq`，无
成功项时为0。它只用于诊断，Windows不得据此批量确认Outbox；安全推进仍以逐事件回执为准。

## 快照、对账、心跳和命令

CON-003固定以下接口载荷。路径均位于`/api/v1/trading`：

| 方法与路径 | Schema | 交付语义 |
|---|---|---|
| `POST /snapshots/assets` | `asset-snapshot.schema.json` | Durable Outbox；`snapshot_id`幂等 |
| `POST /snapshots/positions` | `position-snapshot.schema.json` | Durable Outbox；`snapshot_id`幂等 |
| `POST /reconciliations` | `reconciliation-report.schema.json` | Durable Outbox；`reconciliation_id`幂等 |
| `POST /runtime/heartbeat` | `runtime-heartbeat.schema.json` | 5秒最新状态，失败后由下一次心跳覆盖，不积压Outbox |
| `GET /runtime/commands?after_command_seq=N` | `runtime-command-batch.schema.json` | 按`command_seq`严格递增拉取 |
| `POST /runtime/commands/{command_id}/result` | `runtime-command-result.schema.json` | Durable Outbox；最终结果幂等回传 |
| 所有协议错误响应 | `protocol-error.schema.json` | 统一错误码、可重试性和字段错误 |

### 查询结果和不可知状态

资产、持仓及对账输入使用`AVAILABLE/QUERY_FAILED/UNKNOWN`三态：

- `AVAILABLE`必须携带数据且不得携带`error`；持仓数组为空才明确表示空仓；
- `QUERY_FAILED/UNKNOWN`必须携带`error`且不得携带资产或持仓数据；
- QMT返回`None`、连接状态不明确或重试后仍有歧义时使用`UNKNOWN`，禁止上传零资产或空数组；
- 快照的`as_of`表示券商事实查询时点，不使用中心接收时间替代；
- 金额和价格使用十进制字符串，数量使用非布尔整数。

### 对账结果

每次对账必须同时登记`ASSET/POSITION/ORDER/TRADE`四类输入：

- 四类输入均明确且无差异时为`MATCHED`；
- 四类输入均明确且至少一项差异时为`DISCREPANCIES`；
- 任一输入查询失败或结果不明时为`INDETERMINATE`，不得宣称对账成功；
- 汇总计数必须由差异明细推导；未关闭的CRITICAL差异阻断新增买入和计划完成。

### 心跳与命令

心跳以`node_id + boot_id + heartbeat_seq`识别进程和次序，报告Runtime模式、QMT/账户/
中心/行情连接、队列、Journal、Outbox、WAL、磁盘、时钟偏差和版本。`AUTO`状态必须同时满足
QMT已连接、账户`OK`、行情健康和Journal健康；中心暂时不可达不会抹去本地冻结计划，事件留在
Outbox补传。

命令以`command_id`幂等、以账户内`command_seq`排序并强制TTL。Windows按顺序处理每个命令，
过期命令也必须回传`REJECTED`结果，不能静默跳过。`REQUEST_AUTO_RESUME`只是一项恢复申请；
中心命令可以收紧为`PAUSE_BUY/SELL_ONLY/MANUAL/EMERGENCY_HALT`，不能解除本地HALT或绕过
Hard Safety。HTTP接收成功不等于命令生效，中央页面以最终CommandResult及对应领域事件为准。

本地控制事件使用`PAUSE_BUY/SELL_ONLY/MANUAL_TAKEOVER/EMERGENCY_HALT/CLEAR_HALT/
REQUEST_AUTO`动作。`CLEAR_HALT`只到`READY_PAUSED`，随后`REQUEST_AUTO`作为独立动作要求
本地与中心双重许可；两次结果分别留存和回传。

## HMAC认证

所有跨项目请求使用TLS和HMAC-SHA256 v1。签名覆盖key id、Unix时间戳、每次HTTP尝试唯一的
128位Nonce、request id、HTTP方法、原始origin-form路径/查询串和精确Body SHA-256。响应签名
覆盖HTTP状态，并绑定发起请求的request id和Nonce。

每次HTTP尝试生成新的`request_id`、时间戳、Nonce和签名；业务幂等由载荷内稳定业务ID和内容
hash保证。服务端在HMAC验证成功后通过共享存储原子占用`key_id + nonce`；Nonce Store不可用时失败关闭。完整规范和
服务端验证顺序见`docs/hmac-authentication.md`，跨项目固定向量位于
`examples/security/hmac-sha256-v1.vector.json`。

## 计划ACK

`plan-ack-request.schema.json`冻结`POST /plans/{plan_id}/ack`请求；
`plan-ack-result.schema.json`冻结创建和幂等重复回执。ACK状态为`ACCEPTED/REJECTED/
STARTED/COMPLETED/CANCELLED/EXPIRED`。`ACCEPTED`必须报告`READY_PAUSED`，明确表示计划已验签、
静态检查并原子落盘，但尚未进入AUTO或报单。`REJECTED`必须携带结构化原因，其他状态不得携带
拒绝原因。客户端重试同一逻辑ACK必须保持`request_id`和载荷不变、只更新HTTP Nonce；服务端以
`node_id + request_id`幂等，同一键不同载荷返回冲突。

## 兼容规则

- 当前支持major版本`1`。
- minor版本只增加可选字段，并保持既有字段语义。
- TradePlan拒绝未知major时返回`PLAN_VERSION_UNSUPPORTED`；其他消息返回
  `PROTOCOL_VERSION_UNSUPPORTED`，错误信封本身使用接收方支持的`1.0`。
- `plan_id`进入ACKED或RUNNING后保持不可变；变更发布新的`plan_id`和`plan_hash`。
- 事件新增可选字段属于minor兼容变更；删除字段、改变字段含义或改变既有事件载荷属于major变更。
- 新事件类型必须先保证中心服务能够安全接收和存档，再升级Windows节点开始发送。

## 样例集合

| 文件 | 场景 |
|---|---|
| `examples/trade-plan.ready.json` | 完整数据生成的可执行Shadow计划 |
| `examples/trade-plan.no-trade.json` | 当日零标的安全计划 |
| `examples/trade-plan.partial-shadow.json` | partial数据生成的Shadow计划 |
| `examples/trade-plan.expired.json` | 已过期计划的恢复与拒绝场景 |

事件样例位于`examples/events`，覆盖全部9类事件以及批量请求和逐条回执。快照、对账、心跳、
命令和错误样例分别位于`examples/snapshots`、`examples/reconciliation`、`examples/runtime`
和`examples/errors`。
