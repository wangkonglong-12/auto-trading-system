from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from auto_trading_system.apps.trade_runtime.bootstrap import (
    build_hard_safety_limits,
    build_order_execution_guard,
)
from auto_trading_system.config import RuntimeConfig
from auto_trading_system.domain import (
    Money,
    Price,
    Quantity,
    Ratio,
    SecurityCode,
    ShanghaiTimestamp,
)
from auto_trading_system.execution import (
    CompositeOrderExecutionGuard,
    HardSafetyOrderExecutionGuard,
    HardSafetyViolation,
)
from auto_trading_system.journal import IntentSide, OrderIntentRecord
from auto_trading_system.risk import (
    HardSafetyDecision,
    HardSafetyEvaluator,
    HardSafetyFacts,
    HardSafetyLimits,
)
from auto_trading_system.runtime import RuntimeState


def settings(tmp_path: Path) -> RuntimeConfig:
    path = Path(__file__).parents[2] / "config" / "v1" / "runtime.read-only.example.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["storage"]["root"] = str(tmp_path.resolve())
    return RuntimeConfig.model_validate(payload)


def intent(*, price: str = "12.34", max_notional: str = "5000") -> OrderIntentRecord:
    created_at = ShanghaiTimestamp.parse("2026-09-04T14:35:01+08:00")
    return OrderIntentRecord(
        "intent_01K43HARD000000001",
        "ord_01K43HARD000000001",
        "plan_01K43HARD20260904001",
        "acct_demo_001",
        created_at.trading_date,
        SecurityCode.parse("000001.SZ"),
        IntentSide.BUY,
        Quantity(100),
        Price.parse(price),
        Money.parse(max_notional),
        "ATS000000000000000000088",
        "EXEC_BALANCED_V1",
        created_at,
    )


def facts() -> HardSafetyFacts:
    return HardSafetyFacts(
        "acct_demo_001",
        True,
        True,
        Money.parse("100000"),
        Money.parse("0"),
        (),
        (),
        Money.parse("0"),
        Decimal("0.5"),
        Decimal("1"),
    )


def test_bootstrap_copies_hard_limits_and_composes_runtime_first(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    limits = build_hard_safety_limits(configured)
    calls = 0

    def supply() -> HardSafetyFacts:
        nonlocal calls
        calls += 1
        return facts()

    guard = build_order_execution_guard(
        configured,
        state_supplier=lambda: RuntimeState.HALT,
        hard_safety_facts_supplier=supply,
    )

    assert limits == HardSafetyLimits(
        "WINDOWS_HARD_SAFETY_V1",
        15,
        Ratio.parse("0.60"),
        Ratio.parse("0.05"),
        3,
        1,
        Money.parse("5000"),
        Money.parse("15000"),
        Decimal("1.5"),
        30,
    )
    with pytest.raises(RuntimeError, match="ORDER_EXECUTION_PERMISSION_DENIED"):
        guard(intent())
    assert calls == 0


def test_machine_local_limit_rejects_a_more_permissive_plan_and_is_stable(
    tmp_path: Path,
) -> None:
    guard = build_order_execution_guard(
        settings(tmp_path),
        state_supplier=lambda: RuntimeState.AUTO,
        hard_safety_facts_supplier=facts,
    )
    proposed = intent(price="60", max_notional="7000")

    with pytest.raises(HardSafetyViolation) as first:
        guard(proposed)
    with pytest.raises(HardSafetyViolation) as second:
        guard(proposed)

    assert first.value.code == "WINDOWS_HARD_SAFETY_REJECTED"
    assert first.value.decision == second.value.decision
    failed = [
        value
        for value in first.value.decision.rules
        if value.rule_code == "HARD_SINGLE_ORDER_NOTIONAL"
    ]
    assert len(failed) == 1
    assert failed[0].limit_value == "5000"


def test_hard_guard_fails_closed_on_invalid_fact_supplier() -> None:
    evaluator = HardSafetyEvaluator(
        HardSafetyLimits(
            "WINDOWS_HARD_SAFETY_V1",
            15,
            Ratio.parse("0.6"),
            Ratio.parse("0.05"),
            3,
            1,
            Money.parse("5000"),
            Money.parse("15000"),
            Decimal("1.5"),
            30,
        )
    )
    guard = HardSafetyOrderExecutionGuard(
        evaluator,
        facts_supplier=lambda: cast(HardSafetyFacts, object()),
    )

    with pytest.raises(ValueError, match="facts_supplier"):
        guard(intent())


def test_composite_guard_is_immutable_and_stops_at_first_failure() -> None:
    calls: list[str] = []

    def deny(_: OrderIntentRecord) -> None:
        calls.append("deny")
        raise RuntimeError("denied")

    def later(_: OrderIntentRecord) -> None:
        calls.append("later")

    guard = CompositeOrderExecutionGuard((deny, later))
    with pytest.raises(RuntimeError, match="denied"):
        guard(intent())
    assert calls == ["deny"]
    with pytest.raises(ValueError, match="guards"):
        CompositeOrderExecutionGuard(())
    with pytest.raises(ValueError, match="intent"):
        CompositeOrderExecutionGuard((later,))(cast(OrderIntentRecord, object()))


def test_violation_requires_a_typed_decision() -> None:
    with pytest.raises(ValueError, match="decision"):
        HardSafetyViolation(cast(HardSafetyDecision, object()))


def test_hard_guard_reloads_facts_on_every_call(tmp_path: Path) -> None:
    current = facts()
    guard = build_order_execution_guard(
        settings(tmp_path),
        state_supplier=lambda: RuntimeState.AUTO,
        hard_safety_facts_supplier=lambda: current,
    )
    guard(intent())
    current = replace(current, tick_age_seconds=Decimal("2"))

    with pytest.raises(HardSafetyViolation):
        guard(intent())
