"""Operator caps exercised through the real engine and J1 with a Mock broker."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic

import pytest

from auto_trading_system.apps.trade_runtime.engine_simulation import open_offline_engine, wait_for
from auto_trading_system.domain import Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.broker.models import BrokerOrder, BrokerOrderStatus, OrderSide
from auto_trading_system.runtime.engine_v2 import EngineLimits


def limits(**updates: object) -> EngineLimits:
    return EngineLimits.model_validate(
        dict(
            max_buy_value="1005",
            max_order_value="1005",
            max_positions=1,
            max_order_quantity=100,
            max_total_position_pct="70",
            **updates,
        )
    )


@pytest.mark.parametrize("budget,accepted", [("1000", False), ("1005", True)])
def test_position_limit_checked_before_j1_and_sdk(
    tmp_path: Path,
    budget: str,
    accepted: bool,
) -> None:
    # At 70%, 100000 equity leaves exactly 1000 / 1005 room after holdings.
    with open_offline_engine(tmp_path, limits=limits()) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        rt.confirmed_arm()
        snapshot = rt.kernel._broker_snapshot
        assert snapshot
        rt.kernel._broker_snapshot = replace(
            snapshot,
            account=snapshot.account.model_copy(
                update={
                    "total_equity": "100000",
                    "market_value": str(Decimal("70000") - Decimal(budget)),
                }
            ),
        )
        order = rt.buy()
        if accepted:
            rt.kernel.submit(order, valid_until=monotonic() + 2)
            wait_for(
                rt.kernel,
                lambda s: bool(s.submissions) and s.submissions[0].status == "SDK_ACCEPTED",
            )
            assert len(rt.broker.query_orders()) == 1
        else:
            with pytest.raises(ValueError, match="submission gate rejected"):
                rt.kernel.submit(order, valid_until=monotonic() + 2)
            assert rt.broker.query_orders() == ()
            assert rt.kernel.snapshot().submissions == ()


def test_pending_manual_partial_and_unobserved_buys_count_once(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path, limits=limits()) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        rt.confirmed_arm()
        snapshot = rt.kernel._broker_snapshot
        assert snapshot
        account = snapshot.account.model_copy(
            update={"total_equity": "100000", "market_value": "60000"}
        )
        manual = BrokerOrder(
            client_order_id="ord_manual001",
            broker_order_id="manual001",
            symbol=SecurityCode.parse("600000.SH"),
            side=OrderSide.BUY,
            status=BrokerOrderStatus.PARTIALLY_FILLED,
            order_quantity=Quantity(100),
            filled_quantity=Quantity(40),
            remaining_quantity=Quantity(60),
            limit_price=Price.parse("10"),
            broker_status_code=None,
            status_reason="ACCEPTED",
            updated_at=ShanghaiTimestamp(rt.clock()),
        )
        rt.kernel._broker_snapshot = replace(snapshot, account=account, orders=(manual,))
        assert rt.kernel._position_buy_capacity() == Decimal("9400")
        local = rt.buy()
        rt.kernel._book.reserve(local, account.observation())
        assert rt.kernel._position_buy_capacity() == Decimal("8395")
        assert rt.kernel._position_buy_capacity(exclude_id=local.client_order_id) == Decimal("9400")
        observed = replace(
            manual, client_order_id=local.client_order_id, symbol=SecurityCode.parse(local.symbol)
        )
        account = account.model_copy(
            update={"observed_order_ids": (local.client_order_id, manual.client_order_id)}
        )
        rt.kernel._broker_snapshot = replace(snapshot, account=account, orders=(manual, observed))
        assert rt.kernel._position_buy_capacity() == Decimal("8795")  # 10000 - 600 - (600 + 5 fee)
        # A confirmed terminal order needs no further principal reservation.
        rt.kernel._book.resolve_terminal(local.client_order_id, reconciled=True)
        done = replace(
            observed,
            status=BrokerOrderStatus.FILLED,
            filled_quantity=Quantity(100),
            remaining_quantity=Quantity(0),
        )
        rt.kernel._broker_snapshot = replace(snapshot, account=account, orders=(manual, done))
        assert rt.kernel._position_buy_capacity() == Decimal("9400")


@pytest.mark.parametrize(
    "change",
    [
        {"total_equity": None},
        {"total_equity": "0"},
        {"market_value": None},
        {"market_value": "71000"},
        {"coherent": False},
    ],
)
def test_missing_invalid_or_excess_account_facts_block_buying(
    tmp_path: Path, change: dict[str, object]
) -> None:
    with open_offline_engine(tmp_path, limits=limits()) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        assert rt.kernel._broker_snapshot
        snapshot = rt.kernel._broker_snapshot
        account = snapshot.account.model_copy(
            update={"total_equity": "100000", "market_value": "60000", **change}
        )
        rt.kernel._broker_snapshot = replace(snapshot, account=account)
        assert rt.kernel._position_buy_capacity() == 0
        rt.kernel._broker_snapshot = replace(
            snapshot,
            account=account.model_copy(update={"observed_at": rt.clock() - timedelta(seconds=6)}),
        )
        assert rt.kernel._position_buy_capacity() == 0


def test_local_symbol_budget_cannot_be_expanded_by_plan(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path, limits=limits(max_symbol_buy_value="1000")) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        rt.confirmed_arm()
        assert rt.kernel.entry_resources("000001.SZ")[0] == Decimal("1000")
        with pytest.raises(ValueError, match="submission gate rejected"):
            rt.kernel.submit(rt.buy(), valid_until=monotonic() + 2)
        assert rt.broker.query_orders() == ()


def test_position_limit_rechecked_after_j1_before_sdk(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path, limits=limits()) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        rt.confirmed_arm()
        rt.kernel.submit(rt.buy(), valid_until=monotonic() + 2)
        snapshot = rt.kernel._broker_snapshot
        assert snapshot
        rt.kernel._broker_snapshot = replace(
            snapshot,
            account=snapshot.account.model_copy(
                update={
                    "total_equity": "100000",
                    "market_value": "70000",
                }
            ),
        )
        wait_for(
            rt.kernel, lambda s: bool(s.submissions) and s.submissions[0].status == "NOT_DISPATCHED"
        )
        assert rt.broker.query_orders() == ()
