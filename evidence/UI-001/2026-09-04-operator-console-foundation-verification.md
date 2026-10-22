# UI-001 Windows本地操作台基础验收证据

## 验收结论

UI-001完成。项目已建立Vue 3、TypeScript、Vite、Naive UI和ECharts前端工程，形成运行总览、
当日计划、委托与成交、持仓与资产、买卖状态、风控与控制、对账中心、告警与事件、系统诊断、
设置与版本十个固定一级路由。页面使用统一应用壳、顶部运行身份区、全局Emergency Halt入口、
明确空态和后续交付边界。

Vite生产资源输出到Python包内，FastAPI只在IPv4回环地址发布同源页面。Host白名单、CSP、点击
劫持保护、MIME保护、浏览器能力限制、入口禁缓存、哈希资源长期缓存和关闭source map均已固化。
前端构建已接入仓库统一质量脚本与Windows CI。

## 自动化验证

2026-09-04在Windows、Node 24.19.0、pnpm 11.19.0和Python 3.12.14执行完整质量门：

- pnpm冻结依赖安装、ESLint、Prettier和Vue/TypeScript严格检查通过；
- 10项Vitest组件与路由测试通过；前端语句覆盖率96.55%、分支92.10%、函数92.85%、行100%；
- Vite生产构建完成，生成1个入口页和6个CSS/JavaScript哈希资源，无source map；
- repository secret boundary、Python编译、Ruff、格式检查和mypy严格检查通过；
- 1152项Python测试全部通过，总覆盖率92.46%；
- 真实构建资源的入口、全部文件、安全头、缓存、Hash路由与Host拒绝集成测试通过；
- sdist与dev21 wheel从隔离构建环境生成成功。

## 独立安装验证

全新虚拟环境从dev21 wheel解析并安装全部声明依赖，随后完成：

- 包版本为`0.1.0.dev21`；
- wheel内存在入口页和6个生产哈希资源，且不存在source map；
- 使用wheel内资源构建本地应用成功；
- `127.0.0.1`入口请求返回HTTP 200并包含`X-Frame-Options: DENY`。

## 产物校验和

| 产物 | SHA-256 |
|---|---|
| `dist/auto_trading_system-0.1.0.dev21-py3-none-any.whl` | `13677230f73806fe433b13fea438d07d4fc894fc2ed687bf081a0bc0495a2c48` |
| `frontend/operator-console/pnpm-lock.yaml` | `210d541f1cf8230d36713a799fd8a909f3b9ac16a0d590f4690ffc791fe287ee` |
| `frontend/operator-console/src/components/AppShell.vue` | `3c18a43f59cfffbd011c5d07a5fa6c090ad5991a601c451c880103040187b75d` |
| `frontend/operator-console/src/router.ts` | `1bfb888145ea6638f4ef0f803384f797def070f5af97f6437b7b253412418160` |
| `src/auto_trading_system/apps/local_console/app.py` | `bb06e0df29fb1a2e71e5737b1431038342daec669b6bb0f2b7075d3538ba3c33` |
| `src/auto_trading_system/apps/local_console/static/index.html` | `898bd68591b691d81c9a59e207fa501653e3b9194d1144ae338387d0f7ae1581` |
| `tests/integration/test_operator_console_packaging.py` | `bb8cc90a449e38c1bf7f7332cae8a702091f3c643bc8fbc2c23e6823d56fc5f4` |
| `docs/windows-operator-console.md` | `e7c94d00e8a772a3a1a7bb36159c28a81ae84f47f569c03ea41a09fd0577b0f3` |

## 后续工作

UI-005依赖的CTRL-001与JRN-002均已完成，下一项直接实现本地控制API、接收回执、最终应用回执
和持久化审计链。UI-005完成后进入UI-006本地会话、CSRF和危险操作二次确认。
