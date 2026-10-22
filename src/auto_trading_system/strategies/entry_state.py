"""Plan-bound installed entry policy and immutable once-only allocation targets.

The signed plan pins the complete policy hash. Context/targets use the existing
single-writer recovery table; quote streams and execution progress stay in memory.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from auto_trading_system.journal.critical_writer import CriticalStorePort
from auto_trading_system.protocol.v2.base import Sha256, WireId, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanPayload
from auto_trading_system.protocol.v2.records import MoneyText, PriceText, Symbol


class EntryInstrument(WireModel):
    symbol: Symbol = Field(description="证券代码及交易所")
    reference_price: PriceText = Field(description="计划追价保护参考价，元")
    lower_price: PriceText = Field(description="当日合法申报价下界，元")
    upper_price: PriceText = Field(description="当日合法申报价上界，元")
    price_tick: PriceText = Field(description="已验证价格档位，元")
    min_quantity: int = Field(ge=1, le=10000, description="合法最小买入数量，股")
    quantity_step: int = Field(ge=1, le=10000, description="超过最低数量后的申报步长，股")
    max_order_quantity: int = Field(ge=1, le=2**31 - 1, description="规则包单笔股数上限")
    source_hash: Sha256 = Field(description="参考价和证券规则来源证据摘要")

    @model_validator(mode="after")
    def bounds(self) -> EntryInstrument:
        if (
            any(
                Decimal(v) <= 0
                for v in (self.reference_price, self.lower_price, self.upper_price, self.price_tick)
            )
            or Decimal(self.lower_price) > Decimal(self.upper_price)
            or self.max_order_quantity < self.min_quantity
        ):
            raise ValueError("invalid verified entry instrument")
        return self


class EntryPolicy(WireModel):
    version: WireId = Field(description="已安装买入策略版本，由签名计划引用")
    rulebook_version: WireId = Field(description="已安装证券规则包版本")
    fee_reserve: MoneyText = Field(description="每个买入子单保守费用预留，元")
    max_chase_bps: int = Field(ge=0, le=1000, description="相对参考价的最大追价，基点")
    max_spread_bps: int = Field(ge=0, le=1000, description="买卖一价差相对买一的上限，基点")
    participation_bps: int = Field(ge=1, le=10000, description="60秒成交量参与上限，基点")
    depth_bps: int = Field(ge=1, le=10000, description="可见卖一数量使用比例，基点")
    quote_max_age_ms: int = Field(ge=100, le=5000, description="源时间与单调时钟双重新鲜度上限")
    cancel_after_seconds: int = Field(ge=1, le=300, description="普通阶段子单最小调整间隔，秒")
    max_children: int = Field(ge=1, le=4, description="每个分配最多子单数，含首次及至多三次补单")
    max_active_orders: int = Field(ge=1, le=3, description="账户同时活动或不明委托数上限")
    instruments: tuple[EntryInstrument, ...] = Field(
        min_length=1, max_length=15, description="本次已核验证券规则，最多15个唯一标的"
    )

    @model_validator(mode="after")
    def unique(self) -> EntryPolicy:
        if len({i.symbol for i in self.instruments}) != len(self.instruments):
            raise ValueError("duplicate entry instrument")
        return self

    @property
    def payload_hash(self) -> str:
        return digest(canonical_bytes(self.model_dump(mode="json")))


class EntryTarget(WireModel):
    plan_hash: Sha256 = Field(description="已验签冻结计划的正文摘要")
    policy_hash: Sha256 = Field(description="计划引用的已安装买入策略正文摘要")
    account_id: WireId = Field(description="所属账户内部别名")
    trade_date: date = Field(description="所属交易日")
    allocation_id: WireId = Field(description="不可重复初始化的分配编号")
    symbol: Symbol = Field(description="分配所属证券")
    boot_id: WireId = Field(description="首次确定目标时的进程代际")
    decided_at: AwareDatetime = Field(description="首次确定目标或跳过决定的时间")
    arrival_price: PriceText | None = Field(description="首次合格盘口的卖一价；无盘口跳过时为空")
    target_quantity: int = Field(ge=0, le=2**31 - 1, description="一次性目标股数，跳过为零")
    reason: Literal["ELIGIBLE", "SKIPPED_MIN_ORDER", "POSITION_CAPACITY", "MISSED_ENTRY_WINDOW"] = (
        Field(description="初始化决定，跳过决定在重启后保持")
    )

    @model_validator(mode="after")
    def valid(self) -> EntryTarget:
        if (
            self.decided_at.utcoffset() != timedelta(hours=8)
            or self.decided_at.date() != self.trade_date
            or (self.reason == "ELIGIBLE") != (self.target_quantity > 0)
            or (self.target_quantity > 0 and self.arrival_price is None)
            or (self.arrival_price is not None and Decimal(self.arrival_price) <= 0)
        ):
            raise ValueError("invalid entry target decision")
        return self

    @property
    def payload_hash(self) -> str:
        return digest(canonical_bytes(self.model_dump(mode="json")))

    @property
    def key(self) -> str:
        return f"entry-target:{self.plan_hash}:{self.allocation_id}"


def validate_policy(
    plan: PlanPayload, policy: EntryPolicy, *, require_payload_hash: bool = True
) -> None:
    version = next(p for p in plan.policy_bundle if p.component == "entry")
    if (
        version.version != policy.version
        or (require_payload_hash and version.payload_hash != policy.payload_hash)
        or plan.rulebook_version != policy.rulebook_version
        or {a.symbol for a in plan.allocations} != {i.symbol for i in policy.instruments}
    ):
        raise ValueError("installed entry policy must exactly match the signed plan")


def load_entry_targets(
    store: CriticalStorePort,
    plan: PlanPayload,
    policy: EntryPolicy,
    *,
    require_payload_hash: bool = True,
) -> tuple[EntryTarget, ...]:
    validate_policy(plan, policy, require_payload_hash=require_payload_hash)
    store.append_evidence(
        "recovery_checkpoints",
        f"entry-context:{plan.trade_date}",
        canonical_bytes(
            {"plan": plan.model_dump(mode="json"), "policy": policy.model_dump(mode="json")}
        ),
    )
    plan_hash = digest(canonical_bytes(plan.model_dump(mode="json")))
    result = []
    for allocation in plan.allocations:
        body = store.read_evidence(
            "recovery_checkpoints", f"entry-target:{plan_hash}:{allocation.allocation_id}"
        )
        if body is not None:
            result.append(EntryTarget.model_validate_json(body))
    return tuple(result)


def append_entry_target(store: CriticalStorePort, target: EntryTarget) -> bool:
    target = EntryTarget.model_validate_json(target.model_dump_json())
    return store.append_evidence(
        "recovery_checkpoints", target.key, canonical_bytes(target.model_dump(mode="json"))
    )
