"""Deterministic, side-effect-free plan-level strategy risk evaluation."""

from __future__ import annotations

from decimal import Decimal

from auto_trading_system.risk.models import (
    PlanRiskCandidate,
    PlanRiskContext,
    PlanRiskDecision,
    PlanRiskDecisionOutcome,
    PlanRiskPolicy,
    PlanRiskRuleResult,
    RiskRuleOutcome,
)


def _decimal_text(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def _result(
    rule_code: str,
    passed: bool,
    observed_value: str,
    limit_value: str,
) -> PlanRiskRuleResult:
    outcome = RiskRuleOutcome.PASS if passed else RiskRuleOutcome.FAIL
    return PlanRiskRuleResult(rule_code, outcome, observed_value, limit_value)


class PlanRiskEvaluator:
    """Evaluate a READY plan against explicit policy and current account facts."""

    def __init__(self, policy: PlanRiskPolicy) -> None:
        if not isinstance(policy, PlanRiskPolicy):
            raise ValueError("policy must be a PlanRiskPolicy")
        self._policy = policy

    @property
    def policy(self) -> PlanRiskPolicy:
        return self._policy

    def evaluate(
        self,
        candidate: PlanRiskCandidate,
        context: PlanRiskContext,
        *,
        decision_id: str,
    ) -> PlanRiskDecision:
        if not isinstance(candidate, PlanRiskCandidate):
            raise ValueError("candidate must be a PlanRiskCandidate")
        if not isinstance(context, PlanRiskContext):
            raise ValueError("context must be a PlanRiskContext")

        policy = self._policy
        rules: list[PlanRiskRuleResult] = []
        rules.append(
            _result(
                "PLAN_ACCOUNT_MATCH",
                candidate.account_id == context.account_id,
                candidate.account_id,
                context.account_id,
            )
        )
        rules.append(
            _result(
                "PLAN_STRATEGY_ID",
                candidate.strategy_id == policy.strategy_id,
                candidate.strategy_id,
                policy.strategy_id,
            )
        )
        algorithms = sorted(policy.supported_algorithm_versions)
        rules.append(
            _result(
                "PLAN_ALGORITHM_VERSION",
                candidate.algorithm_version in policy.supported_algorithm_versions,
                candidate.algorithm_version,
                ",".join(algorithms),
            )
        )
        binding_mismatches = sum(
            item.entry_policy not in policy.allowed_entry_policies
            or item.exit_policy not in policy.allowed_exit_policies
            or item.risk_policy not in policy.allowed_risk_policies
            or item.execution_policy not in policy.allowed_execution_policies
            for item in candidate.items
        )
        rules.append(
            _result(
                "PLAN_POLICY_BINDINGS",
                binding_mismatches == 0,
                "MATCHED" if binding_mismatches == 0 else f"MISMATCH_COUNT={binding_mismatches}",
                policy.policy_version,
            )
        )
        effective_max_positions = min(
            candidate.declared_max_positions,
            policy.max_positions,
        )
        rules.append(
            _result(
                "PLAN_DECLARED_POSITION_LIMIT",
                candidate.declared_max_positions <= policy.max_positions,
                str(candidate.declared_max_positions),
                str(policy.max_positions),
            )
        )
        rules.append(
            _result(
                "PLAN_DECLARED_EXPOSURE_LIMIT",
                Decimal(0)
                < candidate.declared_hard_max_exposure.value
                <= policy.max_total_exposure.value,
                candidate.declared_hard_max_exposure.to_wire(),
                policy.max_total_exposure.to_wire(),
            )
        )
        effective_exposure_limit = min(
            candidate.declared_hard_max_exposure.value,
            policy.max_total_exposure.value,
        )
        rules.append(
            _result(
                "PLAN_TARGET_EXPOSURE",
                Decimal(0) < candidate.target_exposure.value <= effective_exposure_limit,
                candidate.target_exposure.to_wire(),
                _decimal_text(effective_exposure_limit),
            )
        )
        item_weight_sum = sum((item.target_weight.value for item in candidate.items), Decimal(0))
        rules.append(
            _result(
                "PLAN_ITEM_WEIGHT_SUM",
                item_weight_sum <= candidate.target_exposure.value,
                _decimal_text(item_weight_sum),
                candidate.target_exposure.to_wire(),
            )
        )
        projected_symbols = set(context.held_symbols)
        projected_symbols.update(item.symbol for item in candidate.items)
        rules.append(
            _result(
                "PLAN_PROJECTED_POSITION_COUNT",
                len(projected_symbols) <= effective_max_positions,
                str(len(projected_symbols)),
                str(effective_max_positions),
            )
        )
        rules.append(
            _result(
                "PLAN_CURRENT_EXPOSURE_HEADROOM",
                context.current_exposure < candidate.target_exposure,
                context.current_exposure.to_wire(),
                candidate.target_exposure.to_wire(),
            )
        )
        plan_notional = sum((item.max_notional.amount for item in candidate.items), Decimal(0))
        effective_notional_limit = min(
            context.available_cash.amount,
            policy.max_plan_buy_notional.amount,
        )
        rules.append(
            _result(
                "PLAN_BUY_NOTIONAL",
                plan_notional <= effective_notional_limit,
                _decimal_text(plan_notional),
                _decimal_text(effective_notional_limit),
            )
        )

        liquidity = {value.symbol: value.trailing_turnover.amount for value in context.liquidity}
        for item in candidate.items:
            symbol = item.symbol.to_wire()
            rules.append(
                _result(
                    "SINGLE_POSITION_TARGET_EXPOSURE",
                    Decimal(0)
                    < item.target_weight.value
                    <= policy.max_single_position_exposure.value,
                    f"{symbol}={item.target_weight.to_wire()}",
                    policy.max_single_position_exposure.to_wire(),
                )
            )
            notional_exposure = item.max_notional.amount / context.net_asset.amount
            rules.append(
                _result(
                    "SINGLE_POSITION_NOTIONAL_EXPOSURE",
                    notional_exposure <= policy.max_single_position_exposure.value,
                    f"{symbol}={_decimal_text(notional_exposure)}",
                    policy.max_single_position_exposure.to_wire(),
                )
            )
            turnover = liquidity.get(item.symbol)
            participation: Decimal | None = None
            if turnover is not None and turnover > 0:
                participation = item.max_notional.amount / turnover
            rules.append(
                _result(
                    "MARKET_PARTICIPATION_RATE",
                    participation is not None
                    and participation <= policy.max_participation_rate.value,
                    (
                        f"{symbol}={_decimal_text(participation)}"
                        if participation is not None
                        else f"{symbol}=UNKNOWN"
                    ),
                    policy.max_participation_rate.to_wire(),
                )
            )

        outcome = PlanRiskDecisionOutcome.ALLOW
        if any(rule.outcome is RiskRuleOutcome.FAIL for rule in rules):
            outcome = PlanRiskDecisionOutcome.REJECT
        return PlanRiskDecision(
            decision_id,
            candidate.plan_id,
            outcome,
            policy.policy_version,
            tuple(rules),
        )
