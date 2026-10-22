# UI-001 Windows本地操作台基础

## 1. 交付范围

Windows本地操作台采用Vue 3、TypeScript、Vite、Naive UI和ECharts。前端生产构建写入
`auto_trading_system.apps.local_console.static`，与Python Runtime Wheel一并发布。
`trade-runtime`内嵌FastAPI并在`127.0.0.1:<local_api.port>`托管同源页面和后续本地控制API。

本工作项建立下列可持续扩展的基础：

- 十个固定一级路由：运行总览、当日计划、委托与成交、持仓与资产、买卖状态、风控与控制、
  对账中心、告警与事件、系统诊断、设置与版本；
- 固定顶部身份区，持续展示节点、账户掩码、运行环境、Runtime模式、QMT和中心连接状态；
- 全局可见的Emergency Halt入口和六类安全控制菜单；
- 运行总览使用真实状态模型的空态，明确展示数据尚未接入和快照时间；
- 模块页采用统一的标题、交付状态和边界说明，后续工作项按路由替换具体内容；
- 同源静态发布、严格Host校验、安全响应头和长期缓存的哈希资源。

## 2. 目录与依赖边界

| 层 | 目录 | 职责 |
|---|---|---|
| 前端应用 | `frontend/operator-console/src` | 页面、路由、组件、视图模型和样式 |
| 前端测试 | `frontend/operator-console/src/**/*.spec.ts` | 组件行为、路由和状态展示测试 |
| 生产资源 | `src/auto_trading_system/apps/local_console/static` | Vite生成的HTML、CSS和哈希JavaScript |
| 本地Web组合 | `src/auto_trading_system/apps/local_console/app.py` | FastAPI应用、回环边界、静态发布和安全响应头 |
| Python测试 | `tests/unit/test_local_console_app.py`、`tests/integration/test_operator_console_packaging.py` | 服务安全边界和真实构建资源验证 |

页面通过后续Application/Control API读取快照、提交命令和查询最终回执。Gateway、LocalJournal和
QMT SDK继续由Runtime应用层持有。页面进程内异常局限于HTTP请求处理，不进入交易决策循环。

## 3. 路由与状态设计

路由使用Hash History，直接打开`/#/orders`等地址时只需要服务器返回同一个`index.html`，避免
本地静态服务承担前端路由回退逻辑。未知路由统一返回运行总览。

导航元数据集中保存在`navigation.ts`，每项包含路径、名称、标记、职责说明和计划交付编号。
路由表从该元数据生成，页面名称和侧边导航保持单一来源。

应用壳使用不可变的`ConsoleIdentity`和`ConsoleHealth`视图模型。UI-002接入只读快照API时按一次
完整响应替换视图模型，避免多个接口响应在页面中拼接出不一致的账户、模式和连接状态。

## 4. 本地服务安全边界

`build_local_console_app`只接受`RuntimeConfig.local_api.host == 127.0.0.1`，Uvicorn配置固定为
IPv4回环地址并关闭代理头、访问日志和服务器标识。Trusted Host中间件只接受
`Host: 127.0.0.1`。

所有响应统一包含以下浏览器安全策略：

- Content Security Policy限定脚本、连接、样式、图片和字体为同源资源；
- `X-Frame-Options: DENY`和`frame-ancestors 'none'`防止页面被嵌入；
- `X-Content-Type-Options: nosniff`固定资源类型解释；
- Referrer、Permissions和Cross-Origin Opener策略限制浏览器能力；
- `index.html`使用`no-store`，哈希资源使用一年期`immutable`缓存。

前端生产构建关闭source map，发布包只包含运行资源。真实控制命令经过操作员短会话、同源CSRF、
实时影响预览、逐字确认、持久化请求、决策循环和最终回执边界，详见
`docs/operator-console-security.md`。

## 5. 构建与质量门

前端依赖由`pnpm-lock.yaml`精确锁定。Node最低版本为20.19，CI使用Node 22和pnpm 11.19.0。
仓库统一质量脚本依次执行冻结依赖安装、ESLint、Prettier、TypeScript检查、Vitest覆盖率、Vite
生产构建、Python静态检查、Python测试、覆盖率和Wheel构建。

```powershell
.\scripts\check.ps1 -Python .\.venv\Scripts\python.exe -Pnpm pnpm
```

前端覆盖率门为语句/函数/行80%、分支75%；Python仓库总覆盖率门为90%。集成测试读取真实
Vite输出，验证入口页、全部生成资源、缓存头、安全头、Hash路由和Host拒绝。发布验收从新建
虚拟环境安装Wheel，再验证包内资源和回环服务。

## 6. 后续接入顺序

1. UI-005已提供本地控制API、命令接收回执、最终应用回执和审计链。
2. UI-006已提供本地会话、CSRF、六类控制影响预览和逐字确认。
3. UI-002在MON-001与P3事实源完成后接入总览、计划、订单、成交、持仓和资产快照。
4. UI-003在P5状态源完成后接入执行、风控与事件页面。
5. UI-004在对账和监控源完成后接入对账中心与系统诊断。
6. UI-007执行页面崩溃、中心断网、慢请求和Runtime交易隔离验证。
