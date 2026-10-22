# MOCK-001 Mock Gateway验收证据（2026-09-04）

## 交付范围

- `BrokerGateway`端口及订单、成交、撤单错误、中立状态和值对象。
- 确定性`MockBrokerGateway`，覆盖受理、拒单、部分成交、撤单成功/失败和超时。
- `MarketGateway`端口及订阅、Tick、盘口中立值对象。
- 确定性`MockMarketGateway`，覆盖订阅上限、重复订阅、旧Tick、延迟和查询故障。
- 正常、倒序、重复和延迟事件交付模式。
- 报单/撤单的“超时前未应用”与“超时后结果未知”两类语义。
- Mock设计说明、生产适配约束以及单元测试异常矩阵。

## 固定行为与恢复语义

- Mock只模拟外部Broker/Market事实，不承载订单状态机、策略、风控或持久化。
- 所有场景由测试显式排队，无随机数、真实网络和真实等待，结果可重复。
- `SubmissionReceipt/CancellationReceipt`只表示Gateway已接收请求，最终状态来自回调或查询。
- `TIMEOUT_AFTER_ACCEPT`与`TIMEOUT_AFTER_APPLY`标记`outcome_unknown=true`，Runtime必须先查询，禁止盲目重发。
- 成功空查询返回空元组；查询失败与超时使用不同异常，不把不可知状态伪装为“没有数据”。
- Broker回调支持倒序、重复和延迟交付；Market旧Tick不会覆盖最新事实。
- stop清理旧会话回调与订阅，但Broker事实和最新行情事实可供重启恢复测试查询。
- QMT适配器以后必须复制SDK字段到相同的中立值对象，并保持同一Gateway端口。

## 验证结果

| 验证项 | 结果 |
|---|---|
| Ruff | 通过 |
| Ruff格式一致性 | 61个文件通过 |
| mypy strict | 61个源文件无问题 |
| pytest | 390项全部通过 |
| 分支覆盖率 | 98.74%，门槛90% |
| MOCK-001新增测试 | 81项；Broker/Market模型、生命周期、场景、查询和交付模式均覆盖 |
| Broker场景 | 受理、拒单、部分成交、撤单成功/拒绝、终态撤单、两类报单/撤单超时通过 |
| 事件交付 | 正常、倒序、重复、延迟、显式flush通过 |
| Market场景 | 幂等订阅、订阅上限、旧Tick、未订阅、查询失败/超时通过 |
| sdist/wheel构建 | 通过，版本`0.1.0.dev4` |
| 锁定依赖环境安装及版本查询 | 通过，输出`auto-trading-runtime 0.1.0.dev4` |
| 安装后Mock Gateway冒烟测试 | Broker/Market均可从wheel导入、启动和停止 |

## 固定哈希

| 文件 | SHA-256 |
|---|---|
| `src/auto_trading_system/gateways/broker/mock.py` | `8d91d3164b64289f15574868c1c52811874fbd4cb84b797ac18fb494be9edbf6` |
| `src/auto_trading_system/gateways/market/mock.py` | `8351e439a3378684f7cd5eba1c85296bacc3531ffe3538901ec283bb622610d4` |
| `docs/mock-gateways.md` | `010479b2e01883e6ce34c546b91dfc91af0fb1cdf2be25f68022599c48baf679` |
| `dist/auto_trading_system-0.1.0.dev4-py3-none-any.whl` | `f68a8341eaab5f80a67d9e1decf86335f0bbc2d10001deb86a5443f720073406` |

`dist`不纳入Git；正式发布时重新构建并在包外发布清单登记最终hash。

## 结论与下一步

MOCK-001完成。订单状态机、恢复、风控和对账后续可以在没有QMT的开发环境中稳定复现
外部成功、拒绝、乱序、重复和不可知超时。下一项按依赖顺序实施CON-003，冻结资产、
持仓、对账、心跳和命令协议。
