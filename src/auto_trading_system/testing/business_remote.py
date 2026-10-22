"""Signed in-memory HTTP peer for the offline business-flow acceptance only.

It uses the real client/authentication/DTO paths, but provides no socket, TLS
server, production credentials or MySQL durability. These remain separate tests.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock

import httpx

from auto_trading_system.protocol.v2.api import (
    CompleteDaily,
    DailyProgress,
    PlanLookup,
    PlanReceipt,
)
from auto_trading_system.protocol.v2.authentication import (
    parse_identity,
    security_headers,
    signed_response_headers,
    verify_request,
)
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.daily_sync import DailyManifest
from auto_trading_system.protocol.v2.plans import PlanEnvelope
from auto_trading_system.sync.daily_upload import PermanentSyncError
from auto_trading_system.sync.http_v2 import NodeCredentials
from auto_trading_system.testing.daily_receiver import InMemoryDailyReceiver


class BusinessRemote:
    def __init__(
        self, plan: PlanEnvelope, credentials: NodeCredentials, clock: Callable[[], float]
    ) -> None:
        self.plan, self.credentials, self.clock = plan, credentials, clock
        self.receiver = InMemoryDailyReceiver()
        self.acks: dict[str, PlanReceipt] = {}
        self.manifests: dict[str, DailyManifest] = {}
        self.requests = 0
        self.drop_complete_once = False
        self.offline = False
        self._nonces: set[str] = set()
        self._lock = RLock()

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            if self.offline:
                raise httpx.ConnectError("OFFLINE_TEST_NETWORK", request=request)
            headers = security_headers(request.headers.raw)
            identity = parse_identity(headers, request.content)
            if (identity.node_id, identity.key_id) != (
                self.credentials.node_id,
                self.credentials.key_id,
            ):
                raise ValueError("simulation node identity mismatch")
            verify_request(
                self.credentials.secret,
                headers["x-ats-signature"],
                method=request.method,
                target=request.url.raw_path.decode(),
                identity=identity,
                body=request.content,
                now_utc_seconds=int(self.clock()),
            )
            if identity.nonce in self._nonces or len(self._nonces) >= 4096:
                raise ValueError("simulation nonce repeated or capacity exceeded")
            self._nonces.add(identity.nonce)
            self.requests += 1
            try:
                status, model = self._route(request)
            except (ValueError, KeyError, PermanentSyncError):
                status, model = 409, None
            body = canonical_bytes(model.model_dump(mode="json")) if model else b""
            signed = signed_response_headers(
                self.credentials.secret,
                key_id=identity.key_id,
                request_id=identity.request_id,
                status=status,
                timestamp=int(self.clock()),
                body=body,
            )
            return httpx.Response(status, content=body, headers=signed)

    def _route(self, request: httpx.Request) -> tuple[int, WireModel | None]:
        path, method = request.url.path, request.method
        if path == "/api/v2/trading/plans/current" and method == "GET":
            p = self.plan.payload
            if dict(request.url.params) != {
                "account_id": p.account_id,
                "strategy_id": p.strategy_id,
                "trade_date": p.trade_date.isoformat(),
            }:
                return 204, None
            return 200, PlanLookup(
                account_id=p.account_id,
                strategy_id=p.strategy_id,
                trade_date=p.trade_date,
                plan_id=p.plan_id,
                state="PUBLISHED",
                state_version=1,
                etag=digest(canonical_bytes(self.plan.model_dump(mode="json"))),
                plan=self.plan,
            )
        if (
            path == f"/api/v2/trading/plans/{self.plan.payload.plan_id}/receipts"
            and method == "POST"
        ):
            receipt = PlanReceipt.model_validate_json(request.content)
            if (receipt.plan_id, receipt.payload_hash, receipt.node_id, receipt.account_id) != (
                self.plan.payload.plan_id,
                self.plan.envelope.payload_hash,
                self.credentials.node_id,
                self.plan.payload.account_id,
            ):
                raise ValueError("simulation receipt scope mismatch")
            if self.acks.setdefault(receipt.receipt_id, receipt) != receipt:
                raise ValueError("simulation receipt conflict")
            return 200, receipt
        root = "/api/v2/trading/daily-syncs"
        if path == root and method == "POST":
            manifest = DailyManifest.model_validate_json(request.content)
            if (manifest.account_id, manifest.node_id, manifest.node_generation) != (
                self.plan.payload.account_id,
                self.credentials.node_id,
                self.plan.payload.target_node_generation,
            ):
                raise ValueError("simulation batch scope mismatch")
            self.receiver.create(manifest)
            self.manifests[manifest.batch_id] = manifest
            return 201, None
        parts = path.removeprefix(root + "/").split("/")
        if not path.startswith(root + "/"):
            return 404, None
        key = parts[0]
        if len(parts) == 2 and parts[1] == "receipt" and method == "GET":
            result = self.receiver.receipt(key)
            return (200, result) if result else (404, None)
        if len(parts) == 1 and method == "GET":
            missing = self.receiver.missing_parts(key)
            return 200, DailyProgress(
                batch_id=key,
                manifest_hash=self.manifests[key].manifest_hash,
                state="RECEIVING" if missing else "RECEIVED",
                missing_parts=missing,
            )
        if len(parts) == 3 and parts[1] == "parts" and method == "PUT":
            self.receiver.put_part(key, parts[2], request.content)
            return 200, None
        if len(parts) == 2 and parts[1] == "complete" and method == "POST":
            self.receiver.complete(
                key, CompleteDaily.model_validate_json(request.content).manifest_hash
            )
            if self.drop_complete_once:
                self.drop_complete_once = False
                raise httpx.ReadTimeout("OFFLINE_TEST_ACK_LOSS", request=request)
            return 200, None
        return 404, None
