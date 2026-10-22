# Runtime单一决策循环与有界队列

## 1. 设计目标

`DecisionEventLoop`是交易节点内唯一可改变实时业务状态的执行任务。QMT回调线程、行情线程、
中心同步Worker、Scheduler、持久化Writer、Watchdog和本地操作台都只能把不可变消息发布到
`BoundedEventIngress`，不得直接修改订单、持仓、计划、风控或Runtime状态。

这个边界解决三类稳定性问题：回调并发造成状态竞争、慢消费者造成内存无限增长、故障处理与
正常事件交错导致安全模式被覆盖。

## 2. 固定运行结构

```text
QMT/Broker callback ─┐
Market callback ─────┤
Sync/Scheduler ──────┼─> BoundedEventIngress ─> DecisionEventLoop ─> 单一状态所有者
Journal/Watchdog ────┤      critical: 256            串行dispatch
Operator Console ────┘      normal: 10000
```

- 入口是线程安全、非阻塞发布的双通道有界队列。
- `critical`通道拥有独立预留容量，处理优先于`normal`通道；每个通道内部保持FIFO。
- 每次成功入队获得全局递增`ingress_sequence`和单调时钟时间。
- 决策循环生成独立递增`dispatch_sequence`，同一时刻只等待一个Handler完成。
- Handler改变业务状态前调用`assert_owner()`；其他线程和Handler派生的子任务均被拒绝。
- Handler异常转换为稳定错误`DECISION_HANDLER_FAILED`，关闭入口、丢弃未处理事件并将循环
  标记为`FAILED`，禁止继续交易。

## 3. 事件来源和优先级

固定来源为`BROKER`、`MARKET`、`SYNC`、`SCHEDULER`、`OPERATOR`、`PERSISTENCE`、
`WATCHDOG`和`INTERNAL`。调用方必须传入强类型枚举，字符串不会被静默接受。

关键通道用于会影响交易许可或事实完整性的低频消息，包括：

- QMT连接断开、账户状态异常和委托/成交关键回调；
- Emergency Halt、本地暂停和授权收回；
- Journal提交失败、磁盘空间危险和Writer故障；
- Watchdog判定的时钟漂移、Runtime失活或恢复异常。

Tick、普通快照刷新、中心轮询结果和常规调度信号进入普通通道。完整Tick Recorder使用独立
副本队列，不把海量Tick写入决策队列，也不把Tick写入SQLite。

## 4. 满载与安全降级

发布结果固定为`ACCEPTED`、`OVERFLOW`或`CLOSED`。任一通道满载时，不阻塞QMT回调线程，
而是原子累计丢弃数量、优先级、来源和首末单调时间。决策循环在处理下一个业务事件之前先派发
`QUEUE_OVERFLOW`报告，因此状态机必须先执行以下降级：

| 溢出类型 | RT-003状态机动作 | 恢复条件 |
|---|---|---|
| 只有普通事件丢失 | 进入`PAUSE_BUY`，允许受控撤单和卖出 | 队列恢复、完成券商事实对账并经本地确认 |
| 任一关键事件丢失 | 进入`HALT` | 完成对账、原因清除并按HALT人工恢复流程处理 |

溢出报告是控制信号，不尝试伪造已丢失业务内容。只要发生丢失，节点就不能假设本地事实完整，
必须以QMT查询结果重建事实。RT-003负责状态动作，后续对账模块负责恢复判定。

Persistence Writer的当前执行请求不计入队列`pending`，因此故障检测同时比较`accepted`与
`completed`。只要存在未完成请求且提交进度超过阈值没有前进，即生成`PERSISTENCE_STALLED`；
Writer进入`FAILED`、队列拒绝或Journal报告低磁盘/不可写，也都通过关键事件进入统一故障策略。
所有此类存储故障进入`HALT`。故障信号按当前原因锁存，健康恢复后才允许再次报告。

报单权限不跨异步等待缓存。订单意图提交前检查一次，提交完成且即将调用Broker前再次检查；
因此Writer阻塞、磁盘告警或急停与报单并发发生时，较新的安全状态会在外部调用边界生效。

## 5. 生命周期

- 新实例处于`CREATED`，只允许调用一次`run()`。
- 正常停止先关闭生产入口并排空已接收事件，状态依次为`RUNNING → STOPPING → STOPPED`。
- Handler在`STOPPING`排空期间仍是合法唯一所有者。
- 紧急停止使用`discard_pending=True`，保留当前正在处理的原子Handler，丢弃其余排队事件。
- 启动前收到停止请求会直接关闭为`STOPPED`；停止或失败后的实例不能重新启动。
- asyncio任务被取消时入口关闭、待处理事件丢弃并进入`STOPPED`。

进程再次启动会创建全新决策循环，并始终从`READY_PAUSED`开始；是否恢复交易由启动对账和
人工/中央许可共同决定。

## 6. 回调与Handler约束

QMT回调必须先把SDK对象复制为项目中立、不可变的值对象，再调用`publish()`。回调中禁止HTTP、
SQLite、等待Future、同步查询QMT或执行策略计算。发布返回`OVERFLOW/CLOSED`时只记录限频结构化
诊断；安全动作由已锁存的控制信号在决策循环内执行。

Handler必须保持短小，负责校验、确定性状态转换和发出命令。HTTP、SQLite和文件压缩等阻塞I/O
交给对应Worker；Worker完成后把结果事件送回入口。业务模块不得保存可由Worker并发修改的共享
可变对象。

## 7. 配置和可观测性

`RuntimeConfig.runtime_queues`提供两个资源上限：

- `normal_capacity=10000`，允许范围100至1000000；
- `critical_capacity=256`，允许范围16至10000。

运行指标至少包括两通道当前深度/容量、发布总数、各来源溢出数、最高排队时长、Handler时长、
最后`dispatch_sequence`和循环状态。密集事件测试必须验证发布线程不会等待Handler，队列内存有界，
溢出先于后续业务事件触发安全动作。
