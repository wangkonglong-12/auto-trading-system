# QMT-004 实盘PoC离线审批预检准备记录

- 日期：2026-09-04
- 工作项状态：`BLOCKED`，离线预检已完成，实盘时序等待目标QMT及业务授权
- 程序版本：`0.1.0.dev37`
- 审批协议：`qmt-order-poc-approval/v1`
- 报告协议：`qmt-order-preflight-report/v1`

## 已实现范围

| 制品 | 作用 |
|---|---|
| `monitoring/qmt_order_preflight.py` | 严格加载审批、校验目标/金额/时间并生成执行绑定与隐私报告 |
| `apps/qmt_order_preflight` | `ASSISTED`配置组合根，仅解析券商账户凭据并执行离线预检 |
| `config/v1/qmt-order-poc-approval.example.json` | QMT-004单笔实盘PoC封闭审批模板 |
| `scripts/verify-qmt-order-preflight.ps1` | 在无QMT开发机复验审批、配置、安全和发布入口 |
| `evidence/GOV-001/target-machine-template.md` | 现场权限、参数、双人复核、预检及实盘执行证据登记表 |

## 冻结的安全规则

1. 审批文件使用精确字段集、最大64 KiB、UTF-8 JSON、重复键拒绝和外部完整SHA-256。
2. 审批只覆盖一个目标机、一个券商账户、一个证券、一个方向和一个正整数数量。
3. 目标机、券商账户、QMT-001/002/003证据hash、价格、数量及一次性nonce共同进入执行绑定hash。
4. 批准窗口固定1至30分钟，撤单期限固定5至120秒，执行人与独立复核人必须不同。
5. 人工全程值守、QMT急停可用和只执行单笔订单三项安全确认均为true。
6. 订单名义金额同时满足审批文件上限与Runtime `max_single_order_notional`。
7. 预检报告使用排他新文件，完整账户、证券代码、一次性nonce、委托备注、凭据和异常文本不落盘。
8. 报告固定记录`OFFLINE_APPROVAL_VALIDATION`与`live_order_submitted=false`；生产Broker报单路径仍唯一收敛在订单意图持久化门。

## 自动验证

- QMT-004及相关安全专项：`142 passed`
- Python全量：`1502 passed`
- Python分支覆盖率：`90.22%`，门槛`90.00%`，按两位小数裁决
- 前端：`24 passed`
- Ruff格式/Lint、Mypy strict、架构策略、敏感值边界、前端类型检查与生产构建：通过
- Wheel/sdist：构建通过
- 全新虚拟环境哈希锁安装与入口检查：通过，环境`.codex_tmp/qmt004-preflight-clean-install-01`
- 安装后入口：`auto-trading-qmt-order-preflight --version`输出`0.1.0.dev37`
- 安装后边界：核心模块、CLI导入成功，`xtquant`未被导入
- sdist内容：`371`项，临时目录、覆盖率、dist、evidence和node_modules命中数`0`

## 发布制品

| 制品 | SHA-256 |
|---|---|
| `auto_trading_system-0.1.0.dev37-py3-none-any.whl` | `afaf48cd51a8b4f99e645e87f8df8a5956ad1e863257c0effe1f1488f3c6ff0e` |
| `auto_trading_system-0.1.0.dev37.tar.gz` | `f985aec4eb93beb93c5db8fb73f3d3e2f683c4bc8dcd5ec1381fb5d219a7f87a` |
| `requirements/runtime.lock` | `b284c0c57f0b0702b9e6119bf1b279c836e636e3e005eb1ed0fd8430835da341` |
| `scripts/verify-qmt-order-preflight.ps1` | `5617b101ffcb2b8c6b9c60691a1efc01824882430ccb3026f466123709d9d875` |

## 剩余现场门

QMT-001、QMT-002和QMT-003必须先在目标机形成经双人复核的现场结论；程序化交易权限、账户、
标的、方向、数量、限价、金额、值守时段和急停责任人必须由业务负责人批准。条件齐备后，先生成
退出码0的离线预检报告并登记执行绑定hash，再开发并执行单笔最小委托/撤单现场时序采证。
