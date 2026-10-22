# 尾盘量化自动交易系统

本仓库承载Windows交易节点。量化服务发布冻结的TradePlan；交易节点负责QMT连接、
本地状态、订单执行、Hard Safety、对账、证据留存和本地操作台。

## 2026-10-21 个人尾盘实盘版

当前源码已将单一尾盘策略接入 `QMT_TRADING`：14:30–14:55 拉取量化后台签名计划、按计划订阅 QMT Tick、卖一保护限价买入、超时撤单及确认后重报、实际成交成本持仓、次日止盈/止损/到时卖出、跨重启最大亏损基线和策略持仓紧急清仓。页面保持首页、QMT账户、尾盘策略、交易监控、消息与日志、系统设置六个入口。交易监控逐只区分信号、买入目标、券商委托和实际成交；消息页保留本次启动内的状态变化。实盘设置保存后若需要重启，页面会直接给出操作提示。

开发期间双击 `打开自动交易系统.cmd`，脚本直接使用当前源码启动本机后台并自动打开浏览器，不需要重新打包 EXE。首次使用或配置失效时会进入完整操作台，页面显示“尚未连接 QMT”，行情订阅和全部交易操作保持锁定。在“系统设置”点击“开始连接 QMT”，配置检查通过后程序自动切换到 QMT 节点。需要单独修改接入位置时，可以双击 `打开交易设置页面.cmd`；离线演示使用 `打开模拟演示.cmd`。准备长期使用时，再通过 `打开已打包版本.cmd` 验收当前发行版。正式启用步骤见 [QMT实盘启用说明](docs/QMT实盘启用说明_20261020.md)。读取资金/持仓不等于函数交易权限已确认；最终权限仍需用户明确授权一笔小额真实报撤单测试后确认。

## 历史阶段记录

以下内容用于保留以前各阶段的开发证据，其中的“尚未接入”“仍为 MOCK”“待打包”等描述已经被上面的 2026-10-21 个人尾盘实盘版取代。当前完成状态以[完成情况与现场验收清单](docs/20261020_剩余开发清单与执行计划.md)为准。

- 2026-10-19真实QMT只读接入：用户完成股票实盘校验后，账户发现、主动状态、资金/真实持仓/当日委托及成交查询、CLOSED行情与清理均通过。新增独立SDK准备/运行脚本、有界只读命令和三个稳定session阶段；源码尚未收入dev54 EXE，正式主进程仍MOCK。后续验证交易时段和异常场景并接入主流程；见[现场实施记录](docs/design/v3.1/QMT真实只读接入实施记录_20261019.md)。

- 2026-10-19：增加 Windows 单文件离线预览 EXE `0.1.0.dev54`，内置运行环境、桌面启动器和本机页面，双击即开；默认 MOCK、零额度、只读。使用与验收边界见[EXE使用说明](docs/Windows_EXE使用说明_20261019.md)。此前 dev53 包保持原样，真实 QMT 仍需单独安装并验收。

- 最新源码复核和完整功能现状：[代码复核与完整功能清单（2026-10-15）](docs/design/v3.1/代码复核与完整功能清单_20261015.md)。已修正现场台额度显示、账单视图范围、页面请求时序及对账成交范围验证；源码修订未替换dev53发行包。完整设计中的各页面与非页面缺项已逐项列明。

- 设计基线：`docs/design/v3.1/详细设计方案.md`及V3.1 Python基线修订Word。
- 推进基线：`开发计划与推进记录.md`、`docs/design/v3.1/开发修订计划与推进记录.md`。
- 新版实现：dev53完成首版四项离线交付：正常进程模拟买卖/重启/日终同步、量化V2日记录查询页面、SAMPLE账单费用核对、发行包及跨项目贯通验收。正式策略仍待业务确认；默认配置MOCK/0额度，显式测试会话使用PAPER额度及虚构行情，每次启动重新核对授权。
- 验证、操作与剩余任务：[首版离线交付与操作说明](docs/design/v3.1/首版离线交付与操作说明_20260926.md)。真实QMT、正式策略/退出汇总、券商正式账单和现场部署单独验收；原备份等历史交付和证据保留。

以下旧阶段任务记录作为复用证据；新版完成状态以R31看板为准。

- 已完成：`ATS-001`、`ATS-002`、`ATS-003`、`CON-001`、`CON-002`、`MOCK-001`、`CON-003`、`CON-004`、`MOCK-002`、`RT-001`、`RT-002`、`RT-003`、`RT-004`、`JRN-001`、`JRN-002`、`JRN-003`、`JRN-004`、`CTRL-001`、`TEST-RT-001`、`RSK-001`、`RSK-002`、`SEC-001`、`UI-001`、`UI-005`、`UI-006`、`WD-001`、`OPS-002`、`QS-001`、`QS-002`、`QS-003`、`QS-004`、`QS-005`、`QS-006`、`QS-007`、`QS-008`、`QS-009`、`TST-001`、`TST-002`、`TST-004`、`TST-005`、`TST-008`
- 环境待办：`GOV-001`环境诊断、`QMT-001`连接探针、`QMT-002`账户快照探针和`QMT-003` Tick探针已完成，等待实际QMT机器生成并复核证据
- 当前开发项：`QMT-004`离线审批预检已完成；实盘时序仍等待目标QMT、程序化交易权限、已批准
  的单笔参数和人工值守窗口。`GOV-001/QMT-001/QMT-002/QMT-003`实际结论等待目标机证据，
  `CI-001`等待本项目提交到远端仓库
- 当前源码/EXE版本：`0.1.0.dev56`；此前 wheel 发行包：`0.1.0.dev53`。

## V3.1 独立进程入口

维护命令：`backup --destination <全新备份目录>`、`verify-backup --bundle <备份目录> --manifest-hash <备份时记录的摘要>`、`restore-backup --bundle <备份目录> --manifest-hash <摘要> --destination <全新恢复目录>`，均接在`auto-trading-node --config <绝对配置路径>`之后。备份需停止引擎和监护且同步任务空闲；恢复需要交互确认，副本保持待核验启动阻断。完整步骤及保全边界见本轮实施报告。

新版现场台：`auto-trading-node --config <绝对配置路径> console --port 8780`，浏览器访问`http://127.0.0.1:8780/`。页面与引擎独立；未配置`operator_access`时保持只读，状态失新会锁定授权。历史记录和账单继续在量化系统查询。旧V1页面入口保留。

`auto-trading-node --config <绝对配置路径> engine`启动交易进程，`agent`启动独立同步进程；`job pull-plan`、`job daily-export`、`job catch-up`由Windows任务触发。`status engine`、`status agent`、`halt`及`stop engine/agent`提供现场操作。

配置示例为`config/v2/node.mock.example.json`，使用前填写实际SID、专属数据目录、可信公钥/日历/策略版本及凭证引用，执行`init`创建全新受保护数据目录。任务部署脚本`scripts/windows/install-node-tasks.ps1`默认只生成并校验计划，显式`-Apply`才注册；本轮保持本机任务原样。缺省配置保持模拟券商和0额度；显式paper_session的正常进程模拟操作见首版说明。

可选`operator_access`配置启用独立人工授权通道。通过`recover`请求核对，等待相应状态版本完成后使用`authorize confirm-recovery`；随后`authorize arm`或`authorize resume`交互输入口令及确切短语。结果补查使用`authorization-result <request_id>`。这些命令沿用引擎持久化与额度检查；当前零额度保持原样。配置、期限和操作示例见[人工授权报告](docs/design/v3.1/本地人工授权实施报告_20260923.md)第2节。

## V3.1 离线模拟入口

持续买入及部分成交恢复：

```powershell
.\.venv\Scripts\python.exe -m auto_trading_system.apps.trade_runtime --simulate-entry-owner .codex_tmp\entry-demo
```

固定100股目标，首次成交40股并完成撤单核对；重建后保持暂停，重新授权后补60股。行情留在内存，一次目标和J1分别保存。证券步长与价格为明确Mock测试规则，实际QMT来源及长期进程接线见本轮报告。

当前主流程集成入口：

```powershell
.\.venv\Scripts\python.exe -m auto_trading_system.apps.trade_runtime --simulate-business-flow .codex_tmp\business-flow-demo
```

同一账户与Journal贯通两天：工作线程拉取并验签计划、owner加载后保持暂停；测试授权后D0买入100股，D1恢复同一批次并卖出100股；日终快照冻结上传，回执丢失后重建任务索引补查，测试接收端查询两天的成交与资产。D1退出策略结果契约待补，业务完整性明确为INCOMPLETE；账单待核，财务状态均为PROVISIONAL。

该入口固定Mock券商与内存HTTP测试接收端，使用真实本地文件/J1、HTTP签名和业务组件。独立进程及认证IPC使用上节新版入口；Windows任务现场部署、量化页面及真实QMT分别验收。

安装依赖后运行：

```powershell
.\.venv\Scripts\python.exe -m auto_trading_system.apps.trade_runtime --simulate .codex_tmp\demo
```

该入口固定使用Mock Broker及内存模拟量化接收端，完成签名计划、J1、部分成交、撤单、恢复分页和日终回执流程。
每次在指定目录下新建模拟子目录；保持真实交易关闭。本机本轮验证环境为`.codex_tmp\r31-venv\Scripts\python.exe`。
原QMT探针、V1测试与旧装配保留，真实切换按新版验收门推进。

新版引擎授权与恢复闭环：

```powershell
.\.venv\Scripts\python.exe -m auto_trading_system.apps.trade_runtime --simulate-engine .codex_tmp\engine-demo
```

该入口固定使用Mock，依次完成启动暂停、恢复证据落盘、人工测试授权、100股买入/50股成交/余量撤单、关闭并重建引擎、重新核对。重建后保持暂停，旧授权保持失效，原委托保持一笔。输出目录包含最小Journal、签名计划和`result.json`。此入口使用固定模拟交易日与测试操作员，用于离线工程验收；实际登录、IPC、行情驱动策略和QMT仍按R31工作包接线。

连续账户观测与持仓批次验收：

```powershell
.\.venv\Scripts\python.exe -m auto_trading_system.apps.trade_runtime --simulate-portfolio .codex_tmp\portfolio-demo
```

固定Mock完成两轮买入/部成/撤单，在原授权内刷新过期观测、查询补回缺失回报、处理迟到重复回报并在内核重建后恢复两个批次。此入口的T+1数量是领域计算；实际Mock SELL使用下一入口，真实交易保持关闭。

跨日封存及D1退出验收：

```powershell
.\.venv\Scripts\python.exe -m auto_trading_system.apps.trade_runtime --simulate-exit .codex_tmp\exit-demo
```

固定Mock演示D0买入100股并封存；D1开盘减仓目标50股，首单成交20股后撤单；重建内核保持暂停，重新授权后补卖30股，最终持仓50股。该入口保留dev42的计划模式适配验收；独立开盘上下文和持续策略使用下一入口。成本、开盘价、规则和日历均为明确测试数据。

持续D1策略与独立恢复记录验收：

```powershell
.\.venv\Scripts\python.exe -m auto_trading_system.apps.trade_runtime --simulate-exit-owner .codex_tmp\exit-owner-demo
```

固定Mock验证两条链路：10:30检查未减仓仍保存状态，重建后保持暂停且再次授权后保留检查结果；100股持仓先提交50股并成交20股，在途目标收紧为0，撤单确认后补卖80股，最终持仓0。此入口使用独立签名退出上下文及SELL-only授权，保持下午计划未加载。行情留在内存，离散状态和每个J1分别确认成功后才继续。生产来源适配、完整路径回放和真实QMT按R31验收。

历史容量与分片恢复验收：

```powershell
.\.venv\Scripts\python.exe -m auto_trading_system.apps.trade_runtime --simulate-journal .codex_tmp\journal-demo
```

在新建私有目录中生成3,200条合成历史和120条当日Mock拒单事实，完成3,320条分页核验，内存保留当日120条，恢复约98KB的分片检查点。重建保持暂停，原历史记录完整保留。该入口验证工程容量与存储边界，正式来源及真实交易沿原发布门验收。

## 开发环境

Windows生产候选基线为Python 3.11 x64，发布前与目标QMT/XtQuant组合完成现场PoC验证。
本轮代码质量门使用Python 3.11；开发工具也支持Python 3.12，便于运行Mock和契约测试。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements\dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

执行完整质量门：

```powershell
.\scripts\check.ps1
```

质量门要求 Python 3.11/3.12 和 Node.js 20.19.0 以上；脚本会在执行测试前校验版本。需要
指定已安装的 Node.js 时，使用 `-NodeBin <包含node.exe的目录>`，CI 固定使用 Node.js 22。

查询版本：

```powershell
.\.venv\Scripts\auto-trading-runtime.exe --version
```

## QMT目标机诊断

目标交易机先使用`scripts/diagnose-environment.ps1`生成只读、脱敏的环境JSON，冻结Python
3.11 x64、QMT、XtQuant、`userdata_mini`和函数交易队列事实；随后使用
`auto-trading-qmt-probe`采集连接、账户订阅、主动状态查询、回调和清理证据。QMT-001通过后使用
`auto-trading-qmt-snapshot-probe`验证资产、持仓、当日委托和当日成交的实际字段形状及`None`
语义，`auto-trading-qmt-tick-probe`完成五类行情场景采证。QMT-004使用离线审批预检把目标、证据、
双人复核、短时窗口和单笔金额绑定成执行hash。全部入口均随wheel发布；访问QMT的探针复用正式
Runtime的账户级Global Mutex，防止诊断会话与自动交易并发。完整命令、退出码、阻塞码和现场
证据模板见`docs/qmt-field-diagnostic.md`。

## 架构边界

依赖方向、线程规则、QMT隔离规则和持久化边界记录在
`docs/architecture-boundaries.md`。任何新模块先确认所属边界，再添加实现和测试。

## 契约

V3.1新版契约位于`contracts/v2`，包含16份Schema、9个已实现节点接口的OpenAPI以及公共测试签名向量。公共测试密钥仅用于测试；发布器拒绝该测试种子，真实签名密钥通过独立环境引用配置。量化V2接口和发布器默认关闭，程序启动不执行迁移。详见`docs/design/v3.1/续开发实施报告_20260920.md`。

旧版协议文件保留在`contracts/v1`。TradePlan及DomainEvent哈希使用UTF-8规范JSON
计算；事件批量上传、逐条回执、幂等和兼容规则见`contracts/README.md`。
资产/持仓快照、四方输入对账、Runtime心跳、远程命令及最终回执的交互设计见
`docs/node-sync-contracts.md`。
HMAC-SHA256请求/响应签名、Nonce防重放、密钥轮换及双项目测试向量见
`docs/hmac-authentication.md`。

## 配置

Windows机器配置Schema、READ_ONLY样例和secret边界位于`config/v1`，详细规则见
`config/README.md`。校验配置时只输出脱敏摘要：

```powershell
.\.venv\Scripts\auto-trading-runtime.exe --check-config <配置文件路径>
```

## 领域基础类型

金额、价格、比例、数量、证券代码、交易日和上海时区时钟统一使用不可变值对象；
边界约束和调用示例见`docs/domain-value-types.md`。业务代码不直接使用`float`表示
金额、价格或比例，也不直接调用系统墙上时钟。

## 测试Gateway

确定性的Mock Broker和Mock Market实现生产Gateway端口，覆盖受理、拒单、部分成交、
撤单、回调乱序/重复/延迟以及结果未知的超时。场景语义、恢复要求和使用方法见
`docs/mock-gateways.md`。Mock不连接QMT、不使用真实等待，并明确区分成功空查询、查询失败
和查询超时。

签名的量化服务Mock API覆盖计划、命令、节点回传、逐条事件回执、断网/超时和响应认证故障，
设计与使用方式见`docs/mock-central-api.md`。该模块只供测试组合根使用，生产Runtime不导入。

## Runtime可靠性基础

账户级Win32 Global Mutex在QMT、LocalJournal和Worker启动前阻止同一券商账户双开；锁名只包含
账户摘要，启动、故障和恢复规则见`docs/runtime-single-instance.md`。

双通道有界入口把QMT回调和Worker消息安全送入唯一决策任务；关键事件拥有预留容量，满载先触发
安全降级，全部业务状态变更串行执行，具体规则见`docs/runtime-decision-loop.md`。

Runtime状态机固定启动/重连回到READY_PAUSED，按本地、Hard Safety、Watchdog、券商事实、中心和
策略优先级收紧权限；AUTO要求健康、对账及本地/中心双重许可，详见`docs/runtime-state-machine.md`。

磁盘低水位、Journal不可写、Writer停滞/失败、队列饱和和组件崩溃统一进入失败关闭策略；
报单在持久化前及Broker调用前分别读取当前权限，故障矩阵与恢复要求见
`docs/runtime-fault-injection.md`。

唯一进程内Scheduler只发布控制事件，计划/命令同步、快照、回传和盘后维护不会创建第二QMT
会话；账户锁、Journal、决策循环、Broker、Worker、Scheduler和本地API的启动/停止顺序见
`docs/runtime-lifecycle-scheduler.md`。

独立Watchdog先发现并附着已启动的Runtime，随后用PID匹配的原子心跳监控存活与磁盘；崩溃或
失联时先写外部HALT证据，再按指数退避受控拉起，连续崩溃进入熔断。任何Watchdog拉起都只能
进入HALT/READY_PAUSED恢复链，协议与参数见`docs/runtime-watchdog.md`。

Windows任务部署使用版本化启动清单和两个专用用户登录任务：Runtime立即启动，Watchdog延迟30秒；
每次触发重新验证绝对路径、参数边界和Runtime/Watchdog二进制SHA-256。安装预览、正式注册、失败
回滚、现场核验和安全卸载步骤见`docs/windows-task-scheduler.md`。

LocalJournal端口冻结了计划、命令、订单意图、券商标识、成交去重、领域事件、Outbox、本地控制、
恢复快照和维护能力；事务与SQLite适配器边界见`docs/local-journal-contract.md`。

订单意图持久化门是生产代码中唯一允许调用Broker报单的路径；意图和事件提交失败时Broker调用
次数为零，重复意图及报单后未决状态必须先对账，详见`docs/order-intent-persistence-gate.md`。

Durable Outbox按最多50条批量上传本地业务事实，经过签名响应的逐条回执才允许ACK；断网、重启、
响应认证失败和任务取消均保留待传数据，指数退避和高水位安全信号见
`docs/durable-outbox.md`。

本地Override和Emergency Halt由决策循环内的控制协调器应用，请求/结果分别持久化并生成审计
事件；急停优先锁HALT，解除限制要求二次确认、健康、对账和本地许可，重启恢复规则见
`docs/local-control.md`。本地HTTP提交、状态回执、Journal重建和Uvicorn生命周期见
`docs/local-control-api.md`。

计划级策略风险检查把认证后的READY TradePlan转换为强类型输入，按账户、仓位、单票、资金、
参与率及策略版本输出完整`RISK_DECISION`；规则、事实口径和后续执行门见
`docs/plan-risk-admission.md`。

Windows Hard Safety从本机配置建立不可远程放宽的最终订单上限，并与Runtime权限固定组合到
生产订单门；活动买单预占仓位，持久化前后两次检查及卖出语义见`docs/windows-hard-safety.md`。

## 本地安全边界

真实委托凭据存入专用Windows服务用户的Credential Manager，交互式安装命令不接收命令行明文；
结构化日志、异常栈和故障包使用同一运行时敏感值注册表完成最终输出脱敏。SQLite、日志、Tick、
备份和诊断目录在启动时收紧为当前服务用户与LocalSystem访问，ACL失败时在连接QMT前终止启动。
Git候选文件、私钥和当前开发环境已知secret纳入统一质量门。实现与运维步骤见
`docs/local-security.md`。

## Windows本地操作台

操作台使用Vue 3、TypeScript、Vite、Naive UI和ECharts，生产静态资源随Runtime Wheel发布，
由FastAPI在`127.0.0.1`同源托管。十个一级路由、固定运行身份区、全局急停入口、Hash路由、
安全响应头和前后端统一质量门已经建立。六类安全控制使用操作员短会话、CSRF、服务端影响预览、
逐字确认和最终回执，详细边界见`docs/windows-operator-console.md`与
`docs/operator-console-security.md`。
