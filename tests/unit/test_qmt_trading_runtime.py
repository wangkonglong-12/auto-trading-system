from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest

import auto_trading_system.runtime.qmt_trading_runtime as runtime_module
from auto_trading_system.domain import Money, Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.market import BookLevel, MarketTick
from auto_trading_system.gateways.market.qmt_live import QmtInstrumentRule
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.recorder.tick_archive import TickArchive
from auto_trading_system.runtime.engine_v2 import CN
from auto_trading_system.runtime.qmt_trading_runtime import QmtTradingRuntime
from auto_trading_system.strategies.trading_settings import TradingSettings
from auto_trading_system.testing.v31_scenarios import example_plan


def trading_settings(*, enabled: bool = True) -> TradingSettings:
    ceiling = "50000" if enabled else "0"
    symbol = "10000" if enabled else "0"
    order = "5000" if enabled else "0"
    return TradingSettings.model_validate_json(
        canonical_bytes(
            {
                "buy": {
                    "order_method": "ASK1_PROTECTED_LIMIT",
                    "max_daily_buy_value": ceiling,
                    "max_symbol_buy_value": symbol,
                    "max_order_value": order,
                    "cancel_after_seconds": 12,
                    "max_retries": 2,
                },
                "sell": {
                    "take_profit_pct": "5",
                    "stop_loss_pct": "3",
                    "sell_by": "14:30",
                },
                "safety": {
                    "max_daily_loss": "2000",
                    "max_positions": 5,
                    "emergency_liquidation_enabled": True,
                },
                "advanced": {
                    "max_order_quantity": 10000,
                    "max_chase_bps": 30,
                    "max_spread_bps": 20,
                    "quote_max_age_ms": 1000,
                },
            }
        )
    )


def market_tick(
    *, sequence: int = 1, volume: int = 100_000, second: int = 0, trading_status: int = 13
) -> MarketTick:
    at = ShanghaiTimestamp(datetime(2026, 9, 7, 14, 40 + second // 60, second % 60, tzinfo=CN))
    return MarketTick(
        symbol=SecurityCode.parse("000001.SZ"),
        occurred_at=at,
        received_at=at,
        last_price=Price.parse("10.126"),
        cumulative_volume=Quantity(volume),
        cumulative_turnover=Money.parse("1000000"),
        bids=(BookLevel(Price.parse("10.12"), Quantity(3000)),),
        asks=(BookLevel(Price.parse("10.13"), Quantity(2500)),),
        source_seq=sequence,
        trading_status=trading_status,
    )


@dataclass
class FakeInstrumentMarket:
    rule: QmtInstrumentRule | None = field(
        default_factory=lambda: QmtInstrumentRule(
            price_tick=Decimal("0.01"),
            lower_price=Decimal("9.11"),
            upper_price=Decimal("11.14"),
            min_buy_quantity=100,
            buy_quantity_step=100,
        )
    )

    def instrument_rule(self, _symbol: SecurityCode) -> QmtInstrumentRule:
        if self.rule is None:
            raise ValueError("instrument unavailable")
        return self.rule


class FakePlanMarket:
    def __init__(self) -> None:
        self.market = FakeInstrumentMarket()
        self.latest: dict[str, MarketTick] = {}
        self.failure_code: str | None = None
        self.polls: list[tuple[str, ...]] = []
        self.stopped = False

    def poll(self, _engine: object, *, extra_symbols: tuple[str, ...] = ()) -> None:
        self.polls.append(extra_symbols)

    def status(self) -> dict[str, Any]:
        return {
            "state": "SUBSCRIBED",
            "plan_hash": "sha256:" + "a" * 64,
            "subscription_count": len(self.latest),
            "symbols": sorted(self.latest),
            "latest_quote_count": len(self.latest),
            "failure_code": self.failure_code,
        }

    def stop(self) -> None:
        self.stopped = True


class FakeEngine:
    tail_history = None

    def __init__(self, loaded: object | None) -> None:
        self._loaded = loaded
        self.plans = SimpleNamespace(plans=self)
        self.halts: list[str] = []
        self.installs: list[tuple[object, object]] = []

    def loaded(self) -> object | None:
        return self._loaded

    def install_entry_runtime(self, allows: object, targets: object) -> None:
        self.installs.append((allows, targets))

    def emergency_halt(self, code: str) -> None:
        self.halts.append(code)

    def snapshot(self) -> object:
        return SimpleNamespace(orders=())


class FakeEntryOwner:
    instances: ClassVar[list[FakeEntryOwner]] = []

    def __init__(self, _engine: object, gate: object) -> None:
        self.gate = gate
        self.quotes: list[object] = []
        self.poll_count = 0
        self.instances.append(self)

    def quote(self, quote: object) -> None:
        self.quotes.append(quote)

    def poll(self) -> None:
        self.poll_count += 1

    def snapshot(self) -> object:
        return SimpleNamespace(
            state="READY",
            progress=(("000001.SZ", "WAITING_CAPACITY"),),
            targets=(
                SimpleNamespace(
                    symbol="000001.SZ",
                    target_quantity=300,
                    reason="ELIGIBLE",
                    decided_at=datetime(2026, 9, 7, 14, 40, 1, tzinfo=CN),
                ),
            ),
        )


class FakeExitOwner:
    def __init__(self) -> None:
        self.called = False

    def request_liquidation(self) -> None:
        self.called = True


def test_phase_restore_reuses_frozen_policy_without_new_market_reference() -> None:
    market = FakePlanMarket()
    market.latest["000001.SZ"] = market_tick()
    runtime = make_runtime(market)
    plan = example_plan()
    frozen = runtime._policy(plan)
    assert frozen is not None
    future: Future[bytes | None] = Future()
    future.set_result(
        canonical_bytes(
            {"plan": plan.model_dump(mode="json"), "policy": frozen.model_dump(mode="json")}
        )
    )
    engine = SimpleNamespace(journal=SimpleNamespace(request=lambda *_args, **_kwargs: future))
    market.latest.clear()
    assert runtime._phase_policy(cast(Any, engine), plan) is None
    assert runtime._phase_policy(cast(Any, engine), plan) == frozen


@pytest.mark.parametrize(
    "group,field,value",
    [
        ("buy", "cancel_after_seconds", 20),
        ("buy", "max_retries", 1),
        ("advanced", "max_chase_bps", 40),
        ("advanced", "max_spread_bps", 30),
        ("advanced", "quote_max_age_ms", 2000),
        ("advanced", "max_order_quantity", 20000),
    ],
)
def test_phase_restore_rejects_old_execution_settings(group: str, field: str, value: int) -> None:
    market = FakePlanMarket()
    market.latest["000001.SZ"] = market_tick()
    runtime = make_runtime(market)
    plan = example_plan()
    frozen = runtime._policy(plan)
    assert frozen is not None and runtime.settings is not None
    future: Future[bytes | None] = Future()
    future.set_result(
        canonical_bytes(
            {"plan": plan.model_dump(mode="json"), "policy": frozen.model_dump(mode="json")}
        )
    )
    engine = SimpleNamespace(journal=SimpleNamespace(request=lambda *_args, **_kwargs: future))
    previous = getattr(runtime.settings, group)
    runtime.settings = runtime.settings.model_copy(
        update={group: previous.model_copy(update={field: value})}
    )
    assert runtime._phase_policy(cast(Any, engine), plan) is None
    with pytest.raises(ValueError, match="execution settings differ"):
        runtime._phase_policy(cast(Any, engine), plan)


class FakeD1Owner(FakeExitOwner):
    instances: ClassVar[list[FakeD1Owner]] = []

    def __init__(self, _engine: object, gate: object) -> None:
        super().__init__()
        self.gate = gate
        self.quotes: list[object] = []
        self.poll_count = 0
        self.instances.append(self)

    def quote(self, quote: object) -> None:
        self.quotes.append(quote)
        cast(Any, self.gate).quotes[cast(Any, quote).symbol] = quote

    def poll(self) -> None:
        self.poll_count += 1

    def snapshot(self) -> object:
        return SimpleNamespace(state="LIQUIDATING" if self.called else "MONITORING")


def make_runtime(market: FakePlanMarket, *, enabled: bool = True) -> QmtTradingRuntime:
    plan = example_plan()
    entry = next(item for item in plan.policy_bundle if item.component == "entry")
    return QmtTradingRuntime(
        cast(Any, market),
        trading_settings(enabled=enabled),
        entry_install=entry,
        rulebook=plan.rulebook_version,
        clock=lambda: datetime(2026, 9, 7, 14, 40, 1, tzinfo=CN),
    )


def test_live_policy_uses_fresh_quote_and_broker_instrument_rules() -> None:
    market = FakePlanMarket()
    runtime = make_runtime(market)

    assert runtime._policy(example_plan()) is None
    market.latest["000001.SZ"] = market_tick()
    policy = runtime._policy(example_plan())

    assert policy is not None
    assert policy.cancel_after_seconds == 12 and policy.max_children == 3
    assert policy.max_chase_bps == 30 and policy.max_spread_bps == 20
    assert policy.quote_max_age_ms == 1000
    assert policy.instruments[0].max_order_quantity == 10000
    assert policy.instruments[0].reference_price == "10.126"
    assert policy.instruments[0].lower_price == "9.11"
    assert policy.instruments[0].upper_price == "11.14"
    assert policy.instruments[0].min_quantity == 100
    assert policy.instruments[0].source_hash.startswith("sha256:")

    disabled = make_runtime(market, enabled=False)
    assert disabled._policy(example_plan()) is None

    market.market.rule = QmtInstrumentRule(
        price_tick=Decimal("0.001"),
        lower_price=Decimal("9.111"),
        upper_price=Decimal("11.149"),
        min_buy_quantity=100,
        buy_quantity_step=100,
    )
    precise = runtime._policy(example_plan())
    assert precise is not None
    assert precise.instruments[0].price_tick == "0.001"
    assert precise.instruments[0].lower_price == "9.111"


def test_pending_manual_order_activity_keeps_stock_code_and_name() -> None:
    runtime = make_runtime(FakePlanMarket())
    engine = SimpleNamespace(
        _known={
            "manual-a": SimpleNamespace(
                symbol="003021.SZ", manual_binding=SimpleNamespace(name="兆威机电")
            )
        },
        snapshot=lambda: SimpleNamespace(
            orders=(SimpleNamespace(client_order_id="manual-a", order=None, phase="SUBMITTING"),)
        ),
    )
    runtime._observe_activity(cast(Any, engine))
    activity = cast(list[dict[str, object]], runtime.status()["recent_activity"])
    assert activity[0]["symbol"] == "003021.SZ"
    assert activity[0]["name"] == "兆威机电"
    assert activity[0]["context"] == "manual-a"


def test_live_policy_fails_closed_without_verified_qmt_rules() -> None:
    market = FakePlanMarket()
    market.latest["000001.SZ"] = market_tick()
    runtime = make_runtime(market)
    market.market.rule = None

    with pytest.raises(ValueError, match="instrument unavailable"):
        runtime._policy(example_plan())
    cast(Any, market).market = object()
    with pytest.raises(ValueError, match="instrument rules are unavailable"):
        runtime._policy(example_plan())


def test_live_poll_builds_one_owner_and_feeds_only_new_complete_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeEntryOwner.instances.clear()
    monkeypatch.setattr(runtime_module, "EntryStrategyOwner", FakeEntryOwner)
    plan = example_plan()
    loaded = SimpleNamespace(
        payload=plan,
        envelope=SimpleNamespace(payload_hash="sha256:" + "1" * 64),
    )
    engine = FakeEngine(loaded)
    market = FakePlanMarket()
    market.latest["000001.SZ"] = market_tick()
    runtime = make_runtime(market)

    runtime.poll(cast(Any, engine))

    owner = FakeEntryOwner.instances[0]
    assert len(engine.installs) == 1 and owner.poll_count == 1
    assert len(owner.quotes) == 1
    first = cast(Any, owner.quotes[0])
    assert first.recent_volume == 0 and first.path_complete is False

    runtime.poll(cast(Any, engine))
    assert len(owner.quotes) == 1 and owner.poll_count == 2

    market.latest["000001.SZ"] = market_tick(sequence=2, volume=101_000, second=30)
    runtime.poll(cast(Any, engine))
    second = cast(Any, owner.quotes[-1])
    assert second.recent_volume == 0 and second.path_complete is False

    market.latest["000001.SZ"] = market_tick(sequence=3, volume=102_500, second=60)
    runtime.poll(cast(Any, engine))
    third = cast(Any, owner.quotes[-1])
    assert third.recent_volume == 2500 and third.path_complete is True
    assert runtime.status()["driver_state"] == "READY"
    assert runtime.status()["entry_progress"] == [
        {"symbol": "000001.SZ", "state": "WAITING_CAPACITY"}
    ]
    activity = cast(list[dict[str, object]], runtime.status()["recent_activity"])
    assert any(item["category"] == "ENTRY_SIGNAL" for item in activity)
    assert any(item["category"] == "ENTRY_TARGET" for item in activity)

    # A long callback gap must not turn several minutes of cumulative volume
    # into a fictitious 60-second liquidity allowance.
    market.latest["000001.SZ"] = market_tick(sequence=4, volume=200_000, second=125)
    runtime.poll(cast(Any, engine))
    after_gap = cast(Any, owner.quotes[-1])
    assert after_gap.recent_volume == 0 and after_gap.path_complete is True
    market.latest["000001.SZ"] = market_tick(sequence=5, volume=200_100, second=126)
    runtime.poll(cast(Any, engine))
    assert cast(Any, owner.quotes[-1]).recent_volume == 100

    engine._loaded = SimpleNamespace(
        payload=plan,
        envelope=SimpleNamespace(payload_hash="sha256:" + "2" * 64),
    )
    runtime.poll(cast(Any, engine))
    assert engine.halts[-1] == "QMT_LIVE_ENTRY_DRIVER_FAILED"
    assert runtime.status()["driver_state"] == "FAILED"

    runtime.stop()
    assert market.stopped


def test_live_poll_marks_non_continuous_qmt_phase_untradable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeEntryOwner.instances.clear()
    monkeypatch.setattr(runtime_module, "EntryStrategyOwner", FakeEntryOwner)
    plan = example_plan()
    engine = FakeEngine(
        SimpleNamespace(
            payload=plan,
            envelope=SimpleNamespace(payload_hash="sha256:" + "1" * 64),
        )
    )
    market = FakePlanMarket()
    market.latest["000001.SZ"] = market_tick(trading_status=17)
    runtime = make_runtime(market)

    runtime.poll(cast(Any, engine))

    assert cast(Any, FakeEntryOwner.instances[0].quotes[0]).tradable is False


def test_failure_latch_and_strategy_only_liquidation_are_fail_closed() -> None:
    market = FakePlanMarket()
    runtime = make_runtime(market)
    engine = FakeEngine(None)
    runtime.failure_code = "QMT_TEST_FAILURE"

    runtime.poll(cast(Any, engine))
    assert engine.halts == ["QMT_TEST_FAILURE"]

    with pytest.raises(ValueError, match="NO_STRATEGY_POSITIONS"):
        runtime.request_liquidation()

    lot = SimpleNamespace(lot_id="lot-1", symbol="600000.SH", cost="1000.00")
    cast(Any, runtime).exit_gate = SimpleNamespace(context=SimpleNamespace(lots=(lot,)))
    exit_owner = FakeExitOwner()
    cast(Any, runtime).exit_owner = exit_owner

    preview = runtime.request_liquidation()
    assert preview == ({"lot_id": "lot-1", "symbol": "600000.SH", "cost": "1000.00"},)
    assert runtime.liquidation_requested and exit_owner.called


def test_live_poll_drives_exit_quotes_and_pending_liquidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeD1Owner.instances.clear()
    monkeypatch.setattr(runtime_module, "D1StrategyOwner", FakeD1Owner)
    market = FakePlanMarket()
    market.latest["000001.SZ"] = market_tick()
    lot = SimpleNamespace(lot_id="lot-1", symbol="000001.SZ", cost="1000.00")
    exit_gate = SimpleNamespace(context=SimpleNamespace(lots=(lot,)), quotes={})
    runtime = make_runtime(market, enabled=False)
    cast(Any, runtime).exit_gate = exit_gate
    runtime.liquidation_requested = True
    engine = FakeEngine(None)

    runtime.poll(cast(Any, engine))

    owner = FakeD1Owner.instances[0]
    assert market.polls == [("000001.SZ",)]
    assert owner.called and owner.poll_count == 1
    assert len(owner.quotes) == 1
    quote = cast(Any, owner.quotes[0])
    assert quote.symbol == "000001.SZ" and quote.bid_price == "10.12"
    assert runtime.status()["exit_state"] == "LIQUIDATING"

    runtime.poll(cast(Any, engine))
    assert len(owner.quotes) == 1 and owner.poll_count == 2


def test_restart_restores_volume_history_without_treating_it_as_live(tmp_path: Path) -> None:
    previous = TickArchive(tmp_path)
    previous.start()
    previous.append(market_tick(sequence=90, volume=100_000, second=0))
    previous.append(market_tick(sequence=91, volume=101_000, second=59))
    assert previous.close()

    archive = TickArchive(tmp_path)
    archive.start()
    market = FakePlanMarket()
    cast(Any, market).archive = archive
    live = market_tick(sequence=1, volume=102_000, second=60)
    market.latest["000001.SZ"] = live
    runtime = make_runtime(market)
    gate = SimpleNamespace(rules={"000001.SZ": SimpleNamespace(upper_price="11.14")})
    owner = FakeEntryOwner(None, gate)
    runtime.owner = cast(Any, owner)
    try:
        runtime._feed()
        archive.flush().result(2)  # Wait for the asynchronous history read off the owner loop.
        runtime._feed()
        assert len(runtime.history_windows["000001.SZ"].records) == 2
        assert len(runtime.volume_windows["000001.SZ"]) == 3
        assert not cast(Any, owner.quotes[-1]).path_complete
        assert market.latest["000001.SZ"] is live
        market.latest["000001.SZ"] = market_tick(sequence=2, volume=104_000, second=120)
        runtime._feed()
        quote = cast(Any, owner.quotes[-1])
        assert quote.path_complete and quote.recent_volume == 2000
    finally:
        assert archive.close()


def test_stalled_history_restore_has_a_deadline() -> None:
    market = FakePlanMarket()
    cast(Any, market).archive = SimpleNamespace(query=lambda *_a, **_k: Future())
    market.latest["000001.SZ"] = market_tick()
    runtime = make_runtime(market)
    runtime.owner = cast(Any, FakeEntryOwner(None, SimpleNamespace(rules={"000001.SZ": object()})))
    runtime._feed()
    runtime._history_deadlines["000001.SZ"] = 0
    with pytest.raises(RuntimeError, match="restore timed out"):
        runtime._feed()


def test_held_system_stock_stays_subscribed_without_entry_plan_or_sell_driver(
    tmp_path: Path,
) -> None:
    from auto_trading_system.gateways.market import MockMarketGateway
    from auto_trading_system.runtime.plan_market import PlanMarketRuntime

    gateway = MockMarketGateway()
    market = PlanMarketRuntime(gateway, archive=TickArchive(tmp_path))
    runtime = make_runtime(cast(Any, market))
    engine = FakeEngine(None)
    portfolio = SimpleNamespace(
        lots=(SimpleNamespace(symbol="000001.SZ", remaining=100),),
        quantities=lambda: {"000001.SZ": 100},
    )
    cast(Any, engine).exit_inputs = lambda: (portfolio, None, ())
    cast(Any, engine).disposable_market_symbols = lambda: frozenset()
    cast(Any, engine).poll = lambda: None
    cast(Any, engine).accepted = lambda: ()
    try:
        runtime.poll(cast(Any, engine))
        assert market.snapshot().symbols == ("000001.SZ",)
        assert gateway.subscription_count == 1
    finally:
        runtime.stop()


def test_same_stock_in_multiple_phases_does_not_generate_infinite_activity() -> None:
    runtime = make_runtime(FakePlanMarket())
    engine = SimpleNamespace(snapshot=lambda: SimpleNamespace(orders=()))
    owners = [
        SimpleNamespace(
            gate=SimpleNamespace(
                plan_hash="plan-" + phase,
                plan=SimpleNamespace(source=SimpleNamespace(decision_phase=phase)),
            ),
            snapshot=lambda state=state: SimpleNamespace(
                state="READY", progress=(("000001.SZ", state),), targets=()
            ),
        )
        for phase, state in [
            ("1440", "FINISHED"),
            ("1445", "DUPLICATE_SIGNAL"),
            ("1450", "DUPLICATE_SIGNAL"),
        ]
    ]
    for _ in range(200):
        for owner in owners:
            runtime.owner = cast(Any, owner)
            runtime._observe_activity(cast(Any, engine))
    signals = [e for e in runtime._activity if e["category"] == "ENTRY_SIGNAL"]
    assert len(signals) == 3
    assert {e["phase"] for e in signals} == {"1440", "1445", "1450"}
    runtime.failure_code = "TEST_ERROR"
    runtime._observe_activity(cast(Any, engine))
    assert runtime._activity[-1]["category"] == "ERROR"
