"""Fail-closed redaction for structured logs and rendered exception text."""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID

from pydantic import SecretStr

REDACTED: Final = "[REDACTED]"
_LIMIT_REDACTION: Final = "[REDACTED:LIMIT]"
_CYCLE_REDACTION: Final = "[REDACTED:CYCLE]"
_MAX_DEPTH = 12
_MAX_NODES = 10_000
_MAX_TEXT_CHARS = 65_536

_SENSITIVE_KEY_EXACT = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "xatssignature",
        "signature",
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "accesstoken",
        "refreshtoken",
        "apikey",
        "credential",
        "credentialblob",
        "brokeraccountid",
        "accountnumber",
        "nodehmacsecret",
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    "password",
    "passwd",
    "secret",
    "token",
    "signature",
    "credential",
    "cookie",
)
_AUTH_PATTERN = re.compile(r"(?i)\b(Bearer|Basic)(\s+)[^\s,;]+")
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(\b(?:authorization|proxy[-_]?authorization|x[-_]?ats[-_]?signature|"
    r"signature|(?:[a-z0-9]+[-_])?token|api[-_]?key|password|passwd|pwd|"
    r"(?:[a-z0-9]+[-_])?secret|"
    r"credential(?:[-_]?blob)?|broker[-_]?account[-_]?id|account[-_]?number|"
    r"node[-_]?hmac[-_]?secret|cookie|set[-_]?cookie)\b\s*(?:=|:)\s*)"
    r'(?:"[^"]*"|\'[^\']*\'|[^\s,;&}]+)'
)

_STANDARD_LOG_RECORD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def is_sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized in _SENSITIVE_KEY_EXACT or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


class SensitiveValueRedactor:
    """Sanitize nested values and exact runtime secrets without rendering unknown objects."""

    __slots__ = ("__secret_values",)

    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        values: set[str] = set()
        for value in secret_values:
            if not isinstance(value, str):
                raise TypeError("registered secret values must be strings")
            if value:
                if len(value) < 4:
                    raise ValueError(
                        "registered secret values must contain at least four characters"
                    )
                values.add(value)
        self.__secret_values = tuple(sorted(values, key=len, reverse=True))

    def __repr__(self) -> str:
        return "SensitiveValueRedactor(<protected>)"

    @classmethod
    def from_secret_values(cls, *values: SecretStr) -> SensitiveValueRedactor:
        return cls(value.get_secret_value() for value in values)

    def redact_text(self, value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("redaction input must be a string")
        redacted = self.redact_registered_values(value)
        redacted = _AUTH_PATTERN.sub(lambda match: f"{match.group(1)} {REDACTED}", redacted)
        redacted = _SENSITIVE_ASSIGNMENT_PATTERN.sub(
            lambda match: f"{match.group(1)}{REDACTED}", redacted
        )
        if len(redacted) > _MAX_TEXT_CHARS:
            return f"{redacted[:_MAX_TEXT_CHARS]}{_LIMIT_REDACTION}"
        return redacted

    def redact_registered_values(self, value: str) -> str:
        """Redact exact secrets while preserving logging format placeholders."""
        if not isinstance(value, str):
            raise TypeError("redaction input must be a string")
        redacted = value
        for secret in self.__secret_values:
            redacted = redacted.replace(secret, REDACTED)
        return redacted

    def contains_registered_secret(self, value: str | bytes) -> bool:
        if isinstance(value, str):
            return any(secret in value for secret in self.__secret_values)
        if isinstance(value, bytes):
            return any(
                secret.encode("utf-8") in value or secret.encode("utf-16-le") in value
                for secret in self.__secret_values
            )
        raise TypeError("secret scan input must be text or bytes")

    def sanitize(self, value: object) -> object:
        seen: set[int] = set()
        node_count = [0]
        return self._sanitize(value, depth=0, seen=seen, node_count=node_count)

    def _sanitize(
        self,
        value: object,
        *,
        depth: int,
        seen: set[int],
        node_count: list[int],
    ) -> object:
        node_count[0] += 1
        if depth > _MAX_DEPTH or node_count[0] > _MAX_NODES:
            return _LIMIT_REDACTION
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else _LIMIT_REDACTION
        if isinstance(value, SecretStr):
            return REDACTED
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return REDACTED
        if isinstance(value, Enum):
            return self._sanitize(value.value, depth=depth + 1, seen=seen, node_count=node_count)
        if isinstance(value, (Decimal, UUID, Path)):
            return self.redact_text(str(value))
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, BaseException):
            return self.redact_text(f"{type(value).__name__}: {value}")

        identity = id(value)
        if identity in seen:
            return _CYCLE_REDACTION
        if isinstance(value, Mapping):
            seen.add(identity)
            try:
                result: dict[str, object] = {}
                for key, item in value.items():
                    safe_key = self.redact_text(key) if isinstance(key, str) else "<redacted-key>"
                    result[safe_key] = (
                        REDACTED
                        if is_sensitive_key(safe_key)
                        else self._sanitize(
                            item,
                            depth=depth + 1,
                            seen=seen,
                            node_count=node_count,
                        )
                    )
                return result
            finally:
                seen.remove(identity)
        if isinstance(value, (list, tuple, set, frozenset)):
            seen.add(identity)
            try:
                return [
                    self._sanitize(
                        item,
                        depth=depth + 1,
                        seen=seen,
                        node_count=node_count,
                    )
                    for item in value
                ]
            finally:
                seen.remove(identity)
        return f"<{type(value).__name__}:redacted>"


class RedactingLogFilter(logging.Filter):
    """Protect message arguments and structured LogRecord extensions."""

    def __init__(self, redactor: SensitiveValueRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = (
            self._redactor.redact_registered_values(record.msg)
            if isinstance(record.msg, str)
            else self._redactor.sanitize(record.msg)
        )
        if isinstance(record.args, Mapping):
            record.args = cast(Any, self._redactor.sanitize(record.args))
        elif record.args:
            record.args = cast(
                Any,
                tuple(self._redactor.sanitize(item) for item in record.args),
            )
        for key in set(record.__dict__) - _STANDARD_LOG_RECORD_FIELDS:
            record.__dict__[key] = self._redactor.sanitize(record.__dict__[key])
        record.exc_text = None
        return True


class RedactingFormatter(logging.Formatter):
    """Final rendering boundary that also protects tracebacks and stack text."""

    def __init__(
        self,
        redactor: SensitiveValueRedactor,
        fmt: str | None = None,
        datefmt: str | None = None,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.redact_text(super().format(record))


def protect_log_handler(
    handler: logging.Handler,
    redactor: SensitiveValueRedactor,
    *,
    fmt: str = "%(asctime)s %(levelname)s %(name)s %(message)s",
) -> logging.Handler:
    """Install both structured and final-text protection on a handler."""
    handler.addFilter(RedactingLogFilter(redactor))
    handler.setFormatter(RedactingFormatter(redactor, fmt=fmt))
    return handler
