from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from auto_trading_system.domain import Money, Ratio, SecurityCode
from auto_trading_system.protocol import (
    calculate_event_hash,
    validate_domain_event_semantics,
)
from auto_trading_system.risk import (
    HardSafetyEvaluator,
    HardSafetyFacts,
    HardSafetyLimits,
    HardSafetyOrder,
    HardSafetySide,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_mapping(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return cast(dict[str, Any], copy.deepcopy(value))


def test_windows_hard_safety_payload_matches_frozen_risk_event_contract() -> None:
    decision = HardSafetyEvaluator(
        HardSafetyLimits(
            "WINDOWS_HARD_SAFETY_V1",
            15,
            Ratio.parse("0.60"),
            Ratio.parse("0.05"),
            3,
            1,
            Money.parse("5000"),
            Money.parse("15000"),
            Decimal("1.5"),
            30,
        )
    ).evaluate(
        HardSafetyOrder(
            "ord_01K43HARDEVENT0001",
            "acct_demo_001",
            SecurityCode.parse("000001.SZ"),
            HardSafetySide.BUY,
            Decimal("1234"),
            Money.parse("5000"),
        ),
        HardSafetyFacts(
            "acct_demo_001",
            True,
            True,
            Money.parse("100000"),
            Money.parse("0"),
            (),
            (),
            Money.parse("0"),
            Decimal("0.5"),
            Decimal("1"),
        ),
        decision_id="risk_hard_01K43HARDEVENT0001",
    )
    event = load_mapping(
        PROJECT_ROOT / "contracts" / "v1" / "examples" / "events" / "event.risk-decision.json"
    )
    event["aggregate_id"] = decision.decision_id
    event["causation_id"] = decision.client_order_id
    event["payload"] = decision.to_event_payload()
    event["event_hash"] = calculate_event_hash(event)
    schema = load_mapping(PROJECT_ROOT / "contracts" / "v1" / "domain-event.schema.json")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    assert list(validator.iter_errors(event)) == []
    validate_domain_event_semantics(event)
