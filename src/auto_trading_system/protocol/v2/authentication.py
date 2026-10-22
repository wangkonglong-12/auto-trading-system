"""V2 HMAC message authentication, independent of HTTP and secret storage.

Transport adapters must enforce TLS and persist request nonces on the receiving
side. These primitives only authenticate bytes; they do not authorize accounts.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from auto_trading_system.protocol.v2.canonical import digest

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    node_id: str
    key_id: str
    timestamp: int
    nonce: str
    request_id: str

    def __post_init__(self) -> None:
        if any(
            not _ID.fullmatch(value)
            for value in (self.node_id, self.key_id, self.nonce, self.request_id)
        ):
            raise ValueError("invalid request identity")
        if type(self.timestamp) is not int or self.timestamp < 0:
            raise ValueError("timestamp must be integer UTC seconds")


def canonical_target(target: str) -> str:
    if len(target) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in target):
        raise ValueError("target contains control characters or exceeds its size limit")
    if re.search(r"%(?![0-9A-Fa-f]{2})", target):
        raise ValueError("malformed URL escape")
    parts = urlsplit(target)
    if (
        parts.scheme
        or parts.netloc
        or parts.fragment
        or not parts.path.startswith("/api/v2/trading/")
    ):
        raise ValueError("target must be a V2 relative API path without fragment")
    path = quote(unquote(parts.path, errors="strict"), safe="/-._~")
    if path != parts.path or any(part in (".", "..") for part in path.split("/")):
        raise ValueError("path must use one unambiguous canonical encoding")
    pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, errors="strict")
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("duplicate query parameters")
    query = "&".join(
        f"{quote(key, safe='-._~')}={quote(value, safe='-._~')}" for key, value in sorted(pairs)
    )
    return path + ("?" + query if query else "")


def request_preimage(method: str, target: str, identity: RequestIdentity, body: bytes) -> bytes:
    if method not in ("GET", "POST", "PUT"):
        raise ValueError("unsupported V2 method")
    return "\n".join(
        (
            "AUTO-TRADING/REQUEST/V2",
            method,
            canonical_target(target),
            identity.node_id,
            identity.key_id,
            str(identity.timestamp),
            identity.nonce,
            identity.request_id,
            digest(body),
        )
    ).encode("utf-8")


def sign_message(secret: bytes, message: bytes) -> str:
    if len(secret) < 32:
        raise ValueError("HMAC secrets require at least 32 bytes")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify_request(
    secret: bytes,
    signature: str,
    *,
    method: str,
    target: str,
    identity: RequestIdentity,
    body: bytes,
    now_utc_seconds: int,
) -> None:
    if abs(now_utc_seconds - identity.timestamp) > 30:
        raise ValueError("request exceeds the 30-second clock-skew window")
    expected = sign_message(secret, request_preimage(method, target, identity, body))
    if not hmac.compare_digest(signature, expected):
        raise ValueError("request signature mismatch")


def response_preimage(request_id: str, status: int, timestamp: int, body: bytes) -> bytes:
    if not _ID.fullmatch(request_id) or not 100 <= status <= 599 or timestamp < 0:
        raise ValueError("invalid response identity")
    return "\n".join(
        (
            "AUTO-TRADING/RESPONSE/V2",
            request_id,
            str(status),
            str(timestamp),
            digest(body),
        )
    ).encode("utf-8")


def request_headers(
    secret: bytes, method: str, target: str, identity: RequestIdentity, body: bytes
) -> dict[str, str]:
    """Each retry keeps business payload identity and obtains a fresh nonce."""
    return {
        "X-ATS-Node-Id": identity.node_id,
        "X-ATS-Key-Id": identity.key_id,
        "X-ATS-Timestamp": str(identity.timestamp),
        "X-ATS-Nonce": identity.nonce,
        "X-ATS-Request-Id": identity.request_id,
        "X-ATS-Content-SHA256": digest(body),
        "X-ATS-Signature": sign_message(secret, request_preimage(method, target, identity, body)),
    }


def security_headers(raw: Sequence[tuple[bytes, bytes]]) -> dict[str, str]:
    """Read raw headers so duplicates cannot be hidden by framework normalization."""
    result: dict[str, str] = {}
    for name, value in raw:
        key = name.decode("ascii").lower()
        if key.startswith("x-ats-"):
            if key in result:
                raise ValueError("duplicate authentication header")
            text = value.decode("ascii")
            if len(text) > 256 or any(ord(char) < 32 or ord(char) > 126 for char in text):
                raise ValueError("invalid authentication header")
            result[key] = text
    return result


def parse_identity(headers: Mapping[str, str], body: bytes) -> RequestIdentity:
    try:
        raw_timestamp = headers["x-ats-timestamp"]
        if not re.fullmatch(r"[0-9]{1,12}", raw_timestamp):
            raise ValueError("invalid timestamp encoding")
        if headers["x-ats-content-sha256"] != digest(body):
            raise ValueError("content hash mismatch")
        return RequestIdentity(
            headers["x-ats-node-id"],
            headers["x-ats-key-id"],
            int(raw_timestamp),
            headers["x-ats-nonce"],
            headers["x-ats-request-id"],
        )
    except KeyError as exc:
        raise ValueError("missing authentication header") from exc


def signed_response_headers(
    secret: bytes, *, key_id: str, request_id: str, status: int, timestamp: int, body: bytes
) -> dict[str, str]:
    return {
        "X-ATS-Key-Id": key_id,
        "X-ATS-Request-Id": request_id,
        "X-ATS-Timestamp": str(timestamp),
        "X-ATS-Content-SHA256": digest(body),
        "X-ATS-Signature": sign_message(
            secret, response_preimage(request_id, status, timestamp, body)
        ),
    }


def verify_response(
    secret: bytes,
    headers: Mapping[str, str],
    *,
    key_id: str,
    request_id: str,
    status: int,
    body: bytes,
    now_utc_seconds: int,
) -> None:
    try:
        raw_timestamp = headers["x-ats-timestamp"]
        if not re.fullmatch(r"[0-9]{1,12}", raw_timestamp):
            raise ValueError("invalid response timestamp")
        timestamp = int(raw_timestamp)
        if abs(now_utc_seconds - timestamp) > 30:
            raise ValueError("response timestamp outside clock-skew window")
        if headers["x-ats-request-id"] != request_id or headers["x-ats-key-id"] != key_id:
            raise ValueError("response does not bind this request and key")
        if headers["x-ats-content-sha256"] != digest(body):
            raise ValueError("response body hash mismatch")
        expected = sign_message(secret, response_preimage(request_id, status, timestamp, body))
        if not hmac.compare_digest(headers["x-ats-signature"], expected):
            raise ValueError("response signature mismatch")
    except KeyError as exc:
        raise ValueError("unsigned response") from exc
