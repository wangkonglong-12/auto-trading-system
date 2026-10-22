# 架构边界

## 当前有效边界：V3.1（2026-10-22校准）

新版组件遵循`docs/design/v3.1/详细设计方案.md`与当前实施报告。下文第1至4节保留V1兼容路径的约束；其Outbox、实时回传与单进程装配描述仅适用于仍保留的V1实现，不用于扩大V3.1 Journal或替代新版验收。

- engine单一所有者维护类型化盘中状态；最小Journal采用APSW单写者，只保存关键提交、授权与恢复事实。报单经J1确认与权限复核；Tick与普通同步任务进入各自内存/文件路径。
- agent拥有HTTPS、不可变计划/日终文件与独立有界任务索引，通过本机控制端口提交加载请求。同步代码不打开最小SQLite、不调用Broker、不授予交易权限。当前端口尚待命名管道/ACL及正式独立进程接线。
- `protocol/v2`采用Pydantic严格载荷和cryptography签名；两项目共享语义通过AST一致性测试验证。HTTP、MySQL、QMT依赖位于各自适配层。
- 日终八类数据最终写入量化既有MySQL专属表组；历史、账单、对账在量化系统展示。Windows Task Scheduler主动拉计划和日终回传；正式任务装配仍待验收。
- 量化V2路由、发布器默认关闭，使用专属线程与连接额度；迁移为显式运维动作，默认离线计划。原量化选股/研究业务文件、全局配置与启动入口保持隔离。
- `runtime/engine_v2.py`拥有新版恢复、授权和提交编排状态；`runtime/recovery_v2.py`负责纯核对规则，`control/approval_v2.py`负责一次性确认凭证，`execution`负责J1协调与SDK权限边界。所有SDK查询/报撤单共用一个工作线程，Journal保持独立单写者，owner轮询已完成结果。
- `orders/projection.py`按独立委托状态、唯一成交和撤单请求归约；`reconcile/position_lots.py`承担纯持仓批次/T+1/数量预占计算；`runtime/observation_v2.py`交叉核验账户、订单与持仓。engine串行发布核验通过的观测，恢复/授权组件保持原职责。持续账户观测只更新缓存，补查的新关键券商事实先经Journal确认。
- `reconcile/portfolio_checkpoint.py`校验本账户Journal内的跨日封存及历史J1前缀；`execution/sell_policy.py`校验单批次目标、T+1、来源及可卖量；`strategies/exit_execution.py`将纯规则结果转换为候选子单。SELL绑定与目标保存在同一次J1，重启后的授权继续由engine单独管理。具体语义与兼容边界见ADR-R31-002。
- 当前源码版本为`0.1.0.dev56`，运行能力以`README.md`顶部当前状态为准；`0.1.0.dev42`及仅Mock组合入口的描述属于2026-09-21历史验证边界。源码接入`QMT_TRADING`不等于实盘准入，程序化交易权限、小额真实报撤单、交易时段与异常场景仍须单独验收。

## 1. 模块职责

| 模块 | 职责 | 允许依赖 |
|---|---|---|
| `apps` | 进程入口、生命周期和组合根 | `runtime`、`protocol`及端口接口 |
| `runtime` | 单一决策循环、状态机和业务调度 | `protocol`、领域模块及端口接口 |
| `protocol` | 跨项目契约、版本兼容、规范JSON和哈希 | Python标准库 |
| `domain` | 金额、价格、比例、数量、证券代码、交易日和时钟基础类型 | Python标准库 |
| `gateways` | Broker/Market端口及QMT、Mock、Replay适配器 | `domain`、`protocol`和对应端口 |
| `journal` | LocalJournal端口、SQLite/WAL适配器和Outbox | `protocol`及领域值对象 |
| `orders`、`risk`、`reconcile`、`strategies` | 纯业务规则和状态转换 | `protocol`及领域值对象 |
| `execution` | 决策循环内的执行用例与持久化门 | `domain`、`protocol`、Broker最小端口和LocalJournal最小端口 |
| `control` | 本地Override、Emergency Halt、结果审计和重启恢复投影 | `runtime`、`journal`、`domain`和`protocol` |
| `recorder` | 独立Tick副本队列和Parquet/ZSTD写入 | Market事件值对象 |
| `sync` | 签名HTTP传输、Durable Outbox投递、重试和背压信号 | `protocol`、`journal`端口、`domain`和HTTPX |
| `monitoring` | 结构化日志、指标、健康和证据导出 | 只读状态接口 |
| `config` | 版本化配置Schema、环境Profile、secret引用与脱敏加载 | `protocol`和Pydantic |
| `security` | Git敏感值检查、Windows目录DACL和安全存储初始化 | Python标准库与操作系统命令边界 |
| `testing` | 仅测试使用的中心API、Broker和Market确定性替身 | `protocol`、FastAPI和测试适配器；生产组合根禁止导入 |

## 2. 固定运行模型

1. `trade-runtime`是唯一交易核心进程和实时状态所有者。
2. 每个交易账户对应一个OS Mutex、一个Runtime和一个XtQuantTrader会话。
3. QMT回调线程完成字段复制和线程安全入队；Runtime事件循环串行处理状态变化。
4. Persistence Writer是LocalJournal唯一写入者，并以短事务返回提交结果。
5. Sync Worker通过签名HTTPS REST拉取计划和命令、上传事件和快照。
6. Recorder Worker消费独立Tick副本队列并写Parquet/ZSTD。
7. Operator Console调用`127.0.0.1`上的Control/Application API。

## 3. 强制依赖规则

1. `xtquant`导入集中在`gateways/broker/cicc_qmt.py`、
   `gateways/broker/cicc_qmt_snapshot.py`、`gateways/broker/qmt_command_transport.py`和
   `gateways/market/qmt.py`。
2. `sqlite3`导入集中在`journal/sqlite.py`。
3. `fastapi`和`uvicorn`导入集中在`apps`的本地API组合根；`testing`允许构造仅用于测试的
   FastAPI中心服务替身，但生产组合根不得导入`testing`。
4. `protocol`和`domain`保持可移植，仅使用Python标准库；协议负责跨项目Wire格式，领域值对象负责节点内部强类型边界。
5. 业务模块通过端口调用Gateway和LocalJournal，测试使用Mock或Replay适配器。
6. 每个状态改变产生领域事件；真实报单前先持久化OrderIntent并取得提交成功结果。
7. 业务模块不得直接读取环境变量或凭据存储；敏感值只通过`config`的SecretProvider边界解析。
8. 金额、价格和比例不得使用`float`；业务模块通过注入的`Clock`读取时间，通过`MonotonicClock`计算超时。
9. 生产组合根在打开LocalJournal、启动Worker或连接QMT前，必须取得真实券商账户对应的Global
   OS Mutex，并持有到Runtime完全停止。
10. `DecisionEventLoop`是实时业务状态唯一所有者；QMT回调和所有Worker只向双通道有界入口
    发布不可变消息。队列溢出先触发安全降级，Handler失败后Runtime失败关闭。
11. `RuntimeStateMachine`只能绑定决策循环的所有者断言；部署Profile是执行能力上限，状态和
    远程命令不能扩大它。启动、重启和QMT重连完成后均停在`READY_PAUSED`。
12. Windows Task Scheduler只拉起Runtime/Watchdog；进程内唯一Scheduler只发布控制事件，不能
    导入QMT、SQLite或HTTP适配器。生命周期按Persistence到Control API顺序启动并逆序停止。
13. 生产代码中的Broker `submit_order`调用只能出现在
    `execution/order_intent_gate.py`；该门在持久化前和Broker调用前分别读取当前Runtime权限，
    中间先原子持久化订单意图与事件；重复意图及等待期间权限被收回均只进入对账，Broker回执的
    request seq随后写入订单关联记录。
14. `sync`只通过Persistence Writer读取和更新Outbox；未经响应HMAC及逐条身份校验不得ACK，
    断网、超时、非完整回执和任务取消必须保留原业务事实。Outbox压力只可收紧交易能力，积压恢复
    不得自动授予AUTO。
15. 本地API通过`LocalControlSubmissionService`执行Journal幂等查询并只向关键事件通道发布控制
    请求；`LocalControlDecisionHandler`必须在决策循环所有者内应用状态。HTTP 202只代表命令进入
    队列，`APPLIED/NOOP/REJECTED`以Journal终态为准。Emergency Halt先锁HALT再审计，能力放宽先
    持久化再校验，终态审计不明及未完成恢复记录均回落HALT。中心权限不能清除本地限制。
16. Journal低磁盘/不可写、Writer队列饱和/停滞/失败及Runtime组件崩溃必须生成关键故障事件，
    由决策循环内的统一故障策略进入HALT；监控恢复只解除信号锁存，不能自动恢复AUTO。
17. TradePlan进入执行编排前必须通过版本化`PlanRiskEvaluator`；评估使用强类型计划、账户事实和
    显式政策对象，输出完整PLAN范围`RISK_DECISION`。任一规则失败时不得创建订单意图。
18. 生产组合根必须用本地RuntimeConfig构造`HardSafetyLimits`，并把Runtime状态检查与Hard Safety
    固定组合进订单持久化门。中心计划、策略结果和远程命令只能收紧，不能替换或扩大本地上限；
    活动买单剩余金额必须计入预计仓位。
19. 真实委托Profile的HMAC和券商完整账号必须由当前服务用户的Windows Credential Manager提供；
    配置文件只保存规范凭据目标，交互式凭据命令只从隐藏输入读取值。
20. 每个生产日志Handler必须同时使用`RedactingLogFilter`和`RedactingFormatter`，并注册已解析的
    HMAC与券商账号。配置诊断只使用`LoadedConfiguration.safe_summary()`。
21. 故障包只能通过`DiagnosticBundleWriter`生成；Artifact先递归脱敏和敏感值扫描，再写入受保护
    的`diagnostics`目录。原始对象、二进制请求正文和任意对象`repr`不进入日志或故障包。
22. 生产组合根在打开LocalJournal前建立固定存储目录并收紧DACL；权限只授予当前服务用户与
    LocalSystem。凭据文件、私钥和已知运行时secret由仓库质量门自动扫描。
23. Broker恢复查询只有通过`validate_order_query_result()`或`validate_trade_query_result()`的
    强类型不可变快照才是权威事实；QMT返回`None`、可变集合、未映射对象或重复身份均产生查询
    不可用故障并失败关闭。
24. `auto-trading-qmt-probe`是独立的QMT-001现场组合根，只接受`READ_ONLY`配置，并仅解析券商
    账户凭据。`CiccQmtReadOnlyClient`只公开注册回调、启动、连接、账户订阅、账户状态查询、
    反订阅和停止；探针在构造QMT客户端前获取与正式Runtime相同的账户级Global Mutex，释放成功
    后才发布报告。报告省略完整账户、SDK对象、原始路径和异常文本，使用新文件原子留证。
25. `auto-trading-qmt-snapshot-probe`是依赖QMT-001现场通过的QMT-002组合根。它复用同一账户锁和
    受控会话，仅增加资产、持仓、当日委托和当日成交四类只读查询；查询前后主动确认账户OK。
    输出只含字段形状和聚合计数，不含账户、证券、订单、成交、文本或财务原值，也不直接映射为
    要求内部`client_order_id`的正式Broker模型。
26. `auto-trading-qmt-tick-probe`是QMT-003独立只读组合根。XtData依赖只允许位于
    `gateways/market/qmt.py`；入口持有同一账户级Global Mutex，调用单股Tick订阅、最新快照和
    反订阅，并将证券代码替换为指纹。现场五场景签署前，字段类型、时间单位、成交量倍率和档位
    深度只作为证据候选，不直接构造生产`MarketTick`。
27. `auto-trading-qmt-order-preflight`是QMT-004的离线审批组合根，仅接受`ASSISTED`配置和外部
    批准的审批文件SHA-256。它只解析券商账户凭据并校验目标机/账户指纹、单一订单参数、双人
    复核、短时窗口、撤单期限及审批/本机双重金额上限；模块不导入XtQuant，输出固定标记未提交
    实盘订单。实际报单仍只能经`execution/order_intent_gate.py`的持久化门进入Broker端口。

## 4. 变更门槛

涉及进程模型、模块依赖方向、TradePlan含义、持久化门、Hard Safety或QMT会话规则的
变更必须新增ADR，并同步更新设计文档、契约测试和开发台账。
