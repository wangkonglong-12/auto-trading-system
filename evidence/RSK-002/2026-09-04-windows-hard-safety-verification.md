# RSK-002 Windows Hard Safety验收证据

## 验收结论

RSK-002完成。生产订单组合根已固定把Runtime状态许可与Windows Hard Safety共同接入唯一订单
持久化门。本地上限只从严格校验的`RuntimeConfig.hard_safety`构造，中心计划、策略结果和远程
命令没有替换或扩大接口。计划金额与本地金额取较小值，所有真实Broker调用前重新读取当前事实。

本地评估覆盖事实完整性、账户、行情新鲜度、时钟、活动单总数、client ID、同股同方向唯一、
单笔金额、预计持仓数、预计总/单票仓位和当日买入金额。活动买单剩余金额预占预计仓位和标的数，
防止多个未成订单分别通过后合并突破上限。

## 买卖安全语义

- 买入执行全部12条规则，任何一条失败即`REJECT`；
- 卖出继续执行账户、事实、行情、时钟、活动单、身份和单笔金额规则；
- 卖出的持仓数、总/单票仓位和当日买入规则记为`NOT_APPLICABLE`，允许降低现有风险；
- Broker/行情查询不明通过availability显式表达并失败关闭；
- Hard Safety决定可直接生成ORDER范围`RISK_DECISION`协议载荷。

## 自动化验证

2026-09-04在Windows、Python 3.12.14执行：

- 编译、Ruff、格式检查和mypy严格检查全部通过；
- 1093项测试全部通过，总覆盖率93.27%；
- 39项Hard Safety、生产组合根、真实SQLite订单门和事件契约专项测试通过；
- 中心计划单笔上限7000元、本地上限5000元时，意图写入数与Broker订单数均为零；
- 意图提交前事实健康、提交后Tick陈旧时，意图留存用于对账且Broker订单数为零；
- 活动买单剩余金额计入预计总仓位、单票仓位和持仓标的数；
- 全部数值上限等于边界时通过，超过最小精度即拒绝；
- 卖出在现有仓位超限时仍能通过仓位类规则，同时保持通用安全检查；
- Hard Safety风险载荷通过`domain-event.schema.json`与语义校验；
- sdist与dev19 wheel从隔离构建环境生成成功。

## 独立安装验证

在全新虚拟环境中从dev19 wheel安装声明依赖并执行本地单笔上限场景：

- 包版本为`0.1.0.dev19`；
- 6000元委托面对5000元本地上限时决定为`REJECT`；
- 12条规则完整输出；
- 验证环境已安全清理。

## 产物校验和

| 产物 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev19-py3-none-any.whl` | `7b2cc18344033f84c8fe318542d59c3f07df80b5c74daf632e864455b0b2b7cc` |
| `src/auto_trading_system/risk/hard_safety_models.py` | `8ac399351cbb0e5eb2c6b08e779726b88ba53499e42043901e82ebd71242ebac` |
| `src/auto_trading_system/risk/hard_safety.py` | `1511609e874e75560507cbccae93aa24320627ceb1db3270012515c1cabd343e` |
| `src/auto_trading_system/execution/hard_safety_guard.py` | `63fbe44e03c1444f74ae27afa09f37906ca3c81aed8cdc497e320a899cdc91b8` |
| `src/auto_trading_system/apps/trade_runtime/bootstrap.py` | `c21f27a49c55e31345b0d75b269c423c68ddd497bf676fc7ea216e8b3d62c9e4` |
| `tests/unit/test_hard_safety.py` | `9d041caac6727091145a065f76ce6fba47b8cf1668cd8e307c86045e07d18b7a` |
| `tests/integration/test_hard_safety_order_gate.py` | `59a6a0d2a5ab60e605efe2ea018283ab715b08b922d9552ae8f6fa6b484d4ca7` |
| `tests/contract/test_hard_safety_event.py` | `687bc23c846f95a87d4774b7a021a8f9069feeda543143787007e688cbecb406` |
| `docs/windows-hard-safety.md` | `74ab87bd191ce30275f2b97202e031e7cf27136b930758637bad4e19e40a206c` |

## 后续工作

P3真实QMT/订单/对账尚未形成前，RSK-003缺少订单状态、对账和行情新鲜度事件输入。按依赖图
转入当前可独立实施的SEC-001，先完成secret存储边界、结构化日志脱敏和最小权限基础；
目标QMT环境可访问后恢复P0/P3并补齐RSK-003动态降级。
