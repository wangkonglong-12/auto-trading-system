"""Board restrictions use local settings and are enforced before any Mock order."""

import json
from pathlib import Path
from time import monotonic

import pytest
from pydantic import ValidationError
from test_tail_feed import NOW, local_feed, wire
from test_tail_phase_execution import phase

from auto_trading_system.apps.trade_runtime.engine_simulation import open_offline_engine, wait_for
from auto_trading_system.runtime.engine_v2 import EngineLimits
from auto_trading_system.strategies.trading_settings import BuySettings, stock_board


@pytest.mark.parametrize(
    "symbol,board",
    [
        ("000001.SZ", "MAIN"),
        ("001287.SZ", "MAIN"),
        ("002001.SZ", "MAIN"),
        ("003816.SZ", "MAIN"),
        ("600000.SH", "MAIN"),
        ("601398.SH", "MAIN"),
        ("603000.SH", "MAIN"),
        ("605001.SH", "MAIN"),
        ("300853.SZ", "CHINEXT"),
        ("301001.SZ", "CHINEXT"),
        ("688001.SH", "STAR"),
        ("689009.SH", "STAR"),
        ("300853.SH", None),
        ("688001.SZ", None),
        ("510300.SH", None),
        ("159001.SZ", None),
        ("900001.SH", None),
        ("830001.BJ", None),
        ("300853", None),
    ],
)
def test_board_classification_requires_equity_code_and_matching_exchange(symbol, board):
    assert stock_board(symbol) == board


def test_legacy_settings_default_main_and_explicit_selection_is_strict():
    base = {
        "max_daily_buy_value": "10000",
        "max_symbol_buy_value": "10000",
        "max_order_value": "10000",
        "cancel_after_seconds": 10,
        "max_retries": 3,
    }
    assert BuySettings.model_validate_json(json.dumps(base)).allowed_boards == ("MAIN",)
    assert (
        BuySettings.model_validate_json(json.dumps(base)).model_dump(mode="json")["order_method"]
        == "ASK1_PROTECTED_LIMIT"
    )
    assert "allowed_boards" not in BuySettings.model_validate_json(json.dumps(base)).model_dump(
        mode="json"
    )
    for invalid in (["INVALID"], ["MAIN", "MAIN"], None, "MAIN"):
        with pytest.raises(ValidationError):
            BuySettings.model_validate_json(json.dumps({**base, "allowed_boards": invalid}))
    assert (
        BuySettings.model_validate_json(json.dumps({**base, "allowed_boards": []})).allowed_boards
        == ()
    )
    selected = BuySettings.model_validate_json(
        json.dumps({**base, "allowed_boards": ["STAR", "MAIN"]})
    )
    assert selected.allowed_boards == ("MAIN", "STAR")
    assert BuySettings.model_validate_json(selected.model_dump_json()) == selected


@pytest.mark.parametrize(
    "boards,expected",
    [
        (("MAIN",), ("600000.SH", "000001.SZ")),
        (("CHINEXT",), ("300853.SZ",)),
        (("STAR",), ("688001.SH",)),
        (("MAIN", "CHINEXT", "STAR"), ("300853.SZ", "600000.SH", "688001.SH", "000001.SZ")),
        ((), ()),
    ],
)
def test_feed_filters_before_execution_without_spending_main_board_slots(
    tmp_path: Path, boards, expected
):
    feed = local_feed(tmp_path)
    feed = type(feed)(
        feed.config,
        feed.settings.model_copy(
            update={
                "buy": feed.settings.buy.model_copy(update={"allowed_boards": boards}),
            }
        ),
        feed.plans,
        feed.key,
    )
    value = wire()
    symbols = ("300853.SZ", "600000.SH", "688001.SH", "000001.SZ")
    value["batches"][0]["stocks"] = [
        {"code": symbol, "name": "测试股票", "rank": rank} for rank, symbol in enumerate(symbols, 1)
    ]
    feed.accept(json.dumps(value).encode(), NOW)
    assert tuple(a.symbol for a in feed.plans.plans.accepted()[0].payload.allocations) == expected
    assert len(feed.snapshot.batches[0].stocks) == 4  # Original source facts remain intact.
    assert feed.complete


@pytest.mark.parametrize(
    "symbol,boards,allowed",
    [
        ("000001.SZ", ("MAIN",), True),
        ("300853.SZ", ("MAIN",), False),
        ("300853.SZ", ("CHINEXT",), True),
        ("688001.SH", ("MAIN",), False),
        ("688001.SH", ("STAR",), True),
        ("000001.SZ", (), False),
    ],
)
def test_unselected_board_cannot_reach_j1_or_sdk_even_in_a_signed_tail_plan(
    tmp_path: Path, symbol, boards, allowed
):
    payload, _ = phase("1440", (symbol,))
    installed = EngineLimits(
        max_buy_value="1005",
        max_order_value="1005",
        max_positions=1,
        max_order_quantity=100,
        allowed_boards=boards,
    )
    with open_offline_engine(
        tmp_path, plan_payload=payload, tail_phases=True, limits=installed
    ) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda state: state.state == "REVIEW_REQUIRED")
        rt.confirmed_arm()
        order = rt.buy().model_copy(update={"symbol": symbol})
        if allowed:
            rt.kernel.submit(order, valid_until=monotonic() + 2)
            wait_for(
                rt.kernel,
                lambda state: bool(state.submissions)
                and state.submissions[0].status == "SDK_ACCEPTED",
            )
            assert len(rt.broker.query_orders()) == 1
        else:
            assert rt.kernel.entry_resources(symbol) == (0, 0, 0)
            with pytest.raises(ValueError, match="submission gate rejected"):
                rt.kernel.submit(order, valid_until=monotonic() + 2)
            assert not rt.kernel.snapshot().submissions and not rt.broker.query_orders()


def test_board_is_rechecked_after_j1_before_sdk_dispatch(tmp_path: Path):
    payload, _ = phase("1440", ("000001.SZ",))
    installed = EngineLimits(
        max_buy_value="1005",
        max_order_value="1005",
        max_positions=1,
        max_order_quantity=100,
        allowed_boards=("MAIN",),
    )
    with open_offline_engine(
        tmp_path, plan_payload=payload, tail_phases=True, limits=installed
    ) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda state: state.state == "REVIEW_REQUIRED")
        rt.confirmed_arm()
        rt.kernel.submit(rt.buy(), valid_until=monotonic() + 2)
        rt.kernel.limits = installed.model_copy(update={"allowed_boards": ()})
        wait_for(
            rt.kernel,
            lambda state: bool(state.submissions)
            and state.submissions[0].status == "NOT_DISPATCHED",
        )
        assert not rt.broker.query_orders()
