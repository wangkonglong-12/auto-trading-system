from __future__ import annotations

import pytest

from auto_trading_system.domain import DomainValueError, Market, SecurityCode


@pytest.mark.parametrize(
    ("text", "digits", "market"),
    [
        ("600000.SH", "600000", Market.SHANGHAI),
        ("000001.SZ", "000001", Market.SHENZHEN),
        ("430001.BJ", "430001", Market.BEIJING),
    ],
)
def test_security_code_parses_canonical_markets(text: str, digits: str, market: Market) -> None:
    code = SecurityCode.parse(text)

    assert code.digits == digits
    assert code.market is market
    assert code.to_wire() == text
    assert str(code) == text


@pytest.mark.parametrize(
    "text",
    ["", "000001", "00001.SZ", "0000001.SZ", "000001.sz", "000001.HK", " 000001.SZ"],
)
def test_security_code_rejects_non_canonical_wire_values(text: str) -> None:
    with pytest.raises(DomainValueError, match="INVALID_SECURITY_CODE"):
        SecurityCode.parse(text)


def test_security_code_rejects_non_string_wire_value() -> None:
    with pytest.raises(DomainValueError, match="INVALID_SECURITY_CODE"):
        SecurityCode.parse(1)  # type: ignore[arg-type]


def test_security_code_constructor_validates_both_parts() -> None:
    with pytest.raises(DomainValueError, match="INVALID_SECURITY_CODE"):
        SecurityCode("12345", Market.SHANGHAI)
    with pytest.raises(DomainValueError, match="INVALID_SECURITY_MARKET"):
        SecurityCode("600000", "SH")  # type: ignore[arg-type]
