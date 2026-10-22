# Mock Broker与Mock Market

## 1. 设计目的

MOCK-001为Runtime、订单状态机、风控、对账和恢复测试提供确定性的外部事实源。Mock只
模拟券商与行情适配器行为，不实现业务订单状态机、交易策略、风险规则或持久化逻辑，
确保同一套业务核心以后可以直接切换到Replay和QMT Gateway。

实现只使用内存对象和注入时钟，不使用随机数、真实网络或等待时间，因此异常用例在CI中
可重复、快速且无波动。

## 2. Broker边界

`BrokerGateway`统一提供：

- `start/stop`生命周期和Broker事件接收器；
- `submit_order`异步提交请求的本地受理回执；
- `cancel_order`异步撤单请求的本地受理回执；
- `query_orders/query_trades/find_order_by_client_id`恢复查询；
- 明确区分成功空结果、查询失败和超时。

`SubmissionReceipt`或`CancellationReceipt`只证明请求被Gateway接收，不证明券商订单已
受理、成交或撤销。最终事实来自`BrokerOrderUpdate`、`BrokerTrade`、
`BrokerCancelError`或后续查询。

### 2.1 报单场景

| 场景 | Mock行为 | Runtime应验证的行为 |
|---|---|---|
| `ACCEPT` | 返回请求序号并发送`ACKED` | 建立券商映射，等待后续状态 |
| `REJECT` | 返回请求序号并发送`REJECTED` | 不盲目重发，保留拒单原因 |
| `PARTIAL_FILL` | 发送ACK、部分成交订单更新和成交事实 | 成交量单调增加，剩余量正确 |
| `TIMEOUT_BEFORE_ACCEPT` | 无券商事实，抛出`outcome_unknown=false` | 按明确未提交处置 |
| `TIMEOUT_AFTER_ACCEPT` | 已建立ACK事实后抛出`outcome_unknown=true` | 先按`client_order_id`查询，禁止重发 |

事件交付支持`NORMAL/REVERSED/DUPLICATE/DEFERRED`。延迟事件可按正序或倒序flush，并可
逐条重复，用于验证Runtime不依赖回调到达顺序。

### 2.2 撤单场景

| 场景 | Mock行为 |
|---|---|
| `APPLY` | 未成交订单变为`CANCELLED`，部分成交订单变为`PARTIALLY_FILLED_CANCELLED` |
| `REJECT` | 订单事实不变，发送`BrokerCancelError` |
| `TIMEOUT_BEFORE_APPLY` | 不改变订单并返回明确未应用超时 |
| `TIMEOUT_AFTER_APPLY` | 先改变券商事实，再返回结果未知超时 |

终态订单不可再次撤单；未知`client_order_id/broker_order_id`明确失败。查询故障按调用维度
一次性注入`TIMEOUT`或`FAILURE`，下一次查询自动恢复，便于测试退避与补偿查询。

## 3. Market边界

`MarketGateway`提供启动、停止、单标的订阅、取消订阅和最新Tick查询。Mock Market固定：

- 同一标的重复订阅返回同一订阅，不重复占用名额；
- 默认最多50个订阅，项目运行配置仍以约15只实际标的为目标；
- 未订阅标的不会发送回调，但仍可形成最新市场事实；
- 旧Tick晚到不会覆盖较新的`latest_tick`；
- 支持立即、重复和延迟交付，延迟队列可倒序和重复flush；
- 最新Tick查询的成功空结果、失败和超时严格区分；
- stop清除旧会话订阅和待发回调，保留最新事实供重启恢复测试。

`MarketTick`统一使用`SecurityCode/Price/Quantity/Money/ShanghaiTimestamp`，限制盘口每侧
最多10档，拒绝接收时间早于行情发生时间以及非法源序号。

## 4. 使用方式

```python
from auto_trading_system.gateways.broker import MockBrokerGateway, SubmitScenario
from auto_trading_system.gateways.market import MockMarketGateway

broker = MockBrokerGateway(clock)
broker.start(runtime_broker_queue.put_nowait)
broker.queue_submit(SubmitScenario())

market = MockMarketGateway(max_subscriptions=50)
market.start(runtime_market_queue.put_nowait)
market.subscribe(symbol)
```

业务测试先排入明确场景，再调用与生产相同的Gateway端口。测试不得依据真实sleep等待Mock
事件；延迟场景通过`flush_pending()`显式推进。

## 5. 后续适配约束

- QMT适配器必须把SDK对象复制为这里定义的中立值对象后再离开回调线程。
- QMT的`None`或异常不得映射为成功空元组，必须转换成查询失败或结果不明。
- Broker适配器把映射后的委托/成交快照交给`validate_order_query_result()`和
  `validate_trade_query_result()`；只有强类型不可变tuple可作为权威空结果或事实集合。
- Replay Market实现同一个`MarketGateway`端口，但从历史Tick按虚拟时钟推进。
- Mock内部事实可直接查询，但业务模块仍只能通过Gateway端口访问。
- Mock不导入`xtquant`、SQLite、Web框架或量化服务客户端。
