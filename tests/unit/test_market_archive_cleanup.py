from __future__ import annotations

from datetime import date, timedelta
from threading import get_ident
from types import SimpleNamespace
from typing import Any

import pytest

from auto_trading_system.reconcile.position_lots import PortfolioProjection, PositionLot
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.runtime.recovery_v2 import (
    AccountFact,
    BrokerRecoverySnapshot,
    PositionFact,
)
from auto_trading_system.testing.v31_scenarios import example_plan


def kernel() -> Any:
    """Supply the same typed immutable inputs used by the real observation path."""
    node = EngineKernel.__new__(EngineKernel)
    value: Any = node
    now = example_plan().execution_window.cancel_by + timedelta(seconds=1)
    value._owner = get_ident()
    value._clock = lambda: now
    value._events = lambda: 0
    account = AccountFact(
        account_id="test-account",
        snapshot_id="snapshot-1",
        observed_at=now,
        cash="1000",
        positions=(),
        observed_order_ids=(),
        coherent=True,
    )
    value._broker_snapshot = BrokerRecoverySnapshot((), (), account, 0, True)
    value._report = SimpleNamespace(status="CLEAR")
    value._observation = value._observed = value._page = value._query = None
    value._checkpoint = value._opening_read = None
    value._audit = []
    value._coordinator = SimpleNamespace(snapshot=lambda: ())
    lot = PositionLot(
        lot_id="lot-1",
        strategy_id="tail-entry",
        symbol="000001.SZ",
        trade_date_acquired=date(2026, 9, 4),
        eligible_sell_date=date(2026, 9, 7),
        calendar_version="calendar-1",
        bought_quantity=100,
        sold_quantity=100,
    )
    value._portfolio = PortfolioProjection((lot,), frozenset(), (), ())
    value._known = {}
    value._order_state = SimpleNamespace(views=lambda: ())
    value.plans = SimpleNamespace(plans=SimpleNamespace(accepted=lambda: ()))
    return value


def test_only_a_positive_reconciled_full_sale_makes_history_disposable() -> None:
    node = kernel()
    assert node.disposable_market_symbols() == frozenset({"000001.SZ"})
    node._portfolio = PortfolioProjection((), frozenset(), (), ())
    assert not node.disposable_market_symbols()  # Zero account position alone proves nothing.


@pytest.mark.parametrize(
    "reason",
    [
        "partial",
        "other-lot",
        "reserved",
        "locked",
        "unowned",
        "unmanaged",
        "account-position",
        "incoherent",
        "stale",
        "unstable",
        "new-event",
        "audit",
        "recovery",
        "blocked-report",
        "pending-order",
        "uncertain-cancel",
        "live-plan",
    ],
)
def test_disposition_is_blocked_until_every_reference_is_safe(reason: str) -> None:
    node = kernel()
    portfolio = node._portfolio
    lot = portfolio.lots[0]
    broker = node._broker_snapshot
    if reason in {"partial", "reserved", "other-lot"}:
        remaining = lot.model_copy(
            update={"sold_quantity": 50, "reserved_quantity": 0 if reason != "reserved" else 50}
        )
        lots = (
            (lot, remaining.model_copy(update={"lot_id": "lot-2"}))
            if reason == "other-lot"
            else (remaining,)
        )
        node._portfolio = PortfolioProjection(lots, frozenset(), (), ())
    elif reason == "locked":
        node._portfolio = PortfolioProjection((lot,), frozenset({lot.symbol}), (), ())
    elif reason == "unowned":
        node._portfolio = PortfolioProjection((lot,), frozenset(), ("unknown-trade",), ())
    elif reason == "unmanaged":
        node._portfolio = PortfolioProjection((lot,), frozenset(), (), (), ((lot.symbol, 100),))
    elif reason in {"account-position", "incoherent", "stale"}:
        change = (
            {"positions": (PositionFact(symbol=lot.symbol, quantity=100, sellable=100),)}
            if reason == "account-position"
            else {"coherent": False}
            if reason == "incoherent"
            else {"observed_at": broker.account.observed_at - timedelta(seconds=6)}
        )
        node._broker_snapshot = BrokerRecoverySnapshot(
            (), (), broker.account.model_copy(update=change), 0, True
        )
    elif reason == "unstable":
        node._broker_snapshot = BrokerRecoverySnapshot((), (), broker.account, 0, False)
    elif reason == "new-event":
        node._events = lambda: 1
    elif reason == "audit":
        node._audit = [object()]
    elif reason == "recovery":
        node._query = object()
    elif reason == "blocked-report":
        node._report.status = "BLOCKED"
    elif reason == "pending-order":
        node._known = {
            "order-1": SimpleNamespace(
                client_order_id="order-1", action_type="NEW", symbol=lot.symbol
            )
        }
    elif reason == "uncertain-cancel":
        node._coordinator = SimpleNamespace(snapshot=lambda: (SimpleNamespace(status="UNCERTAIN"),))
    else:
        node.plans.plans.accepted = lambda: (SimpleNamespace(payload=example_plan()),)
        node._clock = lambda: example_plan().execution_window.entry_not_before
        account = broker.account.model_copy(update={"observed_at": node._clock()})
        node._broker_snapshot = BrokerRecoverySnapshot((), (), account, 0, True)
    assert not node.disposable_market_symbols()
