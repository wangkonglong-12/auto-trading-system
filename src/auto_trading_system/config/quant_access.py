"""One vault-backed API token shared by preview, live feed and event reporting."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from auto_trading_system.config.credentials import SecretLoadError
from auto_trading_system.config.models import WindowsCredentialReference
from auto_trading_system.config.windows_credentials import WindowsCredentialManagerSecretProvider


def token_reference(root: Path) -> WindowsCredentialReference:
    identity = hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()
    return WindowsCredentialReference(
        source="WINDOWS_CREDENTIAL_MANAGER", target=f"AutoTrading/QuantAccess/{identity}"
    )


def read_access_token(root: Path) -> bytes | None:
    if os.name != "nt":
        return None
    try:
        value = (
            WindowsCredentialManagerSecretProvider().read(token_reference(root)).get_secret_value()
        )
    except SecretLoadError as error:
        if error.code == "SECRET_NOT_FOUND":
            return None
        raise
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,256}", value):
        raise SecretLoadError("QUANT_ACCESS_INVALID", "invalid API token")
    return value.encode("ascii")


def save_access_token(root: Path, value: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,256}", value):
        raise ValueError("INVALID_ARGUMENTS")
    WindowsCredentialManagerSecretProvider().write(token_reference(root), value)
