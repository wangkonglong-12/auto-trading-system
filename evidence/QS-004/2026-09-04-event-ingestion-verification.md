# QS-004 事件批量接收、原始存储和幂等回执验收记录

- 验收日期：2026-09-04
- 量化服务项目：`E:\workspace\ai-stock-assistant`
- Windows项目：`E:\workspace\auto-trading-system`
- 结论：通过；QS-004完成，进入QS-005

## 实现结论

1. 新增签名接口`POST /api/v1/trading/events/batch`，一次接收1至50条事件；节点身份、签名请求ID、账户授权和协议版本必须一致。
2. 中心端为9类DomainEvent实现严格Pydantic协议模型，校验未知字段、载荷结构、上海时区、业务日期、聚合绑定、数量守恒、批内唯一性和规范SHA-256。
3. JSON解析拒绝重复对象键；完整请求必须先通过验证才进入Repository，格式或hash错误返回签名422且不写库。
4. 新事件仅追加到`auto_trade_events`，保存规范原始JSON并置`projection_status=PENDING`；接收路径不更新已有事件。
5. `event_id + event_hash`相同返回`DUPLICATE`；相同event_id不同hash返回`EVENT_ID_CONFLICT`；相同`boot_id + local_seq`不同事件返回`LOCAL_SEQUENCE_CONFLICT`。
6. 新事件在一个MySQL事务中提交；存储异常整批回滚并返回签名503，不能对未持久化事件给成功回执。并发唯一键竞争会重新读取并收敛为相同的重复/冲突结果。
7. 回执返回精确`accepted/duplicates/rejected`计数和本批已持久化事件最大`local_seq`。Windows只按逐事件回执推进Outbox，不按高水位批量删除。
8. Windows Mock、语义验证和HTTP传输均已升级到同一冻结契约，程序版本升至`0.1.0.dev26`。

## 双项目冻结契约

两个项目保存并测试字节完全相同的事件Schema和14个Schema/样例文件。关键文件SHA-256：

- `domain-event.schema.json`：`de589c7343ca4469c91f0e8440cba0df12ced5aaae7af482297ceb5148e591e7`
- `event-batch-request.schema.json`：`4df20ca058a70af260b9b9afe6715d800464123b6db70e967e36e2adc47802d2`
- `event-batch-result.schema.json`：`ca36d9b082c84823ec96f17692022e4aece8a551566c4abbd926ebcd2c56267c`
- 批量请求样例：`d35169e4e3d1f5f7c6a6e1841318ac32ddfcf2c9905186a0b9f6baa4f3f70188`
- 批量结果样例：`cebc4b6f5c478de2a57f3d206db1abe522b1ecb0fea23aece90f7c68db7d0bc3`

## MySQL与签名HTTP真实探针

可逆探针临时建立节点、环境密钥引用、事件上传授权和账户，经真实FastAPI路由与MySQL执行：

```json
{
  "ok": true,
  "checks": {
    "accepted_once": true,
    "all_http_200": true,
    "all_responses_signed": true,
    "cleanup_complete": true,
    "duplicate_is_success": true,
    "event_id_conflict_rejected": true,
    "one_immutable_raw_row": true,
    "projection_pending": true,
    "sequence_conflict_rejected": true
  },
  "cleanup": {
    "accounts": 0,
    "events": 0,
    "nodes": 0,
    "nonces": 0
  }
}
```

## 自动化验证

- 量化服务事件、计划和ACK专项：`33 passed`。
- 量化服务全量：`962 passed, 14 failed, 317 subtests passed`；14处仍全部位于此前登记的4个实时新闻SQL/真实数据矩阵文件。
- 排除上述4个既有失败文件的完整隔离回归：`952 passed`。
- Windows事件契约/传输专项：`139 passed`。
- Windows完整质量门：前端`24 passed`；Python`1278 passed`；覆盖率`90.62%`；ruff、格式、mypy、仓库secret边界和sdist/wheel构建全部通过。
- 真实MySQL签名HTTP探针：9类检查全部通过，临时数据零残留。

Windows质量门首次沿用系统Node 18时被前端测试运行时拦截；统一切换工作区随附Node v24.19.0后完整通过，未进行代码绕过。

## 主要文件

量化服务：

- `backend/app/api/auto_trading.py`
- `backend/app/models/auto_trading_schemas.py`
- `backend/app/repositories/auto_trade_event_repository.py`
- `backend/app/services/auto_trade_event_service.py`
- `backend/scripts/auto_trading/verify_auto_trade_event_api.py`
- `backend/docs/auto_trade_event_ingestion.md`
- `backend/contracts/auto_trading/v1/*event*`
- `backend/tests/auto_trading/test_auto_trade_event_api.py`

Windows节点：

- `contracts/v1/domain-event.schema.json`
- `contracts/v1/event-batch-request.schema.json`
- `contracts/v1/event-batch-result.schema.json`
- `src/auto_trading_system/protocol/events.py`
- `src/auto_trading_system/sync/event_transport.py`
- `src/auto_trading_system/testing/central_api.py`

QS-004达到严格接收、原始事实不可变、逐项幂等、冲突失败关闭、签名回执和安全Outbox推进的全部完成标准。
