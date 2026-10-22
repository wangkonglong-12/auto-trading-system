# Runtime故障策略与注入验收

## 1. 安全目标

Runtime把存储、队列和组件故障统一转换为不可放宽交易权限的状态迁移。故障处理只能由
`DecisionEventLoop`所有者执行；检测器只生成不可变事实，不在Writer线程、监控任务或QMT
回调中直接修改Runtime状态。

关键原则是：异步持久化成功不等于仍有报单许可。任何订单在Broker调用前必须重新读取当前
Runtime状态，不能复用等待持久化之前的判断。

## 2. 故障路由

| 故障事实 | 来源组件 | 状态动作 | Authority | 报单结果 |
|---|---|---|---|---|
| `BROKER_QUERY_UNAVAILABLE` | Broker恢复查询 | `HALT` | `HARD_SAFETY` | `None`或非强类型结果不得解释为空事实 |
| `CLOCK_SKEW_UNSAFE` | 时间同步健康探针 | `HALT` | `HARD_SAFETY` | 超过本地30秒上限时阻断全部新委托 |
| `JOURNAL_DISK_PRESSURE` | Journal健康探针 | `HALT` | `HARD_SAFETY` | 阻断全部新委托 |
| `JOURNAL_UNWRITABLE` | Journal健康探针 | `HALT` | `HARD_SAFETY` | 阻断全部新委托 |
| `PERSISTENCE_QUEUE_FULL` | Persistence Writer调用方 | `HALT` | `HARD_SAFETY` | 拒绝当前写请求，阻断全部新委托 |
| `PERSISTENCE_STALLED` | 单调时钟停滞检测 | `HALT` | `WATCHDOG` | 已提交请求完成后仍重新检查权限 |
| `PERSISTENCE_FAILED` | Writer状态快照 | `HALT` | `WATCHDOG` | 阻断全部新委托 |
| `RUNTIME_COMPONENT_CRASH` | 生命周期/Watchdog | `HALT` | `WATCHDOG` | 重启后保持非AUTO并先对账 |
| `CONTROL_AUDIT_UNRESOLVED` | 本地控制恢复 | `HALT` | `LOCAL_OPERATOR` | 人工处置审计缺口 |

普通事件队列发生可确认的非关键丢失时，沿用状态机规则进入`PAUSE_BUY`；只要有关键事件丢失
则进入`HALT`。故障策略不会自动恢复AUTO。

中心断网首先由Durable Outbox保留原事实并整批重试。积压进入`HIGH`时，决策循环内的
`OutboxPressureDecisionHandler`从AUTO收紧到`PAUSE_BUY`；进入`CRITICAL`时收紧到`HALT`；
积压恢复`NORMAL`只记录无状态放宽的NOOP，继续保持当前限制。

## 3. 检测边界

`PersistenceStallDetector`按单调时钟检查`accepted - completed`，因此能识别Writer线程当前
正在执行但队列深度已经为零的请求。完成计数前进会重新开始计时；同一停滞只发出一次信号，
恢复后才能再次触发。计数回退、非法状态和时钟回退均视为监控输入不一致并明确报错。

`JournalHealthFaultDetector`把`LOW_DISK_SPACE`提升为磁盘压力故障，把`UNAVAILABLE`或
`writable=false`提升为不可写故障。不可写优先于低磁盘；同类故障按原因锁存，健康后解除锁存。

## 4. 已执行故障矩阵

| 注入方式 | 验证点 | 预期结果 |
|---|---|---|
| 阻塞真实Persistence Writer线程的Journal提交 | 当前请求`pending=0`但仍被识别为停滞 | Runtime进入HALT；释放阻塞后意图虽落盘，Broker订单为零 |
| SQLite设置高于实际剩余空间的安全低水位 | 使用真实SQLite健康探针产生`LOW_DISK_SPACE` | Runtime进入HALT；意图写入和Broker调用均为零 |
| 容量为1的Writer队列持续压入请求 | 第三个请求立即返回`PERSISTENCE_QUEUE_FULL` | 调用方不阻塞；Runtime进入HALT |
| 子进程在WAL提交后强制退出并重开数据库 | 已提交事务、Outbox和序号恢复一致 | 重启不恢复AUTO；历史意图不得走普通重报路径 |
| 本地控制结果审计遇到SQLite竞争写锁 | Emergency Halt先于审计写入 | HALT立即生效；重启恢复仍保持HALT |
| 中心断网并把Outbox推入HIGH/CRITICAL | 签名传输失败整批重试，压力事件走关键通道 | HIGH进入PAUSE_BUY，CRITICAL进入HALT，NORMAL不自动恢复 |
| QMT断线后重连 | Broker事实驱动CONNECTING和RECONCILING | 对账后只到READY_PAUSED |
| Broker查询返回`None` | 强类型查询边界拒绝非权威结果 | 生成BROKER_QUERY_UNAVAILABLE并进入HALT |
| 时间偏差超过本地上限 | Hard Safety与时钟健康故障双重检查 | 当前委托拒绝，Runtime进入HALT |

上述测试全部使用生产端口和状态机。阻塞/队列场景使用可控Journal替身驱动真实Writer线程，
磁盘阈值和崩溃恢复使用真实SQLite/WAL文件。

## 5. 恢复要求

解除HALT必须先确认Journal可写、Writer有提交进展、关键队列完整，并查询券商委托、成交、持仓
和资产完成对账。随后通过本地`CLEAR_HALT`回到`READY_PAUSED`，最后用新的对账许可和本地/中心
双重授权单独申请AUTO。禁止因探针恢复健康而直接恢复自动交易。
