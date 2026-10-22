# WD-001 独立Runtime Watchdog验收证据

## 验收结论

WD-001完成。项目已提供独立`auto-trading-watchdog`进程、Runtime原子PID心跳、外部HALT锁存标记、
既有Runtime发现/附着、受控终止与拉起、有界指数退避、连续崩溃熔断和Runtime启动安全门。

Watchdog不连接QMT、SQLite或量化服务。它读取不含secret的Runtime设置，并以固定参数数组直接创建
进程。任何异常重启都先持久化HALT证据，并通过文件和`ATS_WATCHDOG_FORCE_HALT=1`双通道要求新
Runtime进入HALT；Watchdog没有恢复`AUTO`的接口。

## 已验证行为

| 行为 | 结果 |
|---|---|
| 独立进程 | wheel提供`auto-trading-watchdog`入口，节点/逻辑账户摘要Global Mutex保证单实例 |
| 既有Runtime | 启动发现窗口等待新心跳，按心跳PID打开Windows进程句柄并附着 |
| 心跳身份 | 绑定node、account、boot、PID、sequence和Runtime state；旧指纹/PID不匹配不算进度 |
| 原子文件 | 同目录独占临时文件、flush、fsync、`os.replace`，读取限制64 KiB |
| 输入校验 | 拒绝重复键、未知字段、错误版本/类型、符号链接及节点账户错配 |
| 时间边界 | 启动、失联、退避、崩溃和熔断全部使用单调时间 |
| 异常退出 | 先写`RUNTIME_EXITED`，再按2/4/.../60秒有界退避拉起 |
| 心跳失联 | 区分首条心跳超时和已建立心跳停滞，先HALT再终止旧进程 |
| 终止升级 | 先正常终止并等待；超时后强制终止；旧进程未退出时不创建第二实例 |
| 磁盘低水位 | 写外部HALT并保留Runtime处理活动委托，bridge把标记送入关键事件链 |
| 连续崩溃 | 默认10分钟3次打开5分钟熔断，半开重试仍强制HALT |
| 标记写失败 | 自动拉起停止，避免新进程在没有安全证据时运行 |
| Runtime启动 | 组合根在连接QMT前读取标记/环境并以WATCHDOG权限进入HALT |
| Watchdog停止 | 后附着的Runtime保持运行；Watchdog自行拉起的Runtime受控停止 |
| Secret边界 | `load_runtime_settings`只验证配置，Watchdog不解析Credential Manager或环境secret |

## 真实子进程恢复验证

集成测试创建一个使用生产心跳存储的真实Windows子进程。第一次启动写出正常安全态心跳后以退出码
17崩溃；Watchdog观察进程句柄退出，写入外部HALT并退避。第二次启动收到
`ATS_WATCHDOG_FORCE_HALT=1`，持续写出`HALT`心跳。测试同时确认：

- 两次启动环境记录依次为`0`和`1`；
- 第二次心跳PID与实际受监护PID一致；
- 外部标记包含`RUNTIME_EXITED`；
- 第二进程建立有效心跳后状态为`HEALTHY`；
- 测试结束时由Watchdog正常终止并回收第二进程。

## 自动化验证

2026-09-04在Windows、Node 24.19.0、pnpm 11.19.0和Python 3.12.14执行完整统一质量门：

- 24项前端测试全部通过；
- ESLint、Prettier、严格TypeScript、Vite生产构建全部通过；
- repository secret boundary、Python编译、Ruff、格式检查和mypy严格检查全部通过；
- 1266项Python测试全部通过，总覆盖率90.63%；
- Runtime/Watchdog真实子进程崩溃和强制HALT拉起集成测试通过；
- Runtime配置Schema与包含Watchdog参数的READ_ONLY示例一致；
- sdist与dev24 wheel从隔离构建环境生成成功。

## 独立安装验证

全新虚拟环境从dev24 wheel解析并安装全部声明依赖，随后完成：

- Distribution版本和源码版本均为`0.1.0.dev24`；
- `ExternalHaltMarkerStore`、`RuntimeHeartbeatStore`、启动安全门和Watchdog supervisor可直接导入；
- `auto-trading-watchdog --version`输出`0.1.0.dev24`；
- `auto-trading-runtime --version`输出`0.1.0.dev24`。

## 产物校验和

| 产物 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev24-py3-none-any.whl` | `2ec475d0c4241cc2caeddbdd63f8787f186073fc89b98d428d0d799d0395fef6` |
| `src/auto_trading_system/runtime/watchdog.py` | `819fac77b1e0b0e6debce9e60da5b8f2b39300b9c4b1c0a1e87c7074e54d6bfe` |
| `src/auto_trading_system/apps/watchdog/supervisor.py` | `17e91d945243258afa566499b3d4ff4cd5c4372f7adeefc8ef4b5782c9a208f7` |
| `src/auto_trading_system/apps/watchdog/__main__.py` | `dbc56b07a82e27464f93f9d796830807be4877dd1499d7ff7efff9c135f35553` |
| `docs/runtime-watchdog.md` | `798ae73ce71e6d82399ef15158403973abc850de5b6afacc94ed90b70bebc5de` |
| `config/v1/runtime-config.schema.json` | `7628d271df8b9242be362d683d179b7dd43d0eb509a6378c7660c5abd3fe3a55` |

## 下一项

RT-004与WD-001均已完成，下一项直接实施OPS-002：用PowerShell安装脚本建立Runtime和Watchdog的
Windows Task Scheduler任务，固化专用用户、启动延迟、参数数组、日志目录、单实例和卸载边界。
