from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from auto_trading_system.config.credentials import SecretLoadError, SecretProvider
from auto_trading_system.config.loader import (
    MAX_CONFIG_BYTES,
    ConfigurationError,
    calculate_config_hash,
    load_runtime_configuration,
    load_runtime_settings,
)
from auto_trading_system.config.models import RuntimeConfig, SecretReference, SecretSource

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PATH = PROJECT_ROOT / "config" / "v1" / "runtime.read-only.example.json"
HMAC_SECRET = "0123456789abcdef0123456789abcdef"
BROKER_ACCOUNT = "A123456789"
OPERATOR_VERIFIER = (
    "ats-scrypt-v1$32768$8$1$AAECAwQFBgcICQoLDA0ODw$f81LIxrvonq2ysChD17nLqfS0_FxQ1_5qy8BxpPy-n4"
)
ENVIRONMENT = {
    "ATS_SECRET_NODE_HMAC": HMAC_SECRET,
    "ATS_SECRET_BROKER_ACCOUNT": BROKER_ACCOUNT,
    "ATS_SECRET_OPERATOR_PASSWORD_VERIFIER": OPERATOR_VERIFIER,
}


def example_payload() -> dict[str, Any]:
    with EXAMPLE_PATH.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def write_payload(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_configuration_resolves_secrets_and_returns_safe_summary() -> None:
    loaded = load_runtime_configuration(EXAMPLE_PATH, environment=ENVIRONMENT)

    assert loaded.secrets.node_hmac_secret.get_secret_value() == HMAC_SECRET
    assert loaded.secrets.broker_account_id.get_secret_value() == BROKER_ACCOUNT
    assert loaded.secrets.operator_password_verifier.get_secret_value() == OPERATOR_VERIFIER
    assert loaded.config_hash == calculate_config_hash(loaded.settings)
    summary_text = json.dumps(loaded.safe_summary(), ensure_ascii=False)
    assert HMAC_SECRET not in summary_text
    assert BROKER_ACCOUNT not in summary_text
    assert "ATS_SECRET_NODE_HMAC" not in summary_text
    assert loaded.safe_summary()["profile"] == "READ_ONLY"
    assert loaded.config_hash == (
        "sha256:8632dd0ad1717d531c6ecf53361e7507f0a5530b4473a399b9e80f4d310b8d96"
    )


def test_config_hash_uses_normalized_content_not_file_formatting(tmp_path: Path) -> None:
    payload = example_payload()
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    second_path.write_text(json.dumps(dict(reversed(payload.items()))), encoding="utf-8")

    first = load_runtime_configuration(first_path, environment=ENVIRONMENT)
    second = load_runtime_configuration(second_path, environment=ENVIRONMENT)

    assert first.config_hash == second.config_hash


def test_watchdog_can_load_settings_without_resolving_any_secret() -> None:
    settings = load_runtime_settings(EXAMPLE_PATH)

    assert settings.node.node_id == "node-dev-01"
    assert settings.watchdog.heartbeat_interval_seconds == 2
    assert settings.watchdog.crash_limit == 3


@pytest.mark.parametrize(
    ("contents", "expected_code"),
    [
        (b"", "CONFIG_EMPTY"),
        (b"\xff", "CONFIG_JSON_INVALID"),
        (b"[1, 2]", "CONFIG_ROOT_INVALID"),
        (b'{"config_version":"1.0","config_version":"1.0"}', "CONFIG_DUPLICATE_KEY"),
    ],
)
def test_rejects_invalid_configuration_documents(
    tmp_path: Path,
    contents: bytes,
    expected_code: str,
) -> None:
    path = tmp_path / "runtime.json"
    path.write_bytes(contents)

    with pytest.raises(ConfigurationError) as exc_info:
        load_runtime_configuration(path, environment=ENVIRONMENT)

    assert exc_info.value.code == expected_code


def test_rejects_configuration_larger_than_one_mebibyte(tmp_path: Path) -> None:
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))

    with pytest.raises(ConfigurationError) as exc_info:
        load_runtime_configuration(path, environment=ENVIRONMENT)

    assert exc_info.value.code == "CONFIG_TOO_LARGE"


def test_missing_configuration_file_is_a_stable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        load_runtime_configuration(tmp_path / "missing.json", environment=ENVIRONMENT)

    assert exc_info.value.code == "CONFIG_READ_FAILED"


def test_validation_error_does_not_echo_invalid_input(tmp_path: Path) -> None:
    payload = example_payload()
    payload["central_api"]["base_url"] = "http://sensitive-host.invalid/not-valid"
    path = tmp_path / "invalid.json"
    write_payload(path, payload)

    with pytest.raises(ConfigurationError) as exc_info:
        load_runtime_configuration(path, environment=ENVIRONMENT)

    rendered = str(exc_info.value)
    assert exc_info.value.code == "CONFIG_VALIDATION_FAILED"
    assert "sensitive-host" not in rendered
    assert "central_api.base_url" in rendered


@pytest.mark.parametrize(
    "environment",
    [
        {"ATS_SECRET_BROKER_ACCOUNT": BROKER_ACCOUNT},
        {
            "ATS_SECRET_NODE_HMAC": "short",
            "ATS_SECRET_BROKER_ACCOUNT": BROKER_ACCOUNT,
            "ATS_SECRET_OPERATOR_PASSWORD_VERIFIER": OPERATOR_VERIFIER,
        },
    ],
)
def test_secret_load_failure_never_echoes_secret(
    environment: dict[str, str],
) -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        load_runtime_configuration(EXAMPLE_PATH, environment=environment)

    rendered = str(exc_info.value)
    assert exc_info.value.code == "CONFIG_SECRET_LOAD_FAILED"
    assert HMAC_SECRET not in rendered
    assert BROKER_ACCOUNT not in rendered
    assert "short" not in rendered


class StaticSecretProvider:
    source = SecretSource.WINDOWS_CREDENTIAL_MANAGER

    def read(self, reference: SecretReference) -> SecretStr:
        identity = getattr(reference, "target", "")
        return SecretStr(
            OPERATOR_VERIFIER if identity.endswith("operator-password") else HMAC_SECRET
        )


class LeakingSecretProvider:
    source = SecretSource.WINDOWS_CREDENTIAL_MANAGER

    def read(self, reference: SecretReference) -> SecretStr:
        del reference
        raise SecretLoadError("PROVIDER_FAILED", f"provider accidentally included {HMAC_SECRET}")


def test_injected_provider_supports_secure_profile(tmp_path: Path) -> None:
    payload = example_payload()
    payload["profile"] = "LIVE_EXIT_ONLY"
    payload["security"]["node_hmac_secret"] = {
        "source": "WINDOWS_CREDENTIAL_MANAGER",
        "target": "AutoTrading/node-dev-01/key-dev-001",
    }
    payload["security"]["broker_account_id"] = {
        "source": "WINDOWS_CREDENTIAL_MANAGER",
        "target": "AutoTrading/node-dev-01/broker-account",
    }
    payload["security"]["operator_password_verifier"] = {
        "source": "WINDOWS_CREDENTIAL_MANAGER",
        "target": "AutoTrading/node-dev-01/operator-password",
    }
    path = tmp_path / "secure.json"
    write_payload(path, payload)
    provider: SecretProvider = StaticSecretProvider()

    loaded = load_runtime_configuration(
        path,
        providers={SecretSource.WINDOWS_CREDENTIAL_MANAGER: provider},
    )

    assert loaded.settings.profile.value == "LIVE_EXIT_ONLY"


def test_unavailable_secure_provider_fails_closed(tmp_path: Path) -> None:
    payload = example_payload()
    payload["profile"] = "LIVE"
    reference = {
        "source": "WINDOWS_CREDENTIAL_MANAGER",
        "target": "AutoTrading/node-dev-01/credential",
    }
    payload["security"]["node_hmac_secret"] = reference
    payload["security"]["broker_account_id"] = reference
    payload["security"]["operator_password_verifier"] = reference
    path = tmp_path / "secure.json"
    write_payload(path, payload)

    with pytest.raises(ConfigurationError) as exc_info:
        load_runtime_configuration(path, environment={})

    assert exc_info.value.code == "CONFIG_SECRET_LOAD_FAILED"
    assert "AutoTrading/node-dev-01/credential" not in str(exc_info.value)
    assert any(
        code in str(exc_info.value)
        for code in ("SECRET_NOT_FOUND", "WINDOWS_CREDENTIAL_MANAGER_UNAVAILABLE")
    )


def test_provider_error_detail_is_not_propagated(tmp_path: Path) -> None:
    payload = example_payload()
    payload["profile"] = "ASSISTED"
    reference = {
        "source": "WINDOWS_CREDENTIAL_MANAGER",
        "target": "AutoTrading/node-dev-01/credential",
    }
    payload["security"]["node_hmac_secret"] = reference
    payload["security"]["broker_account_id"] = reference
    payload["security"]["operator_password_verifier"] = reference
    path = tmp_path / "secure.json"
    write_payload(path, payload)
    provider: SecretProvider = LeakingSecretProvider()

    with pytest.raises(ConfigurationError) as exc_info:
        load_runtime_configuration(
            path,
            providers={SecretSource.WINDOWS_CREDENTIAL_MANAGER: provider},
        )

    assert "PROVIDER_FAILED" in str(exc_info.value)
    assert HMAC_SECRET not in str(exc_info.value)


def test_calculate_config_hash_has_stable_shape() -> None:
    settings = RuntimeConfig.model_validate(example_payload())

    digest = calculate_config_hash(settings)

    assert digest.startswith("sha256:")
    assert len(digest) == 71
