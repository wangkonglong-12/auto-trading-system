"""Rebuild an opening from immutable J1/fills; never fabricate a missed close."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby

from pydantic import JsonValue

from auto_trading_system.domain import Money, Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.broker.models import BrokerTrade, OrderSide
from auto_trading_system.journal.checkpoint_blobs import read_checkpoint
from auto_trading_system.journal.critical_models import RecoveryRecord
from auto_trading_system.journal.critical_writer import CriticalStorePort
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.records import BrokerOrderRecord, BrokerTradeRecord
from auto_trading_system.reconcile.journal_scan import JournalScan, PrefixDigest, RecoveredBaseline
from auto_trading_system.reconcile.portfolio_checkpoint import (
    OpeningReference,
    PortfolioCheckpoint,
    load_opening,
)
from auto_trading_system.reconcile.position_lots import (
    ExchangeCalendar,
    LotAllocation,
    LotIntent,
    project_lots,
)


@dataclass(frozen=True)
class LoadedOpening:
    baseline: PortfolioCheckpoint | RecoveredBaseline | None
    proof: bytes | None = None
    error: str | None = None


def _facts(
    store: CriticalStorePort, records: list[RecoveryRecord]
) -> tuple[dict[str, BrokerOrderRecord], tuple[BrokerTrade, ...]]:
    originals = {
        r.submission.client_order_id: r.submission
        for r in records
        if r.submission.action_type == "NEW"
    }
    terminals: dict[str, dict[bytes, BrokerOrderRecord]] = {}
    trades: dict[tuple[date, str, str], BrokerTrade] = {}
    for body in store.read_broker_facts(tuple(originals)):
        data = json.loads(body)["data"]
        key = data["client_order_id"]
        original = originals.get(key)
        if original is None:
            raise ValueError("RECOVERY_HISTORY_FACT_CONFLICT")
        if (data["account_id"], data["trade_date"], data["symbol"], data["side"]) != (
            original.account_id,
            original.trade_date.isoformat(),
            original.symbol,
            original.side,
        ):
            raise ValueError("RECOVERY_HISTORY_FACT_CONFLICT")
        if "broker_trade_id" in data:
            trade_row = BrokerTradeRecord.model_validate_json(canonical_bytes(data))
            trade = BrokerTrade(
                trade_row.broker_trade_id,
                key,
                trade_row.broker_order_id,
                SecurityCode.parse(trade_row.symbol),
                OrderSide(trade_row.side),
                Price.parse(trade_row.price),
                Quantity(trade_row.quantity),
                Money.parse(trade_row.gross_amount),
                ShanghaiTimestamp(trade_row.traded_at),
            )
            if trade.notional.amount != (trade.price.amount * trade.quantity.value).quantize(
                Decimal(".01"), rounding=ROUND_HALF_UP
            ):
                raise ValueError("RECOVERY_HISTORY_FACT_CONFLICT")
            trade_key = (trade_row.trade_date, trade_row.market, trade.trade_id)
            previous = trades.setdefault(trade_key, trade)
            if previous != trade:
                raise ValueError("RECOVERY_HISTORY_FACT_CONFLICT")
        else:
            row = BrokerOrderRecord.model_validate_json(canonical_bytes(data))
            if row.quantity != original.quantity or Decimal(row.limit_price) != Decimal(
                original.limit_price
            ):
                raise ValueError("RECOVERY_HISTORY_FACT_CONFLICT")
            if row.status in {"FILLED", "CANCELLED", "REJECTED"}:
                financial = row.model_dump(
                    mode="json", exclude={"observed_at", "raw_status", "remark_token"}
                )
                terminals.setdefault(key, {})[canonical_bytes(financial)] = row
    orders: dict[str, BrokerOrderRecord] = {}
    for key in originals:
        choices = terminals.get(key, {})
        if len(choices) != 1:
            raise ValueError(
                "RECOVERY_HISTORY_EVIDENCE_INCOMPLETE"
                if not choices
                else "RECOVERY_HISTORY_FACT_CONFLICT"
            )
        row = next(iter(choices.values()))
        fills = [t for t in trades.values() if t.client_order_id == key]
        if (
            any(t.broker_order_id != row.broker_order_id for t in fills)
            or sum(t.quantity.value for t in fills) != row.filled_quantity
            or (row.status == "FILLED" and row.filled_quantity != row.quantity)
            or (row.status == "REJECTED" and row.filled_quantity != 0)
            or (
                row.status == "CANCELLED"
                and row.filled_quantity + row.cancelled_quantity != row.quantity
            )
        ):
            raise ValueError("RECOVERY_HISTORY_EVIDENCE_INCOMPLETE")
        orders[key] = row
    for record in records:
        item = record.submission
        if item.action_type == "CANCEL":
            original = originals.get(item.original_client_order_id or "")
            order = orders.get(item.original_client_order_id or "")
            if (
                original is None
                or order is None
                or item.original_order_id != order.broker_order_id
                or (
                    item.trade_date,
                    item.symbol,
                    item.side,
                    item.quantity,
                    Decimal(item.limit_price),
                )
                != (
                    original.trade_date,
                    original.symbol,
                    original.side,
                    original.quantity,
                    Decimal(original.limit_price),
                )
            ):
                raise ValueError("RECOVERY_HISTORY_FACT_CONFLICT")
    return orders, tuple(trades[key] for key in sorted(trades))


def _load_recovery_opening(
    store: CriticalStorePort,
    reference: OpeningReference | None,
    *,
    account_id: str,
    today: date,
    calendar: ExchangeCalendar,
) -> LoadedOpening:
    checkpoint = None
    if reference is not None:
        checkpoint = load_opening(
            read_checkpoint(store, reference.key),
            reference,
            account_id=account_id,
            trade_date=today,
            calendar=calendar,
            allow_historical=True,
        )
    scan = JournalScan(
        account_id=account_id, trade_date=today, checkpoint=checkpoint, allow_historical=True
    )
    prefix = PrefixDigest()
    after = 0
    last_day = None
    while True:
        page = store.read_submissions(after, 200)
        if page:
            scan.consume(page)
            for record in page:
                if last_day is not None and record.submission.trade_date < last_day:
                    raise ValueError("RECOVERY_HISTORY_SCOPE_UNRESOLVED")
                last_day = record.submission.trade_date
                if (
                    checkpoint is not None
                    and record.record_seq <= checkpoint.submission_high_watermark
                ):
                    prefix.add(record)
            after = page[-1].record_seq
        if len(page) < 200:
            break
    scan.finish()
    if not scan.historical and (
        checkpoint is None
        or not any(checkpoint.trade_date < day < today for day in calendar.sessions)
    ):
        return LoadedOpening(checkpoint)
    lots = checkpoint.lots if checkpoint else ()
    orders, trades = _facts(store, scan.historical) if scan.historical else ({}, ())
    source = reference.model_dump(mode="json") if reference else None
    historic: list[JsonValue] = []
    financial_orders: list[JsonValue] = []
    financial_trades: list[JsonValue] = []

    def proof(day: date) -> bytes:
        return canonical_bytes(
            {
                "kind": "RECOVERED_OPENING_V1",
                "account_id": account_id,
                "trade_date": day.isoformat(),
                "source": source,
                "submission_high_watermark": prefix.high_watermark,
                "submission_prefix_hash": prefix.payload_hash,
                "historical_journal": historic,
                "orders": financial_orders,
                "trades": financial_trades,
                "lots": [lot.model_dump(mode="json") for lot in lots],
            }
        )

    for day, group in groupby(scan.historical, key=lambda r: r.submission.trade_date):
        rows = tuple(group)
        opening_hashes = {digest(proof(day))}
        if reference is not None:
            opening_hashes.add(reference.payload_hash)
        intents = []
        keys = set()
        for record in rows:
            item = record.submission
            prefix.add(record)
            historic.append([record.record_seq, record.payload_hash])
            if item.action_type != "NEW":
                continue
            keys.add(item.client_order_id)
            allocations: tuple[LotAllocation, ...] = ()
            if item.side == "SELL":
                binding = item.sell_binding
                if binding is None or binding.opening_hash not in opening_hashes:
                    raise ValueError("RECOVERY_HISTORY_SCOPE_UNRESOLVED")
                strategy = binding.strategy_id
                allocations = tuple(
                    LotAllocation(lot_id=a.lot_id, quantity=a.quantity) for a in binding.allocations
                )
            elif item.manual_binding is not None:
                strategy = item.manual_binding.strategy_id
            elif item.entry_binding is not None and item.entry_binding.strategy_id:
                strategy = item.entry_binding.strategy_id
            else:
                raise ValueError("RECOVERY_HISTORY_SCOPE_UNRESOLVED")
            intents.append(LotIntent(item, strategy, allocations))
            financial_orders.append(
                orders[item.client_order_id].model_dump(
                    mode="json", exclude={"observed_at", "raw_status", "remark_token"}
                )
            )
        fills = tuple(t for t in trades if t.client_order_id in keys)
        financial_trades.extend(
            [
                [
                    t.trade_id,
                    t.client_order_id,
                    t.broker_order_id,
                    str(t.symbol),
                    t.side.value,
                    str(t.price),
                    t.quantity.value,
                    str(t.notional),
                    t.traded_at.value.isoformat(),
                ]
                for t in fills
            ]
        )
        projected = project_lots(
            account_id=account_id,
            as_of=day,
            calendar=calendar,
            intents=tuple(intents),
            trades=tuple(
                replace(
                    t,
                    trade_id="trade_"
                    + digest(canonical_bytes([day.isoformat(), t.symbol.market.value, t.trade_id]))[
                        7:
                    ],
                )
                for t in fills
            ),
            terminal_order_ids=frozenset(keys),
            opening=lots,
        )
        if projected.locked_symbols or projected.unowned_trade_ids or projected.sell_reservations:
            raise ValueError("RECOVERY_HISTORY_SCOPE_UNRESOLVED")
        lots = projected.lots
    prior = max(d for d in calendar.sessions if d < today)
    return LoadedOpening(
        RecoveredBaseline(prior, prefix.high_watermark, prefix.payload_hash, lots), proof(today)
    )


def load_recovery_opening(
    store: CriticalStorePort,
    reference: OpeningReference | None,
    *,
    account_id: str,
    today: date,
    calendar: ExchangeCalendar,
) -> LoadedOpening:
    # Evidence validation is a recoverable domain result, not a writer I/O fault.
    # Never let incomplete history poison the single Journal owner's health.
    try:
        return _load_recovery_opening(
            store, reference, account_id=account_id, today=today, calendar=calendar
        )
    except (ValueError, KeyError, TypeError) as error:
        code = str(error)
        allowed = {
            "RECOVERY_HISTORY_FACT_CONFLICT",
            "RECOVERY_HISTORY_EVIDENCE_INCOMPLETE",
            "RECOVERY_HISTORY_SCOPE_UNRESOLVED",
        }
        return LoadedOpening(
            None, error=code if code in allowed else "RECOVERY_OPENING_REFERENCE_FAILED"
        )
