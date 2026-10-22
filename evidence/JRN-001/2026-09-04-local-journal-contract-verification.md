# JRN-001 LocalJournal端口验收证据（2026-09-04）

## 交付范围

- 存储无关、运行时可检查的`LocalJournal`端口。
- 计划、命令、订单意图、券商订单关联、成交、领域事件、Outbox、本地控制和Checkpoint的强类型记录。
- 1至500项不可变操作组成的原子`JournalTransaction`，以及`COMMITTED`/`DUPLICATE`提交结果。
- 按账户和交易日限定的`JournalRecoverySnapshot`、健康状态和维护请求/结果。
- 规范JSON文档、4 MiB上限、稳定标识、内容hash、序列和事件聚合绑定校验。
- 稳定且脱敏的Journal异常边界；交易核心无需依赖SQLite类型或实现细节。

## 固定契约与安全语义

- `AppendDomainEvent`要求存储实现把领域事件及对应Outbox项放在同一事务提交。
- `EnqueueOutbox`仅用于非领域事件消息，避免事件记录与待发送记录分裂。
- 订单意图、计划和命令使用稳定业务标识及内容hash，为后续幂等提交和冲突检测提供依据。
- 恢复快照仅返回指定账户和交易日的执行状态，禁止跨账户、跨交易日污染恢复决策。
- Journal端口只暴露同步短操作；线程模型、SQLite连接和WAL细节由适配器及Persistence Writer负责。
- `journal`目录中除未来`sqlite.py`以外的文件禁止导入`sqlite3`，架构守卫已固化该约束。

## 验证结果

| 验证项 | 结果 |
|---|---|
| 契约模型 | 计划、命令、意图、订单关联、成交、事件、Outbox、控制和Checkpoint构造/拒绝矩阵通过 |
| 原子事务 | 不可变tuple、1至500项边界、操作类型和事务标识校验通过 |
| 事件与Outbox | 事件类型/聚合绑定、内容hash、Outbox状态和回执命令约束通过 |
| 恢复边界 | 账户、交易日、快照集合不可变性和健康/维护模型通过 |
| 错误边界 | 稳定错误码、retryable语义和敏感原因脱敏通过 |
| Protocol对齐 | 生产端口、测试替身和公开导出签名一致性通过 |
| 架构守卫 | 存储中立契约未导入`sqlite3`，SQLite细节保留给适配器 |
| Ruff | 通过 |
| Ruff格式一致性 | 85个文件通过 |
| mypy strict | 85个源文件无问题 |
| pytest | 837项全部通过 |
| 分支覆盖率 | 98.44%，门槛90% |
| JRN-001新增测试 | 74项，覆盖模型、端口对齐和架构边界 |
| sdist/wheel构建 | 通过，版本`0.1.0.dev12` |
| 锁定依赖独立环境安装 | 通过，输出`auto-trading-runtime 0.1.0.dev12` |
| 安装后契约冒烟 | 规范JSON、计划缓存事务和运行时Protocol检查通过 |

## 固定哈希

| 文件 | SHA-256 |
|---|---|
| `src/auto_trading_system/journal/models.py` | `7a46c8c194e0d15a1cf7d3c00bb0ab4ce328fef5d4459032a2ae827f4ef8c831` |
| `src/auto_trading_system/journal/port.py` | `08d302f1d2a8729b0815b8ecf4a244e77480e059f38668d8b0280eba4f137dac` |
| `docs/local-journal-contract.md` | `c8f00c7b9a3713d2fbca88e4803ce9a512f4f3c38b157591c41bfcd14e47eaf5` |
| `dist/auto_trading_system-0.1.0.dev12-py3-none-any.whl` | `cc1e00281725ade79e5b8f99c5f06009d9e6bb83bc3013a933f7d0a7ffed3635` |

`dist`不纳入Git；正式发布时重新构建并在包外发布清单登记最终hash。

## 结论与下一步

JRN-001完成。交易核心、恢复流程、Outbox和维护功能拥有统一且与SQLite隔离的持久化契约。
下一项直接实施JRN-002 SQLite/WAL单Writer、Schema迁移、Checkpoint、备份和健康检查。
