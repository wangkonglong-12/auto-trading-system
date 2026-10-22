# RT-001 账户级单实例保护验收证据（2026-09-04）

## 交付范围

- `AccountInstanceGuard`账户所有权抽象和可替换`InstanceLockBackend`端口。
- Win32 `CreateMutexW/ReleaseMutex/CloseHandle`生产实现。
- `Global\AutoTrading.Account.<SHA-256>`跨进程账户级Mutex命名。
- 使用SecretProvider解析的真实券商账户构造Guard的Runtime组合函数。
- 稳定、脱敏的双开、创建、释放、关闭和平台/API错误码。
- 同进程、真实第二Windows进程、正常释放和不同账户隔离测试。

## 固定安全与生命周期语义

- 账户ID统一转大写并完整SHA-256，Mutex名称不包含真实账户、节点或别名。
- Runtime加载配置/凭据后立即获取Mutex；成功前禁止打开Journal、启动Worker或连接QMT。
- `ERROR_ALREADY_EXISTS`立即返回`ACCOUNT_INSTANCE_ALREADY_RUNNING`并失败关闭。
- Guard必须覆盖Runtime完整生命周期，所有Worker和QMT停止后才释放。
- 新进程取得已释放Mutex后仍从`READY_PAUSED`启动并完成对账，不自动恢复AUTO。
- Mutex解决同机并发；跨机器同账户运行还由部署授权和中心心跳治理。
- SQLite锁不能替代OS Mutex，LocalJournal的单Writer是另一层独立保护。

## 验证结果

| 验证项 | 结果 |
|---|---|
| 同进程冲突 | 同账户第二Guard明确拒绝，不同账户互不影响 |
| 真实跨进程冲突 | 子进程持有Global Mutex时，父进程同账户获取失败 |
| 释放恢复 | 持有进程正常退出后，新Guard可成功获取 |
| 脱敏 | Mutex名称、repr和异常均不包含真实账户ID |
| Win32失败关闭 | API缺失、Create、Release、Close和非法token路径均通过 |
| Ruff | 通过 |
| Ruff格式一致性 | 73个文件通过 |
| mypy strict | 73个源文件无问题 |
| pytest | 634项全部通过 |
| 分支覆盖率 | 98.98%，门槛90% |
| RT-001新增测试 | 20项；输入、生命周期、脱敏、后端故障及真实Windows进程均覆盖 |
| sdist/wheel构建 | 通过，版本`0.1.0.dev8` |
| 锁定依赖环境安装及版本查询 | 通过，输出`auto-trading-runtime 0.1.0.dev8` |
| 安装后Mutex冒烟测试 | 从wheel创建、持有和释放真实Global Mutex成功 |

## 固定哈希

| 文件 | SHA-256 |
|---|---|
| `src/auto_trading_system/runtime/single_instance.py` | `dac8955ce18ba645ef247c5cbdc8aee3648c4383155364757b12ec0925cf9829` |
| `src/auto_trading_system/apps/trade_runtime/bootstrap.py` | `14483bcf7a846439274e4ff63529745dd27e6c5d6c00997d676f6fb1ba8079a8` |
| `docs/runtime-single-instance.md` | `a6423067deb31e2aa948b1865b12e4fa861930adbd5c50652292676a07cb7a29` |
| `dist/auto_trading_system-0.1.0.dev8-py3-none-any.whl` | `f5061aad4a32f689075a7e9534a07d7d3cbc583c6b34b4b6b05e781c8cbbc601` |

`dist`不纳入Git；正式发布时重新构建并在包外发布清单登记最终hash。

## 结论与下一步

RT-001完成。同一券商账户在本机只能有一个Runtime取得所有权，且锁失败不会继续初始化交易
基础设施。下一项直接实施RT-002单一决策事件循环和有界队列。
