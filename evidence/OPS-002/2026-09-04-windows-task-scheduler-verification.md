# OPS-002 Windows Task Scheduler部署验收证据

## 验收结论

OPS-002完成。项目已提供可随wheel交付的Windows Task Scheduler部署脚本，建立
`AutoTrading-Runtime`与`AutoTrading-Watchdog`两个专用用户登录任务。Runtime立即启动，Watchdog
延迟30秒；Runtime进程恢复只由Watchdog负责，Watchdog进程恢复只由Task Scheduler负责。

启动清单固定配置路径、Runtime精确参数数组、日志目录以及Runtime/Watchdog二进制SHA-256。两个
包装脚本每次启动都重新验证清单和hash，参数使用PowerShell数组直接传递，不执行Shell命令文本。

## 交付文件

| 文件 | 验收结果 |
|---|---|
| `TaskDeployment.Common.ps1` | 严格字段、绝对路径、参数、secret形式和SHA-256验证通过 |
| `start-runtime.ps1` | 真实Windows PowerShell子进程执行、退出码透传和双日志生成通过 |
| `start-watchdog.ps1` | 固定组合Watchdog配置参数、`--`边界和Runtime原始参数数组 |
| `install-scheduled-tasks.ps1` | PlanOnly、双任务定义、同名保护、显式替换和失败回滚完整 |
| `uninstall-scheduled-tasks.ps1` | XML证据导出及精确双任务注销，运行数据与凭据保留 |
| `verify-scheduled-task-scripts.ps1` | 临时目录自清理的端到端脚本验证，已接入统一质量门 |

## 任务定义

| 属性 | Runtime | Watchdog |
|---|---|---|
| 名称 | `AutoTrading-Runtime` | `AutoTrading-Watchdog` |
| Trigger | 指定专用用户AtLogOn，`PT0S` | 同一用户AtLogOn，`PT30S` |
| Principal | Interactive、Highest | Interactive、Highest |
| MultipleInstances | IgnoreNew | IgnoreNew |
| ExecutionTimeLimit | `PT0S`长期运行 | `PT0S`长期运行 |
| Task重启 | 0 | 1分钟间隔、最多3次 |
| 业务恢复责任 | Watchdog | Task Scheduler |

## 安全与失败行为

- `-PlanOnly`输出完整JSON计划且不写清单、不调用ScheduledTasks模块；
- 默认发现同名任务直接停止，`-Replace`才进入可审计替换；
- 替换前保存旧任务XML和旧启动清单；
- 第二个任务注册失败时，移除本次已注册项并恢复旧任务与清单；
- 清单使用同目录临时文件和原子替换；
- 运行参数禁止NUL、换行、超长值和password/secret/token/api-key形式；
- Runtime与Watchdog二进制被替换后，包装脚本在启动交易组件前返回70；
- 启动包装器使用Windows PowerShell `RemoteSigned`、NoProfile、NonInteractive；
- 卸载先导出任务XML，再注销两个精确任务名；
- 启动清单、日志、SQLite、Tick、备份、诊断和Credential Manager凭据继续用于审计和回滚。

## 自动化验证

2026-09-04在Windows、Node 24.19.0、pnpm 11.19.0和Python 3.12.14执行完整统一质量门：

- PowerShell验证器生成并解析双任务PlanOnly结果；
- Runtime延迟0秒、Watchdog延迟30秒及各自重启责任断言通过；
- 真实Windows PowerShell经`start-runtime.ps1`启动成功并产生stdout/stderr日志；
- 篡改Runtime hash后包装器返回非零；
- secret形式参数被安装器拒绝；
- 卸载计划的任务范围和保留范围断言通过；
- 24项前端测试全部通过；
- repository policy、Python编译、Ruff、格式检查和mypy严格检查全部通过；
- 1269项Python测试全部通过，总覆盖率90.63%；
- sdist与dev25 wheel从隔离构建环境生成成功。

目标专用用户、MiniQMT登录会话和任务注册属于目标QMT机器现场证据，按计划在GOV-001/QMT-001后
纳入TST-006；开发机没有创建或删除真实交易任务。

## 独立安装验证

全新虚拟环境从dev25 wheel解析并安装全部依赖后完成：

- Distribution、Runtime CLI和Watchdog CLI版本均为`0.1.0.dev25`；
- wheel包含`auto_trading_system/deployment/windows`下全部六个PowerShell文件；
- 直接从安装后的wheel资源运行`verify-scheduled-task-scripts.ps1`成功；
- 安装后资源再次完成PlanOnly、真实包装进程、日志和hash篡改拒绝验证。

## 产物校验和

| 产物 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev25-py3-none-any.whl` | `a0ec5a4d192f3489be475d9ab0055f7766dff9b42a1bcf6f96197af1f235f250` |
| `scripts/windows/TaskDeployment.Common.ps1` | `087c4aefdcd21684c88a6258e9ecdcb1e3d29876042da4095c6bc60a7134d481` |
| `scripts/windows/install-scheduled-tasks.ps1` | `71eabc3e208fd19d34bc64f303c77e5199108cb62e0e6fd2cc6087249448c9ea` |
| `scripts/windows/start-runtime.ps1` | `302ea6792d5d0f022d8231118470f52c1017549188734a10245262ad69bfab36` |
| `scripts/windows/start-watchdog.ps1` | `4a0611463cefe42a97db501d4c22250a4f4307a245a0e23306a658675a7668ba` |
| `scripts/windows/uninstall-scheduled-tasks.ps1` | `a25b775a0fe5050ba592cc76701a8f039c64765f97e56297028a2f7e1d068473` |
| `scripts/windows/verify-scheduled-task-scripts.ps1` | `b752307b2605dfd4a2bbbccbd64e00223a139ef838f192380d0575ce47427426` |
| `docs/windows-task-scheduler.md` | `83905b37052262666d5f594731ffa0924890a67904b23792be9941baebb596b5` |

## 下一依赖点

当前开发机上所有前置依赖已满足且无需目标QMT的顺序工作项已经完成。下一项回到
`GOV-001/QMT-001`：在目标QMT机器记录版本、Python、账户/权限和登录会话事实，并运行只读诊断。
该证据到位后按P3顺序进入GW-001、GW-002、MKT-001和订单闭环。
