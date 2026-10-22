# CTRL-001 本地Override与Emergency Halt验收证据

## 验收结论

CTRL-001完成。Windows交易节点已建立只在Decision Event Loop所有者内执行的本地控制协调器，
覆盖`PAUSE_BUY`、`SELL_ONLY`、`MANUAL_TAKEOVER`、`EMERGENCY_HALT`、`CLEAR_HALT`和
`REQUEST_AUTO`。控制请求和终态结果分别写入LocalJournal，终态同时生成领域事件及Durable
Outbox。中心权限不能解除本地HALT，也不能直接进入AUTO。

## 安全语义

- 本地API后续只需发布不可变控制请求，状态改变仍由唯一决策循环串行完成；
- Emergency Halt先锁定HALT再尝试审计，SQLite写锁/磁盘故障不影响停机状态；
- 其他动作先持久化请求再应用；终态审计失败、取消或事件构造异常统一回落本地HALT；
- 相同command ID返回重复时不再次应用，进入人工对账；
- 二次确认绑定操作员、确认时间和影响预览hash，恢复确认超过两分钟或来自未来时拒绝；
- `expected_state`阻止旧页面确认作用于已变化的Runtime状态；
- Emergency Halt请求明确记录活动订单保留/撤销选择；
- `CLEAR_HALT`只到READY_PAUSED，`REQUEST_AUTO`是另一条独立控制且要求健康、对账、本地和中心许可；
- 重启严格验证请求/结果配对、字段、状态语义和文档hash，任何未决控制及所有仍生效Override均
  恢复为本地HALT，不恢复崩溃前AUTO。

## 自动化验证

2026-09-04在Windows、Python 3.12.14执行：

- 编译、Ruff、格式检查和mypy严格检查全部通过；
- 986项测试全部通过，总覆盖率94.40%；
- 64项本地控制、状态机、契约、组合根和真实SQLite专项测试通过；
- 真实SQLite/WAL验证请求/结果双审计、控制事件Schema、Outbox和跨重启HALT投影；
- 第二SQLite连接持有竞争写锁时，Emergency Halt仍先进入HALT且无半写控制记录；
- 验证中心清除本地HALT及中心直接AUTO均被拒绝；
- 验证终态审计失败和非法事件ID在状态改变后强制回落HALT；
- sdist与dev16 wheel从隔离构建环境生成成功。

## 独立安装验证

在全新虚拟环境中按hash锁定安装运行依赖，以`--no-deps`安装dev16 wheel并验证：

- CLI输出`auto-trading-runtime 0.1.0.dev16`；
- 从安装包构造恢复状态并在BOOTING阶段重建本地HALT；
- restriction authority为`LOCAL_OPERATOR`；
- 中心清除请求不改变HALT；
- 冒烟结果：`CTRL-001 installed-package smoke passed`；
- 临时验证环境已安全清理。

## 产物校验和

| 产物 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev16-py3-none-any.whl` | `5dd98a3dac768b3fc2c4c67637b4e1b701edf26cd6a9c8b8fe70dad3ed4ec181` |
| `src/auto_trading_system/control/service.py` | `ab0c73d6c0a76c05bcc9b7c2703bdd4b2ae648d7126cb5f8719d96f4f954bcd6` |
| `src/auto_trading_system/control/recovery.py` | `89850f58e455deb94e0cc67df4c1a8d03ffdf830e840551685c820bf81e60296` |
| `tests/integration/test_local_control_sqlite.py` | `509fd72a58b95934b689048a275aecc1dbdda82fcb14dcb63fdf730099c2670d` |
| `docs/local-control.md` | `4fc5c734fd924d84651b59eb36c542f8017ab7e4e5a126f2a66f217afb371928` |

## 后续工作

直接进入TEST-RT-001，形成Runtime崩溃、磁盘不可写、Writer阻塞、关键队列溢出和审计失败的
组合故障矩阵，验证全部进入规定安全模式且不会盲目报单。
