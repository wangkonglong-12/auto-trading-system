from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from auto_trading_system.domain import SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.market import CiccQmtMarketGateway, MarketTick
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


def test_qmt_live_gateway_latches_invalid_callback_without_escaping() -> None:
    now = datetime.fromisoformat("2026-09-07T14:40:01+08:00")
    sdk = FakeXtdata()
    gateway = CiccQmtMarketGateway(bindings=sdk.bindings(), clock=FixedClock(now))
    gateway.start(lambda _: None)
    gateway.subscribe(SecurityCode.parse("000001.SZ"))

    sdk.callbacks[0]({"unexpected": raw_tick(1_000_000_000_000)})

    assert gateway.fault_code == "QMT_TICK_CALLBACK_INVALID"
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
