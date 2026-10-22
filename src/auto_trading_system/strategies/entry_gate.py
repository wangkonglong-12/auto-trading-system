"""Entry quote, rule and persisted-target guard, checked again before SDK dispatch."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from threading import get_ident
from time import monotonic_ns

from pydantic import AwareDatetime, Field, model_validator

from auto_trading_system.journal.critical_models import ActionTarget, EntryBinding, Submission
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanPayload
from auto_trading_system.protocol.v2.records import PriceText, Symbol
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.strategies.entry_state import (
    EntryInstrument,
    EntryPolicy,
    EntryTarget,
    validate_policy,
)
from auto_trading_system.strategies.market_session import market_open


class EntryQuote(WireModel):
    symbol: Symbol = Field(description="标准化证券代码")
    sequence: int = Field(ge=1, description="当前进程行情通道的严格递增序号")
    observed_at: AwareDatetime = Field(description="行情源时间，UTC+08:00")
    received_ns: int = Field(ge=0, description="本进程接收行情的单调时钟纳秒值")
    bid_price: PriceText = Field(description="买一价，元")
    ask_price: PriceText = Field(description="卖一价，元")
    ask_quantity: int = Field(ge=0, le=2**63 - 1, description="已标准化卖一股数")
    recent_volume: int = Field(
        ge=0, le=2**63 - 1, description="经行情适配器核验的最近60秒新增成交股数"
    )
    cage_upper: PriceText = Field(description="行情适配器按有效规则计算的当前申报价上界，元")
    path_complete: bool = Field(description="最近60秒量差的连续性和计量单位均已核验")
    tradable: bool = Field(description="当前交易时段和证券状态允许买入")

    @model_validator(mode="after")
    def valid(self) -> EntryQuote:
        if (
            min(Decimal(self.ask_price), Decimal(self.bid_price), Decimal(self.cage_upper)) <= 0
            or Decimal(self.bid_price) > Decimal(self.ask_price)
            or self.observed_at.utcoffset() != timedelta(hours=8)
        ):
            raise ValueError("invalid entry quote")
        return self


def legal_quantity(raw: int, rule: EntryInstrument) -> int:
    if raw < rule.min_quantity:
        return 0
    return rule.min_quantity + (raw - rule.min_quantity) // rule.quantity_step * rule.quantity_step


class EntryStateGate:
    def __init__(
        self,
        plan: PlanPayload,
        policy: EntryPolicy,
        *,
        clock: Callable[[], datetime],
        ns_clock: Callable[[], int] = monotonic_ns,
        require_policy_payload_hash: bool = True,
    ) -> None:
        self.plan = PlanPayload.model_validate_json(plan.model_dump_json())
        self.policy = EntryPolicy.model_validate_json(policy.model_dump_json())
        validate_policy(
            self.plan,
            self.policy,
            require_payload_hash=require_policy_payload_hash,
        )
        self.require_policy_payload_hash = require_policy_payload_hash
        self.plan_hash = digest(canonical_bytes(self.plan.model_dump(mode="json")))
        self.clock, self.ns_clock, self.owner = clock, ns_clock, get_ident()
        self.rules = {i.symbol: i for i in self.policy.instruments}
        self.targets: dict[str, EntryTarget] = {}
        self.quotes: dict[str, EntryQuote] = {}
        self.used_quotes: dict[str, tuple[int, str]] = {}
        self.ready = False
        self.blocked = False
        self.engine: EngineKernel | None = None

    def owner_only(self) -> None:
        if get_ident() != self.owner:
            raise RuntimeError("entry strategy has a single owner")

    def export_targets(self) -> tuple[EntryTarget, ...]:
        """Called by engine capture only after restore and all target commits settle."""
        self.owner_only()
        if not self.ready or self.blocked:
            raise ValueError("entry targets require settled durable state")
        return tuple(self.targets[k] for k in sorted(self.targets))

    def price(self, quote: EntryQuote) -> Decimal | None:
        now, rule = self.clock(), self.rules[quote.symbol]
        age_ms = (now - quote.observed_at).total_seconds() * 1000
        ns_age = self.ns_clock() - quote.received_ns
        price = (Decimal(quote.ask_price) / Decimal(rule.price_tick)).to_integral_value(
            rounding=ROUND_CEILING
        ) * Decimal(rule.price_tick)
        ceiling = min(
            Decimal(rule.upper_price),
            Decimal(quote.cage_upper),
            Decimal(rule.reference_price) * (10000 + self.policy.max_chase_bps) / 10000,
        )
        if (
            now.date() != self.plan.trade_date
            or not market_open(now)
            or not quote.tradable
            or not quote.path_complete
            or not 0 <= age_ms <= self.policy.quote_max_age_ms
            or not 0 <= ns_age <= self.policy.quote_max_age_ms * 1_000_000
            or not Decimal(rule.lower_price) <= price <= ceiling
            or (Decimal(quote.ask_price) - Decimal(quote.bid_price)) * 10000
            > Decimal(quote.bid_price) * self.policy.max_spread_bps
        ):
            return None
        return price

    def allows(self, item: Submission) -> bool:
        self.owner_only()
        engine, binding = self.engine, item.entry_binding
        if not self.ready or self.blocked or engine is None or binding is None:
            return False
        target, quote = self.targets.get(item.symbol), self.quotes.get(item.symbol)
        if target is None or quote is None or target.target_quantity == 0:
            return False
        price = self.price(quote)
        rule = self.rules[item.symbol]
        _, _, known = engine.exit_inputs()
        children = tuple(
            s
            for s in known
            if s.action_type == "NEW"
            and s.side == "BUY"
            and s.symbol == item.symbol
            and s.client_order_id != item.client_order_id
        )
        # Day-to-window conservative participation budget: attempted shares are
        # never credited back, even after cancellation or restart. Rolling
        # timestamped liquidity attribution can later relax this explicit bound.
        volume_limit = quote.recent_volume * self.policy.participation_bps // 10000
        used = self.used_quotes.get(item.symbol)
        allowed = bool(
            price is not None
            and Decimal(item.limit_price) == price
            and item.plan_hash == self.plan_hash
            and item.side == "BUY"
            and binding
            == EntryBinding(
                plan_id=self.plan.plan_id,
                allocation_id=target.allocation_id,
                target_quantity=target.target_quantity,
                state_hash=target.payload_hash,
            )
            and item.target
            == ActionTarget(
                action_id="entry-" + target.payload_hash[7:],
                target_remaining=target.target_quantity,
                rule_id=self.policy.version,
            )
            and item.quantity == legal_quantity(item.quantity, rule)
            and item.quantity <= rule.max_order_quantity
            and item.quantity + sum(s.quantity for s in children) <= volume_limit
            and item.quantity <= quote.ask_quantity * self.policy.depth_bps // 10000
            and Decimal(item.maximum_amount)
            == price * item.quantity + Decimal(self.policy.fee_reserve)
            and len(children) < self.policy.max_children
            and (used is None or used[0] != quote.sequence or used[1] == item.client_order_id)
            and sum(
                v.phase != "TERMINAL_CONFIRMED" and v.client_order_id != item.client_order_id
                for v in engine.snapshot().orders
            )
            < self.policy.max_active_orders
        )
        if allowed:
            # Admission consumes this sequence even if a later guard rejects.
            # A same-price update during J1 may pass fresh capacity checks, but
            # it then belongs to this child too and cannot fund the next child.
            self.used_quotes[item.symbol] = (quote.sequence, item.client_order_id)
        return allowed
