from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from auto_trading_system.protocol import calculate_plan_hash, validate_trade_plan_semantics
from auto_trading_system.protocol.trade_plan import TradePlanSemanticError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
READY_PLAN_PATH = PROJECT_ROOT / "contracts" / "v1" / "examples" / "trade-plan.ready.json"
Plan = dict[str, Any]
Mutation = Callable[[Plan], None]


def ready_plan() -> Plan:
    with READY_PLAN_PATH.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def update_hash(plan: Plan) -> None:
    plan["plan_hash"] = calculate_plan_hash(plan)


def set_value(path: tuple[str, ...], value: Any) -> Mutation:
    def mutate(plan: Plan) -> None:
        container: Plan = plan
        for component in path[:-1]:
            child = container[component]
            assert isinstance(child, dict)
            container = child
        container[path[-1]] = value

    return mutate


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (set_value(("schema_version",), 1), "INVALID_SCHEMA_VERSION"),
        (set_value(("schema_version",), "2.0"), "PLAN_VERSION_UNSUPPORTED"),
        (set_value(("timezone",), "UTC"), "INVALID_TIMEZONE"),
        (set_value(("trade_date",), 20260903), "INVALID_TRADE_DATE"),
        (set_value(("trade_date",), "2026-02-30"), "INVALID_TRADE_DATE"),
        (set_value(("generated_at",), 1), "INVALID_TIMESTAMP"),
        (set_value(("generated_at",), "not-a-date"), "INVALID_TIMESTAMP"),
        (set_value(("generated_at",), "2026-09-03T14:40:00+00:00"), "INVALID_TIMEZONE_OFFSET"),
        (set_value(("expire_at",), "2026-09-04T15:01:00+08:00"), "TRADE_DATE_MISMATCH"),
        (set_value(("items",), {}), "INVALID_ITEMS"),
        (set_value(("items",), "invalid"), "INVALID_ITEMS"),
        (set_value(("items",), []), "READY_PLAN_EMPTY"),
        (set_value(("source",), []), "INVALID_SOURCE"),
        (set_value(("portfolio",), []), "INVALID_PORTFOLIO"),
        (set_value(("portfolio", "target_exposure"), 0.3), "INVALID_DECIMAL"),
        (set_value(("portfolio", "target_exposure"), "invalid"), "INVALID_DECIMAL"),
        (set_value(("portfolio", "target_exposure"), "1.01"), "RATIO_OUT_OF_RANGE"),
        (set_value(("portfolio", "cash_reserve"), "0.80"), "CASH_RESERVE_VIOLATION"),
    ],
    ids=[
        "schema-version-type",
        "schema-version-major",
        "timezone-name",
        "trade-date-type",
        "trade-date-value",
        "timestamp-type",
        "timestamp-value",
        "timestamp-offset",
        "trade-date-mismatch",
        "items-object",
        "items-string",
        "ready-items-empty",
        "source-type",
        "portfolio-type",
        "ratio-type",
        "ratio-value",
        "ratio-range",
        "cash-reserve",
    ],
)
def test_rejects_invalid_semantic_invariants(mutation: Mutation, expected_code: str) -> None:
    plan = ready_plan()
    mutation(plan)
    update_hash(plan)

    with pytest.raises(TradePlanSemanticError) as exc_info:
        validate_trade_plan_semantics(plan)

    assert exc_info.value.code == expected_code


def test_no_trade_plan_cannot_contain_items() -> None:
    plan = ready_plan()
    plan["status"] = "NO_TRADE"
    update_hash(plan)

    with pytest.raises(TradePlanSemanticError, match="NO_TRADE_HAS_ITEMS"):
        validate_trade_plan_semantics(plan)


def test_live_plan_requires_full_source_data() -> None:
    plan = ready_plan()
    plan["live_eligible"] = True
    plan["source"]["data_completeness"] = "partial"
    update_hash(plan)

    with pytest.raises(TradePlanSemanticError, match="INCOMPLETE_PLAN_LIVE_ELIGIBLE"):
        validate_trade_plan_semantics(plan)


def test_live_plan_requires_successful_source_run() -> None:
    plan = ready_plan()
    plan["live_eligible"] = True
    plan["source"]["run_status"] = "failed"
    update_hash(plan)

    with pytest.raises(TradePlanSemanticError, match="SOURCE_NOT_SUCCEEDED"):
        validate_trade_plan_semantics(plan)


@pytest.mark.parametrize(
    ("field", "expected_code"),
    [
        ("intent_id", "DUPLICATE_INTENT_ID"),
        ("symbol", "DUPLICATE_SYMBOL"),
        ("rank", "DUPLICATE_RANK"),
    ],
)
def test_plan_item_identities_are_unique(field: str, expected_code: str) -> None:
    plan = ready_plan()
    duplicate = copy.deepcopy(plan["items"][0])
    duplicate["intent_id"] = "intent_01K43TAIL000000099"
    duplicate["symbol"] = "600000.SH"
    duplicate["rank"] = 2
    duplicate[field] = plan["items"][0][field]
    plan["items"].append(duplicate)
    update_hash(plan)

    with pytest.raises(TradePlanSemanticError) as exc_info:
        validate_trade_plan_semantics(plan)

    assert exc_info.value.code == expected_code


def test_item_weights_cannot_exceed_portfolio_target() -> None:
    plan = ready_plan()
    plan["items"][0]["target_weight"] = "0.31"
    update_hash(plan)

    with pytest.raises(TradePlanSemanticError, match="ITEM_WEIGHT_EXCEEDS_TARGET"):
        validate_trade_plan_semantics(plan)


@pytest.mark.parametrize(
    ("invalid_item", "expected_code"),
    [
        ("invalid", "INVALID_ITEM"),
        ({"intent_id": 1, "symbol": "000001.SZ", "rank": 1}, "INVALID_ITEM_IDENTITY"),
    ],
)
def test_invalid_item_shape_is_rejected(invalid_item: Any, expected_code: str) -> None:
    plan = ready_plan()
    plan["items"] = [invalid_item]
    update_hash(plan)

    with pytest.raises(TradePlanSemanticError) as exc_info:
        validate_trade_plan_semantics(plan)

    assert exc_info.value.code == expected_code
