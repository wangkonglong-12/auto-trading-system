"""Runtime configuration JSON Schema generation."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from auto_trading_system.config.models import RuntimeConfig
from auto_trading_system.config.node_v2 import NodeProcessConfig


def runtime_config_schema() -> dict[str, Any]:
    """Return the canonical RuntimeConfig v1.0 JSON Schema."""
    schema = RuntimeConfig.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://contracts.auto-trading.local/config/v1/runtime-config.schema.json"
    schema["title"] = "Auto Trading Windows Runtime Configuration v1.0"
    return schema


def export_runtime_config_schema(path: Path) -> None:
    """Write a deterministic UTF-8 schema artifact."""
    serialized = json.dumps(
        runtime_config_schema(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    path.write_text(f"{serialized}\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """Export the versioned schema for packaging and review."""
    parser = argparse.ArgumentParser(prog="runtime-config-schema")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--node-v2", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.node_v2:
        arguments.output.write_text(
            json.dumps(NodeProcessConfig.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        export_runtime_config_schema(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
