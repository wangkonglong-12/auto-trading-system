"""Conservative order/account recovery, independent of network and disk adapters.

This first composition requires every journal order to be explained by terminal
broker evidence. An empty query never proves an uncertain order was not sent.
External trades, historical gaps and holdings without known ownership block
authorization; later position-lot/adjustment workflows must resolve them explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from auto_trading_system.domain import Money, Price
from auto_trading_system.gateways.broker.models import BrokerOrder, BrokerOrderStatus, BrokerTrade
from auto_trading_system.gateways.broker.query_validation import (
    validate_order_query_result,
    validate_trade_query_result,
)
from auto_trading_system.journal.critical_models import RecoveryRecord
from auto_trading_system.protocol.v2.base import Sha256, WireId, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.records import MoneyText, Symbol
from auto_trading_system.reconcile.position_lots import PositionLot
from auto_trading_system.runtime.memory_state import AccountObservation


class PositionFact(WireModel):
    """Broker-observed aggregate holding; sellable shares are not a T+1 lot ledger."""

    symbol: Symbol
    quantity: int = Field(ge=0)
    sellable: int = Field(ge=0)
    frozen: int | None = Field(default=None, ge=0, description="券商明确返回的冻结股数，缺失为空")
    market_value: MoneyText | None = Field(default=None, description="券商持仓市值，缺失为空")
    cost_amount: MoneyText | None = Field(default=None, description="券商持仓成本，缺失为空")

    @model_validator(mode="after")
    def bounded(self) -> PositionFact:
        if self.sellable > self.quantity:
            raise ValueError("sellable exceeds holding")
        if self.frozen is not None and self.sellable + self.frozen > self.quantity:
            raise ValueError("sellable and frozen exceed holding")
        return self


class AccountFact(WireModel):
    """Independent broker query used as a conservative reservation baseline.

    cash is currently available cash, not NAV. observed_order_ids contains local
    client IDs already reflected in that cash observation, preventing duplicate
    subtraction of broker-frozen cash. coherent means the adapter actually
    established that relationship; it must never default missing data to zero.
    """

    account_id: WireId
    snapshot_id: WireId
    observed_at: AwareDatetime
    cash: MoneyText
    positions: tuple[PositionFact, ...] = Field(max_length=500)
    observed_order_ids: tuple[WireId, ...] = Field(max_length=2048)
    coherent: bool
    cash_frozen: MoneyText | None = Field(default=None, description="券商冻结现金，缺失为空")
    market_value: MoneyText | None = Field(default=None, description="券商账户证券市值，缺失为空")
    total_equity: MoneyText | None = Field(default=None, description="券商账户总资产，缺失为空")

    @model_validator(mode="after")
    def unique(self) -> AccountFact:
        if len({p.symbol for p in self.positions}) != len(self.positions):
            raise ValueError("duplicate account position")
        if len(set(self.observed_order_ids)) != len(self.observed_order_ids):
            raise ValueError("duplicate observed order")
        return self

    def observation(self) -> AccountObservation:
        return AccountObservation(
            self.snapshot_id,
            Money.parse(self.cash),
            frozenset(self.observed_order_ids),
            tuple((p.symbol, p.sellable) for p in self.positions),
            self.coherent,
        )


@dataclass(frozen=True)
class BrokerRecoverySnapshot:
    orders: tuple[BrokerOrder, ...]
    trades: tuple[BrokerTrade, ...]
    account: AccountFact
    event_version: int
    stable: bool


class RecoveryReport(WireModel):
    report_id: WireId
    account_id: WireId
    engine_boot_id: WireId
    trade_date: date
    observed_at: AwareDatetime
    event_version: int = Field(ge=0)
    submission_count: int = Field(ge=0, le=2048)
    submission_high_watermark: int = Field(ge=0)
    evidence_hash: Sha256
    status: Literal["CLEAR", "BLOCKED"]
    issue_codes: tuple[WireId, ...]
    unresolved_count: int = Field(ge=0)
    unresolved_sample: tuple[WireId, ...] = Field(max_length=32)
    reserved_buy_value: MoneyText

    @property
    def payload_hash(self) -> str:
        return digest(canonical_bytes(self.model_dump(mode="json")))


def reconcile(
    records: tuple[RecoveryRecord, ...],
    snapshot: BrokerRecoverySnapshot,
    *,
    account_id: str,
    boot_id: str,
    trade_date: date,
    now: datetime,
    report_id: str,
    opening: tuple[PositionLot, ...] = (),
    opening_hash: str | None = None,
) -> RecoveryReport:
    """No mutation and no assumption that SDK acceptance means an order is final."""
    issues: set[str] = set()
    unresolved: set[str] = set()
    if len(records) > 2048 or len(snapshot.orders) > 2048 or len(snapshot.trades) > 10000:
        raise ValueError("recovery input budget exceeded")
    # The pure reconciler is also a public boundary: never silently collapse
    # duplicate order IDs or count a duplicated fill twice.
    validate_order_query_result(snapshot.orders)
    validate_trade_query_result(snapshot.trades)
    if len({r.submission.client_order_id for r in records}) != len(records) or any(
        left.record_seq >= right.record_seq for left, right in pairwise(records)
    ):
        raise ValueError("recovery journal identities or sequence are invalid")
    account = snapshot.account
    if account.account_id != account_id:
        raise ValueError("recovery account mismatch")
    if not snapshot.stable or not account.coherent:
        issues.add("OBSERVATION_CROSSED_EVENT")
    if not 0 <= (now - account.observed_at).total_seconds() <= 5:
        issues.add("ACCOUNT_OBSERVATION_STALE")
    originals = {
        r.submission.client_order_id: r.submission
        for r in records
        if r.submission.action_type == "NEW"
    }
    orders = {o.client_order_id: o for o in snapshot.orders}
    if set(account.observed_order_ids) != set(orders):
        issues.add("ACCOUNT_ORDER_WINDOW_MISMATCH")
    trade_totals: dict[str, int] = {}
    positions: dict[str, int] = {}
    if opening and opening_hash is None:
        raise ValueError("opening lots require verified checkpoint evidence")
    for lot in opening:
        positions[lot.symbol] = positions.get(lot.symbol, 0) + lot.remaining
    for trade in snapshot.trades:
        key = trade.client_order_id
        original, order = originals.get(key), orders.get(key)
        if original is None or order is None:
            issues.add("UNOWNED_BROKER_TRADE")
            unresolved.add(key)
            continue
        if (trade.broker_order_id, str(trade.symbol), trade.side.value) != (
            order.broker_order_id,
            original.symbol,
            original.side,
        ) or trade.traded_at.value.date() != trade_date:
            issues.add("BROKER_TRADE_IDENTITY_CONFLICT")
            unresolved.add(key)
        trade_totals[key] = trade_totals.get(key, 0) + trade.quantity.value
        if trade.notional.amount != (trade.price.amount * trade.quantity.value).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ):
            issues.add("TRADE_AMOUNT_MISMATCH")
            unresolved.add(key)
        positions[original.symbol] = positions.get(original.symbol, 0) + (
            trade.quantity.value if original.side == "BUY" else -trade.quantity.value
        )
    for record in records:
        item = record.submission
        original_key = (
            item.client_order_id if item.action_type == "NEW" else item.original_client_order_id
        )
        assert original_key is not None
        key = original_key
        order = orders.get(key)
        if item.account_id != account_id or item.trade_date != trade_date:
            issues.add("HISTORICAL_OR_ACCOUNT_SCOPE_UNRESOLVED")
            unresolved.add(item.client_order_id)
        if order is None:
            issues.add("ORDER_NOT_OBSERVED_IS_NOT_PROOF")
            unresolved.add(item.client_order_id)
            continue
        if (str(order.symbol), order.side.value, order.order_quantity.value, order.limit_price) != (
            item.symbol,
            item.side,
            item.quantity,
            Price.parse(item.limit_price),
        ):
            issues.add("ORDER_IDENTITY_CONFLICT")
            unresolved.add(item.client_order_id)
        if item.action_type == "CANCEL" and order.broker_order_id != item.original_order_id:
            issues.add("CANCEL_TARGET_CONFLICT")
            unresolved.add(item.client_order_id)
        final_consistent = (
            order.is_terminal
            and trade_totals.get(key, 0) == order.filled_quantity.value
            and (order.status != BrokerOrderStatus.FILLED or order.remaining_quantity.is_zero)
            and (
                order.status not in (BrokerOrderStatus.CANCELLED, BrokerOrderStatus.REJECTED)
                or order.filled_quantity.is_zero
            )
            and (
                order.status != BrokerOrderStatus.PARTIALLY_FILLED_CANCELLED
                or (not order.filled_quantity.is_zero and not order.remaining_quantity.is_zero)
            )
        )
        if not final_consistent:
            issues.add("ORDER_ACTIVE_OR_TRADE_MISMATCH")
            unresolved.add(item.client_order_id)
    for key in orders.keys() - originals.keys():
        issues.add("UNOWNED_BROKER_ORDER")
        unresolved.add(key)
    actual_positions = {p.symbol: p.quantity for p in account.positions if p.quantity}
    if {key: value for key, value in positions.items() if value} != actual_positions:
        issues.add("POSITION_OWNERSHIP_UNRESOLVED")
    # Reserve the full maximum of each attempted NEW BUY, even if cancelled.
    # This conservative ceiling survives restarts without inventing fee refunds.
    reserved = sum(
        (
            Decimal(s.maximum_amount)
            for s in originals.values()
            if s.side == "BUY" and s.trade_date == trade_date
        ),
        Decimal(0),
    )
    evidence = recovery_evidence(records, snapshot, opening_hash=opening_hash)
    return RecoveryReport(
        report_id=report_id,
        account_id=account_id,
        engine_boot_id=boot_id,
        trade_date=trade_date,
        observed_at=now,
        event_version=snapshot.event_version,
        submission_count=len(records),
        submission_high_watermark=records[-1].record_seq if records else 0,
        evidence_hash=digest(canonical_bytes(evidence)),
        status="BLOCKED" if issues else "CLEAR",
        issue_codes=tuple(sorted(issues)),
        unresolved_count=len(unresolved),
        unresolved_sample=tuple(sorted(unresolved)[:32]),
        reserved_buy_value=f"{reserved:.2f}",
    )


def recovery_evidence(
    records: tuple[RecoveryRecord, ...],
    snapshot: BrokerRecoverySnapshot,
    *,
    opening_hash: str | None = None,
) -> dict[str, JsonValue]:
    """Replayable query evidence, retained with the critical recovery checkpoint."""
    evidence: dict[str, JsonValue] = {
        "journal": [r.model_dump(mode="json") for r in records],
        "account": snapshot.account.model_dump(mode="json"),
        "orders": [
            [
                o.client_order_id,
                o.broker_order_id,
                str(o.symbol),
                o.side.value,
                o.status.value,
                o.order_quantity.value,
                o.filled_quantity.value,
                str(o.limit_price),
                o.updated_at.value.isoformat(),
            ]
            for o in snapshot.orders
        ],
        "trades": [
            [
                t.trade_id,
                t.client_order_id,
                t.broker_order_id,
                str(t.symbol),
                t.side.value,
                t.quantity.value,
                str(t.price),
                str(t.notional),
                t.traded_at.value.isoformat(),
            ]
            for t in snapshot.trades
        ],
    }
    if opening_hash is not None:
        evidence["opening_hash"] = opening_hash
    return evidence
