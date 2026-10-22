"""Fail-closed loading, secret resolution and safe configuration summaries."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError

from auto_trading_system.config.credentials import (
    EnvironmentSecretProvider,
    RuntimeSecrets,
    SecretLoadError,
    SecretProvider,
)
from auto_trading_system.config.models import RuntimeConfig, SecretReference, SecretSource
from auto_trading_system.config.windows_credentials import build_default_secret_providers
from auto_trading_system.protocol.canonical_json import canonical_json_bytes

MAX_CONFIG_BYTES = 1_048_576


class ConfigurationError(RuntimeError):
    """Public configuration failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class LoadedConfiguration:
    """Validated machine config, resolved secrets and deterministic config hash."""

    settings: RuntimeConfig
    secrets: RuntimeSecrets
    config_hash: str

    def safe_summary(self) -> dict[str, Any]:
        """Return diagnostic fields that cannot reveal credentials or broker account data."""
        return {
            "config_version": self.settings.config_version,
            "config_hash": self.config_hash,
            "profile": self.settings.profile.value,
            "node_id": self.settings.node.node_id,
            "account_id": self.settings.node.account_id,
            "account_alias": self.settings.node.account_alias,
            "node_key_id": self.settings.security.node_key_id,
            "secret_sources": {
                "node_hmac_secret": self.settings.security.node_hmac_secret.source.value,
                "broker_account_id": self.settings.security.broker_account_id.source.value,
                "operator_password_verifier": (
                    self.settings.security.operator_password_verifier.source.value
                ),
            },
            "local_api": {
                "host": self.settings.local_api.host,
                "port": self.settings.local_api.port,
            },
        }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(
                "CONFIG_DUPLICATE_KEY",
                "configuration contains duplicate keys",
            )
        result[key] = value
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError("CONFIG_READ_FAILED", "configuration file cannot be read") from exc
    if not raw_bytes:
        raise ConfigurationError("CONFIG_EMPTY", "configuration file is empty")
    if len(raw_bytes) > MAX_CONFIG_BYTES:
        raise ConfigurationError("CONFIG_TOO_LARGE", "configuration file exceeds 1 MiB")
    try:
        value = json.loads(raw_bytes.decode("utf-8-sig"), object_pairs_hook=_reject_duplicate_keys)
    except ConfigurationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            "CONFIG_JSON_INVALID",
            "configuration is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(value, dict):
        raise ConfigurationError("CONFIG_ROOT_INVALID", "configuration root must be an object")
    return value


def _validation_message(exc: ValidationError) -> str:
    errors = exc.errors(include_input=False, include_url=False)
    descriptions = [
        f"{'.'.join(str(component) for component in error['loc'])}: {error['msg']}"
        for error in errors
    ]
    return "; ".join(descriptions)


def calculate_config_hash(settings: RuntimeConfig) -> str:
    """Hash the normalized, non-secret machine configuration."""
    payload = settings.model_dump(mode="json", exclude_none=True)
    digest = hashlib.sha256(canonical_json_bytes(payload, exclude=frozenset()))
    return f"sha256:{digest.hexdigest()}"


def _resolve_secret(
    reference: SecretReference,
    providers: Mapping[SecretSource, SecretProvider],
) -> SecretStr:
    provider = providers.get(reference.source)
    if provider is None:
        raise SecretLoadError(
            "SECRET_PROVIDER_UNAVAILABLE",
            "configured secret provider is unavailable in this installation",
        )
    return provider.read(reference)


def resolve_secret_reference(
    reference: SecretReference,
    *,
    environment: Mapping[str, str] | None = None,
    providers: Mapping[SecretSource, SecretProvider] | None = None,
) -> SecretStr:
    """Resolve one validated reference for a narrowly scoped composition root."""
    effective_providers: Mapping[SecretSource, SecretProvider]
    if providers is None:
        source_environment = os.environ if environment is None else environment
        environment_provider = EnvironmentSecretProvider(source_environment)
        effective_providers = build_default_secret_providers(environment_provider)
    else:
        effective_providers = providers
    try:
        return _resolve_secret(reference, effective_providers)
    except SecretLoadError as exc:
        raise ConfigurationError(
            "CONFIG_SECRET_LOAD_FAILED",
            f"{exc.code}: secret resolution failed",
        ) from exc


def load_runtime_configuration(
    path: Path,
    *,
    environment: Mapping[str, str] | None = None,
    providers: Mapping[SecretSource, SecretProvider] | None = None,
) -> LoadedConfiguration:
    """Load, validate and resolve a runtime configuration without logging inputs."""
    settings = load_runtime_settings(path)

    effective_providers: Mapping[SecretSource, SecretProvider]
    if providers is None:
        source_environment = os.environ if environment is None else environment
        environment_provider = EnvironmentSecretProvider(source_environment)
        effective_providers = build_default_secret_providers(environment_provider)
    else:
        effective_providers = providers

    try:
        secrets = RuntimeSecrets(
            node_hmac_secret=_resolve_secret(
                settings.security.node_hmac_secret,
                effective_providers,
            ),
            broker_account_id=_resolve_secret(
                settings.security.broker_account_id,
                effective_providers,
            ),
            operator_password_verifier=_resolve_secret(
                settings.security.operator_password_verifier,
                effective_providers,
            ),
        )
    except (SecretLoadError, ValidationError) as exc:
        message = (
            f"{exc.code}: secret resolution failed"
            if isinstance(exc, SecretLoadError)
            else _validation_message(exc)
        )
        raise ConfigurationError("CONFIG_SECRET_LOAD_FAILED", message) from exc

    return LoadedConfiguration(
        settings=settings,
        secrets=secrets,
        config_hash=calculate_config_hash(settings),
    )


def load_runtime_settings(path: Path) -> RuntimeConfig:
    """Load only non-secret settings for helpers that must not resolve credentials."""
    payload = _read_json_object(path)
    try:
        return RuntimeConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfigurationError("CONFIG_VALIDATION_FAILED", _validation_message(exc)) from exc
