# dev49 策略无关的监护与恢复验收

本目录为2026-09-24恢复工作后的最终验收证据。上一日的首轮结果保留在`../20260923-supervision/`，后续追加测试与修订以本目录为准。

- `python-tests.xml`：全量2254项，失败/错误/跳过均为0。
- `coverage.json`：全量分支覆盖统计，综合91.33%。
- `installed-tests.xml`：从独立wheel安装目录导入后，341项监护/进程/授权及业务回归。
- `artifact-verification.json`：Python源文件与wheel一致、安装来源、七个模拟入口、制品摘要；完成文档收尾后复核源码包文档。
- `post-reboot-baseline.json`：本次开发机重启后保存的文件基线，已包含重启前dev49改动。
- `windows-retained-files.json`：与上一轮dev48开始前保存的基线比较，包含dev48与dev49共同变动，仅用于证明原有文件保留，不能作为dev49独占变更清单。
- `windows-final-boundary.json`：与post-reboot-baseline比较，记录重启后的收尾变动。

前端检查使用Node 24.19.0，24项测试及构建通过。Python基线3.11.16，Ruff/格式/mypy（321文件）、V2/配置Schema、Windows任务静态检查与仓库凭据边界检查通过。

本轮只修改自动交易项目；没有量化项目写操作、生产数据库操作、系统任务注册、凭据安装或真实QMT连接。当前策略未定，MOCK宿主安装额度为0。重启只恢复进程与核对流程，不自动恢复交易授权。现场环境和启动极小窗口的剩余验收见本轮实施报告与ADR-R31-008。
