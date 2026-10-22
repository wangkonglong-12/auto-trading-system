"""T1_EXIT_BASELINE_V3 pure state reducer for research and deterministic replay.

Outputs are target holdings, not broker orders. J1, local authorization, market
rules and confirmed sellable quantities remain the execution layer's job. A
persisted target can tighten but can never sell half of the remainder again.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from auto_trading_system.domain import Price
from auto_trading_system.strategies.sell_rules import SellRules


@dataclass(frozen=True, slots=True)
class ExitLot:
    lot_id: str
    initial_quantity: int
    acquired_on: date
    eligible_sell_date: date
    cost: Price
    previous_close: Price
    open_price: Price

    def __post_init__(self) -> None:
        if self.initial_quantity < 1 or self.eligible_sell_date <= self.acquired_on:
            raise ValueError(
                "lot requires positive initial quantity and a verified T+1 trading date"
            )


@dataclass(frozen=True, slots=True)
class ExitState:
    target_remaining: int
    fired: frozenset[str] = frozenset()
    profit_armed: bool = False
    peak_price: Price | None = None
    drawdown_since_ns: int | None = None
    last_received_ns: int | None = None
    path_complete: bool = True


@dataclass(frozen=True, slots=True)
class ExitObservation:
    wall_time: datetime
    received_ns: int
    price: Price
    fresh: bool
    path_complete: bool


@dataclass(frozen=True, slots=True)
class ExitDecision:
    state: ExitState
    reasons: tuple[str, ...]
    can_start_child_order: bool


def evaluate_exit(
    lot: ExitLot,
    state: ExitState,
    observation: ExitObservation,
    rules: SellRules | None = None,
) -> ExitDecision:
    now = observation.wall_time
    if now.utcoffset() != timedelta(hours=8):
        raise ValueError("exit timestamps must use UTC+08:00")
    if not 0 <= state.target_remaining <= lot.initial_quantity or observation.received_ns < 0:
        raise ValueError("exit state or monotonic observation is invalid")
    if now.date() < lot.eligible_sell_date or now.time() < time(9, 30):
        return ExitDecision(state, (), False)
    if now.time() >= time(14, 55, 30):
        return ExitDecision(state, (), False)
    if not observation.fresh:
        return ExitDecision(replace(state, drawdown_since_ns=None), (), False)
    if state.last_received_ns is not None and observation.received_ns <= state.last_received_ns:
        return ExitDecision(replace(state, path_complete=False, drawdown_since_ns=None), (), False)

    if rules is not None:
        fired = set(state.fired)
        configured_reasons: list[str] = []
        change_pct = (observation.price.amount / lot.cost.amount - 1) * 100
        for name, triggered in (
            ("TAKE_PROFIT", change_pct >= rules.take_profit_pct),
            ("STOP_LOSS", change_pct <= -rules.stop_loss_pct),
            ("SELL_BY_TIME", now.time() >= rules.sell_by),
        ):
            if triggered and name not in fired:
                fired.add(name)
                configured_reasons.append(name)
        return ExitDecision(
            replace(
                state,
                target_remaining=0 if configured_reasons else state.target_remaining,
                fired=frozenset(fired),
                last_received_ns=observation.received_ns,
                path_complete=state.path_complete and observation.path_complete,
            ),
            tuple(configured_reasons),
            True,
        )

    target = state.target_remaining
    fired = set(state.fired)
    reasons: list[str] = []
    day_return = observation.price.amount / lot.previous_close.amount - 1
    position_return = observation.price.amount / lot.cost.amount - 1
    path_complete = state.path_complete and observation.path_complete
    peak = state.peak_price
    armed = state.profit_armed
    drawdown_since = state.drawdown_since_ns
    if (
        state.last_received_ns is not None
        and observation.received_ns - state.last_received_ns > 5_000_000_000
    ):
        # A long callback silence does not prove sustained drawdown observations.
        drawdown_since = None

    def fire(rule: str, remaining: int) -> None:
        nonlocal target
        if rule not in fired:
            fired.add(rule)
            reasons.append(rule)
            target = min(target, remaining)

    if "OPEN_OBSERVED" not in fired:
        fired.add("OPEN_OBSERVED")
        if lot.open_price.amount / lot.previous_close.amount - 1 >= Decimal("0.03"):
            fire("OPEN_REDUCE", lot.initial_quantity // 2)
    if day_return >= Decimal("0.07"):
        fire("TAKE_PROFIT", 0)
    if path_complete:
        peak = max(peak or observation.price, observation.price)
        if day_return >= Decimal("0.035"):
            armed = True
            fired.add("PROFIT_ARM")
        if armed and 1 - observation.price.amount / peak.amount >= Decimal("0.01"):
            if drawdown_since is None:
                drawdown_since = observation.received_ns
            if observation.received_ns - drawdown_since >= 3_000_000_000:
                fire("PEAK_EXIT", 0)
        else:
            drawdown_since = None
    else:
        drawdown_since = None
    if now.time() >= time(10, 30) and "TIME_1030_OBSERVED" not in fired:
        fired.add("TIME_1030_OBSERVED")
        if day_return < Decimal("0.035"):
            fire("TIME_1030", 0)
    if now.time() >= time(11) and "TIME_1100_OBSERVED" not in fired:
        fired.add("TIME_1100_OBSERVED")
        if position_return <= Decimal("-0.02"):
            fire("TIME_1100", 0)
    if now.time() >= time(14, 30):
        fire("FINAL_EXIT", 0)
    return ExitDecision(
        ExitState(
            target,
            frozenset(fired),
            armed,
            peak,
            drawdown_since,
            observation.received_ns,
            path_complete,
        ),
        tuple(reasons),
        True,
    )
