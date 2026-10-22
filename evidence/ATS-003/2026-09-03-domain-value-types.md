# ATS-003 领域基础类型验收证据（2026-09-03）

## 交付范围

- `Money`：非负人民币金额，`Decimal`存储，最多两位小数。
- `Price`：严格正价格，`Decimal`存储，最多八位小数。
- `Ratio`：闭区间`[0, 1]`比例，`Decimal`存储，最多八位小数。
- `Quantity`与`LotSize`：拒绝`bool`和非整数，限制在有符号64位存储范围内。
- `SecurityCode`与`Market`：严格规范化`######.SH/SZ/BJ`。
- `TradingDate`：严格ISO日期，不把交易所开市判断混入基础类型。
- `ShanghaiTimestamp`：Wire值强制显式`+08:00`，内部统一`Asia/Shanghai`。
- `Clock`与`MonotonicClock`端口、生产`SystemClock`实现。
- 统一`DomainValueError(code, field, message)`，不回显被拒绝的原始值。

## 固定设计边界

- 金额、价格和比例不接受`float`，外部边界只接受规范十进制字符串。
- 超出小数位数直接失败，不执行隐式舍入。
- 数量只表达完整整数，不写死买入手数、零股卖出等会随市场和交易日变化的规则。
- 证券代码只表达协议/QMT共同使用的规范标识，不推断证券品种。
- 交易日期只表达计划日期；交易日历属于后续独立端口。
- 墙上时间与单调时间分离，防止系统校时影响超时、退避和持续时间判断。
- Windows显式依赖并锁定`tzdata`，不依赖调度库的传递依赖提供时区数据库。
- `domain`包仅依赖Python标准库；`protocol`保持独立的原始Wire校验边界。

## 验证结果

| 验证项 | 结果 |
|---|---|
| Python字节码编译 | 通过 |
| Ruff | 通过 |
| Ruff格式一致性 | 46个文件通过 |
| mypy strict | 46个源文件无问题 |
| pytest | 229项全部通过 |
| 分支覆盖率 | 97.86%，门槛90% |
| ATS-003新增边界测试 | 112项；Decimal、整数、代码、日期、时区和时钟均覆盖 |
| sdist/wheel构建 | 通过，版本`0.1.0.dev2` |
| 清洁环境安装及版本查询 | 通过，输出`auto-trading-runtime 0.1.0.dev2` |
| 安装后领域类型冒烟测试 | 通过，金额、数量、证券代码和上海时钟可用 |

## 构建产物

| 文件 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev2-py3-none-any.whl` | `f7db19a9ea13ab25620bdf5aeef153fea09427e85653e9e9a7a5be59101719ae` |
| `requirements/runtime.lock` | `b284c0c57f0b0702b9e6119bf1b279c836e636e3e005eb1ed0fd8430835da341` |
| `requirements/dev.lock` | `aa336ef272379c980a27a1a4889afeeeac69b389c30d9cb564474ab734e073b0` |

`dist`不纳入Git；正式发布时重新构建，并在包外发布清单记录最终产物hash。

## 结论与下一步

ATS-003完成。订单、风险、执行、对账和事件模块具备统一、不可变的数值及时间边界。
下一项按计划实施CON-002：冻结事件封套和事件类型Schema，并使用本批的领域类型完成应用层
转换。实际交易所手数、价格档位、涨跌停和交易日历规则在对应风险/市场规则工作包中结合
目标QMT环境证据落地。
