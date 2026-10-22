"""Windows Credential Manager adapter with bounded, redaction-safe failures."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any, Protocol

from pydantic import SecretStr

from auto_trading_system.config.credentials import SecretLoadError, SecretProvider
from auto_trading_system.config.models import (
    SecretReference,
    SecretSource,
    WindowsCredentialReference,
)

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_MAX_CREDENTIAL_BLOB_BYTES = 2560


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class CredentialBlobStore(Protocol):
    """Minimal binary boundary around the operating-system credential vault."""

    def read_blob(self, target: str) -> bytearray:
        """Read an owned mutable copy of the credential blob."""

    def write_blob(self, target: str, blob: bytearray) -> None:
        """Replace a generic credential without retaining the supplied blob."""


def erase_mutable_buffer(buffer: bytearray) -> None:
    """Best-effort overwrite of a temporary plaintext buffer."""
    if buffer:
        ctypes.memset((ctypes.c_ubyte * len(buffer)).from_buffer(buffer), 0, len(buffer))


class Win32CredentialManager:
    """Thin CredReadW/CredWriteW wrapper; Win32 messages never cross this boundary."""

    def __init__(self, library: Any | None = None) -> None:
        if os.name != "nt" and library is None:
            raise SecretLoadError(
                "WINDOWS_CREDENTIAL_MANAGER_UNAVAILABLE",
                "Windows Credential Manager is unavailable on this platform",
            )
        if library is None:
            library_loader = getattr(ctypes, "WinDLL", None)
            if library_loader is None:
                raise SecretLoadError(
                    "WINDOWS_CREDENTIAL_MANAGER_UNAVAILABLE",
                    "Windows Credential Manager is unavailable on this platform",
                )
            library = library_loader("Advapi32.dll", use_last_error=True)
        self._cred_read = library.CredReadW
        self._cred_read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CredentialW)),
        ]
        self._cred_read.restype = wintypes.BOOL
        self._cred_write = library.CredWriteW
        self._cred_write.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
        self._cred_write.restype = wintypes.BOOL
        self._cred_free = library.CredFree
        self._cred_free.argtypes = [ctypes.c_void_p]
        self._cred_free.restype = None

    def __repr__(self) -> str:
        return "Win32CredentialManager(<protected>)"

    def read_blob(self, target: str) -> bytearray:
        credential_pointer = ctypes.POINTER(_CredentialW)()
        if not self._cred_read(
            target,
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(credential_pointer),
        ):
            error_code = ctypes.get_last_error()
            if error_code == _ERROR_NOT_FOUND:
                raise SecretLoadError("SECRET_NOT_FOUND", "required Windows credential is missing")
            raise SecretLoadError(
                "WINDOWS_CREDENTIAL_READ_FAILED",
                "Windows Credential Manager could not read the credential",
            )

        try:
            credential = credential_pointer.contents
            blob_size = int(credential.CredentialBlobSize)
            if blob_size <= 0:
                raise SecretLoadError("SECRET_NOT_FOUND", "required Windows credential is empty")
            if blob_size > _MAX_CREDENTIAL_BLOB_BYTES:
                raise SecretLoadError(
                    "SECRET_TOO_LARGE",
                    "Windows credential exceeds the supported size",
                )
            if not credential.CredentialBlob:
                raise SecretLoadError(
                    "WINDOWS_CREDENTIAL_READ_FAILED",
                    "Windows Credential Manager returned an invalid credential",
                )
            result = bytearray(blob_size)
            destination = (ctypes.c_ubyte * blob_size).from_buffer(result)
            ctypes.memmove(destination, credential.CredentialBlob, blob_size)
            return result
        finally:
            self._cred_free(ctypes.cast(credential_pointer, ctypes.c_void_p))

    def write_blob(self, target: str, blob: bytearray) -> None:
        if not 0 < len(blob) <= _MAX_CREDENTIAL_BLOB_BYTES:
            raise SecretLoadError(
                "SECRET_SIZE_INVALID",
                "credential size is outside the supported range",
            )
        blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer(blob)
        credential = _CredentialW(
            Flags=0,
            Type=_CRED_TYPE_GENERIC,
            TargetName=target,
            Comment=None,
            CredentialBlobSize=len(blob),
            CredentialBlob=ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte)),
            Persist=_CRED_PERSIST_LOCAL_MACHINE,
            AttributeCount=0,
            Attributes=None,
            TargetAlias=None,
            UserName=None,
        )
        if not self._cred_write(ctypes.byref(credential), 0):
            raise SecretLoadError(
                "WINDOWS_CREDENTIAL_WRITE_FAILED",
                "Windows Credential Manager could not store the credential",
            )


class WindowsCredentialManagerSecretProvider:
    """Decode generic credentials referenced by validated machine configuration."""

    source = SecretSource.WINDOWS_CREDENTIAL_MANAGER

    def __init__(self, store: CredentialBlobStore | None = None) -> None:
        self._store = store

    def __repr__(self) -> str:
        return "WindowsCredentialManagerSecretProvider(<protected>)"

    def _effective_store(self) -> CredentialBlobStore:
        if self._store is None:
            self._store = Win32CredentialManager()
        return self._store

    def read(self, reference: SecretReference) -> SecretStr:
        if not isinstance(reference, WindowsCredentialReference):
            raise SecretLoadError("SECRET_REFERENCE_MISMATCH", "secret reference type is invalid")
        blob = self._effective_store().read_blob(reference.target)
        try:
            try:
                raw_secret = blob.decode(reference.encoding, errors="strict")
            except UnicodeDecodeError as exc:
                raise SecretLoadError(
                    "SECRET_ENCODING_INVALID",
                    "Windows credential uses an invalid encoding",
                ) from exc
            if not raw_secret.strip():
                raise SecretLoadError("SECRET_NOT_FOUND", "required Windows credential is empty")
            if "\x00" in raw_secret:
                raise SecretLoadError(
                    "SECRET_VALUE_INVALID",
                    "Windows credential contains an unsupported character",
                )
            return SecretStr(raw_secret)
        finally:
            erase_mutable_buffer(blob)

    def write(self, reference: WindowsCredentialReference, secret: str) -> None:
        """Atomically replace one credential while zeroing the encoded temporary buffer."""
        if not isinstance(reference, WindowsCredentialReference):
            raise SecretLoadError("SECRET_REFERENCE_MISMATCH", "secret reference type is invalid")
        if not isinstance(secret, str) or not secret.strip() or "\x00" in secret:
            raise SecretLoadError("SECRET_VALUE_INVALID", "credential value is invalid")
        encoded = bytearray(secret.encode(reference.encoding, errors="strict"))
        try:
            self._effective_store().write_blob(reference.target, encoded)
        finally:
            erase_mutable_buffer(encoded)


def build_default_secret_providers(
    environment_provider: SecretProvider,
) -> dict[SecretSource, SecretProvider]:
    """Build the production provider registry without reading any secret eagerly."""
    windows_provider = WindowsCredentialManagerSecretProvider()
    return {
        environment_provider.source: environment_provider,
        windows_provider.source: windows_provider,
    }
