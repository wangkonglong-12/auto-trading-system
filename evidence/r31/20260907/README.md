# V3.1 首批离线改造证据

日期：2026-09-07。版本：0.1.0.dev38。详细交付与未完项见`docs/design/v3.1/改造实施报告_20260907.md`。

| 文件 | 含义 |
| --- | --- |
| reuse-map.json | 改造前Windows 426个、量化874个文件的路径/hash及复用映射；旧表与默认开关 |
| windows-change-check.json | 相对改造前基线的Windows新增/修改/删除清单；生成本文件前已存在的文件纳入比较 |
| quant-change-check.json | 量化原文件修改0、删除0；新增10个模块内文件的精确清单 |
| python-tests.xml | 最终Windows全部1,575项测试结果，Python 3.11.16 |
| coverage-all.json | 最终Windows行与分支综合覆盖率90.38%及逐文件明细 |
| quant-tests.xml | 量化auto_trading模块119项测试结果，原项目Python 3.13环境；隔离连接/替身测试 |
| journal-latency.json | 开发机200次排队/J1/回执往返延迟；目标QMT存储资格为false |
| simulation-result.json | 最终构建安装后固定Mock的模拟闭环输出；真实交易关闭 |
| build-manifest.json | 最终wheel和sdist的路径、大小、SHA256及安装后验证身份 |

量化回归范围为auto_trading模块，包含原115项和新增4项。这里的证据不等同于量化全项目所有业务测试、实际MySQL并发/恢复演练或真实QMT验收。

Windows全部Python测试由原1,502项和新增73项组成；另完成旧现场台24项前端测试、格式、类型检查与构建。质量门包括仓库凭据边界、Python静态检查、类型检查、覆盖率和构建。

Word修订副本保持47页，最终变化页为7、8、35、40，均已查看。其他43页图像hash与先前已验收版一致。原Word保持原样；渲染文件保存在本轮`.codex_tmp/r31-20260907/render-final`目录。

文件清单统计Git跟踪与非忽略未跟踪文件；忽略的运行数据、环境和临时目录不在hash清单统计内。本轮数据库写入发生于Windows模拟/测试临时库，MySQL迁移未执行。
