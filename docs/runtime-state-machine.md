# Runtime状态机与模式优先级

## 1. 两个不同的控制维度

部署`Profile`决定这台机器允许执行的最高能力，`RuntimeState`决定当前时刻实际开放的能力。
二者同时生效，状态机不能扩大Profile权限。

| Profile | Runtime硬能力上限 |
|---|---|
| `READ_ONLY` | `OBSERVE_ONLY`，不进入AUTO或SELL_ONLY |
| `PAPER`、`SHADOW`、`ASSISTED`、`LIVE` | `FULL`，满足全部前置条件后可进入AUTO |
| `LIVE_EXIT_ONLY` | `EXIT_ONLY`，只允许SELL_ONLY，不进入AUTO |

`ASSISTED`下单时仍需后续执行层落实逐单人工确认；这里的`FULL`只表示状态机没有把卖出或经确认
买入从能力集合中删除。

## 2. 状态定义

| 状态 | 含义 | 自动订单能力 |
|---|---|---|
| `BOOTING` | 进程刚启动，尚未连接QMT | 无 |
| `CONNECTING` | 建立或恢复唯一QMT会话 | 无 |
| `RECONCILING` | 查询资产、持仓、委托、成交并对账 | 无 |
| `READY_PAUSED` | 预检完成但保持安全暂停 | 无 |
| `AUTO` | 计划、健康、授权和Profile均允许自动执行 | 按Profile和风控执行 |
| `PAUSE_BUY` | 停止新增买入，继续风险处置 | 卖出、撤单和对账 |
| `SELL_ONLY` | 只执行退出方向 | 卖出、撤单和对账 |
| `MANUAL` | 自动策略退出控制，保留人工接管 | 自动报单关闭 |
| `HALT` | 紧急停止交易动作 | 无新报单；活动订单按急停策略处置 |
| `STOPPING` | 受控停止和资源排空 | 无 |
| `STOPPED` | 进程生命周期结束 | 无，终态 |

每次新进程固定从`BOOTING`开始，只能按`CONNECTING → RECONCILING → READY_PAUSED`
完成启动。QMT重连也只能返回`READY_PAUSED`，不会恢复崩溃前的AUTO。

## 3. 合法迁移

```text
BOOTING -> CONNECTING -> RECONCILING -> READY_PAUSED -> AUTO
   |            |              |              |           |
   +------------+--------------+--------------+----------> HALT
                                                AUTO -> PAUSE_BUY
                                                AUTO -> SELL_ONLY
                                                AUTO -> MANUAL

任一运行态 -> CONNECTING/RECONCILING -> READY_PAUSED
任一非终态 -> STOPPING -> STOPPED
HALT --本地批准+健康+对账--> READY_PAUSED --双重许可--> AUTO
```

跳过启动对账、从`STOPPED`恢复、从`HALT`直接进入AUTO以及未列出的迁移均返回
`INVALID_RUNTIME_TRANSITION`，状态和迁移序号不变。

## 4. 收紧优先级

业务限制优先级从高到低固定为：

1. 本地Operator Emergency Halt；
2. Windows Hard Safety；
3. 独立Watchdog；
4. QMT/券商事实；
5. 中心收紧命令；
6. Runtime普通规则；
7. 策略请求。

更低权限始终可以把状态进一步收紧，但不能清除已经存在的更高优先级限制。同一状态再次收到
更高优先级限制时不伪造状态变化，返回`NOOP`并升级限制所有者。例如中心已经HALT后，本地再次
Emergency Halt会把恢复权限锁定到本地。

生命周期权限单独控制连接、对账和停止，不等同于业务恢复权限。中心
`REQUEST_AUTO_RESUME`只形成待处理申请，直接请求AUTO会被拒绝。

## 5. READY_PAUSED和AUTO前置条件

`RECONCILING → READY_PAUSED`要求：QMT连接、账户状态OK、Journal健康、完整对账、无未关闭
CRITICAL差异、无UNKNOWN订单、时钟在界内、磁盘可写且事件队列事实完整。暂停态不要求交易时段
行情健康，因此收盘后仍可完成对账和安全启动。

进入AUTO还必须满足：

- Profile映射为`FULL`；
- 行情健康；
- `RuntimeRecoveryPermit`携带有效`reconciliation_id`；
- 本地授权和中心授权同时为真；
- 调用方为Runtime本地决策或本地Operator，不是中心/策略直接修改；
- 没有更高优先级限制。

任一条件不满足时返回稳定`rejection_code`和完整`blockers`，不做部分迁移。

## 6. 安全限制解除

从HALT、MANUAL、SELL_ONLY或PAUSE_BUY向更宽松状态迁移属于限制解除，必须同时满足：

- 由`LOCAL_OPERATOR`发起；
- 当前健康事实满足恢复条件；
- 完成新一轮券商事实对账；
- Permit包含本地授权；
- 发起权限不低于限制所有者。

解除HALT先到`READY_PAUSED`。若需要AUTO，再提交一笔独立、同时具有本地和中心授权的AUTO
迁移；两个动作不会合并，便于操作台二次确认和Journal审计。

## 7. 队列溢出联动

RT-002的`QUEUE_OVERFLOW`由状态机直接处理：

- AUTO下只有普通事件丢失，进入`PAUSE_BUY`；
- 任一关键事件丢失，进入`HALT`；
- 启动/连接/对账期间发生任何事件丢失，进入`HALT`；
- 已处于更安全状态时，普通溢出返回`NOOP`，绝不反向放宽状态。

事件丢失后`event_queue_intact=false`，完成事实对账前恢复前置检查不会通过。

## 8. 单一所有者与审计输出

`RuntimeStateMachine`构造时必须绑定`DecisionEventLoop.assert_owner`。每次请求在读取或改变状态前
执行所有者检查，因此QMT回调、HTTP Worker、本地API线程和Handler派生任务都不能绕过事件循环
直接切换状态。

每个决策返回`TransitionDecision`：结果、前态、请求态、当前态、权限、原因、单调时间、阻断项和
可选迁移序号。只有真正变化的`APPLIED`结果获得递增序号并生成`RUNTIME_STATE_CHANGED`领域事件；
`REJECTED/NOOP`通过控制命令结果和审计事件留痕，不伪造状态变化。

CTRL-001通过`LocalControlCoordinator`落实本地控制的请求/结果两段审计、二次确认、预期状态
比较和重启限制投影。Emergency Halt先改变内存状态再写审计；能力放宽先写请求再检查健康与许可，
终态审计失败回落HALT。详细规则见`local-control.md`。
