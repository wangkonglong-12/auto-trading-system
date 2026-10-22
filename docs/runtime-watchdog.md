# 独立Runtime Watchdog与外部HALT协议

## 目标与边界

`auto-trading-watchdog`是Windows交易节点的独立小进程。它监控Runtime进程、Runtime本地心跳、
存储盘剩余空间和短时间连续崩溃，并在Runtime异常后执行受控拉起。

Watchdog不连接QMT、不读取交易计划、不访问SQLite、不调用量化服务，也不拥有订单状态。它唯一允许
施加的交易权限变化是写入外部HALT标记。Runtime是实时状态的唯一所有者，并在连接QMT前消费该
标记；Watchdog不能把Runtime切换为`AUTO`。

## 进程与启动关系

Windows任务计划程序分别启动Runtime和Watchdog。Watchdog启动后先进入发现窗口：

1. 读取当前心跳指纹并等待下一次原子心跳；
2. 新心跳出现后，按其中的PID打开Windows进程句柄并接管监护；
3. 发现窗口内没有活动心跳时，Watchdog使用安装脚本提供的固定参数数组启动Runtime；
4. 启动命令通过参数数组直接交给Windows，不经过命令解释器；
5. Watchdog自身使用节点和逻辑账户摘要对应的Global Mutex保持单实例。

这套顺序兼容“Runtime先启动、Watchdog延迟30秒启动”，也支持Watchdog在Runtime崩溃后成为新进程
的父进程。停止一个后来附着的Watchdog只关闭监护句柄；停止由Watchdog拉起的监护树时会受控停止
它所拥有的Runtime。

## 本地协议文件

文件固定放在`<storage.root>\runtime`，继承`storage.root`的专用Windows用户和LocalSystem ACL。

| 文件 | 写入方 | 内容与用途 |
|---|---|---|
| `runtime-heartbeat.json` | Runtime | 节点、逻辑账户、boot id、PID、递增序号、Runtime状态和上海时间 |
| `external-halt.json` | Watchdog | 标记id、首末发现时间、原因集合、发生次数、相关PID、重启代次和有界诊断字段 |

两个文件均采用同目录临时文件、刷新文件数据和`os.replace`原子替换。读取端限制为64 KiB，拒绝
重复JSON键、未知字段、错误版本、错误类型、符号链接和节点/账户不匹配。心跳文件可被下一次
Runtime心跳覆盖；外部HALT是锁存标记，运维确认故障和恢复条件前持续保留。

## Runtime侧执行顺序

Runtime组合根使用`build_runtime_watchdog_boundary`建立协议边界，顺序固定为：

```text
加载并验证配置
  -> 初始化安全存储目录
  -> 获取账户级Runtime Mutex
  -> 读取external-halt.json或ATS_WATCHDOG_FORCE_HALT=1
  -> 状态机以WATCHDOG权限进入HALT
  -> 启动Journal与唯一决策循环
  -> 启动watchdog-bridge Worker
  -> 连接QMT并执行启动对账
```

`watchdog-bridge`启动时先把活动HALT标记提交给关键事件接收器，再写第一条心跳；之后按配置周期
写心跳并检查新标记。关键事件提交、标记读取或心跳写入失败会使Worker失败，现有Runtime故障策略
随即收紧权限；心跳停止后独立Watchdog也会介入。

## 故障处置

| 监测结果 | Watchdog动作 | Runtime结果 |
|---|---|---|
| Runtime正常且心跳前进 | 保持监护 | 状态由Runtime自身决定 |
| Runtime启动后无首条心跳 | 先写`RUNTIME_HEARTBEAT_START_TIMEOUT`，再终止并退避拉起 | 新进程强制HALT |
| Runtime存活但心跳停止 | 先写`RUNTIME_HEARTBEAT_STALE`，再终止并退避拉起 | 新进程强制HALT |
| Runtime异常退出 | 写`RUNTIME_EXITED`，按有界指数退避拉起 | 新进程强制HALT |
| 启动命令失败 | 写`RUNTIME_LAUNCH_FAILED`并计入崩溃窗口 | 到期后强制安全重试 |
| 剩余空间低于本机阈值 | 写`WATCHDOG_DISK_SPACE_LOW`，保留当前进程处理委托和退出 | bridge提交HALT事件 |
| 磁盘查询失败 | 写`WATCHDOG_DISK_QUERY_FAILED` | bridge提交HALT事件 |
| 连续崩溃达到阈值 | 追加`RUNTIME_CRASH_LOOP`并打开熔断窗口 | 熔断后只进行一次HALT半开重试 |
| HALT标记损坏或无法写入 | Watchdog失败关闭并停止自动拉起 | 需要现场运维处理存储和标记 |

终止Runtime时先发送正常终止并等待`terminate_grace_seconds`；超时后强制终止并再次等待。无法停止
旧进程时Watchdog不会拉起第二个Runtime，账户级Runtime Mutex仍是第二道单实例保护。

## 单调时间与进程身份

心跳失联、启动等待、指数退避、崩溃窗口和熔断窗口全部使用单调时钟。心跳里的上海时间只用于
诊断，不参与超时判定。Watchdog在每次启动前保存既有心跳指纹，只接受与当前监护PID一致且指纹
发生变化的新心跳，因此旧文件、PID不匹配和墙上时钟校准不能伪造存活进度。

## 配置

`watchdog`配置段随`RuntimeConfig 1.0`发布：

| 字段 | 默认值 | 作用 |
|---|---:|---|
| `heartbeat_interval_seconds` | 2 | Runtime本地心跳周期 |
| `poll_interval_seconds` | 2 | Watchdog检查周期 |
| `heartbeat_startup_timeout_seconds` | 45 | 新进程首条有效心跳期限 |
| `heartbeat_stale_timeout_seconds` | 10 | 已建立心跳允许的最大停滞 |
| `terminate_grace_seconds` | 5 | 正常终止等待时间 |
| `restart_initial_delay_seconds` | 2 | 第一次重启退避 |
| `restart_max_delay_seconds` | 60 | 指数退避上限 |
| `crash_limit` | 3 | 崩溃窗口内打开熔断的次数 |
| `crash_window_seconds` | 600 | 连续崩溃统计窗口 |
| `circuit_open_seconds` | 300 | 熔断等待时间 |

`heartbeat_stale_timeout_seconds`至少是心跳周期的三倍；启动期限不小于失联期限；最大退避不小于
初始退避；崩溃统计窗口不小于最大退避。磁盘阈值复用`storage.minimum_free_space_mb`，从而让
Journal、Recorder和Watchdog使用同一个本机硬下限。

## 命令入口

Watchdog读取Runtime配置但不解析Credential Manager中的任何secret。运行时参数放在`--`之后，
首项必须是已存在的绝对可执行文件路径：

```powershell
auto-trading-watchdog.exe `
  --config C:\ProgramData\AutoTrading\config\runtime.json `
  -- C:\ProgramData\AutoTrading\current\Scripts\auto-trading-runtime.exe <Runtime固定参数>
```

生产任务的完整参数数组由OPS-002安装脚本生成并固化；Watchdog本身不会拼接或执行Shell文本。

## 安全不变量

- Watchdog只附着一个PID或拥有一个子进程句柄；旧进程未确认退出前不会拉起新进程。
- 每次异常必须先写外部HALT证据，写入失败即停止自动拉起。
- 只要外部HALT存在，拉起环境必定设置`ATS_WATCHDOG_FORCE_HALT=1`。
- Runtime在连接QMT前读取文件和环境双通道；任一通道要求安全启动就进入HALT。
- Runtime重启、QMT重连和熔断半开不会自动进入`AUTO`；恢复仍需对账、健康、本地与中心许可。
