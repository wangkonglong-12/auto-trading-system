from dataclasses import replace
from datetime import timedelta
from typing import Any

from test_recovery_v2 import evidence

from auto_trading_system.domain import Money, Quantity
from auto_trading_system.gateways.broker.models import BrokerOrderStatus, OrderSide
from auto_trading_system.reconcile.position_lots import (
    PortfolioProjection,
    PositionLot,
    SellReservation,
)
from auto_trading_system.runtime.manual_sell_view import sell_positions


def test_sell_view_enforces_t1_and_never_includes_unmanaged_shares() -> None:
    _, snapshot = evidence()
    today = snapshot.account.observed_at
    lot = PositionLot(
        lot_id="lot-sell-test",
        strategy_id="tail-entry",
        symbol="000001.SZ",
        trade_date_acquired=today.date(),
        eligible_sell_date=today.date() + timedelta(days=1),
        calendar_version="test-v1",
        bought_quantity=100,
    )
    portfolio = PortfolioProjection((lot,), frozenset(), (), ())
    rows: Any = sell_positions(
        portfolio, snapshot, now=today, ready=True, locks=set(), owned_sell_ids=frozenset()
    )
    assert rows[0]["sellable"] == 0 and rows[0]["disabled_reason"] == "T_PLUS_ONE"
    tomorrow = today + timedelta(days=1)
    richer = replace(
        snapshot,
        account=snapshot.account.model_copy(
            update={
                "positions": tuple(
                    p.model_copy(update={"sellable": 500, "quantity": 500})
                    for p in snapshot.account.positions
                )
            }
        ),
    )
    rows = sell_positions(
        portfolio, richer, now=tomorrow, ready=True, locks=set(), owned_sell_ids=frozenset()
    )
    assert rows[0]["sellable"] == 100


def test_sell_view_counts_owned_fills_and_does_not_double_subtract_reflected_freeze() -> None:
    _, snapshot = evidence()
    now = snapshot.account.observed_at + timedelta(days=1)
    lot = PositionLot(
        lot_id="lot-sell-test",
        strategy_id="tail-entry",
        symbol="000001.SZ",
        trade_date_acquired=now.date() - timedelta(days=1),
        eligible_sell_date=now.date(),
        calendar_version="test-v1",
        bought_quantity=100,
        sold_quantity=40,
        reserved_quantity=20,
    )
    portfolio = PortfolioProjection(
        (lot,), frozenset(), (), (SellReservation("ord_reserved_sell01", "000001.SZ", 20),)
    )
    fill = replace(
        snapshot.trades[0],
        side=OrderSide.SELL,
        client_order_id="ord_reserved_sell01",
        quantity=Quantity(40),
        notional=Money.parse("400"),
    )
    unrelated = replace(fill, trade_id="trade_unrelated01", client_order_id="ord_external_12345678")
    observed = replace(
        snapshot,
        trades=(fill, unrelated),
        account=snapshot.account.model_copy(
            update={
                "positions": tuple(
                    p.model_copy(update={"sellable": 40, "quantity": 60})
                    for p in snapshot.account.positions
                )
            }
        ),
    )
    rows: Any = sell_positions(
        portfolio,
        observed,
        now=now,
        ready=True,
        locks=set(),
        owned_sell_ids=frozenset({"ord_reserved_sell01"}),
    )
    assert (
        rows[0]["remaining"] == 60
        and rows[0]["sold_today"] == 40
        and rows[0]["sell_amount"] == "400.00"
    )
    # The not-yet-reflected 20-share reservation is deducted once.
    assert rows[0]["sellable"] == 20
    fully_reserved = portfolio.lots[0].model_copy(update={"reserved_quantity": 60})
    reserved_portfolio = PortfolioProjection(
        (fully_reserved,),
        frozenset(),
        (),
        (SellReservation("ord_reserved_sell01", "000001.SZ", 60),),
    )
    active = replace(
        snapshot.orders[0],
        client_order_id="ord_reserved_sell01",
        side=OrderSide.SELL,
        status=BrokerOrderStatus.PARTIALLY_FILLED,
        filled_quantity=Quantity(40),
        remaining_quantity=Quantity(60),
    )
    frozen = replace(
        observed,
        orders=(active,),
        account=observed.account.model_copy(
            update={
                "positions": tuple(
                    p.model_copy(update={"sellable": 0, "frozen": 60})
                    for p in observed.account.positions
                )
            }
        ),
    )
    rows = sell_positions(
        reserved_portfolio,
        frozen,
        now=now,
        ready=True,
        locks=set(),
        owned_sell_ids=frozenset({"ord_reserved_sell01"}),
    )
    assert rows[0]["disabled_reason"] == "ORDER_CONFLICT"
