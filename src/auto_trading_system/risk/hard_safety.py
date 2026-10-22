"""Final machine-local order risk evaluation using Windows-owned limits."""

from __future__ import annotations

from decimal import Decimal

from auto_trading_system.domain import SecurityCode
from auto_trading_system.risk.hard_safety_models import (
    HardSafetyDecision,
    HardSafetyDecisionOutcome,
    HardSafetyFacts,
    HardSafetyLimits,
    HardSafetyOrder,
    HardSafetyRuleOutcome,
    HardSafetyRuleResult,
    HardSafetySide,
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
) -> HardSafetyRuleResult:
    outcome = HardSafetyRuleOutcome.PASS if passed else HardSafetyRuleOutcome.FAIL
    return HardSafetyRuleResult(rule_code, outcome, observed_value, limit_value)


class HardSafetyEvaluator:
    """Apply limits that remote plans, policy outputs, and commands cannot widen."""

    def __init__(self, limits: HardSafetyLimits) -> None:
        if not isinstance(limits, HardSafetyLimits):
            raise ValueError("limits must be HardSafetyLimits")
        self._limits = limits

    @property
    def limits(self) -> HardSafetyLimits:
        return self._limits

    def evaluate(
        self,
        order: HardSafetyOrder,
        facts: HardSafetyFacts,
        *,
        decision_id: str,
    ) -> HardSafetyDecision:
        if not isinstance(order, HardSafetyOrder):
            raise ValueError("order must be a HardSafetyOrder")
        if not isinstance(facts, HardSafetyFacts):
            raise ValueError("facts must be HardSafetyFacts")
        limits = self._limits
        rules: list[HardSafetyRuleResult] = []
        facts_available = facts.broker_facts_available and facts.market_data_available
        rules.append(
            _result(
                "HARD_FACTS_AVAILABLE",
                facts_available,
                (
                    f"BROKER={'AVAILABLE' if facts.broker_facts_available else 'UNKNOWN'};"
                    f"MARKET={'AVAILABLE' if facts.market_data_available else 'UNKNOWN'}"
                ),
                "BROKER=AVAILABLE;MARKET=AVAILABLE",
            )
        )
        rules.append(
            _result(
                "HARD_ACCOUNT_MATCH",
                order.account_id == facts.account_id,
                order.account_id,
                facts.account_id,
            )
        )
        rules.append(
            _result(
                "HARD_MARKET_FRESHNESS",
                facts.tick_age_seconds <= limits.max_tick_age_seconds,
                _decimal_text(facts.tick_age_seconds),
                _decimal_text(limits.max_tick_age_seconds),
            )
        )
        rules.append(
            _result(
                "HARD_CLOCK_SKEW",
                facts.absolute_clock_skew_seconds <= Decimal(limits.max_clock_skew_seconds),
                _decimal_text(facts.absolute_clock_skew_seconds),
                str(limits.max_clock_skew_seconds),
            )
        )
        rules.append(
            _result(
                "HARD_ACTIVE_ORDER_COUNT",
                len(facts.active_orders) + 1 <= limits.max_active_orders,
                str(len(facts.active_orders) + 1),
                str(limits.max_active_orders),
            )
        )
        rules.append(
            _result(
                "HARD_CLIENT_ORDER_ID_UNIQUE",
                all(
                    value.client_order_id != order.client_order_id for value in facts.active_orders
                ),
                order.client_order_id,
                "NOT_ACTIVE",
            )
        )
        matching_active = sum(
            value.symbol == order.symbol and value.side is order.side
            for value in facts.active_orders
        )
        rules.append(
            _result(
                "HARD_SYMBOL_SIDE_ACTIVE_ORDER",
                matching_active + 1 <= limits.active_order_per_symbol_side,
                str(matching_active + 1),
                str(limits.active_order_per_symbol_side),
            )
        )
        effective_order_limit = min(
            order.plan_max_notional.amount,
            limits.max_single_order_notional.amount,
        )
        rules.append(
            _result(
                "HARD_SINGLE_ORDER_NOTIONAL",
                order.proposed_notional <= effective_order_limit,
                _decimal_text(order.proposed_notional),
                _decimal_text(effective_order_limit),
            )
        )

        position_values = {value.symbol: value.market_value.amount for value in facts.positions}
        pending_buy_values: dict[SecurityCode, Decimal] = {}
        for active_order in facts.active_orders:
            if active_order.side is HardSafetySide.BUY:
                pending_buy_values[active_order.symbol] = (
                    pending_buy_values.get(active_order.symbol, Decimal(0))
                    + active_order.remaining_notional.amount
                )
        is_buy = order.side is HardSafetySide.BUY
        current_symbol_value = position_values.get(order.symbol, Decimal(0))
        projected_symbols = {
            symbol for symbol, market_value in position_values.items() if market_value > 0
        }
        projected_symbols.update(pending_buy_values)
        projected_position_count = len(projected_symbols)
        projected_total = facts.total_position_value.amount + sum(
            pending_buy_values.values(), Decimal(0)
        )
        projected_symbol = current_symbol_value + pending_buy_values.get(order.symbol, Decimal(0))
        projected_daily_buy = facts.daily_buy_notional.amount
        if is_buy:
            projected_symbols.add(order.symbol)
            projected_position_count = len(projected_symbols)
            projected_total += order.proposed_notional
            projected_symbol += order.proposed_notional
            projected_daily_buy += order.proposed_notional
        total_exposure = projected_total / facts.net_asset.amount
        symbol_exposure = projected_symbol / facts.net_asset.amount

        def buy_observed(value: str) -> str:
            return value if is_buy else "NOT_APPLICABLE"

        rules.append(
            _result(
                "HARD_PROJECTED_POSITION_COUNT",
                not is_buy or projected_position_count <= limits.max_positions,
                buy_observed(str(projected_position_count)),
                str(limits.max_positions),
            )
        )
        rules.append(
            _result(
                "HARD_PROJECTED_TOTAL_EXPOSURE",
                not is_buy or total_exposure <= limits.hard_max_exposure.value,
                buy_observed(_decimal_text(total_exposure)),
                limits.hard_max_exposure.to_wire(),
            )
        )
        rules.append(
            _result(
                "HARD_PROJECTED_SINGLE_EXPOSURE",
                not is_buy or symbol_exposure <= limits.max_single_position_exposure.value,
                buy_observed(_decimal_text(symbol_exposure)),
                limits.max_single_position_exposure.to_wire(),
            )
        )
        rules.append(
            _result(
                "HARD_DAILY_BUY_NOTIONAL",
                not is_buy or projected_daily_buy <= limits.max_daily_buy_notional.amount,
                buy_observed(_decimal_text(projected_daily_buy)),
                limits.max_daily_buy_notional.to_wire(),
            )
        )

        decision = HardSafetyDecisionOutcome.ALLOW
        if any(rule.outcome is HardSafetyRuleOutcome.FAIL for rule in rules):
            decision = HardSafetyDecisionOutcome.REJECT
        return HardSafetyDecision(
            decision_id,
            order.client_order_id,
            decision,
            limits.policy_version,
            tuple(rules),
        )
