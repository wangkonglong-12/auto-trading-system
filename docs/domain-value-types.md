# 领域基础类型

## 1. 目的

ATS-003为订单、风险、执行、对账和事件模块提供唯一的基础值对象。边界在对象创建时
一次性校验，后续业务逻辑只接收已合法的值，避免金额浮点误差、布尔值冒充整数、证券
代码格式漂移、无时区时间以及系统校时导致的超时判断错误。

实现位于`src/auto_trading_system/domain`，只依赖Python标准库。
Windows安装包显式锁定`tzdata`，不依赖其他组件间接提供IANA时区数据库。

## 2. 类型与固定约束

| 类型 | 内部表示 | 固定约束 | Wire表示 |
|---|---|---|---|
| `Money` | `Decimal` | 非负；最多12位整数、2位小数 | 无指数十进制字符串 |
| `Price` | `Decimal` | 大于0；最多10位整数、8位小数 | 无指数十进制字符串 |
| `Ratio` | `Decimal` | `0 <= value <= 1`；最多8位小数 | 无指数十进制字符串 |
| `Quantity` | `int` | 非负；不接受`bool`；不超过有符号64位整数上限 | JSON整数 |
| `LotSize` | `int` | 大于0；仅表达规则参数，不内置交易所规则 | 内部值对象 |
| `SecurityCode` | 六位代码 + `Market` | 严格接受`######.SH/SZ/BJ` | 规范证券代码字符串 |
| `TradingDate` | `date` | 严格`YYYY-MM-DD`；不承诺该日为开市日 | ISO日期字符串 |
| `ShanghaiTimestamp` | aware `datetime` | 内部统一`Asia/Shanghai`；Wire输入必须显式`+08:00` | ISO 8601字符串 |
| `Clock` | Protocol | 业务墙上时间入口 | `ShanghaiTimestamp` |
| `MonotonicClock` | Protocol | 间隔、超时和截止时长入口 | 单调纳秒计数 |

所有值对象不可变。输入错误统一抛出`DomainValueError`，携带稳定的`code`和`field`，
错误消息不回显被拒绝的原始值。

## 3. 使用规则

```python
from datetime import time

from auto_trading_system.domain import Money, Quantity, SecurityCode, TradingDate

budget = Money.parse("100000.00")
quantity = Quantity(100)
symbol = SecurityCode.parse("600000.SH")
entry_start = TradingDate.parse("2026-09-03").at(time(14, 50))
```

- 外部JSON和配置边界通过`parse()`创建值对象，不接受科学计数法、空格、前导加号或
  非规范格式。
- 内部精确计算使用`Decimal`构造值对象；金额分值可以用`Money.from_minor_units()`。
- `Quantity`只保证整数与存储安全。买卖手数、零股卖出、不同板块规则由后续版本化
  Market Rule/Risk Policy判断，不写死在基础类型中。
- `TradingDate`只表示计划所选日期。是否交易日、半日市或临时休市由交易日历端口判断。
- 业务代码注入`Clock`；持续时间、重试退避和超时注入`MonotonicClock`。只有组合根创建
  `SystemClock`，测试使用固定时钟。
- `Price`不负责价格档位和涨跌停校验；这些规则依赖标的和交易日，归入后续风险策略。

## 4. 精度和边界决策

`Money`使用两位小数，对应人民币分；`Price`和`Ratio`使用八位小数，与TradePlan v1.0
协议精度一致。构造过程不会自动四舍五入，超出精度直接失败，避免在风控或报单前发生
静默金额变化。需要舍入的业务场景必须由调用方依据明确、版本化的策略先完成舍入。

协议模块继续保持独立并验证原始Wire对象，领域模块不反向侵入协议Schema。协议通过后，
应用层再把字符串转换为这些值对象，后续CON-002和Runtime工作包沿用同一边界。
