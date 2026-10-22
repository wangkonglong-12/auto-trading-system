# Windows Task Scheduler部署设计

## 部署结果

OPS-002使用五个PowerShell脚本部署Windows长期运行任务：

| 文件 | 职责 |
|---|---|
| `TaskDeployment.Common.ps1` | 严格读取启动清单、校验路径/参数/SHA-256并记录进程输出 |
| `start-runtime.ps1` | 校验清单后以前台阻塞方式运行唯一Runtime进程 |
| `start-watchdog.ps1` | 校验清单后运行独立Watchdog，并把同一Runtime参数数组交给它用于拉起 |
| `install-scheduled-tasks.ps1` | 生成启动清单并注册两个登录触发任务 |
| `uninstall-scheduled-tasks.ps1` | 导出任务XML证据并注销两个任务，保留运行数据与日志 |

脚本目录为`scripts/windows`，wheel同时把同一目录收录到
`auto_trading_system/deployment/windows`。生产安装包从wheel资源复制到版本化安装位置，任务动作
固定指向安装后的脚本和启动清单。

## 两个任务

| 属性 | AutoTrading-Runtime | AutoTrading-Watchdog |
|---|---|---|
| 触发器 | 专用用户登录 | 同一专用用户登录后30秒 |
| 登录类型 | Interactive | Interactive |
| 权限 | Highest | Highest |
| 多实例 | IgnoreNew | IgnoreNew |
| 执行期限 | 无期限 | 无期限 |
| Task Scheduler重启 | 0次 | 1分钟间隔，最多3次 |
| 进程恢复 | 由Watchdog负责 | 由Task Scheduler负责 |

MiniQMT和QMT Python会话运行在同一个专用用户交互登录会话。Runtime任务不启用Task Scheduler
进程重启，避免和Watchdog形成双重拉起竞争；账户级Runtime Mutex继续阻止同账户双开。

## 启动清单

安装器生成`scheduled-task-launch.json`，内容固定为：

- 清单版本；
- Runtime配置的绝对路径；
- 进程日志目录；
- Runtime可执行文件、精确参数数组和安装时SHA-256；
- Watchdog可执行文件和安装时SHA-256。

清单不包含节点HMAC、券商完整账户、操作员密码或其他secret。命令参数出现`password`、`secret`、
`access-token`或`api-key`时安装直接失败，敏感值统一由Runtime配置中的Credential Manager引用
解析。

每次任务触发时，启动脚本重新验证：清单大小不超过64 KiB、字段集合和版本完全匹配、所有路径为
绝对路径、可执行文件存在、参数无控制字符、两个二进制SHA-256与安装时一致。任一检查失败时进程
返回70，交易组件不会启动。

## 安装预览

先在目标专用用户和发布目录上生成无副作用计划：

```powershell
.\scripts\windows\install-scheduled-tasks.ps1 `
  -ServiceUser ".\AutoTradingSvc" `
  -ConfigPath "C:\ProgramData\AutoTrading\config\runtime.json" `
  -RuntimeExecutable "C:\ProgramData\AutoTrading\current\Scripts\auto-trading-runtime.exe" `
  -WatchdogExecutable "C:\ProgramData\AutoTrading\current\Scripts\auto-trading-watchdog.exe" `
  -RuntimeArguments @("--run", "--config", "C:\ProgramData\AutoTrading\config\runtime.json") `
  -PlanOnly
```

输出JSON明确列出用户、触发延迟、PowerShell动作、Runtime参数、二进制hash和任务设置，不写清单、
不注册任务。

## 正式安装

以管理员身份在目标Windows机器执行同一命令并移除`-PlanOnly`。安装器按以下顺序执行：

1. 验证用户文本、配置、脚本、可执行文件和Runtime参数；
2. 计算Runtime与Watchdog二进制SHA-256；
3. 检查两个同名任务；
4. 原子写入启动清单并创建日志目录；
5. 注册Runtime任务；
6. 注册Watchdog任务；
7. 输出与预览相同的最终JSON计划作为安装记录。

默认遇到同名任务即停止。批准替换时显式加入`-Replace`；安装器先导出旧任务XML到内存，任一新任务
注册失败就移除本次已注册任务、恢复旧任务XML并恢复原启动清单。

## 启动与现场核验

专用用户登录后，先看到`AutoTrading-Runtime`进入Running；30秒后
`AutoTrading-Watchdog`进入Running。核验项目为：

1. 任务Principal为指定专用用户、Interactive和Highest；
2. Runtime动作只包含Windows PowerShell、`start-runtime.ps1`和清单路径；
3. Watchdog动作只包含Windows PowerShell、`start-watchdog.ps1`和同一清单路径；
4. `<log_root>`产生各自带毫秒时间戳的stdout/stderr文件；
5. `<storage.root>\runtime\runtime-heartbeat.json`的PID与Runtime进程一致且sequence递增；
6. 手工结束Runtime后出现`external-halt.json`，退避后新心跳状态为HALT；
7. 第二次手工启动Runtime被账户级Mutex拒绝，不产生第二个QMT会话。

目标QMT机器上的第5至7项证据归入TST-006；当前开发机的OPS-002自动验证使用真实PowerShell子进程
完成计划生成、清单hash校验、日志创建和篡改拒绝。

## 安全卸载

先预览：

```powershell
.\scripts\windows\uninstall-scheduled-tasks.ps1 -PlanOnly
```

正式执行时脚本把两个任务的XML导出到
`C:\ProgramData\AutoTrading\evidence\scheduled-tasks`，再注销`AutoTrading-Runtime`和
`AutoTrading-Watchdog`。启动清单、进程日志、SQLite、Tick、备份、诊断证据和Credential Manager
凭据继续保留，用于审计、回滚和后续版本复用。

## 自动验证

`verify-scheduled-task-scripts.ps1`在临时目录完成以下验证，并已接入统一`check.ps1`质量门：

- `-PlanOnly`生成固定的双任务结构且不写磁盘；
- Runtime触发延迟为0，Watchdog延迟为30秒；
- Runtime任务不配置重启，Watchdog配置3次重启；
- 清单二进制hash正确时Runtime包装脚本执行成功并创建日志；
- 篡改hash后包装脚本返回非零；
- secret形式的命令行参数被拒绝；
- 卸载计划固定注销两个任务并保留运行数据。
