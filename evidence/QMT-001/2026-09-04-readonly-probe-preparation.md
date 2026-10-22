# QMT-001只读API采证执行器准备记录

- 准备日期：2026-09-04
- 工作项状态：`BLOCKED`，等待目标QMT机器真实执行
- 程序版本：`0.1.0.dev34`
- 报告协议：`qmt-readonly-probe/v1`

## 实现范围

| 组件 | 责任 |
|---|---|
| `monitoring/qmt_readonly.py` | 有界状态观测、稳定结论、清理裁决、脱敏报告和排他原子留证 |
| `gateways/broker/cicc_qmt.py` | 动态加载目标机XtQuant，仅适配回调、启动、连接、账户订阅、状态查询、反订阅和停止 |
| `apps/qmt_readonly_probe` | 强制READ_ONLY配置、只解析券商账户凭据、账户单实例保护、执行采证并返回稳定退出码 |
| `scripts/verify-qmt-readonly-probe.ps1` | 在无QMT开发机使用SDK替身复验全部允许/拒绝路径 |

## 已验证安全性质

1. 成功必须同时满足`connect=0`、`subscribe=0`和主动
   `query_account_status()`命中目标账户`status=0`；状态回调不能替代主动查询。
2. 连接失败、订阅失败、目标账户缺失、非OK状态和断线返回`BLOCKED`；SDK返回`None`、类型
   不符、目标账户重复、回调结构异常和清理失败返回`ERROR`。
3. 连接或观测失败后仍执行受控`unsubscribe/stop`；清理失败覆盖原结论并保留原始稳定失败码。
4. 适配器公开接口不包含报单、撤单、资产、持仓、委托和成交查询。
5. 完整账户、`userdata_mini`路径、SDK对象和异常文本不进入报告；账户与路径只保存短指纹，
   账户显示值至少掩盖四位。
6. 报告最大1 MiB，阶段和状态证据分别最多256项；输出使用同目录临时文件、`fsync`和排他
   硬链接原子发布，已有证据不会被覆盖，并对完整账户执行UTF-8/UTF-16双编码残留检查。
7. 动态`xtquant`引用仍受架构测试约束，只允许位于QMT Adapter文件。
8. 探针在创建QMT客户端前获取与正式Runtime相同的账户级Global Mutex；锁冲突不访问QMT，
   QMT清理和锁释放均完成后才允许发布报告。同一Session的跨进程连接间隔由现场规范固定为
   超过3秒。

## 验证结论

专项测试覆盖核心状态机、真实适配调用顺序、回调复制、SDK类型漂移、CLI凭据边界、报告不可变
性、失败关闭和版本入口。目标机现场仍需填写`target-machine-template.md`并归档真实JSON与
SHA-256；本记录不替代QMT-001的真实SDK、账户和客户端验收。

## 自动验证结果

- QMT专项验证：78项通过，入口版本为`auto-trading-qmt-probe 0.1.0.dev34`；
- Windows全量验证：1354项Python测试通过，总覆盖率90.12%；
- 前端验证：24项测试、ESLint、Prettier、TypeScript检查和Vite生产构建通过；
- Ruff、Mypy、仓库敏感信息边界、Python格式和Windows任务脚本验证通过；
- `0.1.0.dev34` sdist/wheel构建成功，并在全新虚拟环境按哈希锁安装；安装后的CLI版本和
  QMT探针模块导入通过；
- wheel SHA-256：`3815591625d5c0f8218a10ea407cd573aee29f1a7fd60e88f4638db334636d54`；
- sdist SHA-256：`321fc33142628a51592643fc59f9b45669d19f3ba218606d25b71b75460c83b9`；
- `requirements/runtime.lock` SHA-256：
  `b284c0c57f0b0702b9e6119bf1b279c836e636e3e005eb1ed0fd8430835da341`；
- 专项验证脚本SHA-256：
  `8d86a143a922502c5b3667f4684da670fd623987ded5a3d0a269ad78c4fc5373`。
