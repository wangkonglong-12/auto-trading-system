from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from auto_trading_system.domain import SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.market import CiccQmtMarketGateway, MarketTick
from auto_trading_system.gateways.market.errors import MarketDataQueryError
from auto_trading_system.gateways.market.qmt import XtdataBindings


@dataclass(frozen=True)
class FixedClock:
    instant: datetime

    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp(self.instant)


@dataclass
class FakeXtdata:
    callbacks: list[Callable[[object], None]] = field(default_factory=list)
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def subscribe_quote(self, symbol: str, **kwargs: object) -> int:
        self.calls.append(("subscribe", symbol, kwargs["period"], kwargs["count"]))
        callback = kwargs["callback"]
        assert callable(callback)
        self.callbacks.append(callback)
        return 100 + len(self.callbacks)

    def unsubscribe_quote(self, subscription_id: int) -> None:
        self.calls.append(("unsubscribe", subscription_id))

    def get_full_tick(self, symbols: list[str]) -> object:
        return {symbol: {} for symbol in symbols}

    def get_instrument_detail(self, symbol: str, complete: bool) -> object:
        self.calls.append(("instrument", symbol, complete))
        return {
            "IsTrading": True,
            "PriceTick": 0.01,
            "DownStopPrice": 8.12,
            "UpStopPrice": 12.18,
        }

    def bindings(self) -> XtdataBindings:
        return XtdataBindings(
            self.subscribe_quote,
            self.unsubscribe_quote,
            self.get_full_tick,
            "fixture",
            self.get_instrument_detail,
        )


def raw_tick(timestamp: int) -> dict[str, object]:
    return {
        "time": timestamp,
        "lastPrice": 10.1,
        "amount": 1_000_000.125,
        "volume": 1_000,
        "pvolume": 100_000,
        "stockStatus": 13,
        "askPrice": [10.1, 10.2],
        "bidPrice": [10.0, 9.9],
        "askVol": [10, 20],
        "bidVol": [50, 40],
    }


def test_qmt_live_gateway_subscribes_converts_and_unsubscribes() -> None:
    now = datetime.fromisoformat("2026-09-07T14:40:01+08:00")
    timestamp = int(datetime.fromisoformat("2026-09-07T14:40:00+08:00").timestamp() * 1000)
    sdk = FakeXtdata()
    gateway = CiccQmtMarketGateway(bindings=sdk.bindings(), clock=FixedClock(now))
    events: list[MarketTick] = []
    symbol = SecurityCode.parse("000001.SZ")
    gateway.start(events.append)

    first = gateway.subscribe(symbol)
    duplicate = gateway.subscribe(symbol)
    sdk.callbacks[0]({"000001.SZ": raw_tick(timestamp)})

    assert first == duplicate
    assert sdk.calls == [("subscribe", "000001.SZ", "tick", 0)]
    assert len(events) == 1
    assert events[0].symbol == symbol
    assert events[0].cumulative_volume.value == 100_000
    assert events[0].trading_status == 13
    assert events[0].asks[0].quantity.value == 1_000
    assert events[0].cumulative_turnover.to_wire() == "1000000.13"
    assert gateway.latest_tick(symbol) == events[0]
    assert gateway.fault_code is None

    assert gateway.unsubscribe(first.subscription_id)
    assert gateway.latest_tick(symbol) is None
    assert not gateway.unsubscribe(first.subscription_id)
    gateway.stop()
    assert sdk.calls[-1] == ("unsubscribe", first.subscription_id)


def test_qmt_live_gateway_prefers_open_int_exchange_phase() -> None:
    now = datetime.fromisoformat("2026-09-07T14:40:01+08:00")
    timestamp = int(datetime.fromisoformat("2026-09-07T14:40:00+08:00").timestamp() * 1000)
    sdk = FakeXtdata()
    gateway = CiccQmtMarketGateway(bindings=sdk.bindings(), clock=FixedClock(now))
    events: list[MarketTick] = []
    gateway.start(events.append)
    gateway.subscribe(SecurityCode.parse("000001.SZ"))
    payload = raw_tick(timestamp)
    payload["openInt"] = 17

    sdk.callbacks[0]({"000001.SZ": payload})

    assert events[0].trading_status == 17
    gateway.stop()


@pytest.mark.parametrize(
    "volume,pvolume", [(115314, 11531413), (359705, 35970460), (888276, 88827626), (0, 13), (0, 0)]
)
def test_a_share_units_accept_rounded_hands_and_keep_exact_shares(
    volume: int, pvolume: int
) -> None:
    from auto_trading_system.gateways.market.qmt_live import _normalize_tick

    now = datetime.fromisoformat("2026-10-27T11:20:00+08:00")
    payload = raw_tick(int(now.timestamp() * 1000))
    payload.update(volume=volume, pvolume=pvolume)
    tick = _normalize_tick(SecurityCode.parse("003021.SZ"), payload, ShanghaiTimestamp(now), 1)
    assert tick.cumulative_volume.value == pvolume
    assert tick.asks[0].quantity.value == 1000
    assert tick.bids[0].quantity.value == 5000


@pytest.mark.parametrize("volume,pvolume", [(1, 1000), (1000, 1000), (0, 100), (-1, 0)])
def test_inconsistent_a_share_volume_still_rejects(volume: int, pvolume: int) -> None:
    from auto_trading_system.gateways.market.qmt_live import _normalize_tick

    now = datetime.fromisoformat("2026-10-27T11:20:00+08:00")
    payload = raw_tick(int(now.timestamp() * 1000))
    payload.update(volume=volume, pvolume=pvolume)
    with pytest.raises(ValueError):
        _normalize_tick(SecurityCode.parse("003021.SZ"), payload, ShanghaiTimestamp(now), 1)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.01, 0.0, 1e-10])
def test_invalid_or_unrepresentable_price_still_rejects(value: float) -> None:
    from auto_trading_system.gateways.market.qmt_live import _normalize_tick

    now = datetime.fromisoformat("2026-10-27T11:20:00+08:00")
    payload = raw_tick(int(now.timestamp() * 1000))
    payload["lastPrice"] = value
    with pytest.raises(ValueError):
        _normalize_tick(SecurityCode.parse("003021.SZ"), payload, ShanghaiTimestamp(now), 1)


def test_vendor_float_price_tails_do_not_block_valid_ticks_or_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from auto_trading_system.gateways.market.qmt_live import _normalize_tick

    now = datetime.fromisoformat("2026-10-27T11:20:00+08:00")
    payload = raw_tick(int(now.timestamp() * 1000))
    payload.update(lastPrice=94.68000000000001, askPrice=[94.69000000000001], askVol=[10])
    tick = _normalize_tick(SecurityCode.parse("003021.SZ"), payload, ShanghaiTimestamp(now), 1)
    assert str(tick.last_price.amount) == "94.68"
    assert str(tick.asks[0].price.amount) == "94.69"
    sdk = FakeXtdata()
    monkeypatch.setattr(
        sdk,
        "get_instrument_detail",
        lambda *_: {
            "PriceTick": 0.01,
            "DownStopPrice": 86.63000000000001,
            "UpStopPrice": 105.89000000000001,
        },
    )
    rule = CiccQmtMarketGateway(bindings=sdk.bindings()).instrument_rule(
        SecurityCode.parse("003021.SZ")
    )
    assert str(rule.lower_price) == "86.63"
    assert str(rule.upper_price) == "105.89"


def test_qmt_live_gateway_rejects_repeated_invalid_callbacks_and_recovers() -> None:
    now = datetime.fromisoformat("2026-09-07T14:40:01+08:00")
    sdk = FakeXtdata()
    gateway = CiccQmtMarketGateway(bindings=sdk.bindings(), clock=FixedClock(now))
    gateway.start(lambda _: None)
    gateway.subscribe(SecurityCode.parse("000001.SZ"))

    sdk.callbacks[0]({"unexpected": raw_tick(1_000_000_000_000)})
    assert gateway.fault_code is None
    sdk.callbacks[0]({"unexpected": raw_tick(1_000_000_000_000)})
    assert gateway.fault_code is None
    sdk.callbacks[0]({"unexpected": raw_tick(1_000_000_000_000)})
    assert gateway.fault_code == "QMT_TICK_CALLBACK_INVALID"
    sdk.callbacks[0]({"000001.SZ": raw_tick(int(now.timestamp() * 1000))})
    assert gateway.fault_code is None
    gateway.stop()


def test_qmt_live_gateway_reads_broker_instrument_rules() -> None:
    now = datetime.fromisoformat("2026-09-07T14:40:01+08:00")
    sdk = FakeXtdata()
    gateway = CiccQmtMarketGateway(bindings=sdk.bindings(), clock=FixedClock(now))
    gateway.start(lambda _: None)

    mainboard = gateway.instrument_rule(SecurityCode.parse("000001.SZ"))
    star = gateway.instrument_rule(SecurityCode.parse("688001.SH"))

    assert (mainboard.min_buy_quantity, mainboard.buy_quantity_step) == (100, 100)
    assert (star.min_buy_quantity, star.buy_quantity_step) == (200, 1)
    assert str(mainboard.lower_price) == "8.12"
    assert sdk.calls == [
        ("instrument", "000001.SZ", False),
        ("instrument", "688001.SH", False),
    ]
    gateway.stop()


@pytest.mark.parametrize("trading_field", [{"IsTrading": False}, {"IsTrading": None}, {}])
def test_stock_rules_do_not_require_is_trading(
    trading_field: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = FakeXtdata()
    raw = {
        "PriceTick": 0.01,
        "DownStopPrice": 86.63,
        "UpStopPrice": 105.89,
        **trading_field,
    }
    monkeypatch.setattr(sdk, "get_instrument_detail", lambda *_: raw)
    gateway = CiccQmtMarketGateway(bindings=sdk.bindings())

    rule = gateway.instrument_rule(SecurityCode.parse("003021.SZ"))

    assert str(rule.lower_price) == "86.63"
    assert str(rule.upper_price) == "105.89"
    assert (rule.min_buy_quantity, rule.buy_quantity_step) == (100, 100)

    # Relaxing the flag must not also relax the existing price-limit check.
    raw["UpStopPrice"] = raw["DownStopPrice"]
    with pytest.raises(MarketDataQueryError, match="price limits are invalid"):
        gateway.instrument_rule(SecurityCode.parse("003021.SZ"))


@pytest.mark.parametrize("lead", [1.02, 3.0])
def test_small_clock_lead_waits_without_rewriting_exchange_time(lead: float) -> None:
    from datetime import timedelta
    from types import SimpleNamespace

    now = datetime.fromisoformat("2026-10-28T09:45:02+08:00")
    clock = SimpleNamespace(instant=now)
    clock.now = lambda: ShanghaiTimestamp(clock.instant)
    sdk = FakeXtdata()
    gateway = CiccQmtMarketGateway(bindings=sdk.bindings(), clock=clock)
    events = []
    gateway.start(events.append)
    gateway.subscribe(SecurityCode.parse("000001.SZ"))
    occurred = now + timedelta(seconds=lead)
    sdk.callbacks[0]({"000001.SZ": raw_tick(int(occurred.timestamp() * 1000))})
    assert not events and gateway.fault_code is None
    clock.instant = occurred - timedelta(milliseconds=1)
    assert gateway.fault_code is None and not events
    clock.instant = occurred
    assert gateway.fault_code is None and len(events) == 1
    assert events[0].occurred_at.value == occurred
    assert events[0].received_at.value == occurred
    gateway.stop()


def test_excess_clock_lead_and_bounded_queue_fail_closed() -> None:
    now = datetime.fromisoformat("2026-10-28T09:45:02+08:00")
    sdk = FakeXtdata()
    gateway = CiccQmtMarketGateway(bindings=sdk.bindings(), clock=FixedClock(now))
    events = []
    gateway.start(events.append)
    gateway.subscribe(SecurityCode.parse("000001.SZ"))
    timestamp = int(now.timestamp() * 1000)
    sdk.callbacks[0]({"000001.SZ": raw_tick(timestamp + 3001)})
    assert gateway.fault_code == "QMT_TICK_CLOCK_SKEW" and not events
    sdk.callbacks[0]({"000001.SZ": raw_tick(timestamp)})
    assert gateway.fault_code is None
    for _ in range(257):
        sdk.callbacks[0]({"000001.SZ": raw_tick(timestamp + 1000)})
    assert gateway.fault_code == "QMT_TICK_CLOCK_SKEW"
    assert len(gateway._pending_ticks) == 256
    gateway.stop()


def test_healthy_other_stock_cannot_clear_broken_stock() -> None:
    now = datetime.fromisoformat("2026-10-28T09:45:02+08:00")
    sdk = FakeXtdata()
    gateway = CiccQmtMarketGateway(bindings=sdk.bindings(), clock=FixedClock(now))
    gateway.start(lambda _: None)
    gateway.subscribe(SecurityCode.parse("000001.SZ"))
    gateway.subscribe(SecurityCode.parse("600698.SH"))
    for _ in range(3):
        sdk.callbacks[0]({"unexpected": {}})
    sdk.callbacks[1]({"600698.SH": raw_tick(int(now.timestamp() * 1000))})
    assert gateway.fault_code == "QMT_TICK_CALLBACK_INVALID"
    gateway.stop()


def test_pending_older_quote_cannot_clear_newer_invalid_callback_fault() -> None:
    from datetime import timedelta
    from types import SimpleNamespace

    now = datetime.fromisoformat("2026-10-28T09:45:02+08:00")
    clock = SimpleNamespace(instant=now)
    clock.now = lambda: ShanghaiTimestamp(clock.instant)
    sdk = FakeXtdata()
    gateway = CiccQmtMarketGateway(bindings=sdk.bindings(), clock=clock)
    gateway.start(lambda _: None)
    gateway.subscribe(SecurityCode.parse("000001.SZ"))
    timestamp = int(now.timestamp() * 1000)
    sdk.callbacks[0]({"000001.SZ": raw_tick(timestamp + 1000)})
    for _ in range(3):
        sdk.callbacks[0]({"unexpected": {}})
    clock.instant += timedelta(seconds=1)
    assert gateway.fault_code == "QMT_TICK_CALLBACK_INVALID"
    sdk.callbacks[0]({"000001.SZ": raw_tick(timestamp + 1000)})
    assert gateway.fault_code is None
    gateway.stop()
