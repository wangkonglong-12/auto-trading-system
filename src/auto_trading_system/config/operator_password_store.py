"""Persist password verifiers in the Windows vault, never plaintext files."""

from __future__ import annotations

import hashlib

from pydantic import SecretStr

from auto_trading_system.config.credentials import SecretLoadError
from auto_trading_system.config.models import SecretReference, WindowsCredentialReference
from auto_trading_system.config.node_v2 import resolve_secret
from auto_trading_system.config.windows_credentials import WindowsCredentialManagerSecretProvider
from auto_trading_system.security.operator_password import validate_operator_password_verifier


class OperatorPasswordStore:
    def __init__(self, reference: SecretReference) -> None:
        self.original = reference
        # Environment-backed desktop profiles also need a persistent vault entry.
        identity = hashlib.sha256(reference.model_dump_json().encode()).hexdigest()
        self.reference = (
            reference
            if isinstance(reference, WindowsCredentialReference)
            else WindowsCredentialReference(
                source="WINDOWS_CREDENTIAL_MANAGER",
                target=f"AutoTrading/OperatorPassword/{identity}",
            )
        )
        self.vault = WindowsCredentialManagerSecretProvider()

    def read(self) -> SecretStr:
        try:
            value = self.vault.read(self.reference)
        except SecretLoadError as exc:
            if exc.code != "SECRET_NOT_FOUND" or self.original == self.reference:
                raise
            value = SecretStr(resolve_secret(self.original).decode("utf-8"))
        validate_operator_password_verifier(value.get_secret_value())
        return value

    def write(self, verifier: str) -> None:
        validate_operator_password_verifier(verifier)
        self.vault.write(self.reference, verifier)
