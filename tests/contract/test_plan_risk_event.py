from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from auto_trading_system.domain import Money, Ratio, SecurityCode
from auto_trading_system.protocol import (
    calculate_event_hash,
    validate_domain_event_semantics,
)
from auto_trading_system.risk import (
    MarketLiquidity,
    PlanRiskContext,
    PlanRiskEvaluator,
    PlanRiskPolicy,
    parse_trade_plan_risk_candidate,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_mapping(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return cast(dict[str, Any], copy.deepcopy(value))


def test_plan_risk_decision_payload_matches_frozen_domain_event_contract() -> None:
    plan = load_mapping(PROJECT_ROOT / "contracts" / "v1" / "examples" / "trade-plan.ready.json")
    candidate = parse_trade_plan_risk_candidate(plan)
    decision = PlanRiskEvaluator(
        PlanRiskPolicy(
            "PLAN_RISK_V1",
            "TAIL_CLOSE",
            frozenset({"tail_1431_v5_6_selection_eligibility_guard"}),
            frozenset({"TAIL_ENTRY_V1"}),
            frozenset({"T1_EXIT_V1"}),
            frozenset({"STOCK_RISK_V1"}),
            frozenset({"EXEC_BALANCED_V1"}),
            15,
            Ratio.parse("0.60"),
            Ratio.parse("0.05"),
            Ratio.parse("0.05"),
            Money.parse("20000"),
        )
    ).evaluate(
        candidate,
        PlanRiskContext(
            "acct_demo_001",
            Money.parse("100000"),
            Money.parse("20000"),
            Ratio.parse("0.10"),
            (),
            (
                MarketLiquidity(
                    SecurityCode.parse("000001.SZ"),
                    Money.parse("200000"),
                ),
            ),
        ),
        decision_id="risk_plan_01K43TAIL000000001",
    )
    event = load_mapping(
        PROJECT_ROOT / "contracts" / "v1" / "examples" / "events" / "event.risk-decision.json"
    )
    event["aggregate_id"] = decision.decision_id
    event["causation_id"] = candidate.plan_id
    event["payload"] = decision.to_event_payload()
    event["event_hash"] = calculate_event_hash(event)

    schema = load_mapping(PROJECT_ROOT / "contracts" / "v1" / "domain-event.schema.json")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    assert list(validator.iter_errors(event)) == []
    validate_domain_event_semantics(event)
