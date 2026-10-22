from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from auto_trading_system.config.models import RuntimeConfig
from auto_trading_system.config.schema import (
    export_runtime_config_schema,
    main,
    runtime_config_schema,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = PROJECT_ROOT / "config" / "v1" / "runtime-config.schema.json"
EXAMPLE_PATH = PROJECT_ROOT / "config" / "v1" / "runtime.read-only.example.json"


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


def test_committed_schema_matches_runtime_model() -> None:
    assert load_json(SCHEMA_PATH) == runtime_config_schema()


def test_schema_is_valid_and_accepts_read_only_example() -> None:
    schema = load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    assert list(validator.iter_errors(load_json(EXAMPLE_PATH))) == []


def test_model_and_schema_reject_unknown_fields() -> None:
    payload = load_json(EXAMPLE_PATH)
    payload["unknown"] = "unsafe"
    validator = Draft202012Validator(load_json(SCHEMA_PATH))

    assert not validator.is_valid(payload)
    settings = RuntimeConfig.model_validate(json.loads(json.dumps(load_json(EXAMPLE_PATH))))
    assert settings.config_version == "1.0"


def test_schema_requires_decimal_strings() -> None:
    payload = load_json(EXAMPLE_PATH)
    payload["hard_safety"]["hard_max_exposure"] = 0.6
    validator = Draft202012Validator(load_json(SCHEMA_PATH))

    assert not validator.is_valid(payload)


def test_schema_export_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    export_runtime_config_schema(first)
    assert main(["--output", str(second)]) == 0

    assert first.read_bytes() == second.read_bytes()
    assert load_json(first) == load_json(SCHEMA_PATH)
