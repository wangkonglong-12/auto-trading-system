from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "v1"
EXPECTED_SHA256 = {
    "plan-ack-request.schema.json": (
        "0c3f1a5a0013d0aa8b2cbf525114b89595b0806487d96459880e9f0ec155fef6"
    ),
    "plan-ack-result.schema.json": (
        "cf3a453fcec9373c9b4891b76585dade6f9a159a9988579cb60cc38cc317b300"
    ),
    "examples/acks/plan-ack.accepted.json": (
        "3cb54906afddfcbcf88c5bb761be8068d447cf23ae5199c71c3f8870c34c4dbb"
    ),
    "examples/acks/plan-ack.rejected.json": (
        "38cd6c728fd5ab8201f605b0473429cee7f7aa5ea070639464242d77ad362d9f"
    ),
    "examples/acks/plan-ack-result.created.json": (
        "b5533c7a12e0efe056bc8f2359746faeee8140d459414f2412c867749e157b18"
    ),
}


def _load(relative: str) -> dict[str, Any]:
    with (CONTRACT_ROOT / relative).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TypeError("contract JSON must be an object")
    return cast(dict[str, Any], value)


def test_plan_ack_files_match_frozen_cross_project_hashes() -> None:
    for relative, expected in EXPECTED_SHA256.items():
        payload = (CONTRACT_ROOT / relative).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected


@pytest.mark.parametrize(
    ("schema_name", "example_name"),
    [
        ("plan-ack-request.schema.json", "plan-ack.accepted.json"),
        ("plan-ack-request.schema.json", "plan-ack.rejected.json"),
        ("plan-ack-result.schema.json", "plan-ack-result.created.json"),
    ],
)
def test_plan_ack_examples_match_schema(schema_name: str, example_name: str) -> None:
    schema = _load(schema_name)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    assert list(validator.iter_errors(_load(f"examples/acks/{example_name}"))) == []


def test_ack_schema_rejects_reasons_on_accepted_status() -> None:
    validator = Draft202012Validator(_load("plan-ack-request.schema.json"))
    payload = _load("examples/acks/plan-ack.accepted.json")
    payload["reject_reasons"] = [{"code": "UNEXPECTED", "message": "must be rejected"}]

    assert not validator.is_valid(payload)
