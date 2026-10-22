"""Production XtData market gateway used by the plan-driven runtime.

The XtData callback only validates and converts one bounded payload before
handing an immutable tick to the configured sink.  It never performs strategy,
database, broker-query, or HTTP work on the SDK callback thread.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from threading import Lock
from typing import cast

from auto_trading_system.domain import (
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
    SystemClock,
)
from auto_trading_system.domain.clock import Clock
from auto_trading_system.domain.trading_time import SHANGHAI_TIMEZONE
from auto_trading_system.gateways.market.errors import (
    MarketDataQueryError,
    MarketNotStartedError,
    SubscriptionLimitExceeded,
)
from auto_trading_system.gateways.market.models import BookLevel, MarketSubscription, MarketTick
from auto_trading_system.gateways.market.port import MarketEventSink
from auto_trading_system.gateways.market.qmt import XtdataBindings, load_xtdata_bindings

_MAX_SUBSCRIPTIONS = 15
_MAX_CALLBACK_BATCH = 256
_REQUIRED_FIELDS = frozenset(
    {
        "time",
        "lastPrice",
        "amount",
        "volume",
        "pvolume",
        "askPrice",
        "bidPrice",
        "askVol",
        "bidVol",
    }
)


@dataclass(frozen=True, slots=True)
class QmtInstrumentRule:
    """Broker supplied price limits plus exchange quantity rules for one A share."""

    price_tick: Decimal
    lower_price: Decimal
    upper_price: Decimal
    min_buy_quantity: int
    buy_quantity_step: int


class CiccQmtMarketGateway:
    """Bounded, thread-safe XtData adapter for plan-selected securities."""

    def __init__(
        self,
        *,
        bindings: XtdataBindings | None = None,
        clock: Clock | None = None,
        max_subscriptions: int = _MAX_SUBSCRIPTIONS,
    ) -> None:
        if type(max_subscriptions) is not int or not 1 <= max_subscriptions <= _MAX_SUBSCRIPTIONS:
            raise ValueError("QMT market subscription limit must be between 1 and 15")
        self._bindings = load_xtdata_bindings() if bindings is None else bindings
        self._clock = SystemClock() if clock is None else clock
        self._maximum = max_subscriptions
        self._sink: MarketEventSink | None = None
        self._lock = Lock()
        self._by_symbol: dict[SecurityCode, MarketSubscription] = {}
        self._by_id: dict[int, SecurityCode] = {}
        self._callbacks: dict[int, Callable[[object], None]] = {}
        self._latest: dict[SecurityCode, MarketTick] = {}
        self._fault: str | None = None
        self._sequence = 0

    @property
    def fault_code(self) -> str | None:
        with self._lock:
            return self._fault

    @property
    def subscription_count(self) -> int:
        with self._lock:
            return len(self._by_id)

    def start(self, event_sink: MarketEventSink) -> None:
        if not callable(event_sink):
            raise TypeError("event_sink must be callable")
        with self._lock:
            if self._sink is not None:
                raise MarketNotStartedError("QMT market gateway is already started")
            self._sink = event_sink
            self._fault = None

    def stop(self) -> None:
        with self._lock:
            ids = tuple(reversed(tuple(self._by_id)))
        failure: Exception | None = None
        for subscription_id in ids:
            try:
                self.unsubscribe(subscription_id)
            except Exception as exc:  # cleanup continues for every owned subscription
                failure = failure or exc
        with self._lock:
            self._sink = None
            self._by_symbol.clear()
            self._by_id.clear()
            self._callbacks.clear()
            self._latest.clear()
        if failure is not None:
            raise MarketDataQueryError("QMT market unsubscribe failed during shutdown") from failure

    def subscribe(self, symbol: SecurityCode) -> MarketSubscription:
        self._require_started()
        with self._lock:
            existing = self._by_symbol.get(symbol)
            if existing is not None:
                return existing
            if len(self._by_symbol) >= self._maximum:
                raise SubscriptionLimitExceeded(
                    f"QMT market subscription limit {self._maximum} exceeded"
                )
        wire = symbol.to_wire()

        def callback(payload: object) -> None:
            self._callback(symbol, payload)

        try:
            raw_id = self._bindings.subscribe_quote(wire, period="tick", count=0, callback=callback)
        except Exception as exc:
            raise MarketDataQueryError("QMT tick subscription failed") from exc
        if type(raw_id) is not int or raw_id <= 0:
            raise MarketDataQueryError("QMT tick subscription returned an invalid id")
        subscription = MarketSubscription(raw_id, symbol)
        with self._lock:
            if raw_id in self._by_id or symbol in self._by_symbol:
                try:
                    self._bindings.unsubscribe_quote(raw_id)
                finally:
                    self._fault = "QMT_SUBSCRIPTION_ID_DUPLICATE"
                raise MarketDataQueryError("QMT reused an active subscription id")
            self._by_symbol[symbol] = subscription
            self._by_id[raw_id] = symbol
            self._callbacks[raw_id] = callback
        return subscription

    def unsubscribe(self, subscription_id: int) -> bool:
        self._require_started()
        with self._lock:
            symbol = self._by_id.get(subscription_id)
        if symbol is None:
            return False
        try:
            self._bindings.unsubscribe_quote(subscription_id)
        except Exception as exc:
            raise MarketDataQueryError("QMT tick unsubscribe failed") from exc
        with self._lock:
            self._by_id.pop(subscription_id, None)
            self._by_symbol.pop(symbol, None)
            self._callbacks.pop(subscription_id, None)
            self._latest.pop(symbol, None)
        return True

    def latest_tick(self, symbol: SecurityCode) -> MarketTick | None:
        self._require_started()
        with self._lock:
            return self._latest.get(symbol)

    def instrument_rule(self, symbol: SecurityCode) -> QmtInstrumentRule:
        """Read current contract facts before constructing a live order policy."""
        reader = self._bindings.get_instrument_detail
        if reader is None:
            raise MarketDataQueryError("QMT instrument-detail API is unavailable")
        try:
            raw = reader(symbol.to_wire(), False)
        except Exception as exc:
            raise MarketDataQueryError("QMT instrument-detail query failed") from exc
        if not isinstance(raw, Mapping):
            raise MarketDataQueryError("QMT instrument-detail result is invalid")
        if raw.get("IsTrading") is not True:
            raise MarketDataQueryError("QMT reports the security is not tradable")
        tick = _decimal(raw.get("PriceTick"), "PriceTick", positive=True)
        lower = _decimal(raw.get("DownStopPrice"), "DownStopPrice", positive=True)
        upper = _decimal(raw.get("UpStopPrice"), "UpStopPrice", positive=True)
        if lower >= upper or tick > Decimal("1"):
            raise MarketDataQueryError("QMT instrument price limits are invalid")
        wire = symbol.to_wire()
        # STAR Market buy orders start at 200 shares and may increase one share at a time.
        minimum, step = (200, 1) if wire.startswith("68") else (100, 100)
        return QmtInstrumentRule(tick, lower, upper, minimum, step)

    def _callback(self, symbol: SecurityCode, payload: object) -> None:
        try:
            records = _callback_records(symbol.to_wire(), payload)
            for record in records:
                received_at = self._clock.now()
                with self._lock:
                    self._sequence += 1
                    sequence = self._sequence
                tick = _normalize_tick(symbol, record, received_at, sequence)
                with self._lock:
                    if symbol not in self._by_symbol or self._sink is None:
                        return
                    current = self._latest.get(symbol)
                    if current is not None and tick.occurred_at < current.occurred_at:
                        continue
                    self._latest[symbol] = tick
                    sink = self._sink
                sink(tick)
        except Exception:
            # SDK callback exceptions must never escape into XtData.  The owner
            # sees this fail-closed latch on its next poll and halts new orders.
            with self._lock:
                self._fault = "QMT_TICK_CALLBACK_INVALID"

    def _require_started(self) -> None:
        with self._lock:
            if self._sink is None:
                raise MarketNotStartedError("QMT market gateway is not started")


def _callback_records(wire: str, payload: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(payload, Mapping) or set(payload) != {wire}:
        raise ValueError("QMT callback symbol set is invalid")
    raw = payload[wire]
    if isinstance(raw, Mapping):
        values: Sequence[object] = (raw,)
    elif isinstance(raw, (list, tuple)):
        values = raw
    else:
        raise ValueError("QMT callback record container is invalid")
    if not 1 <= len(values) <= _MAX_CALLBACK_BATCH:
        raise ValueError("QMT callback batch is invalid")
    if any(not isinstance(item, Mapping) for item in values):
        raise ValueError("QMT callback record is invalid")
    return tuple(cast(Mapping[str, object], item) for item in values)


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"QMT {field} is not numeric")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"QMT {field} is not finite")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"QMT {field} is invalid") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ValueError(f"QMT {field} is outside range")
    return result


def _whole(value: object, field: str) -> int:
    number = _decimal(value, field)
    integral = number.to_integral_value()
    if number != integral or integral > 2**63 - 1:
        raise ValueError(f"QMT {field} is not a bounded integer")
    return int(integral)


def _book(prices: object, quantities: object, multiplier: int, side: str) -> tuple[BookLevel, ...]:
    if not isinstance(prices, (list, tuple)) or not isinstance(quantities, (list, tuple)):
        raise ValueError(f"QMT {side} book is not a sequence")
    if len(prices) != len(quantities) or len(prices) > 50:
        raise ValueError(f"QMT {side} book depth is invalid")
    result: list[BookLevel] = []
    for raw_price, raw_quantity in zip(prices[:10], quantities[:10], strict=True):
        price = _decimal(raw_price, side + "Price")
        quantity = _whole(raw_quantity, side + "Vol")
        if price == 0 or quantity == 0:
            continue
        result.append(BookLevel(Price(price), Quantity(quantity * multiplier)))
    return tuple(result)


def _normalize_tick(
    symbol: SecurityCode,
    record: Mapping[str, object],
    received_at: ShanghaiTimestamp,
    sequence: int,
) -> MarketTick:
    if not _REQUIRED_FIELDS.issubset(record):
        raise ValueError("QMT tick is missing required fields")
    raw_time = record["time"]
    if type(raw_time) is not int or not 1_000_000_000_000 <= raw_time <= 9_999_999_999_999:
        raise ValueError("QMT tick time must be epoch milliseconds")
    occurred = ShanghaiTimestamp(datetime.fromtimestamp(raw_time / 1000, tz=SHANGHAI_TIMEZONE))
    if occurred > received_at:
        raise ValueError("QMT tick time is ahead of the local receive time")
    volume = _whole(record["volume"], "volume")
    pvolume = _whole(record["pvolume"], "pvolume")
    multiplier = 1
    if volume:
        if pvolume % volume or not 1 <= pvolume // volume <= 10_000:
            raise ValueError("QMT volume unit cannot be verified")
        multiplier = pvolume // volume
    elif pvolume:
        raise ValueError("QMT volume fields are inconsistent")
    turnover = _decimal(record["amount"], "amount").quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    # Current XtData full-tick callbacks expose the exchange phase in
    # ``openInt``. Some broker builds expose the same 11..23 phase codes in
    # ``stockStatus``; accept that only when openInt is absent. An unknown
    # status must never be interpreted as continuous trading.
    raw_status = record.get("openInt")
    if raw_status is None:
        raw_status = record.get("stockStatus")
    if type(raw_status) is not int or not 0 <= raw_status <= 255:
        raise ValueError("QMT trading status is invalid")
    return MarketTick(
        symbol=symbol,
        occurred_at=occurred,
        received_at=received_at,
        last_price=Price(_decimal(record["lastPrice"], "lastPrice", positive=True)),
        cumulative_volume=Quantity(pvolume),
        cumulative_turnover=Money(turnover),
        bids=_book(record["bidPrice"], record["bidVol"], multiplier, "bid"),
        asks=_book(record["askPrice"], record["askVol"], multiplier, "ask"),
        source_seq=sequence,
        trading_status=raw_status,
    )
