from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

import pytest

from auto_trading_system.config.credentials import SecretLoadError
from auto_trading_system.config.models import (
    EnvironmentSecretReference,
    SecretSource,
    WindowsCredentialReference,
)
from auto_trading_system.config.windows_credentials import (
    Win32CredentialManager,
    WindowsCredentialManagerSecretProvider,
    _CredentialW,
)


@dataclass
class MemoryCredentialStore:
    read_buffer: bytearray = field(default_factory=bytearray)
    written_target: str | None = None
    written_value: bytes | None = None
    write_buffer: bytearray | None = None

    def read_blob(self, target: str) -> bytearray:
        del target
        return self.read_buffer

    def write_blob(self, target: str, blob: bytearray) -> None:
        self.written_target = target
        self.written_value = bytes(blob)
        self.write_buffer = blob


class FakeFunction:
    def __init__(self, callback: Any) -> None:
        self._callback = callback
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *arguments: object) -> object:
        return self._callback(*arguments)


class FakeCredentialLibrary:
    def __init__(
        self,
        blob: bytes = b"vault-value",
        *,
        read_success: bool = True,
        write_success: bool = True,
        read_error: int = 5,
    ) -> None:
        self._blob = (ctypes.c_ubyte * len(blob))(*blob)
        self._credential = _CredentialW(
            CredentialBlobSize=len(blob),
            CredentialBlob=ctypes.cast(self._blob, ctypes.POINTER(ctypes.c_ubyte)),
        )
        self.read_success = read_success
        self.write_success = write_success
        self.read_error = read_error
        self.free_count = 0
        self.written: bytes | None = None
        self.CredReadW = FakeFunction(self._read)
        self.CredWriteW = FakeFunction(self._write)
        self.CredFree = FakeFunction(self._free)

    def _read(self, *arguments: Any) -> int:
        if not self.read_success:
            ctypes.set_last_error(self.read_error)
            return 0
        output = ctypes.cast(
            arguments[3],
            ctypes.POINTER(ctypes.POINTER(_CredentialW)),
        )
        output[0] = ctypes.pointer(self._credential)
        return 1

    def _write(self, *arguments: Any) -> int:
        credential = ctypes.cast(arguments[0], ctypes.POINTER(_CredentialW)).contents
        self.written = ctypes.string_at(
            credential.CredentialBlob,
            credential.CredentialBlobSize,
        )
        return int(self.write_success)

    def _free(self, *arguments: Any) -> None:
        del arguments
        self.free_count += 1


def reference(
    *, encoding: Literal["utf-16-le", "utf-8"] = "utf-16-le"
) -> WindowsCredentialReference:
    return WindowsCredentialReference(
        source=SecretSource.WINDOWS_CREDENTIAL_MANAGER,
        target="AutoTrading/node-dev-01/test-secret",
        encoding=encoding,
    )


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-8"])
def test_provider_decodes_and_zeroes_owned_blob(
    encoding: Literal["utf-16-le", "utf-8"],
) -> None:
    raw = "credential-value-0123456789"
    store = MemoryCredentialStore(bytearray(raw.encode(encoding)))
    provider = WindowsCredentialManagerSecretProvider(store)

    value = provider.read(reference(encoding=encoding))

    assert value.get_secret_value() == raw
    assert store.read_buffer == bytearray(len(store.read_buffer))
    assert raw not in repr(provider)


def test_provider_zeroes_blob_when_decoding_fails() -> None:
    store = MemoryCredentialStore(bytearray(b"\xff"))
    provider = WindowsCredentialManagerSecretProvider(store)

    with pytest.raises(SecretLoadError) as exc_info:
        provider.read(reference())

    assert exc_info.value.code == "SECRET_ENCODING_INVALID"
    assert store.read_buffer == bytearray(b"\x00")


@pytest.mark.parametrize("raw", ["", " \t", "abc\x00def"])
def test_provider_rejects_blank_or_embedded_null_values(raw: str) -> None:
    store = MemoryCredentialStore(bytearray(raw.encode("utf-8")))
    provider = WindowsCredentialManagerSecretProvider(store)

    with pytest.raises(SecretLoadError):
        provider.read(reference(encoding="utf-8"))

    assert not any(store.read_buffer)


def test_provider_rejects_reference_from_another_source_without_vault_access() -> None:
    store = MemoryCredentialStore(bytearray(b"not-read"))
    provider = WindowsCredentialManagerSecretProvider(store)
    wrong_reference = EnvironmentSecretReference(
        source=SecretSource.ENVIRONMENT,
        variable="ATS_SECRET_TEST_VALUE",
    )

    with pytest.raises(SecretLoadError) as exc_info:
        provider.read(wrong_reference)

    assert exc_info.value.code == "SECRET_REFERENCE_MISMATCH"
    assert store.read_buffer == bytearray(b"not-read")


def test_provider_write_passes_encoded_value_then_zeroes_temporary_buffer() -> None:
    store = MemoryCredentialStore()
    provider = WindowsCredentialManagerSecretProvider(store)

    provider.write(reference(encoding="utf-8"), "new-credential-value")

    assert store.written_target == "AutoTrading/node-dev-01/test-secret"
    assert store.written_value == b"new-credential-value"
    assert store.write_buffer is not None
    assert not any(store.write_buffer)


def test_win32_adapter_copies_and_frees_credential_blob() -> None:
    library = FakeCredentialLibrary(b"protected-value")
    manager = Win32CredentialManager(library)

    result = manager.read_blob("AutoTrading/node/secret")

    assert result == bytearray(b"protected-value")
    assert library.free_count == 1
    assert "protected-value" not in repr(manager)


@pytest.mark.parametrize(
    ("error_code", "expected_code"),
    [(1168, "SECRET_NOT_FOUND"), (5, "WINDOWS_CREDENTIAL_READ_FAILED")],
)
def test_win32_adapter_translates_read_errors_without_target(
    error_code: int,
    expected_code: str,
) -> None:
    library = FakeCredentialLibrary(read_success=False, read_error=error_code)
    manager = Win32CredentialManager(library)

    with pytest.raises(SecretLoadError) as exc_info:
        manager.read_blob("AutoTrading/node/confidential-target")

    assert exc_info.value.code == expected_code
    assert "confidential-target" not in str(exc_info.value)


def test_win32_adapter_writes_blob_and_translates_write_failure() -> None:
    success_library = FakeCredentialLibrary()
    Win32CredentialManager(success_library).write_blob("AutoTrading/node/secret", bytearray(b"new"))
    assert success_library.written == b"new"

    failed_library = FakeCredentialLibrary(write_success=False)
    with pytest.raises(SecretLoadError) as exc_info:
        Win32CredentialManager(failed_library).write_blob(
            "AutoTrading/node/secret", bytearray(b"new")
        )
    assert exc_info.value.code == "WINDOWS_CREDENTIAL_WRITE_FAILED"


@pytest.mark.parametrize("blob", [bytearray(), bytearray(2561)])
def test_win32_adapter_rejects_invalid_write_sizes(blob: bytearray) -> None:
    with pytest.raises(SecretLoadError, match="SECRET_SIZE_INVALID"):
        Win32CredentialManager(FakeCredentialLibrary()).write_blob("AutoTrading/node/secret", blob)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Credential Manager")
def test_real_windows_provider_reports_missing_credential_without_target() -> None:
    target = f"AutoTrading/codex-test/missing-{uuid4().hex}"
    provider = WindowsCredentialManagerSecretProvider()

    with pytest.raises(SecretLoadError) as exc_info:
        provider.read(
            WindowsCredentialReference(
                source=SecretSource.WINDOWS_CREDENTIAL_MANAGER,
                target=target,
            )
        )

    assert exc_info.value.code == "SECRET_NOT_FOUND"
    assert target not in str(exc_info.value)
