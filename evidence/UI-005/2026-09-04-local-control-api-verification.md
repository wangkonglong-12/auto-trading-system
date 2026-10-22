# UI-005 本地控制API、回执和审计链验收证据

## 验收结论

UI-005完成。本地控制API已建立`POST /api/v1/local/controls`与
`GET /api/v1/local/controls/{command_id}`。提交接口只负责Journal幂等检查、注册有界进度回执并把
不可变命令以关键优先级发布给唯一决策循环；HTTP 202 `ACCEPTED`与最终
`APPLIED/NOOP/REJECTED/FAILED/UNRESOLVED`状态明确分离。

决策处理器在唯一状态所有者内复用CTRL-001的`LocalControlCoordinator`，保持请求持久化、状态迁移、
结果/领域事件/Outbox审计顺序。重启后的回执从SQLite/WAL审计记录重建，只有请求没有结果时返回
`UNRESOLVED`并要求对账。Uvicorn作为Runtime的`CONTROL_API`组件最后启动、最先停止。

## 关键行为验证

| 行为 | 结果 |
|---|---|
| 新命令 | HTTP 202、`ACCEPTED`、独立`status_url`、关键OPERATOR事件 |
| 进程内相同重试 | 返回同一回执且不重复入队 |
| 重启后相同重试 | 返回Journal终态HTTP 200且不重复迁移 |
| 同ID不同正文 | HTTP 409 `LOCAL_CONTROL_IDEMPOTENCY_CONFLICT` |
| 队列满/关闭 | HTTP 503稳定错误，释放未发布的回执槽位 |
| Journal不可读 | HTTP 503 `LOCAL_CONTROL_AUDIT_UNAVAILABLE`，拒绝新命令 |
| 未知命令 | HTTP 404 `LOCAL_CONTROL_RECEIPT_NOT_FOUND` |
| 回执容量 | 只淘汰最早终态；活动命令占满时HTTP 503 |
| 域校验 | 动作、预期状态、确认、时间和活动订单处置跨字段校验通过 |
| 安全输出 | API错误不包含数据库、线程、路径和底层异常内容 |

## 自动化验证

2026-09-04在Windows、Node 24.19.0、pnpm 11.19.0和Python 3.12.14执行完整质量门：

- 10项前端测试通过，前端语句覆盖率96.55%、分支92.10%、函数92.85%、行100%；
- repository secret boundary、编译、Ruff、格式检查和mypy严格检查全部通过；
- 1187项Python测试全部通过，总覆盖率91.57%；
- HTTP→关键队列→DecisionEventLoop→Persistence Writer→SQLite/WAL→状态机→Outbox完整链路通过；
- Emergency Halt初始返回202，最终回执为APPLIED且Runtime从AUTO进入HALT；
- SQLite重启后GET与幂等POST均恢复同一APPLIED终态；
- 真实Uvicorn在IPv4回环地址使用随机端口完成启动ready和受控停止；
- sdist与dev22 wheel从隔离构建环境生成成功。

## 独立安装验证

全新虚拟环境从dev22 wheel解析并安装全部声明依赖，随后完成：

- 包版本为`0.1.0.dev22`；
- wheel内Console静态入口通过`127.0.0.1`返回HTTP 200；
- `LocalControlCommandBody`从JSON恢复并通过领域不变量校验；
- `LocalConsoleServer`及全部UI-005模块可从安装包导入。

## 产物校验和

| 产物 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev22-py3-none-any.whl` | `93e0a6b01b2358589a79b5eab197b3dd9dc56780d611e3774e890357332b7ef6` |
| `src/auto_trading_system/control/dispatch.py` | `3b3cefc7816d050b075bb10bde86621767fa02f1e5fcb35bafc43d7aa29c2d3c` |
| `src/auto_trading_system/control/recovery.py` | `462f876de5cf836b331e2191e535016f092295fd67c74b3da8d918f798237b70` |
| `src/auto_trading_system/apps/local_console/control_api.py` | `daf66bd8e872549004fb5cc6e4ee9e4de3d7171f53b402154464956fbba1faf8` |
| `src/auto_trading_system/apps/local_console/server.py` | `ff173e074caea41096c228ab473055aaa51fef87e6955ef4954715cb8aa104bd` |
| `src/auto_trading_system/apps/local_console/composition.py` | `121e83593e2c4020ec285418716b05cb6971841febe9ebbe1094ffaf37df76b4` |
| `tests/integration/test_local_control_api_sqlite.py` | `707c8fa9108d99395ba7c053b129e52e18816698c81008417dea9daa57e11355` |
| `tests/integration/test_local_console_uvicorn.py` | `02436655de8bbeaa968d595fe2f7185f26d0bd6bd999fa76d19705897c298a07` |
| `docs/local-control-api.md` | `f6e65155c3673de42b398904661aa1bb8095b6d0582d782c4b02cd9d03610ef5` |

## 后续工作

UI-006依赖的UI-001、UI-005与SEC-001均已完成，下一项直接接入Windows操作员短会话、同源CSRF、
服务端影响预览、危险操作二次确认和权限审计。
