# JRN-004 Durable Outbox验收证据

## 验收结论

JRN-004完成。Windows交易节点已实现Scheduler单次驱动的Durable Outbox投递：业务事实在
LocalJournal持久化后以最多50条批次主动上传量化服务；只有通过HMAC响应认证、请求绑定和逐条
身份校验的回执才会ACK或进入永久死信。断网、超时、非200、响应认证失败、结果不完整、进程
取消和重启均保留原业务事实并按有界指数退避继续投递。

## 安全语义

- Persistence Writer是Outbox读取和回执事务的唯一SQLite调用路径；
- Journal不可写、返回非PENDING/重复/超批次记录时，在任何HTTP调用前失败关闭；
- 每次HTTP尝试使用新的request ID、Nonce、发送时间和HMAC，事件ID、hash和内容保持不变；
- 响应先验签再解释状态码和Body，无法取得可信完整回执时整批重新调度；
- `ACCEPTED/DUPLICATE`安全ACK，认证的临时拒绝重试，认证的永久拒绝进入死信；
- 重试次数达到表示上限时保留PENDING并失败关闭，不静默丢弃；
- 50,000/100,000默认高水位通过决策循环关键通道发布，恢复到NORMAL不自动授予AUTO；
- Worker禁止重叠执行，取消发生在网络阶段时不产生本地状态变更。

## 自动化验证

2026-09-04在Windows、Python 3.12.13执行：

- 编译、Ruff、格式检查和mypy严格检查全部通过；
- 956项测试全部通过，总覆盖率95.90%；
- 41项Outbox专项、传输、组合根及集成测试通过；
- 真实SQLite/WAL、唯一Persistence Writer、签名HTTPX ASGI和量化服务Mock组合通过；
- 验证混合ACK/重试/永久拒绝在一个Journal事务应用；
- 验证响应认证失败后整批保留，进程停止/重启后中心返回`DUPLICATE`并安全ACK；
- 验证积压跨越HIGH/CRITICAL/NORMAL时只发布级别变化；
- sdist与dev15 wheel从隔离构建环境生成成功。

## 独立安装验证

在全新虚拟环境中按hash锁定安装运行依赖，以`--no-deps`安装dev15 wheel并验证：

- CLI输出`auto-trading-runtime 0.1.0.dev15`；
- 可从安装包导入签名事件传输、Outbox Worker设置和压力级别；
- 50条批次边界、300秒退避上限和CRITICAL高水位计算正确；
- 冒烟结果：`JRN-004 installed-package smoke passed`；
- 临时验证环境已安全清理。

## 产物校验和

| 产物 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev15-py3-none-any.whl` | `66af590b283e1805e17c89481fc09c72c7a6ff164b31f7e49c11771184777a90` |
| `src/auto_trading_system/sync/outbox_worker.py` | `095f242e5e53f886c66e68d5d39bcba58d02b76ae78e631505bfd153b7377bc8` |
| `src/auto_trading_system/sync/event_transport.py` | `9dc622adc4ffa86713b4fcc378a64e0fcf94320e21c2e2c64cb7db64c9a026b7` |
| `tests/integration/test_durable_outbox_delivery.py` | `13fe6869c145bfa3b8ec0f3bf870ade30eaabbca41f5d5a0b5191956e6152731` |
| `docs/durable-outbox.md` | `56f52326221e0c5c35d64d9a49aa7b7f22bf01d39a8e80ee58b344f87ce0ac6b` |

## 后续工作

直接进入CTRL-001，实现本地Override、Emergency Halt、恢复审批和LocalJournal审计，确保远程
AUTO命令不能解除本地HALT。
