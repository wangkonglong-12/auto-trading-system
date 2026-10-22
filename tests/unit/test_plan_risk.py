from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from auto_trading_system.domain import Money, Ratio, SecurityCode
from auto_trading_system.protocol import calculate_plan_hash
from auto_trading_system.risk import (
    MarketLiquidity,
    PlanRiskCandidate,
    PlanRiskContext,
    PlanRiskDecision,
    PlanRiskDecisionOutcome,
    PlanRiskEvaluator,
    PlanRiskInputError,
    PlanRiskItem,
    PlanRiskPolicy,
    PlanRiskRuleResult,
    RiskRuleOutcome,
    parse_trade_plan_risk_candidate,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
READY_PLAN_PATH = PROJECT_ROOT / "contracts" / "v1" / "examples" / "trade-plan.ready.json"
NO_TRADE_PLAN_PATH = PROJECT_ROOT / "contracts" / "v1" / "examples" / "trade-plan.no-trade.json"


def plan_at(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def ready_plan() -> dict[str, Any]:
    return plan_at(READY_PLAN_PATH)


def update_hash(plan: dict[str, Any]) -> None:
    plan["plan_hash"] = calculate_plan_hash(plan)


def candidate() -> PlanRiskCandidate:
    return parse_trade_plan_risk_candidate(ready_plan())


def policy() -> PlanRiskPolicy:
    return PlanRiskPolicy(
        policy_version="PLAN_RISK_V1",
        strategy_id="TAIL_CLOSE",
        supported_algorithm_versions=frozenset({"tail_1431_v5_6_selection_eligibility_guard"}),
        allowed_entry_policies=frozenset({"TAIL_ENTRY_V1"}),
        allowed_exit_policies=frozenset({"T1_EXIT_V1"}),
        allowed_risk_policies=frozenset({"STOCK_RISK_V1"}),
        allowed_execution_policies=frozenset({"EXEC_BALANCED_V1"}),
        max_positions=15,
        max_total_exposure=Ratio.parse("0.60"),
        max_single_position_exposure=Ratio.parse("0.05"),
        max_participation_rate=Ratio.parse("0.05"),
        max_plan_buy_notional=Money.parse("20000"),
    )


def context() -> PlanRiskContext:
    return PlanRiskContext(
        account_id="acct_demo_001",
        net_asset=Money.parse("100000"),
        available_cash=Money.parse("20000"),
        current_exposure=Ratio.parse("0.10"),
        held_symbols=(SecurityCode.parse("600000.SH"),),
        liquidity=(MarketLiquidity(SecurityCode.parse("000001.SZ"), Money.parse("200000")),),
    )


def evaluate(
    value: PlanRiskCandidate | None = None,
    facts: PlanRiskContext | None = None,
    configured_policy: PlanRiskPolicy | None = None,
) -> PlanRiskDecision:
    return PlanRiskEvaluator(configured_policy or policy()).evaluate(
        value or candidate(),
        facts or context(),
        decision_id="risk_plan_01K43TAIL000000001",
    )


def rule(decision: PlanRiskDecision, code: str) -> PlanRiskRuleResult:
    matches = [value for value in decision.rules if value.rule_code == code]
    assert len(matches) == 1
    return matches[0]


def test_trade_plan_adapter_returns_strongly_typed_risk_candidate() -> None:
    value = candidate()

    assert value.plan_id == "plan_01K43TAIL20260903001"
    assert value.account_id == "acct_demo_001"
    assert value.target_exposure == Ratio.parse("0.30")
    assert value.declared_hard_max_exposure == Ratio.parse("0.60")
    assert value.declared_max_positions == 15
    assert value.items == (
        PlanRiskItem(
            SecurityCode.parse("000001.SZ"),
            Ratio.parse("0.05"),
            Money.parse("5000"),
            "TAIL_ENTRY_V1",
            "T1_EXIT_V1",
            "STOCK_RISK_V1",
            "EXEC_BALANCED_V1",
        ),
    )


def test_trade_plan_adapter_requires_valid_hash_and_ready_status() -> None:
    tampered = ready_plan()
    tampered["strategy_id"] = "OTHER"
    with pytest.raises(PlanRiskInputError) as bad_hash:
        parse_trade_plan_risk_candidate(tampered)
    assert bad_hash.value.code == "PLAN_HASH_MISMATCH"

    with pytest.raises(PlanRiskInputError) as not_ready:
        parse_trade_plan_risk_candidate(plan_at(NO_TRADE_PLAN_PATH))
    assert not_ready.value.code == "PLAN_NOT_READY"


@pytest.mark.parametrize(
    ("mutation", "field"),
    [
        (lambda value: value.__setitem__("portfolio", []), "portfolio"),
        (
            lambda value: cast(dict[str, Any], value["portfolio"]).__setitem__(
                "max_positions", True
            ),
            "portfolio.max_positions",
        ),
        (
            lambda value: cast(list[dict[str, Any]], value["items"])[0].__setitem__(
                "max_notional", 5000
            ),
            "items[0].max_notional",
        ),
        (
            lambda value: cast(list[dict[str, Any]], value["items"])[0].__setitem__(
                "execution_policy", "bad"
            ),
            "items[0]",
        ),
    ],
    ids=["portfolio", "max-positions-bool", "money-type", "policy-code"],
)
def test_trade_plan_adapter_rejects_invalid_risk_fields(
    mutation: Any,
    field: str,
) -> None:
    value = ready_plan()
    mutation(value)
    update_hash(value)

    with pytest.raises(PlanRiskInputError) as captured:
        parse_trade_plan_risk_candidate(value)

    assert captured.value.field == field


def test_valid_plan_is_allowed_with_complete_ordered_rules_and_event_payload() -> None:
    decision = evaluate()

    assert decision.decision is PlanRiskDecisionOutcome.ALLOW
    assert len(decision.rules) == 14
    assert all(value.outcome is RiskRuleOutcome.PASS for value in decision.rules)
    assert decision.to_event_payload() == {
        "decision_id": "risk_plan_01K43TAIL000000001",
        "scope": "PLAN",
        "subject_id": "plan_01K43TAIL20260903001",
        "decision": "ALLOW",
        "policy_version": "PLAN_RISK_V1",
        "rules": [value.to_mapping() for value in decision.rules],
    }


@pytest.mark.parametrize(
    ("code", "candidate_change", "context_change", "policy_change", "observed"),
    [
        (
            "PLAN_ACCOUNT_MATCH",
            None,
            lambda value: replace(value, account_id="acct_other_001"),
            None,
            "acct_demo_001",
        ),
        (
            "PLAN_STRATEGY_ID",
            lambda value: replace(value, strategy_id="OTHER_STRATEGY"),
            None,
            None,
            "OTHER_STRATEGY",
        ),
        (
            "PLAN_ALGORITHM_VERSION",
            lambda value: replace(value, algorithm_version="tail_other_v1"),
            None,
            None,
            "tail_other_v1",
        ),
        (
            "PLAN_DECLARED_POSITION_LIMIT",
            None,
            None,
            lambda value: replace(value, max_positions=14),
            "15",
        ),
        (
            "PLAN_DECLARED_EXPOSURE_LIMIT",
            None,
            None,
            lambda value: replace(
                value,
                max_total_exposure=Ratio.parse("0.50"),
            ),
            "0.6",
        ),
        (
            "PLAN_PROJECTED_POSITION_COUNT",
            None,
            lambda value: replace(
                value,
                held_symbols=tuple(
                    SecurityCode.parse(f"{number:06d}.SZ") for number in range(100, 115)
                ),
            ),
            None,
            "16",
        ),
        (
            "PLAN_CURRENT_EXPOSURE_HEADROOM",
            None,
            lambda value: replace(value, current_exposure=Ratio.parse("0.30")),
            None,
            "0.3",
        ),
        (
            "PLAN_BUY_NOTIONAL",
            None,
            lambda value: replace(value, available_cash=Money.parse("4999.99")),
            None,
            "5000",
        ),
    ],
    ids=[
        "account",
        "strategy",
        "algorithm",
        "declared-position-limit",
        "declared-exposure-limit",
        "projected-positions",
        "exposure-headroom",
        "plan-notional",
    ],
)
def test_plan_level_failures_reject_with_inspectable_rule(
    code: str,
    candidate_change: Any,
    context_change: Any,
    policy_change: Any,
    observed: str,
) -> None:
    value = candidate()
    facts = context()
    configured_policy = policy()
    if candidate_change is not None:
        value = candidate_change(value)
    if context_change is not None:
        facts = context_change(facts)
    if policy_change is not None:
        configured_policy = policy_change(configured_policy)

    decision = evaluate(value, facts, configured_policy)

    assert decision.decision is PlanRiskDecisionOutcome.REJECT
    failed = rule(decision, code)
    assert failed.outcome is RiskRuleOutcome.FAIL
    assert failed.observed_value == observed


def test_target_exposure_must_be_positive_and_within_policy() -> None:
    zero = evaluate(replace(candidate(), target_exposure=Ratio.parse("0")))
    above = evaluate(
        replace(candidate(), target_exposure=Ratio.parse("0.61")),
        context(),
        replace(policy(), max_total_exposure=Ratio.parse("0.60")),
    )

    assert rule(zero, "PLAN_TARGET_EXPOSURE").outcome is RiskRuleOutcome.FAIL
    assert rule(above, "PLAN_TARGET_EXPOSURE").outcome is RiskRuleOutcome.FAIL


def test_plan_declared_exposure_and_weight_sum_cannot_self_expand() -> None:
    original = candidate()
    zero_declared = evaluate(replace(original, declared_hard_max_exposure=Ratio.parse("0")))
    target_above_declared = evaluate(
        replace(original, declared_hard_max_exposure=Ratio.parse("0.20"))
    )
    overweight_item = replace(original.items[0], target_weight=Ratio.parse("0.31"))
    overweight = evaluate(replace(original, items=(overweight_item,)))

    assert rule(zero_declared, "PLAN_DECLARED_EXPOSURE_LIMIT").outcome is RiskRuleOutcome.FAIL
    assert rule(target_above_declared, "PLAN_TARGET_EXPOSURE").outcome is RiskRuleOutcome.FAIL
    assert rule(overweight, "PLAN_ITEM_WEIGHT_SUM").outcome is RiskRuleOutcome.FAIL


def test_policy_binding_mismatch_rejects_without_accepting_unknown_version() -> None:
    original = candidate()
    changed_item = replace(original.items[0], risk_policy="STOCK_RISK_V2")
    decision = evaluate(replace(original, items=(changed_item,)))

    failed = rule(decision, "PLAN_POLICY_BINDINGS")
    assert failed.outcome is RiskRuleOutcome.FAIL
    assert failed.observed_value == "MISMATCH_COUNT=1"


@pytest.mark.parametrize(
    ("item_change", "code", "expected_observed"),
    [
        (
            lambda value: replace(value, target_weight=Ratio.parse("0")),
            "SINGLE_POSITION_TARGET_EXPOSURE",
            "000001.SZ=0",
        ),
        (
            lambda value: replace(value, target_weight=Ratio.parse("0.06")),
            "SINGLE_POSITION_TARGET_EXPOSURE",
            "000001.SZ=0.06",
        ),
        (
            lambda value: replace(value, max_notional=Money.parse("5000.01")),
            "SINGLE_POSITION_NOTIONAL_EXPOSURE",
            "000001.SZ=0.0500001",
        ),
    ],
    ids=["zero-weight", "weight-limit", "notional-exposure"],
)
def test_single_position_limits_are_conservative(
    item_change: Any,
    code: str,
    expected_observed: str,
) -> None:
    value = candidate()
    decision = evaluate(replace(value, items=(item_change(value.items[0]),)))

    failed = rule(decision, code)
    assert failed.outcome is RiskRuleOutcome.FAIL
    assert failed.observed_value == expected_observed


@pytest.mark.parametrize(
    ("liquidity", "observed"),
    [
        ((), "000001.SZ=UNKNOWN"),
        (
            (MarketLiquidity(SecurityCode.parse("000001.SZ"), Money.parse("0")),),
            "000001.SZ=UNKNOWN",
        ),
        (
            (MarketLiquidity(SecurityCode.parse("000001.SZ"), Money.parse("50000")),),
            "000001.SZ=0.1",
        ),
    ],
    ids=["missing", "zero-turnover", "above-limit"],
)
def test_participation_requires_positive_market_turnover_and_enforces_limit(
    liquidity: tuple[MarketLiquidity, ...],
    observed: str,
) -> None:
    decision = evaluate(facts=replace(context(), liquidity=liquidity))

    failed = rule(decision, "MARKET_PARTICIPATION_RATE")
    assert failed.outcome is RiskRuleOutcome.FAIL
    assert failed.observed_value == observed


def test_evaluation_is_deterministic_and_boundary_values_pass() -> None:
    first = evaluate()
    second = evaluate()

    assert first == second
    assert rule(first, "SINGLE_POSITION_TARGET_EXPOSURE").outcome is RiskRuleOutcome.PASS
    assert rule(first, "SINGLE_POSITION_NOTIONAL_EXPOSURE").outcome is RiskRuleOutcome.PASS
    assert rule(first, "PLAN_DECLARED_EXPOSURE_LIMIT").outcome is RiskRuleOutcome.PASS


def test_six_item_plan_stays_within_event_rule_capacity() -> None:
    original = candidate()
    items = tuple(
        replace(
            original.items[0],
            symbol=SecurityCode.parse(f"{number:06d}.SZ"),
            max_notional=Money.parse("1000"),
        )
        for number in range(1, 7)
    )
    liquidity = tuple(MarketLiquidity(item.symbol, Money.parse("100000")) for item in items)

    decision = evaluate(
        replace(original, items=items),
        replace(context(), held_symbols=(), liquidity=liquidity),
    )

    assert decision.decision is PlanRiskDecisionOutcome.ALLOW
    assert len(decision.rules) == 29


def test_model_collections_are_immutable_and_duplicates_are_rejected() -> None:
    value = candidate()
    with pytest.raises(FrozenInstanceError):
        value.plan_id = "plan_changed_0001"  # type: ignore[misc]
    with pytest.raises(ValueError, match="unique"):
        replace(value, items=(value.items[0], value.items[0]))
    with pytest.raises(ValueError, match="held_symbols"):
        replace(context(), held_symbols=(context().held_symbols[0],) * 2)
    with pytest.raises(ValueError, match="liquidity symbols"):
        replace(context(), liquidity=context().liquidity * 2)


@pytest.mark.parametrize(
    "invalid_policy",
    [
        lambda value: replace(value, max_positions=True),
        lambda value: replace(value, max_participation_rate=Ratio.parse("0")),
        lambda value: replace(value, max_plan_buy_notional=Money.parse("0")),
        lambda value: replace(value, allowed_risk_policies=frozenset()),
        lambda value: replace(
            value,
            max_single_position_exposure=Ratio.parse("0.7"),
        ),
        lambda value: replace(
            value,
            supported_algorithm_versions=frozenset({"a" * 49, "b" * 49}),
        ),
    ],
    ids=[
        "bool-position-count",
        "zero-participation",
        "zero-notional",
        "empty-policy-set",
        "single-above-total",
        "unrepresentable-version-list",
    ],
)
def test_policy_rejects_invalid_or_unrepresentable_limits(invalid_policy: Any) -> None:
    with pytest.raises(ValueError):
        invalid_policy(policy())


def test_decision_rejects_inconsistent_summary_and_invalid_evaluator_inputs() -> None:
    allowed = evaluate()
    with pytest.raises(ValueError, match="agree"):
        replace(allowed, decision=PlanRiskDecisionOutcome.REJECT)
    with pytest.raises(ValueError, match="policy"):
        PlanRiskEvaluator(cast(PlanRiskPolicy, object()))
    with pytest.raises(ValueError, match="candidate"):
        PlanRiskEvaluator(policy()).evaluate(
            cast(PlanRiskCandidate, object()),
            context(),
            decision_id="risk_plan_01K43TAIL000000001",
        )


def test_decimal_calculations_do_not_use_binary_float() -> None:
    decision = evaluate(
        facts=replace(
            context(),
            net_asset=Money(Decimal("99999.99")),
            liquidity=(
                MarketLiquidity(
                    SecurityCode.parse("000001.SZ"),
                    Money(Decimal("166666.67")),
                ),
            ),
        )
    )

    exposure = rule(decision, "SINGLE_POSITION_NOTIONAL_EXPOSURE").observed_value
    participation = rule(decision, "MARKET_PARTICIPATION_RATE").observed_value
    assert exposure.startswith("000001.SZ=0.050000005")
    assert participation.startswith("000001.SZ=0.029999999")
