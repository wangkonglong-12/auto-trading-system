# CON-003 节点同步契约验收证据（2026-09-04）

## 交付范围

- 共享协议类型和错误结构Schema。
- 资产快照和持仓快照Schema，明确成功、查询失败和结果未知三态。
- 含资产、持仓、委托、成交四类输入的完整对账报告Schema。
- Runtime心跳Schema，覆盖连接、队列、Journal、Outbox、WAL、磁盘、时钟和版本。
- 类型/作用域受约束的远程命令、递增命令批次及最终结果Schema。
- 标准协议错误信封Schema。
- 13个成功、降级、查询失败、差异、不可知和拒绝样例。
- Python语义验证器及跨字段、时间、顺序、身份和安全前置条件测试。

## 固定协议语义

- `AVAILABLE`必须携带查询数据且禁止`error`；只有成功查询的空持仓数组才表示空仓。
- `QUERY_FAILED/UNKNOWN`必须携带`error`且禁止资产/持仓数据；QMT的歧义`None`使用`UNKNOWN`。
- 对账必须各有一项`ASSET/POSITION/ORDER/TRADE`输入；任一不可知则整体为`INDETERMINATE`。
- 对账汇总从差异明细推导，`MATCHED`禁止差异，`DISCREPANCIES`要求完整输入和至少一项差异。
- `AUTO`心跳要求QMT连接、账户OK、行情健康和Journal健康；中心短暂不可达可由Outbox补传。
- 命令按`command_seq`递增、以`command_id`幂等并受TTL和类型/作用域约束。
- `REQUEST_AUTO_RESUME`仅是申请，中心命令不能解除本地HALT或绕过Hard Safety。
- 命令HTTP下发不等于生效，最终以CommandResult和`CONTROL_COMMAND_APPLIED`领域事件为准。
- 业务快照、对账和命令结果使用Durable Outbox；心跳只发送最新状态，不积压历史心跳。

## 验证结果

| 验证项 | 结果 |
|---|---|
| JSON Schema Draft 2020-12自校验 | 8类消息Schema和1份公共定义全部通过 |
| 协议样例 | 13个正常/错误/降级样例全部通过Schema和语义验证 |
| 必需错误用例 | 查询错误、协议错误、未知major、缺字段、未知字段全部覆盖 |
| 精度和时间 | 金额/价格拒绝float，协议时间强制`+08:00`，交易日绑定通过 |
| Ruff | 通过 |
| Ruff格式一致性 | 64个文件通过 |
| mypy strict | 64个源文件无问题 |
| pytest | 526项全部通过 |
| 分支覆盖率 | 98.94%，门槛90% |
| CON-003新增测试 | 136项；Schema、查询三态、对账、心跳、命令、回执和错误均覆盖 |
| sdist/wheel构建 | 通过，版本`0.1.0.dev5` |
| 锁定依赖环境安装及版本查询 | 通过，输出`auto-trading-runtime 0.1.0.dev5` |
| 安装后节点消息冒烟测试 | 公共验证器可从wheel导入并验证协议错误信封 |

## 固定哈希

| 文件 | SHA-256 |
|---|---|
| `contracts/v1/common.schema.json` | `2534fb4a034d8903e1e354465c40dd9f6d40adad874f4ef22c5ad471681e680c` |
| `contracts/v1/asset-snapshot.schema.json` | `46faf128804638f062a93d087973d89610c6b063194b989fe0c7a344cd1351b2` |
| `contracts/v1/position-snapshot.schema.json` | `a443820a5bd4b36f0212ac8b2344c800d88a2d3fbaca521cfe1aedaaf7a52ebf` |
| `contracts/v1/reconciliation-report.schema.json` | `b70577c93988bf4d878554482697cfb68d105c6c3d2f24433672009e27eee267` |
| `contracts/v1/runtime-heartbeat.schema.json` | `dbfc11acfa993c1afd2204ae65928bc30aba87344ac32ebd5438988e9fba0d33` |
| `contracts/v1/runtime-command.schema.json` | `87cfba5f0c558cc6c23bd2b90fb711f66f7ebd37ff0cff614c8f7638e161094a` |
| `contracts/v1/runtime-command-batch.schema.json` | `99766709e289a7899b1718296c934e1dbe1099d0557f22c64b0e3db398a190b6` |
| `contracts/v1/runtime-command-result.schema.json` | `c483af8b600c6e5ca484fa10c9f764dac532e271bd9d7d386a76cbfa47062032` |
| `contracts/v1/protocol-error.schema.json` | `2bb61a52643a79fa32462d61779b1588b0a082ec875549bda933156afc5ec662` |
| `src/auto_trading_system/protocol/node_messages.py` | `814127f806105bd26d297fbfe101388e045763bc9a7942badfcd80e75fd5c97d` |
| `docs/node-sync-contracts.md` | `4c6ec1effa546662b18df165346ba56d1516859afa984ce0f0ab64c4d611c3ac` |
| `dist/auto_trading_system-0.1.0.dev5-py3-none-any.whl` | `ab63329637a30200b33e7485e7538b4b9cbda9b0fd2ea9cb62ae39967d30c7fb` |

`dist`不纳入Git；正式发布时重新构建并在包外发布清单登记最终hash。

## 结论与下一步

CON-003完成。Windows与量化服务对资产、持仓、对账、健康和控制已具备共同机器契约，
并能区分“明确为空”和“事实不可知”。下一项直接实施CON-004，冻结HMAC签名、响应验签、
时间窗口、Nonce防重放和跨项目测试向量。
