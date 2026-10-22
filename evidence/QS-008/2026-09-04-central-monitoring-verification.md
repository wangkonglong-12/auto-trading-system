# EV-QS-008 中央监控、告警和 Incident 闭环验证

## 结论

QS-008 已完成。量化服务新增了不进入 Windows/QMT 决策循环的中央控制面，统一聚合节点、
计划、订单、事件投影、快照、对账和 Incident；系统巡检能够去重发现异常、自动关闭已恢复的
持续状态问题，并为人工确认和解决保存幂等审计链。Vue 管理端已提供可操作页面。

## 实现范围

- 后端总览：`GET /api/v1/trading/monitor/overview`；
- 巡检入口：Scheduler 单实例周期巡检及管理员手工巡检；
- Incident：查询、详情、确认、解决、幂等重试和完整审计轨迹；
- 规则：节点失联、节点降级、Outbox 积压、事件投影错误、命令失败、严重对账差异；
- 生命周期：持续状态恢复后自动关闭；同一失败命令只建一次；仍然存在的投影/对账问题不能被
  人工关闭永久掩盖；
- 页面：节点状态、当日计划、订单、数据同步、快照、对账和 Incident 五个视图，5秒可见态刷新；
- 安全边界：中央页面和巡检不修改 Windows 本地状态，不绕过本地 HALT、Hard Safety 或对账门。

## 验证结果

### 量化服务专项与回归

- 自动交易与调度专项：111项通过；
- 排除4个已知历史失败文件后的量化服务回归：995项通过；
- 4个历史失败文件单独复核：10项通过、14项失败，失败集合与QS-007基线一致，均属于既有选股
  数据/SQL静态检查问题，与本项无调用或文件依赖关系；
- Ruff：本项后端、脚本和测试全部通过；
- Python compileall：通过。

### 前端

- Vite生产构建：4711个模块转换并成功输出 `AutoTradeMonitor` 独立资源；
- 全仓 `vue-tsc` 仍报告6个并行工作区既有隐式`any`错误，文件为
  `StockPoolScopeCard.vue`、`CustomStrategies.vue`、`Search.vue`、`Trades.vue`和
  `UsersAdmin.vue`；本项新增页面无类型错误，生产构建通过；
- 路由和侧栏均使用管理员权限门，普通用户由现有路由守卫拒绝访问。

### 真实 MySQL 可逆探针

执行：

```powershell
cd E:\workspace\ai-stock-assistant\backend
.\.venv\Scripts\python.exe -m scripts.verify_auto_trade_monitor
```

10项检查全部为`true`：

1. 一次巡检创建4类探针 Incident；
2. 失联、投影错误、命令失败和严重对账类型识别正确；
3. 总览正确聚合失联节点和严重 Incident；
4. 账户过滤后的 Incident 数量正确；
5. 确认动作状态转换成功并写审计；
6. 同一请求完全重试返回幂等结果；
7. 新健康心跳到达后失联 Incident 自动关闭；
8. Incident 审计轨迹包含创建、确认和自动关闭；
9. 探针 ID 使用随机隔离命名；
10. 清理后节点、账户、授权、心跳、事件、命令、结果、对账、差异、Incident和审计均为0。

## 关键文件

量化服务：

- `backend/app/repositories/auto_trade_monitor_repository.py`
- `backend/app/services/auto_trade_monitor_service.py`
- `backend/app/api/auto_trading.py`
- `backend/app/core/scheduler.py`
- `frontend/src/views/AutoTradeMonitor.vue`
- `backend/tests/auto_trading/test_auto_trade_monitor_api.py`
- `backend/scripts/auto_trading/verify_auto_trade_monitor.py`
- `backend/docs/auto_trade_monitoring.md`

## 验收判断

QS-008 完成标准“节点、计划、订单、同步、差异和事故统一可见及处置闭环”已满足。下一项按顺序
进入 QS-009：密钥、节点和账户权限治理。
