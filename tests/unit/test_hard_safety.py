from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from typing import Any, cast

import pytest

from auto_trading_system.domain import Money, Ratio, SecurityCode
from auto_trading_system.risk import (
    ActiveOrderSlot,
    HardSafetyDecision,
    HardSafetyDecisionOutcome,
    HardSafetyEvaluator,
    HardSafetyFacts,
    HardSafetyLimits,
    HardSafetyOrder,
    HardSafetyRuleOutcome,
    HardSafetyRuleResult,
    HardSafetySide,
    SymbolPositionValue,
)


def limits() -> HardSafetyLimits:
    return HardSafetyLimits(
        "WINDOWS_HARD_SAFETY_V1",
        3,
        Ratio.parse("0.60"),
        Ratio.parse("0.20"),
        2,
        1,
        Money.parse("5000"),
        Money.parse("10000"),
        Decimal("1.5"),
        30,
    )


def order(*, side: HardSafetySide = HardSafetySide.BUY) -> HardSafetyOrder:
    return HardSafetyOrder(
        "ord_01K43HARD000000001",
        "acct_demo_001",
        SecurityCode.parse("000001.SZ"),
        side,
        Decimal("4000"),
        Money.parse("4500"),
    )


def facts() -> HardSafetyFacts:
    position = SymbolPositionValue(SecurityCode.parse("600000.SH"), Money.parse("10000"))
    return HardSafetyFacts(
        "acct_demo_001",
        True,
        True,
        Money.parse("100000"),
        Money.parse("10000"),
        (position,),
        (),
        Money.parse("1000"),
        Decimal("0.5"),
        Decimal("1"),
    )


def evaluate(
    proposed: HardSafetyOrder | None = None,
    current: HardSafetyFacts | None = None,
    configured_limits: HardSafetyLimits | None = None,
) -> HardSafetyDecision:
    return HardSafetyEvaluator(configured_limits or limits()).evaluate(
        proposed or order(),
        current or facts(),
        decision_id="risk_hard_01K43HARD000000001",
    )


def rule(decision: HardSafetyDecision, code: str) -> HardSafetyRuleResult:
    values = [value for value in decision.rules if value.rule_code == code]
    assert len(values) == 1
    return values[0]


def test_safe_buy_is_allowed_with_all_local_limits_evidenced() -> None:
    decision = evaluate()

    assert decision.decision is HardSafetyDecisionOutcome.ALLOW
    assert len(decision.rules) == 12
    assert all(value.outcome is HardSafetyRuleOutcome.PASS for value in decision.rules)
    assert decision.to_event_payload() == {
        "decision_id": "risk_hard_01K43HARD000000001",
        "scope": "ORDER",
        "subject_id": "ord_01K43HARD000000001",
        "decision": "ALLOW",
        "policy_version": "WINDOWS_HARD_SAFETY_V1",
        "rules": [value.to_mapping() for value in decision.rules],
    }


@pytest.mark.parametrize(
    ("code", "order_change", "facts_change", "expected"),
    [
        (
            "HARD_FACTS_AVAILABLE",
            None,
            lambda value: replace(value, broker_facts_available=False),
            "BROKER=UNKNOWN;MARKET=AVAILABLE",
        ),
        (
            "HARD_FACTS_AVAILABLE",
            None,
            lambda value: replace(value, market_data_available=False),
            "BROKER=AVAILABLE;MARKET=UNKNOWN",
        ),
        (
            "HARD_ACCOUNT_MATCH",
            lambda value: replace(value, account_id="acct_other_001"),
            None,
            "acct_other_001",
        ),
        (
            "HARD_MARKET_FRESHNESS",
            None,
            lambda value: replace(value, tick_age_seconds=Decimal("1.501")),
            "1.501",
        ),
        (
            "HARD_CLOCK_SKEW",
            None,
            lambda value: replace(value, absolute_clock_skew_seconds=Decimal("30.1")),
            "30.1",
        ),
        (
            "HARD_ACTIVE_ORDER_COUNT",
            None,
            lambda value: replace(
                value,
                active_orders=(
                    ActiveOrderSlot(
                        "ord_active_other_00000001",
                        SecurityCode.parse("000002.SZ"),
                        HardSafetySide.BUY,
                        Money.parse("1000"),
                    ),
                    ActiveOrderSlot(
                        "ord_active_other_00000002",
                        SecurityCode.parse("000003.SZ"),
                        HardSafetySide.SELL,
                        Money.parse("1000"),
                    ),
                ),
            ),
            "3",
        ),
        (
            "HARD_SYMBOL_SIDE_ACTIVE_ORDER",
            None,
            lambda value: replace(
                value,
                active_orders=(
                    ActiveOrderSlot(
                        "ord_active_same_000000001",
                        SecurityCode.parse("000001.SZ"),
                        HardSafetySide.BUY,
                        Money.parse("1000"),
                    ),
                ),
            ),
            "2",
        ),
        (
            "HARD_SINGLE_ORDER_NOTIONAL",
            lambda value: replace(
                value,
                proposed_notional=Decimal("5000.01"),
                plan_max_notional=Money.parse("7000"),
            ),
            None,
            "5000.01",
        ),
        (
            "HARD_DAILY_BUY_NOTIONAL",
            None,
            lambda value: replace(value, daily_buy_notional=Money.parse("6000.01")),
            "10000.01",
        ),
    ],
    ids=[
        "broker-unknown",
        "market-unknown",
        "account",
        "tick-age",
        "clock-skew",
        "active-orders",
        "same-symbol-side",
        "single-order",
        "daily-buy",
    ],
)
def test_common_hard_safety_failures_reject_with_rule_evidence(
    code: str,
    order_change: Any,
    facts_change: Any,
    expected: str,
) -> None:
    proposed = order()
    current = facts()
    if order_change is not None:
        proposed = order_change(proposed)
    if facts_change is not None:
        current = facts_change(current)

    decision = evaluate(proposed, current)

    assert decision.decision is HardSafetyDecisionOutcome.REJECT
    failed = rule(decision, code)
    assert failed.outcome is HardSafetyRuleOutcome.FAIL
    assert failed.observed_value == expected


def test_projected_position_count_blocks_a_new_symbol_but_not_an_existing_symbol() -> None:
    positions = tuple(
        SymbolPositionValue(SecurityCode.parse(f"{number:06d}.SZ"), Money.parse("1000"))
        for number in range(2, 5)
    )
    crowded = replace(
        facts(),
        total_position_value=Money.parse("3000"),
        positions=positions,
    )
    new_symbol = evaluate(current=crowded)
    existing = evaluate(
        replace(order(), symbol=positions[0].symbol),
        crowded,
    )

    assert rule(new_symbol, "HARD_PROJECTED_POSITION_COUNT").outcome is HardSafetyRuleOutcome.FAIL
    assert rule(existing, "HARD_PROJECTED_POSITION_COUNT").outcome is HardSafetyRuleOutcome.PASS


def test_projected_total_and_single_position_exposure_are_independent() -> None:
    total_position = SymbolPositionValue(SecurityCode.parse("600000.SH"), Money.parse("59000"))
    total = evaluate(
        current=replace(
            facts(),
            total_position_value=Money.parse("59000"),
            positions=(total_position,),
        )
    )
    same_symbol = SymbolPositionValue(order().symbol, Money.parse("17000"))
    single = evaluate(
        current=replace(
            facts(),
            total_position_value=Money.parse("17000"),
            positions=(same_symbol,),
        )
    )

    assert rule(total, "HARD_PROJECTED_TOTAL_EXPOSURE").outcome is HardSafetyRuleOutcome.FAIL
    assert rule(single, "HARD_PROJECTED_SINGLE_EXPOSURE").outcome is HardSafetyRuleOutcome.FAIL


def test_active_buy_remaining_notional_is_reserved_in_projected_exposure() -> None:
    total_position = SymbolPositionValue(SecurityCode.parse("600000.SH"), Money.parse("54000"))
    pending_other = ActiveOrderSlot(
        "ord_pending_other_00000001",
        SecurityCode.parse("000002.SZ"),
        HardSafetySide.BUY,
        Money.parse("3000"),
    )
    total = evaluate(
        current=replace(
            facts(),
            total_position_value=Money.parse("54000"),
            positions=(total_position,),
            active_orders=(pending_other,),
        )
    )
    current_symbol = SymbolPositionValue(order().symbol, Money.parse("15000"))
    pending_same = ActiveOrderSlot(
        "ord_pending_same_000000001",
        order().symbol,
        HardSafetySide.BUY,
        Money.parse("2000"),
    )
    single = evaluate(
        current=replace(
            facts(),
            total_position_value=Money.parse("15000"),
            positions=(current_symbol,),
            active_orders=(pending_same,),
        )
    )

    assert rule(total, "HARD_PROJECTED_TOTAL_EXPOSURE").observed_value == "0.61"
    assert rule(total, "HARD_PROJECTED_TOTAL_EXPOSURE").outcome is HardSafetyRuleOutcome.FAIL
    assert rule(single, "HARD_PROJECTED_SINGLE_EXPOSURE").observed_value == "0.21"
    assert rule(single, "HARD_PROJECTED_SINGLE_EXPOSURE").outcome is HardSafetyRuleOutcome.FAIL


def test_active_client_order_id_cannot_be_submitted_again() -> None:
    duplicate = ActiveOrderSlot(
        order().client_order_id,
        SecurityCode.parse("000002.SZ"),
        HardSafetySide.SELL,
        Money.parse("1000"),
    )

    decision = evaluate(current=replace(facts(), active_orders=(duplicate,)))

    assert rule(decision, "HARD_CLIENT_ORDER_ID_UNIQUE").outcome is HardSafetyRuleOutcome.FAIL


def test_sell_keeps_common_safety_checks_and_bypasses_buy_exposure_rules() -> None:
    oversized = SymbolPositionValue(order().symbol, Money.parse("90000"))
    decision = evaluate(
        order(side=HardSafetySide.SELL),
        replace(
            facts(),
            total_position_value=Money.parse("90000"),
            positions=(oversized,),
            daily_buy_notional=Money.parse("10000"),
        ),
    )

    assert decision.decision is HardSafetyDecisionOutcome.ALLOW
    for code in (
        "HARD_PROJECTED_POSITION_COUNT",
        "HARD_PROJECTED_TOTAL_EXPOSURE",
        "HARD_PROJECTED_SINGLE_EXPOSURE",
        "HARD_DAILY_BUY_NOTIONAL",
    ):
        value = rule(decision, code)
        assert value.outcome is HardSafetyRuleOutcome.PASS
        assert value.observed_value == "NOT_APPLICABLE"


def test_all_numeric_boundaries_are_inclusive() -> None:
    proposed = replace(
        order(),
        proposed_notional=Decimal("5000"),
        plan_max_notional=Money.parse("5000"),
    )
    position = SymbolPositionValue(proposed.symbol, Money.parse("15000"))
    current = replace(
        facts(),
        total_position_value=Money.parse("55000"),
        positions=(
            position,
            SymbolPositionValue(SecurityCode.parse("600000.SH"), Money.parse("40000")),
        ),
        daily_buy_notional=Money.parse("5000"),
        tick_age_seconds=Decimal("1.5"),
        absolute_clock_skew_seconds=Decimal("30"),
    )

    decision = evaluate(proposed, current)

    assert decision.decision is HardSafetyDecisionOutcome.ALLOW
    assert rule(decision, "HARD_PROJECTED_TOTAL_EXPOSURE").observed_value == "0.6"
    assert rule(decision, "HARD_PROJECTED_SINGLE_EXPOSURE").observed_value == "0.2"


def test_plan_max_notional_can_only_tighten_the_local_limit() -> None:
    proposed = replace(
        order(),
        proposed_notional=Decimal("4000"),
        plan_max_notional=Money.parse("3999.99"),
    )

    decision = evaluate(proposed)

    failed = rule(decision, "HARD_SINGLE_ORDER_NOTIONAL")
    assert failed.outcome is HardSafetyRuleOutcome.FAIL
    assert failed.limit_value == "3999.99"


def test_facts_enforce_internal_consistency_and_unique_broker_identities() -> None:
    with pytest.raises(ValueError, match="sum"):
        replace(facts(), total_position_value=Money.parse("9999"))
    with pytest.raises(ValueError, match="position symbols"):
        replace(facts(), positions=facts().positions * 2, total_position_value=Money.parse("20000"))
    active = ActiveOrderSlot(
        "ord_duplicate_active_000001",
        SecurityCode.parse("000002.SZ"),
        HardSafetySide.BUY,
        Money.parse("1000"),
    )
    with pytest.raises(ValueError, match="active order ids"):
        replace(facts(), active_orders=(active, active))
    with pytest.raises(ValueError, match="tick_age_seconds"):
        replace(facts(), tick_age_seconds=Decimal("NaN"))


@pytest.mark.parametrize(
    "invalid",
    [
        lambda value: replace(value, max_positions=True),
        lambda value: replace(value, max_active_orders=4),
        lambda value: replace(value, active_order_per_symbol_side=True),
        lambda value: replace(value, hard_max_exposure=Ratio.parse("0")),
        lambda value: replace(
            value,
            max_single_position_exposure=Ratio.parse("0.7"),
        ),
        lambda value: replace(value, max_tick_age_seconds=Decimal("0")),
        lambda value: replace(value, max_clock_skew_seconds=31),
        lambda value: replace(
            value,
            max_single_order_notional=Money.parse("10000.01"),
        ),
    ],
    ids=[
        "bool-positions",
        "active-count",
        "per-symbol",
        "zero-exposure",
        "single-exposure",
        "tick-age",
        "clock-skew",
        "order-above-daily",
    ],
)
def test_hard_limits_reject_invalid_configuration(invalid: Any) -> None:
    with pytest.raises(ValueError):
        invalid(limits())


def test_decision_is_immutable_and_summary_cannot_disagree() -> None:
    decision = evaluate()
    with pytest.raises(FrozenInstanceError):
        decision.policy_version = "CHANGED"  # type: ignore[misc]
    with pytest.raises(ValueError, match="agree"):
        replace(decision, decision=HardSafetyDecisionOutcome.REJECT)


def test_evaluator_rejects_invalid_boundary_types() -> None:
    with pytest.raises(ValueError, match="limits"):
        HardSafetyEvaluator(cast(HardSafetyLimits, object()))
    evaluator = HardSafetyEvaluator(limits())
    with pytest.raises(ValueError, match="order"):
        evaluator.evaluate(
            cast(HardSafetyOrder, object()),
            facts(),
            decision_id="risk_hard_01K43HARD000000001",
        )
    with pytest.raises(ValueError, match="facts"):
        evaluator.evaluate(
            order(),
            cast(HardSafetyFacts, object()),
            decision_id="risk_hard_01K43HARD000000001",
        )
