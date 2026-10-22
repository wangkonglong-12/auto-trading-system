# SEC-001 本地安全边界

## 1. 技术落点

Windows交易节点采用以下固定实现：

| 安全对象 | 实现 | 生产接入点 |
|---|---|---|
| 节点HMAC密钥 | Windows Credential Manager Generic Credential，当前服务用户作用域 | `WindowsCredentialManagerSecretProvider` |
| 券商完整账号 | Windows Credential Manager Generic Credential，当前服务用户作用域 | `WindowsCredentialManagerSecretProvider` |
| 操作员密码 | Windows Credential Manager保存带随机盐的版本化`scrypt`校验器 | `OperatorPasswordHasher`、`LocalOperatorSessionManager` |
| 配置文件 | 保存`AutoTrading/...`凭据目标、逻辑账号和非敏感参数 | `RuntimeConfig`、`load_runtime_configuration` |
| 日志 | 注册运行时三个敏感值，先净化结构化字段，再净化最终文本和异常栈 | `SensitiveValueRedactor`、`RedactingLogFilter`、`RedactingFormatter` |
| 故障包 | 只接收文本或结构化对象，递归净化后写入有大小上限的ZIP | `DiagnosticBundleWriter` |
| 本地数据 | 专用存储根及固定子目录，DACL只授予当前服务用户和LocalSystem完全控制 | `initialize_secure_storage`、`WindowsDirectoryAclHardener` |
| Git入口 | 检查凭据文件名、私钥内容和当前`ATS_SECRET_*`精确值，含DOCX/ZIP成员 | `RepositorySecretBoundary`、`scripts/check.ps1` |

生产配置加载默认注册环境变量Provider和Windows Credential Manager Provider。Provider按配置
引用惰性读取；真实委托Profile只接受Windows Credential Manager引用。Windows API错误转换为
稳定错误码，凭据目标、操作系统消息和明文值均停留在Provider内部。

## 2. 凭据生命周期

### 2.1 安装与轮换命令

专用Windows服务用户登录交互式终端后运行：

```powershell
auto-trading-credential `
  --target AutoTrading/node-prod-01/key-202609 `
  --kind node-hmac

auto-trading-credential `
  --target AutoTrading/node-prod-01/broker-account `
  --kind broker-account

auto-trading-credential `
  --target AutoTrading/node-prod-01/operator-password `
  --kind operator-password
```

命令只从隐藏回显的两次交互输入接收值。命令行、标准输出和标准错误均不承载明文；成功输出固定
文本`credential stored`。节点HMAC值至少32个UTF-8字节，券商账号采用4至64位ASCII字母或数字，
操作员密码采用14至128个字符并只保存`scrypt`校验器。
凭据目标由`AutoTrading/<节点>/<用途>`的规范分段组成。

节点HMAC轮换采用新目标和新`node_key_id`：盘后停止Runtime，在中心注册新key id和值，在同一
服务用户凭据库写入新目标，更新本机配置引用，执行`--check-config`，启动到`READY_PAUSED`并完成
签名连通验证。旧目标保留到回滚窗口结束，再由Windows凭据管理器清理。

### 2.2 内存边界

`CredReadW`返回的系统内存由`CredFree`释放；Provider使用可变`bytearray`复制、严格解码，并在成功
或异常路径统一覆写为零。`CredWriteW`使用可变编码缓冲，调用完成后统一覆写。业务层持有
Pydantic `SecretStr`；组合根只在HMAC签名和QMT账户构造的最小调用范围取出值。

## 3. 日志和异常处理

组合根通过`build_runtime_redactor(RuntimeSecrets)`建立唯一脱敏器，并将每个日志Handler交给
`protect_log_handler`。处理顺序固定为：

1. 结构化字段按敏感键名净化；
2. HMAC、券商账号和操作员密码校验器按精确值净化；
3. Authorization、Cookie、Token、Password、Signature、Credential等文本赋值净化；
4. Formatter完成异常栈和stack text渲染后再次执行最终文本净化。

未知对象采用类型占位符进入日志，其`repr`不参与渲染；二进制正文统一显示`[REDACTED]`。递归
深度、节点数和文本长度均有上限，循环对象和超限对象以固定标记表示。逻辑`account_id`、
`node_key_id`、配置hash、Runtime状态和稳定错误码作为诊断字段保留。

## 4. 故障证据包

故障包入口为`build_diagnostic_bundle_writer(RuntimeSecrets)`。每个Artifact使用扁平、受限的`.json`
或`.txt`名称；内容先经过同一脱敏器，再以UTF-8写入ZIP。Manifest记录版本、带时区创建时间、
Artifact名称、长度和SHA-256。单Artifact上限1 MiB、总净化内容上限8 MiB、数量上限32。

导出采用同目录临时文件和最终原子替换；已存在的目标文件保持原样。Writer在写入前再次扫描已
注册敏感值的UTF-8和UTF-16LE表示。Runtime日志和故障包位于受DACL保护的`logs`与
`diagnostics`目录，本地操作台只展示`LoadedConfiguration.safe_summary()`及已净化诊断内容。

## 5. 本地目录最小权限

`build_persistence_writer`在SQLite初始化前调用`initialize_secure_storage`。初始化器建立：

```text
<storage.root>/
  data/
  logs/
  diagnostics/
  ticks/
  backups/
```

`icacls.exe`以参数数组执行，关闭继承，移除Everyone、Authenticated Users、Administrators和Users
组的显式授予，再为当前服务用户SID与LocalSystem SID设置容器/对象继承的完全控制。SID通过
`whoami.exe /user /fo csv /nh`解析并严格校验。目标必须是已解析的专用绝对目录；磁盘根目录和
符号链接均被拒绝。任一创建或ACL步骤失败时，Runtime在打开SQLite、连接QMT及启动Worker之前
终止启动。

生产Windows Task Scheduler与凭据安装命令使用同一个专用服务用户。该账户拥有QMT运行权限、
Credential Manager项和`storage.root`；LocalSystem保留系统恢复能力。Runtime本地HTTP端点固定
监听`127.0.0.1`。

## 6. 自动化验收

安全测试覆盖：

- 真实Windows Credential Manager缺失项读取与稳定错误；
- CredRead/CredWrite成功、错误映射、尺寸限制、系统内存释放和临时缓冲覆写；
- 结构化字段、格式参数、Authorization、查询参数、异常栈、循环对象和未知对象脱敏；
- 故障包路径穿越、重复名、覆盖、时区、大小限制、Manifest和内容扫描；
- 真实Windows临时目录DACL收紧后写日志和导出故障包，随后按已注册值扫描所有内容；
- Git候选文件、私钥、凭据文件名、DOCX/ZIP成员和当前开发环境敏感值扫描；
- 生产SQLite组合根在Journal创建前强制完成安全目录初始化。

SEC-002将在发布阶段增加配置签名、发布hash和交易时段变更保护；本项已经固定运行时凭据、输出
脱敏和文件权限边界。
