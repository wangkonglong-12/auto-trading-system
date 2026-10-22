# QS-006 快照、中央账本与对账 API 验证记录

## 结论

QS-006 已完成。Windows 节点冻结的资产快照、持仓快照和四输入对账契约已经接入量化
服务真实签名 API；中央侧实现不可变原始账本、资产/持仓查询态投影、对账查询、差异处置
和审计闭环。Windows Runtime 调度器继续承担 QMT 主动查询和上报时点，量化服务承担接收、
集中查询和治理。

## 实现范围

量化服务 `E:\workspace\ai-stock-assistant\backend`：

- 新增 `POST /api/v1/trading/snapshots/assets`；
- 新增 `POST /api/v1/trading/snapshots/positions`；
- 新增 `POST /api/v1/trading/reconciliations`；
- 新增管理员对账列表、详情和差异处置接口；
- 增加严格 Pydantic 模型，执行上海时区、交易日、三态、四输入、摘要和差异唯一性校验；
- 增加 MySQL Repository 和应用服务，执行节点/账户授权、双幂等键、并发冲突收敛、事务写入；
- AVAILABLE 快照投影当前资产/持仓，QUERY_FAILED/UNKNOWN 保留最近可用状态；
- 差异从 OPEN 单向转为 RESOLVED/ACCEPTED_EXTERNAL，操作人、说明和前后状态写审计表；
- 原始 `report_json` 在处置前后保持一致，当前差异状态由结构化表维护；
- 冻结契约和 7 个示例与 Windows 源文件逐字节一致。

Windows 项目：

- 对账语义补充 `(category, subject_key, field)` 唯一性，与中央表约束一致；
- 版本由 `0.1.0.dev26` 升至 `0.1.0.dev27`；
- 主计划更新为 V1.35，QS-007 进入开发。

## 专项验证

量化服务专项命令：

```powershell
$env:PYTHONPATH = 'backend'
backend\.venv\Scripts\python.exe -m pytest `
  backend/tests/auto_trading/test_auto_trade_ledger_api.py `
  backend/tests/auto_trading/test_auto_trade_event_api.py `
  backend/tests/auto_trading/test_auto_trade_plan_api.py `
  backend/tests/auto_trading/test_auto_trade_projections.py -q
```

结果：`47 passed`。覆盖冻结文件一致性、全部有效示例、三态互斥、签名响应、精确重试、
内容冲突、投影触发、管理员查询、幂等处置及审计。

真实 MySQL + 签名 HTTP 探针：

```powershell
$env:PYTHONPATH = 'backend'
backend\.venv\Scripts\python.exe backend/scripts/auto_trading/verify_auto_trade_ledger_api.py
```

13 项业务检查全部为 `true`：

- 所有节点响应签名有效；
- 资产首次上传和精确重试均为 202；
- 相同快照 ID 的不同内容返回 409；
- 最新 AVAILABLE 资产和持仓已投影；
- QUERY_FAILED 持仓不会清空最近可用投影；
- 对账报告可上传、筛选和读取；
- 差异只处置一次且只写一条审计记录；
- 当前未关闭数更新为 0，原始报告仍记录接收时的 1 条 OPEN 差异；
- 测试节点、账户、Nonce、快照、对账和审计数据全部清理为 0。

## 回归验证

量化服务排除四个既有失败文件后的回归结果：`972 passed`。完整回归结果为
`981 passed, 14 failed`；14 个失败仍全部位于以下四个既有文件，失败集合没有扩大：

- `test_custom_strategy_field_matrix.py`；
- `test_mysql_sql_static.py`；
- `test_realtime_news_mysql_sql.py`；
- `test_search_dsl_field_matrix.py`。

这些失败来自当前真实行情数据覆盖和量化服务工作区内既有并行修改，与 QS-006 文件及表无关。

Windows 统一质量门使用工作区自带 Node.js 24 和 `.venv-dev` 执行，结果如下：

- 前端 ESLint、Prettier、TypeScript 和生产构建通过；
- 前端 `24 passed`；
- Python `1279 passed`；
- Python 覆盖率 `90.63%`，达到 90% 门槛；
- Ruff、格式检查和 Mypy 全通过；
- 成功构建 `auto_trading_system-0.1.0.dev27` 的 sdist 和 wheel。

机器默认 Node.js 18 在 Vitest 启动阶段缺少 `node:inspector/promises`；切换项目要求的
Node.js 24 后同一质量门通过，属于开发机工具链选择，不是产品代码故障。

## 关键语义

1. `positions=[]` 表示已知空仓，会替换当前持仓；QUERY_FAILED/UNKNOWN 表示事实未知，会保留
   最近一次 AVAILABLE 投影。
2. 快照和报告的原始 JSON 是不可变事实；管理员处置属于新的治理事实。
3. Windows 调度器负责 15:10 查询 QMT 并主动上报，中央服务接收和治理，不反向调用 Windows。
4. 原始事实已提交而即时投影失败时返回可重试故障；节点重试同一消息会继续投影。
5. 相同业务 ID 或相同 `(node_id, request_id)` 只能绑定同一份规范内容。
