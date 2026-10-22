"""Transport-neutral HMAC-SHA256 signing and replay protection."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

SIGNATURE_VERSION = "v1"
SIGNATURE_SCHEME = "ATS-HMAC-SHA256"
DEFAULT_MAX_CLOCK_SKEW_SECONDS = 300
MIN_SECRET_BYTES = 32

HEADER_KEY_ID = "X-ATS-Key-Id"
HEADER_TIMESTAMP = "X-ATS-Timestamp"
HEADER_NONCE = "X-ATS-Nonce"
HEADER_REQUEST_NONCE = "X-ATS-Request-Nonce"
HEADER_REQUEST_ID = "X-ATS-Request-Id"
HEADER_CONTENT_SHA256 = "X-ATS-Content-SHA256"
HEADER_SIGNATURE = "X-ATS-Signature"

_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_REQUEST_ID_PATTERN = re.compile(r"^req_[0-9a-f]{32}$")
_CONTENT_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SIGNATURE_PATTERN = re.compile(r"^v1=[0-9a-f]{64}$")
_METHOD_PATTERN = re.compile(r"^[A-Z]+$")
_DUMMY_SECRET = b"\x00" * MIN_SECRET_BYTES


class SignatureError(ValueError):
    """Raised for malformed, invalid, stale, or replayed signatures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class NonceStoreError(RuntimeError):
    """Raised when replay state cannot be recorded safely."""


class SigningKeyResolver(Protocol):
    """Resolve active or grace-period verification keys by public key id."""

    def resolve(self, key_id: str) -> bytes | None:
        """Return raw secret bytes without logging them, or None for an unknown key."""
        ...


class NonceStore(Protocol):
    """Atomically claim an authenticated request nonce for its validity window."""

    def claim(
        self,
        key_id: str,
        nonce: str,
        *,
        expires_at_epoch_seconds: int,
        now_epoch_seconds: int,
    ) -> bool:
        """Return True only for the first claim of key_id plus nonce."""
        ...


class MappingKeyResolver:
    """Small redaction-safe resolver useful for configuration adapters and tests."""

    __slots__ = ("_keys",)

    def __init__(self, keys: Mapping[str, bytes]) -> None:
        validated: dict[str, bytes] = {}
        for key_id, secret in keys.items():
            _validate_key_id(key_id)
            _validate_secret(secret)
            validated[key_id] = bytes(secret)
        self._keys = validated

    def resolve(self, key_id: str) -> bytes | None:
        secret = self._keys.get(key_id)
        return None if secret is None else bytes(secret)

    def __repr__(self) -> str:
        return f"MappingKeyResolver(key_ids={sorted(self._keys)!r}, secrets=**REDACTED**)"


class InMemoryNonceStore:
    """Thread-safe test/development nonce store; production uses a shared durable adapter."""

    __slots__ = ("_claims", "_lock", "_max_entries")

    def __init__(self, *, max_entries: int = 100_000) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._claims: dict[tuple[str, str], int] = {}
        self._lock = Lock()
        self._max_entries = max_entries

    def claim(
        self,
        key_id: str,
        nonce: str,
        *,
        expires_at_epoch_seconds: int,
        now_epoch_seconds: int,
    ) -> bool:
        identity = (key_id, nonce)
        with self._lock:
            expired = [
                key for key, expires_at in self._claims.items() if expires_at < now_epoch_seconds
            ]
            for key in expired:
                del self._claims[key]
            if identity in self._claims:
                return False
            if len(self._claims) >= self._max_entries:
                raise NonceStoreError("nonce store capacity exhausted")
            self._claims[identity] = expires_at_epoch_seconds
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._claims)


@dataclass(frozen=True, slots=True)
class VerifiedRequestSignature:
    """Authenticated request identity used to bind the signed response."""

    key_id: str
    timestamp: int
    nonce: str
    request_id: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedResponseSignature:
    """Authenticated response metadata."""

    key_id: str
    timestamp: int
    request_nonce: str
    request_id: str
    content_sha256: str


def new_signature_nonce() -> str:
    """Create a 128-bit lowercase nonce for one HTTP attempt."""
    return secrets.token_hex(16)


def calculate_content_sha256(body: bytes) -> str:
    """Hash the exact transmitted body bytes, including an empty body."""
    if not isinstance(body, bytes):
        raise TypeError("body must be bytes")
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _validate_secret(secret: bytes) -> None:
    if not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES:
        raise SignatureError("SIGNING_SECRET_INVALID", "secret must contain at least 32 bytes")


def _validate_key_id(key_id: str) -> None:
    if not isinstance(key_id, str) or _KEY_ID_PATTERN.fullmatch(key_id) is None:
        raise SignatureError("KEY_ID_INVALID", "key id format is invalid")


def _validate_timestamp(timestamp: int) -> None:
    if type(timestamp) is not int or not 0 <= timestamp <= 99_999_999_999:
        raise SignatureError("TIMESTAMP_INVALID", "timestamp must be Unix seconds")


def _validate_nonce(nonce: str, *, field: str = "nonce") -> None:
    if not isinstance(nonce, str) or _NONCE_PATTERN.fullmatch(nonce) is None:
        raise SignatureError(
            "NONCE_INVALID", f"{field} must be 32 lowercase hexadecimal characters"
        )


def _validate_request_id(request_id: str) -> None:
    if not isinstance(request_id, str) or _REQUEST_ID_PATTERN.fullmatch(request_id) is None:
        raise SignatureError("REQUEST_ID_INVALID", "request id format is invalid")


def _validate_method(method: str) -> str:
    if not isinstance(method, str):
        raise SignatureError("METHOD_INVALID", "HTTP method is invalid")
    normalized = method.upper()
    if _METHOD_PATTERN.fullmatch(normalized) is None:
        raise SignatureError("METHOD_INVALID", "HTTP method is invalid")
    return normalized


def _validate_request_target(request_target: str) -> None:
    if (
        not isinstance(request_target, str)
        or not request_target.startswith("/")
        or "#" in request_target
        or len(request_target) > 4096
    ):
        raise SignatureError("REQUEST_TARGET_INVALID", "use the exact origin-form path and query")
    try:
        encoded = request_target.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SignatureError(
            "REQUEST_TARGET_INVALID", "target must be ASCII percent-encoded"
        ) from exc
    if any(byte < 0x20 or byte == 0x7F for byte in encoded):
        raise SignatureError("REQUEST_TARGET_INVALID", "target contains a control character")


def _normalized_headers(headers: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str):
            raise SignatureError("SIGNATURE_HEADER_INVALID", "header name must be text")
        lowered = name.lower()
        if lowered in normalized:
            raise SignatureError("DUPLICATE_SIGNATURE_HEADER", f"duplicate header {name!r}")
        if not isinstance(value, str):
            raise SignatureError("SIGNATURE_HEADER_INVALID", f"header {name!r} must be text")
        normalized[lowered] = value
    return normalized


def _required_header(headers: Mapping[str, str], name: str) -> str:
    try:
        return headers[name.lower()]
    except KeyError as exc:
        raise SignatureError(
            "SIGNATURE_HEADER_MISSING", f"required header {name} is missing"
        ) from exc


def _parse_signature_headers(
    headers: Mapping[str, str], *, response: bool
) -> tuple[str, int, str, str, str, str]:
    normalized = _normalized_headers(headers)
    key_id = _required_header(normalized, HEADER_KEY_ID)
    timestamp_text = _required_header(normalized, HEADER_TIMESTAMP)
    nonce_header = HEADER_REQUEST_NONCE if response else HEADER_NONCE
    nonce = _required_header(normalized, nonce_header)
    request_id = _required_header(normalized, HEADER_REQUEST_ID)
    content_sha256 = _required_header(normalized, HEADER_CONTENT_SHA256)
    signature = _required_header(normalized, HEADER_SIGNATURE)

    _validate_key_id(key_id)
    if not timestamp_text.isascii() or not timestamp_text.isdecimal():
        raise SignatureError("TIMESTAMP_INVALID", "timestamp header must be Unix seconds")
    timestamp = int(timestamp_text)
    _validate_timestamp(timestamp)
    _validate_nonce(nonce, field=nonce_header)
    _validate_request_id(request_id)
    if _CONTENT_DIGEST_PATTERN.fullmatch(content_sha256) is None:
        raise SignatureError("CONTENT_DIGEST_INVALID", "content digest format is invalid")
    if _SIGNATURE_PATTERN.fullmatch(signature) is None:
        raise SignatureError("SIGNATURE_FORMAT_INVALID", "signature must use v1 lowercase hex")
    return key_id, timestamp, nonce, request_id, content_sha256, signature


def _validate_freshness(
    timestamp: int, now_epoch_seconds: int, max_clock_skew_seconds: int
) -> None:
    _validate_timestamp(now_epoch_seconds)
    if type(max_clock_skew_seconds) is not int or max_clock_skew_seconds < 1:
        raise SignatureError("CLOCK_SKEW_POLICY_INVALID", "max clock skew must be positive")
    if abs(now_epoch_seconds - timestamp) > max_clock_skew_seconds:
        raise SignatureError(
            "SIGNATURE_TIMESTAMP_OUT_OF_WINDOW", "signature timestamp is stale or future"
        )


def canonical_request_string(
    *,
    key_id: str,
    timestamp: int,
    nonce: str,
    request_id: str,
    method: str,
    request_target: str,
    content_sha256: str,
) -> str:
    """Build the exact v1 request string-to-sign."""
    _validate_key_id(key_id)
    _validate_timestamp(timestamp)
    _validate_nonce(nonce)
    _validate_request_id(request_id)
    normalized_method = _validate_method(method)
    _validate_request_target(request_target)
    if _CONTENT_DIGEST_PATTERN.fullmatch(content_sha256) is None:
        raise SignatureError("CONTENT_DIGEST_INVALID", "content digest format is invalid")
    return "\n".join(
        (
            SIGNATURE_SCHEME,
            "request",
            SIGNATURE_VERSION,
            key_id,
            str(timestamp),
            nonce,
            request_id,
            normalized_method,
            request_target,
            content_sha256,
        )
    )


def canonical_response_string(
    *,
    key_id: str,
    timestamp: int,
    request_nonce: str,
    request_id: str,
    status_code: int,
    content_sha256: str,
) -> str:
    """Build a response string bound to the originating request nonce and id."""
    _validate_key_id(key_id)
    _validate_timestamp(timestamp)
    _validate_nonce(request_nonce, field="request_nonce")
    _validate_request_id(request_id)
    if type(status_code) is not int or not 100 <= status_code <= 599:
        raise SignatureError("STATUS_CODE_INVALID", "HTTP status code is invalid")
    if _CONTENT_DIGEST_PATTERN.fullmatch(content_sha256) is None:
        raise SignatureError("CONTENT_DIGEST_INVALID", "content digest format is invalid")
    return "\n".join(
        (
            SIGNATURE_SCHEME,
            "response",
            SIGNATURE_VERSION,
            key_id,
            str(timestamp),
            request_nonce,
            request_id,
            str(status_code),
            content_sha256,
        )
    )


def _hmac_signature(secret: bytes, canonical_value: str) -> str:
    _validate_secret(secret)
    digest = hmac.new(secret, canonical_value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def sign_request(
    *,
    secret: bytes,
    key_id: str,
    timestamp: int,
    nonce: str,
    request_id: str,
    method: str,
    request_target: str,
    body: bytes,
) -> dict[str, str]:
    """Return all v1 headers for one request attempt."""
    content_sha256 = calculate_content_sha256(body)
    canonical_value = canonical_request_string(
        key_id=key_id,
        timestamp=timestamp,
        nonce=nonce,
        request_id=request_id,
        method=method,
        request_target=request_target,
        content_sha256=content_sha256,
    )
    return {
        HEADER_KEY_ID: key_id,
        HEADER_TIMESTAMP: str(timestamp),
        HEADER_NONCE: nonce,
        HEADER_REQUEST_ID: request_id,
        HEADER_CONTENT_SHA256: content_sha256,
        HEADER_SIGNATURE: _hmac_signature(secret, canonical_value),
    }


def sign_response(
    *,
    secret: bytes,
    key_id: str,
    timestamp: int,
    request_nonce: str,
    request_id: str,
    status_code: int,
    body: bytes,
) -> dict[str, str]:
    """Return response headers bound to the corresponding request attempt."""
    content_sha256 = calculate_content_sha256(body)
    canonical_value = canonical_response_string(
        key_id=key_id,
        timestamp=timestamp,
        request_nonce=request_nonce,
        request_id=request_id,
        status_code=status_code,
        content_sha256=content_sha256,
    )
    return {
        HEADER_KEY_ID: key_id,
        HEADER_TIMESTAMP: str(timestamp),
        HEADER_REQUEST_NONCE: request_nonce,
        HEADER_REQUEST_ID: request_id,
        HEADER_CONTENT_SHA256: content_sha256,
        HEADER_SIGNATURE: _hmac_signature(secret, canonical_value),
    }


def verify_request(
    *,
    headers: Mapping[str, str],
    method: str,
    request_target: str,
    body: bytes,
    key_resolver: SigningKeyResolver,
    nonce_store: NonceStore,
    now_epoch_seconds: int,
    max_clock_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
) -> VerifiedRequestSignature:
    """Authenticate a request and atomically reject nonce replay."""
    key_id, timestamp, nonce, request_id, content_sha256, signature = _parse_signature_headers(
        headers, response=False
    )
    _validate_freshness(timestamp, now_epoch_seconds, max_clock_skew_seconds)
    expected_content_sha256 = calculate_content_sha256(body)
    if not hmac.compare_digest(content_sha256, expected_content_sha256):
        raise SignatureError("CONTENT_DIGEST_MISMATCH", "body does not match content digest")
    canonical_value = canonical_request_string(
        key_id=key_id,
        timestamp=timestamp,
        nonce=nonce,
        request_id=request_id,
        method=method,
        request_target=request_target,
        content_sha256=content_sha256,
    )
    resolved_secret = key_resolver.resolve(key_id)
    expected_signature = _hmac_signature(
        _DUMMY_SECRET if resolved_secret is None else resolved_secret, canonical_value
    )
    signature_valid = hmac.compare_digest(signature, expected_signature)
    if resolved_secret is None:
        raise SignatureError("SIGNING_KEY_UNKNOWN", "signing key is unknown or inactive")
    if not signature_valid:
        raise SignatureError("SIGNATURE_MISMATCH", "signature verification failed")
    try:
        claimed = nonce_store.claim(
            key_id,
            nonce,
            expires_at_epoch_seconds=timestamp + max_clock_skew_seconds,
            now_epoch_seconds=now_epoch_seconds,
        )
    except NonceStoreError as exc:
        raise SignatureError(
            "NONCE_STORE_UNAVAILABLE", "cannot establish replay protection"
        ) from exc
    if not claimed:
        raise SignatureError("SIGNATURE_REPLAYED", "request nonce has already been used")
    return VerifiedRequestSignature(key_id, timestamp, nonce, request_id, content_sha256)


def verify_response(
    *,
    headers: Mapping[str, str],
    status_code: int,
    body: bytes,
    expected_request_id: str,
    expected_request_nonce: str,
    key_resolver: SigningKeyResolver,
    now_epoch_seconds: int,
    max_clock_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
) -> VerifiedResponseSignature:
    """Authenticate a response and bind it to the current request attempt."""
    key_id, timestamp, request_nonce, request_id, content_sha256, signature = (
        _parse_signature_headers(headers, response=True)
    )
    _validate_freshness(timestamp, now_epoch_seconds, max_clock_skew_seconds)
    _validate_request_id(expected_request_id)
    _validate_nonce(expected_request_nonce, field="expected_request_nonce")
    if not hmac.compare_digest(request_id, expected_request_id) or not hmac.compare_digest(
        request_nonce, expected_request_nonce
    ):
        raise SignatureError("RESPONSE_REQUEST_MISMATCH", "response is not bound to this request")
    expected_content_sha256 = calculate_content_sha256(body)
    if not hmac.compare_digest(content_sha256, expected_content_sha256):
        raise SignatureError("CONTENT_DIGEST_MISMATCH", "body does not match content digest")
    canonical_value = canonical_response_string(
        key_id=key_id,
        timestamp=timestamp,
        request_nonce=request_nonce,
        request_id=request_id,
        status_code=status_code,
        content_sha256=content_sha256,
    )
    resolved_secret = key_resolver.resolve(key_id)
    expected_signature = _hmac_signature(
        _DUMMY_SECRET if resolved_secret is None else resolved_secret, canonical_value
    )
    signature_valid = hmac.compare_digest(signature, expected_signature)
    if resolved_secret is None:
        raise SignatureError("SIGNING_KEY_UNKNOWN", "signing key is unknown or inactive")
    if not signature_valid:
        raise SignatureError("SIGNATURE_MISMATCH", "signature verification failed")
    return VerifiedResponseSignature(key_id, timestamp, request_nonce, request_id, content_sha256)
