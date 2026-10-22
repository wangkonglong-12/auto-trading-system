"""Protocol version parsing and compatibility checks."""

from __future__ import annotations

from dataclasses import dataclass


class ProtocolVersionError(ValueError):
    """Raised when a schema version is malformed or unsupported."""


@dataclass(frozen=True, slots=True, order=True)
class ProtocolVersion:
    """A numeric major.minor protocol version."""

    major: int
    minor: int

    @classmethod
    def parse(cls, value: str) -> ProtocolVersion:
        """Parse exactly two non-negative numeric version components."""
        components = value.split(".")
        if len(components) != 2 or any(not component.isdigit() for component in components):
            raise ProtocolVersionError(f"invalid schema_version: {value!r}")
        return cls(major=int(components[0]), minor=int(components[1]))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


def require_supported_major(value: str, *, supported_major: int = 1) -> ProtocolVersion:
    """Return a parsed version when its major component is supported."""
    version = ProtocolVersion.parse(value)
    if version.major != supported_major:
        raise ProtocolVersionError(
            f"unsupported schema major {version.major}; supported major is {supported_major}"
        )
    return version
