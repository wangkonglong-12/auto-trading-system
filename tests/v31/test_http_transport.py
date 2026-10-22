from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from test_v2_files_sync import sample_batch

from auto_trading_system.protocol.v2.api import (
    CapabilityManifest,
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
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.daily_sync import AppliedReceipt
from auto_trading_system.protocol.v2.plans import PlanEnvelope
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.daily_upload import (
    PermanentSyncError,
    RetryableSyncError,
    upload_once,
)
from auto_trading_system.sync.http_v2 import (
    HttpQuantTransport,
    NodeCredentials,
    RemoteBackoff,
    SignedHttpClient,
)

SECRET = b"public-transport-test-secret-0001"
EPOCH = int(datetime(2026, 9, 7, 7, 30, tzinfo=UTC).timestamp())


def signed_response(
    request: httpx.Request,
    status: int = 200,
    model: WireModel | None = None,
    *,
    raw: bytes | None = None,
) -> httpx.Response:
    identity = parse_identity(security_headers(request.headers.raw), request.content)
    verify_request(
        SECRET,
        request.headers["x-ats-signature"],
        method=request.method,
        target=request.url.raw_path.decode(),
        identity=identity,
        body=request.content,
        now_utc_seconds=EPOCH,
    )
    body = (
        raw
        if raw is not None
        else (canonical_bytes(model.model_dump(mode="json")) if model else b"")
    )
    headers = signed_response_headers(
        SECRET,
        key_id="test-key",
        request_id=identity.request_id,
        status=status,
        timestamp=EPOCH,
        body=body,
    )
    return httpx.Response(status, content=body, headers=headers)


def make_client(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: object
) -> SignedHttpClient:
    # Extra timing hooks are provided by specialized tests directly when needed.
    assert not kwargs
    return SignedHttpClient(
        "https://quant.test",
        NodeCredentials("test-node", "test-key", SECRET),
        clock=lambda: EPOCH,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://quant.test",
        "https://user:secret@quant.test",
        "https://quant.test/api",
        "https://quant.test?x=1",
        "https://quant.test#frag",
    ],
)
def test_tls_origin_is_mandatory(url: str) -> None:
    with pytest.raises(ValueError):
        SignedHttpClient(url, NodeCredentials("test-node", "test-key", SECRET))


@pytest.mark.parametrize("status", [400, 401, 403, 409, 413, 302])
def test_permanent_errors_and_redirects_stop_the_job(status: int) -> None:
    client = make_client(lambda request: signed_response(request, status))
    try:
        with pytest.raises(PermanentSyncError):
            client.request("GET", "/api/v2/trading/manifest")
    finally:
        client.close()


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_remote_failure_preserves_retryability(status: int) -> None:
    client = make_client(lambda request: signed_response(request, status))
    try:
        with pytest.raises(RetryableSyncError):
            client.request("GET", "/api/v2/trading/manifest")
    finally:
        client.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("X-ATS-Signature", "0" * 64),
        ("X-ATS-Key-Id", "wrong-key"),
        ("X-ATS-Request-Id", "wrong-request"),
        ("X-ATS-Timestamp", str(EPOCH - 31)),
        ("X-ATS-Content-SHA256", "sha256:" + "0" * 64),
    ],
)
def test_untrusted_response_never_becomes_success(field: str, value: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = signed_response(request)
        response.headers[field] = value
        return response

    client = make_client(handler)
    try:
        with pytest.raises(PermanentSyncError, match="UNTRUSTED"):
            client.request("GET", "/api/v2/trading/manifest")
    finally:
        client.close()


def test_retry_identity_and_resource_limits() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return signed_response(request)

    client = make_client(handler)
    try:
        for _ in range(2):
            client.request(
                "POST", "/api/v2/trading/daily-syncs", b"{}", request_id="stable-request"
            )
        assert requests[0].headers["x-ats-nonce"] != requests[1].headers["x-ats-nonce"]
        assert all(item.headers["x-ats-request-id"] == "stable-request" for item in requests)
        with pytest.raises(PermanentSyncError, match="TOO_LARGE"):
            client.request("POST", "/api/v2/trading/daily-syncs", b"x" * (512 * 1024 + 1))
        with pytest.raises(ValueError, match="override"):
            client.request(
                "GET", "/api/v2/trading/manifest", extra_headers={"X-ATS-Key-Id": "wrong"}
            )
        assert client._slot.acquire(blocking=False)
        try:
            with pytest.raises(RemoteBackoff):
                client.request("GET", "/api/v2/trading/manifest")
        finally:
            client._slot.release()
        assert len(requests) == 2
    finally:
        client.close()


@pytest.mark.parametrize(
    "failure", ["timeout", "oversize", "compression", "unsigned", "proxy_busy"]
)
def test_streaming_transport_faults(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("test", request=request)
        if failure == "oversize":
            return signed_response(request, raw=b"x" * (512 * 1024 + 1))
        if failure == "compression":
            return httpx.Response(200, headers={"Content-Encoding": "unsupported"})
        if failure == "proxy_busy":
            return httpx.Response(503)
        return httpx.Response(200)

    client = make_client(handler)
    error = RetryableSyncError if failure in ("timeout", "proxy_busy") else PermanentSyncError
    try:
        with pytest.raises(error):
            client.request("GET", "/api/v2/trading/manifest")
    finally:
        client.close()


def test_typed_endpoints_and_daily_upload(tmp_path: Path) -> None:
    files = ArtifactFiles(tmp_path / "files")
    batch = sample_batch(files, count=1)
    path = Path(__file__).resolve().parents[2] / "contracts/v2/examples/signed-plan.json"
    plan = PlanEnvelope.model_validate_json(path.read_bytes())
    completed = False
    uploaded: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal completed
        path = request.url.path
        if path.endswith("/manifest"):
            return signed_response(
                request,
                model=CapabilityManifest(
                    manifest_version="v2.0",
                    generated_at=datetime.fromtimestamp(EPOCH, UTC),
                    capabilities=("daily-sync",),
                    trusted_plan_key_ids=(),
                    rulebook_versions=(),
                ),
            )
        if path.endswith("/plans/current"):
            if "if-none-match" in request.headers:
                return signed_response(request, 304)
            return signed_response(
                request,
                model=PlanLookup(
                    account_id=plan.payload.account_id,
                    strategy_id=plan.payload.strategy_id,
                    trade_date=plan.payload.trade_date,
                    plan_id=plan.payload.plan_id,
                    state="PUBLISHED",
                    state_version=1,
                    etag="sha256:" + "a" * 64,
                    plan=plan,
                ),
            )
        if "/plans/" in path:
            if path.endswith("/receipts"):
                return signed_response(
                    request, model=PlanReceipt.model_validate_json(request.content)
                )
            return signed_response(request, model=plan)
        if path.endswith("/receipt"):
            if not completed:
                return signed_response(request, 404)
            return signed_response(
                request,
                model=AppliedReceipt(
                    batch_id=batch.manifest.batch_id,
                    account_id=batch.manifest.account_id,
                    trade_date=batch.manifest.trade_date,
                    revision=1,
                    manifest_hash=batch.manifest.manifest_hash,
                    business_status="COMPLETE",
                    financial_status="PROVISIONAL",
                    applied_at=datetime.fromtimestamp(EPOCH, UTC),
                ),
            )
        if path.endswith("/complete"):
            completed = True
            return signed_response(request)
        if "/parts/" in path:
            uploaded.append(path.rsplit("/", 1)[1])
            assert request.extensions["timeout"]["read"] == 15
            return signed_response(request)
        if request.method == "POST":
            return signed_response(request, 201, model=batch.manifest)
        return signed_response(
            request,
            model=DailyProgress(
                batch_id=batch.manifest.batch_id,
                manifest_hash=batch.manifest.manifest_hash,
                state="CREATED",
                missing_parts=tuple(part.part_id for part in batch.manifest.parts),
            ),
        )

    client = make_client(handler)
    remote = HttpQuantTransport(client)
    try:
        assert remote.manifest().schema_major == 2
        lookup = remote.current_plan(
            plan.payload.account_id, plan.payload.strategy_id, plan.payload.trade_date
        )
        assert lookup and lookup.plan == plan
        assert (
            remote.current_plan(
                plan.payload.account_id,
                plan.payload.strategy_id,
                plan.payload.trade_date,
                lookup.etag,
            )
            is None
        )
        assert remote.plan(plan.payload.plan_id) == plan
        remote.plan_receipt(
            PlanReceipt(
                receipt_id="receipt-1",
                plan_id=plan.payload.plan_id,
                payload_hash=plan.envelope.payload_hash,
                node_id="test-node",
                node_generation=1,
                account_id=plan.payload.account_id,
                engine_boot_id="boot-1",
                status="LOADED",
                reason_code="VALIDATED",
                observed_at=datetime.fromtimestamp(EPOCH, UTC),
            )
        )
        assert upload_once(batch, files, remote).status == "SYNCED"
        assert len(uploaded) == 1
        assert upload_once(batch, files, remote).status == "SYNCED"
        assert len(uploaded) == 1
    finally:
        client.close()
