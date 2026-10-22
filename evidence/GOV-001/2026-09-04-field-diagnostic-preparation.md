# GOV-001 目标机诊断准备验收记录

- 验收日期：2026-09-04
- 工作范围：`GOV-001`现场执行准备
- 工作项状态：`BLOCKED`，等待目标QMT机器执行
- 程序版本：`0.1.0.dev33`

## 已完成交付

| 交付物 | 结论 |
|---|---|
| `scripts/diagnose-environment.ps1` | Windows PowerShell 5.1兼容的只读、脱敏、有界诊断脚本 |
| `docs/qmt-field-diagnostic.md` | 输入、命令、退出码、阻塞码、证据和后续PoC顺序已冻结 |
| `evidence/GOV-001/target-machine-template.md` | 版本三元组、权限审批、回滚包和双人复核模板已落盘 |
| wheel资源 | 诊断脚本随`auto_trading_system/deployment`发布 |

## 自动验证

- 使用真实Windows PowerShell 5.1执行；
- 使用临时QMT目录验证二进制SHA-256、XtQuant目录指纹、`userdata_mini`和
  `up_queue_xtquant`识别；
- 验证Python版本、位数、解释器SHA-256和XtQuant导入探针；
- 验证已有JSON的原子替换、绝对路径失败关闭和隐私字段脱敏；
- 验证脚本不包含QMT会话、行情订阅、查询或报单调用，并且不使用递归磁盘扫描。

验证结果：

- 诊断专项、Windows任务脚本和版本专项共12项通过；
- 完整Python测试1318项通过，总覆盖率90.48%；
- 前端测试24项、ESLint、Prettier、TypeScript检查和Vite生产构建通过；
- Ruff、Mypy、仓库敏感信息边界、Python编译和Windows任务脚本验证通过；
- `0.1.0.dev33` sdist/wheel构建成功；wheel SHA-256为
  `fcef3a5f94f40a4f215944ed98a580fff3e207f2852b422ad9e3a6ec9d99940a`；
- 全新虚拟环境按哈希锁安装成功，安装后Runtime版本、诊断资源和脚本执行均通过；
- 源码与安装后诊断脚本SHA-256一致：
  `713f0c43500765e2afe2fe6dee7d4d5f7724f314831c7a2e8bfef9fb3865cd89`。

当前开发机执行`-RequireReady`返回退出码2，阻塞码为
`PYTHON_3_11_X64_NOT_FOUND`、`QMT_INSTALLATION_NOT_FOUND`、`XTQUANT_NOT_FOUND`、
`USERDATA_MINI_NOT_FOUND`和`FUNCTION_TRADING_QUEUE_NOT_FOUND`，与现有环境事实一致。

## 当前结论

本机仍未提供真实QMT/XtQuant、Python 3.11、`userdata_mini`和交易权限，因此本记录只关闭
现场诊断工具准备，不关闭`GOV-001`。目标机按规范生成JSON、补全模板并通过复核后，
`GOV-001`进入`VERIFYING`，随后执行`QMT-001`只读PoC。
