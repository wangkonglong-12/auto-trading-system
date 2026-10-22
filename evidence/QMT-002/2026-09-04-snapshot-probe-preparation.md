# QMT-002账户快照采证执行器准备记录

- 准备日期：2026-09-04
- 工作项状态：`BLOCKED`，依赖QMT-001目标机通过及QMT-002场景矩阵
- 程序版本：`0.1.0.dev35`
- 报告协议：`qmt-snapshot-probe/v1`

## 实现范围

| 组件 | 责任 |
|---|---|
| `monitoring/qmt_snapshot.py` | 账户状态门、有界字段形状、四数据集裁决、隐私报告和不可变留证 |
| `gateways/broker/cicc_qmt_snapshot.py` | 复用QMT-001会话，只增加资产、持仓、当日委托和当日成交查询 |
| `apps/qmt_snapshot_probe` | READ_ONLY配置、账户Global Mutex、采证执行和稳定退出码 |
| `scripts/verify-qmt-snapshot-probe.ps1` | 在无QMT开发机用SDK替身复验允许、阻塞、异常和隐私边界 |

## 已验证安全性质

1. 构造QMT客户端前获取与正式Runtime相同的账户级Global Mutex；锁冲突不访问QMT，锁释放失败
   不发布报告。
2. `connect=0`、`subscribe=0`且主动查询目标账户`status=0`后才允许查询；每个数据集后再次确认
   账户状态，断线或非OK立即停止后续查询。
3. 仅允许`query_stock_asset`、`query_stock_positions`、`query_stock_orders(account, False)`和
   `query_stock_trades`，不包含报单、撤单、数据库或HTTP能力。
4. Asset的`None`明确为查询失败；Positions/Orders/Trades的`None`保留“失败或空”歧义；仅空
   `list`记录为`EMPTY_LIST`。
5. 必需字段缺失、类型漂移、bool冒充数值、NaN/Inf、非目标账户、错误容器、超过10000条和
   重复持仓/委托/成交身份均失败关闭。可选`fetch_balance/current_balance`单独记录存在性。
6. 报告仅保留字段名、类型和聚合计数；完整账户、路径、证券、订单、成交、文本、金额、数量和
   价格原值全部省略，并在发布前扫描账户残留。
7. 查询或裁决失败仍执行反订阅和停止；清理失败形成ERROR报告，账户锁释放后才原子发布。

## 当前边界

本机SDK替身只能证明程序的调用顺序、错误裁决和隐私边界，不能冻结券商版QMT的真实字段、类型、
单位、精度或`None`行为。QMT-002保持`BLOCKED`，目标机须在QMT-001通过后完成空账户、有持仓、
当日有委托/成交、断线、校正中和收盘后六类场景并双人复核。

## 自动验收结果

- QMT-002专项验证：`121 passed`
- Python全量验证：`1397 passed`，总覆盖率`90.07%`
- 前端验证：`24 passed`，lint、格式、类型检查和生产构建通过
- Python静态检查：repository policy、compileall、Ruff、Mypy全部通过
- 发行构建：wheel和sdist构建通过
- 全新环境安装：按`requirements/runtime.lock`哈希锁安装依赖，再以`--no-deps`安装wheel成功
- 安装后入口：`auto-trading-qmt-snapshot-probe --version`输出`0.1.0.dev35`
- 安装后导入：命令组合根、QMT适配器和探针核心均导入成功

## 发行物SHA-256

| 发行物 | SHA-256 |
|---|---|
| `auto_trading_system-0.1.0.dev35-py3-none-any.whl` | `4c2fe697afaee3044252dfeb41f508f289fefd654f4c8f12d638ee0771f1980e` |
| `auto_trading_system-0.1.0.dev35.tar.gz` | `e07d3a4071826a62585b3739dfbf70cdf68fe130c34a65a37375a74f01dfcad1` |
| `requirements/runtime.lock` | `b284c0c57f0b0702b9e6119bf1b279c836e636e3e005eb1ed0fd8430835da341` |
| `scripts/verify-qmt-snapshot-probe.ps1` | `20c3b9daa1222fd1525c2d660fb6321e688f4b9551c51d5ccbd55da55fea08be` |

## 质量门禁修正

统一质量脚本现对pnpm和每条Python原生命令显式读取`LASTEXITCODE`并在非零时终止，避免覆盖率、
静态检查、测试或构建失败后继续执行并形成假通过。该约束已有仓库策略测试保护。
