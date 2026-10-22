# RSK-001 计划级策略风险检查验收证据

## 验收结论

RSK-001完成。Windows交易节点已建立确定性、无副作用的计划准入风险组件。认证且通过Schema、
语义和hash检查的READY TradePlan会转换为强类型候选，与账户资产、可用资金、当前仓位、持仓
标的和窗口成交额事实共同评估。任一规则失败时决定为`REJECT`，不会进入订单意图创建。

风险政策显式携带版本，精确绑定strategy、algorithm、entry、exit、risk和execution版本；
组合仓位、单票仓位、预计标的数、计划金额和市场参与率使用Decimal规则计算，并输出完整
PLAN范围`RISK_DECISION`证据。

## 固定规则

- 计划账户、策略ID、算法版本和四类策略参数版本；
- 计划声明标的数及总仓位上限；
- 目标仓位、逐标的权重合计和当前仓位剩余空间；
- 已持仓与计划标的并集后的预计标的数；
- 逐标的最大金额合计与可用资金/政策金额较小值；
- 单票目标仓位和最大金额/净资产仓位；
- 最大金额/窗口成交额参与率，成交额缺失或为零时失败关闭。

所有规则均执行并留痕，最多六个计划标的产生29条规则，符合DomainEvent最多32条限制。

## 自动化验证

2026-09-04在Windows、Python 3.12.14执行：

- 编译、Ruff、格式检查和mypy严格检查全部通过；
- 1056项测试全部通过，总覆盖率93.66%；
- 36项计划风险和DomainEvent契约专项测试通过；
- TradePlan hash错误、非READY、类型错误和未知策略代码明确拒绝；
- 账户、组合仓位、单票仓位、资金、持仓数、参与率及全部版本边界通过；
- 参与率缺少成交额、零成交额、超过上限和等于上限的行为均已冻结；
- 六标的最大规则数量、确定性结果及Decimal精度通过；
- 风险决定载荷通过`domain-event.schema.json`和语义校验；
- sdist与dev18 wheel从隔离构建环境生成成功。

## 独立安装验证

在全新虚拟环境中从dev18 wheel安装声明依赖并构造一套计划、政策和账户事实：

- 包版本为`0.1.0.dev18`；
- 风险决定为`ALLOW`；
- 单标的产生14条完整规则；
- 验证环境已安全清理。

## 产物校验和

| 产物 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev18-py3-none-any.whl` | `b54373017f36e97701455e67fce7e55c42619c67c18417d0356bbb26c2232f74` |
| `src/auto_trading_system/risk/models.py` | `5f9b6725db6966dedc681f2e5d50f8862d61639f37eb6259307ae62f7848efa8` |
| `src/auto_trading_system/risk/trade_plan.py` | `93f8f9168418d405a451f3b0f7efdf44a99298b9a38bfe7e2f3d54b8188fb4b5` |
| `src/auto_trading_system/risk/plan_evaluator.py` | `4c34862ff1f3e8ee8b68fb1c920bad3d2ba6ab493063f0d7e427304a69c164d1` |
| `tests/unit/test_plan_risk.py` | `2c7e28e64b4cf56e503d59567ef76fb3dc308fa88ddb58acec849b92b70decd5` |
| `tests/contract/test_plan_risk_event.py` | `3f880058b5ef9e77706e0bba4c05f33081036e9ea22b1e4a83a6db009e1eafaa` |
| `docs/plan-risk-admission.md` | `1ca8f1e5287fb0f029c168bfe38941dfdf2052fa83daffd748c55c7bbbb05337` |

## 后续工作

直接进入RSK-002，使用本机RuntimeConfig中已有Hard Safety上限形成独立于中心计划的最终本地
安全门，并验证中心计划、策略结果和命令均无法扩大这些限制。
