"""Strict machine configuration models for the Windows trading node."""

from __future__ import annotations

import re
from decimal import Decimal
from decimal import InvalidOperation as DecimalInvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_validator,
    model_validator,
)


def _parse_decimal_string(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if not isinstance(value, str):
        raise ValueError("must be a decimal string")
    try:
        parsed = Decimal(value)
    except DecimalInvalidOperation as exc:
        raise ValueError("must be a decimal string") from exc
    if not parsed.is_finite():
        raise ValueError("must be a finite decimal string")
    return parsed


def _serialize_decimal(value: Decimal) -> str:
    return format(value, "f")


DecimalText = Annotated[
    Decimal,
    BeforeValidator(_parse_decimal_string, json_schema_input_type=str),
    PlainSerializer(_serialize_decimal, return_type=str, when_used="json"),
]

Exposure = Annotated[
    DecimalText,
    Field(gt=Decimal("0"), le=Decimal("1"), max_digits=9, decimal_places=8),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^(?!0(?:\.0{1,8})?$)(?:0(?:\.[0-9]{1,8})?|1(?:\.0{1,8})?)$",
        },
        mode="validation",
    ),
]
Money = Annotated[
    DecimalText,
    Field(gt=Decimal("0"), max_digits=14, decimal_places=2),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^(?!0(?:\.0{1,2})?$)(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,2})?$",
        },
        mode="validation",
    ),
]
PositiveSeconds = Annotated[
    DecimalText,
    Field(gt=Decimal("0"), le=Decimal("300"), max_digits=8, decimal_places=3),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": (
                r"^(?!0(?:\.0{1,3})?$)(?:(?:[0-9]{1,2}|[12][0-9]{2})"
                r"(?:\.[0-9]{1,3})?|300(?:\.0{1,3})?)$"
            ),
        },
        mode="validation",
    ),
]


class StrictConfigModel(BaseModel):
    """Base for immutable configuration objects with safe validation errors."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class EnvironmentProfile(StrEnum):
    """Deployment profile; the Runtime state machine remains a separate concern."""

    READ_ONLY = "READ_ONLY"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    ASSISTED = "ASSISTED"
    LIVE_EXIT_ONLY = "LIVE_EXIT_ONLY"
    LIVE = "LIVE"


class SecretSource(StrEnum):
    """Supported secret reference kinds."""

    ENVIRONMENT = "ENVIRONMENT"
    WINDOWS_CREDENTIAL_MANAGER = "WINDOWS_CREDENTIAL_MANAGER"


class EnvironmentSecretReference(StrictConfigModel):
    """Development-only reference to a process environment variable."""

    source: Literal[SecretSource.ENVIRONMENT]
    variable: str = Field(pattern=r"^ATS_SECRET_[A-Z0-9_]{3,64}$")


class WindowsCredentialReference(StrictConfigModel):
    """Reference to a generic credential stored by the installation process."""

    source: Literal[SecretSource.WINDOWS_CREDENTIAL_MANAGER]
    target: str = Field(
        min_length=8,
        max_length=160,
        pattern=r"^AutoTrading/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+$",
    )
    encoding: Literal["utf-16-le", "utf-8"] = "utf-16-le"

    @field_validator("target")
    @classmethod
    def validate_target_segments(cls, value: str) -> str:
        if any(segment in {".", ".."} for segment in value.split("/")):
            raise ValueError("credential target contains an invalid segment")
        return value


SecretReference = Annotated[
    EnvironmentSecretReference | WindowsCredentialReference,
    Field(discriminator="source"),
]


class NodeSettings(StrictConfigModel):
    """Logical node identity; the full broker account is loaded as a secret."""

    node_id: str = Field(pattern=r"^node-[a-z0-9][a-z0-9-]{2,47}$")
    account_id: str = Field(pattern=r"^acct_[A-Za-z0-9_-]{3,48}$")
    account_alias: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")


class QmtSettings(StrictConfigModel):
    """Machine-bound QMT connection settings."""

    userdata_mini: Path
    session_id: int = Field(ge=1, le=2_147_483_647)
    account_type: Literal["STOCK"] = "STOCK"

    @field_validator("userdata_mini", mode="before")
    @classmethod
    def validate_userdata_mini(cls, value: object) -> Path:
        if isinstance(value, str):
            value = Path(value)
        if not isinstance(value, Path):
            raise ValueError("userdata_mini must be a path string")
        if not value.is_absolute():
            raise ValueError("userdata_mini must be an absolute path")
        if value.name.casefold() != "userdata_mini":
            raise ValueError("userdata_mini path must end with the userdata_mini directory")
        return value


class CentralApiSettings(StrictConfigModel):
    """Signed HTTPS control-plane endpoint and transport limits."""

    base_url: AnyHttpUrl
    verify_tls: Literal[True] = True
    connect_timeout_seconds: PositiveSeconds = Decimal("3")
    read_timeout_seconds: PositiveSeconds = Decimal("10")
    max_connections: int = Field(default=4, ge=1, le=16)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https":
            raise ValueError("central API must use HTTPS")
        if (value.path or "").rstrip("/") != "/api/v1/trading":
            raise ValueError("central API path must be /api/v1/trading")
        return value


class LocalApiSettings(StrictConfigModel):
    """Loopback-only operator console endpoint."""

    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    operator_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{2,95}$")
    session_idle_seconds: int = Field(default=900, ge=300, le=3600)
    session_absolute_seconds: int = Field(default=3600, ge=900, le=14_400)
    login_failure_window_seconds: int = Field(default=300, ge=60, le=3600)
    login_max_failures: int = Field(default=5, ge=3, le=10)
    control_preview_ttl_seconds: int = Field(default=90, ge=30, le=120)

    @model_validator(mode="after")
    def validate_session_durations(self) -> Self:
        if self.session_absolute_seconds < self.session_idle_seconds:
            raise ValueError("session absolute lifetime must be at least its idle lifetime")
        return self


class StorageSettings(StrictConfigModel):
    """Local state and evidence storage root."""

    root: Path
    minimum_free_space_mb: int = Field(default=2048, ge=1024, le=1_048_576)

    @field_validator("root", mode="before")
    @classmethod
    def validate_root(cls, value: object) -> Path:
        if isinstance(value, str):
            value = Path(value)
        if not isinstance(value, Path):
            raise ValueError("storage root must be a path string")
        if not value.is_absolute():
            raise ValueError("storage root must be an absolute path")
        return value


class HardSafetySettings(StrictConfigModel):
    """Machine-local limits that a central plan cannot exceed."""

    max_positions: int = Field(default=15, ge=1, le=15)
    hard_max_exposure: Exposure = Decimal("0.60")
    max_single_position_exposure: Exposure = Decimal("0.05")
    max_active_orders: int = Field(default=3, ge=1, le=3)
    active_order_per_symbol_side: Literal[1] = 1
    max_single_order_notional: Money
    max_daily_buy_notional: Money
    max_tick_age_seconds: PositiveSeconds = Decimal("1.5")
    max_clock_skew_seconds: int = Field(default=30, ge=1, le=30)

    @model_validator(mode="after")
    def validate_limit_relationships(self) -> Self:
        if self.max_single_position_exposure > self.hard_max_exposure:
            raise ValueError("single-position exposure cannot exceed total exposure")
        if self.max_single_order_notional > self.max_daily_buy_notional:
            raise ValueError("single-order notional cannot exceed daily buy notional")
        return self


class SyncSettings(StrictConfigModel):
    """Control-plane polling and batching cadence."""

    plan_active_interval_seconds: PositiveSeconds = Decimal("2")
    plan_idle_interval_seconds: PositiveSeconds = Decimal("30")
    command_active_interval_seconds: PositiveSeconds = Decimal("2")
    command_long_poll_seconds: PositiveSeconds = Decimal("20")
    event_upload_interval_seconds: PositiveSeconds = Decimal("1")
    event_batch_size: int = Field(default=50, ge=1, le=50)
    outbox_retry_base_seconds: int = Field(default=2, ge=1, le=300)
    outbox_retry_max_seconds: int = Field(default=300, ge=1, le=86_400)
    outbox_high_watermark: int = Field(default=50_000, ge=100, le=999_999)
    outbox_critical_watermark: int = Field(default=100_000, ge=101, le=1_000_000)
    heartbeat_active_interval_seconds: PositiveSeconds = Decimal("5")
    heartbeat_idle_interval_seconds: PositiveSeconds = Decimal("30")

    @model_validator(mode="after")
    def validate_outbox_limits(self) -> Self:
        if self.outbox_retry_max_seconds < self.outbox_retry_base_seconds:
            raise ValueError("outbox retry maximum must be at least the base delay")
        if self.outbox_critical_watermark <= self.outbox_high_watermark:
            raise ValueError("outbox critical watermark must exceed the high watermark")
        return self


class ReconciliationSettings(StrictConfigModel):
    """Periodic broker-fact query cadence."""

    position_interval_seconds: int = Field(default=15, ge=5, le=300)
    asset_interval_seconds: int = Field(default=30, ge=5, le=300)
    active_orders_interval_seconds: int = Field(default=2, ge=1, le=30)


class RuntimeQueueSettings(StrictConfigModel):
    """Bounded decision-ingress lanes with reserved capacity for critical events."""

    normal_capacity: int = Field(default=10_000, ge=100, le=1_000_000)
    critical_capacity: int = Field(default=256, ge=16, le=10_000)


class RecorderSettings(StrictConfigModel):
    """Tick recorder resource limits and retention policy."""

    enabled: bool = True
    queue_capacity: int = Field(default=100_000, ge=1_000, le=2_000_000)
    retention_trading_days: int = Field(default=20, ge=1, le=120)
    compression: Literal["zstd"] = "zstd"


class WatchdogSettings(StrictConfigModel):
    """Independent supervisor timing and crash-loop limits."""

    heartbeat_interval_seconds: int = Field(default=2, ge=1, le=10)
    poll_interval_seconds: int = Field(default=2, ge=1, le=10)
    heartbeat_startup_timeout_seconds: int = Field(default=45, ge=10, le=300)
    heartbeat_stale_timeout_seconds: int = Field(default=10, ge=3, le=120)
    terminate_grace_seconds: int = Field(default=5, ge=1, le=30)
    restart_initial_delay_seconds: int = Field(default=2, ge=1, le=60)
    restart_max_delay_seconds: int = Field(default=60, ge=2, le=600)
    crash_limit: int = Field(default=3, ge=2, le=10)
    crash_window_seconds: int = Field(default=600, ge=60, le=3600)
    circuit_open_seconds: int = Field(default=300, ge=30, le=3600)

    @model_validator(mode="after")
    def validate_timing_relationships(self) -> Self:
        if self.heartbeat_stale_timeout_seconds < self.heartbeat_interval_seconds * 3:
            raise ValueError("heartbeat stale timeout must be at least three heartbeat intervals")
        if self.heartbeat_startup_timeout_seconds < self.heartbeat_stale_timeout_seconds:
            raise ValueError("heartbeat startup timeout must be at least the stale timeout")
        if self.restart_max_delay_seconds < self.restart_initial_delay_seconds:
            raise ValueError("restart maximum delay must be at least the initial delay")
        if self.crash_window_seconds < self.restart_max_delay_seconds:
            raise ValueError("crash window must be at least the restart maximum delay")
        return self


class SecuritySettings(StrictConfigModel):
    """Non-secret identifiers plus opaque references to sensitive values."""

    node_key_id: str = Field(pattern=r"^key_[A-Za-z0-9_-]{4,48}$")
    node_hmac_secret: SecretReference
    broker_account_id: SecretReference
    operator_password_verifier: SecretReference


class RuntimeConfig(StrictConfigModel):
    """Complete version 1.0 machine configuration."""

    config_version: Literal["1.0"]
    profile: EnvironmentProfile
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    restart_mode: Literal["READY_PAUSED"] = "READY_PAUSED"
    node: NodeSettings
    qmt: QmtSettings
    central_api: CentralApiSettings
    local_api: LocalApiSettings
    storage: StorageSettings
    hard_safety: HardSafetySettings
    sync: SyncSettings = Field(default_factory=SyncSettings)
    reconciliation: ReconciliationSettings = Field(default_factory=ReconciliationSettings)
    runtime_queues: RuntimeQueueSettings = Field(default_factory=RuntimeQueueSettings)
    recorder: RecorderSettings = Field(default_factory=RecorderSettings)
    watchdog: WatchdogSettings = Field(default_factory=WatchdogSettings)
    security: SecuritySettings

    @field_validator("profile", mode="before")
    @classmethod
    def parse_profile(cls, value: object) -> EnvironmentProfile:
        if not isinstance(value, str):
            raise ValueError("profile must be a string")
        try:
            return EnvironmentProfile(value)
        except ValueError as exc:
            raise ValueError("profile is not supported") from exc

    @model_validator(mode="after")
    def validate_profile_security(self) -> Self:
        real_order_profiles = {
            EnvironmentProfile.ASSISTED,
            EnvironmentProfile.LIVE_EXIT_ONLY,
            EnvironmentProfile.LIVE,
        }
        references = (
            self.security.node_hmac_secret,
            self.security.broker_account_id,
            self.security.operator_password_verifier,
        )
        if self.profile in real_order_profiles and any(
            reference.source != SecretSource.WINDOWS_CREDENTIAL_MANAGER for reference in references
        ):
            raise ValueError(
                "real-order profiles require Windows Credential Manager secret references"
            )
        if self.storage.root == self.qmt.userdata_mini:
            raise ValueError("storage root must not be the QMT userdata_mini directory")
        return self


def assert_environment_secret_name(name: str) -> None:
    """Validate secret environment names before performing a lookup."""
    if re.fullmatch(r"ATS_SECRET_[A-Z0-9_]{3,64}", name) is None:
        raise ValueError("invalid secret environment variable name")
