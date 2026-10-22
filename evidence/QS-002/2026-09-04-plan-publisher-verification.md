# QS-002 Plan Publisher 验收记录

- 验收日期：2026-09-04
- 量化服务项目：`E:\workspace\ai-stock-assistant`
- 工作项：QS-002 Plan Publisher读取正式尾盘结果
- 结论：通过；QS-002完成，进入QS-003

## 实现结论

1. 新增严格的TradePlan协议模型，禁止额外字段并校验版本、枚举、金额精度、时间、状态和LIVE安全语义。
2. Plan Publisher只读取已提交的14:40正式选股结果：`decision_phase=1440`、`run_role=official`；排除`manual_force`和明确非正式结果。
3. full结果生成`READY`或`NO_TRADE`；partial、算法版本缺失和非法标的统一失败关闭，最多生成`SHADOW/READ_ONLY`，绝不具备LIVE资格。
4. LIVE计划必须同时通过账户环境、账户准入级别、`allow_live`开关、完整数据和READY状态五类门禁；任一条件不满足即降级为SHADOW。
5. `plan_id`、`intent_id`和SHA-256计划哈希确定性生成；计划及明细在一个MySQL事务内写入，并以账户、策略和源运行号保证幂等。
6. APScheduler仅在`AUTO_TRADE_PLAN_PUBLISHER_ENABLED=true`时注册任务；14:40至14:56每分钟检查一次，利用持久化唯一键安全补偿。
7. 发布器只写`auto_trade_plans`和`auto_trade_plan_items`，未接入或修改`user_trades`手工记录链路。

## 主要文件

- `backend/app/models/auto_trading_schemas.py`
- `backend/app/repositories/auto_trade_plan_repository.py`
- `backend/app/services/auto_trade_plan_service.py`
- `backend/app/core/config.py`
- `backend/app/core/scheduler.py`
- `backend/scripts/auto_trading/publish_auto_trade_plans.py`
- `backend/scripts/auto_trading/verify_auto_trade_plan_publisher.py`
- `backend/docs/auto_trade_plan_publisher.md`
- `backend/tests/auto_trading/test_auto_trade_plan_service.py`
- `backend/tests/test_scheduler_startup_config.py`

## 自动化验证

执行QS-001、QS-002及调度专项回归：

```text
35 passed, 1 warning in 2.28s
```

编译检查覆盖协议模型、Repository、Service和两个运维脚本，结果通过。

## MySQL 8真实幂等探针

在现有MySQL 8开发库执行可逆探针。探针建立一个禁用的临时账户和一项计划，连续提交相同计划两次，然后检查数据库并清理：

```json
{
  "ok": true,
  "checks": {
    "first_insert_created": true,
    "second_insert_reused": true,
    "one_plan_row": true,
    "one_item_row": true,
    "stored_hash_matches": true,
    "recomputed_hash_matches": true,
    "cleanup_complete": true
  },
  "cleanup": {
    "accounts_remaining": 0,
    "plans_remaining": 0
  }
}
```

同时以未来日期运行正式发布命令，在不存在正式源结果时返回`NOT_READY`、零写入且退出成功，证明轮询早于正式结果完成时不会制造空计划。

## 全量回归说明

量化服务全量测试结果为`929 passed, 14 failed, 317 subtests passed`。QS-001/QS-002及调度专项测试全部通过；14处失败均位于本工作项未修改的既有实时新闻SQL静态测试和依赖当前MySQL业务数据的选股矩阵测试，失败文件不包含自动交易新增模块。该项目工作树在QS-002开始前已有大量未提交的尾盘策略改动，本项未覆盖或回退这些改动。

## 验收判定

- full生成READY/NO_TRADE：通过。
- partial不得LIVE：通过。
- 过期、非法输入和缺失算法版本失败关闭：通过。
- 确定性哈希、事务写入、重复调用和并发唯一键恢复：通过。
- 默认关闭、显式启用、单实例有界调度：通过。
- 真实MySQL写入与清理：通过。

QS-002达到完成标准。下一工作项为QS-003：计划读取、详情和ACK API。
