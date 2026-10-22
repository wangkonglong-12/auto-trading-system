# Windows节点配置设计

## 1. 配置分层

| 配置层 | 内容 | 载体 | 变更方式 |
|---|---|---|---|
| TradePlan策略配置 | Selection、组合、Entry、Exit、Risk、Execution | 中心签名TradePlan | 计划版本治理 |
| Windows机器配置 | QMT路径、逻辑账号、端点、周期和资源限制 | RuntimeConfig JSON | 盘后替换并重启 |
| Windows Hard Safety | 总/单票仓位、金额、订单数、行情和时钟上限 | RuntimeConfig JSON | 离线审批、签名和重启 |
| Sensitive Values | 节点HMAC secret、券商完整账号 | SecretProvider | 独立轮换，不写配置文件 |
| 本地Override | Halt、接管和冻结状态 | LocalJournal | 控制事件持久化并审计 |

机器配置只负责基础设施和不可突破的本地上限。策略目标来自TradePlan，Runtime执行时取
计划限制与Hard Safety的较小值。

## 2. 启动规则

配置加载按固定顺序执行：限制文件大小并读取UTF-8 JSON、拒绝重复键、执行严格Schema
校验、校验字段关系、选择SecretProvider、解析敏感值、计算规范配置hash。任何步骤失败
都以稳定错误码退出，且错误消息不包含原始配置内容和secret。

所有Profile固定`timezone=Asia/Shanghai`、`restart_mode=READY_PAUSED`、中央端点使用HTTPS，
本地API固定监听`127.0.0.1`。QMT路径和数据根目录必须是绝对路径且不能相同。

## 3. 安全边界

- JSON文件保存逻辑`account_id`和脱敏别名；券商完整账号通过SecretProvider加载。
- Pydantic `SecretStr`负责内存对象的默认显示和JSON脱敏，业务代码只有在调用QMT或签名
  的最小范围内才能显式取值。
- `safe_summary()`是日志、诊断和操作台唯一允许使用的配置摘要。
- 环境变量Provider只允许`ATS_SECRET_*`白名单命名，并限制在非真实委托Profile。
- `ASSISTED`、`LIVE_EXIT_ONLY`和`LIVE`要求Windows Credential Manager引用；Provider未
  安装或secret缺失时启动失败。
- JSON Schema和配置hash随发布包保存；交易时段不热更新机器配置。

## 4. 后续衔接

`SEC-001`已经实现Windows Credential Manager Provider、交互式安装/轮换命令、统一日志与故障包
脱敏、存储目录DACL和Git入口扫描，详见`docs/local-security.md`。`SEC-002`为机器配置与
Hard Safety包增加离线签名验证。`ATS-003`提供金额、数量、证券代码和交易日领域类型，后续配置
字段逐步切换为这些统一值对象。
