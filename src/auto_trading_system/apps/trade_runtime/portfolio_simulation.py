"""Offline acceptance: two partial-buy/cancel rounds, query repair and lot replay."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from pydantic import JsonValue

from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.domain import Money, Price, Quantity
from auto_trading_system.gateways.broker.mock import (
    EventDeliveryMode,
    MockBrokerGateway,
    SubmitOutcome,
    SubmitScenario,
)
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.reconcile.position_lots import PortfolioProjection
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.testing.v31_scenarios import ScenarioClock


def _refresh(engine: EngineKernel) -> None:
    version = engine.snapshot().observation_version
    engine.refresh_account()
    wait_for(engine, lambda state: state.observation_version > version)


def run_portfolio_simulation(output_root: Path) -> dict[str, JsonValue]:
    directory = output_root.resolve() / ("portfolio-simulation-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    now = [DEMO_NOW]
    broker = MockBrokerGateway(ScenarioClock(DEMO_NOW))
    with open_offline_engine(directory, broker=broker, clock=lambda: now[0], max_orders=2) as rt:
        engine = rt.kernel
        engine.start_recovery()
        wait_for(engine, lambda s: s.state == "REVIEW_REQUIRED")
        rt.confirmed_arm()
        original_grant = engine.snapshot().grant_id
        for index in range(2):
            if index:
                now[0] += timedelta(seconds=6)
                _refresh(engine)
            broker.queue_submit(
                SubmitScenario(
                    outcome=SubmitOutcome.PARTIAL_FILL,
                    partial_fill_quantity=Quantity(50),
                    fill_price=Price.parse("10"),
                    fill_notional=Money.parse("500"),
                    delivery=EventDeliveryMode.DEFERRED if index else EventDeliveryMode.REVERSED,
                )
            )
            item = rt.buy()
            engine.submit(item, valid_until=monotonic() + 1)
            wait_for(engine, lambda s: s.submissions[-1].status == "SDK_ACCEPTED")
            if index:
                _refresh(engine)
                broker.flush_pending(reverse=True, duplicate=True)
            wait_for(engine, lambda _: engine.critical_idle)
            source = next(
                o for o in broker.query_orders() if o.client_order_id == item.client_order_id
            )
            cancel = item.model_copy(
                update={
                    "client_order_id": "ord_" + uuid4().hex,
                    "action_type": "CANCEL",
                    "original_client_order_id": item.client_order_id,
                    "original_order_id": source.broker_order_id,
                }
            )
            engine.submit(cancel, valid_until=monotonic() + 1)
            wait_for(engine, lambda s: s.submissions[-1].status == "SDK_ACCEPTED")
            wait_for(engine, lambda _: engine.critical_idle)
            assert engine.snapshot().grant_id == original_grant
            assert engine.snapshot().in_flight == ()
        state = engine.snapshot()
        assert len(state.lots) == 2 and sum(lot.remaining for lot in state.lots) == 100
        portfolio = PortfolioProjection(state.lots, frozenset(), (), ())
        today = portfolio.sellable(
            symbol="000001.SZ",
            strategy_id=rt.plan.payload.strategy_id,
            as_of=DEMO_NOW.date(),
            broker_sellable=100,
            reflected_order_ids=frozenset(),
        )
        eligible_date = state.lots[0].eligible_sell_date
        next_day = portfolio.sellable(
            symbol="000001.SZ",
            strategy_id=rt.plan.payload.strategy_id,
            as_of=eligible_date,
            broker_sellable=100,
            reflected_order_ids=frozenset(),
        )
        assert today == 0 and next_day == 100
        lot_ids = tuple(lot.lot_id for lot in state.lots)
    with open_offline_engine(
        directory, broker=broker, clock=lambda: now[0], max_orders=2
    ) as restarted:
        engine = restarted.kernel
        engine.start_recovery()
        state = wait_for(engine, lambda s: s.state == "REVIEW_REQUIRED")
        assert tuple(lot.lot_id for lot in state.lots) == lot_ids
        assert state.halted and state.grant_id is None
        result: dict[str, JsonValue] = {
            "mode": "OFFLINE_PORTFOLIO",
            "real_trading_enabled": False,
            "order_count": len(broker.query_orders()),
            "submission_records": 4,
            "lot_count": len(state.lots),
            "remaining_shares": sum(lot.remaining for lot in state.lots),
            "d0_sellable_policy_shares": today,
            "next_session_sellable_policy_shares": next_day,
            "sellable_policy_note": (
                "Offline projection with hypothetical broker sellable=100; no SELL dispatched"
            ),
            "query_repaired_missing_callbacks": True,
            "same_grant_across_rounds": True,
            "lot_ids_rebuilt": True,
            "restart_paused": state.halted,
            "output_directory": str(directory),
            "lots": [lot.model_dump(mode="json") for lot in state.lots],
        }
    (directory / "result.json").write_bytes(canonical_bytes(result))
    return result
