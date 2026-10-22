from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from auto_trading_system.apps.credential_admin.__main__ import run
from auto_trading_system.config.windows_credentials import (
    WindowsCredentialManagerSecretProvider,
)


@dataclass
class RecordingStore:
    writes: list[tuple[str, bytes]] = field(default_factory=list)

    def read_blob(self, target: str) -> bytearray:
        del target
        raise AssertionError("read is not expected")

    def write_blob(self, target: str, blob: bytearray) -> None:
        self.writes.append((target, bytes(blob)))


def reader(*values: str) -> Callable[[str], str]:
    remaining = iter(values)
    return lambda _: next(remaining)


def test_admin_stores_confirmed_hmac_without_printing_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "0123456789abcdef0123456789abcdef"
    store = RecordingStore()

    result = run(
        [
            "--target",
            "AutoTrading/node-dev-01/key-001",
            "--kind",
            "node-hmac",
            "--encoding",
            "utf-8",
        ],
        secret_reader=reader(secret, secret),
        provider=WindowsCredentialManagerSecretProvider(store),
    )

    assert result == 0
    assert store.writes == [("AutoTrading/node-dev-01/key-001", secret.encode())]
    captured = capsys.readouterr()
    assert captured.out.strip() == "credential stored"
    assert secret not in captured.out + captured.err


def test_admin_rejects_mismatch_without_writing_or_leaking_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = "0123456789abcdef0123456789abcdef"
    second = "fedcba9876543210fedcba9876543210"
    store = RecordingStore()

    result = run(
        ["--target", "AutoTrading/node/key", "--kind", "node-hmac"],
        secret_reader=reader(first, second),
        provider=WindowsCredentialManagerSecretProvider(store),
    )

    assert result == 2
    assert store.writes == []
    captured = capsys.readouterr()
    assert first not in captured.err
    assert second not in captured.err


def test_admin_validates_broker_account_shape_before_vault_write(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = RecordingStore()

    result = run(
        ["--target", "AutoTrading/node/account", "--kind", "broker-account"],
        secret_reader=reader("A-123", "A-123"),
        provider=WindowsCredentialManagerSecretProvider(store),
    )

    assert result == 2
    assert store.writes == []
    assert "A-123" not in capsys.readouterr().err


def test_admin_hashes_operator_password_before_vault_write(
    capsys: pytest.CaptureFixture[str],
) -> None:
    password = "Correct-Horse-Battery-2026"
    store = RecordingStore()

    result = run(
        [
            "--target",
            "AutoTrading/node/operator-password",
            "--kind",
            "operator-password",
        ],
        secret_reader=reader(password, password),
        provider=WindowsCredentialManagerSecretProvider(store),
    )

    assert result == 0
    assert len(store.writes) == 1
    target, stored = store.writes[0]
    verifier = stored.decode("utf-16-le")
    assert target == "AutoTrading/node/operator-password"
    assert verifier.startswith("ats-scrypt-v1$32768$8$1$")
    assert password not in verifier
    assert password not in capsys.readouterr().out
