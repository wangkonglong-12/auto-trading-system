"""Secret-provider boundary and redaction-safe runtime secret values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from auto_trading_system.config.models import (
    EnvironmentSecretReference,
    SecretReference,
    SecretSource,
    assert_environment_secret_name,
)
from auto_trading_system.security.operator_password import validate_operator_password_verifier


class SecretLoadError(RuntimeError):
    """Raised without including a secret value or provider-specific target."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class SecretProvider(Protocol):
    """Resolve a secret reference without exposing it to configuration code."""

    source: SecretSource

    def read(self, reference: SecretReference) -> SecretStr:
        """Return a redacting secret value or raise SecretLoadError."""


class EnvironmentSecretProvider:
    """Environment-backed provider restricted to explicitly named secret variables."""

    source = SecretSource.ENVIRONMENT

    def __init__(self, environment: Mapping[str, str]) -> None:
        self._environment = environment

    def read(self, reference: SecretReference) -> SecretStr:
        if not isinstance(reference, EnvironmentSecretReference):
            raise SecretLoadError("SECRET_REFERENCE_MISMATCH", "secret reference type is invalid")
        assert_environment_secret_name(reference.variable)
        raw_secret = self._environment.get(reference.variable)
        if raw_secret is None or not raw_secret.strip():
            raise SecretLoadError("SECRET_NOT_FOUND", "required environment secret is missing")
        return SecretStr(raw_secret)


class RuntimeSecrets(BaseModel):
    """Resolved secrets; repr and JSON serialization remain redacted."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    node_hmac_secret: SecretStr
    broker_account_id: SecretStr
    operator_password_verifier: SecretStr

    @field_validator("node_hmac_secret")
    @classmethod
    def validate_hmac_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("node HMAC secret must contain at least 32 UTF-8 bytes")
        return value

    @field_validator("broker_account_id")
    @classmethod
    def validate_broker_account(cls, value: SecretStr) -> SecretStr:
        validate_broker_account_id(value.get_secret_value())
        return value

    @field_validator("operator_password_verifier")
    @classmethod
    def validate_operator_verifier(cls, value: SecretStr) -> SecretStr:
        validate_operator_password_verifier(value.get_secret_value())
        return value


def validate_broker_account_id(value: str) -> str:
    """Validate a resolved broker account without including it in failures."""
    if not isinstance(value, str) or not 4 <= len(value) <= 64:
        raise ValueError("broker account id must be 4-64 ASCII letters or digits")
    if not value.isascii() or not value.isalnum():
        raise ValueError("broker account id must be 4-64 ASCII letters or digits")
    return value
