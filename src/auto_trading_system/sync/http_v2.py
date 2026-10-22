"""Bounded, signed HTTPS transport used only by the Windows agent.

This module owns no trading permission or SQLite connection. A failed request
keeps the immutable business ID/body; the scheduler chooses when to retry.
Certificate verification, no redirects, response signatures and byte limits
apply even to error responses. Tests inject an httpx transport, not a TLS bypass.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from threading import BoundedSemaphore
from time import monotonic, time
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

import httpx

from auto_trading_system.protocol.v2.api import (
    CapabilityManifest,
    CompleteDaily,
    DailyProgress,
    PlanLookup,
    PlanReceipt,
)
from auto_trading_system.protocol.v2.authentication import (
    RequestIdentity,
    canonical_target,
    request_headers,
    security_headers,
    verify_response,
)
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, parse_bytes
from auto_trading_system.protocol.v2.daily_sync import AppliedReceipt, DailyManifest
from auto_trading_system.protocol.v2.plans import PlanEnvelope
from auto_trading_system.sync.daily_upload import PermanentSyncError, RetryableSyncError


@dataclass(frozen=True, slots=True)
class NodeCredentials:
    node_id: str
    key_id: str
    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        RequestIdentity(self.node_id, self.key_id, 0, "validate", "validate")
        if len(self.secret) < 32:
            raise ValueError("node signing key must contain at least 32 bytes")


@dataclass(frozen=True, slots=True)
class SignedResponse:
    status: int
    body: bytes


class RemoteBackoff(RetryableSyncError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("REMOTE_BUSY")
        self.retry_after_seconds = retry_after_seconds


class SignedHttpClient:
    def __init__(
        self,
        base_url: str,
        credentials: NodeCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time,
        monotonic_clock: Callable[[], float] = monotonic,
        nonce_factory: Callable[[], str] = lambda: uuid4().hex,
        tls_context: ssl.SSLContext | None = None,
        admission: Callable[[], None] | None = None,
    ) -> None:
        origin = urlsplit(base_url)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or origin.path not in ("", "/")
        ):
            raise ValueError("quant endpoint must be an HTTPS origin without credentials or path")
        self._base_url = base_url.rstrip("/")
        self._credentials = credentials
        self._clock = clock
        self._monotonic = monotonic_clock
        self._nonce = nonce_factory
        self._admission = admission
        self._slot = BoundedSemaphore(1)
        if tls_context is not None and (
            tls_context.verify_mode != ssl.CERT_REQUIRED or not tls_context.check_hostname
        ):
            raise ValueError("TLS context must verify the certificate chain and hostname")
        self._client = httpx.Client(
            transport=transport,
            verify=tls_context if tls_context is not None else True,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept-Encoding": "identity", "Accept": "application/json"},
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        target: str,
        body: bytes = b"",
        *,
        request_id: str | None = None,
        part_upload: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> SignedResponse:
        if self._admission is not None:
            self._admission()  # Invocation deadline/stop checked before each network operation.
        target = canonical_target(target)
        if len(body) > 512 * 1024:
            raise PermanentSyncError("REQUEST_TOO_LARGE")
        if extra_headers and set(extra_headers) - {"If-None-Match"}:
            raise ValueError("caller cannot override transport security headers")
        if not self._slot.acquire(blocking=False):
            raise RemoteBackoff(1)
        budget = 15 if part_upload else 8
        started = self._monotonic()
        try:
            identity = RequestIdentity(
                self._credentials.node_id,
                self._credentials.key_id,
                int(self._clock()),
                self._nonce(),
                request_id or uuid4().hex,
            )
            headers = request_headers(self._credentials.secret, method, target, identity, body)
            headers.update(extra_headers or {})
            headers["Content-Type"] = "application/json"
            with self._client.stream(
                method,
                self._base_url + target,
                content=body,
                headers=headers,
                timeout=httpx.Timeout(budget, connect=3, pool=1),
            ) as response:
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise PermanentSyncError("UNEXPECTED_RESPONSE_COMPRESSION")
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    if len(chunks) + len(chunk) > 512 * 1024:
                        raise PermanentSyncError("RESPONSE_TOO_LARGE")
                    chunks.extend(chunk)
                    if self._monotonic() - started > budget:
                        raise RetryableSyncError("RESPONSE_DEADLINE_EXCEEDED")
                data = bytes(chunks)
                if self._monotonic() - started > budget:
                    raise RetryableSyncError("RESPONSE_DEADLINE_EXCEEDED")
                # A reverse proxy/auth outage may have no signing key available.
                # Trust no payload in that case; a bounded retry is safe because
                # business IDs and immutable bodies remain unchanged.
                if response.status_code in (429, 503) and "x-ats-signature" not in response.headers:
                    raise RemoteBackoff(15)
                try:
                    verify_response(
                        self._credentials.secret,
                        security_headers(response.headers.raw),
                        key_id=identity.key_id,
                        request_id=identity.request_id,
                        status=response.status_code,
                        body=data,
                        now_utc_seconds=int(self._clock()),
                    )
                except ValueError as exc:
                    raise PermanentSyncError("UNTRUSTED_RESPONSE") from exc
                if response.status_code in (429, 503):
                    delay = response.headers.get("retry-after", "15")
                    raise RemoteBackoff(min(900, max(1, int(delay))) if delay.isdecimal() else 15)
                if response.status_code >= 500:
                    raise RetryableSyncError("REMOTE_FAILURE")
                if response.status_code not in (200, 201, 204, 304, 404):
                    raise PermanentSyncError(f"REMOTE_STATUS_{response.status_code}")
                if data:
                    parse_bytes(data, max_bytes=512 * 1024)
                return SignedResponse(response.status_code, data)
        except httpx.HTTPError as exc:
            raise RetryableSyncError("TRANSPORT_UNCERTAIN") from exc
        finally:
            self._slot.release()


class HttpQuantTransport:
    """Typed plan and daily-sync APIs over a single authenticated client."""

    def __init__(self, client: SignedHttpClient) -> None:
        self._client = client

    def manifest(self) -> CapabilityManifest:
        response = self._client.request("GET", "/api/v2/trading/manifest")
        self._require(response, 200)
        return CapabilityManifest.model_validate_json(response.body)

    def current_plan(
        self, account_id: str, strategy_id: str, trade_date: date, etag: str | None = None
    ) -> PlanLookup | None:
        query = urlencode(
            {
                "account_id": account_id,
                "strategy_id": strategy_id,
                "trade_date": trade_date.isoformat(),
            }
        )
        response = self._client.request(
            "GET",
            "/api/v2/trading/plans/current?" + query,
            extra_headers={"If-None-Match": f'"{etag}"'} if etag else None,
        )
        if response.status in (204, 304):
            if response.status == 304 and etag is None:
                raise PermanentSyncError("UNEXPECTED_NOT_MODIFIED")
            return None
        self._require(response, 200)
        lookup = PlanLookup.model_validate_json(response.body)
        if (lookup.account_id, lookup.strategy_id, lookup.trade_date) != (
            account_id,
            strategy_id,
            trade_date,
        ):
            raise PermanentSyncError("PLAN_LOOKUP_SCOPE_MISMATCH")
        return lookup

    def plan(self, plan_id: str) -> PlanEnvelope:
        response = self._client.request("GET", f"/api/v2/trading/plans/{_path_id(plan_id)}")
        self._require(response, 200)
        return PlanEnvelope.model_validate_json(response.body)

    def plan_receipt(self, receipt: PlanReceipt) -> None:
        response = self._client.request(
            "POST", f"/api/v2/trading/plans/{_path_id(receipt.plan_id)}/receipts", _body(receipt)
        )
        self._require(response, 200)
        acknowledged = PlanReceipt.model_validate_json(response.body)
        if acknowledged != receipt:
            raise PermanentSyncError("PLAN_RECEIPT_ACK_MISMATCH")

    def receipt(self, batch_id: str) -> AppliedReceipt | None:
        response = self._client.request("GET", self._daily(batch_id) + "/receipt")
        if response.status == 404:
            return None
        self._require(response, 200)
        receipt = AppliedReceipt.model_validate_json(response.body)
        if receipt.batch_id != batch_id:
            raise PermanentSyncError("RECEIPT_BATCH_MISMATCH")
        return receipt

    def create(self, manifest: DailyManifest) -> None:
        response = self._client.request("POST", "/api/v2/trading/daily-syncs", _body(manifest))
        self._require(response, 200, 201)

    def missing_parts(self, batch_id: str) -> tuple[str, ...]:
        response = self._client.request("GET", self._daily(batch_id))
        self._require(response, 200)
        progress = DailyProgress.model_validate_json(response.body)
        if progress.batch_id != batch_id:
            raise PermanentSyncError("PROGRESS_BATCH_MISMATCH")
        return progress.missing_parts

    def put_part(self, batch_id: str, part_id: str, body: bytes) -> None:
        response = self._client.request(
            "PUT", self._daily(batch_id) + f"/parts/{_path_id(part_id)}", body, part_upload=True
        )
        self._require(response, 200)

    def complete(self, batch_id: str, manifest_hash: str) -> None:
        response = self._client.request(
            "POST",
            self._daily(batch_id) + "/complete",
            _body(CompleteDaily(manifest_hash=manifest_hash)),
        )
        self._require(response, 200)

    @staticmethod
    def _daily(batch_id: str) -> str:
        return f"/api/v2/trading/daily-syncs/{_path_id(batch_id)}"

    @staticmethod
    def _require(response: SignedResponse, *statuses: int) -> None:
        if response.status not in statuses:
            raise PermanentSyncError(f"UNEXPECTED_STATUS_{response.status}")


def _path_id(value: str) -> str:
    # Preserve the WireId alphabet while encoding path-reserved characters once.
    if (
        not value
        or len(value) > 96
        or value in (".", "..")
        or any(not (c.isascii() and (c.isalnum() or c in "_.:-")) for c in value)
    ):
        raise ValueError("invalid API path identifier")
    return quote(value, safe="-._~")


def _body(model: WireModel) -> bytes:
    return canonical_bytes(model.model_dump(mode="json"))
