"""Production phase composition driven by a real journal and explicit Mock broker."""

from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from typing import Any, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_tail_phase_execution import phase

from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.apps.trade_runtime.exit_owner_simulation import ExitScenarioClock
from auto_trading_system.apps.trade_runtime.exit_simulation import fill_scenario
from auto_trading_system.domain import Money, Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.broker.mock import MockBrokerGateway
from auto_trading_system.gateways.market import BookLevel, MarketTick
from auto_trading_system.gateways.market.qmt_live import QmtInstrumentRule
from auto_trading_system.protocol.v2.api import PlanLookup
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import sign_plan
from auto_trading_system.runtime.engine_v2 import EngineLimits
from auto_trading_system.runtime.qmt_trading_runtime import QmtTradingRuntime
from auto_trading_system.strategies.trading_settings import TradingSettings


def test_live_driver_appends_restores_and_exports_shared_phase_owners(tmp_path: Path) -> None:
    clock = ExitScenarioClock(DEMO_NOW)
    broker = MockBrokerGateway(clock)
    key = Ed25519PrivateKey.generate()
    payload, _ = phase("1440", ("000001.SZ",))
    settings = TradingSettings.model_validate(
        {
            "buy": {
                "signal_mode": "TAIL_EXCELLENT_3_PHASES",
                "max_daily_symbols": 1,
                "max_daily_buy_value": "2030",
                "max_symbol_buy_value": "1005",
                "max_order_value": "1005",
                "cancel_after_seconds": 10,
                "max_retries": 2,
            },
            "sell": None,
            "safety": {
                "max_daily_loss": "100",
                "max_positions": 5,
                "emergency_liquidation_enabled": False,
            },
            "advanced": {
                "max_order_quantity": 100,
                "max_chase_bps": 30,
                "max_spread_bps": 20,
                "quote_max_age_ms": 1000,
            },
        }
    )
    latest: dict[str, MarketTick] = {}
    market = SimpleNamespace(
        market=SimpleNamespace(
            instrument_rule=lambda _: QmtInstrumentRule(
                price_tick=Price.parse("0.01").amount,
                lower_price=Price.parse("9").amount,
                upper_price=Price.parse("11").amount,
                min_buy_quantity=100,
                buy_quantity_step=100,
            )
        ),
        latest=latest,
        plan_hash=None,
        failure_code=None,
        poll=lambda *_args, **_kwargs: None,
        status=lambda: {},
        stop=lambda: None,
    )

    def quote(symbol: str, seq: int, volume: int) -> None:
        at = ShanghaiTimestamp(clock.instant)
        latest[symbol] = MarketTick(
            symbol=SecurityCode.parse(symbol),
            occurred_at=at,
            received_at=at,
            last_price=Price.parse("10"),
            cumulative_volume=Quantity(volume),
            cumulative_turnover=Money.parse("1000000"),
            bids=(BookLevel(Price.parse("9.99"), Quantity(1000)),),
            asks=(BookLevel(Price.parse("10"), Quantity(1000)),),
            source_seq=seq,
            trading_status=13,
        )

    limits = EngineLimits(
        max_buy_value="2030",
        max_order_value="1005",
        max_positions=5,
        max_order_quantity=100,
        max_daily_symbols=1,
    )
    with open_offline_engine(
        tmp_path,
        broker=broker,
        clock=lambda: clock.instant,
        plan_payload=payload,
        signing_key=key,
        limits=limits,
        tail_phases=True,
    ) as runtime:

        def driver() -> QmtTradingRuntime:
            return QmtTradingRuntime(
                cast(Any, market),
                settings,
                entry_install=next(p for p in payload.policy_bundle if p.component == "entry"),
                rulebook=payload.rulebook_version,
                clock=lambda: clock.instant,
            )

        live = driver()

        def pump(predicate: Any) -> None:
            deadline = monotonic() + 5
            while monotonic() < deadline:
                live.poll(runtime.kernel)
                assert live.failure_code is None
                if predicate():
                    return
                sleep(0.001)
            raise AssertionError(live.status())

        quote("000001.SZ", 1, 1000)
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        pump(lambda: live.owner is not None and live.owner.gate.ready)
        runtime.confirmed_arm(duration_seconds=900)
        clock.advance(60)
        quote("000001.SZ", 2, 100000)
        broker.queue_submit(fill_scenario(100, full=True))
        pump(lambda: bool(broker.query_trades()))
        first_policy = cast(Any, live.owner).gate.policy
        assert live.status()["entry_targets"]

        second, _ = phase("1445", ("000001.SZ", "600000.SH"))
        clock.advance(240)
        signed = sign_plan(second, "offline-ephemeral-key", key)
        body = canonical_bytes(signed.model_dump(mode="json"))
        lookup = PlanLookup(
            account_id=second.account_id,
            strategy_id=second.strategy_id,
            trade_date=second.trade_date,
            plan_id=second.plan_id,
            state="PUBLISHED",
            state_version=2,
            etag=digest(body),
            plan=signed,
        )
        assert (
            runtime.kernel.apply(lookup, runtime.kernel.plans._files.put(body)).status == "LOADED"
        )
        quote("000001.SZ", 3, 200000)
        quote("600000.SH", 1, 1000)
        pump(
            lambda: len(live.phase_owners) == 2
            and all(o.gate.ready for o in live.phase_owners.values())
        )
        clock.advance(60)
        quote("600000.SH", 2, 100000)
        pump(
            lambda: any(
                o.gate.targets.get("600000.SH") is not None for o in live.phase_owners.values()
            )
        )
        assert len(broker.query_trades()) == 1
        assert any(
            dict(o.snapshot().progress).get("000001.SZ") == "DUPLICATE_SIGNAL"
            for o in live.phase_owners.values()
        )
        runtime.kernel.emergency_halt("TEST_RESTART")
        for _ in range(5):
            live.poll(runtime.kernel)
        settled = list(live._activity)
        for _ in range(50):
            live.poll(runtime.kernel)
        assert list(live._activity) == settled
        assert len(broker.query_trades()) == 1
    with open_offline_engine(
        tmp_path,
        broker=broker,
        clock=lambda: clock.instant,
        plan_payload=payload,
        signing_key=key,
        limits=limits,
        tail_phases=True,
    ) as runtime:
        live = driver()
        latest.clear()
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        pump(
            lambda: len(live.phase_owners) == 2
            and all(o.gate.ready for o in live.phase_owners.values())
        )
        assert next(iter(live.phase_owners.values())).gate.policy == first_policy
        assert runtime.kernel.snapshot().halted and len(broker.query_trades()) == 1
        assert live.liquidation_preview() == ()
