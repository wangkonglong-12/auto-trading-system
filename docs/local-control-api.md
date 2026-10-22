# UI-005 本地控制API、回执和审计链

## 1. 运行拓扑

本地控制API与Operator Console由同一个FastAPI应用提供，Uvicorn固定监听
`127.0.0.1:<local_api.port>`。API作为`CONTROL_API`生命周期组件最后启动、最先停止。

控制链路固定为：

```text
Operator Console
  -> POST /api/v1/local/controls
  -> 关键优先级事件入口
  -> DecisionEventLoop唯一状态所有者
  -> LOCAL_CONTROL_REQUEST持久化
  -> RuntimeStateMachine迁移
  -> LOCAL_CONTROL_RESULT + DomainEvent + Outbox原子提交
  -> GET /api/v1/local/controls/{command_id}
```

`LocalControlSubmissionService`负责HTTP命令准入和幂等检查，`LocalControlDecisionHandler`只在
决策循环内调用`LocalControlCoordinator`。`JournalLocalControlReceiptReader`通过Persistence
Writer读取历史，API、页面和决策处理器均不直接打开SQLite。

## 2. 接口

### 2.1 提交命令

`POST /api/v1/local/controls`

请求使用`application/json`，字段如下：

| 字段 | 含义 |
|---|---|
| `command_id` | 客户端命令幂等标识，格式`cmd_*` |
| `action` | `PAUSE_BUY`、`SELL_ONLY`、`MANUAL_TAKEOVER`、`EMERGENCY_HALT`、`CLEAR_HALT`或`REQUEST_AUTO` |
| `actor_id` | 本地操作员标识；UI-006由登录会话绑定 |
| `reason_code` | 稳定的大写原因码 |
| `requested_at` | 上海时区请求时间 |
| `expected_state` | 影响预览时看到的Runtime状态 |
| `correlation_id` | 本次操作链路关联标识 |
| `confirmation` | 确认ID、操作员、确认时间和影响预览SHA-256 |
| `cancel_active_orders` | Emergency Halt的活动订单处置选择 |

新命令进入关键事件队列后返回HTTP 202和`status=ACCEPTED`。该状态只表示Runtime已接收不可变
命令，`final=false`，页面按`status_url`查询最终回执。相同`command_id`与完全相同正文重复提交
返回现有回执；相同标识对应不同正文返回HTTP 409。

当命令已经在Journal形成终态时，重复提交返回HTTP 200和持久化终态，不会再次发布或再次迁移。

### 2.2 查询回执

`GET /api/v1/local/controls/{command_id}`

状态含义：

| 状态 | 是否终态 | 含义 |
|---|---:|---|
| `ACCEPTED` | 否 | 已进入关键事件队列 |
| `PROCESSING` | 否 | 决策循环正在持久化和应用 |
| `APPLIED` | 是 | 状态迁移成功且结果审计已提交 |
| `NOOP` | 是 | Runtime已经处于目标状态且结果审计已提交 |
| `REJECTED` | 是 | 预期状态、健康、许可或状态机规则拒绝，结果已审计 |
| `FAILED` | 是 | 持久化或处理失败，返回稳定`error_code`和对账要求 |
| `UNRESOLVED` | 是 | 重启后发现只有持久化请求没有结果，Runtime按HALT恢复并要求对账 |

终态返回前态、现态、迁移序号、拒绝原因和阻断项。`FAILED/UNRESOLVED`返回`retryable`与
`requires_reconciliation`。读取不到格式正确的命令返回HTTP 404。

## 3. 持久化和幂等

首次提交前查询账户LocalControl审计历史，确保重启后的重复请求不会再次进入决策循环。进程内
使用有界回执注册表保存`ACCEPTED/PROCESSING`和近期终态；容量满时只淘汰最早终态，所有槽位均
为活动命令时返回HTTP 503。

决策循环首先把命令标记为`PROCESSING`，再调用既有`LocalControlCoordinator`。普通控制先提交
请求记录，再验证状态并迁移；Emergency Halt先锁定HALT再审计请求。终态结果与领域事件在同一
事务提交，事件进入Durable Outbox回传中央服务。

Runtime重启后，查询接口从Journal重建`APPLIED/NOOP/REJECTED`回执。只有请求没有结果的记录返回
`UNRESOLVED`，与启动恢复投影使用同一套完整性校验。

## 4. 失败语义

| HTTP | 稳定错误 | 处置 |
|---:|---|---|
| 409 | `LOCAL_CONTROL_IDEMPOTENCY_CONFLICT` | 保留原命令并核对调用方标识生成逻辑 |
| 404 | `LOCAL_CONTROL_RECEIPT_NOT_FOUND` | 校验命令ID或重新读取页面状态 |
| 503 | `LOCAL_CONTROL_EVENT_QUEUE_FULL` | 队列溢出安全信号负责收紧Runtime，页面显示提交失败 |
| 503 | `LOCAL_CONTROL_EVENT_LOOP_UNAVAILABLE` | Runtime控制通道未运行，页面显示本地控制不可用 |
| 503 | `LOCAL_CONTROL_AUDIT_UNAVAILABLE` | Journal状态未知，拒绝接收新命令 |
| 503 | `LOCAL_CONTROL_RECEIPT_CAPACITY_EXHAUSTED` | 活动命令已占满有界注册表，进入运行诊断 |

所有错误响应只包含稳定码和边界消息，底层数据库、线程、路径和异常内容进入受保护的脱敏日志。
所有API响应继续应用Console的CSP、Host白名单和`no-store`响应策略。

## 5. 生命周期

`LocalConsoleServer`封装Uvicorn后台任务和明确状态：`CREATED -> STARTING -> RUNNING -> STOPPING
-> STOPPED`。启动等待服务器ready，超时或绑定失败返回`LOCAL_CONSOLE_START_FAILED`；停止超时
强制取消服务任务并返回`LOCAL_CONSOLE_STOP_TIMEOUT`。

`build_local_control_api_runtime`创建一套共享的回执注册表、Journal读取器、提交服务、决策处理器、
FastAPI应用、Uvicorn服务和`RuntimeComponent`。完整Runtime把该组件放在Persistence、决策循环、
QMT会话、Worker和Scheduler之后启动，并在停止时按相反顺序关闭。

## 6. 安全边界

UI-006已经把控制写接口固定在Windows操作员会话之后。`POST /controls`要求有效HttpOnly会话、
精确同源请求、CSRF令牌和`X-Control-Authorization`。授权由服务端影响预览和逐字口令确认生成，
绑定账户、Runtime状态、委托、持仓、连接、对账事实以及动作参数。

`GET /controls/{command_id}`要求有效操作员会话。服务重启撤销短时会话和授权，Journal终态仍可在
重新登录后查询。完整端点、配置、操作菜单和错误语义见`docs/operator-console-security.md`。
