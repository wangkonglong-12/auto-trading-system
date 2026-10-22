# 本地Override与Emergency Halt设计

## 1. 控制路径

Operator Console的回环API只负责认证、生成影响预览和把不可变控制请求发布到关键事件通道。
`LocalControlCoordinator`只在`DecisionEventLoop`所有者任务内执行，是本地运行模式变更的应用入口。
它调用同一个`RuntimeStateMachine`和Persistence Writer，不直接连接QMT、SQLite或中心服务。

支持的首期动作如下：

| 动作 | 目标状态 | 二次确认 | 关键约束 |
|---|---|---|---|
| `PAUSE_BUY` | `PAUSE_BUY` | 非必需 | 立即停止新增买入 |
| `SELL_ONLY` | `SELL_ONLY` | 必需 | 只开放退出方向 |
| `MANUAL_TAKEOVER` | `MANUAL` | 必需 | 自动策略退出订单控制 |
| `EMERGENCY_HALT` | `HALT` | 必需 | 先锁HALT，再写审计；明确是否撤活动订单 |
| `CLEAR_HALT` | `READY_PAUSED` | 必需 | 只能从HALT解除，必须健康、完成对账并有本地许可 |
| `REQUEST_AUTO` | `AUTO` | 必需 | 只能从READY_PAUSED单独发起，要求本地和中心双重许可 |

二次确认包含确认ID、操作员、确认时间和影响预览hash。确认必须在请求后五分钟内形成；
`CLEAR_HALT/REQUEST_AUTO`执行时的确认年龄不能超过两分钟。控制请求携带`expected_state`，页面预览
后状态已改变时返回`CONTROL_EXPECTED_STATE_MISMATCH`，禁止把旧确认应用到新现场状态。

## 2. 持久化与生效顺序

普通Override和所有能力放宽动作使用以下顺序：

1. 写入不可变`LOCAL_CONTROL_REQUEST`；
2. 校验预期状态、确认时效、Runtime健康、对账和恢复许可；
3. 由本地Operator权限请求状态机迁移；
4. 在一个Journal事务中写`LOCAL_CONTROL_RESULT`、`CONTROL_COMMAND_APPLIED`领域事件和Outbox。

`EMERGENCY_HALT`采用安全特例：决策循环先把状态锁定到HALT，再执行上述审计。即使SQLite被锁、
磁盘故障或请求审计结果不明，HALT仍保持有效。请求中固定记录`cancel_active_orders`处置选择；
订单闭环模块消费HALT状态并按该选择执行活动委托处置，处置事实继续进入订单事件和对账。

任何终态结果或事件审计失败都会立即再施加本地HALT并返回稳定错误，不能向调用方报告成功。
相同`command_id`的请求事务返回`DUPLICATE`时不重复应用状态，而是进入人工对账。

## 3. 本地优先级

所有动作以`LOCAL_OPERATOR`权限进入既有状态机。它拥有业务限制最高优先级：

- 中心可以把状态进一步收紧，但不能清除本地HALT或本地Override；
- 中心`REQUEST_AUTO_RESUME`只形成恢复申请，不能直接进入AUTO；
- HALT解除固定先到READY_PAUSED；进入AUTO必须提交另一条本地控制并同时满足中心授权；
- 结果审计失败、未完成控制或恢复记录异常统一解释为HALT。

## 4. 审计内容

请求记录包含command ID、动作、操作员、原因、请求时间、预期状态、关联ID、确认凭据、影响hash
和活动订单处置。结果记录包含前态、请求态、终态、`APPLIED/NOOP/REJECTED`、拒绝码、阻断项、
迁移序号和决定时间。两类记录使用由command ID派生的稳定记录ID和事务ID，内容以规范JSON及
SHA-256校验。

终态同时生成`CONTROL_COMMAND_APPLIED`领域事件。事件经JRN-004 Durable Outbox上传中心，页面
HTTP受理结果与最终应用结果保持分离；本地Journal始终是本地控制的原始审计证据。

UI-005接口、回执状态、幂等和错误码详见`docs/local-control-api.md`。

## 5. 重启恢复

启动恢复读取账户全部LocalControl记录并执行严格投影：

- 请求和结果必须成对、动作及操作员一致、内容hash正确；
- APPLIED/NOOP结果必须符合目标状态和迁移序号规则；
- 最后一条成功限制恢复为本地优先级限制；
- 任何只有请求没有结果的控制恢复为HALT；
- 文档损坏、孤立/重复结果或不一致字段触发`LOCAL_CONTROL_RECOVERY_INVALID`并失败关闭。

持久化HALT可在Runtime仍处于BOOTING时立即重建。重启发现仍生效的PAUSE_BUY、SELL_ONLY或
MANUAL时也统一进入本地HALT，保留原目标用于操作台说明；操作员完成启动对账后重新确认下一步
模式。恢复不自动开放任何报单能力，也不恢复崩溃前的AUTO。
