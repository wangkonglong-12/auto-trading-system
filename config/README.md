# Windows节点配置

## 文件边界

- `v1/runtime-config.schema.json`是机器配置v1.0的发布Schema。
- `v1/runtime.read-only.example.json`是可提交Git的READ_ONLY样例，只包含非敏感配置和
  secret引用。
- `v1/qmt-order-poc-approval.example.json`是QMT-004单笔实盘PoC的封闭审批模板。
- 机器实际配置放在被Git忽略的`config/local/`，部署时安装到
  `%ProgramData%\AutoTrading\config`并由专用Windows账户读取。
- 节点HMAC secret、券商完整账号和操作员密码校验器不写入JSON配置。

## Profile

支持`READ_ONLY`、`PAPER`、`SHADOW`、`ASSISTED`、`LIVE_EXIT_ONLY`和`LIVE`。
Profile是部署许可上限，Runtime状态仍从`READY_PAUSED`启动。`ASSISTED`及以上真实委托
Profile只接受Windows Credential Manager引用；环境变量引用只用于开发、测试和
非真实委托Profile。

## Secret来源

代码采用统一`SecretProvider`边界。环境变量Provider只解析`ATS_SECRET_*`，Windows Credential
Manager Provider通过`CredReadW`读取Generic Credential。真实委托Profile默认启用Windows
Provider；Provider不可用、目标缺失、编码错误或值校验失败时配置加载直接失败。安装与轮换使用
交互式`auto-trading-credential`命令，完整规则见`docs/local-security.md`。

本地验证READ_ONLY样例：

```powershell
$env:ATS_SECRET_NODE_HMAC = "replace-with-at-least-32-bytes-secret"
$env:ATS_SECRET_BROKER_ACCOUNT = "REPLACE1234"
$env:ATS_SECRET_OPERATOR_PASSWORD_VERIFIER = "replace-with-ats-scrypt-v1-verifier"
.\.venv\Scripts\auto-trading-runtime.exe `
  --check-config .\config\v1\runtime.read-only.example.json
```

命令只输出配置hash、Profile、逻辑节点标识、key id、secret来源和本地API地址，不输出
secret值或券商完整账号。

## QMT-001只读配置

目标机QMT-001使用`READ_ONLY`配置，并把`security.broker_account_id`设置为Windows Credential
Manager引用。`auto-trading-qmt-probe`只解析该账户引用，读取`qmt.userdata_mini`、
`qmt.session_id`和`qmt.account_type`后执行连接、账户订阅和状态查询；HMAC与操作员凭据不参与
该探针。探针复用正式Runtime的账户级Global Mutex，已有同账户进程时不会访问QMT；现场命令、
Session间隔和证据判定见`docs/qmt-field-diagnostic.md`。

QMT-001通过后，同一份`READ_ONLY`配置用于`auto-trading-qmt-snapshot-probe`。该入口只增加资产、
持仓、当日委托和当日成交查询，报告不保存任何业务原值；QMT-002完整场景矩阵仍须在目标机执行。

QMT-003继续使用同一份`READ_ONLY`配置和账户级Global Mutex，并通过命令行显式提供1至15个规范
证券代码、现场场景和观察秒数。`auto-trading-qmt-tick-probe`只调用XtData单股Tick订阅、最新
全推快照和反订阅；报告以证券指纹记录字段形状、时间戳量级、成交量倍率候选、档位深度与状态码。

## QMT-004审批预检

QMT-004现场实盘PoC使用`ASSISTED`配置，三个secret引用全部指向当前专用服务用户的Windows
Credential Manager。`auto-trading-qmt-order-preflight`只解析券商账户引用，并把账户摘要、
GOV目标机指纹、本机配置hash、QMT-001/002/003证据hash和审批订单参数绑定到离线预检报告。

审批文件从`qmt-order-poc-approval.example.json`复制到受控证据目录后填写；账户指纹直接取自
同账户QMT-001报告的`target_account_fingerprint`。执行人和独立复核人
核对单一标的、方向、数量、限价、审批金额上限、5至120秒撤单期限和1至30分钟有效窗口；审批
文件SHA-256登记到批准的变更单。命令行使用变更单中的hash校验文件，Runtime本机单笔金额上限
继续作为不可放宽的第二道限制。详细命令和证据流程见`docs/qmt-field-diagnostic.md`。

## Runtime事件队列

`runtime_queues.normal_capacity`配置普通事件通道，默认10000；`critical_capacity`
配置独立预留的关键事件通道，默认256。QMT回调和Worker线程只做字段复制与非阻塞入队。
任一通道满载时，溢出会汇总为不会丢失的决策循环信号，Runtime必须先进入安全模式再处理
后续业务事件。这两个容量是内存资源上限，不能用于绕过交易风险限制。

## 独立Watchdog

`watchdog`段定义Runtime本地心跳、检查周期、首条心跳期限、失联期限、终止宽限、重启退避、
连续崩溃窗口和熔断时间。失联期限至少为心跳周期的三倍；配置关系不成立时节点启动失败。
Watchdog复用`storage.minimum_free_space_mb`作为磁盘HALT阈值，并通过只加载非敏感设置的
`load_runtime_settings`读取该段，不解析任何节点、账户或操作员secret。完整协议见
`docs/runtime-watchdog.md`。

## Schema生成

```powershell
.\scripts\export-config-schema.ps1
```

Schema必须由当前`RuntimeConfig`模型重新生成并通过测试，禁止手工修改生成文件。
