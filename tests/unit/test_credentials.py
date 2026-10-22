from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from auto_trading_system.config.credentials import (
    EnvironmentSecretProvider,
    RuntimeSecrets,
    SecretLoadError,
)
from auto_trading_system.config.models import (
    EnvironmentSecretReference,
    SecretSource,
    WindowsCredentialReference,
    assert_environment_secret_name,
)

HMAC_SECRET = "0123456789abcdef0123456789abcdef"
BROKER_ACCOUNT = "A123456789"
OPERATOR_VERIFIER = (
    "ats-scrypt-v1$32768$8$1$AAECAwQFBgcICQoLDA0ODw$f81LIxrvonq2ysChD17nLqfS0_FxQ1_5qy8BxpPy-n4"
)


def environment_reference() -> EnvironmentSecretReference:
    return EnvironmentSecretReference(
        source=SecretSource.ENVIRONMENT,
        variable="ATS_SECRET_NODE_HMAC",
    )


def test_environment_provider_returns_redacting_secret() -> None:
    provider = EnvironmentSecretProvider({"ATS_SECRET_NODE_HMAC": HMAC_SECRET})

    secret = provider.read(environment_reference())

    assert secret.get_secret_value() == HMAC_SECRET
    assert HMAC_SECRET not in repr(secret)


@pytest.mark.parametrize("environment", [{}, {"ATS_SECRET_NODE_HMAC": "   "}])
def test_environment_provider_rejects_missing_or_blank_secret(
    environment: dict[str, str],
) -> None:
    provider = EnvironmentSecretProvider(environment)

    with pytest.raises(SecretLoadError, match="SECRET_NOT_FOUND"):
        provider.read(environment_reference())


def test_environment_provider_rejects_reference_type_mismatch() -> None:
    provider = EnvironmentSecretProvider({})
    reference = WindowsCredentialReference(
        source=SecretSource.WINDOWS_CREDENTIAL_MANAGER,
        target="AutoTrading/node-dev-01/secret",
    )

    with pytest.raises(SecretLoadError, match="SECRET_REFERENCE_MISMATCH"):
        provider.read(reference)


def test_runtime_secrets_never_render_plaintext() -> None:
    secrets = RuntimeSecrets(
        node_hmac_secret=SecretStr(HMAC_SECRET),
        broker_account_id=SecretStr(BROKER_ACCOUNT),
        operator_password_verifier=SecretStr(OPERATOR_VERIFIER),
    )

    rendered = f"{secrets!r}\n{secrets.model_dump_json()}"
    assert HMAC_SECRET not in rendered
    assert BROKER_ACCOUNT not in rendered
    assert OPERATOR_VERIFIER not in rendered
    assert "**********" in rendered


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("node_hmac_secret", "short", "at least 32"),
        ("broker_account_id", "A-123", "ASCII letters or digits"),
        ("broker_account_id", "账户1234", "ASCII letters or digits"),
        ("operator_password_verifier", "not-a-verifier", "invalid format"),
    ],
)
def test_runtime_secret_value_rules(field: str, value: str, message: str) -> None:
    values = {
        "node_hmac_secret": SecretStr(HMAC_SECRET),
        "broker_account_id": SecretStr(BROKER_ACCOUNT),
        "operator_password_verifier": SecretStr(OPERATOR_VERIFIER),
    }
    values[field] = SecretStr(value)

    with pytest.raises(ValidationError, match=message):
        RuntimeSecrets.model_validate(values)


def test_secret_environment_name_guard_rejects_unscoped_names() -> None:
    with pytest.raises(ValueError, match="invalid secret environment"):
        assert_environment_secret_name("PATH")
