# RSK-001 计划级策略风险检查

## 1. 组件位置

`PlanRiskEvaluator`位于TradePlan进入执行编排之前。量化服务发布的计划依次经过响应签名、
账户授权、Schema、语义和`plan_hash`校验，再由`parse_trade_plan_risk_candidate()`转换为强类型
风险输入。Runtime使用同一时点的账户资产、可用资金、持仓集合和成交额事实执行评估。

评估器是确定性纯业务组件：输入相同则规则顺序、观测值、结论和事件载荷完全相同。它不读取
环境变量、数据库、系统时钟、行情Gateway或QMT对象；调用方负责取得一致的事实快照。

## 2. 明确输入

| 输入 | 类型 | 含义 |
|---|---|---|
| `PlanRiskCandidate` | 不可变计划候选 | 计划/账户/策略、算法版本、组合声明上限和逐标的策略参数 |
| `PlanRiskContext` | 不可变账户事实 | 净资产、可用资金、当前仓位比例、已有持仓标的和逐标的窗口成交额 |
| `PlanRiskPolicy` | 不可变版本化规则 | 策略及算法白名单、四类策略版本、组合/单票/参与率/金额上限 |
| `decision_id` | 稳定风险决定ID | 关联后续`RISK_DECISION`事件与执行链 |

`Money`使用两位十进制，`Ratio`使用最多八位十进制，全部计算基于`Decimal`。计划对象只保留
风险所需字段，QMT及中心服务的原始字典不会进入风险计算核心。

## 3. 固定规则集

评估器按固定顺序输出全部规则，不在首个失败处提前结束：

| 规则 | 计算与限制 |
|---|---|
| `PLAN_ACCOUNT_MATCH` | 计划账户必须等于当前Runtime账户 |
| `PLAN_STRATEGY_ID` | `strategy_id`必须等于生效策略 |
| `PLAN_ALGORITHM_VERSION` | `source.algorithm_version`必须命中策略允许集合 |
| `PLAN_POLICY_BINDINGS` | entry、exit、risk、execution四类版本全部命中允许集合 |
| `PLAN_DECLARED_POSITION_LIMIT` | 计划声明标的数上限不超过本地生效策略 |
| `PLAN_DECLARED_EXPOSURE_LIMIT` | 计划声明总仓位上限为正且不超过生效策略 |
| `PLAN_TARGET_EXPOSURE` | 目标仓位为正且不超过“计划声明上限/生效策略上限”的较小值 |
| `PLAN_ITEM_WEIGHT_SUM` | 所有标的目标权重合计不超过组合目标仓位 |
| `PLAN_PROJECTED_POSITION_COUNT` | 已持仓与计划标的并集不超过双方位置上限较小值 |
| `PLAN_CURRENT_EXPOSURE_HEADROOM` | 当前仓位必须低于本次买入计划目标仓位 |
| `PLAN_BUY_NOTIONAL` | 逐标的最大金额合计不超过“可用资金/策略金额上限”的较小值 |
| `SINGLE_POSITION_TARGET_EXPOSURE` | 每个标的目标权重大于零且不超过单票仓位上限 |
| `SINGLE_POSITION_NOTIONAL_EXPOSURE` | 每个标的最大金额/净资产不超过单票仓位上限 |
| `MARKET_PARTICIPATION_RATE` | 每个标的最大金额/窗口成交额不超过参与率上限；成交额缺失或为零时失败关闭 |

最多六个计划标的产生29条结果，低于DomainEvent契约的32条上限。任一规则为`FAIL`时总决定为
`REJECT`；全部通过时为`ALLOW`。

## 4. 策略参数版本

`PlanRiskPolicy.policy_version`随每个风险决定写入事件。策略ID、算法版本和entry/exit/risk/
execution版本均采用精确匹配；新参数以新版本对象发布，Runtime组合根显式选择批准版本。
同一评估不接受调用方临时覆盖字段，版本对象和其中的集合均为不可变值。

具体资金、仓位和参与率数值由GOV-005批准后注入生产组合根。测试使用显式测试政策，验证规则
实现和边界，不把测试数值作为实盘批准值。

## 5. 输出与后续交互

`PlanRiskDecision`包含决定ID、计划ID、`ALLOW/REJECT`、政策版本和完整规则列表。
`to_event_payload()`生成冻结DomainEvent契约中的PLAN范围`RISK_DECISION`载荷，字段和长度已经
通过Schema与语义测试。

`ALLOW`只表示计划级策略风险通过。订单生成阶段继续使用实时资金、价格、lot和容量计算，
每个订单经过订单级风险与Windows Hard Safety，再进入订单意图持久化门。`REJECT`计划保留完整
规则证据并回传中心服务，不创建订单意图。

## 6. 已验证边界

- TradePlan hash错误、非READY计划、字段类型错误和未知策略代码明确拒绝；
- 账户、策略、算法、四类策略版本及声明上限逐项留痕；
- 既有持仓与计划标的去重后计算预计标的数；
- 当前仓位没有新增空间、资金不足、单票权重/金额超限均拒绝；
- 成交额缺失、零成交额和参与率超限均失败关闭，等于上限时通过；
- 规则计算不使用二进制浮点数；
- 风险结果载荷通过`domain-event.schema.json`及语义校验。
