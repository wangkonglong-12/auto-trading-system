"""Immutable Windows Hard Safety inputs, limits, and decision evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from auto_trading_system.domain import Money, Ratio, SecurityCode

_ACCOUNT_ID_PATTERN = re.compile(r"acct_[A-Za-z0-9_-]{3,48}")
_ORDER_ID_PATTERN = re.compile(r"ord_[A-Za-z0-9_-]{8,64}")
_DECISION_ID_PATTERN = re.compile(r"risk_[A-Za-z0-9_-]{8,64}")
_CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


def _require_pattern(value: str, pattern: re.Pattern[str], field: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field} has an invalid format")


def _require_non_negative_decimal(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be a non-negative finite Decimal")


class HardSafetySide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class HardSafetyOrder:
    client_order_id: str
    account_id: str
    symbol: SecurityCode
    side: HardSafetySide
    proposed_notional: Decimal
    plan_max_notional: Money

    def __post_init__(self) -> None:
        _require_pattern(self.client_order_id, _ORDER_ID_PATTERN, "client_order_id")
        _require_pattern(self.account_id, _ACCOUNT_ID_PATTERN, "account_id")
        if not isinstance(self.symbol, SecurityCode):
            raise ValueError("symbol must be a SecurityCode")
        if not isinstance(self.side, HardSafetySide):
            raise ValueError("side must be a HardSafetySide")
        _require_non_negative_decimal(self.proposed_notional, "proposed_notional")
        if self.proposed_notional == 0:
            raise ValueError("proposed_notional must be positive")
        if not isinstance(self.plan_max_notional, Money) or self.plan_max_notional.amount <= 0:
            raise ValueError("plan_max_notional must be positive Money")


@dataclass(frozen=True, slots=True)
class ActiveOrderSlot:
    client_order_id: str
    symbol: SecurityCode
    side: HardSafetySide
    remaining_notional: Money

    def __post_init__(self) -> None:
        _require_pattern(self.client_order_id, _ORDER_ID_PATTERN, "client_order_id")
        if not isinstance(self.symbol, SecurityCode):
            raise ValueError("symbol must be a SecurityCode")
        if not isinstance(self.side, HardSafetySide):
            raise ValueError("side must be a HardSafetySide")
        if not isinstance(self.remaining_notional, Money) or self.remaining_notional.amount <= 0:
            raise ValueError("remaining_notional must be positive Money")


@dataclass(frozen=True, slots=True)
class SymbolPositionValue:
    symbol: SecurityCode
    market_value: Money

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, SecurityCode):
            raise ValueError("symbol must be a SecurityCode")
        if not isinstance(self.market_value, Money):
            raise ValueError("market_value must be Money")


@dataclass(frozen=True, slots=True)
class HardSafetyFacts:
    account_id: str
    broker_facts_available: bool
    market_data_available: bool
    net_asset: Money
    total_position_value: Money
    positions: tuple[SymbolPositionValue, ...]
    active_orders: tuple[ActiveOrderSlot, ...]
    daily_buy_notional: Money
    tick_age_seconds: Decimal
    absolute_clock_skew_seconds: Decimal

    def __post_init__(self) -> None:
        _require_pattern(self.account_id, _ACCOUNT_ID_PATTERN, "account_id")
        if type(self.broker_facts_available) is not bool:
            raise ValueError("broker_facts_available must be a boolean")
        if type(self.market_data_available) is not bool:
            raise ValueError("market_data_available must be a boolean")
        if not isinstance(self.net_asset, Money) or self.net_asset.amount <= 0:
            raise ValueError("net_asset must be positive Money")
        if not isinstance(self.total_position_value, Money):
            raise ValueError("total_position_value must be Money")
        if not isinstance(self.positions, tuple) or not all(
            isinstance(value, SymbolPositionValue) for value in self.positions
        ):
            raise ValueError("positions must contain SymbolPositionValue values")
        position_symbols = tuple(value.symbol for value in self.positions)
        if len(set(position_symbols)) != len(position_symbols):
            raise ValueError("position symbols must be unique")
        position_sum = sum((value.market_value.amount for value in self.positions), Decimal(0))
        if position_sum != self.total_position_value.amount:
            raise ValueError("total_position_value must equal the position value sum")
        if not isinstance(self.active_orders, tuple) or not all(
            isinstance(value, ActiveOrderSlot) for value in self.active_orders
        ):
            raise ValueError("active_orders must contain ActiveOrderSlot values")
        active_ids = tuple(value.client_order_id for value in self.active_orders)
        if len(set(active_ids)) != len(active_ids):
            raise ValueError("active order ids must be unique")
        if not isinstance(self.daily_buy_notional, Money):
            raise ValueError("daily_buy_notional must be Money")
        _require_non_negative_decimal(self.tick_age_seconds, "tick_age_seconds")
        _require_non_negative_decimal(
            self.absolute_clock_skew_seconds,
            "absolute_clock_skew_seconds",
        )


@dataclass(frozen=True, slots=True)
class HardSafetyLimits:
    policy_version: str
    max_positions: int
    hard_max_exposure: Ratio
    max_single_position_exposure: Ratio
    max_active_orders: int
    active_order_per_symbol_side: int
    max_single_order_notional: Money
    max_daily_buy_notional: Money
    max_tick_age_seconds: Decimal
    max_clock_skew_seconds: int

    def __post_init__(self) -> None:
        _require_pattern(self.policy_version, _CODE_PATTERN, "policy_version")
        if type(self.max_positions) is not int or not 1 <= self.max_positions <= 15:
            raise ValueError("max_positions must be between 1 and 15")
        if not isinstance(self.hard_max_exposure, Ratio) or self.hard_max_exposure.value <= 0:
            raise ValueError("hard_max_exposure must be positive Ratio")
        if (
            not isinstance(self.max_single_position_exposure, Ratio)
            or self.max_single_position_exposure.value <= 0
        ):
            raise ValueError("max_single_position_exposure must be positive Ratio")
        if self.max_single_position_exposure > self.hard_max_exposure:
            raise ValueError("single-position exposure cannot exceed total exposure")
        if type(self.max_active_orders) is not int or not 1 <= self.max_active_orders <= 3:
            raise ValueError("max_active_orders must be between 1 and 3")
        if type(self.active_order_per_symbol_side) is not int or (
            self.active_order_per_symbol_side != 1
        ):
            raise ValueError("active_order_per_symbol_side must equal 1")
        if (
            not isinstance(self.max_single_order_notional, Money)
            or self.max_single_order_notional.amount <= 0
        ):
            raise ValueError("max_single_order_notional must be positive Money")
        if (
            not isinstance(self.max_daily_buy_notional, Money)
            or self.max_daily_buy_notional.amount <= 0
        ):
            raise ValueError("max_daily_buy_notional must be positive Money")
        if self.max_single_order_notional > self.max_daily_buy_notional:
            raise ValueError("single-order notional cannot exceed daily buy notional")
        if (
            not isinstance(self.max_tick_age_seconds, Decimal)
            or not self.max_tick_age_seconds.is_finite()
            or not Decimal(0) < self.max_tick_age_seconds <= Decimal(300)
        ):
            raise ValueError("max_tick_age_seconds must be between 0 and 300")
        if (
            type(self.max_clock_skew_seconds) is not int
            or not 1 <= self.max_clock_skew_seconds <= 30
        ):
            raise ValueError("max_clock_skew_seconds must be between 1 and 30")


class HardSafetyRuleOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class HardSafetyDecisionOutcome(StrEnum):
    ALLOW = "ALLOW"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class HardSafetyRuleResult:
    rule_code: str
    outcome: HardSafetyRuleOutcome
    observed_value: str
    limit_value: str

    def __post_init__(self) -> None:
        _require_pattern(self.rule_code, _CODE_PATTERN, "rule_code")
        if not isinstance(self.outcome, HardSafetyRuleOutcome):
            raise ValueError("outcome must be a HardSafetyRuleOutcome")
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
class HardSafetyDecision:
    decision_id: str
    client_order_id: str
    decision: HardSafetyDecisionOutcome
    policy_version: str
    rules: tuple[HardSafetyRuleResult, ...]

    def __post_init__(self) -> None:
        _require_pattern(self.decision_id, _DECISION_ID_PATTERN, "decision_id")
        _require_pattern(self.client_order_id, _ORDER_ID_PATTERN, "client_order_id")
        if not isinstance(self.decision, HardSafetyDecisionOutcome):
            raise ValueError("decision must be a HardSafetyDecisionOutcome")
        _require_pattern(self.policy_version, _CODE_PATTERN, "policy_version")
        if not isinstance(self.rules, tuple) or not 1 <= len(self.rules) <= 32:
            raise ValueError("rules must contain between 1 and 32 results")
        if not all(isinstance(rule, HardSafetyRuleResult) for rule in self.rules):
            raise ValueError("rules must contain HardSafetyRuleResult values")
        expected = HardSafetyDecisionOutcome.ALLOW
        if any(rule.outcome is HardSafetyRuleOutcome.FAIL for rule in self.rules):
            expected = HardSafetyDecisionOutcome.REJECT
        if self.decision is not expected:
            raise ValueError("decision must agree with its rule outcomes")

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "scope": "ORDER",
            "subject_id": self.client_order_id,
            "decision": self.decision.value,
            "policy_version": self.policy_version,
            "rules": [rule.to_mapping() for rule in self.rules],
        }
