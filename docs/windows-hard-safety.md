# RSK-002 Windows Hard Safety

## 1. 最终本地安全边界

Windows Hard Safety是Broker调用前的最终机器本地风险边界。生产组合根从经过严格Schema校验的
`RuntimeConfig.hard_safety`复制不可变`HardSafetyLimits`，并固定组合以下两层执行许可：

1. `RuntimeOrderExecutionGuard`检查当前Runtime模式和买卖权限；
2. `HardSafetyOrderExecutionGuard`读取最新账户/行情事实并重新计算本地硬上限。

生产`build_order_intent_gate()`直接完成上述组合，调用方不能传入中心计划或远程命令提供的
替代上限。中心可以发布计划和受控状态命令，但实际委托始终使用Windows配置值。

## 2. 本地事实与上限

`HardSafetyFacts`是同一决策时点的不可变快照：账户、Broker/行情事实可用性、净资产、持仓
总市值、逐标的市值、活动订单剩余金额、当日已占用买入金额、Tick年龄和绝对时钟偏差。

事实对象强制满足：

- 逐标的市值合计严格等于总持仓市值；
- 持仓标的唯一，活动订单`client_order_id`唯一；
- 净资产为正，金额和时间值有限且非负；
- 查询不明通过`broker_facts_available/market_data_available=false`显式表达。

`HardSafetyLimits`完整承载本机`max_positions`、总/单票仓位、活动订单数、同股同方向活动单、
单笔金额、当日买入金额、Tick年龄和时钟偏差上限，政策版本固定写入风险决定。

## 3. 每笔委托固定规则

每次评估输出12条规则：

| 规则 | 计算方式 |
|---|---|
| `HARD_FACTS_AVAILABLE` | Broker与行情事实均为AVAILABLE |
| `HARD_ACCOUNT_MATCH` | 委托账户等于Runtime账户 |
| `HARD_MARKET_FRESHNESS` | Tick年龄不超过本地秒数上限 |
| `HARD_CLOCK_SKEW` | 绝对时钟偏差不超过本地秒数上限 |
| `HARD_ACTIVE_ORDER_COUNT` | 当前活动单加本次委托不超过本地总数上限 |
| `HARD_CLIENT_ORDER_ID_UNIQUE` | 本次`client_order_id`不在活动委托中 |
| `HARD_SYMBOL_SIDE_ACTIVE_ORDER` | 同标的同方向活动委托加本次不超过1 |
| `HARD_SINGLE_ORDER_NOTIONAL` | 价乘量不超过“计划单笔上限/本地单笔上限”的较小值 |
| `HARD_PROJECTED_POSITION_COUNT` | 持仓、未成买单和本次买入涉及标的并集不超过本地上限 |
| `HARD_PROJECTED_TOTAL_EXPOSURE` | 持仓市值、全部未成买单和本次买入之和/净资产不超过总仓位上限 |
| `HARD_PROJECTED_SINGLE_EXPOSURE` | 单票持仓、该票未成买单和本次买入之和/净资产不超过单票上限 |
| `HARD_DAILY_BUY_NOTIONAL` | 已占用当日买入金额加本次买入不超过本地日上限 |

活动买单的`remaining_notional`会预先占用预计总仓位、单票仓位和标的数，防止多个尚未成交的
委托分别检查通过、合并成交后突破上限。当日买入金额口径由调用方包含当日已提交委托，避免
活动委托在日限额中重复计算。

卖单继续执行账户、事实完整性、行情、时钟、活动委托、身份和单笔金额检查；仓位数、总/单票
仓位和当日买入规则以`NOT_APPLICABLE`通过，使降低风险的卖出不因现有超仓而被阻断。可卖数量
和T+1约束由订单/退出模块继续检查。

## 4. 与持久化门的竞态处理

订单意图门在持久化前执行一次完整组合检查，在LocalJournal提交成功后、调用Broker前再次执行。
两次检查分别读取当前Runtime状态和最新Hard Safety事实：

- 首次失败返回`ORDER_EXECUTION_NOT_ALLOWED`，不写订单意图，Broker调用为零；
- 等待持久化期间事实恶化时，第二次失败返回`ORDER_EXECUTION_PERMISSION_REVOKED`，已落盘意图
  保留并进入对账，Broker调用仍为零；
- Hard Safety拒绝原因保存在`HardSafetyViolation.decision`，包含完整规则证据。

计划声明金额只能通过较小值进一步收紧本地单笔金额。中心计划、策略决定和远程恢复命令均无
接口修改`HardSafetyLimits`；改变本地上限必须更新受控RuntimeConfig并按配置发布流程重启。

## 5. 风险事件

`HardSafetyDecision.to_event_payload()`生成DomainEvent中ORDER范围的`RISK_DECISION`载荷，包含
稳定决定ID、订单ID、`ALLOW/REJECT`、`WINDOWS_HARD_SAFETY_V1`和12条规则。载荷已通过冻结
Schema和语义验证。后续订单编排把最终检查决定与OrderIntent置于同一可追踪链路。

## 6. 已验证边界

- 真实SQLite/WAL意图门与Mock Broker组合验证前后两次Hard Safety检查；
- 中心计划给出7000元单笔上限、本机固定5000元时，在写意图前拒绝且Broker订单为零；
- 意图提交前事实健康、提交后Tick陈旧时，意图保留用于对账且Broker订单为零；
- 未成买单金额纳入预计总仓位、单票仓位和标的数；
- 查询不明、行情陈旧、时钟偏差、活动单、重复client ID、单笔/日金额逐项失败关闭；
- 数值等于本地上限时通过，超过最小精度即拒绝；
- 卖出保持通用安全检查，并允许在现有仓位超限时降低风险。
