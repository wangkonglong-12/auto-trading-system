from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from auto_trading_system.config.models import EnvironmentProfile, RuntimeConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PATH = PROJECT_ROOT / "config" / "v1" / "runtime.read-only.example.json"


def example_payload() -> dict[str, Any]:
    with EXAMPLE_PATH.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def test_read_only_example_builds_immutable_config() -> None:
    settings = RuntimeConfig.model_validate(example_payload())

    assert settings.profile is EnvironmentProfile.READ_ONLY
    assert settings.restart_mode == "READY_PAUSED"
    assert str(settings.hard_safety.hard_max_exposure) == "0.60"
    assert settings.runtime_queues.normal_capacity == 10_000
    assert settings.runtime_queues.critical_capacity == 256
    assert settings.watchdog.heartbeat_stale_timeout_seconds == 10
    with pytest.raises(ValidationError, match="frozen_instance"):
        settings.profile = EnvironmentProfile.LIVE


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("config_version",), "2.0", "Input should be '1.0'"),
        (("central_api", "base_url"), "http://example.test/api/v1/trading", "must use HTTPS"),
        (("central_api", "base_url"), "https://example.test/wrong", "must be /api/v1/trading"),
        (("central_api", "verify_tls"), False, "Input should be True"),
        (("local_api", "host"), "0.0.0.0", "Input should be '127.0.0.1'"),
        (("qmt", "userdata_mini"), "relative/userdata_mini", "must be an absolute path"),
        (("qmt", "userdata_mini"), "C:\\QMT\\other", "must end with"),
        (("storage", "root"), "relative", "must be an absolute path"),
        (("hard_safety", "max_positions"), 16, "less than or equal to 15"),
        (("hard_safety", "max_clock_skew_seconds"), 31, "less than or equal to 30"),
        (("runtime_queues", "normal_capacity"), 99, "greater than or equal to 100"),
        (("runtime_queues", "critical_capacity"), 15, "greater than or equal to 16"),
        (("sync", "event_batch_size"), 51, "less than or equal to 50"),
        (("watchdog", "crash_limit"), 1, "greater than or equal to 2"),
    ],
)
def test_rejects_unsafe_or_invalid_fields(
    path: tuple[str, ...],
    value: Any,
    message: str,
) -> None:
    payload = example_payload()
    container = payload
    for component in path[:-1]:
        child = container[component]
        assert isinstance(child, dict)
        container = child
    container[path[-1]] = value

    with pytest.raises(ValidationError, match=message):
        RuntimeConfig.model_validate(payload)


def test_rejects_unknown_configuration_field() -> None:
    payload = example_payload()
    payload["unsafe_override"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RuntimeConfig.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("outbox_retry_max_seconds", 1, "at least the base delay"),
        ("outbox_critical_watermark", 50_000, "must exceed the high watermark"),
    ],
)
def test_rejects_inverted_outbox_retry_and_pressure_limits(
    field: str,
    value: int,
    message: str,
) -> None:
    payload = example_payload()
    payload["sync"][field] = value

    with pytest.raises(ValidationError, match=message):
        RuntimeConfig.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("heartbeat_stale_timeout_seconds", 5, "three heartbeat intervals"),
        ("heartbeat_startup_timeout_seconds", 9, "greater than or equal to 10"),
        ("restart_max_delay_seconds", 1, "greater than or equal to 2"),
        ("crash_window_seconds", 59, "greater than or equal to 60"),
    ],
)
def test_rejects_unsafe_watchdog_timing(
    field: str,
    value: int,
    message: str,
) -> None:
    payload = example_payload()
    payload["watchdog"][field] = value

    with pytest.raises(ValidationError, match=message):
        RuntimeConfig.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("qmt", "session_id", "131001"),
        ("recorder", "enabled", "false"),
        ("hard_safety", "max_positions", "15"),
        ("hard_safety", "hard_max_exposure", 0.6),
        ("runtime_queues", "normal_capacity", "10000"),
    ],
)
def test_rejects_silent_type_coercion(
    section: str,
    field: str,
    value: Any,
) -> None:
    payload = example_payload()
    payload[section][field] = value

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(payload)


def test_rejects_single_position_limit_above_total_limit() -> None:
    payload = example_payload()
    payload["hard_safety"]["max_single_position_exposure"] = "0.70"

    with pytest.raises(ValidationError, match="single-position exposure"):
        RuntimeConfig.model_validate(payload)


def test_rejects_single_order_limit_above_daily_limit() -> None:
    payload = example_payload()
    payload["hard_safety"]["max_single_order_notional"] = "20000.00"

    with pytest.raises(ValidationError, match="single-order notional"):
        RuntimeConfig.model_validate(payload)


def test_rejects_storage_root_equal_to_qmt_state_directory() -> None:
    payload = example_payload()
    payload["storage"]["root"] = payload["qmt"]["userdata_mini"]

    with pytest.raises(ValidationError, match="must not be the QMT userdata_mini"):
        RuntimeConfig.model_validate(payload)


@pytest.mark.parametrize("profile", ["ASSISTED", "LIVE_EXIT_ONLY", "LIVE"])
def test_real_order_profiles_require_windows_credential_references(profile: str) -> None:
    payload = example_payload()
    payload["profile"] = profile

    with pytest.raises(ValidationError, match="require Windows Credential Manager"):
        RuntimeConfig.model_validate(payload)


def test_real_order_profile_accepts_windows_credential_references() -> None:
    payload = example_payload()
    payload["profile"] = "ASSISTED"
    payload["security"]["node_hmac_secret"] = {
        "source": "WINDOWS_CREDENTIAL_MANAGER",
        "target": "AutoTrading/node-dev-01/key-dev-001",
    }
    payload["security"]["broker_account_id"] = {
        "source": "WINDOWS_CREDENTIAL_MANAGER",
        "target": "AutoTrading/node-dev-01/broker-account",
        "encoding": "utf-8",
    }
    payload["security"]["operator_password_verifier"] = {
        "source": "WINDOWS_CREDENTIAL_MANAGER",
        "target": "AutoTrading/node-dev-01/operator-password",
    }

    settings = RuntimeConfig.model_validate(payload)

    assert settings.profile is EnvironmentProfile.ASSISTED


@pytest.mark.parametrize(
    "target",
    [
        "AutoTrading/node",
        "AutoTrading/node//secret",
        "AutoTrading/../secret",
        "AutoTrading/node/./secret",
    ],
)
def test_windows_credential_target_requires_canonical_segments(target: str) -> None:
    payload = example_payload()
    payload["security"]["node_hmac_secret"] = {
        "source": "WINDOWS_CREDENTIAL_MANAGER",
        "target": target,
    }

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(payload)
