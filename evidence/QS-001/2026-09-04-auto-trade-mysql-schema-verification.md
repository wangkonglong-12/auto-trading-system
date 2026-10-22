# QS-001 自动交易中央表和迁移验收证据

日期：2026-09-04  
量化服务项目：`E:\workspace\ai-stock-assistant`  
中央计划项目：`E:\workspace\auto-trading-system`

## 1. 交付结果

量化服务新增自动交易协议 v1 的独立 MySQL 表族、不可变迁移、执行器、结构核验和专项测试。
迁移只允许 `CREATE TABLE IF NOT EXISTS auto_trade_*`，没有修改现有 `user_trades` 或尾盘选股表。

交付文件：

- `backend/scripts/migrations/20260904_auto_trade_protocol_v1.sql`
- `backend/scripts/auto_trading/migrate_auto_trade_protocol.py`
- `backend/tests/auto_trading/test_auto_trade_protocol_migration.py`
- `backend/docs/auto_trade_protocol_schema.md`

## 2. 数据模型范围

共建立 27 张表，覆盖：

- 节点、密钥引用、账户及节点账户授权；
- 不可变 TradePlan、计划项目和计划回执；
- 原始领域事件、投影检查点、订单和成交；
- 资产/持仓原始快照和当前投影；
- Runtime 心跳、控制命令和最终命令回执；
- 四输入对账、差异处置和 Incident；
- 请求幂等、Nonce 防重放、中央日指标和治理审计。

迁移 SHA-256：
`2d2f4f65b7ab02a19c0b17b4decae9f1f1e8f6f29f14978f89c1776b7c62a480`

## 3. 自动化验证

专项测试命令：

```powershell
cd E:\workspace\ai-stock-assistant\backend
.\.venv\Scripts\python.exe -m pytest tests\test_auto_trade_protocol_migration.py -q
```

结果：`11 passed`。

覆盖内容：

- 迁移表集合与 27 张结构契约完全一致；
- 非 `auto_trade_*` 表、`ALTER`、`DROP` 和数据写入在连接数据库前被拒绝；
- 首次执行登记文件哈希；
- 相同哈希重复执行跳过全部业务 DDL；
- 同名迁移内容漂移立即失败；
- 中途 DDL 异常不登记迁移完成状态；
- 关键唯一索引保持字段顺序和唯一性。

## 4. 真实 MySQL 验证

验证实例：MySQL `8.0.28`，数据库 `stock`。

首次执行结果：

- `applied=["20260904_auto_trade_protocol_v1.sql"]`
- `ok=true`
- `table_count=27`
- 27 张表的引擎、必需列、关键类型、索引和外键均通过；`problems=[]`。

紧接着再次执行和执行 `--verify-only`：

- 两次均为 `applied=[]`
- 两次均为 `ok=true`
- 表、索引、外键数量保持一致。

迁移登记实查：

- `migration_id=20260904_auto_trade_protocol_v1.sql`
- `file_sha256=2d2f4f65b7ab02a19c0b17b4decae9f1f1e8f6f29f14978f89c1776b7c62a480`
- `applied_at=2026-09-04 15:10:10`

## 5. 旧表不变证明

迁移前 `SHOW CREATE TABLE user_trades` 的 SHA-256：
`af0e886a3e48700ee51ac8f9b173306bfd774d43b0f223d4cfc2e15e55290787`

迁移后同一建表定义的 SHA-256：
`af0e886a3e48700ee51ac8f9b173306bfd774d43b0f223d4cfc2e15e55290787`

哈希完全一致；迁移 SQL 的实际语句也不包含 `user_trades`。现有手工交易语义保持不变。

## 6. 结论

QS-001 完成。量化服务已经具备后续 Plan Publisher、事件接收、投影、快照、命令、对账和治理
功能所需的独立中央数据边界；迁移可重复执行，结构冲突会失败关闭，旧手工交易表未受影响。
