"""Deterministic, bounded lot projection and T+1 sell-reservation arithmetic.

This is a local risk projection, not the central financial ledger. A caller must
provide a trusted, versioned exchange calendar and pre-bound sell allocations.
Unknown/manual trades produce symbol locks rather than invented strategy lots.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pydantic import Field, model_validator

from auto_trading_system.gateways.broker.models import BrokerTrade
from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.orders.projection import OrderProjection, trade_key
from auto_trading_system.protocol.v2.base import WireId, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.records import PriceText, Shares, Symbol


class ExchangeCalendar(WireModel):
    version: WireId = Field(description="已验证交易日日历版本，必须覆盖买入日至下一交易日")
    sessions: tuple[date, ...] = Field(
        min_length=2, max_length=4000, description="完整覆盖区间内的升序交易日"
    )

    @model_validator(mode="after")
    def ordered(self) -> ExchangeCalendar:
        if tuple(sorted(set(self.sessions))) != self.sessions:
            raise ValueError("calendar sessions must be unique and ordered")
        return self

    def next_session(self, acquired: date) -> date:
        if acquired not in self.sessions or acquired == self.sessions[-1]:
            raise ValueError("calendar does not cover acquisition and next session")
        return self.sessions[self.sessions.index(acquired) + 1]


class PositionLot(WireModel):
    lot_id: WireId = Field(description="账户、原买入子单绑定的稳定批次编号")
    strategy_id: WireId = Field(description="唯一责任策略")
    symbol: Symbol = Field(description="证券代码及市场")
    trade_date_acquired: date = Field(description="该批次实际买入交易日")
    eligible_sell_date: date = Field(description="由已验证交易日日历计算的最早可卖交易日")
    calendar_version: WireId = Field(description="可卖日期依据的日历版本")
    bought_quantity: Shares = Field(description="累计真实买入股数")
    cost_price: PriceText | None = Field(
        default=None, description="该批次真实买入成交的加权平均单位成本；旧检查点可为空"
    )
    sold_quantity: Shares = Field(default=0, description="分配到该批次的累计真实卖出股数")
    reserved_quantity: Shares = Field(
        default=0, description="该批次尚未终结卖出意图的未成交预占股数"
    )

    @model_validator(mode="after")
    def conserved(self) -> PositionLot:
        if self.eligible_sell_date <= self.trade_date_acquired:
            raise ValueError("T+1 eligibility must follow acquisition")
        if self.sold_quantity + self.reserved_quantity > self.bought_quantity:
            raise ValueError("lot sold and reserved shares exceed bought quantity")
        return self

    @property
    def remaining(self) -> int:
        return self.bought_quantity - self.sold_quantity


class LotAllocation(WireModel):
    lot_id: WireId = Field(description="报单前冻结的卖出来源批次")
    quantity: int = Field(gt=0, le=2**31 - 1, description="本子单从该批次最多卖出股数")


@dataclass(frozen=True)
class LotIntent:
    submission: Submission
    strategy_id: str
    sell_allocations: tuple[LotAllocation, ...] = ()


@dataclass(frozen=True)
class SellReservation:
    client_order_id: str
    symbol: str
    remaining: int


@dataclass(frozen=True)
class PortfolioProjection:
    lots: tuple[PositionLot, ...]
    locked_symbols: frozenset[str]
    unowned_trade_ids: tuple[str, ...]
    sell_reservations: tuple[SellReservation, ...]
    unmanaged_positions: tuple[tuple[str, int], ...] = ()

    def quantities(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for lot in self.lots:
            result[lot.symbol] = result.get(lot.symbol, 0) + lot.remaining
        for symbol, quantity in self.unmanaged_positions:
            result[symbol] = result.get(symbol, 0) + quantity
        return {symbol: quantity for symbol, quantity in result.items() if quantity}

    def sellable(
        self,
        *,
        symbol: str,
        strategy_id: str,
        as_of: date,
        broker_sellable: int,
        reflected_order_ids: frozenset[str],
    ) -> int:
        if type(broker_sellable) is not int or broker_sellable < 0:
            raise ValueError("broker sellable must be confirmed nonnegative shares")
        if symbol in self.locked_symbols:
            return 0
        owned = sum(
            lot.remaining - lot.reserved_quantity
            for lot in self.lots
            if lot.symbol == symbol
            and lot.strategy_id == strategy_id
            and lot.eligible_sell_date <= as_of
        )
        unreflected = sum(
            r.remaining
            for r in self.sell_reservations
            if r.symbol == symbol and r.client_order_id not in reflected_order_ids
        )
        return min(owned, max(0, broker_sellable - unreflected))


def lot_id(submission: Submission) -> str:
    return "lot-" + digest(canonical_bytes([submission.account_id, submission.client_order_id]))[7:]


def project_lots(
    *,
    account_id: str,
    as_of: date,
    calendar: ExchangeCalendar,
    intents: tuple[LotIntent, ...],
    trades: tuple[BrokerTrade, ...],
    terminal_order_ids: frozenset[str] = frozenset(),
    opening: tuple[PositionLot, ...] = (),
    unmanaged_positions: tuple[tuple[str, int], ...] = (),
) -> PortfolioProjection:
    """Replay actual fills; unknown attribution stays locked and known sells conserve shares.

    opening comes from the engine's same-Journal prefix-verified checkpoint.
    A provided J1 binding must agree with the replay adapter's allocations;
    the adapter cannot silently reassign a fill to a different strategy/lot.
    """
    if len(intents) > 2048 or len(trades) > 10000 or len(opening) > 2048:
        raise ValueError("portfolio projection capacity exceeded")
    if as_of not in calendar.sessions:
        raise ValueError("portfolio date absent from verified calendar")
    known: dict[str, LotIntent] = {}
    projection = OrderProjection()
    for intent in intents:
        item = intent.submission
        if (
            item.action_type != "NEW"
            or item.account_id != account_id
            or item.trade_date > as_of
            or not intent.strategy_id
        ):
            raise ValueError("lot intent scope invalid")
        if item.client_order_id in known:
            raise ValueError("duplicate lot intent")
        known[item.client_order_id] = intent
        projection.register(item)
        allocations = intent.sell_allocations
        if item.sell_binding is not None and (
            item.sell_binding.strategy_id != intent.strategy_id
            or tuple((a.lot_id, a.quantity) for a in item.sell_binding.allocations)
            != tuple((a.lot_id, a.quantity) for a in allocations)
        ):
            raise ValueError("projection differs from durable sell allocation")
        if item.side == "SELL":
            if sum(a.quantity for a in allocations) != item.quantity or len(
                {a.lot_id for a in allocations}
            ) != len(allocations):
                raise ValueError("sell allocations must bind every submitted share once")
        elif allocations:
            raise ValueError("buy cannot carry sell allocations")
    lots: dict[str, PositionLot] = {}
    for lot in opening:
        lot = PositionLot.model_validate_json(lot.model_dump_json())
        if lot.lot_id in lots or lot.trade_date_acquired >= as_of or lot.reserved_quantity:
            raise ValueError("opening lots require unique settled prior-day ownership")
        if (
            lot.calendar_version != calendar.version
            or calendar.next_session(lot.trade_date_acquired) != lot.eligible_sell_date
        ):
            raise ValueError("opening lot calendar evidence mismatch")
        lots[lot.lot_id] = lot
    if len({symbol for symbol, _ in unmanaged_positions}) != len(unmanaged_positions) or any(
        quantity <= 0 for _, quantity in unmanaged_positions
    ):
        raise ValueError("unmanaged position baseline is invalid")
    locked: set[str] = {symbol for symbol, _ in unmanaged_positions}
    unowned: list[str] = []
    quantities: dict[str, int] = {}
    notionals: dict[str, Decimal] = {}
    # Arrival order has no accounting meaning. De-duplicate before allocating.
    unique: dict[tuple[str, str, str], BrokerTrade] = {}
    for trade in trades:
        if trade.traded_at.value.date() > as_of:
            raise ValueError("future trade in portfolio")
        projection.observe_trade(trade)
        fill_key = trade_key(trade)
        if fill_key in unique:
            continue
        unique[fill_key] = trade
        if trade.client_order_id not in known:
            locked.add(str(trade.symbol))
            unowned.append(trade.trade_id)
            continue
        quantities[trade.client_order_id] = (
            quantities.get(trade.client_order_id, 0) + trade.quantity.value
        )
        if trade.side.value == "BUY":
            notionals[trade.client_order_id] = (
                notionals.get(trade.client_order_id, Decimal(0))
                + trade.price.amount * trade.quantity.value
            )
    # Build buy lots before distributing sell fills across their frozen sources.
    for key, intent in known.items():
        item = intent.submission
        if item.side != "BUY" or not quantities.get(key):
            continue
        identifier = lot_id(item)
        if identifier in lots:
            raise ValueError("opening lot overlaps replayed buy")
        lots[identifier] = PositionLot(
            lot_id=identifier,
            strategy_id=intent.strategy_id,
            symbol=item.symbol,
            trade_date_acquired=item.trade_date,
            eligible_sell_date=calendar.next_session(item.trade_date),
            calendar_version=calendar.version,
            bought_quantity=quantities[key],
            cost_price=format(notionals[key] / quantities[key], "f"),
        )
    reservations: list[SellReservation] = []
    for key, intent in known.items():
        item = intent.submission
        if item.side != "SELL":
            continue
        filled = quantities.get(key, 0)
        for allocation in intent.sell_allocations:
            allocated_lot = lots.get(allocation.lot_id)
            if (
                allocated_lot is None
                or (allocated_lot.symbol, allocated_lot.strategy_id)
                != (item.symbol, intent.strategy_id)
                or item.trade_date < allocated_lot.eligible_sell_date
            ):
                raise ValueError("sell allocation lacks eligible strategy ownership")
            lot = allocated_lot
            sold = min(filled, allocation.quantity)
            filled -= sold
            reserved = 0 if key in terminal_order_ids else allocation.quantity - sold
            # Re-validate, rather than using unchecked model_copy for arithmetic.
            lots[lot.lot_id] = PositionLot(
                **{
                    **lot.model_dump(),
                    "sold_quantity": lot.sold_quantity + sold,
                    "reserved_quantity": lot.reserved_quantity + reserved,
                }
            )
        if key not in terminal_order_ids:
            reservations.append(
                SellReservation(key, item.symbol, item.quantity - quantities.get(key, 0))
            )
    owners: dict[str, str] = {}
    for lot in lots.values():
        if lot.remaining and owners.setdefault(lot.symbol, lot.strategy_id) != lot.strategy_id:
            raise ValueError("one symbol has multiple active strategy owners")
    if len(lots) > 2048:
        raise ValueError("lot projection capacity exceeded")
    return PortfolioProjection(
        tuple(sorted(lots.values(), key=lambda lot: lot.lot_id)),
        frozenset(locked),
        tuple(sorted(unowned)),
        tuple(reservations),
        tuple(sorted(unmanaged_positions)),
    )


def assert_position_match(portfolio: PortfolioProjection, observed: Mapping[str, int]) -> None:
    if portfolio.locked_symbols or portfolio.quantities() != {
        symbol: qty for symbol, qty in observed.items() if qty
    }:
        raise ValueError("account holdings or external trades have unresolved ownership")
