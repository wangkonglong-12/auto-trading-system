from __future__ import annotations

import io
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import SecretStr

from auto_trading_system.monitoring.redaction import (
    REDACTED,
    RedactingFormatter,
    SensitiveValueRedactor,
    is_sensitive_key,
    protect_log_handler,
)

SECRET = "0123456789abcdef0123456789abcdef"
ACCOUNT = "A123456789"


class LeakingObject:
    def __repr__(self) -> str:
        return f"LeakingObject(secret={SECRET})"


def redactor() -> SensitiveValueRedactor:
    return SensitiveValueRedactor.from_secret_values(SecretStr(SECRET), SecretStr(ACCOUNT))


def test_nested_sanitization_redacts_keys_values_bytes_and_unknown_objects() -> None:
    source = {
        "authorization": "Bearer arbitrary-token",
        "broker_account_id": ACCOUNT,
        "nested": [SECRET, {"password": "unknown-password"}, b"raw-body"],
        "object": LeakingObject(),
        "logical_account_id": "acct_alpha",
    }

    sanitized = redactor().sanitize(source)

    assert sanitized == {
        "authorization": REDACTED,
        "broker_account_id": REDACTED,
        "nested": [REDACTED, {"password": REDACTED}, REDACTED],
        "object": "<LeakingObject:redacted>",
        "logical_account_id": "acct_alpha",
    }
    assert source["broker_account_id"] == ACCOUNT


def test_text_redaction_covers_registered_values_headers_and_query_parameters() -> None:
    source = (
        f"account={ACCOUNT} secret={SECRET} Authorization: Bearer dynamic.jwt.value "
        "url=https://example.invalid/path?access_token=other-token&safe=1 "
        "X-ATS-Signature=abc123"
    )

    rendered = redactor().redact_text(source)

    assert SECRET not in rendered
    assert ACCOUNT not in rendered
    assert "dynamic.jwt.value" not in rendered
    assert "other-token" not in rendered
    assert "abc123" not in rendered
    assert rendered.count(REDACTED) >= 5


def test_text_redaction_removes_quoted_secrets_with_spaces() -> None:
    rendered = redactor().redact_text(
        "password='two secret words' client_secret=dynamic-value safe=value"
    )

    assert "two secret words" not in rendered
    assert "dynamic-value" not in rendered
    assert "safe=value" in rendered


def test_sensitive_key_detection_does_not_hide_logical_identifiers() -> None:
    assert is_sensitive_key("node_hmac_secret")
    assert is_sensitive_key("X-ATS-Signature")
    assert is_sensitive_key("refreshToken")
    assert is_sensitive_key("broker_account_id")
    assert not is_sensitive_key("account_id")
    assert not is_sensitive_key("node_key_id")
    assert not is_sensitive_key("secret_sources")


def test_redactor_handles_cycles_and_never_exposes_secrets_in_repr() -> None:
    cyclic: list[object] = [SECRET]
    cyclic.append(cyclic)
    sanitizer = redactor()

    result = sanitizer.sanitize(cyclic)

    assert result == [REDACTED, "[REDACTED:CYCLE]"]
    assert SECRET not in repr(sanitizer)
    assert sanitizer.contains_registered_secret(SECRET.encode())
    assert sanitizer.contains_registered_secret(ACCOUNT.encode("utf-16-le"))


def test_protected_handler_redacts_arguments_extras_and_exception_traceback() -> None:
    stream = io.StringIO()
    handler = protect_log_handler(
        logging.StreamHandler(stream),
        redactor(),
        fmt="%(levelname)s %(message)s %(context)s",
    )
    logger = logging.getLogger("tests.secure-logging")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    try:
        raise RuntimeError(f"broker {ACCOUNT} rejected secret {SECRET}")
    except RuntimeError:
        logger.exception(
            "request Authorization=Bearer %s",
            "runtime-token",
            extra={"context": {"password": "another-secret", "account_id": "acct_1"}},
        )

    rendered = stream.getvalue()
    assert SECRET not in rendered
    assert ACCOUNT not in rendered
    assert "runtime-token" not in rendered
    assert "another-secret" not in rendered
    assert "acct_1" in rendered


def test_formatter_alone_is_a_final_defence_for_traceback_text() -> None:
    formatter = RedactingFormatter(redactor(), fmt="%(message)s")
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, SECRET, (), None)

    assert formatter.format(record) == REDACTED


def test_sanitizer_preserves_safe_scalar_diagnostics_and_rejects_weak_registration() -> None:
    value = {
        "amount": Decimal("12.34"),
        "path": Path("C:/AutoTrading/data"),
        "time": datetime(2026, 9, 4, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        "non_finite": float("nan"),
    }

    assert redactor().sanitize(value) == {
        "amount": "12.34",
        "path": str(Path("C:/AutoTrading/data")),
        "time": "2026-09-04T14:30:00+08:00",
        "non_finite": "[REDACTED:LIMIT]",
    }
    try:
        SensitiveValueRedactor(("abc",))
    except ValueError as exc:
        assert "four characters" in str(exc)
    else:
        raise AssertionError("weak secret registration should fail")
