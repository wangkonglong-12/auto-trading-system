"""V2 HTTP payloads, independent from FastAPI and transport implementations."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from auto_trading_system.protocol.v2.base import Sha256, WireId, WireModel
from auto_trading_system.protocol.v2.plans import PlanEnvelope


class ApiError(WireModel):
    request_id: WireId
    code: WireId
    message: str = Field(min_length=1, max_length=512)
    retryable: bool


class PlanReceipt(WireModel):
    receipt_id: WireId
    plan_id: WireId
    payload_hash: Sha256
    node_id: WireId
    node_generation: int = Field(ge=1)
    account_id: WireId
    engine_boot_id: WireId
    status: Literal["LOADED", "REJECTED", "WITHDRAWN", "CHANGE_REQUIRES_PAUSE"]
    reason_code: WireId
    observed_at: AwareDatetime


class PlanLookup(WireModel):
    account_id: WireId
    strategy_id: WireId
    trade_date: date
    plan_id: WireId
    state: Literal["PUBLISHED", "WITHDRAWN"]
    state_version: int = Field(ge=1)
    etag: Sha256
    plan: PlanEnvelope | None = None

    @model_validator(mode="after")
    def identity(self) -> PlanLookup:
        if (self.state == "PUBLISHED") != (self.plan is not None):
            raise ValueError("published lookup requires a plan; withdrawal has no new plan")
        if self.plan is not None:
            payload = self.plan.payload
            for name in ("account_id", "strategy_id", "trade_date", "plan_id"):
                if getattr(payload, name) != getattr(self, name):
                    raise ValueError("lookup identity differs from signed plan")
        return self


class CompleteDaily(WireModel):
    manifest_hash: Sha256


class DailyProgress(WireModel):
    batch_id: WireId
    manifest_hash: Sha256
    state: Literal["CREATED", "RECEIVING", "RECEIVED", "APPLYING", "APPLIED", "REJECTED"]
    missing_parts: tuple[WireId, ...] = Field(max_length=2048)


class CapabilityManifest(WireModel):
    schema_major: Literal[2] = 2
    manifest_version: WireId
    generated_at: AwareDatetime
    capabilities: tuple[WireId, ...] = Field(max_length=32)
    maximum_part_bytes: int = Field(default=512 * 1024, ge=1, le=512 * 1024)
    maximum_part_records: int = Field(default=200, ge=1, le=200)
    maximum_batch_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=16 * 1024 * 1024)
    trusted_plan_key_ids: tuple[WireId, ...] = Field(max_length=8)
    rulebook_versions: tuple[WireId, ...] = Field(max_length=16)
