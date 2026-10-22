"""Strict adapter from an authenticated TradePlan mapping to typed risk input."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from auto_trading_system.domain import DomainValueError, Money, Ratio, SecurityCode
from auto_trading_system.protocol import validate_trade_plan_semantics
from auto_trading_system.protocol.trade_plan import TradePlanSemanticError
from auto_trading_system.risk.models import PlanRiskCandidate, PlanRiskItem


class PlanRiskInputError(ValueError):
    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(f"{code} at {field}: {message}")
        self.code = code
        self.field = field


def _fail(code: str, field: str, message: str) -> NoReturn:
    raise PlanRiskInputError(code, field, message)


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("PLAN_RISK_INPUT_INVALID", field, "must be an object")
    return value


def _string(container: Mapping[str, Any], field: str, path: str) -> str:
    value = container.get(field)
    if not isinstance(value, str):
        _fail("PLAN_RISK_INPUT_INVALID", path, "must be a string")
    return value


def _ratio(container: Mapping[str, Any], field: str, path: str) -> Ratio:
    try:
        return Ratio.parse(_string(container, field, path))
    except DomainValueError as exc:
        raise PlanRiskInputError("PLAN_RISK_INPUT_INVALID", path, str(exc)) from exc


def _money(container: Mapping[str, Any], field: str, path: str) -> Money:
    try:
        return Money.parse(_string(container, field, path))
    except DomainValueError as exc:
        raise PlanRiskInputError("PLAN_RISK_INPUT_INVALID", path, str(exc)) from exc


def _security(container: Mapping[str, Any], field: str, path: str) -> SecurityCode:
    try:
        return SecurityCode.parse(_string(container, field, path))
    except DomainValueError as exc:
        raise PlanRiskInputError("PLAN_RISK_INPUT_INVALID", path, str(exc)) from exc


def parse_trade_plan_risk_candidate(plan: Mapping[str, Any]) -> PlanRiskCandidate:
    """Require protocol semantics and retain only fields used by plan risk."""
    if not isinstance(plan, Mapping):
        _fail("PLAN_RISK_INPUT_INVALID", "$", "must be an object")
    try:
        validate_trade_plan_semantics(plan)
    except TradePlanSemanticError as exc:
        raise PlanRiskInputError(exc.code, exc.field, str(exc)) from exc
    if plan.get("status") != "READY":
        _fail("PLAN_NOT_READY", "status", "plan-admission risk only accepts READY plans")

    portfolio = _mapping(plan.get("portfolio"), "portfolio")
    raw_max_positions = portfolio.get("max_positions")
    if type(raw_max_positions) is not int:
        _fail("PLAN_RISK_INPUT_INVALID", "portfolio.max_positions", "must be an integer")
    raw_items = plan.get("items")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        _fail("PLAN_RISK_INPUT_INVALID", "items", "must be an array")

    items: list[PlanRiskItem] = []
    for index, raw_item in enumerate(raw_items):
        path = f"items[{index}]"
        item = _mapping(raw_item, path)
        try:
            items.append(
                PlanRiskItem(
                    symbol=_security(item, "symbol", f"{path}.symbol"),
                    target_weight=_ratio(item, "target_weight", f"{path}.target_weight"),
                    max_notional=_money(item, "max_notional", f"{path}.max_notional"),
                    entry_policy=_string(item, "entry_policy", f"{path}.entry_policy"),
                    exit_policy=_string(item, "exit_policy", f"{path}.exit_policy"),
                    risk_policy=_string(item, "risk_policy", f"{path}.risk_policy"),
                    execution_policy=_string(
                        item,
                        "execution_policy",
                        f"{path}.execution_policy",
                    ),
                )
            )
        except PlanRiskInputError:
            raise
        except ValueError as exc:
            raise PlanRiskInputError("PLAN_RISK_INPUT_INVALID", path, str(exc)) from exc

    source = _mapping(plan.get("source"), "source")
    try:
        return PlanRiskCandidate(
            plan_id=_string(plan, "plan_id", "plan_id"),
            account_id=_string(plan, "account_id", "account_id"),
            strategy_id=_string(plan, "strategy_id", "strategy_id"),
            algorithm_version=_string(
                source,
                "algorithm_version",
                "source.algorithm_version",
            ),
            target_exposure=_ratio(
                portfolio,
                "target_exposure",
                "portfolio.target_exposure",
            ),
            declared_hard_max_exposure=_ratio(
                portfolio,
                "hard_max_exposure",
                "portfolio.hard_max_exposure",
            ),
            declared_max_positions=raw_max_positions,
            items=tuple(items),
        )
    except PlanRiskInputError:
        raise
    except ValueError as exc:
        raise PlanRiskInputError("PLAN_RISK_INPUT_INVALID", "$", str(exc)) from exc
