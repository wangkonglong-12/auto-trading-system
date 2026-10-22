"""Versioned, fail-closed runtime configuration."""

from auto_trading_system.config.credentials import (
    RuntimeSecrets,
    SecretLoadError,
    validate_broker_account_id,
)
from auto_trading_system.config.loader import (
    ConfigurationError,
    LoadedConfiguration,
    load_runtime_configuration,
    load_runtime_settings,
    resolve_secret_reference,
)
from auto_trading_system.config.models import EnvironmentProfile, RuntimeConfig
from auto_trading_system.config.windows_credentials import (
    Win32CredentialManager,
    WindowsCredentialManagerSecretProvider,
)

__all__ = [
    "ConfigurationError",
    "EnvironmentProfile",
    "LoadedConfiguration",
    "RuntimeConfig",
    "RuntimeSecrets",
    "SecretLoadError",
    "Win32CredentialManager",
    "WindowsCredentialManagerSecretProvider",
    "load_runtime_configuration",
    "load_runtime_settings",
    "resolve_secret_reference",
    "validate_broker_account_id",
]
