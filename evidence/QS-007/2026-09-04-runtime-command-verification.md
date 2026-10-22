# QS-007 心跳、远程命令及命令回执验证记录

## 结论

QS-007 已实现节点心跳、中心命令签发、Windows 主动轮询、唯一最终结果回传和管理员查询
闭环。中心命令只表达请求，Windows 本地 Emergency Halt、Hard Safety、券商事实和人工批准
保持更高优先级；Windows 返回的本地安全拒绝会作为正确最终事实落库。

## 实现范围

量化服务 `E:\workspace\ai-stock-assistant\backend`：

- 新增 `POST /api/v1/trading/runtime/heartbeat`；
- 新增 `GET /api/v1/trading/runtime/commands`；
- 新增 `POST /api/v1/trading/runtime/commands/{command_id}/result`；
- 新增管理员命令签发和命令/最终结果查询接口；
- 新增心跳、命令批次和命令结果严格 Pydantic 模型；
- 新增 MySQL Runtime Repository 和应用服务；
- 心跳保留原始历史，并按 boot 最近时点及 boot 内最大序号更新节点当前视图；
- 命令使用节点行锁分配单调递增 `command_seq`，签发 TTL 最长 15 分钟；
- 命令 PENDING、DELIVERED、COMPLETED 生命周期可查询；
- 命令签发和唯一最终结果分别写入中央审计表；
- 4 个 Schema 和 5 个运行时示例与 Windows 契约逐字节一致。

## 专项与真实数据库验证

专项测试：

```powershell
$env:PYTHONPATH = 'backend'
backend\.venv\Scripts\python.exe -m pytest `
  backend/tests/auto_trading/test_auto_trade_runtime_api.py `
  backend/tests/auto_trading/test_auto_trade_ledger_api.py `
  backend/tests/auto_trading/test_auto_trade_event_api.py `
  backend/tests/auto_trading/test_auto_trade_plan_api.py `
  backend/tests/auto_trading/test_auto_trade_projections.py -q
```

结果：`61 passed`，其中 QS-007 新增测试 `14 passed`。Ruff 检查和格式检查通过。

真实签名 HTTP + MySQL 探针：

```powershell
$env:PYTHONPATH = 'backend'
backend\.venv\Scripts\python.exe backend/scripts/auto_trading/verify_auto_trade_runtime_api.py
```

15 项检查全部为 `true`：

- 所有节点响应签名有效；
- 心跳首次上传和精确重试均为 202；
- 心跳身份绑定不同内容返回 409；
- 同一 boot 的旧序号晚到后，当前视图仍保持较大序号；
- 节点运行版本与服务端 `last_seen_at` 更新；
- 相同命令签发请求幂等返回原命令；
- 两条命令序号为 1、2，轮询顺序和高水位为 2；
- `REQUEST_AUTO_RESUME` 被本地 HALT 拒绝后，中心接受并展示拒绝原因；
- 最终结果精确重试幂等，改变结果内容返回 409；
- 命令生命周期为 DELIVERED、COMPLETED；
- 两次命令签发和一次结果各写一条审计记录；
- 测试心跳、命令、结果、Nonce、节点和账户全部清理为 0。

## 回归结果

量化服务排除四个既有失败文件后的回归结果为 `986 passed`。四个既有问题文件单独复跑结果
为 `10 passed, 14 failed`，原有 14 项失败集合没有扩大，仍全部位于：

- `test_custom_strategy_field_matrix.py`；
- `test_mysql_sql_static.py`；
- `test_realtime_news_mysql_sql.py`；
- `test_search_dsl_field_matrix.py`。

这些失败属于真实行情数据覆盖和当前工作区并行修改，与 `auto_trade_*` Runtime 模块没有依赖关系。

Windows 侧运行时契约和本地安全优先级代码在 QS-007 中保持不变；最近一次统一质量门为
前端 `24 passed`、Python `1279 passed`、覆盖率 `90.63%`、Ruff/Mypy/构建全通过。

## 冻结语义

1. Windows 是所有跨项目通信的发起方，中心不会反向连接 Windows。
2. 心跳是最新健康信号，保留历史但不进入 Windows Durable Outbox。
3. 业务命令进入 Windows LocalJournal 后才由唯一决策循环按序应用。
4. 中心首次把命令放入响应即标记 DELIVERED；响应丢失后旧高水位仍能重新取得命令。
5. 过期命令仍会下发，由 Windows 返回 REJECTED/NOOP 以安全越过持久化序号。
6. `REQUEST_AUTO_RESUME` 只触发本地检查和人工批准流程，不能解除本地 HALT。
7. 中心在收到 APPLIED/REJECTED/NOOP/DUPLICATE/FAILED 最终结果前只显示已签发/已下发。
