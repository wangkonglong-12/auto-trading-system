# 量化服务自动交易模块目录隔离验证

## 结论

量化服务中的自动交易增量代码已经迁入各层独立的 `auto_trading` 子目录，并由后端与前端总开关控制。默认配置下，量化服务不注册自动交易 API、不装载自动交易业务运行模块、不注册自动交易后台任务，前端不注册自动交易页面和菜单。

本次操作没有删除量化服务或 Windows 自动交易项目中的任何受版本控制文件，没有执行仓库清理、重置、数据库迁移写入或数据删除。

## 目录边界

- 后端 API：`backend/app/api/auto_trading`
- 后端配置与调度：`backend/app/core/auto_trading`
- 后端模型、协议、数据访问和服务：各层 `auto_trading` 子目录
- 脚本与迁移：`backend/scripts/auto_trading`
- 测试：`backend/tests/auto_trading`
- 文档：`backend/docs/auto_trading`
- 协议副本：`backend/contracts/auto_trading/v1`
- 前端 API 与页面：`frontend/src/api/auto_trading`、`frontend/src/views/auto_trading`

## 启用规则

- 后端总开关：`AUTO_TRADE_MODULE_ENABLED=false`
- 前端总开关：`VITE_AUTO_TRADE_MODULE_ENABLED=false`
- 自动交易计划发布、事件投影和监控任务只有在后端总开关与各自子开关同时开启时注册。
- MySQL 自动交易迁移仍由显式命令执行，不随量化服务启动自动运行。

## 验证结果

- 两个项目受版本控制文件删除数：`0`
- 默认关闭启动：自动交易路由数量 `0`，已装载自动交易运行模块数量 `0`
- 自动交易专项与共享调度测试：`128 passed`
- 自动交易 Python 目录编译：通过
- 前端默认关闭和显式开启两种生产构建：均通过
- 最终默认关闭构建中的自动交易页面产物：`0`
- 双项目协议：双方各 49 个实际文件逐文件 SHA-256 一致；清单登记的 48 个受保护文件一致
- MySQL：仅执行 `--verify-only`，27 张 `auto_trade_*` 表结构验证通过且当前均为空；本次没有数据库写入
- 量化服务完整后端回归：`1022 passed, 14 failed`；14 项失败均位于本次目录隔离范围外的既有真实数据矩阵或资讯 SQL 静态断言，自动交易测试无失败
- 总体设计文档已更新为 `尾盘量化自动交易系统整体实现与详细设计方案_v2.4.docx`；通过 Microsoft Word 只读导出并对 62 页渲染结果逐页检查，未发现文字截断、对象重叠、缺字或表格破损

## 环境限制

Windows 项目当前没有 `.venv\Scripts\python.exe`，系统 Python 也缺少 `jsonschema`，因此联合脚本中的 Windows 测试段未在本机重新执行。替代验证已完成：双项目协议逐文件哈希一致，量化服务端契约测试通过。该限制不影响目录隔离和默认关闭结论。
