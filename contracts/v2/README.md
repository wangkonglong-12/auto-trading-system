# V2 首批契约

本目录固定V3.1首批已实现的数据结构与公开测试签名向量。

- `schemas`：18份Schema，包含计划信封、日终清单、最终回执、本地控制、八类日终记录、账单导入元信息，以及本地关键提交/持仓封存/开盘引用。
- `examples/signed-plan.json`：模拟计划，绑定`simulation-account`。
- `examples/signature-vector.json`：公开测试种子、公钥、HMAC预像和签名。测试密钥仅用于一致性检查。

`scripts/export-v2-contracts.py --check`验证生成结果；修改模型后显式重新生成，并在代码审查中核对差异。
量化项目的V2日终结构、规范化和HMAC实现放在`backend/app/protocol/auto_trading/v2`，跨项目测试比较语法树，运行程序不会自动复制文件。

V2已提供9接口OpenAPI、八类逐字段日终结构、HTTP适配及持久化nonce；正式账单/费用/结算契约继续由R31-002/R31-017完成，生产路由开关保持原验收流程。

dev42的`submission-record`新增可选SELL批次绑定；`portfolio-checkpoint`与`opening-reference`为Windows本地Journal契约。它们与`daily-submission_records`的HTTP日终摘要契约分别演进，本轮保持跨项目共享HTTP结构原样。新增本地Schema不表示量化接口已经接收批次级退出证据；该项继续列入日终导出接线。
