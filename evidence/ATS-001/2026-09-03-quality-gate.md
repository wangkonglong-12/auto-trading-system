# ATS-001 质量门证据（2026-09-03）

## 验证环境

- 操作系统：Windows 11 x64
- 验证解释器：Python 3.12.13 x64（开发验证环境）
- 目标生产解释器：Python 3.11 x64（等待GOV-001目标QMT机器确认）
- 应用版本：`0.1.0.dev0`

## 验证结果

| 检查项 | 结果 |
|---|---|
| Python字节码编译 | 通过 |
| Ruff静态检查 | 通过 |
| mypy strict | 通过，27个源文件无问题 |
| pytest | 通过，62项测试全部成功 |
| 分支覆盖率 | 98.27%，门槛90% |
| sdist/wheel构建 | 通过 |
| 清洁虚拟环境wheel安装及版本查询 | 通过，输出`auto-trading-runtime 0.1.0.dev0` |
| 环境诊断脚本JSON输出 | 通过，未发现QMT候选目录 |

统一验证命令：

```powershell
.\scripts\check.ps1 -Python .\.venv-dev\Scripts\python.exe
```

## 构建与锁文件哈希

| 文件 | SHA-256 |
|---|---|
| `requirements/runtime.lock` | `df8bc03b21f9b54e784f30383aa6a744715db1167647fa9da04a167dbbf2d34b` |
| `requirements/dev.lock` | `26cfa4c950e9f1610b2d7c37c9af176930b164d3ec79c1c4647bfb2c2524bfe1` |

`dist`为可重复生成目录，不纳入Git。sdist会包含证据文档，因此不在包内记录自身哈希；
正式发布时由包外发布清单统一记录sdist、wheel和配置哈希。

## 结论

ATS-001的可安装、可测试、版本可查询和本地质量门完成。GitHub Actions定义已建立，
其Python 3.11实际运行结果由CI-001继续验收；本证据不替代目标QMT机器的生产兼容验证。
