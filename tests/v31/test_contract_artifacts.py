from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]


def test_v2_contract_artifacts_are_reproducible() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/export-v2-contracts.py"), "--check"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert "verified" in result.stdout


def test_v2_schemas_and_signed_plan_are_valid_json_schema() -> None:
    for path in (ROOT / "contracts/v2/schemas").glob("*.schema.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))
    schema = json.loads(
        (ROOT / "contracts/v2/schemas/plan-envelope.schema.json").read_text(encoding="utf-8")
    )
    plan = json.loads((ROOT / "contracts/v2/examples/signed-plan.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(plan)
