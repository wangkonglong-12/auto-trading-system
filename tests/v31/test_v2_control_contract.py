from __future__ import annotations

from typing import Any

import pytest

from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.daily_sync import LocalControlRequest, SessionGrant
from auto_trading_system.protocol.v2.openapi import document


def grant() -> SessionGrant:
    return SessionGrant.model_validate_json(
        canonical_bytes(
            {
                "grant_id": "grant-1",
                "account_id": "test-account",
                "engine_boot_id": "boot-1",
                "session_generation": 1,
                "mode": "LIVE_EXIT_ONLY",
                "directions": ["SELL"],
                "max_buy_value": "0",
                "max_order_value": "1000",
                "max_positions": 1,
                "valid_from": "2026-09-07T09:00:00+08:00",
                "valid_until": "2026-09-07T15:01:00+08:00",
                "confirmed_by": "operator-1",
                "recovery_report_hash": "sha256:" + "1" * 64,
            }
        )
    )


@pytest.mark.parametrize("action", ["HALT", "PAUSE", "SNAPSHOT", "REQUEST_RECOVERY", "MAINTENANCE"])
def test_restrictive_controls_carry_no_implicit_permission(action: str) -> None:
    fields: dict[str, Any] = {
        "protocol_major": 2,
        "request_id": "request-1",
        "engine_boot_id": "boot-1",
        "sender_role": "agent",
        "deadline": "2026-09-07T10:00:00+08:00",
        "expected_state_version": 1,
        "action": action,
    }
    assert LocalControlRequest.model_validate_json(canonical_bytes(fields)).grant is None
    fields["grant"] = grant().model_dump(mode="json")
    with pytest.raises(ValueError, match="ARM/RESUME"):
        LocalControlRequest.model_validate_json(canonical_bytes(fields))


def test_expansions_require_boot_bound_grant_and_operator_authorization() -> None:
    body: dict[str, Any] = {
        "protocol_major": 2,
        "request_id": "request-1",
        "engine_boot_id": "boot-1",
        "sender_role": "agent",
        "deadline": "2026-09-07T10:00:00+08:00",
        "expected_state_version": 1,
        "action": "ARM",
    }
    with pytest.raises(ValueError):
        LocalControlRequest.model_validate_json(canonical_bytes(body))
    body.update(grant=grant().model_dump(mode="json"), operator_authorization_id="operator-once-1")
    assert LocalControlRequest.model_validate_json(canonical_bytes(body)).grant == grant()
    for request_change in (
        {"engine_boot_id": "old-boot"},
        {"sender_role": "supervisor"},
        {"artifact_hash": "sha256:" + "1" * 64},
        {"trade_date": "2026-09-07"},
    ):
        with pytest.raises(ValueError):
            LocalControlRequest.model_validate_json(canonical_bytes({**body, **request_change}))
    grant_changes: tuple[dict[str, Any], ...] = (
        {"directions": ["BUY"]},
        {"directions": ["SELL", "SELL"]},
        {"valid_until": "2026-09-08T15:01:00+08:00"},
    )
    for change in grant_changes:
        with pytest.raises(ValueError):
            SessionGrant.model_validate_json(
                canonical_bytes({**grant().model_dump(mode="json"), **change})
            )


def test_openapi_contains_nine_routes_typed_datasets_and_resolvable_references() -> None:
    spec = document()
    assert len(spec["paths"]) == 9 and len(spec["x-dataset-schemas"]) == 8

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                target: Any = spec
                for segment in value["$ref"].removeprefix("#/").split("/"):
                    target = target[segment]
                assert isinstance(target, dict)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(spec)
    for operations in spec["paths"].values():
        for method, operation in operations.items():
            assert operation["security"] == [{"NodeHmac": []}]
            if method in ("post", "put"):
                assert operation["requestBody"]["required"]
