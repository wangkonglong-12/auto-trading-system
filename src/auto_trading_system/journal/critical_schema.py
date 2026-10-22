"""V2 minimal recovery schema. SQL comments are the column data dictionary.

SQLite has no MySQL-style COLUMN COMMENT metadata. Keep the Chinese meaning,
unit and lifecycle next to every column; schema tests enforce this convention.
No migration of an existing V1 journal is performed by application startup.
"""

APPLICATION_ID = 0x41543331
SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- 本库仅由Journal Owner访问；所有表均为关键恢复证据。
CREATE TABLE store_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1), -- 唯一元数据记录，固定为1
    schema_version INTEGER NOT NULL, -- 当前Schema版本，未知版本禁止启动
    journal_generation TEXT NOT NULL, -- 数据库创建代际，恢复及回传绑定此值
    account_id TEXT NOT NULL, -- 绑定账户内部标识，禁止复用到其他账户
    created_at TEXT NOT NULL -- 创建时间，UTC ISO8601
);
CREATE TABLE action_targets (
    action_id TEXT PRIMARY KEY, -- 策略动作幂等键
    target_remaining INTEGER NOT NULL CHECK (target_remaining >= 0), -- 目标剩余持仓，整数股
    rule_id TEXT NOT NULL -- 固定规则版本，后续收紧目标沿用原版本
);
CREATE TABLE submission_records (
    record_seq INTEGER PRIMARY KEY AUTOINCREMENT, -- 单库单调记录序号，恢复分页水位
    client_order_id TEXT NOT NULL UNIQUE, -- 一次实际调用的幂等编号
    payload_hash TEXT NOT NULL, -- 规范化提交正文SHA256，同编号异内容拒绝
    payload_json TEXT NOT NULL, -- 包含计划hash和运行代际的完整恢复依据
    action_id TEXT NOT NULL REFERENCES action_targets(action_id), -- 与本次J1原子保存的动作键
    status TEXT NOT NULL CHECK (status = 'SEND_REQUESTED'), -- 有提交依据，不能据此认定券商已受理
    committed_at TEXT NOT NULL -- J1事务内记录的UTC时间，不代替磁盘确认
);
CREATE TABLE broker_facts (
    fact_key TEXT PRIMARY KEY, -- 账户交易日市场及券商事实编号组成的唯一键
    payload_hash TEXT NOT NULL, -- 原始事实内容摘要，去重并检测冲突
    payload_json TEXT NOT NULL, -- 回报或查询事实正文，禁止写入Tick及普通日志
    recorded_at TEXT NOT NULL -- 本地记录UTC时间，正文另含券商事实时间
);
CREATE TABLE control_records (
    control_id TEXT PRIMARY KEY, -- 授权或收紧动作的唯一编号
    payload_hash TEXT NOT NULL, -- 控制正文摘要，禁止同编号变更内容
    payload_json TEXT NOT NULL, -- 运行代际、许可版本、确认人及权限限额
    recorded_at TEXT NOT NULL -- 本地记录UTC时间；收紧先在内存生效
);
CREATE TABLE recovery_checkpoints (
    checkpoint_id TEXT PRIMARY KEY, -- 恢复检查点唯一编号
    payload_hash TEXT NOT NULL, -- 检查点正文摘要
    payload_json TEXT NOT NULL, -- 持仓归属、策略状态、截止序号及质量
    recorded_at TEXT NOT NULL -- 本地记录UTC时间，不自动恢复交易授权
);
"""
