"""Authenticated HTTP transport for Domain Event outbox batches."""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import httpx

from auto_trading_system.domain import Clock, ShanghaiTimestamp
from auto_trading_system.journal import OutboxItem, OutboxKind, OutboxState
from auto_trading_system.protocol import (
    MappingKeyResolver,
    SignatureError,
    new_request_id,
    sign_request,
    validate_domain_event_semantics,
    validate_event_batch_result_semantics,
    validate_event_batch_semantics,
    verify_response,
)
from auto_trading_system.protocol.canonical_json import canonical_json_bytes
from auto_trading_system.sync.models import (
    OutboxDeliveryDisposition,
    OutboxDeliveryError,
    OutboxDeliveryReceipt,
)

EVENT_BATCH_PATH = "/events/batch"
_NODE_ID_PATTERN = re.compile(r"node-[a-z0-9][a-z0-9-]{2,47}")
_ACCOUNT_ID_PATTERN = re.compile(r"acct_[A-Za-z0-9_-]{3,48}")


@runtime_checkable
class EpochClock(Protocol):
    def now_epoch_seconds(self) -> int: ...


class SystemEpochClock:
    def now_epoch_seconds(self) -> int:
        return int(time.time())


class _EventReceiptStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


class SignedEventBatchHttpTransport:
    """Send one event batch with a fresh signed request and authenticated response."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        node_id: str,
        account_id: str,
        key_id: str,
        secret: bytes,
        clock: Clock,
        epoch_clock: EpochClock | None = None,
        request_id_factory: Callable[[], str] = new_request_id,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(client, httpx.AsyncClient):
            raise ValueError("client must be an httpx.AsyncClient")
        url = _validate_base_url(base_url)
        if not isinstance(node_id, str) or _NODE_ID_PATTERN.fullmatch(node_id) is None:
            raise ValueError("node_id has an invalid format")
        if not isinstance(account_id, str) or _ACCOUNT_ID_PATTERN.fullmatch(account_id) is None:
            raise ValueError("account_id has an invalid format")
        if type(secret) is not bytes:
            raise ValueError("secret must be bytes")
        if not callable(getattr(clock, "now", None)):
            raise ValueError("clock must implement Clock")
        effective_epoch_clock = epoch_clock or SystemEpochClock()
        if not isinstance(effective_epoch_clock, EpochClock):
            raise ValueError("epoch_clock must implement EpochClock")
        if not callable(request_id_factory):
            raise ValueError("request_id_factory must be callable")
        effective_nonce_factory = nonce_factory or (lambda: secrets.token_hex(16))
        if not callable(effective_nonce_factory):
            raise ValueError("nonce_factory must be callable")
        self._client = client
        self._base_url = str(url.copy_with(path=url.path.rstrip("/")))
        self._url = url.join(EVENT_BATCH_PATH.removeprefix("/"))
        self._node_id = node_id
        self._account_id = account_id
        self._key_id = key_id
        self._secret = secret
        self._key_resolver = MappingKeyResolver({key_id: self._secret})
        self._clock = clock
        self._epoch_clock = effective_epoch_clock
        self._request_id_factory = request_id_factory
        self._nonce_factory = effective_nonce_factory

    def __repr__(self) -> str:
        return (
            "SignedEventBatchHttpTransport("
            f"base_url={self._base_url!r}, "
            f"node_id={self._node_id!r}, account_id={self._account_id!r}, "
            f"key_id={self._key_id!r}, secret=**REDACTED**)"
        )

    async def deliver(self, items: tuple[OutboxItem, ...]) -> tuple[OutboxDeliveryReceipt, ...]:
        request_id = self._request_id_factory()
        nonce = self._nonce_factory()
        try:
            request = self._build_request(items, request_id)
            body = canonical_json_bytes(request, exclude=frozenset())
            timestamp = self._epoch_clock.now_epoch_seconds()
            headers = sign_request(
                secret=self._secret,
                key_id=self._key_id,
                timestamp=timestamp,
                nonce=nonce,
                request_id=request_id,
                method="POST",
                request_target=self._url.path,
                body=body,
            )
        except (TypeError, ValueError) as exc:
            raise OutboxDeliveryError(
                "OUTBOX_EVENT_INVALID",
                "local event batch cannot be serialized for delivery",
                retryable=False,
            ) from exc
        headers["Content-Type"] = "application/json"
        try:
            response = await self._client.post(
                self._url,
                content=body,
                headers=headers,
                follow_redirects=False,
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise OutboxDeliveryError(
                "OUTBOX_TRANSPORT_TIMEOUT",
                "event batch request timed out",
                retryable=True,
            ) from exc
        except (httpx.TransportError, ConnectionError) as exc:
            raise OutboxDeliveryError(
                "OUTBOX_TRANSPORT_UNAVAILABLE",
                "event batch transport is unavailable",
                retryable=True,
            ) from exc

        try:
            verify_response(
                headers=dict(response.headers),
                status_code=response.status_code,
                body=response.content,
                expected_request_id=request_id,
                expected_request_nonce=nonce,
                key_resolver=self._key_resolver,
                now_epoch_seconds=self._epoch_clock.now_epoch_seconds(),
            )
        except SignatureError as exc:
            raise OutboxDeliveryError(
                "OUTBOX_RESPONSE_AUTH_FAILED",
                "event batch response authentication failed",
                retryable=False,
            ) from exc

        if response.status_code != 200:
            retryable = response.status_code >= 500 or response.status_code in {408, 425, 429}
            raise OutboxDeliveryError(
                "OUTBOX_HTTP_RETRYABLE" if retryable else "OUTBOX_HTTP_REJECTED",
                "event batch endpoint returned a non-success status",
                retryable=retryable,
            )
        try:
            result = json.loads(response.content)
            if not isinstance(result, Mapping):
                raise TypeError("batch result must be an object")
            validate_event_batch_result_semantics(result, request=request)
            return self._parse_receipts(items, result)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OutboxDeliveryError(
                "OUTBOX_RESPONSE_INVALID",
                "event batch response violates its contract",
                retryable=False,
            ) from exc

    def _build_request(self, items: tuple[OutboxItem, ...], request_id: str) -> dict[str, Any]:
        if not isinstance(items, tuple) or not 1 <= len(items) <= 50:
            raise ValueError("event batch must contain between 1 and 50 items")
        events: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, OutboxItem):
                raise TypeError("event batch items must be OutboxItem values")
            if item.kind is not OutboxKind.DOMAIN_EVENT or item.state is not OutboxState.PENDING:
                raise ValueError("event batch contains a non-pending domain event")
            event = item.document.to_mapping()
            validate_domain_event_semantics(event)
            if (
                event.get("event_id") != item.document.document_id
                or event.get("event_hash") != item.document.content_hash
                or event.get("node_id") != self._node_id
                or event.get("account_id") != self._account_id
            ):
                raise ValueError("event identity or scope does not match the transport")
            events.append(event)
        request: dict[str, Any] = {
            "schema_version": "1.0",
            "request_id": request_id,
            "node_id": self._node_id,
            "account_id": self._account_id,
            "sent_at": self._clock.now().to_wire(),
            "events": events,
        }
        validate_event_batch_semantics(request)
        return request

    @staticmethod
    def _parse_receipts(
        items: tuple[OutboxItem, ...], result: Mapping[str, Any]
    ) -> tuple[OutboxDeliveryReceipt, ...]:
        if set(result) != {
            "schema_version",
            "request_id",
            "received_at",
            "accepted",
            "duplicates",
            "rejected",
            "high_watermark",
            "results",
        }:
            raise ValueError("batch result contains unexpected fields")
        if result.get("schema_version") != "1.0":
            raise ValueError("batch result version is unsupported")
        received_at = result.get("received_at")
        if not isinstance(received_at, str):
            raise ValueError("received_at must be a string")
        ShanghaiTimestamp.parse(received_at)
        raw_receipts = result.get("results")
        if not isinstance(raw_receipts, list):
            raise ValueError("results must be an array")
        by_event: dict[tuple[object, object], OutboxDeliveryReceipt] = {}
        item_by_event = {
            (item.document.document_id, item.document.content_hash): item for item in items
        }
        for raw in raw_receipts:
            if not isinstance(raw, Mapping):
                raise ValueError("event receipt must be an object")
            raw_status = raw.get("status")
            if not isinstance(raw_status, str):
                raise ValueError("receipt status must be a string")
            status = _EventReceiptStatus(raw_status)
            retryable = raw.get("retryable")
            if type(retryable) is not bool:
                raise ValueError("receipt retryable must be a boolean")
            event_id = raw.get("event_id")
            event_hash = raw.get("event_hash")
            if not isinstance(event_id, str) or not isinstance(event_hash, str):
                raise ValueError("receipt identity must contain strings")
            identity = (event_id, event_hash)
            item = item_by_event.get(identity)
            if item is None or identity in by_event:
                raise ValueError("receipt identity is absent or duplicated")
            if status in {_EventReceiptStatus.ACCEPTED, _EventReceiptStatus.DUPLICATE}:
                if retryable or set(raw) != {"event_id", "event_hash", "status", "retryable"}:
                    raise ValueError("accepted receipt contains invalid fields")
                delivery_receipt = OutboxDeliveryReceipt(
                    item.outbox_id,
                    item.document.content_hash,
                    OutboxDeliveryDisposition.ACKNOWLEDGE,
                )
            else:
                if set(raw) != {
                    "event_id",
                    "event_hash",
                    "status",
                    "retryable",
                    "error_code",
                    "error_message",
                }:
                    raise ValueError("rejected receipt contains invalid fields")
                error_code = raw.get("error_code")
                error_message = raw.get("error_message")
                if (
                    not isinstance(error_code, str)
                    or not isinstance(error_message, str)
                    or not (1 <= len(error_message) <= 256)
                ):
                    raise ValueError("rejected receipt error is invalid")
                disposition = (
                    OutboxDeliveryDisposition.RETRY
                    if retryable
                    else OutboxDeliveryDisposition.DEAD_LETTER
                )
                delivery_receipt = OutboxDeliveryReceipt(
                    item.outbox_id,
                    item.document.content_hash,
                    disposition,
                    error_code,
                )
            by_event[identity] = delivery_receipt
        if set(by_event) != set(item_by_event):
            raise ValueError("batch result is incomplete")
        return tuple(
            by_event[(item.document.document_id, item.document.content_hash)] for item in items
        )


def _validate_base_url(value: str) -> httpx.URL:
    if not isinstance(value, str):
        raise ValueError("base_url must be a string")
    try:
        url = httpx.URL(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("base_url is invalid") from exc
    if (
        url.scheme != "https"
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or url.path.rstrip("/") != "/api/v1/trading"
    ):
        raise ValueError("base_url must be an HTTPS /api/v1/trading endpoint")
    return url.copy_with(path=url.path.rstrip("/") + "/")
