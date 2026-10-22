from __future__ import annotations

from typing import cast

import pytest

from auto_trading_system.security import (
    OperatorPasswordHasher,
    parse_operator_password_verifier,
    validate_operator_password_verifier,
)

PASSWORD = "Correct-Horse-Battery-2026"
VERIFIER = (
    "ats-scrypt-v1$32768$8$1$AAECAwQFBgcICQoLDA0ODw$f81LIxrvonq2ysChD17nLqfS0_FxQ1_5qy8BxpPy-n4"
)


def test_scrypt_verifier_is_versioned_deterministic_and_constant_time_checked() -> None:
    hasher = OperatorPasswordHasher(salt_factory=lambda size: bytes(range(size)))

    generated = hasher.hash_password(PASSWORD)

    assert generated == VERIFIER
    assert hasher.verify(PASSWORD, generated) is True
    assert hasher.verify("Wrong-Password-Value-2026", generated) is False
    assert validate_operator_password_verifier(generated) == generated
    salt, digest = parse_operator_password_verifier(generated)
    assert salt == bytes(range(16))
    assert len(digest) == 32


@pytest.mark.parametrize(
    "password",
    [
        "too-short",
        " leading-space-password",
        "trailing-space-password ",
        "embedded\x00password-value",
        "x" * 129,
    ],
)
def test_new_operator_password_policy_rejects_unsupported_values(password: str) -> None:
    hasher = OperatorPasswordHasher(salt_factory=lambda _: b"s" * 16)

    with pytest.raises(ValueError, match="operator password"):
        hasher.hash_password(password)


@pytest.mark.parametrize(
    "verifier",
    [
        None,
        "not-a-verifier",
        VERIFIER.replace("v1", "v2"),
        VERIFIER.replace("32768", "16384"),
        VERIFIER[:-1] + "=",
    ],
)
def test_verifier_parser_rejects_wrong_type_version_parameters_and_encoding(
    verifier: object,
) -> None:
    with pytest.raises(ValueError, match="verifier"):
        parse_operator_password_verifier(cast(str, verifier))


def test_hasher_rejects_invalid_salt_factory_and_unencodable_login_value() -> None:
    with pytest.raises(ValueError, match="salt_factory"):
        OperatorPasswordHasher(salt_factory=cast(object, None))  # type: ignore[arg-type]

    hasher = OperatorPasswordHasher(salt_factory=lambda _: b"short")
    with pytest.raises(ValueError, match="salt factory"):
        hasher.hash_password(PASSWORD)

    assert OperatorPasswordHasher().verify("bad-surrogate-\ud800", VERIFIER) is False
