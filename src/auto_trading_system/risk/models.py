"""Immutable plan-admission risk values and versioned policy parameters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from auto_trading_system.domain import Money, Ratio, SecurityCode

_PLAN_ID_PATTERN = re.compile(r"plan_[A-Za-z0-9_-]{8,64}")
_ACCOUNT_ID_PATTERN = re.compile(r"acct_[A-Za-z0-9_-]{3,48}")
_DECISION_ID_PATTERN = re.compile(r"risk_[A-Za-z0-9_-]{8,64}")
_CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_ALGORITHM_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,95}")


def _require_pattern(value: str, pattern: re.Pattern[str], field: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field} has an invalid format")


def _require_code_set(values: frozenset[str], field: str) -> None:
    if not isinstance(values, frozenset) or not values:
        raise ValueError(f"{field} must be a non-empty frozenset")
    if any(
        not isinstance(value, str) or _CODE_PATTERN.fullmatch(value) is None for value in values
    ):
        raise ValueError(f"{field} contains an invalid code")


@dataclass(frozen=True, slots=True)
class PlanRiskItem:
    symbol: SecurityCode
    target_weight: Ratio
    max_notional: Money
    entry_policy: str
    exit_policy: str
    risk_policy: str
    execution_policy: str

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, SecurityCode):
            raise ValueError("symbol must be a SecurityCode")
        if not isinstance(self.target_weight, Ratio):
            raise ValueError("target_weight must be a Ratio")
        if not isinstance(self.max_notional, Money) or self.max_notional.amount <= 0:
            raise ValueError("max_notional must be positive Money")
        for field, value in (
            ("entry_policy", self.entry_policy),
            ("exit_policy", self.exit_policy),
            ("risk_policy", self.risk_policy),
            ("execution_policy", self.execution_policy),
        ):
            _require_pattern(value, _CODE_PATTERN, field)


@dataclass(frozen=True, slots=True)
class PlanRiskCandidate:
    plan_id: str
    account_id: str
    strategy_id: str
    algorithm_version: str
    target_exposure: Ratio
    declared_hard_max_exposure: Ratio
    declared_max_positions: int
    items: tuple[PlanRiskItem, ...]

    def __post_init__(self) -> None:
        _require_pattern(self.plan_id, _PLAN_ID_PATTERN, "plan_id")
        _require_pattern(self.account_id, _ACCOUNT_ID_PATTERN, "account_id")
        _require_pattern(self.strategy_id, _CODE_PATTERN, "strategy_id")
        _require_pattern(self.algorithm_version, _ALGORITHM_PATTERN, "algorithm_version")
        if not isinstance(self.target_exposure, Ratio):
            raise ValueError("target_exposure must be a Ratio")
        if not isinstance(self.declared_hard_max_exposure, Ratio):
            raise ValueError("declared_hard_max_exposure must be a Ratio")
        if (
            type(self.declared_max_positions) is not int
            or not 1 <= self.declared_max_positions <= 15
        ):
            raise ValueError("declared_max_positions must be between 1 and 15")
        if not isinstance(self.items, tuple) or not 1 <= len(self.items) <= 6:
            raise ValueError("items must be a tuple containing one to six items")
        if not all(isinstance(item, PlanRiskItem) for item in self.items):
            raise ValueError("items must contain PlanRiskItem values")
        symbols = tuple(item.symbol for item in self.items)
        if len(set(symbols)) != len(symbols):
            raise ValueError("plan risk item symbols must be unique")


@dataclass(frozen=True, slots=True)
class MarketLiquidity:
    symbol: SecurityCode
    trailing_turnover: Money

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, SecurityCode):
            raise ValueError("symbol must be a SecurityCode")
        if not isinstance(self.trailing_turnover, Money):
            raise ValueError("trailing_turnover must be Money")


@dataclass(frozen=True, slots=True)
class PlanRiskContext:
    account_id: str
    net_asset: Money
    available_cash: Money
    current_exposure: Ratio
    held_symbols: tuple[SecurityCode, ...]
    liquidity: tuple[MarketLiquidity, ...]

    def __post_init__(self) -> None:
        _require_pattern(self.account_id, _ACCOUNT_ID_PATTERN, "account_id")
        if not isinstance(self.net_asset, Money) or self.net_asset.amount <= 0:
            raise ValueError("net_asset must be positive Money")
        if not isinstance(self.available_cash, Money):
            raise ValueError("available_cash must be Money")
        if not isinstance(self.current_exposure, Ratio):
            raise ValueError("current_exposure must be a Ratio")
        if not isinstance(self.held_symbols, tuple) or not all(
            isinstance(symbol, SecurityCode) for symbol in self.held_symbols
        ):
            raise ValueError("held_symbols must contain SecurityCode values")
        if len(set(self.held_symbols)) != len(self.held_symbols):
            raise ValueError("held_symbols must be unique")
        if not isinstance(self.liquidity, tuple) or not all(
            isinstance(value, MarketLiquidity) for value in self.liquidity
        ):
            raise ValueError("liquidity must contain MarketLiquidity values")
        liquidity_symbols = tuple(value.symbol for value in self.liquidity)
        if len(set(liquidity_symbols)) != len(liquidity_symbols):
            raise ValueError("liquidity symbols must be unique")


@dataclass(frozen=True, slots=True)
class PlanRiskPolicy:
    policy_version: str
    strategy_id: str
    supported_algorithm_versions: frozenset[str]
    allowed_entry_policies: frozenset[str]
    allowed_exit_policies: frozenset[str]
    allowed_risk_policies: frozenset[str]
    allowed_execution_policies: frozenset[str]
    max_positions: int
    max_total_exposure: Ratio
    max_single_position_exposure: Ratio
    max_participation_rate: Ratio
    max_plan_buy_notional: Money

    def __post_init__(self) -> None:
        _require_pattern(self.policy_version, _CODE_PATTERN, "policy_version")
        _require_pattern(self.strategy_id, _CODE_PATTERN, "strategy_id")
        if (
            not isinstance(self.supported_algorithm_versions, frozenset)
            or not self.supported_algorithm_versions
            or any(
                not isinstance(value, str) or _ALGORITHM_PATTERN.fullmatch(value) is None
                for value in self.supported_algorithm_versions
            )
        ):
            raise ValueError("supported_algorithm_versions contains an invalid version")
        if len(",".join(sorted(self.supported_algorithm_versions))) > 96:
            raise ValueError("supported_algorithm_versions cannot be represented in a risk rule")
        for field, values in (
            ("allowed_entry_policies", self.allowed_entry_policies),
            ("allowed_exit_policies", self.allowed_exit_policies),
            ("allowed_risk_policies", self.allowed_risk_policies),
            ("allowed_execution_policies", self.allowed_execution_policies),
        ):
            _require_code_set(values, field)
        if type(self.max_positions) is not int or not 1 <= self.max_positions <= 15:
            raise ValueError("max_positions must be between 1 and 15")
        ratios = (
            self.max_total_exposure,
            self.max_single_position_exposure,
            self.max_participation_rate,
        )
        if any(not isinstance(value, Ratio) or value.value <= 0 for value in ratios):
            raise ValueError("policy exposure and participation limits must be positive Ratio")
        if self.max_single_position_exposure > self.max_total_exposure:
            raise ValueError("single-position exposure cannot exceed total exposure")
        if not isinstance(self.max_plan_buy_notional, Money) or (
            self.max_plan_buy_notional.amount <= 0
        ):
            raise ValueError("max_plan_buy_notional must be positive Money")


class RiskRuleOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class PlanRiskDecisionOutcome(StrEnum):
    ALLOW = "ALLOW"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class PlanRiskRuleResult:
    rule_code: str
    outcome: RiskRuleOutcome
    observed_value: str
    limit_value: str

    def __post_init__(self) -> None:
        _require_pattern(self.rule_code, _CODE_PATTERN, "rule_code")
        if not isinstance(self.outcome, RiskRuleOutcome):
            raise ValueError("outcome must be a RiskRuleOutcome")
        for field, value in (
            ("observed_value", self.observed_value),
            ("limit_value", self.limit_value),
        ):
            if not isinstance(value, str) or not 1 <= len(value) <= 96:
                raise ValueError(f"{field} must contain between 1 and 96 characters")

    def to_mapping(self) -> dict[str, str]:
        return {
            "rule_code": self.rule_code,
            "outcome": self.outcome.value,
            "observed_value": self.observed_value,
            "limit_value": self.limit_value,
        }


@dataclass(frozen=True, slots=True)
class PlanRiskDecision:
    decision_id: str
    plan_id: str
    decision: PlanRiskDecisionOutcome
    policy_version: str
    rules: tuple[PlanRiskRuleResult, ...]

    def __post_init__(self) -> None:
        _require_pattern(self.decision_id, _DECISION_ID_PATTERN, "decision_id")
        _require_pattern(self.plan_id, _PLAN_ID_PATTERN, "plan_id")
        if not isinstance(self.decision, PlanRiskDecisionOutcome):
            raise ValueError("decision must be a PlanRiskDecisionOutcome")
        _require_pattern(self.policy_version, _CODE_PATTERN, "policy_version")
        if not isinstance(self.rules, tuple) or not 1 <= len(self.rules) <= 32:
            raise ValueError("rules must contain between 1 and 32 results")
        if not all(isinstance(rule, PlanRiskRuleResult) for rule in self.rules):
            raise ValueError("rules must contain PlanRiskRuleResult values")
        expected = PlanRiskDecisionOutcome.ALLOW
        if any(rule.outcome is RiskRuleOutcome.FAIL for rule in self.rules):
            expected = PlanRiskDecisionOutcome.REJECT
        if self.decision is not expected:
            raise ValueError("decision must agree with its rule outcomes")

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "scope": "PLAN",
            "subject_id": self.plan_id,
            "decision": self.decision.value,
            "policy_version": self.policy_version,
            "rules": [rule.to_mapping() for rule in self.rules],
        }
