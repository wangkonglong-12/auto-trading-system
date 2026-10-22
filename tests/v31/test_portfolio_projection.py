from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from typing import TypedDict

import pytest

from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW
from auto_trading_system.domain import Money, Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.broker.models import (
    BrokerOrder,
    BrokerOrderStatus,
    BrokerTrade,
    OrderSide,
)
from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.orders.projection import OrderProjection, merge_order
from auto_trading_system.reconcile.position_lots import (
    ExchangeCalendar,
    LotAllocation,
    LotIntent,
    PositionLot,
    assert_position_match,
    lot_id,
    project_lots,
)

S = BrokerOrderStatus


class PortfolioArgs(TypedDict):
    account_id: str
    as_of: date
    calendar: ExchangeCalendar
    intents: tuple[LotIntent, ...]
    trades: tuple[BrokerTrade, ...]


class SellScope(TypedDict):
    symbol: str
    strategy_id: str
    as_of: date


def order(item: Submission, *, status: BrokerOrderStatus = S.ACKED, filled: int = 0) -> BrokerOrder:
    return BrokerOrder(
        item.client_order_id,
        "broker-" + item.client_order_id,
        SecurityCode.parse(item.symbol),
        OrderSide(item.side),
        status,
        Quantity(item.quantity),
        Quantity(filled),
        Quantity(item.quantity - filled),
        Price.parse(item.limit_price),
        50,
        "TEST_FACT",
        ShanghaiTimestamp.from_instant(DEMO_NOW),
    )


def trade(
    item: Submission, *, quantity: int = 50, identifier: str = "trade_test001"
) -> BrokerTrade:
    return BrokerTrade(
        identifier,
        item.client_order_id,
        "broker-" + item.client_order_id,
        SecurityCode.parse(item.symbol),
        OrderSide(item.side),
        Price.parse(item.limit_price),
        Quantity(quantity),
        Money.parse(str(quantity * 10)),
        ShanghaiTimestamp.from_instant(
            DEMO_NOW.replace(
                year=item.trade_date.year, month=item.trade_date.month, day=item.trade_date.day
            )
        ),
    )


def calendar() -> ExchangeCalendar:
    # Explicit fixture contains a weekend and a multi-day closure; it is not a
    # claim about a live exchange calendar downloaded for this environment.
    return ExchangeCalendar(
        version="test-sessions",
        sessions=(date(2026, 9, 4), date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 14)),
    )


def test_fill_before_order_duplicate_and_terminal_wait_for_actual_trades(
    submission: Submission,
) -> None:
    state = OrderProjection()
    state.register(submission)
    assert state.views()[0].phase == "WAITING_ORDER"
    assert state.observe_trade(trade(submission))
    assert not state.observe_trade(trade(submission))
    assert state.views()[0].effective_filled_quantity == 50
    state.observe_order(replace(order(submission, status=S.SUBMITTING), broker_order_id=None))
    state.observe_order(order(submission, status=S.FILLED, filled=100))
    assert state.views()[0].phase == "WAITING_TRADES"
    state.observe_trade(trade(submission, identifier="trade_test002"))
    assert state.views()[0].phase == "TERMINAL_CONFIRMED"
    state.observe_order(order(submission))
    final_order = state.views()[0].order
    assert final_order and final_order.status == S.FILLED


@pytest.mark.parametrize(
    ("status", "filled"),
    [
        (S.SUBMITTING, 1),
        (S.ACKED, 50),
        (S.CANCEL_PENDING, 50),
        (S.PARTIALLY_FILLED, 0),
        (S.PARTIALLY_FILLED, 100),
        (S.PARTIAL_FILL_CANCEL_PENDING, 0),
        (S.PARTIALLY_FILLED_CANCELLED, 0),
        (S.PARTIALLY_FILLED_CANCELLED, 100),
        (S.FILLED, 50),
        (S.CANCELLED, 1),
        (S.REJECTED, 1),
    ],
)
def test_impossible_status_quantities_are_rejected(
    submission: Submission, status: BrokerOrderStatus, filled: int
) -> None:
    with pytest.raises(ValueError, match="status"):
        merge_order(None, order(submission, status=status, filled=filled))


def test_cancel_pending_and_uncertainty_never_regress_on_late_ack(submission: Submission) -> None:
    ack = order(submission)
    pending = replace(ack, status=S.CANCEL_PENDING)
    assert merge_order(pending, ack) == pending
    partial = order(submission, status=S.PARTIALLY_FILLED, filled=50)
    assert merge_order(partial, ack) == partial
    uncertain = merge_order(partial, replace(ack, status=S.UNKNOWN))
    assert uncertain.filled_quantity.value == 50 and uncertain.status == S.UNKNOWN
    assert merge_order(uncertain, partial) == uncertain
    with pytest.raises(ValueError, match="loses"):
        merge_order(partial, replace(ack, status=S.CANCELLED))
    terminal = replace(partial, status=S.PARTIALLY_FILLED_CANCELLED)
    with pytest.raises(ValueError, match="terminal"):
        merge_order(terminal, order(submission, status=S.FILLED, filled=100))


@pytest.mark.parametrize(
    "mutation", ["amount", "limit", "symbol", "broker", "overfill", "trade_reuse"]
)
def test_trade_conflicts_never_mutate_existing_projection(
    submission: Submission, mutation: str
) -> None:
    state = OrderProjection()
    state.register(submission)
    state.observe_order(order(submission))
    original = trade(submission)
    state.observe_trade(original)
    incoming = trade(submission, identifier="trade_test002")
    if mutation == "amount":
        incoming = replace(incoming, notional=Money.parse("499"))
    elif mutation == "limit":
        incoming = replace(incoming, price=Price.parse("11"), notional=Money.parse("550"))
    elif mutation == "symbol":
        incoming = replace(incoming, symbol=SecurityCode.parse("000002.SZ"))
    elif mutation == "broker":
        incoming = replace(incoming, broker_order_id="wrong")
    elif mutation == "overfill":
        incoming = trade(submission, quantity=100, identifier="trade_test002")
    else:
        incoming = replace(original, price=Price.parse("9"), notional=Money.parse("450"))
    with pytest.raises(ValueError):
        state.observe_trade(incoming)
    assert state.trades() == (original,)
    assert state.views()[0].traded_quantity == 50


@pytest.mark.parametrize(
    "case", ["missing_order", "missing_trade", "regression", "unknown", "unowned", "broker_id"]
)
def test_query_cannot_erase_or_weaken_callback_evidence(submission: Submission, case: str) -> None:
    state = OrderProjection()
    state.register(submission)
    current = order(submission, status=S.PARTIALLY_FILLED, filled=50)
    fill = trade(submission)
    state.observe_order(current)
    state.observe_trade(fill)
    orders: tuple[BrokerOrder, ...] = (current,)
    trades: tuple[BrokerTrade, ...] = (fill,)
    if case == "missing_order":
        orders = ()
    elif case == "missing_trade":
        trades = ()
    elif case == "regression":
        orders = (order(submission),)
    elif case == "unknown":
        orders = (replace(current, status=S.UNKNOWN),)
    elif case == "unowned":
        trades += (replace(fill, trade_id="trade_other001", client_order_id="ord_external001"),)
    else:
        orders = (replace(current, broker_order_id=None),)
    before = state.views()
    with pytest.raises(ValueError):
        state.checked_query((submission,), orders, trades)
    assert state.views() == before


def test_owner_and_memory_capacity_are_enforced(submission: Submission) -> None:
    state = OrderProjection(capacity=1, trade_capacity=1)
    state.register(submission)
    with pytest.raises(ValueError, match="capacity"):
        state.register(submission.model_copy(update={"client_order_id": "ord_other0001"}))
    state.observe_trade(trade(submission))
    with pytest.raises(ValueError, match="capacity"):
        state.observe_trade(trade(submission, identifier="trade_test002"))
    with ThreadPoolExecutor(max_workers=1) as worker:
        for call in (state.views, state.orders, state.trades, lambda: state.register(submission)):
            with pytest.raises(RuntimeError, match="owner"):
                worker.submit(call).result()
    with pytest.raises(ValueError):
        OrderProjection(capacity=0)


def test_cancel_history_is_separate_from_broker_terminal_status(submission: Submission) -> None:
    state = OrderProjection(capacity=2)
    state.register(submission)
    command = submission.model_copy(
        update={
            "client_order_id": "ord_cancel0001",
            "action_type": "CANCEL",
            "original_client_order_id": submission.client_order_id,
            "original_order_id": "broker-" + submission.client_order_id,
        }
    )
    state.register(command)
    state.observe_order(order(submission))
    assert state.views()[0].phase == "ACTIVE"
    assert state.views()[0].cancel_request_ids == (command.client_order_id,)
    with pytest.raises(ValueError, match="identity"):
        state.register(command.model_copy(update={"original_order_id": "different"}))
    with pytest.raises(ValueError, match="capacity"):
        state.register(submission.model_copy(update={"client_order_id": "ord_other0001"}))
    rebuilt = state.checked_query(
        (submission, command), (order(submission, status=S.CANCELLED),), ()
    )
    assert rebuilt.views()[0].phase == "TERMINAL_CONFIRMED"
    assert rebuilt.views()[0].cancel_request_ids == (command.client_order_id,)


def test_unknown_status_cannot_erase_quantity_on_query_and_broker_ids_are_unique(
    submission: Submission,
) -> None:
    state = OrderProjection()
    state.register(submission)
    partial = order(submission, status=S.PARTIALLY_FILLED, filled=50)
    state.observe_order(partial)
    state.observe_order(replace(order(submission), status=S.UNKNOWN))
    assert state.views()[0].phase == "UNRESOLVED"
    with pytest.raises(ValueError, match="regresses"):
        state.checked_query((submission,), (order(submission),), ())
    with pytest.raises(ValueError, match="another local"):
        state.observe_order(replace(partial, client_order_id="ord_collision001"))


def test_t1_uses_verified_sessions_and_duplicate_fills_form_one_lot(submission: Submission) -> None:
    cal = calendar()
    assert cal.next_session(date(2026, 9, 4)) == date(2026, 9, 7)
    assert cal.next_session(date(2026, 9, 8)) == date(2026, 9, 14)
    portfolio = project_lots(
        account_id=submission.account_id,
        as_of=submission.trade_date,
        calendar=cal,
        intents=(LotIntent(submission, "strategy1"),),
        trades=(trade(submission), trade(submission)),
    )
    assert len(portfolio.lots) == 1 and portfolio.lots[0].remaining == 50
    assert portfolio.lots[0].cost_price == "10"
    assert portfolio.lots[0].eligible_sell_date == date(2026, 9, 8)
    assert (
        portfolio.sellable(
            symbol=submission.symbol,
            strategy_id="strategy1",
            as_of=submission.trade_date,
            broker_sellable=50,
            reflected_order_ids=frozenset(),
        )
        == 0
    )
    assert (
        portfolio.sellable(
            symbol=submission.symbol,
            strategy_id="strategy1",
            as_of=date(2026, 9, 8),
            broker_sellable=50,
            reflected_order_ids=frozenset(),
        )
        == 50
    )
    assert_position_match(portfolio, {submission.symbol: 50})
    with pytest.raises(ValueError, match="holdings"):
        assert_position_match(portfolio, {submission.symbol: 51})


def test_sell_allocation_conserves_fills_and_unreflected_freezes(submission: Submission) -> None:
    sell = submission.model_copy(
        update={
            "client_order_id": "ord_sell0001",
            "side": "SELL",
            "quantity": 80,
            "trade_date": date(2026, 9, 8),
        }
    )
    intents = (
        LotIntent(submission, "s1"),
        LotIntent(sell, "s1", (LotAllocation(lot_id=lot_id(submission), quantity=80),)),
    )
    args: PortfolioArgs = {
        "account_id": submission.account_id,
        "as_of": sell.trade_date,
        "calendar": calendar(),
        "intents": intents,
        "trades": (
            trade(submission, quantity=100),
            trade(sell, quantity=30, identifier="trade_sell001"),
        ),
    }
    portfolio = project_lots(**args)
    lot = portfolio.lots[0]
    assert (lot.bought_quantity, lot.sold_quantity, lot.reserved_quantity, lot.remaining) == (
        100,
        30,
        50,
        70,
    )
    common: SellScope = {"symbol": submission.symbol, "strategy_id": "s1", "as_of": sell.trade_date}
    assert portfolio.sellable(**common, broker_sellable=70, reflected_order_ids=frozenset()) == 20
    assert (
        portfolio.sellable(
            **common, broker_sellable=20, reflected_order_ids=frozenset({sell.client_order_id})
        )
        == 20
    )
    terminal = project_lots(**args, terminal_order_ids=frozenset({sell.client_order_id}))
    assert terminal.lots[0].reserved_quantity == 0 and terminal.lots[0].remaining == 70
    assert terminal.sellable(**common, broker_sellable=70, reflected_order_ids=frozenset()) == 70


@pytest.mark.parametrize(
    "fault", ["D0", "owner", "overallocate", "missing", "multiple", "future", "calendar"]
)
def test_unproven_lot_ownership_or_capacity_is_rejected(submission: Submission, fault: str) -> None:
    sell = submission.model_copy(
        update={"client_order_id": "ord_sell0001", "side": "SELL", "trade_date": date(2026, 9, 8)}
    )
    binding = LotAllocation(lot_id=lot_id(submission), quantity=100)
    strategy = "s1"
    if fault == "D0":
        sell = sell.model_copy(update={"trade_date": submission.trade_date})
    elif fault == "owner":
        strategy = "s2"
    elif fault == "overallocate":
        sell = sell.model_copy(update={"quantity": 101})
        binding = binding.model_copy(update={"quantity": 101})
    elif fault == "missing":
        binding = binding.model_copy(update={"lot_id": "missing-lot"})
    intents: tuple[LotIntent, ...] = (
        LotIntent(submission, "s1"),
        LotIntent(sell, strategy, (binding,)),
    )
    trades = (trade(submission, quantity=100),)
    if fault == "multiple":
        intents += (
            LotIntent(
                sell.model_copy(update={"client_order_id": "ord_sell0002"}), "s1", (binding,)
            ),
        )
    elif fault == "future":
        trades = (
            replace(
                trades[0], traded_at=ShanghaiTimestamp.from_instant(DEMO_NOW + timedelta(days=3))
            ),
        )
    cal = (
        calendar()
        if fault != "calendar"
        else ExchangeCalendar(version="short", sessions=(date(2026, 9, 4), date(2026, 9, 8)))
    )
    with pytest.raises(ValueError):
        project_lots(
            account_id=submission.account_id,
            as_of=date(2026, 9, 8),
            calendar=cal,
            intents=intents,
            trades=trades,
        )


def test_manual_unknown_trade_locks_symbol_without_inventing_strategy_ownership(
    submission: Submission,
) -> None:
    external = replace(
        trade(submission), client_order_id="ord_manual0001", trade_id="trade_manual001"
    )
    portfolio = project_lots(
        account_id=submission.account_id,
        as_of=submission.trade_date,
        calendar=calendar(),
        intents=(LotIntent(submission, "s1"),),
        trades=(trade(submission), external),
    )
    assert portfolio.quantities() == {submission.symbol: 50}
    assert portfolio.locked_symbols == {submission.symbol}
    assert portfolio.unowned_trade_ids == (external.trade_id,)
    assert (
        portfolio.sellable(
            symbol=submission.symbol,
            strategy_id="s1",
            as_of=date(2026, 9, 8),
            broker_sellable=100,
            reflected_order_ids=frozenset(),
        )
        == 0
    )


def test_opening_lots_require_calendar_proof_and_unique_strategy(submission: Submission) -> None:
    opening = PositionLot(
        lot_id="opening1",
        strategy_id="s1",
        symbol=submission.symbol,
        trade_date_acquired=date(2026, 9, 4),
        eligible_sell_date=date(2026, 9, 7),
        calendar_version=calendar().version,
        bought_quantity=200,
    )
    args: PortfolioArgs = {
        "account_id": submission.account_id,
        "as_of": submission.trade_date,
        "calendar": calendar(),
        "intents": (),
        "trades": (),
    }
    assert project_lots(**args, opening=(opening,)).quantities() == {submission.symbol: 200}
    with pytest.raises(ValueError, match="calendar"):
        project_lots(**args, opening=(opening.model_copy(update={"calendar_version": "wrong"}),))
    with pytest.raises(ValueError, match="multiple"):
        project_lots(
            **args,
            opening=(
                opening,
                opening.model_copy(update={"lot_id": "opening2", "strategy_id": "s2"}),
            ),
        )
    with pytest.raises(ValueError, match="ordered"):
        ExchangeCalendar(version="bad", sessions=(date(2026, 9, 8), date(2026, 9, 7)))
