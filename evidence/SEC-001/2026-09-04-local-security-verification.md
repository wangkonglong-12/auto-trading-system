# SEC-001 本地安全边界验收证据

## 验收结论

SEC-001完成。生产配置默认接入Windows Credential Manager，交互式凭据命令只通过隐藏输入写入
Generic Credential；真实委托Profile继续强制使用Windows凭据引用。CredRead/CredWrite边界限制
Blob大小、严格解码、映射稳定错误并统一覆写临时明文缓冲。

生产组合根通过`RuntimeSecrets`注册节点HMAC和券商完整账号。结构化日志字段、格式参数、最终文本、
异常栈和故障包共用统一脱敏器；未知对象、二进制正文、敏感键和认证文本均以固定标记输出。
故障包只写净化后的文本/JSON及完整性Manifest。

SQLite组合根在创建Journal前建立固定存储布局并执行Windows DACL收紧，只保留当前服务用户与
LocalSystem。Git质量门扫描凭据文件、私钥内容、当前`ATS_SECRET_*`值以及DOCX/ZIP成员。

## 安全边界验证

| 边界 | 验证结果 |
|---|---|
| Windows Credential Manager | 真实缺失项返回`SECRET_NOT_FOUND`且错误不含target；Win32替身覆盖读写、释放、尺寸和错误映射 |
| 凭据安装/轮换 | wheel安装后的`auto-trading-credential`入口可用，只提供target、kind和encoding参数 |
| 明文临时缓冲 | 读解码成功、解码失败和写入完成路径均验证bytearray覆写为零 |
| 日志 | 注册值、敏感键、Bearer/Basic、Cookie、Token、Password、Signature、格式参数及异常栈全部净化 |
| 故障包 | Artifact名称、大小、数量、时区、重复、覆盖和路径穿越受限；Manifest哈希可核验 |
| Windows DACL | 真实临时目录关闭继承、移除宽泛组，并授予当前服务用户与LocalSystem继承权限 |
| 生产组合根 | `build_persistence_writer`在SQLite初始化前强制执行安全目录初始化和ACL |
| Git质量门 | 受限文件名、私钥、已知值、UTF-8/UTF-16LE及压缩文档成员扫描通过 |

## 自动化验证

2026-09-04在Windows、Python 3.12.14执行完整质量门：

- repository secret boundary、编译、Ruff、格式检查和mypy严格检查全部通过；
- 1146项测试全部通过，总覆盖率92.51%；
- 49项凭据、脱敏、故障包、ACL、存储和仓库边界专项测试通过；
- 真实Windows集成测试完成DACL收紧、文件日志、故障包导出和二次已知值扫描；
- Runtime Config Schema重新生成并与模型一致；
- sdist与dev20 wheel从隔离构建环境生成成功。

## 独立安装验证

全新虚拟环境从dev20 wheel解析并安装全部声明依赖，随后完成：

- 包版本为`0.1.0.dev20`；
- 安全存储根的受保护主体数为2；
- 故障包Artifact数为1，ZIP展开内容未出现测试HMAC或券商账号；
- 真实Windows Credential Manager随机缺失目标返回`SECRET_NOT_FOUND`；
- `auto-trading-credential --help`入口可执行；
- repository secret boundary再次通过；
- 验证目录已在路径边界确认后清理。

## 产物校验和

| 产物 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev20-py3-none-any.whl` | `3dd197fead6836d425f599b2e91e8fc6a7ea6fcee460420a02bd15930c4196cb` |
| `src/auto_trading_system/config/windows_credentials.py` | `94417db63d30eb27001f56dea52279ec4259fdcaa8ee20dd4990c13a7da156fb` |
| `src/auto_trading_system/monitoring/redaction.py` | `41759ec0c367629b57799fc90a4172ad98c3beec0ce8ab64948699b881b42b18` |
| `src/auto_trading_system/monitoring/diagnostics.py` | `e3f444d9acd256d6aaad383d56577ba581fbb10104086f897648833f3e07aefe` |
| `src/auto_trading_system/security/windows_acl.py` | `6a48ae6ba232e7c139c37e92cfcc0d804c45253685a10ec287630390f1c95c24` |
| `src/auto_trading_system/security/storage.py` | `a59ee443465afe620dd7c92a30210aeb1540da08a8a730ca141cbcbee8b02dbf` |
| `src/auto_trading_system/security/repository_policy.py` | `5355124811a2c66877895500174408e6e5f738ba7a42991f4e7e83bb48a9d2ed` |
| `src/auto_trading_system/apps/credential_admin/__main__.py` | `1ac992f4a67b232574697fc7f61bbed1c8edba996f40bd3ed2ee4c63099c377f` |
| `tests/integration/test_local_security_boundary.py` | `4439871e581616e2bf6c555eb2bcbb88f2ca1a45eb618d80155b813b1dbc6a0e` |
| `docs/local-security.md` | `9624e09bb9755bc848187f71d254bdb7c725d210616d0058b89969ee619f523c` |

## 后续工作

P3、RSK-003和MON-001仍等待目标QMT订单、行情与对账链路。按当前依赖图，`UI-001`只依赖已完成的
ATS-001，因此下一项进入Windows本地操作台前端骨架、路由、设计规范和Runtime静态资源发布。
