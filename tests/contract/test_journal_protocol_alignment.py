from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from auto_trading_system.journal import (
    AggregateType,
    EventSeverity,
    EventSource,
    EventType,
    OutboxKind,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVENT_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "v1" / "domain-event.schema.json"


def event_schema() -> dict[str, Any]:
    parsed = json.loads(EVENT_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def enum_values(
    enum_type: type[EventSource | EventSeverity | EventType | AggregateType],
) -> set[str]:
    return {item.value for item in enum_type}


def test_event_draft_enums_match_published_domain_event_contract() -> None:
    properties = event_schema()["properties"]

    assert enum_values(EventSource) == set(properties["source"]["enum"])
    assert enum_values(EventSeverity) == set(properties["severity"]["enum"])
    assert enum_values(EventType) == set(properties["event_type"]["enum"])
    assert enum_values(AggregateType) == set(properties["aggregate_type"]["enum"])


def test_outbox_kinds_cover_durable_node_messages_and_exclude_heartbeat() -> None:
    assert {item.value for item in OutboxKind} == {
        "DOMAIN_EVENT",
        "ASSET_SNAPSHOT",
        "POSITION_SNAPSHOT",
        "RECONCILIATION",
        "COMMAND_RESULT",
    }
