# CON-002 事件契约验收证据（2026-09-04）

## 交付范围

- DomainEvent v1.0严格JSON Schema和规范事件封套。
- `RUNTIME_STATE_CHANGED`、`PLAN_STATUS_CHANGED`、`ORDER_INTENT_CREATED`、
  `ORDER_STATUS_CHANGED`、`TRADE_RECORDED`、`RISK_DECISION`、
  `CONTROL_COMMAND_APPLIED`、`RECONCILIATION_DISCREPANCY`、`ALERT_RAISED`
  共9类严格载荷。
- EventBatchRequest v1.0，单批1至50条。
- EventBatchResult v1.0，对每个`event_id + event_hash`返回独立回执。
- 9类事件样例、批量请求样例和批量结果样例。
- 事件哈希计算/常量时间校验以及`event_id/boot_id/request_id`生成器。
- 事件、批次、聚合绑定、时间、数量与回执完整性的语义校验。

## 固定幂等与顺序语义

- `event_id`是业务幂等键，在首次本地提交前生成，Outbox重试保持不变。
- `event_hash`移除自身后按规范JSON计算SHA-256；同一`event_id`不同hash是永久冲突。
- Runtime每次启动生成新的`boot_id`；`local_seq`由LocalJournal Writer在该boot内从1递增。
- 中心对`event_id`和`node_id + boot_id + local_seq`分别建立唯一约束。
- 同ID同hash返回`DUPLICATE`且不得重复投影；Windows将其视作上传成功。
- 中心允许断网补传造成的跨批乱序，序号低于高水位不能作为拒绝依据。
- Windows只在收到完全匹配的`ACCEPTED`或`DUPLICATE`回执后删除Outbox项。
- HTTP失败、响应验签失败和`retryable=true`保留Outbox；永久错误转死信并产生CRITICAL告警。
- 同一Body的网络重试复用`request_id`，批次内容变化时生成新`request_id`。

## 安全和数据边界

- 时间戳使用严格ISO 8601格式并显式为`+08:00`，发生日必须匹配`trade_date`。
- `recorded_at`不得早于`occurred_at`。
- 金额、价格使用十进制字符串，数量使用非布尔整数。
- `aggregate_type/aggregate_id`必须与事件类型及载荷主标识一致。
- 订单状态事件要求`filled_quantity + remaining_quantity = order_quantity`。
- 事件和载荷均拒绝未知字段，告警及差异文本设置长度上限。
- `account_id`使用系统逻辑账号标识；载荷不得放入券商完整账号、secret或认证材料。

## 验证结果

| 验证项 | 结果 |
|---|---|
| JSON Schema Draft 2020-12自校验 | 3份Schema全部通过 |
| 全事件类型样例覆盖 | 9类全部覆盖并通过Schema、语义和hash验证 |
| Ruff | 通过 |
| Ruff格式一致性 | 49个文件通过 |
| mypy strict | 49个源文件无问题 |
| pytest | 309项全部通过 |
| 分支覆盖率 | 98.27%，门槛90% |
| CON-002新增测试 | 80项；Schema、载荷、hash、时区、聚合、批次和回执边界均覆盖 |
| sdist/wheel构建 | 通过，版本`0.1.0.dev3` |
| 锁定依赖环境安装及版本查询 | 通过，输出`auto-trading-runtime 0.1.0.dev3` |
| 安装后事件协议冒烟测试 | 通过，hash及三类ID生成器可用 |

## 固定哈希

| 文件 | SHA-256 |
|---|---|
| `contracts/v1/domain-event.schema.json` | `55802b0464e004979960cd44dce7c1e97b64a5e0450a0e227812f82375a6eafa` |
| `contracts/v1/event-batch-request.schema.json` | `4df20ca058a70af260b9b9afe6715d800464123b6db70e967e36e2adc47802d2` |
| `contracts/v1/event-batch-result.schema.json` | `8bfdeaa5c2ac750638ffff8d10e53983f6680191aaf5023e9807db7f318213e0` |
| `dist/auto_trading_system-0.1.0.dev3-py3-none-any.whl` | `a14618f3ff92342ab10089ccd5badd7d4be377ef37d0299e411e67480830a1aa` |

`dist`不纳入Git；正式发布时重新构建并在包外发布清单登记最终hash。

## 结论与下一步

CON-002完成。Windows LocalJournal/Outbox和量化服务事件接收端现在具有可共同执行的
机器契约、样例、幂等规则和逐条确认规则。下一项按计划实施MOCK-001：建立Mock Broker
和Mock Market，为订单状态机、异常恢复和事件生成提供确定性测试环境。
