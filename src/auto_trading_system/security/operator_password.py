"""Scrypt verifier format for the local Windows operator credential."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable

_SCRYPT_N = 32_768
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_SALT_BYTES = 16
_VERIFIER_PATTERN = re.compile(
    r"ats-scrypt-v1\$32768\$8\$1\$(?P<salt>[A-Za-z0-9_-]{22})\$"
    r"(?P<digest>[A-Za-z0-9_-]{43})"
)


class OperatorPasswordHasher:
    """Create and verify a fixed, versioned password representation."""

    def __init__(self, *, salt_factory: Callable[[int], bytes] = secrets.token_bytes) -> None:
        if not callable(salt_factory):
            raise ValueError("salt_factory must be callable")
        self._salt_factory = salt_factory

    def hash_password(self, password: str) -> str:
        _validate_new_password(password)
        salt = self._salt_factory(_SALT_BYTES)
        if not isinstance(salt, bytes) or len(salt) != _SALT_BYTES:
            raise ValueError("salt factory returned an invalid value")
        digest = _derive(password, salt)
        return (
            f"ats-scrypt-v1${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_encode(salt)}${_encode(digest)}"
        )

    def verify(self, password: str, verifier: str) -> bool:
        salt, expected = parse_operator_password_verifier(verifier)
        try:
            raw = password.encode("utf-8")
        except UnicodeEncodeError:
            return False
        if len(raw) > 512 or "\x00" in password:
            return False
        candidate = bytearray(raw)
        try:
            actual = hashlib.scrypt(
                candidate,
                salt=salt,
                n=_SCRYPT_N,
                r=_SCRYPT_R,
                p=_SCRYPT_P,
                maxmem=_SCRYPT_MAXMEM,
                dklen=_SCRYPT_DKLEN,
            )
            return hmac.compare_digest(actual, expected)
        finally:
            candidate[:] = b"\x00" * len(candidate)


def validate_operator_password_verifier(verifier: str) -> str:
    parse_operator_password_verifier(verifier)
    return verifier


def parse_operator_password_verifier(verifier: str) -> tuple[bytes, bytes]:
    if not isinstance(verifier, str):
        raise ValueError("operator password verifier must be a string")
    match = _VERIFIER_PATTERN.fullmatch(verifier)
    if match is None:
        raise ValueError("operator password verifier has an invalid format")
    try:
        salt = _decode(match.group("salt"))
        digest = _decode(match.group("digest"))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("operator password verifier has invalid encoding") from exc
    if len(salt) != _SALT_BYTES or len(digest) != _SCRYPT_DKLEN:
        raise ValueError("operator password verifier has an invalid size")
    return salt, digest


def _validate_new_password(password: str) -> None:
    if not isinstance(password, str):
        raise ValueError("operator password must be a string")
    if not 14 <= len(password) <= 128:
        raise ValueError("operator password must contain 14-128 characters")
    if password != password.strip() or "\x00" in password or len(password.encode("utf-8")) > 512:
        raise ValueError("operator password contains unsupported whitespace or characters")


def _derive(password: str, salt: bytes) -> bytes:
    candidate = bytearray(password.encode("utf-8"))
    try:
        return hashlib.scrypt(
            candidate,
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            maxmem=_SCRYPT_MAXMEM,
            dklen=_SCRYPT_DKLEN,
        )
    finally:
        candidate[:] = b"\x00" * len(candidate)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
