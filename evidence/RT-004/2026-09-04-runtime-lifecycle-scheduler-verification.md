# RT-004 内部Scheduler与受控生命周期验收证据（2026-09-04）

## 交付范围

- 唯一进程内`RuntimeScheduler`、APScheduler生产Backend和可替换测试Backend。
- 计划、命令、事件回传、心跳、资产/持仓/活动委托对账及三项盘后维护共11类任务。
- 活跃/空闲间隔原地切换、上海时区、单Job实例、合并补跑和misfire窗口。
- Scheduler回调只向有界入口发布类型化事件，不持有QMT、SQLite或HTTP能力。
- `ManagedRuntime`账户所有权、组件Phase、部分启动回滚和逆序停止编排。
- Decision Loop与Scheduler生命周期适配器及单实例Phase约束。

## 固定安全与生命周期语义

- Windows Task Scheduler只拉起一个长期Runtime和Watchdog，业务定时任务不创建独立进程。
- Persistence、Decision Loop、Broker Session、Scheduler和Control API各只允许一个组件；
  Worker Phase允许多个受管Worker。
- Global账户Mutex最先获取、最后释放；获取失败时任何组件都不会启动。
- 启动顺序为Journal、Decision Loop、唯一QMT会话、Workers、Scheduler、本地API。
- 停止顺序完全相反，保证先停止新任务和回调，再排空决策队列，最后关闭Journal。
- 部分启动组件也进入回滚集合；一个stop失败不会阻断其他组件和账户锁释放尝试。
- 账户锁释放结果不明确时继续标记为持有并进入FAILED，允许再次stop重试。
- Scheduler启动成功前的回调和停止后的竞态回调均抑制；队列满沿用RT-002/003安全降级。

## 验证结果

| 验证项 | 结果 |
|---|---|
| 调度任务 | 11类Job、活跃/空闲节奏、15:10/15:20/15:30/15:40盘后序列通过 |
| APScheduler配置 | Asia/Shanghai、coalesce、max_instances=1、misfire和真实启停通过 |
| 回调边界 | 只发布ScheduledRuntimeEvent，普通/关键优先级、溢出/关闭/抑制计数通过 |
| 并发触发 | 100个并发回调序号唯一，队列保持有界 |
| 单QMT会话 | 10次调度触发期间Broker Session只启动和停止各一次 |
| 生命周期 | 固定Phase顺序、逆序停止、幂等、取消和run-until通过 |
| 失败恢复 | 账户锁失败、部分启动失败、多个stop失败和锁释放重试通过 |
| 架构守卫 | Scheduler源码不依赖QMT Gateway、xtquant、SQLite或HTTP客户端 |
| Ruff | 通过 |
| Ruff格式一致性 | 81个文件通过 |
| mypy strict | 81个源文件无问题 |
| pytest | 763项全部通过 |
| 分支覆盖率 | 99.02%，门槛90% |
| RT-004新增测试 | 51项，覆盖Scheduler、生命周期、并发和架构边界 |
| sdist/wheel构建 | 通过，版本`0.1.0.dev11` |
| 锁定依赖环境安装及版本查询 | 通过，输出`auto-trading-runtime 0.1.0.dev11` |
| 安装后组合冒烟测试 | wheel内真实APScheduler、决策循环和ManagedRuntime完整启停通过 |

## 固定哈希

| 文件 | SHA-256 |
|---|---|
| `src/auto_trading_system/runtime/scheduler.py` | `c713e20dbe638d7a7af1ce8ddb00c659904a1715bcfe743340e1b331ea72aab7` |
| `src/auto_trading_system/runtime/lifecycle.py` | `b18f6e1f636cbea34d1fd25f6745425a9873c8fa14803d4894e092f8a17f1ac5` |
| `src/auto_trading_system/apps/trade_runtime/bootstrap.py` | `ffa032f11fceb517d2f51a021a637e8558deb6aa09cef8dde9e7604c9f168d5a` |
| `docs/runtime-lifecycle-scheduler.md` | `a648a62930307c22a3f87f0b216605a5fb974be80ea1db61204ce7535f723817` |
| `dist/auto_trading_system-0.1.0.dev11-py3-none-any.whl` | `0ac8afc541ecd4225f8ca48aa7d5ac9b7ab6f359b20db28f3ee087b39c12f7a2` |

`dist`不纳入Git；正式发布时重新构建并在包外发布清单登记最终hash。

## 结论与下一步

RT-004完成。定时业务已收敛在唯一Runtime内，所有Job只发布控制事件，账户所有权、唯一QMT
会话、Worker、决策循环和Journal具备固定启停与失败回滚顺序。下一项直接实施JRN-001
LocalJournal端口与SQLite适配器接口。
