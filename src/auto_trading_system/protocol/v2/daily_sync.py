"""Transport contracts for frozen daily revisions and durable APPLIED receipts.

Transport completeness is distinct from business completeness and settlement.
Dataset records carry a stable id and canonical JSON; financial projection is a
separate quant-backend responsibility, not performed by the upload agent.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from auto_trading_system.protocol.v2.base import Sha256, WireId, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.records import MoneyText

Dataset = Literal[
    "plan_results",
    "submission_records",
    "broker_orders",
    "broker_trades",
    "positions",
    "assets",
    "reconciliations",
    "incidents",
]
DATASETS: tuple[Dataset, ...] = (
    "plan_results",
    "submission_records",
    "broker_orders",
    "broker_trades",
    "positions",
    "assets",
    "reconciliations",
    "incidents",
)


class DatasetStatus(WireModel):
    dataset: Dataset
    status: Literal["COMPLETE", "INCOMPLETE", "UNAVAILABLE"]
    record_count: int = Field(ge=0, le=409600)
    reason: str = Field(default="", max_length=256)

    @model_validator(mode="after")
    def explained(self) -> DatasetStatus:
        if self.status != "COMPLETE" and not self.reason:
            raise ValueError("incomplete datasets require a reason")
        if self.status == "UNAVAILABLE" and self.record_count != 0:
            raise ValueError("unavailable is not a partial successful query")
        return self


class PartDescription(WireModel):
    part_id: WireId
    dataset: Dataset
    record_count: int = Field(ge=1, le=200)
    uncompressed_bytes: int = Field(ge=2, le=512 * 1024)
    part_hash: Sha256


class DailyManifest(WireModel):
    schema_major: Literal[2] = 2
    batch_id: WireId
    node_id: WireId
    node_generation: int = Field(ge=1)
    account_id: WireId
    trade_date: date
    revision: int = Field(ge=1)
    supersedes_batch_id: WireId | None = None
    generated_at: AwareDatetime
    data_as_of: AwareDatetime
    journal_generation: WireId
    last_record_seq: int = Field(ge=0)
    parts: tuple[PartDescription, ...] = Field(max_length=2048)
    dataset_status: tuple[DatasetStatus, ...] = Field(min_length=8, max_length=8)
    manifest_hash: Sha256

    @model_validator(mode="after")
    def consistent(self) -> DailyManifest:
        if {s.dataset for s in self.dataset_status} != set(DATASETS):
            raise ValueError("daily manifest must explicitly describe all eight datasets")
        if len({part.part_id for part in self.parts}) != len(self.parts):
            raise ValueError("part ids must be unique")
        if self.generated_at < self.data_as_of:
            raise ValueError("data_as_of must not be after generation")
        if (self.revision == 1) != (self.supersedes_batch_id is None):
            raise ValueError("correction revision requires its previous batch id")
        if self.batch_id == self.supersedes_batch_id:
            raise ValueError("batch cannot supersede itself")
        for status in self.dataset_status:
            count = sum(part.record_count for part in self.parts if part.dataset == status.dataset)
            if count != status.record_count:
                raise ValueError("part counts do not equal dataset count")
        if (
            digest(canonical_bytes(self.model_dump(mode="json", exclude={"manifest_hash"})))
            != self.manifest_hash
        ):
            raise ValueError("daily manifest hash mismatch")
        return self


class DailyRecord(WireModel):
    record_id: WireId
    data: dict[str, JsonValue]


class AppliedReceipt(WireModel):
    batch_id: WireId
    account_id: WireId
    trade_date: date
    revision: int = Field(ge=1)
    manifest_hash: Sha256
    state: Literal["APPLIED"] = "APPLIED"
    business_status: Literal["COMPLETE", "INCOMPLETE"]
    financial_status: Literal["PROVISIONAL", "SETTLED"]
    applied_at: AwareDatetime

    def check_manifest(self, manifest: DailyManifest) -> None:
        for key in ("batch_id", "account_id", "trade_date", "revision", "manifest_hash"):
            if getattr(self, key) != getattr(manifest, key):
                raise ValueError("APPLIED receipt does not bind the uploaded daily revision")


class SessionGrant(WireModel):
    grant_id: WireId
    account_id: WireId
    engine_boot_id: WireId
    session_generation: int = Field(ge=1)
    mode: Literal["PAPER", "SHADOW", "ASSISTED", "LIVE_EXIT_ONLY", "LIVE"]
    directions: tuple[Literal["BUY", "SELL"], ...] = Field(min_length=1, max_length=2)
    max_buy_value: MoneyText
    max_order_value: MoneyText
    max_positions: int = Field(ge=0, le=15)
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    confirmed_by: WireId
    recovery_report_hash: Sha256

    @model_validator(mode="after")
    def conservative(self) -> SessionGrant:
        if len(set(self.directions)) != len(self.directions):
            raise ValueError("grant directions must be unique")
        if not 0 < (self.valid_until - self.valid_from).total_seconds() <= 86400:
            raise ValueError("grant must have a positive window of at most one day")
        if self.mode == "LIVE_EXIT_ONLY" and self.directions != ("SELL",):
            raise ValueError("exit-only grants authorize SELL only")
        return self


class LocalControlRequest(WireModel):
    protocol_major: Literal[2] = 2
    request_id: WireId
    engine_boot_id: WireId
    sender_role: Literal["agent", "supervisor"]
    deadline: AwareDatetime
    expected_state_version: int = Field(ge=0)
    action: Literal[
        "HALT",
        "PAUSE",
        "SNAPSHOT",
        "REQUEST_RECOVERY",
        "LOAD_PLAN",
        "ARM",
        "RESUME",
        "CONFIRM_RECOVERY",
        "EXPORT_DAILY",
        "MAINTENANCE",
    ]
    grant: SessionGrant | None = None
    artifact_hash: Sha256 | None = None
    trade_date: date | None = None
    operator_authorization_id: WireId | None = Field(
        default=None, description="本地操作台已核实的一次性授权ID；消息角色不是身份凭证"
    )

    @model_validator(mode="after")
    def action_payload(self) -> LocalControlRequest:
        if (self.action in ("ARM", "RESUME")) != (self.grant is not None):
            raise ValueError("ARM/RESUME require exactly one session grant")
        if (self.action in ("LOAD_PLAN", "CONFIRM_RECOVERY")) != (self.artifact_hash is not None):
            raise ValueError("plan/recovery actions require the exact artifact hash")
        if (self.action == "EXPORT_DAILY") != (self.trade_date is not None):
            raise ValueError("daily export requires its trading date")
        expands = self.action in ("ARM", "RESUME", "CONFIRM_RECOVERY")
        if expands != (self.operator_authorization_id is not None):
            raise ValueError("permission expansion requires one local operator authorization")
        if self.grant and self.grant.engine_boot_id != self.engine_boot_id:
            raise ValueError("session grant belongs to another engine boot")
        if self.sender_role == "supervisor" and self.action not in ("HALT", "SNAPSHOT"):
            raise ValueError("supervisor can only halt or read state")
        return self


class LocalControlResult(WireModel):
    request_id: WireId
    engine_boot_id: WireId
    status: Literal["APPLIED", "REJECTED", "PENDING"]
    state_version: int = Field(ge=0)
    permission_epoch: int = Field(ge=0)
    reason_code: WireId
    observed_at: AwareDatetime
    artifact_hash: Sha256 | None = None
    in_flight_order_ids: tuple[WireId, ...] = Field(default=(), max_length=32)


class StatementImport(WireModel):
    account_id: WireId
    trade_date: date
    file_hash: Sha256
    statement_revision: int = Field(ge=1)
    original_filename: str = Field(min_length=1, max_length=128)
    media_type: Literal["text/csv", "application/pdf"]
    byte_length: int = Field(ge=1, le=20 * 1024 * 1024)
