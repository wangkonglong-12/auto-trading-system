# QMT目标机现场诊断规范

## 1. 目标与边界

`scripts/diagnose-environment.ps1`用于完成`GOV-001`的技术环境采集，并为`QMT-001`
只读PoC提供输入。脚本在Windows PowerShell 5.1中运行，采集以下事实：

- Windows版本和架构；
- Python解释器版本、位数、可执行文件SHA-256和XtQuant导入位置；
- QMT安装候选、关键二进制版本和SHA-256；
- QMT目录内XtQuant包的有界文件指纹；
- `userdata_mini`和`up_queue_xtquant`是否存在；
- 技术就绪结论、稳定阻塞码和后续人工证据清单。

该脚本只读取本机文件、注册表、进程路径和Python模块信息。它不创建QMT会话，不连接
券商，不订阅行情，也不调用查询、撤单或报单接口。脚本不会读取账户号和
`userdata_mini`内部业务文件；计算机名转换为16位不可逆指纹，用户名和用户目录会替换为
环境变量标记。

## 2. 输入与输出

| 参数 | 要求 | 用途 |
|---|---|---|
| `-Python` | 指向目标Python的绝对路径，推荐显式提供 | 验证版本、64位和XtQuant可导入性 |
| `-QmtRoot` | QMT/MiniQMT安装根目录绝对路径 | 有界查找关键二进制与XtQuant目录 |
| `-UserdataMini` | 实际`userdata_mini`绝对路径 | 验证函数交易队列入口 |
| `-OutputPath` | 已存在父目录下的绝对JSON路径 | 原子写入可归档证据 |
| `-RequireReady` | 开关 | 技术条件不完整时返回退出码2 |

脚本始终把完整JSON写到标准输出。指定`-OutputPath`时，同一JSON以UTF-8无BOM形式原子
写入文件；已有文件会被原子替换，不会留下半写入结果。

自动发现仅使用卸载注册表、QMT相关运行进程和各磁盘根目录的第一层目录。显式QMT根目录
采用宽度和深度均有上限的遍历：每个根目录最多访问300个目录、记录32个关键二进制，
每个XtQuant目录最多取128个代码/动态库文件生成指纹。扫描跳过`userdata_mini`、日志、
缓存、历史行情和下载目录。

## 3. 目标机执行步骤

在目标Windows交易机使用与QMT相同的登录用户执行。QMT客户端应处于已安装状态；若券商
客户端只在启动后生成`up_queue_xtquant`，先正常登录并开启函数交易，再运行诊断。

```powershell
$pythonPath = "C:\QMT-Python311\python.exe"
$qmtPath = "C:\BrokerQMT"
$userdataPath = "C:\BrokerQMT\userdata_mini"
$evidencePath = "C:\AutoTradingEvidence\qmt-environment.json"

& .\scripts\diagnose-environment.ps1 `
    -Python $pythonPath `
    -QmtRoot $qmtPath `
    -UserdataMini $userdataPath `
    -OutputPath $evidencePath `
    -RequireReady
$diagnosticExitCode = $LASTEXITCODE
Get-FileHash -LiteralPath $evidencePath -Algorithm SHA256
```

退出码`0`表示五项技术检查全部满足；退出码`2`表示JSON已经生成，但仍存在技术阻塞项；
其他非零退出码表示输入路径、证据目录或脚本执行本身失败。

## 4. 自动判定标准

| JSON检查字段 | 通过条件 | 对应阻塞码 |
|---|---|---|
| `python_3_11_x64_found` | 至少一个CPython 3.11、64位解释器探针成功 | `PYTHON_3_11_X64_NOT_FOUND` |
| `qmt_installation_found` | QMT候选目录存在且含QMT关键二进制或XtQuant包 | `QMT_INSTALLATION_NOT_FOUND` |
| `xtquant_found` | Python可定位`xtquant`，或QMT根目录中发现XtQuant包 | `XTQUANT_NOT_FOUND` |
| `userdata_mini_found` | 至少一个`userdata_mini`目录存在 | `USERDATA_MINI_NOT_FOUND` |
| `function_trading_queue_found` | `up_queue_xtquant`入口存在 | `FUNCTION_TRADING_QUEUE_NOT_FOUND` |

`technical_ready=true`仅代表环境文件条件满足。真实交易准入还需要完成模板中的人工证据，
以及`GOV-002`至`GOV-007`审批。

## 5. 证据归档

现场人员使用`evidence/GOV-001/target-machine-template.md`记录以下信息：

1. 诊断JSON文件及其SHA-256；
2. QMT客户端版本、安装包SHA-256、安装路径和回滚包SHA-256；
3. XtQuant来源、版本和包SHA-256；
4. Python 3.11 x64版本及可执行文件SHA-256；
5. 脱敏账户尾号、账户类型、函数交易权限和行情权限审批编号；
6. 程序化交易合规结论、现场执行人和复核人。

完整账户号、口令、验证码、密钥、会话文件和`userdata_mini`业务内容不进入项目证据库。

## 6. QMT-001只读API采证

环境JSON技术检查通过、券商账户凭据已写入Windows Credential Manager后，使用
`auto-trading-qmt-probe`执行QMT-001。执行器依据迅投官方
[XtQuant交易模块](https://dict.thinktrader.net/nativeApi/xttrader.html)限定为以下调用顺序：

```text
获取 Global OS Mutex(account_id)
→
构造 XtQuantTrader(userdata_mini, session_id)
→ register_callback(callback)
→ start()
→ connect()
→ subscribe(StockAccount)
→ query_account_status()
→ unsubscribe(StockAccount)
→ stop()
→ 释放 Global OS Mutex(account_id)
```

执行器的公开客户端接口只包含上述连接、账户订阅、状态查询和清理动作，没有报单、撤单、
资产、持仓、委托或成交查询方法。回调线程只复制目标账户的`account_type/status`或断线信号到
内存证据收集器。

探针与正式Runtime复用完全相同的账户级Global Win32 Mutex。目标账户已有Runtime或另一探针时，
本次执行直接返回`ACCOUNT_INSTANCE_ALREADY_RUNNING`，不会构造`XtQuantTrader`、连接QMT或生成
可能被误判为有效的报告。现场必须先受控停止该账户Runtime，并保证同一稳定Session的两个进程
连续`connect()`间隔超过3秒；探针执行期间不得启动Runtime。

现场配置使用完整`RuntimeConfig v1.0`，并固定：

| 配置 | 现场要求 |
|---|---|
| `profile` | `READ_ONLY` |
| `qmt.userdata_mini` | 已通过环境诊断的绝对路径 |
| `qmt.session_id` | 为该节点冻结的稳定正整数 |
| `qmt.account_type` | 首期固定`STOCK` |
| `security.broker_account_id` | 指向Windows Credential Manager中的规范引用 |

执行器只解析`broker_account_id`引用，不读取本次诊断不需要的HMAC和操作员密码凭据。完整券商
账户仅在进程内构造`StockAccount`；报告只保存不可逆指纹和至少四位掩码。

先用隐藏输入写入账户凭据：

```powershell
auto-trading-credential.exe `
    --target AutoTrading/node-prod-01/broker-account `
    --kind broker-account
```

然后使用新的时间戳文件名生成不可变报告：

```powershell
$configPath = "C:\ProgramData\AutoTrading\config\runtime.qmt-readonly.json"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$reportPath = "C:\AutoTradingEvidence\qmt-readonly-${timestamp}.json"

auto-trading-qmt-probe.exe `
    --config $configPath `
    --output $reportPath `
    --observation-seconds 30 `
    --query-interval-milliseconds 1000
$probeExitCode = $LASTEXITCODE
if (Test-Path -LiteralPath $reportPath) {
    Get-FileHash -LiteralPath $reportPath -Algorithm SHA256
}
```

| 退出码 | 结论 |
|---:|---|
| `0` | `connect=0`、`subscribe=0`，主动查询命中目标账户且`status=0`，清理成功 |
| `2` | 账户单实例锁已被占用，或连接、订阅、断线、目标账户缺失、状态未达到OK形成明确现场阻塞 |
| `3` | 单实例保护、SDK兼容、返回类型、回调结构、清理或证据写入发生技术错误 |

报告使用`qmt-readonly-probe/v1`，记录阶段耗时、数值返回码、查询/回调状态序列、断线次数、
SDK版本和稳定失败码。回调状态0不能替代主动查询状态0。输出路径必须是不存在的新绝对JSON
路径；执行器使用同目录临时文件、`fsync`和排他原子发布生成报告，已有现场证据不会被覆盖。
执行器等待QMT清理结束并成功释放账户锁后才发布报告；QMT清理失败会形成`ERROR`报告，账户
锁释放失败则拒绝发布，避免把所有权状态不明的会话结果误判为有效证据。

## 7. QMT-002账户快照字段采证

QMT-001报告经复核为`PASS`后，等待同一Session前次连接超过3秒，再使用独立的
`auto-trading-qmt-snapshot-probe`。该执行器重新完成账户锁、连接、订阅和主动状态确认，仅在
目标账户为OK时按顺序调用迅投官方只读接口：

```text
query_stock_asset(account)
→ query_stock_positions(account)
→ query_stock_orders(account, False)
→ query_stock_trades(account)
```

每类查询后再次主动确认目标账户为OK；断线、账户状态变化或回调结构异常立即停止后续查询并
进入受控清理。字段基线为：

| 数据集 | 必需字段 | 探测字段 |
|---|---|---|
| 资产 | `account_type/account_id/cash/frozen_cash/market_value/total_asset` | `fetch_balance/current_balance` |
| 持仓 | `account_type/account_id/stock_code/volume/can_use_volume/open_price/market_value/frozen_volume/on_road_volume/yesterday_volume/avg_price` | 无 |
| 委托 | `account_type/account_id/stock_code/order_id/order_sysid/order_time/order_type/order_volume/price_type/price/traded_volume/traded_price/order_status/status_msg/strategy_name/order_remark/direction/offset_flag` | 无 |
| 成交 | `account_type/account_id/stock_code/order_type/traded_id/traded_time/traded_price/traded_volume/traded_amount/order_id/order_sysid/strategy_name/order_remark/direction/offset_flag` | 无 |

报告只记录每个字段是否存在、Python类型、空值/异常/非有限数/负数计数、数据集记录数和重复身份
计数。完整账号、证券代码、订单号、成交号、文本和全部资金/数量/价格原值均不写入报告。
`query_stock_asset()`返回`None`明确记为查询失败；持仓、委托或成交返回`None`记为“失败或空”的
歧义结果，绝不转换为空集合。只有实际空`list`才记录为`EMPTY_LIST`。

```powershell
$configPath = "C:\ProgramData\AutoTrading\config\runtime.qmt-readonly.json"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$reportPath = "C:\AutoTradingEvidence\qmt-snapshot-${timestamp}.json"

auto-trading-qmt-snapshot-probe.exe `
    --config $configPath `
    --output $reportPath
$probeExitCode = $LASTEXITCODE
if (Test-Path -LiteralPath $reportPath) {
    Get-FileHash -LiteralPath $reportPath -Algorithm SHA256
}
```

| 退出码 | 单次执行结论 |
|---:|---|
| `0` | 四类查询均返回结构可判定的对象/列表，查询前后账户保持OK，清理成功 |
| `2` | 单实例锁冲突、连接/订阅/账户状态阻塞、断线或任一`None`使结果不可判定 |
| `3` | SDK调用、容器/字段/类型/身份、回调、清理、锁释放或证据写入错误 |

单次退出码0只说明该场景采证成功，不代表QMT-002完成。现场必须分别覆盖：真实空账户、真实有
持仓、当日有委托/成交、断线、校正中`status=5`和收盘后`status=6`；同时对照QMT界面确认字段
单位、时间编码和金额精度。所有场景由执行人与复核人签署后，才冻结生产映射。

## 8. QMT-003 Tick字段与单位采证

QMT-001现场报告通过后，使用`auto-trading-qmt-tick-probe`执行独立XtData采证。执行器调用
`subscribe_quote(code, period='tick', count=0, callback=...)`、`get_full_tick(code_list)`和
`unsubscribe_quote(subscription_id)`；单次支持1至15个规范证券代码，订阅号大于0才进入快照和
有界回调观察。

字段采集规则如下：

| 字段组 | 字段 | 采集结果 |
|---|---|---|
| 时间 | `time` | Python类型和秒/毫秒/微秒量级候选 |
| 价格 | `lastPrice/open/high/low/lastClose` | 类型、空值、非有限数和负数计数 |
| 成交 | `amount/volume/pvolume` | 类型及`pvolume ÷ volume`整数倍率候选 |
| 状态 | `stockStatus` | 类型和实际状态码集合 |
| 五档/多档 | `askPrice/bidPrice/askVol/bidVol` | 容器/元素类型、最小与最大深度、价量档位不一致计数 |
| 版本可选 | `openInt/lastSettlementPrice/transactionNum` | 出现性、类型和有效性 |

报告用证券代码SHA-256前16位指纹关联同一标的，只保留字段形状、计数、量级和单位倍率候选；
完整证券代码、价格、金额、数量及原始Tick载荷不进入报告。ACTIVE、LIMIT_UP和LIMIT_DOWN场景
要求每个标的至少收到一条有效回调；HALTED和CLOSED场景以有效最新快照为必需条件，并如实记录
回调是否出现。

```powershell
$configPath = "C:\ProgramData\AutoTrading\config\runtime.qmt-readonly.json"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$reportPath = "C:\AutoTradingEvidence\qmt-tick-active-${timestamp}.json"

auto-trading-qmt-tick-probe.exe `
    --config $configPath `
    --output $reportPath `
    --symbol "600000.SH" `
    --scenario ACTIVE `
    --observation-seconds 30
$probeExitCode = $LASTEXITCODE
if (Test-Path -LiteralPath $reportPath) {
    Get-FileHash -LiteralPath $reportPath -Algorithm SHA256
}
```

| 退出码 | 单次执行结论 |
|---:|---|
| `0` | 订阅、最新快照、场景要求的回调和全部反订阅均完成，字段结构有效 |
| `2` | 订阅返回失败、最新快照缺标的，或要求实时回调的场景未收到每标的一条回调 |
| `3` | SDK调用、回调/快照结构、字段类型、档位配对、清理、账户锁或证据写入错误 |

现场分别执行ACTIVE、HALTED、LIMIT_UP、LIMIT_DOWN和CLOSED五类场景；每类报告与QMT界面、
交易所行情和券商版本三元组交叉复核。只有五类场景签署完成后，才冻结生产`MarketTick`映射、
时间换算、成交量换算、档位深度与陈旧行情阈值。官方接口基线：
`https://dict.thinktrader.net/nativeApi/xtdata.html`和
`https://dict.thinktrader.net/innerApi/data_structure.html?id=7zqjlm`。

## 9. QMT-004实盘PoC审批预检

QMT-004使用`ASSISTED`配置和`auto-trading-qmt-order-preflight`执行实盘前离线预检。预检读取
`config/v1/qmt-order-poc-approval.example.json`对应的封闭审批文件，把目标机、券商账户、
QMT-001/002/003证据hash、证券、方向、数量、限价、金额上限、撤单期限、有效窗口、执行人、
独立复核人和变更单绑定为一个`execution_binding_sha256`。该入口不导入XtQuant；报告固定记录
`collection_mode=OFFLINE_APPROVAL_VALIDATION`和`live_order_submitted=false`。

审批文件遵循以下硬约束：

| 项目 | 规则 |
|---|---|
| 文件完整性 | 审批JSON使用封闭字段集；命令接收变更单中独立记录的完整文件SHA-256 |
| 执行目标 | 目标机使用GOV-001指纹；账户使用QMT-001报告的`target_account_fingerprint`；预检时均与实际输入恒定时间比较 |
| 单次授权 | 32位十六进制`single_use_nonce`绑定本次执行；revision固定为1 |
| 订单边界 | 明确一个证券、BUY/SELL方向、正整数数量、正限价和审批金额上限 |
| 本机上限 | 订单名义金额同时满足审批上限与Runtime `max_single_order_notional` |
| 时间边界 | 审批窗口为1至30分钟，撤单期限为5至120秒 |
| 人员责任 | 执行人与独立复核人身份不同，并绑定变更单号和1至24位ASCII委托备注 |
| 安全确认 | 人工全程值守、QMT急停可用、只执行单笔订单三项确认均为true |

现场先由执行人填写审批文件，由独立复核人核对QMT-001/002/003现场结论、账户、标的、价格和
金额，并将审批文件SHA-256登记到批准的变更单。执行人从变更单取得该hash后运行：

```powershell
$configPath = "C:\ProgramData\AutoTrading\config\runtime.assisted.json"
$approvalPath = "C:\AutoTradingEvidence\qmt004-approval.json"
$approvalHash = "从已批准变更单复制的64位小写SHA-256"
$hostFingerprint = "GOV-001报告中的16位host_fingerprint"
$reportPath = "C:\AutoTradingEvidence\qmt004-preflight-$(Get-Date -Format 'yyyyMMdd-HHmmss').json"

auto-trading-qmt-order-preflight.exe `
    --config $configPath `
    --approval $approvalPath `
    --approval-sha256 $approvalHash `
    --host-fingerprint $hostFingerprint `
    --output $reportPath
$preflightExitCode = $LASTEXITCODE
Get-FileHash -LiteralPath $reportPath -Algorithm SHA256
```

| 退出码 | 单次执行结论 |
|---:|---|
| `0` | 审批hash、目标绑定、金额上限和时间窗口全部通过，产生唯一执行绑定hash |
| `2` | Profile、配置、账户凭据或目标/账户/金额/时间条件阻断 |
| `3` | 审批格式、hash、输入时钟、隐私检查或不可变证据写入错误 |

报告保留审批编号、人员、变更单、批准的订单数值和前置证据hash；完整账户、证券代码、
一次性nonce和委托备注使用省略或指纹形式。预检报告及其hash登记完成后，项目负责人另行批准
QMT-004现场实盘执行窗口。

## 10. QMT-004单笔报单与撤单调试

源码提供独立的`auto-trading-qmt-command-probe`和`run-qmt-command-probe.ps1`。该入口只执行
审批文件绑定的一笔限价委托：启动前再次检查账户状态、资金或可卖数量以及重复备注；异步请求
序号只记录为“已发出请求”。随后用券商委托查询取得order_id并核对证券、方向、数量、价格和
备注。达到审批中的撤单等待时间仍未终态时，只申请一次撤单并继续查询。任何超时、断线、回报
冲突或查询不确定都返回`UNKNOWN`，禁止自动重发。

运行时必须使用已经验证的QMT专用Python和SDK目录，不能使用普通项目测试环境：

```powershell
./scripts/run-qmt-command-probe.ps1 `
  -EnvironmentRoot 'E:/workspace/auto-trading-system/.codex_tmp/qmt-readonly-verified-20261019' `
  -Config 'C:/ProgramData/AutoTrading/config/runtime.assisted.json' `
  -Approval 'C:/AutoTradingEvidence/qmt004-approval.json' `
  -ApprovalSha256 '<批准文件的64位SHA-256>' `
  -HostFingerprint '<GOV-001的16位主机指纹>' `
  -Output 'C:/AutoTradingEvidence/qmt004-command-result.json' `
  -Execute
```

`-Execute`、`ASSISTED`配置、有效审批窗口、目标主机、账户指纹、本机金额上限和未使用nonce缺一
不可。结果JSON使用不可覆盖的原子写入。`ORDER_TERMINAL`只说明委托查询已到券商终态；结果仍
固定标记`manual_reconciliation_required=true`，需在MiniQMT中核对委托、成交和资金持仓。
该入口不会把`QMT_READ_ONLY`会话升级为交易会话，也不会自动启动策略。

## 11. 后续现场顺序

技术检查通过并完成GOV人工证据后，按以下顺序推进：

1. `QMT-001`：运行上述执行器，复核连接、订阅、状态序列、断线和清理证据；
2. `QMT-002`：按上述场景矩阵运行账户快照探针并冻结查询行为；
3. `QMT-003`：按上述五场景运行Tick探针并冻结时间戳、volume单位、档位和特殊行情行为；
4. `QMT-004`：先完成离线审批预检，再在审批的小资金账户和人工值守窗口内执行单笔最小
   报单/撤单PoC；
5. `QMT-005`至`QMT-010`：完成回调、故障、无人值守、账单和兼容矩阵验收。

技术环境诊断本身不替代上述QMT API现场证据。
