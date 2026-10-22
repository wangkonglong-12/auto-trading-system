from __future__ import annotations

from datetime import datetime

from auto_trading_system.domain import Money, Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.market import MarketTick, MockMarketGateway
from auto_trading_system.runtime.plan_market import PlanMarketRuntime
from auto_trading_system.testing.v31_scenarios import example_plan


def tick(symbol: str) -> MarketTick:
    at = ShanghaiTimestamp(datetime.fromisoformat("2026-09-07T14:40:01+08:00"))
    return MarketTick(
        symbol=SecurityCode.parse(symbol),
        occurred_at=at,
        received_at=at,
        last_price=Price.parse("10"),
        cumulative_volume=Quantity(100_000),
        cumulative_turnover=Money.parse("1000000"),
        source_seq=1,
    )


def test_plan_runtime_automatically_reconciles_plan_symbols() -> None:
    market = MockMarketGateway(max_subscriptions=15)
    runtime = PlanMarketRuntime(market)
    first = example_plan()

    runtime.reconcile(first)
    assert runtime.status()["symbols"] == ["000001.SZ"]
    assert market.subscription_count == 1
    assert market.publish(tick("000001.SZ"))
    runtime._drain()
    assert runtime.status()["latest_quote_count"] == 1

    changed = first.model_copy(
        update={
            "plan_id": "simulation-plan-2",
            "allocations": (
                first.allocations[0].model_copy(
                    update={"allocation_id": "simulation-allocation-2", "symbol": "600000.SH"}
                ),
            ),
        }
    )
    runtime.reconcile(changed)
    assert runtime.status()["symbols"] == ["600000.SH"]
    assert runtime.status()["latest_quote_count"] == 0
    assert market.subscription_count == 1

    runtime.reconcile(None)
    assert runtime.status()["state"] == "WAITING_PLAN"
    assert market.subscription_count == 0
    runtime.stop()


def test_exit_symbols_remain_subscribed_before_entry_plan_arrives() -> None:
    market = MockMarketGateway(max_subscriptions=15)
    runtime = PlanMarketRuntime(market)
    runtime.reconcile(None, ("600000.SH", "600000.SH"))
    assert runtime.status()["symbols"] == ["600000.SH"]
    assert market.subscription_count == 1
    runtime.reconcile(example_plan(), ("600000.SH",))
    assert runtime.status()["symbols"] == ["600000.SH", "000001.SZ"]
    runtime.stop()
