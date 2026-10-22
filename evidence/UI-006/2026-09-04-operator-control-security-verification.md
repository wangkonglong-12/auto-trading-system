# UI-006 操作员会话与危险控制保护验收证据

## 验收结论

UI-006完成。Windows本地操作台的六类控制已经统一接入操作员密码验证、短时会话、精确同源校验、
CSRF、服务端实时影响预览、逐字口令、短时授权、命令提交和Journal最终回执。

操作员密码以带随机盐的版本化`scrypt`校验器保存到Windows Credential Manager。进程只保存会话
令牌与CSRF令牌的SHA-256摘要；浏览器使用HttpOnly、SameSite Strict Cookie，CSRF令牌只保存在
当前页面内存。服务重启后操作员重新验证身份，既有Journal控制审计仍可读取。

## 完整控制链

```text
操作员密码
  -> POST /api/v1/local/session
  -> HttpOnly会话 + 内存CSRF
  -> POST /api/v1/local/control-previews
  -> 展示账户/环境/状态/委托/持仓/连接/对账事实与影响hash
  -> 逐字输入服务端口令
  -> POST /api/v1/local/control-previews/{id}/confirm
  -> 一次性X-Control-Authorization + 服务端命令模板
  -> POST /api/v1/local/controls
  -> 关键事件入口 -> DecisionLoop -> SQLite/WAL -> Outbox
  -> GET status_url直到最终回执
```

统一菜单覆盖`PAUSE_BUY`、`SELL_ONLY`、`MANUAL_TAKEOVER`、`EMERGENCY_HALT`、`CLEAR_HALT`和
`REQUEST_AUTO`。Emergency Halt明确选择撤销活动委托或保留并转人工核对。所有动作使用相同的
服务端事实绑定和审计流程。

## 安全行为验证

| 行为 | 结果 |
|---|---|
| 密码存储 | CLI生成随机盐`scrypt`校验器，Credential Manager中不含原始密码 |
| 密码验证 | 固定参数、严格格式、恒定时间摘要比较、临时UTF-8缓冲覆写 |
| 登录限流 | 有界失败窗口和串行密码验证，达到阈值返回HTTP 429与`Retry-After` |
| 会话 | 256位随机令牌、服务端哈希索引、空闲/绝对单调时钟失效、固定容量 |
| 浏览器边界 | `127.0.0.1`精确Origin、`Sec-Fetch-Site=same-origin`、CSRF、Trusted Host |
| 影响预览 | 绑定节点、账户、Profile、操作员、Runtime状态、委托、持仓、连接、对账和UNKNOWN |
| 事实变化 | 确认或提交前事实变化返回`CONTROL_PREVIEW_STALE` |
| 授权重放 | 首次命令绑定后只接受完全相同重试，其他正文返回冲突 |
| 注销 | 同时撤销会话、预览、授权和浏览器Cookie |
| 重启 | 短时权限全部失效，SQLite控制终态在重新登录后恢复查询 |
| 页面 | 六类控制展示影响、逐字确认、活动委托处置、处理中状态和最终回执 |
| 响应边界 | 前端对所有成功响应执行运行时字段解码，错误只显示稳定码和边界消息 |

## 自动化验证

2026-09-04在Windows、Node 24.19.0、pnpm 11.19.0和Python 3.12.14执行完整统一质量门：

- 24项前端测试全部通过；
- 前端语句覆盖率85.95%、分支80.00%、函数83.33%、行89.71%；
- ESLint、Prettier、严格TypeScript、Vite生产构建全部通过；
- repository secret boundary、Python编译、Ruff、格式检查和mypy严格检查全部通过；
- 1227项Python测试全部通过，总覆盖率91.12%；
- 真实SQLite链路完成登录、预览、确认、HALT、最终回执、Outbox和重启后回执读取；
- sdist与dev23 wheel从隔离构建环境生成成功。

## 独立安装验证

全新虚拟环境从dev23 wheel解析并安装全部声明依赖，随后完成：

- Distribution版本和源码版本均为`0.1.0.dev23`；
- Wheel包含Console入口和至少四个哈希JavaScript资源；
- 安装包内`scrypt`固定向量验证成功；
- `LocalConsoleSecurityController`与`LocalControlPreviewManager`可直接导入；
- `auto-trading-runtime --version`输出`0.1.0.dev23`。

## 产物校验和

| 产物 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev23-py3-none-any.whl` | `c5cf42371646ed0223d90cea92e8d31a97c554fd02ef0ed2ae738a105713bde9` |
| `src/auto_trading_system/security/operator_password.py` | `183440c3df4a29a6f0a79b6a3e464c7400d947719dcaf9c7a2e9d25f71920461` |
| `src/auto_trading_system/security/operator_session.py` | `77ffdc711bb22c32a4d563cb71e4a62e7434dd43723bf4d67721fd989c1fa67f` |
| `src/auto_trading_system/control/authorization.py` | `def83b7b1e8537f06ad84e3e4fcaacea8179b1645082b7794293e8c6dd853310` |
| `src/auto_trading_system/apps/local_console/security_api.py` | `85001f861c5884805711f606e900539afcb9d56fddca469a4a5303459137a916` |
| `src/auto_trading_system/apps/local_console/control_api.py` | `0b493c003e45b4ba0fa5fd29496f2f9dfd19208fd13da2c734f5d5dc4ce05355` |
| `src/auto_trading_system/apps/local_console/composition.py` | `528b1fe9bb7f56e5d9e40f87be35b00e620dd504051d545346039a66e68492cc` |
| `frontend/operator-console/src/services/localConsoleApi.ts` | `adc966534cd5c0db41bb362abb45c2f3a4402da0a44d402a77e1996415d2c38a` |
| `frontend/operator-console/src/components/ProtectedControlDialog.vue` | `fe57c0584dc51c0f3cc4f0dc9cfbdfe8877608fac3b6de2b3c38f526ebe6386d` |
| `docs/operator-console-security.md` | `ef54be0becc61a8c6638a7ce7dc2d6a5343a2e454ef85fe5a23d5e492ebd2ed0` |

## 后续工作

依赖图中UI-002、UI-003、UI-004和UI-007继续等待QMT、监控、执行和对账事实源。WD-001只依赖已
完成的RT-001，下一项实施独立Watchdog、外部HALT标记和“拉起后停在安全态”的恢复协议。
