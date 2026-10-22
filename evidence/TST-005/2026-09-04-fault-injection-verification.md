# TST-005 故障注入验收记录

## 验收结论

TST-005完成。中心断网、QMT断线、Broker查询返回`None`、Runtime组件崩溃、Journal磁盘压力
和时钟偏差六类故障均通过生产端口或状态机完成安全降级验证；任何恢复信号都不会自动授予
AUTO权限。

## 实现内容

- 新增Broker权威查询校验器：只有强类型不可变tuple可表示成功查询；`None`、可变集合、未映射
  对象和重复订单/成交身份统一抛出`BrokerQueryError`。
- Runtime故障类型增加`BROKER_QUERY_UNAVAILABLE`和`CLOCK_SKEW_UNSAFE`，两者经决策循环内
  故障策略进入`HALT`并锁定`HARD_SAFETY`恢复权限。
- 新增`OutboxPressureDecisionHandler`：中心不可达导致积压进入`HIGH`时从AUTO进入
  `PAUSE_BUY`，进入`CRITICAL`时进入`HALT`，恢复`NORMAL`保持当前限制。
- 新增六类集成验收用例及`scripts/verify-fault-injection-matrix.ps1`固定验证入口。

## 故障矩阵

| 注入 | 安全结果 |
|---|---|
| 中心断网/超时 | 签名传输返回可重试错误，Outbox整批重新调度；HIGH暂停买入，CRITICAL停止交易 |
| QMT断线 | AUTO进入CONNECTING；重连后执行RECONCILING，对账完成只到READY_PAUSED |
| 查询`None` | 明确判定事实不可用，进入HALT；不会形成空委托或空成交 |
| Runtime崩溃 | 组件故障进入HALT并锁定WATCHDOG权限；真实子进程由Watchdog安全拉起 |
| 磁盘满/低水位 | 真实SQLite健康探针产生磁盘压力，进入HALT且Broker调用为零 |
| 时钟漂移 | Hard Safety拒绝超限委托，时钟故障进入HALT |

## 测试、构建与安装

- 专项验收：9个测试文件、163项通过，成功JSON包含全部6类故障。
- 完整质量门：前端24项通过并完成3280模块生产构建；Python 1313项通过，总覆盖率
  90.62%；仓库策略、Ruff、格式和183个源文件的严格mypy检查通过。
- 构建：`0.1.0.dev31` sdist与wheel成功；wheel SHA-256为
  `ec52694c133d00f2ec84a3007f939d4d3c5c05971f01579e91105fdfbedd17d6`。
- 全新安装：按运行依赖哈希锁安装后安装dev31 wheel，Runtime、Watchdog、操作台静态资源、
  Windows部署脚本、Outbox压力处理器和查询`None`失败关闭均从安装环境验证通过。

## 恢复边界

解除限制必须重新取得权威Broker事实、完成对账、确认Journal/队列/时钟健康，并经过本地恢复
决策。网络、磁盘、QMT或时间同步恢复只改变健康事实，不直接恢复AUTO。

## 遗留事项

无TST-005遗留必做事项。真实QMT进程和目标机器行为由TST-006、P0及P3现场验收。
