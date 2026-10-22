# 跨项目HMAC认证与防重放协议

## 1. 安全目标

量化服务和Windows交易节点对`/api/v1/trading`流量使用TLS和HMAC-SHA256。TLS负责传输机密性
与服务器身份，HMAC负责节点身份、消息完整性和请求/响应关联。HMAC不代替HTTPS。

协议版本固定为`v1`，默认允许服务器时间与请求时间相差300秒。时间戳使用Unix秒，避免HTTP
签名受展示时区影响。节点和服务器必须使用NTP同步；超过窗口时拒绝请求并告警。

## 2. 请求头

| Header | 值 |
|---|---|
| `X-ATS-Key-Id` | 3至64字符的公开密钥标识，用于轮换选择密钥 |
| `X-ATS-Timestamp` | 签名时的Unix秒 |
| `X-ATS-Nonce` | 每次HTTP尝试新生成的128位小写十六进制随机数 |
| `X-ATS-Request-Id` | `req_`加32位小写十六进制；同一业务请求重试保持不变 |
| `X-ATS-Content-SHA256` | 精确请求Body字节的`sha256:<64位小写hex>` |
| `X-ATS-Signature` | `v1=<HMAC-SHA256小写hex>` |

GET和无Body请求对零字节Body计算SHA-256。Body按实际发送的UTF-8字节签名，签名后禁止重新
序列化、改变空白或追加换行。`request_target`使用线上实际发送的origin-form原始路径与查询串，
例如`/api/v1/trading/runtime/commands?after_command_seq=40`；查询参数的顺序和百分号编码属于
签名内容。

## 3. 请求签名串

各字段用单个LF连接，末尾不加LF：

```text
ATS-HMAC-SHA256
request
v1
<key_id>
<unix_timestamp>
<nonce>
<request_id>
<UPPERCASE_METHOD>
<exact_origin_form_request_target>
<sha256:body_digest>
```

对上述UTF-8字节执行`HMAC-SHA256(secret, canonical_request)`，输出小写十六进制并加`v1=`。

## 4. 服务端验证顺序

1. 在Web服务器/框架的原始Header列表中拒绝任何重复安全头，再构造大小写不敏感的映射；
2. 校验所有Header的格式、时间窗和请求目标格式；
3. 对收到的原始Body字节计算SHA-256并常量时间比较；
4. 按`key_id`读取当前或轮换宽限期内密钥，重算HMAC并常量时间比较；
5. HMAC成功后，在共享Nonce Store中原子插入`key_id + nonce`；已存在即拒绝重放；
6. 完成节点/账户授权、Schema验证和业务幂等处理。

Nonce必须在认证成功后占用，避免未认证请求污染存储。量化服务多进程/多实例部署使用MySQL唯一
约束或等价共享存储实现原子占用，保留至签名窗口结束；进程内Nonce Store只用于测试和单进程
开发。Nonce Store不可用时认证失败关闭，不能绕过防重放继续处理。

## 5. 响应签名

所有成功响应和已识别密钥的认证后错误响应均签名。计划、命令、事件回执、快照/对账回执和
命令最终结果回执必须验签成功后才能使用。认证前无法识别密钥的错误只能依赖TLS返回通用信息。

响应Header使用`X-ATS-Request-Nonce`回显对应请求Nonce，其余使用请求同名Header。响应签名串：

```text
ATS-HMAC-SHA256
response
v1
<key_id>
<unix_timestamp>
<originating_request_nonce>
<originating_request_id>
<http_status_code>
<sha256:response_body_digest>
```

Windows验证响应Body、HTTP状态、请求ID和请求Nonce，因此旧响应不能移植到新的轮询请求。响应
验签失败时不得接收计划/命令，也不得确认或删除Outbox记录。

## 6. 重试和业务幂等

传输层不执行隐藏重试。每次Scheduler补传生成新的`request_id`、时间戳、Nonce和签名；载荷内
业务ID、内容hash和业务字段保持不变。
服务端对业务请求ID及对应内容实施幂等；同请求ID同内容返回既有结果，同请求ID不同内容永久
拒绝。捕获的完整HTTP请求再次发送会因Nonce已存在而失败。

## 7. 密钥管理与轮换

- 每个节点使用独立随机密钥，至少32字节；禁止将测试向量密钥用于任何环境。
- Windows密钥通过SecretProvider从Credential Manager读取；量化服务密钥保存在受控Secret
  Store，代码、配置文件、日志和数据库业务表不保存明文。
- `key_id`不是秘密。轮换时服务端在短宽限期同时接受旧/新key id；Windows切换新key验证后，
  再停用旧key。
- 禁用节点时立即停用其全部key id，并保留审计记录。
- 日志可记录key id、request id、结果和错误码，不记录secret、完整签名或原始认证Header集合。

## 8. 固定错误码

| 错误码 | 含义 | 处理 |
|---|---|---|
| `SIGNATURE_HEADER_MISSING/INVALID` | 认证Header缺失或格式非法 | 不重试原请求，检查客户端版本 |
| `SIGNATURE_TIMESTAMP_OUT_OF_WINDOW` | 时钟偏差或过期请求 | 同步时钟后新Nonce重签 |
| `CONTENT_DIGEST_MISMATCH` | Body字节与摘要不同 | 丢弃，禁止业务处理 |
| `SIGNING_KEY_UNKNOWN` | key不存在、已禁用或不在宽限期 | 检查授权/轮换状态 |
| `SIGNATURE_MISMATCH` | HMAC验证失败 | 丢弃并计入安全告警 |
| `SIGNATURE_REPLAYED` | 同key id与Nonce已使用 | 丢弃；不得把重放当业务重试 |
| `NONCE_STORE_UNAVAILABLE` | 无法建立防重放状态 | 失败关闭，恢复共享存储后重试 |
| `RESPONSE_REQUEST_MISMATCH` | 响应不属于当前请求 | 丢弃，保留Outbox或继续轮询 |

## 9. 双项目测试向量

`contracts/v1/examples/security/hmac-sha256-v1.vector.json`包含测试专用密钥、中文UTF-8 Body、
请求/响应规范串、Body摘要和最终签名。Windows项目和量化服务项目必须分别用各自实现复算全部
值；任何字段不一致都阻断协议发布。
