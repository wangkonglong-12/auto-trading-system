# MOCK-002 量化服务Mock API验收证据（2026-09-04）

## 交付范围

- 进程内确定性FastAPI量化服务替身和仅测试使用的组合根。
- 当前计划查询、签名204无计划响应和TradePlan ETag。
- 按`after_command_seq`过滤的命令批次、高水位及空增量。
- 事件批量上传的逐条接受、重复、临时拒绝、永久拒绝及批内混合回执。
- 资产、持仓、对账、心跳和命令最终结果的签名接收与记录。
- 节点、账户、request id和路径command id绑定。
- 一次性断网、超时及五类响应认证故障注入。
- 实际HMAC请求验签、时间窗口、Nonce防重放和响应签名。

## 固定测试语义

- Mock实现协议与传输边界，不实现选股、MySQL账本、正式权限或中央投影。
- 所有正常响应真实执行HMAC-SHA256 v1签名，客户端必须验签。
- 网络故障在读取Body和占用Nonce前发生，队列消费一次后恢复。
- 响应故障覆盖缺少签名、错误签名、错误Body Hash、错误请求绑定和过期时间戳。
- 每个事件独立返回回执；客户端只确认`ACCEPTED/DUPLICATE`，临时拒绝保留重试。
- 收到的消息以深复制快照暴露给测试，测试代码不能修改服务内部事实。
- `auto_trading_system.testing`不得被生产Runtime组合根导入。

## 验证结果

| 验证项 | 结果 |
|---|---|
| 计划和命令 | 有/无计划、ETag、序号过滤、高水位和空增量通过 |
| 节点回传 | 事件、资产、持仓、对账、心跳和命令结果通过 |
| 事件批回执 | accepted/duplicate/retryable/permanent及混合批通过 |
| 网络故障 | 断网和超时确定性一次性注入通过 |
| 响应认证故障 | 五类故障均被客户端验签拒绝，下一请求自动恢复 |
| 身份与重放 | request id、节点、账户、command id和重复Nonce均拒绝 |
| Ruff | 通过 |
| Ruff格式一致性 | 70个文件通过 |
| mypy strict | 70个源文件无问题 |
| pytest | 614项全部通过 |
| 分支覆盖率 | 98.93%，门槛90% |
| MOCK-002新增测试 | 35项；API正常流、错误流、故障和签名绑定均覆盖 |
| sdist/wheel构建 | 通过，版本`0.1.0.dev7` |
| 锁定依赖环境安装及版本查询 | 通过，输出`auto-trading-runtime 0.1.0.dev7` |
| 安装后Mock API冒烟测试 | 可从wheel创建服务、挂载FastAPI并排入故障 |

## 固定哈希

| 文件 | SHA-256 |
|---|---|
| `src/auto_trading_system/testing/central_api.py` | `58ad4c1d907efdd6050758ea6ac3ca83c5a47993bc2af856a8d84514c166f523` |
| `src/auto_trading_system/testing/__init__.py` | `a63dad46cb56e6f2fbec79c25745815925bc8a14a94bcd4dd2d4b5668032f539` |
| `docs/mock-central-api.md` | `d41e6301708f34bfc90f088a0900f64528678061dee8d5128a30b7d3b9d96c34` |
| `dist/auto_trading_system-0.1.0.dev7-py3-none-any.whl` | `146d677cb19de943b913780c3941ead6d1dcd143befa8f2134a87b37864ce880` |

`dist`不纳入Git；正式发布时重新构建并在包外发布清单登记最终hash。

## 结论与下一步

MOCK-002完成。本项目可以在不连接真实量化服务和QMT的情况下开发Sync Worker、Outbox和
端到端恢复逻辑。P1在本项目当前可执行范围内完成，下一项直接进入RT-001账户级单实例保护。
