"""Prompt-only credential installation and rotation command."""

from __future__ import annotations

import argparse
import getpass
import hmac
import sys
from collections.abc import Callable, Sequence
from enum import StrEnum

from pydantic import ValidationError

from auto_trading_system.config.credentials import SecretLoadError
from auto_trading_system.config.models import SecretSource, WindowsCredentialReference
from auto_trading_system.config.windows_credentials import (
    WindowsCredentialManagerSecretProvider,
)
from auto_trading_system.security import OperatorPasswordHasher


class CredentialKind(StrEnum):
    NODE_HMAC = "node-hmac"
    IPC_HMAC = "ipc-hmac"
    BROKER_ACCOUNT = "broker-account"
    OPERATOR_PASSWORD = "operator-password"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto-trading-credential")
    parser.add_argument("--target", required=True, help="validated AutoTrading/... target name")
    parser.add_argument("--kind", required=True, choices=tuple(CredentialKind))
    parser.add_argument("--encoding", choices=("utf-16-le", "utf-8"), default="utf-16-le")
    return parser


def _validate_value(kind: CredentialKind, value: str) -> None:
    if kind in (CredentialKind.NODE_HMAC, CredentialKind.IPC_HMAC):
        if len(value.encode()) < 32:
            raise ValueError("node HMAC credential must contain at least 32 UTF-8 bytes")
        return
    if kind is CredentialKind.BROKER_ACCOUNT and (
        not 4 <= len(value) <= 64 or not value.isascii() or not value.isalnum()
    ):
        raise ValueError("broker account credential must be 4-64 ASCII letters or digits")


def run(
    argv: Sequence[str] | None = None,
    *,
    secret_reader: Callable[[str], str] = getpass.getpass,
    provider: WindowsCredentialManagerSecretProvider | None = None,
    password_hasher: OperatorPasswordHasher | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        reference = WindowsCredentialReference(
            source=SecretSource.WINDOWS_CREDENTIAL_MANAGER,
            target=arguments.target,
            encoding=arguments.encoding,
        )
    except ValidationError:
        print("credential target is invalid", file=sys.stderr)
        return 2

    if secret_reader is getpass.getpass and not sys.stdin.isatty():
        print("credential not stored: an interactive terminal is required", file=sys.stderr)
        return 2
    try:
        first = secret_reader("Credential: ")
        second = secret_reader("Confirm credential: ")
    except (EOFError, KeyboardInterrupt):
        print("credential not stored: input was cancelled", file=sys.stderr)
        return 2
    try:
        kind = CredentialKind(arguments.kind)
        _validate_value(kind, first)
        if not hmac.compare_digest(first, second):
            raise ValueError("credential confirmation does not match")
        stored_value = (
            (password_hasher or OperatorPasswordHasher()).hash_password(first)
            if kind is CredentialKind.OPERATOR_PASSWORD
            else first
        )
        effective_provider = (
            WindowsCredentialManagerSecretProvider() if provider is None else provider
        )
        effective_provider.write(reference, stored_value)
    except (SecretLoadError, UnicodeEncodeError, ValueError) as exc:
        detail = exc.code if isinstance(exc, SecretLoadError) else str(exc)
        print(f"credential not stored: {detail}", file=sys.stderr)
        return 2
    print("credential stored")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
