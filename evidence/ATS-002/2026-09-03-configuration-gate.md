# ATS-002 配置体系验收证据（2026-09-03）

## 交付范围

- RuntimeConfig v1.0严格模型及JSON Schema。
- READ_ONLY机器配置样例和确定性`config_hash`。
- `READ_ONLY/PAPER/SHADOW/ASSISTED/LIVE_EXIT_ONLY/LIVE` Profile。
- 节点HMAC secret及券商完整账号的`SecretProvider`隔离边界。
- 开发/测试环境变量Provider及`ATS_SECRET_*`命名约束。
- 真实委托Profile强制Windows Credential Manager引用；Provider缺失时拒绝启动。
- CLI配置校验和只包含安全字段的诊断摘要。
- QMT路径、HTTPS中心端点、回环本地API、Hard Safety、同步、对账和Tick Recorder配置。

## 安全与失败规则

- 配置文件限制为1 MiB UTF-8 JSON并拒绝重复键、未知字段和静默类型转换。
- 中央API固定HTTPS及`/api/v1/trading`路径，本地API固定`127.0.0.1`。
- Runtime固定`Asia/Shanghai`且重启模式固定`READY_PAUSED`。
- 本地Hard Safety限制之间执行交叉校验；配置中的数值字段采用十进制字符串。
- secret不进入JSON配置、配置hash、CLI摘要或ValidationError。
- SecretProvider即使错误地把secret写入内部异常，公开配置错误也会清除该详情。
- `ASSISTED`、`LIVE_EXIT_ONLY`和`LIVE`不能使用环境变量secret引用。

## 验证结果

| 验证项 | 结果 |
|---|---|
| PowerShell脚本语法 | 4个脚本通过 |
| Ruff | 通过 |
| Ruff格式一致性 | 35个文件通过 |
| mypy strict | 35个源文件无问题 |
| pytest | 117项全部通过 |
| 分支覆盖率 | 96.82%，门槛90% |
| sdist/wheel构建 | 通过，版本`0.1.0.dev1` |
| 清洁环境安装及版本查询 | 通过，输出`auto-trading-runtime 0.1.0.dev1` |
| 安装后READ_ONLY配置校验 | 通过；输出配置hash且无secret/券商完整账号 |

## 固定哈希

| 文件/内容 | SHA-256 |
|---|---|
| `config/v1/runtime-config.schema.json` | `c9a6bed672b4422657bb635d7e9ab200af3b7c670cc7aa7554d68c4a2438d1f9` |
| `config/v1/runtime.read-only.example.json` | `9fe37db95f1d089296bfe9521f4bb877ce5df6d943f32fb096e131848b53c6d4` |
| `dist/auto_trading_system-0.1.0.dev1-py3-none-any.whl` | `d1c8ef27acf97a7eebd59ba316bb530af515a47b7bdc69609b34e03b6ebbc808` |
| READ_ONLY样例规范配置hash | `sha256:19dac95d0979065026a560379a504fc6475ad351f3c05c8a481cb4ec31869e49` |

`dist`不纳入Git；正式发布时重新构建，并在包外发布清单记录最终产物hash。

## 结论与后续边界

ATS-002完成。Windows Credential Manager/DPAPI的实际Provider、安装和轮换脚本属于
SEC-001；在其完成前，真实委托Profile因Provider不可用而无法通过启动配置门。配置包
离线签名属于SEC-002，在其完成前项目不具备LIVE发布资格。
