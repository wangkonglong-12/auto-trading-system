# QMT-003 Tick采证执行器准备记录

- 准备日期：2026-09-04
- 工作项状态：`BLOCKED`，依赖QMT-001目标机通过及QMT-003五场景矩阵
- 程序版本：`0.1.0.dev36`
- 报告协议：`qmt-tick-probe/v1`

## 实现范围

| 组件 | 责任 |
|---|---|
| `monitoring/qmt_tick.py` | 场景规则、有界并发聚合、字段/时间/量比/档位证据、隐私报告和不可变留证 |
| `gateways/market/qmt.py` | 动态加载XtData，只开放单股Tick订阅、最新全推快照、等待和反订阅 |
| `apps/qmt_tick_probe` | READ_ONLY配置、证券参数、账户Global Mutex、稳定退出码和锁后发布 |
| `scripts/verify-qmt-tick-probe.ps1` | 在无QMT开发机用SDK替身复验字段、回调、异常、清理和隐私边界 |

## 已实现安全性质

1. 单次仅接受1至15个唯一A股规范代码；账户锁冲突时不创建XtData客户端。
2. 每个`subscribe_quote`固定`period='tick'`、`count=0`且订阅号必须大于0；重复订阅号失败关闭。
3. 最新快照必须覆盖全部请求标的；额外标的视为结构错误，缺失标的形成现场阻塞。
4. ACTIVE、LIMIT_UP和LIMIT_DOWN要求每标的有效回调；HALTED和CLOSED允许零回调并保留事实计数。
5. 字段验证覆盖缺失、None、bool冒充数值、NaN/Inf、负数、档位元素、50档上限和价量深度配对。
6. 报告记录时间戳量级、`pvolume/volume`整数倍率候选、档位深度、状态码和可选字段存在性。
7. 回调聚合上限为100000条，聚合器使用线程锁；原始Tick、证券代码和全部价量值均不落盘。
8. 部分订阅或探测失败仍按逆序反订阅；清理失败覆盖为ERROR并保留主失败码。
9. 账户锁释放成功后才以排他原子方式发布新JSON；已有报告保持不可覆盖。

## 当前边界

开发机SDK替身只能验证调用和裁决逻辑。时间单位、成交量单位、真实档位数、停牌/涨跌停状态码、
特殊行情回调及版本可选字段必须由目标机五场景报告和人工双人复核冻结；QMT-003保持`BLOCKED`。

## 自动验收结果

- QMT-003专项验证：`149 passed`
- Python全量验证：`1458 passed`，总覆盖率`90.16%`
- 前端验证：`24 passed`，lint、格式、类型检查和生产构建通过
- Python静态检查：repository policy、compileall、Ruff、Mypy全部通过
- 发行构建：wheel和sdist构建通过
- 全新环境安装：按`requirements/runtime.lock`哈希锁安装依赖，再以`--no-deps`安装wheel成功
- 安装后入口：`auto-trading-qmt-tick-probe --version`输出`0.1.0.dev36`
- 安装后导入：命令组合根、XtData适配器和Tick探针核心均导入成功

## 发行物SHA-256

| 发行物 | SHA-256 |
|---|---|
| `auto_trading_system-0.1.0.dev36-py3-none-any.whl` | `f4758cacbf218e614ff8ca00e6c7f4de990ab6ecf03a654e6c23904e4cea6f4b` |
| `auto_trading_system-0.1.0.dev36.tar.gz` | `893d873de5f00a685f071a030716c72c8493b6aa2421f475434756d4073cd901` |
| `requirements/runtime.lock` | `b284c0c57f0b0702b9e6119bf1b279c836e636e3e005eb1ed0fd8430835da341` |
| `scripts/verify-qmt-tick-probe.ps1` | `292f3e9a0a394bf2bd1ada6b67d60b0a006dd4681009b431af4cc35d93be55ba` |

## 质量门禁精度

覆盖率报告固定`precision=2`并保持`fail_under=90`。因此89.99%会形成非零退出，统一质量脚本再按
原生命令退出码立即终止，避免整数四舍五入形成假通过。

源码包显式排除`evidence`、本地临时环境、既有`dist`、前端依赖和覆盖率目录；最终源码包共364个
条目，禁入条目计数为0，发行物哈希不再与其内部证据形成自引用。
