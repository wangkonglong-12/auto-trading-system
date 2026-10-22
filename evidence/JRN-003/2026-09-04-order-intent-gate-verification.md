# JRN-003 订单意图持久化门验收证据

## 验收结论

JRN-003完成。Windows交易节点已建立唯一生产Broker报单路径：新订单必须先把不可变
`OrderIntentRecord`和`ORDER_INTENT_CREATED`事件原子提交到LocalJournal，只有收到该
事务自身的全新`COMMITTED`结果后才调用Broker。重复事务、提交失败、取消、结果错配和
事件缺失均不会调用Broker。

Broker返回后，`request_seq`、`client_order_id`和`remark_token`通过第二个事务建立
持久关联；该事务完成前不会向调用方报告`SUBMITTED`。

## 安全语义

- `owner_guard`限定持久化门只能从唯一Decision Event Loop所有者任务调用；
- 订单命令直接从已校验意图生成，调用者不能向Journal和Broker传入两套不同参数；
- 限价乘数量超过`max_notional`时在任何持久化和Broker副作用前拒绝；
- 相同意图事务返回`DUPLICATE`时进入`PERSISTED_RECONCILIATION_REQUIRED`，禁止自动重报；
- Broker异常、结果不明或非法回执统一报告报单后未决错误；
- Broker已返回但回执关联提交失败/取消时进入安全对账，不返回成功；
- 架构测试阻止生产源码在持久化门之外新增`submit_order(...)`调用。

## 自动化验证

2026-09-04在Windows、Python 3.12.13执行：

- 编译、Ruff、格式检查和mypy严格检查全部通过；
- 912项测试全部通过；
- 总覆盖率97.24%，新增持久化门模块达到100%覆盖；
- 相关专项测试32项通过；
- 真实SQLite/WAL、唯一Persistence Writer和Mock Broker组合验证正常提交、事件Schema、
  Outbox、request seq关联、恢复和重复阻断；
- 使用第二个SQLite连接持有竞争写锁，确认`JOURNAL_BUSY`时Broker订单数为零、意图和
  Outbox均无半写记录；
- sdist与wheel从隔离构建环境生成成功。

## 独立安装验证

在全新虚拟环境中按hash锁定安装运行依赖，以`--no-deps`安装dev14 wheel并验证：

- CLI输出`auto-trading-runtime 0.1.0.dev14`；
- 通过wheel组合SQLite Writer、持久化门和Mock Broker；
- 首次请求返回`SUBMITTED`并产生一个Broker订单；
- 同一请求再次提交返回`PERSISTED_RECONCILIATION_REQUIRED`，Broker订单仍为一个；
- 恢复快照包含唯一意图及request seq关联，Outbox包含唯一领域事件；
- 冒烟结果：`WHEEL_ORDER_INTENT_GATE_SMOKE=PASS`；
- 临时验证环境已安全清理。

## 产物校验和

| 产物 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev14-py3-none-any.whl` | `1a67ca3e9dfc5999fd3c4ed972a98a86cb7bf30b41411832aa270d4c4c434485` |
| `src/auto_trading_system/execution/order_intent_gate.py` | `7884bfdacd4ad1534b4f69031f53b2d1f82ed09494373fcfee643d398e548272` |
| `tests/unit/test_order_intent_gate.py` | `02ca301b052fc973bb2a1614cbe970bbed038cd9ba44812c41212aaacb74dfb8` |
| `tests/integration/test_order_intent_gate_sqlite.py` | `d3ea8b461e8cc10d852c9bc92e39b0bd9d779be579fab1bb2f9131fb6f15a0cf` |
| `docs/order-intent-persistence-gate.md` | `4e57467d784b5cfaa96876cd95a666f095fb662eca5c6b06a090d9856cb68c27` |

## 后续工作

JRN-004直接消费LocalJournal中的PENDING Outbox，实现签名批量上传、逐条ACK、永久拒绝
死信、指数退避、高水位和Runtime安全背压；网络故障与进程重启不得造成永久丢失。
