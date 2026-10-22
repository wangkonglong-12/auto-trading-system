# QMT/XtQuant 项目接口提取与实现约束 v1.0

> 来源：迅投知识库 `https://dict.thinktrader.net/`，提取日期：2026-08-13。  
> 范围：只保留尾盘量化自动交易系统首期需要的 XtTrader、XtData 能力和工程约束。  
> 说明：本文是项目实施映射，不替代随券商 QMT 客户端交付的实际 SDK、版本说明和权限说明。

## 1. 提取结论

1. Windows 交易节点必须以长期运行进程连接 MiniQMT；Windows Task Scheduler 只负责开机/登录后拉起和进程守护，不负责用多个临时脚本分别连接 QMT。
2. 计划、交易事件和账单数据均由 Windows 主动访问量化服务：计划定时拉取；实时事件通过 Outbox 周期回传；盘后完整查询并回传对账快照。
3. XtTrader 交易连接需要主动重连。`connect()` 是一次性连接，断线不会自动恢复。
4. 首期使用异步报单 `order_stock_async()`。返回的 `seq` 只是请求关联号，不表示柜台已受理，更不表示成交；必须等待异步回报、委托推送、成交推送或主动查询。
5. 回调线程只做字段复制、基础校验和入队。官方虽然提供“宽松返回时序”，但开启后查询结果和推送顺序会变得不确定，因此不得在回调里执行同步查询、HTTP、数据库写入或交易决策。
6. `query_stock_orders/trades/positions()` 返回 `None` 时，官方定义包含“查询失败或结果为空”，不能直接解释为空仓/无委托/无成交。自动交易必须结合连接状态、账号状态、重试和交叉查询判断；无法确认时禁止新增买入。
7. MiniQMT 的 `order_remark` 最多 24 个英文字符，一个中文约占 3 个字符。只写短追踪令牌，完整映射保存在 LocalJournal 和中央账本。
8. 行情只订阅计划候选、当前持仓和必要指数。官方建议单股订阅不超过 50；本项目约 15 只，适合使用 `subscribe_quote()`，无须全市场转发。
9. 完整 Tick 不写 LocalJournal。Runtime 只维护内存滑动窗口；报单、风控拒绝、撤单、成交、异常时保存小型行情证据快照。需要完整回放时建设独立 MarketData Recorder。
10. 券商版 QMT 可能落后于官网 XtQuant 包。必须固定“QMT客户端版本 + xtquant版本 + Python版本”，先在实际券商环境做 PoC，再冻结生产依赖。

## 2. 启动、连接与订阅

### 2.1 官方接口

| 能力 | XtQuant 接口 | 官方关键语义 | 项目实现要求 |
|---|---|---|---|
| 创建实例 | `XtQuantTrader(path, session_id)` | `path` 指向 `userdata_mini`；不同会话 ID 不得重复；通常只创建一个实例 | 一个交易账号只允许一个活动 Runtime 和一个 Trader 实例；使用 OS Mutex 防双开 |
| 注册回调 | `register_callback(callback)` | 注册 `XtQuantTraderCallback` 实例 | 必须在 `start/connect/subscribe` 前完成 |
| 启动交易线程 | `start()` | 准备交易运行环境 | 启动失败进入 `HALT`，不得继续连接/下单 |
| 连接 | `connect()` | 成功返回 0，失败非 0；断开后不会自动重连 | 连接失败或 `on_disconnected` 后进入 `RECONNECTING/READY_PAUSED`；退避重连，成功后重新订阅并完整 Reconcile |
| 订阅账号 | `subscribe(account)` | 成功 0、失败 -1；订阅资金、委托、成交和持仓信息 | `connect=0` 且 `subscribe=0` 才算交易通道就绪 |
| 账号状态 | `query_account_status()` / `on_account_status()` | `OK=0`，另有连接中、登录中、失败、校正中、收盘后、停用等状态 | 只有 `ACCOUNT_STATUS_OK` 可新增买入；校正中、失败、停用、连接中均暂停 |
| 停止 | `stop()` | 停止 API | 受控退出：先停止新增意图，等待/处置活动订单，再停止 |

### 2.2 固定启动顺序

```text
校验配置/密钥/磁盘
→ 获取 OS Mutex(account_id)
→ 打开 LocalJournal 并做健康检查
→ 创建 XtQuantTrader(path, stable_session_id)
→ 注册 Callback
→ start()
→ connect() == 0
→ subscribe(account) == 0
→ query_account_status() == OK
→ 查询资产/持仓/当日委托/当日成交
→ 与 LocalJournal、中央账本执行 Reconcile
→ READY_PAUSED
→ 经人工确认或受控 AUTO_RESUME 后进入 AUTO
```

### 2.3 Session ID 规则

- Session ID 使用账号和节点配置派生的稳定整数，不要每次启动随机生成。
- 同一 Session 的两个 Python 进程连续 `connect()` 间隔必须超过 3 秒。
- 只有在确认无旧进程、稳定 Session 确实不可用时，才允许从预留范围选取备用值，并记录 Incident。
- 不频繁更换 Session，避免 `userdata_mini` 产生大量 `down_queue` 文件。

## 3. 报单、撤单与回调

### 3.1 首期接口映射

| 业务动作 | XtQuant 接口 | 返回/回调 | 项目状态处理 |
|---|---|---|---|
| 异步股票报单 | `order_stock_async(account, code, type, qty, price_type, price, strategy_name, remark)` | 立即返回 `seq>0` 或 `-1`；正常请求随后触发 `on_order_stock_async_response` | `seq>0` 仅记为 `SUBMIT_REQUESTED`；拿到 `order_id` 后才建立券商映射；最终以委托/成交状态为准 |
| 同步股票报单 | `order_stock(...)` | 返回 `order_id>0` 或 `-1` | 只用于 PoC/人工诊断；首期 Runtime 不作为主路径 |
| 异步撤单 | `cancel_order_stock_async(account, order_id)` | 返回撤单 `seq`；失败可触发 `on_cancel_error` | 返回成功只表示请求发出；必须等委托状态变为已撤/部撤，或查询确认 |
| 合同号撤单 | `cancel_order_stock_sysid_async(account, market, order_sysid)` | 返回撤单 `seq` | 本地 `order_id` 不可靠或恢复场景下的补充手段 |
| 委托推送 | `on_stock_order(XtOrder)` | 委托状态、已成数量、柜台合同号等变化 | 转为内部 `BROKER_ORDER_UPDATE`，按状态机单调推进并允许重复/乱序 |
| 成交推送 | `on_stock_trade(XtTrade)` | 成交编号、价格、数量、金额、订单号、合同号 | `account_id + trade_date + traded_id` 幂等；写 Fill 和 Outbox |
| 报单失败 | `on_order_error(XtOrderError)` | `order_id/error_id/error_msg/remark` | 转 `REJECTED` 或 `ERROR_HOLD`；保存原始错误文本，不按未知错误自动重试 |
| 撤单失败 | `on_cancel_error(XtCancelError)` | `order_id/order_sysid/error_id/error_msg` | 查询当前委托状态后决定继续等待、重撤或人工处理 |
| 断线 | `on_disconnected()` | 无参数 | 立即停止新增报单；活动委托标记“待查询”，由非回调线程启动重连与 Reconcile |

### 3.2 QMT 委托状态映射

| QMT状态 | 值 | 内部建议状态 | 是否终态 |
|---|---:|---|---|
| `ORDER_UNREPORTED` 未报 | 48 | `SUBMITTING` | 否 |
| `ORDER_WAIT_REPORTING` 待报 | 49 | `SUBMITTING` | 否 |
| `ORDER_REPORTED` 已报 | 50 | `ACKED` | 否 |
| `ORDER_REPORTED_CANCEL` 已报待撤 | 51 | `CANCEL_PENDING` | 否 |
| `ORDER_PARTSUCC_CANCEL` 部成待撤 | 52 | `PARTIAL_FILL_CANCEL_PENDING` | 否 |
| `ORDER_PART_CANCEL` 部撤 | 53 | `PARTIALLY_FILLED_CANCELLED` | 是 |
| `ORDER_CANCELED` 已撤 | 54 | `CANCELLED` | 是 |
| `ORDER_PART_SUCC` 部成 | 55 | `PARTIALLY_FILLED` | 否 |
| `ORDER_SUCCEEDED` 已成 | 56 | `FILLED` | 是 |
| `ORDER_JUNK` 废单 | 57 | `REJECTED` | 是 |
| `ORDER_UNKNOWN` 未知 | 255 | `UNKNOWN/ERROR_HOLD` | 否；必须查询/人工结论 |

状态更新规则：

- 不根据到达顺序覆盖状态；根据内部合法迁移和累计成交量应用更新。
- `FILLED`、`CANCELLED`、`PARTIALLY_FILLED_CANCELLED`、`REJECTED` 不被更早状态回退。
- `traded_volume` 只允许增加；部分成交后的剩余量为 `order_volume - traded_volume`。
- `status_msg` 原样保存，尤其是废单原因。

### 3.3 订单标识和备注

官方可用标识：

- `seq`：异步请求关联号，仅进程/会话内追踪请求与异步响应。
- `order_id`：QMT 系统订单编号，用于查询和撤单。
- `order_sysid`：券商柜台合同编号。
- `traded_id`：成交编号，作为成交事实去重核心。
- `strategy_name`、`order_remark`：可回传到委托/成交结构。

项目内部仍必须生成：

- `intent_id`：量化计划中某次交易意图的稳定 ID。
- `client_order_id`：每次实际报单的全局唯一 ID。
- `remark_token`：最多 24 个 ASCII 字符的短令牌，例如 `A1B2C3D4E5F6G7H8`。

映射链：

```text
plan_id → intent_id → client_order_id → remark_token
                                      ↘ seq → order_id → order_sysid → traded_id
```

## 4. 查询、账单和对账

| 数据 | XtQuant接口 | 关键字段 | 调用时点 |
|---|---|---|---|
| 资产 | `query_stock_asset(account)` | `cash/frozen_cash/market_value/total_asset`；新版可能含 `fetch_balance/current_balance` | 启动、执行前、关键事件、15:10 |
| 当日委托 | `query_stock_orders(account, cancelable_only=False)` | `order_id/order_sysid/order_status/order_volume/traded_volume/status_msg/remark` | 启动、重连、活动单轮询、收尾、15:10 |
| 当日成交 | `query_stock_trades(account)` | `traded_id/traded_time/traded_price/traded_volume/traded_amount/order_id/order_sysid` | 启动、重连、回调缺口补偿、15:10 |
| 持仓 | `query_stock_positions(account)` | `volume/can_use_volume/frozen_volume/on_road_volume/yesterday_volume/avg_price` | 启动、买卖前后、T+1卖出前、15:10 |
| 单股持仓 | `query_stock_position(account, code)` | 单只 `XtPosition` | 具体股票卖出门和异常复核 |
| 账号状态 | `query_account_status()` | `status` | 启动、重连、周期健康检查 |
| 数据导出/查询 | `export_data/query_data` | 官网为通用导出能力，具体 `data_type` 取决于版本/券商 | 仅作为账单增强 PoC，不作为首期核心依赖 |

### 4.1 `None` 的安全处理

- `query_stock_asset()` 返回 `None`：官方明确为查询失败。
- 委托、成交、持仓列表返回 `None`：官方描述同时代表失败或空列表，存在歧义。
- 因此不得用 `None` 证明“无委托”“无成交”或“空仓”。
- 处理流程：确认交易连接 → 确认账号状态 OK → 最多 3 次退避重试 → 必要时单项查询/回调缓存/上一快照交叉验证 → 仍不明确则 `READY_PAUSED` 或 `MANUAL`。

### 4.2 Windows 向量化服务回传

实时路径：

```text
QMT Callback
→ 快速复制并入内部队列
→ Runtime 状态机
→ LocalJournal 事务：事实记录 + Outbox
→ SyncWorker 定时 POST /api/v1/trading/events/batch
→ 服务端按 event_id 幂等
```

盘后路径：

```text
15:10 定时触发
→ query_stock_asset
→ query_stock_positions
→ query_stock_orders
→ query_stock_trades
→ 与 LocalJournal/中央投影比对
→ 上传资金/持仓快照、当日委托/成交和 reconciliation 结论
```

官网 `export_data/query_data` 不能直接等同于正式券商交割单。是否能导出满足财务/合规口径的完整账单，需要用实际券商客户端和 `data_type` 做 PoC；首期以资产、持仓、委托、成交结构化查询为核心。

## 5. 行情接口与 Tick 策略

### 5.1 首期使用接口

| 场景 | XtData接口 | 项目用法 |
|---|---|---|
| 单股实时 Tick | `subscribe_quote(code, period='tick', count=0, callback=...)` | 订阅当天候选、当前持仓、必要指数；返回订阅号大于0成功，-1失败 |
| 1分钟行情 | `subscribe_quote(code, period='1m', count=...)` / `get_market_data_ex()` | VWAP、成交额、趋势和回放辅助 |
| 当前全推快照 | `get_full_tick(code_list)` | 执行前报价/档位快照；不依赖全市场订阅 |
| 取消订阅 | `unsubscribe_quote(seq)` | 标的退出计划和持仓后释放 |
| 交易日历 | `get_trading_calendar()` 等 | Windows本地交易日二次校验；最终仍以量化计划 trade_date 和券商状态共同判断 |

官方建议单股订阅不超过 50，本项目最多约 15 只，使用单股订阅更简单、资源边界更明确。

### 5.2 Tick 核心字段

- `time`
- `lastPrice`
- `open/high/low/lastClose`
- `amount/volume/pvolume`
- `stockStatus`
- `askPrice/bidPrice`
- `askVol/bidVol`
- `transactionNum`（可用性依版本/权限）

### 5.3 本地保存策略

- 内存中保留每只股票最近 N 秒/N 条 Tick，用于新鲜度、价格保护、买卖盘和成交额判断。
- LocalJournal 不保存连续完整 Tick 流。
- 下列事件保存行情证据快照：生成订单意图、通过/拒绝风控、实际报单、撤单、成交、滑点异常、停牌/行情陈旧。
- 若需要研究/回放完整 Tick，单独部署 Recorder；官方说明 Level-2 无历史存储、跨交易日清理，更不能依赖它做系统恢复。

## 6. 线程与稳定性约束

1. QMT 回调不得写 SQLite、不得访问量化服务 HTTP、不得做同步查询、不得执行策略计算。
2. 回调只构造不可变内部事件并入有界队列；队列接近上限即告警，溢出时停止新增交易并主动查询补偿。
3. 不依赖 `set_relaxed_response_order_enabled(True)` 解决线程问题。官方说明开启后会造成查询结果与推送时序不确定；首期保持关闭。
4. 连接断开后：本地立即 `PAUSE_BUY` → 独立重连协调器退避调用 `connect()` → 重新 `subscribe()` → 查询四类事实 → Reconcile → 人工/受控恢复。
5. 官网“交易接口重连”示例明确声明不是线程安全的，只能参考重连动作，不能原样用于实盘。
6. 报单前先将 OrderIntent 和 Outbox 在 LocalJournal 中提交成功，再调用 QMT；持久化失败禁止新增报单。
7. QMT查询超时/异常与空结果必须区分；接口本身不能区分的，系统按“不确定”处理。

## 7. 版本与权限基线

- 官网当前页面显示 XtQuant 支持 64 位 Python 3.6–3.12；版本下载页中较新包还标注了 Python 3.13 支持。生产环境必须以实际下载包和券商客户端为准。
- XtQuant 版本页有明确的“已知缺陷、暂不提供下载”版本，禁止只按发布日期取最新包。
- 官网提醒券商版升级可能落后，历史上提供过公版兼容包；必须验证实际券商端兼容性。
- MiniQMT 启动时需使用极简模式；连接路径为客户端安装目录下 `userdata_mini`。
- `userdata_mini` 若没有 `up_queue_xtquant`，可能意味着账号未开通函数下单权限，需要联系券商。
- 若客户端安装在 C 盘可能出现写权限问题；建议安装到独立目录并用专用 Windows 账号运行，不建议日常使用管理员权限规避设计问题。

## 8. 必做现场 PoC 清单

1. 固定实际 QMT 客户端、XtQuant、Python 三个版本，验证导入、连接和登录。
2. 确认账号函数下单权限、A股普通账户类型和 `userdata_mini` 路径。
3. 连接/订阅返回值、账号状态从登录到 OK 的耗时和状态序列。
4. 异步报单完整序列：`seq → async response → order push → trade push`，验证真实到达顺序、重复和延迟。
5. 同一订单部分成交、多笔成交、撤单、部撤、废单和未知状态。
6. 拔网、重启 MiniQMT、重启 Runtime、Session 冲突、同账号双进程。
7. `query_*` 在真实空账户、断线、校正中、收盘后的返回差异。
8. `order_remark` 截断规则，验证 24 个 ASCII 字符短令牌可完整回传到委托和成交。
9. Tick字段、档位数量、volume单位、时间戳、停牌/涨跌停情况下的返回。
10. `export_data/query_data` 可用的 `data_type`、字段、时间范围及其是否等同券商正式账单。
11. QMT无人值守登录、断线重登、Windows锁屏/RDP断开后的持续运行能力。
12. 真实交易费率、最小委托单位、价格精度、涨跌停价、撤单频率等券商差异。

## 9. 官方页面索引

- 快速开始：`https://dict.thinktrader.net/nativeApi/start_now.html`
- XtData 行情模块：`https://dict.thinktrader.net/nativeApi/xtdata.html`
- XtTrader 交易模块：`https://dict.thinktrader.net/nativeApi/xttrader.html`
- 完整实例：`https://dict.thinktrader.net/nativeApi/code_examples.html`
- 常见问题：`https://dict.thinktrader.net/nativeApi/question_function.html`
- XtQuant 版本下载与变更：`https://dict.thinktrader.net/nativeApi/download_xtquant.html`

