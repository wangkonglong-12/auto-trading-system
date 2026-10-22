# EV-QS-009 节点、账户、权限与密钥治理验证

## 结论

QS-009 已完成。量化服务已经提供节点、账户、节点账户授权和 HMAC 密钥的统一管理员控制面，
使用安全默认态、显式前置条件、乐观并发、请求幂等和事务审计防止误开权限。真实 MySQL 与真实
签名请求验证了密钥轮换、吊销回退、授权撤销和过期拒绝；Vue 管理页已接入管理员路由。

治理模块只决定中央接口准入，不进入 Windows/QMT 决策循环，不改变 Windows 本地 HALT、
Hard Safety、对账和券商事实优先级。

## 实现范围

### 安全默认态和最小权限

- 新节点固定为 `PENDING`，新账户固定为禁用；
- 节点激活前必须具有当前有效密钥，并至少授权到一个已启用账户；
- 账户启用前必须具有已启用节点授权；
- 授权分别控制读取计划、上传事件、接收命令，并设执行模式上限；
- 授权模式不能超过账户准入模式；
- `ASSISTED/LIVE_EXIT_ONLY/LIVE` 授权及 LIVE 账户启用必须逐字确认；
- 节点禁用、账户禁用或授权撤销后，签名业务接口立即拒绝相应访问。

### 密钥治理

- MySQL 仅保存 `env:VARIABLE_NAME` 引用，接口和页面不接收、不返回密钥明文；
- 激活前实际解析中央环境变量，密钥少于 32 字节或尚未安装时拒绝；
- 新密钥内容与该节点其他可用密钥相同时拒绝；
- 新密钥激活后成为 `ACTIVE`，旧密钥只在 0 至 3600 秒有界窗口保持 `GRACE`；
- 吊销立即生效，当前密钥被吊销时只回退到仍有效的旧密钥，否则清空当前密钥；
- Windows 侧密钥继续由 Credential Manager 保存，两端使用同一随机值但不同存储实现。

### 并发、幂等和审计

- 更新请求携带 `expected_updated_at`，过期版本返回 409；
- 每项变更携带 `request_id` 和原因；完全相同请求重试返回 `duplicate=true`；
- 不同内容复用请求ID返回 409；
- 所有治理写入经 MySQL 全局命名锁串行化，并在单事务写入业务状态和审计；
- 审计保存操作人、动作、对象、请求摘要、变更前后、原因和UTC时间，不保存秘密。

### 管理接口和页面

- `GET /api/v1/trading/governance`；
- 节点登记/更新、账户登记/更新、授权新建/更新；
- 密钥引用登记、激活/轮换和紧急吊销；
- Vue “自动交易治理”管理员页面，包含节点、账户、授权和密钥四个页签；
- 路由守卫和侧栏均使用现有管理员权限门；
- 完整操作规程见量化服务 `backend/docs/auto_trade_governance.md`。

## 验证结果

### Python专项、回归和静态检查

- QS-009 API/服务专项：7项通过；
- 自动交易及Scheduler专项：118项通过；
- 排除4个已知历史失败文件后的量化服务回归：1002项通过；
- 4个历史失败文件单独复核：10项通过、14项失败，失败集合与QS-008完全一致，仍为选股数据
  覆盖和既有SQL静态检查问题，与本项治理文件无调用关系；
- Ruff：本项后端、脚本和测试全部通过；
- Python compileall：通过。

量化项目虚拟环境没有安装 Ruff；静态检查使用自动交易项目已经锁定的 Ruff 版本执行，检查
目标仍是量化项目本项文件，未跳过任何规则。

### 前端

- Vite生产构建成功，转换4714个模块并输出独立 `AutoTradeGovernance` 资源；
- 全仓 `vue-tsc` 仍只有6个并行工作区既有隐式 `any` 错误，位于
  `StockPoolScopeCard.vue`、`CustomStrategies.vue`、`Search.vue`、`Trades.vue` 和
  `UsersAdmin.vue`；新增治理页面没有类型错误；
- 页面不包含秘密值输入框，危险启用动作由前后端双重逐字校验。

### 真实 MySQL 与真实 HMAC 可逆探针

执行：

```powershell
cd E:\workspace\ai-stock-assistant\backend
.\.venv\Scripts\python.exe -m scripts.verify_auto_trade_governance
```

15项检查全部为 `true`：

1. 节点登记精确重试幂等；
2. 新账户默认禁用；
3. 授权的节点、账户和三项能力作用域正确；
4. 数据库只保存外部秘密引用；
5. 未满足前置条件时节点激活被拒绝，完整接入后成功；
6. 首把密钥可完成真实签名计划访问；
7. 轮换后新密钥为ACTIVE、旧密钥为有界GRACE，重叠期内均可验签；
8. 吊销新密钥后立即拒绝该密钥；
9. 吊销当前密钥后正确回退到仍有效旧密钥；
10. 撤销计划读取授权后，真实签名计划请求返回403；
11. 过期乐观锁版本返回409；
12. 不同内容复用请求ID返回409；
13. GRACE窗口结束后旧密钥返回401；
14. 每次治理变更只写一条审计；
15. 清理后节点、账户、授权、密钥和审计均为0。

## 关键文件

量化服务：

- `backend/app/repositories/auto_trade_governance_repository.py`
- `backend/app/services/auto_trade_governance_service.py`
- `backend/app/models/auto_trading_schemas.py`
- `backend/app/api/auto_trading.py`
- `backend/tests/auto_trading/test_auto_trade_governance_api.py`
- `backend/scripts/auto_trading/verify_auto_trade_governance.py`
- `backend/docs/auto_trade_governance.md`
- `frontend/src/views/AutoTradeGovernance.vue`
- `frontend/src/api/index.ts`
- `frontend/src/router/index.ts`
- `frontend/src/layouts/MainLayout.vue`

## 验收判断和下一依赖

QS-009 完成标准“密钥可轮换/禁用，节点只能访问授权账户”已经满足，P4量化服务增量改造全部
完成。下一关键路径是 `GOV-001/QMT-001`：需要在目标 Windows QMT 机器取得实际客户端、
XtQuant/Python版本、`userdata_mini`、账户类型和函数交易权限，先执行只读连接/查询诊断，再按
现场事实实现 P3 QMT Gateway。当前开发机没有 QMT/XtQuant，不能用 Mock 结论代替现场验收。
