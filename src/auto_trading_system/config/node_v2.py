"""Versioned process configuration; secret references, never plaintext keys.

QMT_READ_ONLY is a long-lived broker-backed diagnostic profile whose SDK
boundary rejects commands. Local operator authentication may still be enabled
to protect account details in the browser. QMT_TRADING is separately configured.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, JsonValue, model_validator

from auto_trading_system.config.credentials import EnvironmentSecretProvider
from auto_trading_system.config.models import SecretReference, StrictConfigModel
from auto_trading_system.config.paper_session import PaperSession
from auto_trading_system.config.windows_credentials import build_default_secret_providers
from auto_trading_system.control.ipc_protocol import Role
from auto_trading_system.protocol.v2.base import WireId
from auto_trading_system.protocol.v2.canonical import canonical_bytes, parse_bytes
from auto_trading_system.protocol.v2.plans import PolicyVersion
from auto_trading_system.protocol.v2.records import MoneyText
from auto_trading_system.reconcile.position_lots import ExchangeCalendar


class IPCSecrets(StrictConfigModel):
    agent: SecretReference = Field(description="代理到引擎的本机认证凭证引用")
    jobrunner: SecretReference = Field(description="定时任务到代理的本机认证凭证引用")
    operator: SecretReference = Field(description="现场状态/急停/退出命令的凭证引用")


class OperatorAccess(StrictConfigModel):
    actor_id: str = Field(
        pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{2,95}$", description="本机唯一操作员身份，由配置确定"
    )
    password_verifier: SecretReference = Field(description="独立scrypt口令校验值的凭据引用")


class SupervisionPolicy(StrictConfigModel):
    """Operational limits only; these values never select or authorize a strategy."""

    poll_seconds: int = Field(default=1, ge=1, le=5, description="独立监护检查间隔，秒")
    startup_seconds: int = Field(default=30, ge=5, le=120, description="首次主循环进度等待秒数")
    stale_seconds: int = Field(default=10, ge=3, le=60, description="主循环进度停止容忍秒数")
    stop_seconds: int = Field(default=5, ge=1, le=15, description="停止自有子进程等待秒数")
    restart_seconds: int = Field(default=2, ge=1, le=30, description="失败后指数退避的初始秒数")
    failure_limit: int = Field(default=3, ge=1, le=10, description="累计失败熔断阈值，跨重启保留")
    minimum_free_bytes: int = Field(
        default=268435456, ge=1048576, description="数据盘最低剩余字节数，低于阈值停止并锁定"
    )


class QmtReadOnlySettings(StrictConfigModel):
    userdata_mini: Path = Field(description="中金极简交易终端的userdata_mini绝对目录")
    account_fingerprint: str = Field(
        pattern=r"^[a-f0-9]{16}$", description="已验证股票资金账号的SHA-256前16位"
    )
    session_base: int = Field(default=6102301, ge=1, le=2_147_483_646)

    @model_validator(mode="after")
    def dedicated_path(self) -> Self:
        if (
            not self.userdata_mini.is_absolute()
            or self.userdata_mini.name.casefold() != "userdata_mini"
            or self.userdata_mini.resolve() != self.userdata_mini.absolute()
        ):
            raise ValueError("QMT userdata_mini must be a direct absolute directory")
        return self


class NodeProcessConfig(StrictConfigModel):
    schema_major: Literal[2] = Field(default=2, description="独立进程配置主版本")
    broker: Literal["MOCK", "QMT_READ_ONLY", "QMT_TRADING"] = Field(description="节点的券商接入档")
    node_id: WireId = Field(description="量化系统登记的节点身份")
    account_id: WireId = Field(description="业务账户别名，决定恢复与互斥范围")
    node_generation: int = Field(ge=1, description="部署代际，绑定计划和上传")
    journal_generation: WireId = Field(description="当前关键恢复库代际")
    strategy_id: WireId = Field(description="拉取计划对应的策略身份")
    service_sid: str = Field(pattern=r"^S-1-(?:[0-9]+-)+[0-9]+$", description="专用运行用户SID")
    data_root: Path = Field(description="init命令创建的专属受保护数据目录，绝对路径")
    ipc_secrets: IPCSecrets
    operator_access: OperatorAccess | None = Field(
        default=None, description="交互式恢复确认/PAPER授权；未配置时保持此入口关闭"
    )
    quant_origin: str = Field(description="量化系统HTTPS源地址")
    quant_key_id: WireId = Field(description="量化HTTP签名密钥编号")
    quant_secret: SecretReference = Field(description="量化HTTP密钥引用，与三类IPC凭证分离")
    quant_ca_file: Path | None = Field(default=None, description="可选私有CA证书文件；仍校验主机名")
    trusted_plan_keys: dict[WireId, str] = Field(
        min_length=1, max_length=8, description="可信Ed25519公钥hex"
    )
    policies: tuple[PolicyVersion, ...] = Field(
        min_length=4, max_length=4, description="已安装四类策略版本"
    )
    rulebook: WireId = Field(description="已安装证券交易规则版本")
    calendar: ExchangeCalendar = Field(description="明确交易日列表及日历版本")
    mock_cash: MoneyText = Field(default="10000.00", description="MOCK初始资金，明确测试值")
    qmt_read_only: QmtReadOnlySettings | None = Field(
        default=None, description="真实QMT账户绑定；账号以指纹绑定，权限由broker档位决定"
    )
    paper_session: PaperSession | None = Field(
        default=None, description="显式离线测试会话；缺省仍零额度且不开启买卖驱动"
    )
    supervision: SupervisionPolicy = Field(
        default_factory=SupervisionPolicy, description="独立进程监护；与策略参数和交易授权分离"
    )

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if not self.data_root.is_absolute() or self.data_root == Path(self.data_root.anchor):
            raise ValueError("data root must be a dedicated absolute directory")
        if self.data_root.resolve() != self.data_root.absolute():
            raise ValueError("data root cannot traverse links")
        if self.quant_ca_file is not None and not self.quant_ca_file.is_absolute():
            raise ValueError("CA path must be absolute")
        origin = urlsplit(self.quant_origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or origin.path not in ("", "/")
        ):
            raise ValueError("quant origin must be HTTPS without a path or credentials")
        references = [
            self.quant_secret,
            self.ipc_secrets.agent,
            self.ipc_secrets.jobrunner,
            self.ipc_secrets.operator,
        ]
        if self.operator_access:
            references.append(self.operator_access.password_verifier)
        if len({ref.model_dump_json() for ref in references}) != len(references):
            raise ValueError("HTTP and IPC roles require separate credential references")
        if {item.component for item in self.policies} != {"entry", "exit", "execution", "risk"}:
            raise ValueError("all four policies must be present")
        self.public_keys()
        if self.paper_session is not None:
            if self.broker != "MOCK":
                raise ValueError("paper session requires MOCK broker")
            if self.operator_access is None:
                raise ValueError("paper execution requires local operator authentication")
            if self.paper_session.as_of.date() not in self.calendar.sessions:
                raise ValueError("paper session date is outside installed calendar")
            if not self.account_id.startswith("paper-"):
                raise ValueError("paper session requires a paper- account alias")
        if self.broker in {"QMT_READ_ONLY", "QMT_TRADING"}:
            if self.qmt_read_only is None:
                raise ValueError("QMT profile requires its account binding")
            if self.broker == "QMT_TRADING" and self.operator_access is None:
                raise ValueError("QMT trading requires local operator authentication")
            if self.account_id != "qmt-" + self.qmt_read_only.account_fingerprint:
                raise ValueError("QMT account alias must bind the discovered fingerprint")
            if self.paper_session is not None:
                raise ValueError("QMT cannot use a paper session")
        elif self.qmt_read_only is not None:
            raise ValueError("MOCK cannot carry a QMT binding")
        return self

    def public_keys(self) -> dict[str, Ed25519PublicKey]:
        return {
            name: Ed25519PublicKey.from_public_bytes(bytes.fromhex(value))
            for name, value in self.trusted_plan_keys.items()
        }

    def pipe_name(self, channel: str) -> str:
        identity = hashlib.sha256((self.node_id + ":" + self.account_id).encode()).hexdigest()[:32]
        return rf"\\.\pipe\AutoTrading.v2.{identity}.{channel}"

    def scope(self) -> bytes:
        fields: dict[str, JsonValue] = {
            "schema_major": 2,
            "node_id": self.node_id,
            "account_id": self.account_id,
            "node_generation": self.node_generation,
            "journal_generation": self.journal_generation,
            "broker": self.broker,
            "service_sid": self.service_sid,
        }
        if self.qmt_read_only:
            fields["qmt_read_only"] = self.qmt_read_only.model_dump(mode="json")
        return canonical_bytes(fields)


def load_node_config(path: Path) -> NodeProcessConfig:
    with path.open("rb") as source:
        value = parse_bytes(source.read(131073), max_bytes=131072)
    return NodeProcessConfig.model_validate_json(canonical_bytes(value))


def resolve_secret(
    reference: SecretReference, *, environment: Mapping[str, str] | None = None
) -> bytes:
    providers = build_default_secret_providers(
        EnvironmentSecretProvider(os.environ if environment is None else environment)
    )
    key = providers[reference.source].read(reference).get_secret_value().encode("utf-8")
    if not 32 <= len(key) <= 1024:
        raise ValueError("credential length is outside supported bounds")
    return key


def role_keys(config: NodeProcessConfig, roles: tuple[Role, ...]) -> dict[Role, bytes]:
    keys = {role: resolve_secret(getattr(config.ipc_secrets, role)) for role in roles}
    if len(set(keys.values())) != len(keys):
        raise ValueError("IPC role keys must differ")
    return keys
