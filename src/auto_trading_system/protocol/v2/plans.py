"""Typed immutable plan envelope and Ed25519 verification for V2.

The engine trusts preconfigured public keys, not keys received alongside a plan.
Node upload HMAC secrets cannot sign plans. Verification proves authenticity;
local risk checks and a fresh boot-bound authorization are still required.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import AwareDatetime, Field, model_validator

from auto_trading_system.protocol.v2.base import Sha256, WireId, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.protocol.v2.records import MoneyText, Symbol

_SIGNATURE_DOMAIN = b"AUTO-TRADING/PLAN/V2\n"


class SourceRevision(WireModel):
    source_run_id: WireId = Field(description="正式选股运行编号")
    source_revision: int = Field(ge=1, description="不可变来源版本")
    source_hash: Sha256 = Field(description="同版本来源头和明细摘要")
    algorithm_version: WireId = Field(description="选股算法版本")
    completed_at: AwareDatetime = Field(description="来源生成完成时间")
    market_data_cutoff: AwareDatetime = Field(description="来源行情截止时间")
    scheduled_at: AwareDatetime = Field(description="正式调度计划时点")
    published_at: AwareDatetime = Field(description="来源正式发布时点")
    market_coverage_bps: int = Field(ge=0, le=10000, description="市场覆盖率，万分比整数")
    run_status: Literal["SUCCEEDED"]
    data_completeness: Literal["FULL"]
    publication_status: Literal["FINAL"]
    finalized: Literal[True]

    @model_validator(mode="after")
    def time_order(self) -> SourceRevision:
        if not self.market_data_cutoff <= self.completed_at <= self.published_at:
            raise ValueError("source cutoff, completion and publication must be ordered")
        if self.completed_at < self.scheduled_at:
            raise ValueError("source completes before scheduled time")
        return self


class ExecutionWindow(WireModel):
    entry_not_before: AwareDatetime
    stop_new_buy_at: AwareDatetime
    cancel_by: AwareDatetime
    min_execution_seconds: int = Field(ge=1, le=1200)

    @model_validator(mode="after")
    def ordered(self) -> ExecutionWindow:
        if not self.entry_not_before < self.stop_new_buy_at <= self.cancel_by:
            raise ValueError("execution window must be ordered")
        return self


class Allocation(WireModel):
    allocation_id: WireId
    symbol: Symbol
    rank: int = Field(ge=1, le=500)
    target_value: MoneyText = Field(description="目标金额，元，十进制字符串")
    max_value: MoneyText = Field(description="硬金额上限，元，十进制字符串")
    max_qty: int = Field(ge=1, le=2**31 - 1, description="数量上限，整数股")

    @model_validator(mode="after")
    def bounded(self) -> Allocation:
        if Decimal(self.target_value) > Decimal(self.max_value):
            raise ValueError("allocation target exceeds maximum")
        return self


class PolicyVersion(WireModel):
    component: Literal["entry", "exit", "execution", "risk"]
    version: WireId
    payload_hash: Sha256


class RiskBudget(WireModel):
    max_buy_value: MoneyText
    budget_basis_as_of: AwareDatetime


class OfflineAuthorization(WireModel):
    directions: tuple[Literal["BUY", "SELL"], ...] = Field(min_length=1, max_length=2)
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def unique(self) -> OfflineAuthorization:
        if len(set(self.directions)) != len(self.directions):
            raise ValueError("authorization directions must be unique")
        return self


class PlanPayload(WireModel):
    schema_major: Literal[2] = 2
    plan_id: WireId
    account_id: WireId
    strategy_id: WireId
    trade_date: date
    source: SourceRevision
    execution_window: ExecutionWindow
    allocations: tuple[Allocation, ...] = Field(max_length=500)
    policy_bundle: tuple[PolicyVersion, ...] = Field(min_length=4, max_length=4)
    risk_budget: RiskBudget
    offline_authorization: OfflineAuthorization
    rulebook_version: WireId
    target_node_generation: int = Field(ge=1)

    @model_validator(mode="after")
    def consistent(self) -> PlanPayload:
        if {item.component for item in self.policy_bundle} != {
            "entry",
            "exit",
            "execution",
            "risk",
        }:
            raise ValueError("plan requires all four distinct policy components")
        if len({item.symbol for item in self.allocations}) != len(self.allocations):
            raise ValueError("a symbol can appear only once in a plan")
        if len({item.allocation_id for item in self.allocations}) != len(self.allocations):
            raise ValueError("allocation ids must be unique")
        if len({item.rank for item in self.allocations}) != len(self.allocations):
            raise ValueError("allocation ranks must be unique")
        if sum(Decimal(item.target_value) for item in self.allocations) > Decimal(
            self.risk_budget.max_buy_value
        ):
            raise ValueError("allocated targets exceed approved total budget")
        for instant in (
            self.execution_window.entry_not_before,
            self.execution_window.stop_new_buy_at,
            self.execution_window.cancel_by,
        ):
            if instant.utcoffset() != timedelta(hours=8) or instant.date() != self.trade_date:
                raise ValueError("execution timestamps must use trade_date in UTC+08:00")
        if self.offline_authorization.valid_until < self.execution_window.stop_new_buy_at:
            raise ValueError("plan authorization expires before entry window closes")
        return self


class PlanSignature(WireModel):
    key_id: WireId
    payload_hash: Sha256
    algorithm: Literal["Ed25519"] = "Ed25519"
    signature: str = Field(min_length=88, max_length=88)


class PlanEnvelope(WireModel):
    payload: PlanPayload
    envelope: PlanSignature


def sign_plan(payload: PlanPayload, key_id: str, key: Ed25519PrivateKey) -> PlanEnvelope:
    body = canonical_bytes(payload.model_dump(mode="json"))
    return PlanEnvelope(
        payload=payload,
        envelope=PlanSignature(
            key_id=key_id,
            payload_hash=digest(body),
            signature=base64.b64encode(key.sign(_SIGNATURE_DOMAIN + body)).decode("ascii"),
        ),
    )


def verify_plan(data: bytes, trusted_keys: Mapping[str, Ed25519PublicKey]) -> PlanEnvelope:
    # Parse without duplicate keys/floats before model validation; don't let a
    # lenient JSON parser choose between two different signed field values.
    plan = PlanEnvelope.model_validate_json(canonical_bytes(parse_bytes(data)))
    body = canonical_bytes(plan.payload.model_dump(mode="json"))
    if digest(body) != plan.envelope.payload_hash:
        raise ValueError("plan payload hash mismatch")
    key = trusted_keys.get(plan.envelope.key_id)
    if key is None:
        raise ValueError("plan key is not trusted")
    key.verify(base64.b64decode(plan.envelope.signature, validate=True), _SIGNATURE_DOMAIN + body)
    return plan


def check_plan_binding(
    plan: PlanPayload,
    *,
    account_id: str,
    node_generation: int,
    now: datetime,
) -> None:
    if now.tzinfo is None:
        raise ValueError("current time must be timezone aware")
    now = now.astimezone(plan.execution_window.entry_not_before.tzinfo)
    if plan.account_id != account_id or plan.target_node_generation != node_generation:
        raise ValueError("plan account or node generation mismatch")
    if now.date() != plan.trade_date or now >= plan.offline_authorization.valid_until:
        raise ValueError("plan has expired")
    remaining = (plan.execution_window.stop_new_buy_at - now).total_seconds()
    if remaining < plan.execution_window.min_execution_seconds:
        raise ValueError("insufficient remaining execution window")
