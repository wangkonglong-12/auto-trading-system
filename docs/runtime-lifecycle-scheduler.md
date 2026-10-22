# Runtime内部调度与受控生命周期

## 1. 进程边界

Windows Task Scheduler只启动一个`auto-trading-runtime`长期进程和独立Watchdog，不按买入计划、
结果回传或账单任务反复启动Python脚本。计划拉取、命令拉取、事件回传、快照、对账和盘后维护
都由该Runtime内唯一`RuntimeScheduler`触发。

调度回调只向RT-002的有界入口发布`ScheduledRuntimeEvent`。它不导入Broker/Market Gateway、
`xtquant`、SQLite或HTTP客户端，因此一次或多次调度触发都不会创建第二个QMT会话，也不会在
APScheduler线程里执行交易、数据库或网络操作。

## 2. 固定任务表

| Job ID | 事件 | 默认节奏 | 优先级 | 实际工作执行者 |
|---|---|---|---|---|
| `plan-sync` | `PLAN_SYNC` | 活跃2秒，空闲30秒 | 普通 | Sync Worker拉取冻结计划 |
| `command-sync` | `COMMAND_SYNC` | 2秒 | 普通 | Sync Worker发起20秒内受控长轮询 |
| `event-upload` | `EVENT_UPLOAD` | 1秒 | 普通 | Outbox Worker批量回传事件 |
| `heartbeat` | `HEARTBEAT` | 活跃5秒，空闲30秒 | 普通 | Sync Worker发送当前只读快照 |
| `position-snapshot` | `POSITION_SNAPSHOT` | 15秒 | 普通 | Broker Worker查询持仓 |
| `asset-snapshot` | `ASSET_SNAPSHOT` | 30秒 | 普通 | Broker Worker查询资产 |
| `active-order-reconciliation` | `ACTIVE_ORDER_RECONCILIATION` | 2秒 | 关键 | Broker Worker查询活动委托/成交 |
| `post-close-reconciliation` | `POST_CLOSE_RECONCILIATION` | 工作日15:10 | 关键 | Reconcile流程执行四事实对账 |
| `wal-checkpoint` | `WAL_CHECKPOINT` | 工作日15:20 | 普通 | Persistence Writer执行Checkpoint |
| `journal-backup` | `JOURNAL_BACKUP` | 工作日15:30 | 普通 | Persistence Writer调用Backup API |
| `tick-retention` | `TICK_RETENTION` | 工作日15:40 | 普通 | Recorder Worker清理过期分区 |

所有时间固定为`Asia/Shanghai`。交易所休市日由决策Handler读取版本化交易日历后返回NOOP，不能
只依赖周一至周五。每个Job固定`coalesce=true`、`max_instances=1`，错过多次只合并为一次控制
事件；盘后任务允许30分钟misfire窗口，其他任务默认30秒。

活跃/空闲切换使用`set_interval()`原地调整既有Job，不重建Scheduler、Runtime或QMT会话。

## 3. 调度回调安全语义

- Scheduler成功启动后才接受触发；启动过程中的抢跑回调计入`suppressed`且不发布。
- 停止时先关闭接受开关，再等待APScheduler停止，竞态回调不会产生新业务任务。
- 发布结果分别计入`accepted/overflowed/closed`；队列满由RT-002锁存溢出并由RT-003降级。
- 全局`fire_sequence`便于识别调度顺序；业务幂等仍使用计划、命令、快照和请求自身ID。
- Handler把控制事件分派给单实例Worker；耗时操作不占用APScheduler回调或决策Handler。
- 调度异常使用稳定错误码，错误文本不携带凭据、账户或下游原始异常内容。

## 4. 启动顺序

`ManagedRuntime`按以下固定Phase启动：

1. 获取真实券商账户的Global OS Mutex；
2. `PERSISTENCE`：打开LocalJournal并验证可写、迁移和健康；
3. `DECISION_LOOP`：创建唯一决策任务；
4. `BROKER_SESSION`：创建唯一XtQuantTrader、连接和订阅；
5. `WORKER`：启动Persistence、Sync、Recorder等Worker；
6. `SCHEDULER`：注册并启动唯一内部Scheduler；
7. `CONTROL_API`：最后开放127.0.0.1本地操作台API。

除`WORKER`可有多个外，Persistence、Decision Loop、Broker Session、Scheduler和Control API
每个Phase只允许一个组件。相同Phase内保持组合根声明顺序。

每个组件进入启动前就登记为“需要清理”，因此即使`start()`只完成一半后抛错，编排器也会调用
它的幂等`stop()`。任一启动失败按已登记组件的逆序回滚，最后释放账户Mutex；回滚失败会列出
稳定组件名，Runtime进入`FAILED`。

## 5. 停止顺序

停止严格按启动逆序执行：

```text
Control API停止接收操作
→ Scheduler停止产生任务
→ Workers停止并提交/返回最后结果
→ Broker Session取消订阅并停止QMT回调
→ Decision Loop排空已接收事件后停止
→ LocalJournal关闭
→ 释放账户Global Mutex
```

每个组件即使停止失败，后续组件和账户锁释放仍会继续尝试。账户锁释放结果不明确时
`owns_account=true`保留，状态标记`FAILED`，允许再次调用stop重试，绝不假装已经释放。

正常停止、启动前停止和重复停止幂等；已停止Runtime不能原地重新启动。需要恢复时创建新进程，
重新取得Mutex，并按RT-003从`BOOTING`走到`READY_PAUSED`。

## 6. 组件契约

每个`RuntimeComponent`必须提供异步、幂等且可处理“部分启动”的`start/stop`。组件错误不得包含
secret。Broker组件独占QMT会话；Worker只能使用注入的Gateway端口，不能创建Trader。

`DecisionLoopComponent`只创建一个名为`runtime-decision-loop`的asyncio Task，停止时关闭入口并
等待排空；同一适配器无论正在运行还是已经停止，都拒绝第二次start。

## 7. 验收重点

- 任意数量的计划/同步/盘后触发只使Broker组件启动一次；
- 启动第N个组件失败时，前N个组件含部分启动者均逆序停止；
- 多组件停止失败仍会尝试全部清理并报告完整失败组件集合；
- Scheduler回调并发触发时序号唯一、入口容量有界、停止后全部抑制；
- APScheduler真实适配器在当前asyncio循环以Asia/Shanghai启动和停止；
- 构建后的wheel能够组合账户锁、决策循环、Scheduler和生命周期适配器。
