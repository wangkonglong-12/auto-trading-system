# CON-004 HMAC安全契约验收证据（2026-09-04）

## 交付范围

- 与HTTP框架无关的HMAC-SHA256 v1请求签名和验签实现。
- 精确原始Body SHA-256、HTTP方法、原始path/query、key id、时间戳、Nonce和request id绑定。
- 响应Body、HTTP状态、请求ID和请求Nonce绑定及验签。
- `SigningKeyResolver`密钥轮换端口及脱敏的Mapping实现。
- `NonceStore`原子防重放端口、线程安全内存实现、过期清理和容量保护。
- 300秒默认时钟窗口、常量时间比较以及Nonce Store失败关闭。
- 含中文UTF-8 Body的跨项目固定请求/响应测试向量和Schema。
- 完整的部署、重试、共享Nonce Store和密钥轮换规范。

## 固定安全语义

- TLS必须启用；HMAC提供消息身份和完整性，不替代TLS机密性。
- 签名使用精确发送的Body字节和origin-form原始路径/查询串，签名后不得重新序列化。
- 网络重试保持业务`request_id`和Body不变，每次HTTP尝试生成新的时间戳、Nonce和签名。
- 服务端先验HMAC，再通过共享存储原子占用`key_id + nonce`；重放请求拒绝进入业务处理。
- Nonce Store不可用时返回`NONCE_STORE_UNAVAILABLE`并失败关闭。
- 响应绑定发起请求的request id和Nonce；旧响应不能用于新的计划/命令轮询。
- 响应验签失败时禁止接受计划/命令，禁止删除Outbox记录。
- 未知key使用等长虚拟密钥完成HMAC计算后再报错，减少密钥存在性时序泄漏。
- 每节点独立至少32字节密钥；测试向量密钥明确禁止用于任何环境。

## 验证结果

| 验证项 | 结果 |
|---|---|
| 固定向量Schema与独立HMAC复算 | 请求和响应均一致 |
| 篡改测试 | Body、方法、路径、HTTP状态、request id、Nonce和密钥篡改全部拒绝 |
| 防重放 | 首次Nonce原子占用成功，重复占用拒绝，过期回收与容量上限通过 |
| 时间窗口 | 正负边界接受，窗口外、未来、非法策略拒绝 |
| Header安全 | 缺失、重复、错误大小写映射、非法格式和非文本值均覆盖 |
| Ruff | 通过 |
| Ruff格式一致性 | 67个文件通过 |
| mypy strict | 67个源文件无问题 |
| pytest | 579项全部通过 |
| 分支覆盖率 | 99.07%，门槛90% |
| CON-004新增测试 | 53项；固定向量、请求/响应、篡改、时间、Nonce和密钥边界均覆盖 |
| sdist/wheel构建 | 通过，版本`0.1.0.dev6` |
| 锁定依赖环境安装及版本查询 | 通过，输出`auto-trading-runtime 0.1.0.dev6` |
| 安装后HMAC冒烟测试 | 从wheel完成签名、验签和Nonce占用 |

## 固定哈希

| 文件 | SHA-256 |
|---|---|
| `contracts/v1/signature-test-vector.schema.json` | `f4b355a8fe77dcd9d3327b24990f93799ec19385614c790b60b13b39a27626c2` |
| `contracts/v1/examples/security/hmac-sha256-v1.vector.json` | `697ed7a7db7a521aa11d9fa4c1652fda40728b50419ecfa81eb5ec0b3086cf07` |
| `src/auto_trading_system/protocol/signing.py` | `1f7e69fc80e7f5ea0abc0092150690962b6224a434bf3ee827fa0f1e4d9dc313` |
| `docs/hmac-authentication.md` | `446d253668461b9403101f23c65bff4af3c77fbfaa21aabf73a08eab30438167` |
| `dist/auto_trading_system-0.1.0.dev6-py3-none-any.whl` | `09a5c79e5dd372bef6a58b25bc01815a98f141d266bba8dbc6be0b6b9de42e67` |

`dist`不纳入Git；正式发布时重新构建并在包外发布清单登记最终hash。

## 结论与下一步

CON-004完成。两个项目可以直接用同一固定向量实现并验证请求、响应和防重放逻辑。下一项
实施MOCK-002，提供签名的量化服务Mock API，并可重复模拟计划、命令、断网、签名错误和
事件批量回执。
