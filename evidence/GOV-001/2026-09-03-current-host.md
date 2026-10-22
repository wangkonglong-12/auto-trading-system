# GOV-001 当前开发机核验记录

- 核验日期：2026-09-03
- 工作项状态：`BLOCKED`
- 核验范围：当前Codex开发机

## 已确认事实

| 项目 | 结果 |
|---|---|
| Python Launcher | Python 3.10、3.13、3.14 |
| 当前`python` | Python 3.13.13 x64 |
| Codex开发运行时 | Python 3.12.13 x64 |
| Python 3.11 x64 | 当前开发机未安装 |
| QMT/MiniQMT | 系统安装项和磁盘根目录均未发现 |
| XtQuant | 当前工作区未发现SDK |
| `userdata_mini` | 当前开发机未提供 |

## 结论

当前机器用于项目骨架、协议、Mock和静态测试。生产版本三元组必须从实际Windows交易机
采集，包含QMT客户端版本及安装包SHA-256、XtQuant版本及包SHA-256、Python 3.11 x64
版本、安装路径、`userdata_mini`路径和回滚包。

## 后续证据

在目标交易机运行`scripts/diagnose-environment.ps1`，补充脱敏账户类型、函数交易权限和
`up_queue_xtquant`验证结果。证据补齐后进入`VERIFYING`并由QMT PoC评审关闭。
