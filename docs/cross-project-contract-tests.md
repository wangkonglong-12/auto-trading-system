# 双项目契约一致性验证

## 1. 目标

TST-002保证Windows交易节点与量化服务对协议1.0具有相同的机器可读定义和可观察行为。验证范围
包括TradePlan、计划ACK、领域事件、资产/持仓快照、对账、心跳、命令、统一错误信封和
HMAC-SHA256固定向量。

## 2. 独立可验证的契约清单

两个项目的 `contracts/v1` 都保存完整协议包和相同的 `manifest.sha256.json`。清单：

- 按规范相对路径列出协议包中的全部48个Schema、样例和测试向量；
- 为每个文件保存SHA-256；
- 不把清单自身纳入清单，避免递归哈希；
- 拒绝缺文件、多文件、重复路径、不安全路径或摘要不一致；
- 允许两个仓库各自在CI中独立证明其协议包等于同一冻结基线。

协议变更必须在同一次受评审变更中更新两端文件和两端清单。只修改一个项目、只比较交集或
跳过新增文件都会失败。

## 3. 双实现行为验证

Windows测试使用JSON Schema、独立规范JSON哈希、客户端签名/验签、版本兼容层和Mock Central
API。量化服务测试使用严格Pydantic模型、服务端签名/验签、真实API服务、Repository幂等状态机
和同一批样例。

| 能力 | Windows侧证据 | 量化服务侧证据 |
|---|---|---|
| Schema与示例 | 全部Schema契约测试 | 清单、严格Pydantic模型和API请求验证 |
| HMAC | 请求/响应固定向量及篡改矩阵 | 同一固定向量、请求验签和响应签名 |
| 版本 | `ProtocolVersion`兼容矩阵 | Literal 1.0模型与未知版本API拒绝 |
| 错误信封 | Schema、三态错误语义 | API统一错误载荷和签名错误响应 |
| 幂等 | Mock Central、Outbox逐条回执 | Plan ACK、事件、快照、对账和命令Repository |

## 4. 统一执行

两个项目位于同一父目录时：

```powershell
.\scripts\verify-cross-project-contracts.ps1 `
  -WindowsPython .\.venv-dev\Scripts\python.exe
```

也可以显式指定量化项目和解释器：

```powershell
.\scripts\verify-cross-project-contracts.ps1 `
  -QuantProjectRoot E:\workspace\ai-stock-assistant `
  -WindowsPython .\.venv-dev\Scripts\python.exe `
  -QuantPython .\.venv\Scripts\python.exe
```

脚本先逐字节比较两端清单，再分别调用两个项目自己的测试实现。任何一端失败都返回非零退出码。
脚本只读取契约并运行测试，不复制文件、不修改数据库，也不连接QMT或外部网络。

## 5. 边界

契约一致只证明双方理解同一协议，不证明QMT客户端、券商权限或真实网络已经可用。实际QMT字段、
回调时序和交易权限继续由QMT-001至QMT-010验证；HTTPS证书、DNS和防火墙继续由GOV-007验证。
