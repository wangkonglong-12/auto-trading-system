from datetime import date, datetime
from types import SimpleNamespace

import pytest

from auto_trading_system.gateways.broker.qmt_display import display_order, display_trade
from auto_trading_system.gateways.market import qmt
from auto_trading_system.monitoring.qmt_readonly import QmtReadOnlyAdapterError
from auto_trading_system.runtime.engine_v2 import CN

NOW = datetime(2026, 10, 23, 14, 46, tzinfo=CN)


@pytest.mark.parametrize(
    "rows,expected",
    [
        (
            ["20261022", "20261023", "20261026"],
            (date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26)),
        ),
        (["20261022", "20261023"], (date(2026, 10, 22), date(2026, 10, 23))),
        (["20261026", "20261027"], ()),
        (["20261026", "20261023"], ()),
        (["20261023", "20261023", "20261026"], ()),
        (["20261032", "20261026"], ()),
        ([1, 2], ()),
        (None, ()),
        ([], ()),
    ],
)
def test_qmt_display_calendar_validates_dates_and_future_coverage(
    monkeypatch: pytest.MonkeyPatch, rows: object, expected: tuple[date, ...]
) -> None:
    def reader(market: str, start: str, end: str) -> object:
        assert (market, start, end) == ("SH", "20260922", "")
        return rows

    monkeypatch.setattr(
        qmt, "load_xtdata_bindings", lambda: SimpleNamespace(get_trading_calendar=reader)
    )
    assert qmt.read_trading_calendar(date(2026, 10, 23)) == expected


def test_qmt_calendar_unavailable_does_not_fail_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        qmt, "load_xtdata_bindings", lambda: SimpleNamespace(get_trading_calendar=None)
    )
    assert qmt.read_trading_calendar(date(2026, 10, 23)) == ()

    def fail() -> None:
        raise RuntimeError("disconnected")

    monkeypatch.setattr(qmt, "load_xtdata_bindings", fail)
    assert qmt.read_trading_calendar(date(2026, 10, 23)) == ()


def test_display_trade_preserves_broker_time_and_marks_external_ownership() -> None:
    row = SimpleNamespace(
        stock_code="000001.SZ",
        traded_id="real-fill-1",
        order_id=100,
        traded_volume=100,
        order_type=23,
        traded_price=10.01,
        traded_amount=1001.0,
        traded_time=int(NOW.timestamp()),
    )
    result = display_trade(row, NOW, buy=23, sell=24)
    assert result["price"] == "10.01" and result["amount"] == "1001.00"
    assert result["traded_at"] == NOW.isoformat()
    assert result["owned_by_system"] is False
    row.traded_price = 10.0123
    assert display_trade(row, NOW, buy=23, sell=24)["price"] == "10.0123"
    row.traded_time -= 86400
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_DISPLAY_TIME_INVALID"):
        display_trade(row, NOW, buy=23, sell=24)


def test_display_order_never_becomes_cancelable_or_authorizes_live_orders() -> None:
    row = SimpleNamespace(
        stock_code="600000.SH",
        order_id=123,
        order_volume=300,
        traded_volume=100,
        order_status=55,
        order_type=23,
        price=10.0,
        order_time=int(NOW.timestamp()),
    )
    result = display_order(row, NOW, buy=23, sell=24)
    assert result["status"] == "PARTIALLY_FILLED"
    assert result["remaining"] == 200 and result["cancelable"] is False
    row.traded_volume = 301
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_DISPLAY_ORDER_INVALID"):
        display_order(row, NOW, buy=23, sell=24)
