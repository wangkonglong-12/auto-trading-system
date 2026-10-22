# UI-006 操作员会话与危险控制确认

## 1. 固定实现

Windows本地操作台使用一套进程内安全边界保护控制写操作：

| 能力 | 实现 |
|---|---|
| 操作员凭据 | Windows Credential Manager保存`scrypt`校验器 |
| 密码算法 | `scrypt(N=32768, r=8, p=1, dkLen=32)`、16字节随机盐、版本化URL-safe Base64格式 |
| 浏览器会话 | 256位随机会话令牌、服务端SHA-256索引、`HttpOnly`和`SameSite=Strict` Cookie |
| 会话时限 | 默认15分钟空闲失效、1小时绝对失效，使用单调时钟判定 |
| 登录保护 | 默认5次失败后锁定5分钟，验证过程串行化以限制CPU并发消耗 |
| 写请求保护 | 精确回环Origin、浏览器`Sec-Fetch-Site=same-origin`和独立CSRF令牌 |
| 危险操作确认 | 服务端实时事实快照、SHA-256影响摘要、中文逐字口令、90秒一次性授权 |
| 控制审计 | 既有LocalJournal请求/结果双记录、DomainEvent和Durable Outbox |

操作员密码校验器和HMAC、券商账号一起由`RuntimeSecrets`加载并进入统一脱敏器。浏览器只持有
短时会话Cookie和当前登录返回的CSRF令牌；CSRF令牌只保存在页面内存中，页面重新加载后通过
再次验证操作员身份取得新的写权限。

## 2. 凭据安装和配置

使用运行Runtime的同一Windows专用账户执行：

```powershell
auto-trading-credential `
  --target AutoTrading/node-prod-01/operator-password `
  --kind operator-password
```

命令隐藏回显读取两次密码，在本机计算随机盐`scrypt`校验器，把校验器写入Credential Manager。
原始密码不会写入配置、凭据库、日志或命令行。密码长度为14至128个字符。

生产配置使用：

```json
{
  "local_api": {
    "host": "127.0.0.1",
    "port": 8765,
    "operator_id": "operator_primary",
    "session_idle_seconds": 900,
    "session_absolute_seconds": 3600,
    "login_failure_window_seconds": 300,
    "login_max_failures": 5,
    "control_preview_ttl_seconds": 90
  },
  "security": {
    "operator_password_verifier": {
      "source": "WINDOWS_CREDENTIAL_MANAGER",
      "target": "AutoTrading/node-prod-01/operator-password"
    }
  }
}
```

## 3. HTTP交互

### 3.1 会话

| 方法与路径 | 用途 | 关键结果 |
|---|---|---|
| `POST /api/v1/local/session` | 验证密码并建立会话 | 设置`ats_local_session` HttpOnly Cookie，正文返回CSRF令牌 |
| `GET /api/v1/local/session` | 查询现有只读会话 | 返回操作员和有效期，正文不返回CSRF令牌 |
| `DELETE /api/v1/local/session` | 注销 | 撤销会话、全部预览和授权并清除Cookie |

所有POST和DELETE请求由同源页面发起。服务端同时验证Cookie、Origin、Sec-Fetch-Site和
`X-CSRF-Token`。会话、失败窗口和授权表均有固定容量，并在到期时清理。

### 3.2 影响预览和确认

每个控制固定执行以下顺序：

```text
选择动作和活动委托处置
  -> POST /api/v1/local/control-previews
  -> 展示账户、Profile、Runtime状态、委托、持仓、连接、对账和UNKNOWN事实
  -> 操作员逐字输入服务端口令
  -> POST /api/v1/local/control-previews/{preview_id}/confirm
  -> 获得一次性X-Control-Authorization和服务端命令模板
  -> POST /api/v1/local/controls
  -> 按status_url轮询Journal最终回执
```

影响摘要绑定节点、账户、环境、操作员、动作、原因码、预期状态、活动订单处置和全部实时事实。
确认和提交时服务端重新读取事实；事实变化会返回`CONTROL_PREVIEW_STALE`，页面重新生成预览。
授权首次提交后绑定完整命令，完全相同的网络重试复用该授权，其他命令返回冲突。

统一安全控制菜单提供`PAUSE_BUY`、`SELL_ONLY`、`MANUAL_TAKEOVER`、`EMERGENCY_HALT`、
`CLEAR_HALT`和`REQUEST_AUTO`。Emergency Halt额外要求选择“撤销全部活动委托”或“保留并转人工
核对”。所有动作都显示服务端影响、输入逐字口令并等待最终审计回执。

## 4. 错误和恢复语义

| HTTP | 稳定错误族 | 页面动作 |
|---:|---|---|
| 401 | 会话缺失、失效、密码错误 | 显示操作员验证窗口 |
| 403 | Origin、CSRF、会话归属或口令不匹配 | 终止本次操作并重新验证输入 |
| 409 | 影响过期、事实变化、重复确认或命令不匹配 | 重新读取实时事实并生成新预览 |
| 429 | 登录失败次数达到限制 | 按`Retry-After`显示剩余等待时间 |
| 503 | 预览容量、事实源、Journal或事件入口不可用 | 保持现有Runtime状态并进入本地诊断 |

服务重启会撤销进程内会话、CSRF、预览和授权。LocalJournal中的控制请求与结果继续保留；操作员
重新登录后可以读取历史回执。重启后的新动作必须基于当前Runtime事实生成新的影响预览。

## 5. 代码边界

| 层 | 实现 |
|---|---|
| 密码校验器 | `security/operator_password.py` |
| 会话和CSRF | `security/operator_session.py` |
| 影响摘要和授权 | `control/authorization.py` |
| FastAPI边界 | `apps/local_console/security_api.py`、`control_api.py` |
| 生产组合 | `apps/local_console/composition.py` |
| Vue API客户端 | `frontend/operator-console/src/services/localConsoleApi.ts` |
| 操作界面 | `OperatorLoginDialog.vue`、`ProtectedControlDialog.vue`、`AppShell.vue` |

前端使用严格TypeScript并对所有成功响应执行运行时字段解码。密码字段使用浏览器Password输入，
提交成功或对话框重新打开时清空。页面生成`command_id`和`correlation_id`，动作、操作员、时间、
预期状态和确认记录全部取自服务端授权模板。
