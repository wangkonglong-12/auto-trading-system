# TST-008 性能和资源边界验收记录

## 验收结论

TST-008完成。开发机以15只标的、30,000次连续行情回调验证
`MockMarketGateway → 不可变Tick → BoundedEventIngress`热路径，全部事件成功接收并完整排空，
Outbox远端补传等待期间Broker关键回调仍可立即进入独立预留通道。

## 固定门槛与实测

| 指标 | 固定门槛 | 实测 | 结论 |
|---|---:|---:|---|
| 标的数 | 15 | 15 | 通过 |
| 回调数 | 30,000 | 30,000 | 通过 |
| 溢出/丢失 | 0 | 0 | 通过 |
| 平均吞吐 | ≥5,000次/秒 | 79,650.113次/秒 | 通过 |
| 单次入口P95 | ≤5,000微秒 | 15.3微秒 | 通过 |
| Python增量峰值内存 | ≤64 MiB | 5.157 MiB | 通过 |

测量使用`time.perf_counter_ns()`和`tracemalloc`，计入Mock Market发布、同步回调、双通道锁、
不可变Envelope分配及延迟样本。事件守恒要求`accepted=drained=events`且`overflows=0`。

## Outbox隔离验证

- `DurableOutboxWorker`在异步Dispatcher中等待远端恢复；
- 同一时刻向`BoundedEventIngress`关键通道发布Broker回调；
- 回调在5毫秒门槛内成功进入预留通道；
- 释放远端等待后Outbox逐项ACK并原子提交，不产生重复投递任务。

这验证了HTTP等待与交易回调线程、Persistence Writer和行情入口的隔离关系。

## 测试、构建与安装

- 性能验收脚本：`scripts/verify-performance-boundaries.ps1`；4个相关测试文件、59项通过。
- 完整质量门：前端24项通过并完成3280模块生产构建；Python 1315项通过，总覆盖率
  90.48%；仓库策略、Ruff、格式和185个源文件的严格mypy检查通过。
- 构建：`0.1.0.dev32` sdist与wheel成功；wheel SHA-256为
  `c6306ad6d339c2bb370d76aa79479e8c4fe4bfca6703e1435ac332d49b2a2121`。
- 全新安装：按运行依赖哈希锁安装dev32 wheel后，Runtime、Watchdog、操作台静态资源、
  Windows部署脚本及安装包内性能入口均验证通过；安装环境7,500次复测吞吐87,355.674次/秒、
  P95 12.3微秒、峰值1.289 MiB。

## 现场边界

目标QMT机器的真实XtData回调、CPU、工作集、磁盘写入和持续运行数据归P0/P3/TST-006现场
证据。本项固定开发机容量基线及可执行验收入口，不替代目标机现场结论。

## 遗留事项

无TST-008本地验收遗留必做事项。
