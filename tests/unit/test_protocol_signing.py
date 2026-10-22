from __future__ import annotations

import copy
import re
from typing import Any

import pytest

from auto_trading_system.protocol.signing import (
    HEADER_CONTENT_SHA256,
    HEADER_KEY_ID,
    HEADER_NONCE,
    HEADER_REQUEST_ID,
    HEADER_REQUEST_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    InMemoryNonceStore,
    MappingKeyResolver,
    NonceStoreError,
    SignatureError,
    calculate_content_sha256,
    canonical_request_string,
    canonical_response_string,
    new_signature_nonce,
    sign_request,
    sign_response,
    verify_request,
    verify_response,
)

SECRET = b"unit-test-secret-at-least-thirty-two-bytes"
OTHER_SECRET = b"other-unit-test-secret-at-least-32-bytes"
KEY_ID = "node-key-current"
TIMESTAMP = 1_788_504_300
NONCE = "0123456789abcdeffedcba9876543210"
REQUEST_ID = "req_0123456789abcdeffedcba9876543210"
METHOD = "POST"
TARGET = "/api/v1/trading/events/batch?limit=50"
BODY = b'{"schema_version":"1.0"}'


def request_headers(**overrides: Any) -> dict[str, str]:
    arguments: dict[str, Any] = {
        "secret": SECRET,
        "key_id": KEY_ID,
        "timestamp": TIMESTAMP,
        "nonce": NONCE,
        "request_id": REQUEST_ID,
        "method": METHOD,
        "request_target": TARGET,
        "body": BODY,
    }
    arguments.update(overrides)
    return sign_request(**arguments)


def response_headers(**overrides: Any) -> dict[str, str]:
    arguments: dict[str, Any] = {
        "secret": SECRET,
        "key_id": KEY_ID,
        "timestamp": TIMESTAMP + 1,
        "request_nonce": NONCE,
        "request_id": REQUEST_ID,
        "status_code": 200,
        "body": BODY,
    }
    arguments.update(overrides)
    return sign_response(**arguments)


def verify_valid_request(
    headers: dict[str, str] | None = None,
    *,
    body: bytes = BODY,
    method: str = METHOD,
    target: str = TARGET,
    secret: bytes = SECRET,
    nonce_store: Any | None = None,
    now: int = TIMESTAMP,
    skew: int = 300,
) -> Any:
    return verify_request(
        headers=request_headers() if headers is None else headers,
        method=method,
        request_target=target,
        body=body,
        key_resolver=MappingKeyResolver({KEY_ID: secret}),
        nonce_store=InMemoryNonceStore() if nonce_store is None else nonce_store,
        now_epoch_seconds=now,
        max_clock_skew_seconds=skew,
    )


def test_new_signature_nonce_is_canonical_and_unique() -> None:
    first = new_signature_nonce()
    second = new_signature_nonce()

    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert re.fullmatch(r"[0-9a-f]{32}", second)
    assert first != second


def test_content_hash_uses_exact_body_bytes_including_empty_body() -> None:
    assert calculate_content_sha256(b"") == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert calculate_content_sha256(BODY) != calculate_content_sha256(BODY + b"\n")
    with pytest.raises(TypeError, match="body must be bytes"):
        calculate_content_sha256("not-bytes")  # type: ignore[arg-type]


def test_mapping_key_resolver_is_copying_and_redaction_safe() -> None:
    mutable_secret = bytearray(SECRET)
    resolver = MappingKeyResolver({KEY_ID: bytes(mutable_secret)})
    mutable_secret[0] = 0

    assert resolver.resolve(KEY_ID) == SECRET
    assert resolver.resolve("unknown-key") is None
    assert SECRET.decode() not in repr(resolver)
    assert "**REDACTED**" in repr(resolver)


@pytest.mark.parametrize(
    ("keys", "code"),
    [
        ({"bad key": SECRET}, "KEY_ID_INVALID"),
        ({KEY_ID: b"short"}, "SIGNING_SECRET_INVALID"),
    ],
)
def test_mapping_key_resolver_rejects_invalid_configuration(
    keys: dict[str, bytes], code: str
) -> None:
    with pytest.raises(SignatureError, match=code):
        MappingKeyResolver(keys)


def test_nonce_store_rejects_replay_and_expires_old_claims() -> None:
    store = InMemoryNonceStore(max_entries=2)

    assert store.claim(KEY_ID, NONCE, expires_at_epoch_seconds=20, now_epoch_seconds=10)
    assert not store.claim(KEY_ID, NONCE, expires_at_epoch_seconds=20, now_epoch_seconds=10)
    assert len(store) == 1
    assert store.claim(KEY_ID, "f" * 32, expires_at_epoch_seconds=30, now_epoch_seconds=21)
    assert len(store) == 1


def test_nonce_store_enforces_capacity_and_constructor_limit() -> None:
    with pytest.raises(ValueError, match="max_entries must be positive"):
        InMemoryNonceStore(max_entries=0)

    store = InMemoryNonceStore(max_entries=1)
    assert store.claim(KEY_ID, NONCE, expires_at_epoch_seconds=20, now_epoch_seconds=10)
    with pytest.raises(NonceStoreError, match="capacity exhausted"):
        store.claim(KEY_ID, "f" * 32, expires_at_epoch_seconds=20, now_epoch_seconds=10)


def test_request_signing_normalizes_method_and_verifies_case_insensitive_headers() -> None:
    headers = request_headers(method="post")
    lowercase_headers = {name.lower(): value for name, value in headers.items()}

    verified = verify_valid_request(lowercase_headers, method="post")

    assert verified.key_id == KEY_ID
    assert verified.timestamp == TIMESTAMP
    assert verified.content_sha256 == headers[HEADER_CONTENT_SHA256]


@pytest.mark.parametrize(
    ("argument", "value", "code"),
    [
        ("secret", b"short", "SIGNING_SECRET_INVALID"),
        ("key_id", "bad key", "KEY_ID_INVALID"),
        ("timestamp", -1, "TIMESTAMP_INVALID"),
        ("timestamp", True, "TIMESTAMP_INVALID"),
        ("nonce", "xyz", "NONCE_INVALID"),
        ("request_id", "request-1", "REQUEST_ID_INVALID"),
        ("method", "P0ST", "METHOD_INVALID"),
        ("method", None, "METHOD_INVALID"),
    ],
)
def test_request_signing_rejects_invalid_inputs(argument: str, value: object, code: str) -> None:
    with pytest.raises(SignatureError, match=code):
        request_headers(**{argument: value})


@pytest.mark.parametrize(
    "target",
    [
        "https://example.test/path",
        "/path#fragment",
        "/路径",
        "/path\nother",
        "/" + "a" * 4096,
    ],
)
def test_request_target_must_be_exact_safe_origin_form(target: str) -> None:
    with pytest.raises(SignatureError, match="REQUEST_TARGET_INVALID"):
        request_headers(request_target=target)


def test_canonical_builders_reject_invalid_digest_and_status() -> None:
    with pytest.raises(SignatureError, match="CONTENT_DIGEST_INVALID"):
        canonical_request_string(
            key_id=KEY_ID,
            timestamp=TIMESTAMP,
            nonce=NONCE,
            request_id=REQUEST_ID,
            method=METHOD,
            request_target=TARGET,
            content_sha256="invalid",
        )
    with pytest.raises(SignatureError, match="STATUS_CODE_INVALID"):
        canonical_response_string(
            key_id=KEY_ID,
            timestamp=TIMESTAMP,
            request_nonce=NONCE,
            request_id=REQUEST_ID,
            status_code=99,
            content_sha256=calculate_content_sha256(BODY),
        )
    with pytest.raises(SignatureError, match="CONTENT_DIGEST_INVALID"):
        canonical_response_string(
            key_id=KEY_ID,
            timestamp=TIMESTAMP,
            request_nonce=NONCE,
            request_id=REQUEST_ID,
            status_code=200,
            content_sha256="invalid",
        )


def test_verifier_requires_every_header() -> None:
    headers = request_headers()
    del headers[HEADER_NONCE]

    with pytest.raises(SignatureError, match="SIGNATURE_HEADER_MISSING"):
        verify_valid_request(headers)


def test_verifier_rejects_case_insensitive_duplicate_header() -> None:
    headers = request_headers()
    headers[HEADER_KEY_ID.lower()] = headers[HEADER_KEY_ID]

    with pytest.raises(SignatureError, match="DUPLICATE_SIGNATURE_HEADER"):
        verify_valid_request(headers)


@pytest.mark.parametrize(
    ("header", "value", "code"),
    [
        (HEADER_KEY_ID, "bad key", "KEY_ID_INVALID"),
        (HEADER_TIMESTAMP, "not-a-time", "TIMESTAMP_INVALID"),
        (HEADER_TIMESTAMP, "\uff11\uff12\uff13", "TIMESTAMP_INVALID"),
        (HEADER_NONCE, "not-a-nonce", "NONCE_INVALID"),
        (HEADER_REQUEST_ID, "bad-request", "REQUEST_ID_INVALID"),
        (HEADER_CONTENT_SHA256, "sha256:bad", "CONTENT_DIGEST_INVALID"),
        (HEADER_SIGNATURE, "v2=bad", "SIGNATURE_FORMAT_INVALID"),
    ],
)
def test_verifier_rejects_malformed_security_headers(header: str, value: str, code: str) -> None:
    headers = request_headers()
    headers[header] = value

    with pytest.raises(SignatureError, match=code):
        verify_valid_request(headers)


def test_verifier_rejects_non_text_header_name_or_value() -> None:
    headers_bad_name: dict[Any, Any] = request_headers()
    headers_bad_name[1] = "value"
    with pytest.raises(SignatureError, match="SIGNATURE_HEADER_INVALID"):
        verify_valid_request(headers_bad_name)

    headers_bad_value: dict[Any, Any] = request_headers()
    headers_bad_value[HEADER_KEY_ID] = 1
    with pytest.raises(SignatureError, match="SIGNATURE_HEADER_INVALID"):
        verify_valid_request(headers_bad_value)


@pytest.mark.parametrize(("now", "skew"), [(TIMESTAMP + 301, 300), (TIMESTAMP - 301, 300)])
def test_verifier_rejects_stale_or_future_request(now: int, skew: int) -> None:
    with pytest.raises(SignatureError, match="SIGNATURE_TIMESTAMP_OUT_OF_WINDOW"):
        verify_valid_request(now=now, skew=skew)


def test_verifier_accepts_clock_window_boundary() -> None:
    verify_valid_request(now=TIMESTAMP + 300)
    verify_valid_request(now=TIMESTAMP - 300)


@pytest.mark.parametrize(
    ("now", "skew", "code"),
    [(-1, 300, "TIMESTAMP_INVALID"), (TIMESTAMP, 0, "CLOCK_SKEW_POLICY_INVALID")],
)
def test_verifier_rejects_invalid_clock_policy(now: int, skew: int, code: str) -> None:
    with pytest.raises(SignatureError, match=code):
        verify_valid_request(now=now, skew=skew)


def test_verifier_rejects_body_method_and_target_tampering() -> None:
    with pytest.raises(SignatureError, match="CONTENT_DIGEST_MISMATCH"):
        verify_valid_request(body=BODY + b" ")
    with pytest.raises(SignatureError, match="SIGNATURE_MISMATCH"):
        verify_valid_request(method="PUT")
    with pytest.raises(SignatureError, match="SIGNATURE_MISMATCH"):
        verify_valid_request(target=TARGET + "&other=1")


def test_verifier_rejects_unknown_key_and_wrong_signature() -> None:
    with pytest.raises(SignatureError, match="SIGNING_KEY_UNKNOWN"):
        verify_request(
            headers=request_headers(),
            method=METHOD,
            request_target=TARGET,
            body=BODY,
            key_resolver=MappingKeyResolver({"other-key": OTHER_SECRET}),
            nonce_store=InMemoryNonceStore(),
            now_epoch_seconds=TIMESTAMP,
        )
    with pytest.raises(SignatureError, match="SIGNATURE_MISMATCH"):
        verify_valid_request(secret=OTHER_SECRET)


def test_request_nonce_can_be_claimed_only_once() -> None:
    store = InMemoryNonceStore()
    verify_valid_request(nonce_store=store)

    with pytest.raises(SignatureError, match="SIGNATURE_REPLAYED"):
        verify_valid_request(nonce_store=store)


class FailingNonceStore:
    def claim(self, *args: Any, **kwargs: Any) -> bool:
        raise NonceStoreError("backend down")


def test_nonce_store_failure_is_fail_closed() -> None:
    with pytest.raises(SignatureError, match="NONCE_STORE_UNAVAILABLE"):
        verify_valid_request(nonce_store=FailingNonceStore())


def test_response_verifies_and_returns_metadata() -> None:
    verified = verify_response(
        headers=response_headers(),
        status_code=200,
        body=BODY,
        expected_request_id=REQUEST_ID,
        expected_request_nonce=NONCE,
        key_resolver=MappingKeyResolver({KEY_ID: SECRET}),
        now_epoch_seconds=TIMESTAMP + 1,
    )

    assert verified.key_id == KEY_ID
    assert verified.timestamp == TIMESTAMP + 1
    assert verified.request_id == REQUEST_ID


@pytest.mark.parametrize(
    ("request_id", "request_nonce"),
    [
        ("req_ffffffffffffffffffffffffffffffff", NONCE),
        (REQUEST_ID, "f" * 32),
    ],
)
def test_response_must_bind_current_request(request_id: str, request_nonce: str) -> None:
    with pytest.raises(SignatureError, match="RESPONSE_REQUEST_MISMATCH"):
        verify_response(
            headers=response_headers(),
            status_code=200,
            body=BODY,
            expected_request_id=request_id,
            expected_request_nonce=request_nonce,
            key_resolver=MappingKeyResolver({KEY_ID: SECRET}),
            now_epoch_seconds=TIMESTAMP + 1,
        )


@pytest.mark.parametrize(
    ("body", "status", "secret", "code"),
    [
        (BODY + b" ", 200, SECRET, "CONTENT_DIGEST_MISMATCH"),
        (BODY, 202, SECRET, "SIGNATURE_MISMATCH"),
        (BODY, 200, OTHER_SECRET, "SIGNATURE_MISMATCH"),
    ],
)
def test_response_rejects_tampering(body: bytes, status: int, secret: bytes, code: str) -> None:
    with pytest.raises(SignatureError, match=code):
        verify_response(
            headers=response_headers(),
            status_code=status,
            body=body,
            expected_request_id=REQUEST_ID,
            expected_request_nonce=NONCE,
            key_resolver=MappingKeyResolver({KEY_ID: secret}),
            now_epoch_seconds=TIMESTAMP + 1,
        )


def test_response_rejects_unknown_key_and_stale_timestamp() -> None:
    with pytest.raises(SignatureError, match="SIGNING_KEY_UNKNOWN"):
        verify_response(
            headers=response_headers(),
            status_code=200,
            body=BODY,
            expected_request_id=REQUEST_ID,
            expected_request_nonce=NONCE,
            key_resolver=MappingKeyResolver({"other-key": OTHER_SECRET}),
            now_epoch_seconds=TIMESTAMP + 1,
        )
    with pytest.raises(SignatureError, match="SIGNATURE_TIMESTAMP_OUT_OF_WINDOW"):
        verify_response(
            headers=response_headers(),
            status_code=200,
            body=BODY,
            expected_request_id=REQUEST_ID,
            expected_request_nonce=NONCE,
            key_resolver=MappingKeyResolver({KEY_ID: SECRET}),
            now_epoch_seconds=TIMESTAMP + 302,
        )


def test_response_rejects_malformed_expected_request_identity() -> None:
    with pytest.raises(SignatureError, match="REQUEST_ID_INVALID"):
        verify_response(
            headers=response_headers(),
            status_code=200,
            body=BODY,
            expected_request_id="bad",
            expected_request_nonce=NONCE,
            key_resolver=MappingKeyResolver({KEY_ID: SECRET}),
            now_epoch_seconds=TIMESTAMP + 1,
        )
    with pytest.raises(SignatureError, match="NONCE_INVALID"):
        verify_response(
            headers=response_headers(),
            status_code=200,
            body=BODY,
            expected_request_id=REQUEST_ID,
            expected_request_nonce="bad",
            key_resolver=MappingKeyResolver({KEY_ID: SECRET}),
            now_epoch_seconds=TIMESTAMP + 1,
        )


def test_response_uses_request_nonce_header_not_request_nonce_generation() -> None:
    headers = copy.deepcopy(response_headers())
    assert HEADER_REQUEST_NONCE in headers
    assert HEADER_NONCE not in headers
    assert headers[HEADER_REQUEST_NONCE] == NONCE
    assert headers[HEADER_TIMESTAMP] == str(TIMESTAMP + 1)
    assert headers[HEADER_REQUEST_ID] == REQUEST_ID
    assert headers[HEADER_SIGNATURE].startswith("v1=")
