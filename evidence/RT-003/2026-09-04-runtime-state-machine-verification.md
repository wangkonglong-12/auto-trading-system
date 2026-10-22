# RT-003 Runtime状态机与模式优先级验收证据（2026-09-04）

## 交付范围

- 11个协议一致的`RuntimeState`和完整合法迁移矩阵。
- 部署Profile到`OBSERVE_ONLY/EXIT_ONLY/FULL`硬能力上限的组合根映射。
- 本地、Hard Safety、Watchdog、券商事实、中心、Runtime和策略限制优先级。
- 启动/重连固定返回`READY_PAUSED`，AUTO健康门和本地/中心双重Permit。
- 高优先级限制锁存、本地受控解除以及HALT两阶段恢复。
- RT-002普通/关键队列溢出到`PAUSE_BUY/HALT`的直接联动。
- `TransitionDecision`稳定结果、阻断码、单调时间和只对真实状态变化递增的序号。

## 固定安全语义

- 新进程只能`BOOTING → CONNECTING → RECONCILING → READY_PAUSED`，不能恢复上次AUTO。
- 重连完成同样停在READY_PAUSED；完整健康事实和新对账不满足时不能离开RECONCILING。
- Profile是硬能力上限：READ_ONLY不进入AUTO/SELL_ONLY，LIVE_EXIT_ONLY只允许SELL_ONLY。
- 中心与策略不能直接进入AUTO；AUTO要求QMT、账户、行情、Journal、对账、时钟、磁盘和
  队列事实健康，同时具有本地和中心授权。
- 任意权限可以进一步收紧；解除PAUSE_BUY/SELL_ONLY/MANUAL/HALT只接受本地Operator、
  新对账、健康检查和本地许可。
- 同状态的更高优先级急停会升级限制所有者，但不伪造一次状态变化。
- 状态机绑定决策循环所有者断言，回调线程和派生任务不能直接改变Runtime状态。

## 验证结果

| 验证项 | 结果 |
|---|---|
| 启动与重连 | 合法序列、跳步拒绝、健康对账门、READY_PAUSED安全默认值通过 |
| AUTO门 | 10类独立健康阻断、双重许可、远程恢复拒绝和Profile上限通过 |
| 优先级 | 高优先级限制锁存、低优先级不能解除、本地恢复两阶段流程通过 |
| 队列联动 | 普通溢出PAUSE_BUY、关键/启动期溢出HALT、已安全状态不放宽 |
| 生命周期 | 权限受控的STOPPING/STOPPED以及终态不可恢复通过 |
| 唯一所有者 | 与真实DecisionEventLoop组合验证，循环外修改被拒绝 |
| 决策结果 | APPLIED/NOOP/REJECTED字段不变量和迁移序号通过 |
| Ruff | 通过 |
| Ruff格式一致性 | 77个文件通过 |
| mypy strict | 77个源文件无问题 |
| pytest | 712项全部通过 |
| 分支覆盖率 | 99.05%，门槛90% |
| RT-003新增测试 | 55项，覆盖迁移、权限、健康、Profile、溢出和组合边界 |
| sdist/wheel构建 | 通过，版本`0.1.0.dev10` |
| 锁定依赖环境安装及版本查询 | 通过，输出`auto-trading-runtime 0.1.0.dev10` |
| 安装后状态机冒烟测试 | 从wheel绑定真实决策循环并完成BOOTING到CONNECTING迁移 |

## 固定哈希

| 文件 | SHA-256 |
|---|---|
| `src/auto_trading_system/runtime/state_machine.py` | `70b6a06690ca263e5bcd2b35cf8f045d63691c4e4c4ba903679d218fea0da1d4` |
| `src/auto_trading_system/apps/trade_runtime/bootstrap.py` | `8a5a11c945a1058ca165ae14f30fc742c3eee61f3fc91bad94edc7e38599239d` |
| `docs/runtime-state-machine.md` | `fbb150021c87b9a1746d2c9bfe6b8f4a3bf5277131aa48a2495312c6545279b9` |
| `dist/auto_trading_system-0.1.0.dev10-py3-none-any.whl` | `d3bda335e24eb899806ee6285bd5ad87ecdf53c73b213fd605fedb6670632796` |

`dist`不纳入Git；正式发布时重新构建并在包外发布清单登记最终hash。

## 结论与下一步

RT-003完成。Runtime状态、部署能力上限、安全限制优先级和恢复权限已经在唯一决策所有者内
固化，远程命令、重启和低优先级逻辑不能静默扩大交易许可。下一项直接实施RT-004内部
Scheduler和受控启动/停止。
