# QS-005 订单、成交、资产和持仓投影验收记录

- 验收日期：2026-09-04
- 量化服务项目：`E:\workspace\ai-stock-assistant`
- 结论：通过；QS-005完成，进入QS-006

## 实现结论

1. 新增`ORDER_FILL_V1`中央投影器，以`auto_trade_events`不可变原始事实为唯一输入；查询表是可替换投影，不反向改写事实。
2. 每个订单聚合按`occurred_at + recorded_at + event_id`稳定顺序从完整历史重建，因此HTTP到达顺序和重复运行不改变最终结果。
3. 订单状态先于意图到达时标记`WAITING_DEPENDENCY`，不伪造股票、方向、价格、计划或意图；意图补到后自动重建全部等待事实。
4. 成交可先以空的计划/意图关联落入投影；订单意图到达后在同一事务内回填派生关联。相同成交事实收敛为一行，不同事实复用成交ID失败关闭。
5. 意图内容、订单数量、broker order ID、节点/账户/日期和成交绑定冲突会将聚合标记为`ERROR`，保留原始事实和错误说明。
6. 投影批次有数量上限，APScheduler单实例控制进程内并发，MySQL advisory lock控制跨进程并发；功能默认关闭并要求在专用Scheduler进程显式启用。
7. 资产和持仓投影只使用`AVAILABLE`快照；`UNKNOWN/QUERY_FAILED`不会转成零资产或空仓。最新快照按`as_of + snapshot_id`确定性选择，不依赖到达顺序。
8. 持仓按账户整份原子替换；明确AVAILABLE空数组会清空当前仓位，未知快照不会清空最后可用事实。

## 真实MySQL可逆探针

探针先写入订单状态与成交，再补订单意图和最终FILLED状态，同时验证资产/持仓快照投影：

```json
{
  "ok": true,
  "checks": {
    "all_events_projected": true,
    "asset_latest_available_wins": true,
    "cleanup_complete": true,
    "fill_link_backfilled": true,
    "final_order_filled": true,
    "late_intent_rebuilt_order": true,
    "later_status_rebuilt_order": true,
    "out_of_order_waited": true,
    "positions_replace_and_empty": true,
    "projector_idempotent": true
  }
}
```

清理复核覆盖事件、订单、成交、资产/持仓快照、资产/持仓当前态、投影检查点、节点和账户共10类对象，全部为0。

## 自动化验证

- 投影纯逻辑：5项通过，覆盖乱序收敛、缺依赖、冲突、成交去重和订单关联。
- QS-005关联专项：44项通过；最终变更复核专项31项通过。
- 量化服务排除4个既有实时新闻SQL/真实数据矩阵失败文件后的完整回归：`958 passed`。
- QS-004结束时Windows完整质量门已通过：前端24项、Python1278项、覆盖率90.62%、类型检查和构建全部成功；QS-005未修改Windows运行代码。
- 真实MySQL投影探针：10项业务检查及10类清理检查全部通过。

## 主要文件

- `backend/app/models/auto_trade_projections.py`
- `backend/app/repositories/auto_trade_projection_repository.py`
- `backend/app/services/auto_trade_projection_service.py`
- `backend/scripts/auto_trading/project_auto_trade_events.py`
- `backend/scripts/auto_trading/verify_auto_trade_projections.py`
- `backend/docs/auto_trade_projections.md`
- `backend/tests/auto_trading/test_auto_trade_projections.py`
- `backend/app/core/scheduler.py`
- `backend/app/core/config.py`

QS-005达到重复和乱序事件产生相同订单/成交结果、快照当前态安全选择、显式空仓与未知态分离、跨进程单投影器和真实数据库可重放验证的完成标准。
