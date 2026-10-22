# QS-003 计划读取、详情与ACK API验收记录

- 验收日期：2026-09-04
- 量化服务项目：`E:\workspace\ai-stock-assistant`
- Windows项目：`E:\workspace\auto-trading-system`
- 结论：通过；QS-003完成，进入QS-004

## 实现结论

1. 新增独立`/api/v1/trading`节点接口：`GET /plans/current`、`GET /plans/{plan_id}`和`POST /plans/{plan_id}/ack`。
2. current接口使用`account_id + strategy_id + trade_date`精确定位；支持带引号的强ETag，返回签名的200、204或304。
3. 计划返回前同时检查节点账户授权、账户启用状态、计划模式上限、节点支持版本、过期时间、存储ETag及重新计算的计划hash。
4. 服务端实现与Windows冻结向量完全一致的`ATS-HMAC-SHA256 v1`；签名覆盖原始请求目标和精确Body，响应绑定请求ID、Nonce和HTTP状态。
5. 活跃密钥只在MySQL保存`secret_ref`，当前使用`env:VARIABLE_NAME`读取实际密钥；原始密钥不入库。
6. 请求验签成功后将Nonce的SHA-256摘要原子写入`auto_trade_nonces`；重复Nonce和Nonce Store故障均失败关闭。
7. ACK契约固定`ACCEPTED/REJECTED/STARTED/COMPLETED/CANCELLED/EXPIRED`；ACCEPTED必须为READY_PAUSED，NO_TRADE不得STARTED，过期计划只接收EXPIRED。
8. ACK按`node_id + request_id`幂等；相同请求和载荷返回`duplicate=true`，相同请求ID不同载荷返回409。计划行锁保护ACK状态转换并发一致性。
9. API输出READY计划时排除未设置的`no_trade_reason`，保证Windows按收到的JSON重算hash与Publisher一致。

## 双项目冻结契约

两个项目各自保存并校验完全相同的文件：

- `plan-ack-request.schema.json`：`0c3f1a5a0013d0aa8b2cbf525114b89595b0806487d96459880e9f0ec155fef6`
- `plan-ack-result.schema.json`：`cf3a453fcec9373c9b4891b76585dade6f9a159a9988579cb60cc38cc317b300`
- ACCEPTED样例：`3cb54906afddfcbcf88c5bb761be8068d447cf23ae5199c71c3f8870c34c4dbb`
- REJECTED样例：`38cd6c728fd5ab8201f605b0473429cee7f7aa5ea070639464242d77ad362d9f`
- ACK结果样例：`b5533c7a12e0efe056bc8f2359746faeee8140d459414f2412c867749e157b18`

请求和响应签名同时通过CON-004固定向量。

## MySQL与签名HTTP真实探针

可逆探针临时建立节点、环境密钥引用、账户授权和计划，通过真实FastAPI路由和真实MySQL执行：

```json
{
  "ok": true,
  "checks": {
    "current_200": true,
    "current_plan_matches": true,
    "current_hash_shape_matches_windows": true,
    "etag_matches": true,
    "conditional_304": true,
    "ack_created_201": true,
    "ack_duplicate_200": true,
    "one_ack_row": true,
    "all_responses_signed": true,
    "cleanup_complete": true
  },
  "cleanup": {
    "accounts": 0,
    "acks": 0,
    "nodes": 0,
    "nonces": 0,
    "plans": 0
  }
}
```

## 自动化验证

- 量化服务QS-001至QS-003、调度与契约专项：`53 passed`。
- 量化服务排除4个已留证既有数据/SQL失败文件后的完整回归：`937 passed`。
- Windows双项目契约及全部质量门：前端`24 passed`，Python`1274 passed`，覆盖率`90.63%`，ruff、格式、mypy和构建全部通过。
- 真实MySQL签名API探针：全部10项断言通过，临时数据零残留。

Windows质量门首次使用系统旧Node时因缺少`node:inspector/promises`被运行时拦截；切换到工作区随附Node v24.19.0后同一质量门完整通过，未进行代码绕过。

## 主要文件

量化服务：

- `backend/app/api/auto_trading.py`
- `backend/app/core/trading_node_auth.py`
- `backend/app/protocol/auto_trade_signing.py`
- `backend/app/repositories/auto_trade_api_repository.py`
- `backend/app/services/auto_trade_plan_api_service.py`
- `backend/app/models/auto_trading_schemas.py`
- `backend/scripts/auto_trading/verify_auto_trade_plan_api.py`
- `backend/docs/auto_trade_plan_api.md`
- `backend/contracts/auto_trading/v1/plan-ack-*.schema.json`

Windows节点：

- `contracts/v1/plan-ack-request.schema.json`
- `contracts/v1/plan-ack-result.schema.json`
- `contracts/v1/examples/acks/*.json`
- `tests/contract/test_plan_ack_contract.py`

QS-003达到ETag、过期、版本拒绝、签名、授权和ACK幂等全部完成标准。
