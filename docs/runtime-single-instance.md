# Runtime账户级单实例保护

## 1. 设计结论

每个真实券商账户在一台Windows机器上只允许一个`trade-runtime`持有运行所有权。实现使用
Win32命名Mutex，不依赖进程名、PID文件、SQLite写锁或QMT连接报错来间接判断双开。

Mutex默认位于`Global`命名空间，名称格式为：

```text
Global\AutoTrading.Account.<SHA-256账户摘要>
```

摘要输入是通过SecretProvider解析的真实券商账户ID，统一转换为大写后计算。名称、异常和日志只
暴露12位诊断指纹，不包含真实账户、逻辑别名、节点ID或凭据引用。

## 2. 启动顺序

Runtime组合根固定执行：

1. 读取严格配置并解析Credential Manager中的真实券商账户；
2. 构造账户级Instance Guard；
3. 原子创建并立即持有Global命名Mutex；
4. 成功后才允许打开LocalJournal、启动事件循环、连接QMT或启动本地API；
5. 碰到`ERROR_ALREADY_EXISTS`立即返回`ACCOUNT_INSTANCE_ALREADY_RUNNING`，不连接QMT；
6. Runtime所有Worker和QMT会话完全停止后释放Mutex并关闭句柄。

`build_instance_guard()`已经把解析后的真实账户绑定到锁。RT-004组合完整Runtime生命周期时，
必须让该Guard覆盖整个进程运行期，不能只覆盖QMT连接阶段。

## 3. 故障行为

- CreateMutex、ReleaseMutex或CloseHandle失败均以稳定错误码失败关闭；
- 同一Guard重复获取属于程序错误，明确拒绝；重复释放安全返回，便于清理路径幂等；
- 正常退出显式Release并Close；进程异常终止时Windows内核回收句柄；
- Watchdog发现Mutex仍被持有时不得启动第二Runtime或持续重启冲击；
- 异常进程退出后，新Runtime即使成功取得Mutex，也从`READY_PAUSED`启动并完成全量对账，绝不
  因“重新取得锁”自动恢复AUTO。

Mutex只解决同机同账户并发进程问题，不解决跨机器重复运行。生产治理还必须保证账户只授权给
一个Windows节点；中心节点/账户授权和心跳用于发现跨机器冲突。

## 4. 为什么不使用SQLite锁替代

SQLite/WAL锁属于持久化实现细节，数据库尚未打开、路径配置错误、备份操作或异常清理期间都不
能可靠代表Runtime所有权。OS Mutex在任何QMT、网络和存储初始化前生效，避免两个进程各自创建
Trader、订阅相同账户或竞争本地状态。LocalJournal仍保留单Writer规则，两层保护职责不同。

## 5. 验证要求

- 同一进程两个Guard争用同一账户时，第二个失败；
- 两个真实Windows进程争用同一Global Mutex时，第二个失败；
- 账户大小写差异映射到同一Mutex，不同账户互不影响；
- 持有进程正常退出后，新进程可获取；
- Mutex名称、repr和异常不出现真实账户ID；
- Win32创建、释放、关闭和API缺失错误均有失败关闭测试。
