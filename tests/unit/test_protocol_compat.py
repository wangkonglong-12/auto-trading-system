from __future__ import annotations

import pytest

from auto_trading_system.protocol.compat import (
    ProtocolVersion,
    ProtocolVersionError,
    require_supported_major,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1.0", ProtocolVersion(1, 0)), ("1.12", ProtocolVersion(1, 12))],
)
def test_parse_protocol_version(raw: str, expected: ProtocolVersion) -> None:
    assert ProtocolVersion.parse(raw) == expected


@pytest.mark.parametrize("raw", ["1", "v1.0", "1.0.0", "1.x", "-1.0", ""])
def test_reject_malformed_protocol_version(raw: str) -> None:
    with pytest.raises(ProtocolVersionError):
        ProtocolVersion.parse(raw)


def test_reject_unknown_major_version() -> None:
    with pytest.raises(ProtocolVersionError, match="unsupported schema major 2"):
        require_supported_major("2.0")


def test_accept_additive_minor_version() -> None:
    assert require_supported_major("1.8") == ProtocolVersion(1, 8)


def test_protocol_version_has_stable_string_form() -> None:
    assert str(ProtocolVersion(1, 8)) == "1.8"
