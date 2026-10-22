# dev52维护备份与恢复候选验证

- python-tests.xml：Python 3.11.16全量2,360项，0失败/错误/跳过。
- coverage.json：全量覆盖率91.44%。
- installed-tests.xml：独立安装最终wheel后464项通过。
- artifact-verification.json：安装位置、200个Python文件/11个页面资源与wheel一致、7个模拟入口及wheel摘要。
- initial-python-tests.xml和initial-coverage.json：首轮1项架构失败保留；数据库操作已收归Journal维护适配器，最终全量通过。
- windows-final-boundary.json：相对于本轮基线的最终改动清单，原业务文件删除0项。

最终wheel SHA256：764584ca8f1e43ce27f7b1467843d549f58f258b7897e8883554bc552edd9c87。
设计及操作步骤见docs/design/v3.1中的ADR-R31-011和维护备份与恢复实施报告_20260926.md。

三个命令交付维护备份、校验、受启动阻断的恢复候选；正式激活/代际切换另行开发和验收。
测试采用隔离临时目录，MOCK/零额度/实盘关闭；未连接真实QMT，未修改量化后台，未操作生产数据库、凭据或系统任务注册。
