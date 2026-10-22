# 量化服务Mock API

## 1. 目的与边界

`MockCentralService`是在自动交易项目内运行的确定性FastAPI替身，用于验证Windows Sync Worker
和后续端到端流程。它实现与量化服务相同的协议边界，但不实现量化选股、MySQL中央账本、用户
权限或正式业务投影。

Mock只在测试组合根中创建，生产Runtime禁止导入`auto_trading_system.testing`。默认通过
FastAPI TestClient或HTTPX ASGI Transport进程内调用，不占用端口、不访问真实网络，也不接触
生产凭据。

## 2. 已实现接口

| 接口 | Mock行为 |
|---|---|
| `GET /api/v1/trading/plans/current` | 返回注入的TradePlan和ETag；无计划时返回签名204 |
| `GET /api/v1/trading/runtime/commands` | 按`after_command_seq`过滤、排序并返回高水位 |
| `POST /api/v1/trading/events/batch` | 对每个事件返回独立接受、重复、临时拒绝或永久拒绝回执 |
| `POST /api/v1/trading/snapshots/assets` | 验证认证身份后记录并签名确认 |
| `POST /api/v1/trading/snapshots/positions` | 验证认证身份后记录并签名确认 |
| `POST /api/v1/trading/reconciliations` | 验证认证身份后记录并签名确认 |
| `POST /api/v1/trading/runtime/heartbeat` | 验证认证身份后记录并签名确认 |
| `POST /api/v1/trading/runtime/commands/{id}/result` | 校验路径/Body command id并记录最终结果 |

所有正常响应执行HMAC-SHA256 v1签名。请求使用生产协议实现验签，并真实执行时间窗口与Nonce
防重放，因此客户端测试不能绕过认证流程。

## 3. 故障注入

每个故障从FIFO队列消费一次，之后自动恢复默认行为：

| 类型 | 场景 |
|---|---|
| 网络 | `DISCONNECTED`、`TIMEOUT`在读取Body和占用Nonce前抛出明确异常 |
| 响应认证 | 缺少签名、错误签名、错误Body Hash、绑定错误请求、过期签名 |
| 事件回执 | `ACCEPTED`、`DUPLICATE`、可重试`REJECTED`、永久`REJECTED`，支持批内混合 |
| 服务数据 | 有/无计划、任意合法命令集合、命令高水位和空增量 |

网络故障发生在认证前，因此测试可以用新的HTTP尝试验证重试；真实重试仍必须使用新Nonce。
响应认证故障用于确认Windows不会接收未验签计划/命令，也不会错误删除Outbox。

## 4. 使用示例

```python
from fastapi.testclient import TestClient

from auto_trading_system.testing import MockCentralService, MockResponseFault

service = MockCentralService(
    key_id="key_test_001",
    secret=b"test-secret-with-at-least-32-bytes",
    node_id="node-test-001",
    account_id="acct_test_001",
    epoch_seconds=1788504300,
)
service.set_plan(trade_plan)
service.queue_response_fault(MockResponseFault.INVALID_SIGNATURE)

with TestClient(service.app) as client:
    response = client.get(signed_url, headers=signed_headers)
```

测试应使用`sign_request()`生成每次尝试的认证Header，并用`verify_response()`验证所有响应。
不要直接断言Mock内部私有字段；`received_messages`提供深复制的只读快照，避免测试修改服务状态。

## 5. 与真实量化服务的关系

Mock用于先行稳定Windows客户端和异常恢复；量化服务项目以后仍需独立完成Schema校验、MySQL
事务、幂等键、节点/账户授权、共享Nonce Store及投影测试。两边共同依赖`contracts/v1`及HMAC
固定向量，确保替换Mock时不改变Windows业务核心。
