"""Repeatable local performance acceptance for the callback hot path."""

from __future__ import annotations

import argparse
import json
import math
import time
import tracemalloc
from dataclasses import asdict, dataclass
from datetime import time as wall_time

from auto_trading_system.domain import (
    Money,
    Price,
    Quantity,
    SecurityCode,
    SystemClock,
    TradingDate,
)
from auto_trading_system.gateways.market import MarketTick, MockMarketGateway
from auto_trading_system.runtime import (
    BoundedEventIngress,
    PublishOutcome,
    RuntimeEventSource,
)


@dataclass(frozen=True, slots=True)
class PerformanceThresholds:
    minimum_throughput_per_second: float = 5_000.0
    maximum_p95_latency_microseconds: float = 5_000.0
    maximum_peak_memory_mib: float = 64.0

    def __post_init__(self) -> None:
        for field_name, value in asdict(self).items():
            if not isinstance(value, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{field_name} must be a positive finite float")


@dataclass(frozen=True, slots=True)
class MarketIngressPerformance:
    symbol_count: int
    events: int
    accepted: int
    overflows: int
    drained: int
    elapsed_nanoseconds: int
    p95_latency_nanoseconds: int
    peak_memory_bytes: int

    def __post_init__(self) -> None:
        counts = (
            self.symbol_count,
            self.events,
            self.accepted,
            self.overflows,
            self.drained,
            self.elapsed_nanoseconds,
            self.p95_latency_nanoseconds,
            self.peak_memory_bytes,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("performance values must be non-negative integers")
        if self.symbol_count < 1 or self.events < 1 or self.elapsed_nanoseconds < 1:
            raise ValueError("performance sample must contain symbols, events, and elapsed time")
        if self.accepted + self.overflows != self.events or self.drained != self.accepted:
            raise ValueError("performance event accounting is inconsistent")

    @property
    def throughput_per_second(self) -> float:
        return self.events * 1_000_000_000 / self.elapsed_nanoseconds

    @property
    def p95_latency_microseconds(self) -> float:
        return self.p95_latency_nanoseconds / 1_000

    @property
    def peak_memory_mib(self) -> float:
        return self.peak_memory_bytes / (1024 * 1024)

    def passes(self, thresholds: PerformanceThresholds) -> bool:
        if not isinstance(thresholds, PerformanceThresholds):
            raise ValueError("thresholds must be PerformanceThresholds")
        return (
            self.overflows == 0
            and self.throughput_per_second >= thresholds.minimum_throughput_per_second
            and self.p95_latency_microseconds <= thresholds.maximum_p95_latency_microseconds
            and self.peak_memory_mib <= thresholds.maximum_peak_memory_mib
        )

    def to_mapping(self, thresholds: PerformanceThresholds) -> dict[str, object]:
        return {
            "symbol_count": self.symbol_count,
            "events": self.events,
            "accepted": self.accepted,
            "overflows": self.overflows,
            "drained": self.drained,
            "elapsed_milliseconds": round(self.elapsed_nanoseconds / 1_000_000, 3),
            "throughput_per_second": round(self.throughput_per_second, 3),
            "p95_latency_microseconds": round(self.p95_latency_microseconds, 3),
            "peak_memory_mib": round(self.peak_memory_mib, 3),
            "thresholds": asdict(thresholds),
            "passed": self.passes(thresholds),
        }


def run_market_ingress_performance(
    *,
    symbol_count: int = 15,
    events_per_symbol: int = 2_000,
) -> MarketIngressPerformance:
    if type(symbol_count) is not int or not 1 <= symbol_count <= 50:
        raise ValueError("symbol_count must be between 1 and 50")
    if type(events_per_symbol) is not int or not 1 <= events_per_symbol <= 100_000:
        raise ValueError("events_per_symbol must be between 1 and 100000")
    event_count = symbol_count * events_per_symbol
    ingress = BoundedEventIngress[MarketTick](
        normal_capacity=event_count,
        critical_capacity=64,
        monotonic_clock=SystemClock(),
    )
    accepted = 0
    overflows = 0

    def publish_to_ingress(tick: MarketTick) -> None:
        nonlocal accepted, overflows
        outcome = ingress.publish(tick, source=RuntimeEventSource.MARKET)
        if outcome is PublishOutcome.ACCEPTED:
            accepted += 1
        else:
            overflows += 1

    market = MockMarketGateway(max_subscriptions=symbol_count)
    market.start(publish_to_ingress)
    trading_date = TradingDate.parse("2026-09-04")
    ticks = tuple(
        MarketTick(
            symbol=SecurityCode.parse(f"{600000 + index:06d}.SH"),
            occurred_at=trading_date.at(wall_time(14, 35)),
            received_at=trading_date.at(wall_time(14, 35, 0, 1_000)),
            last_price=Price.parse("10.01"),
            cumulative_volume=Quantity(10_000),
            cumulative_turnover=Money.parse("100100.00"),
            source_seq=1,
        )
        for index in range(symbol_count)
    )
    for tick in ticks:
        market.subscribe(tick.symbol)

    durations: list[int] = []
    tracemalloc.start()
    started = time.perf_counter_ns()
    try:
        for _ in range(events_per_symbol):
            for tick in ticks:
                call_started = time.perf_counter_ns()
                selected = market.publish(tick)
                durations.append(time.perf_counter_ns() - call_started)
                if not selected:
                    raise RuntimeError("subscribed market callback was not selected")
        elapsed = max(1, time.perf_counter_ns() - started)
        _, peak_memory = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    ingress.close()
    drained = 0
    while ingress.take_blocking() is not None:
        drained += 1
    ordered_durations = sorted(durations)
    p95_index = max(0, math.ceil(len(ordered_durations) * 0.95) - 1)
    return MarketIngressPerformance(
        symbol_count=symbol_count,
        events=event_count,
        accepted=accepted,
        overflows=overflows,
        drained=drained,
        elapsed_nanoseconds=elapsed,
        p95_latency_nanoseconds=ordered_durations[p95_index],
        peak_memory_bytes=peak_memory,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the market callback ingress boundary.")
    parser.add_argument("--symbols", type=int, default=15)
    parser.add_argument("--events-per-symbol", type=int, default=2_000)
    parser.add_argument("--minimum-throughput", type=float, default=5_000.0)
    parser.add_argument("--maximum-p95-microseconds", type=float, default=5_000.0)
    parser.add_argument("--maximum-peak-memory-mib", type=float, default=64.0)
    args = parser.parse_args(argv)
    thresholds = PerformanceThresholds(
        minimum_throughput_per_second=args.minimum_throughput,
        maximum_p95_latency_microseconds=args.maximum_p95_microseconds,
        maximum_peak_memory_mib=args.maximum_peak_memory_mib,
    )
    result = run_market_ingress_performance(
        symbol_count=args.symbols,
        events_per_symbol=args.events_per_symbol,
    )
    print(json.dumps(result.to_mapping(thresholds), ensure_ascii=False, separators=(",", ":")))
    return 0 if result.passes(thresholds) else 1


if __name__ == "__main__":  # pragma: no cover - exercised by acceptance script
    raise SystemExit(main())
