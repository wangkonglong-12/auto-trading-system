"""Stable validation error raised by domain value objects."""

from __future__ import annotations


class DomainValueError(ValueError):
    """Report a machine-readable code without leaking the rejected value."""

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(f"{code} at {field}: {message}")
        self.code = code
        self.field = field
