from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from auto_trading_system.protocol import calculate_plan_hash, validate_trade_plan_semantics
from auto_trading_system.protocol.trade_plan import TradePlanSemanticError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = PROJECT_ROOT / "contracts" / "v1"
SCHEMA_PATH = CONTRACT_ROOT / "trade-plan.schema.json"
EXAMPLES_PATH = CONTRACT_ROOT / "examples"
EXPECTED_EXAMPLES = {
    "trade-plan.expired.json",
    "trade-plan.no-trade.json",
    "trade-plan.partial-shadow.json",
    "trade-plan.ready.json",
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return load_json(SCHEMA_PATH)


@pytest.fixture(scope="module")
def validator(schema: dict[str, Any]) -> Draft202012Validator:
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def example_paths() -> tuple[Path, ...]:
    return tuple(sorted(EXAMPLES_PATH.glob("*.json")))


def test_expected_example_set_is_complete() -> None:
    assert {path.name for path in example_paths()} == EXPECTED_EXAMPLES


@pytest.mark.parametrize("example_path", example_paths(), ids=lambda path: path.stem)
def test_example_matches_schema(
    validator: Draft202012Validator,
    example_path: Path,
) -> None:
    errors = sorted(
        validator.iter_errors(load_json(example_path)),
        key=lambda error: list(error.path),
    )
    assert errors == []


@pytest.mark.parametrize("example_path", example_paths(), ids=lambda path: path.stem)
def test_example_hash_and_semantics_are_valid(example_path: Path) -> None:
    plan = load_json(example_path)
    assert plan["plan_hash"] == calculate_plan_hash(plan)
    validate_trade_plan_semantics(plan)


def test_schema_rejects_unknown_top_level_field(validator: Draft202012Validator) -> None:
    plan = load_json(EXAMPLES_PATH / "trade-plan.ready.json")
    plan["unexpected"] = "value"
    assert not validator.is_valid(plan)


def test_schema_rejects_live_eligible_partial_plan(validator: Draft202012Validator) -> None:
    plan = load_json(EXAMPLES_PATH / "trade-plan.partial-shadow.json")
    plan["live_eligible"] = True
    plan["execution_mode"] = "LIVE"
    assert not validator.is_valid(plan)


@pytest.mark.parametrize(
    ("field_path", "zero_value"),
    [
        (("items", 0, "max_notional"), "0.00"),
        (("items", 0, "price_guard", "reference_price"), "0"),
    ],
)
def test_schema_rejects_zero_for_positive_values(
    validator: Draft202012Validator,
    field_path: tuple[str | int, ...],
    zero_value: str,
) -> None:
    plan = load_json(EXAMPLES_PATH / "trade-plan.ready.json")
    container: Any = plan
    for component in field_path[:-1]:
        container = container[component]
    container[field_path[-1]] = zero_value
    assert not validator.is_valid(plan)


def test_semantics_reject_timeline_regression() -> None:
    plan = copy.deepcopy(load_json(EXAMPLES_PATH / "trade-plan.ready.json"))
    plan["entry_start"] = "2026-09-03T14:58:00+08:00"
    plan["plan_hash"] = calculate_plan_hash(plan)

    with pytest.raises(TradePlanSemanticError, match="INVALID_PLAN_TIMELINE"):
        validate_trade_plan_semantics(plan)


def test_semantics_reject_exposure_above_hard_max() -> None:
    plan = copy.deepcopy(load_json(EXAMPLES_PATH / "trade-plan.ready.json"))
    plan["portfolio"]["target_exposure"] = "0.70"
    plan["plan_hash"] = calculate_plan_hash(plan)

    with pytest.raises(TradePlanSemanticError, match="EXPOSURE_EXCEEDS_HARD_MAX"):
        validate_trade_plan_semantics(plan)


def test_semantics_reject_modified_plan_with_stale_hash() -> None:
    plan = copy.deepcopy(load_json(EXAMPLES_PATH / "trade-plan.ready.json"))
    plan["portfolio"]["target_exposure"] = "0.29"

    with pytest.raises(TradePlanSemanticError, match="PLAN_HASH_MISMATCH"):
        validate_trade_plan_semantics(plan)
