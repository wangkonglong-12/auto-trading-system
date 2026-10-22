from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from auto_trading_system.apps.local_console import (
    CONTROL_AUTHORIZATION_HEADER_NAME,
    CSRF_HEADER_NAME,
    LocalConsoleSecurityController,
    build_local_console_app,
)
from auto_trading_system.config.models import RuntimeConfig
from auto_trading_system.control import (
    LocalControlImpactFacts,
    LocalControlPreviewManager,
    LocalControlReceiptRegistry,
    LocalControlRequest,
    LocalControlSubmissionService,
    PersistedLocalControlReceipt,
)
from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.runtime import (
    EventPriority,
    PublishOutcome,
    RuntimeEventSource,
    RuntimeState,
)
from auto_trading_system.security import (
    LocalOperatorSessionManager,
    OperatorPasswordHasher,
)

OPERATOR_PASSWORD = "Correct-Horse-Battery-2026"
OPERATOR_VERIFIER = (
    "ats-scrypt-v1$32768$8$1$AAECAwQFBgcICQoLDA0ODw$f81LIxrvonq2ysChD17nLqfS0_FxQ1_5qy8BxpPy-n4"
)


@dataclass(frozen=True, slots=True)
class FixedClock:
    value: ShanghaiTimestamp = field(
        default_factory=lambda: ShanghaiTimestamp.parse("2026-09-04T10:00:02+08:00")
    )

    def now(self) -> ShanghaiTimestamp:
        return self.value


@dataclass
class IncrementingClock:
    value: int = 0

    def monotonic_ns(self) -> int:
        self.value += 1
        return self.value


class StubPasswordHasher(OperatorPasswordHasher):
    def verify(self, password: str, verifier: str) -> bool:
        return password == OPERATOR_PASSWORD and verifier == OPERATOR_VERIFIER


@dataclass
class StubReader:
    receipt: PersistedLocalControlReceipt | None = None
    failure: BaseException | None = None

    async def find(self, command_id: str) -> PersistedLocalControlReceipt | None:
        if self.failure is not None:
            raise self.failure
        return self.receipt


@dataclass
class StubIngress:
    outcome: PublishOutcome = PublishOutcome.ACCEPTED
    calls: list[LocalControlRequest] = field(default_factory=list)

    def publish(
        self,
        payload: LocalControlRequest,
        *,
        source: RuntimeEventSource,
        priority: EventPriority = EventPriority.NORMAL,
    ) -> PublishOutcome:
        assert source is RuntimeEventSource.OPERATOR
        assert priority is EventPriority.CRITICAL
        self.calls.append(payload)
        return self.outcome


def runtime_settings() -> RuntimeConfig:
    path = Path(__file__).resolve().parents[2] / "config" / "v1" / "runtime.read-only.example.json"
    return RuntimeConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


def asset_root(tmp_path: Path) -> Path:
    root = tmp_path / "console-assets"
    root.mkdir()
    (root / "index.html").write_text("<html><body>console</body></html>", encoding="utf-8")
    return root.resolve()


def command_body() -> dict[str, object]:
    return {
        "command_id": "cmd_api_control_00000001",
        "action": "PAUSE_BUY",
        "actor_id": "operator_demo",
        "reason_code": "OPERATOR_REQUEST",
        "requested_at": "2026-09-04T10:00:00+08:00",
        "expected_state": "AUTO",
        "correlation_id": "corr_api_control_00000001",
        "confirmation": None,
        "cancel_active_orders": None,
    }


def security_controller(settings: RuntimeConfig) -> LocalConsoleSecurityController:
    monotonic = IncrementingClock()
    sessions = LocalOperatorSessionManager(
        SecretStr(OPERATOR_VERIFIER),
        actor_id=settings.local_api.operator_id,
        clock=FixedClock(),
        monotonic_clock=monotonic,
        password_hasher=StubPasswordHasher(),
    )
    previews = LocalControlPreviewManager(
        node_id=settings.node.node_id,
        account_id=settings.node.account_id,
        account_alias=settings.node.account_alias,
        profile=settings.profile,
        clock=FixedClock(),
        monotonic_clock=monotonic,
        facts_supplier=lambda: LocalControlImpactFacts(
            RuntimeState.AUTO,
            0,
            0,
            0,
            0,
            True,
            True,
            True,
            False,
        ),
    )
    return LocalConsoleSecurityController(
        sessions,
        previews,
        expected_origin=f"http://127.0.0.1:{settings.local_api.port}",
        session_max_age_seconds=settings.local_api.session_absolute_seconds,
    )


def authorized_command(
    client: TestClient,
    *,
    command_id: str = "cmd_api_control_00000001",
    reason_code: str = "OPERATOR_REQUEST",
) -> tuple[dict[str, object], dict[str, str]]:
    preview_response = client.post(
        "/api/v1/local/control-previews",
        json={"action": "PAUSE_BUY", "reason_code": reason_code},
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    confirmation_response = client.post(
        f"/api/v1/local/control-previews/{preview['preview_id']}/confirm",
        json={"phrase": preview["confirmation_phrase"]},
    )
    assert confirmation_response.status_code == 200
    confirmed = confirmation_response.json()
    body = confirmed["command_template"]
    body.update(
        {
            "command_id": command_id,
            "correlation_id": "corr_api_control_00000001",
        }
    )
    return body, {
        CONTROL_AUTHORIZATION_HEADER_NAME: confirmed["authorization_token"],
    }


def api_client(
    tmp_path: Path,
    *,
    reader: StubReader | None = None,
    ingress: StubIngress | None = None,
) -> tuple[TestClient, StubIngress]:
    effective_ingress = ingress or StubIngress()
    service = LocalControlSubmissionService(
        reader=reader or StubReader(),
        ingress=effective_ingress,
        registry=LocalControlReceiptRegistry(capacity=4),
        clock=FixedClock(),
    )
    settings = runtime_settings()
    app = build_local_console_app(
        settings,
        asset_root=asset_root(tmp_path),
        control_service=service,
        console_security=security_controller(settings),
    )
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={
            "origin": f"http://127.0.0.1:{settings.local_api.port}",
            "sec-fetch-site": "same-origin",
        },
    )
    login = client.post("/api/v1/local/session", json={"password": OPERATOR_PASSWORD})
    assert login.status_code == 200
    client.headers[CSRF_HEADER_NAME] = login.json()["csrf_token"]
    return client, effective_ingress


def test_control_post_returns_accepted_receipt_and_separate_status_resource(
    tmp_path: Path,
) -> None:
    client, ingress = api_client(tmp_path)
    body, authorization = authorized_command(client)

    accepted = client.post(
        "/api/v1/local/controls",
        json=body,
        headers=authorization,
    )

    assert accepted.status_code == 202
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.json() == {
        "schema_version": "1.0",
        "command_id": "cmd_api_control_00000001",
        "action": "PAUSE_BUY",
        "actor_id": "operator_demo",
        "status": "ACCEPTED",
        "final": False,
        "requested_at": "2026-09-04T10:00:02+08:00",
        "updated_at": "2026-09-04T10:00:02+08:00",
        "expected_state": "AUTO",
        "requested_state": "PAUSE_BUY",
        "previous_state": None,
        "current_state": None,
        "transition_sequence": None,
        "rejection_code": None,
        "blockers": [],
        "error_code": None,
        "retryable": False,
        "requires_reconciliation": False,
        "status_url": "/api/v1/local/controls/cmd_api_control_00000001",
        "poll_after_ms": 250,
    }
    assert len(ingress.calls) == 1

    receipt = client.get(accepted.json()["status_url"])
    assert receipt.status_code == 200
    assert receipt.json()["status"] == "ACCEPTED"
    assert receipt.json()["final"] is False


def test_control_api_enforces_cross_field_domain_validation(tmp_path: Path) -> None:
    client, ingress = api_client(tmp_path)
    invalid = command_body()
    invalid.update(
        {
            "action": "EMERGENCY_HALT",
            "confirmation": None,
            "cancel_active_orders": None,
        }
    )

    response = client.post("/api/v1/local/controls", json=invalid)

    assert response.status_code == 422
    assert ingress.calls == []


def test_control_api_rejects_extra_fields_and_untrusted_host(tmp_path: Path) -> None:
    client, ingress = api_client(tmp_path)
    body = command_body()
    body["unexpected"] = "value"

    assert client.post("/api/v1/local/controls", json=body).status_code == 422
    assert (
        client.post(
            "/api/v1/local/controls",
            json=command_body(),
            headers={"host": "remote.example"},
        ).status_code
        == 400
    )
    assert ingress.calls == []


def test_control_api_maps_idempotency_conflict_to_http_409(tmp_path: Path) -> None:
    client, ingress = api_client(tmp_path)
    original, first_authorization = authorized_command(client)
    assert (
        client.post(
            "/api/v1/local/controls",
            json=original,
            headers=first_authorization,
        ).status_code
        == 202
    )
    changed, second_authorization = authorized_command(client, reason_code="CHANGED_REASON")

    conflict = client.post(
        "/api/v1/local/controls",
        json=changed,
        headers=second_authorization,
    )

    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "LOCAL_CONTROL_IDEMPOTENCY_CONFLICT"
    assert len(ingress.calls) == 1


def test_control_api_maps_queue_pressure_and_audit_failure_to_503(tmp_path: Path) -> None:
    full_client, _ = api_client(tmp_path, ingress=StubIngress(PublishOutcome.OVERFLOW))
    body, authorization = authorized_command(full_client)
    full = full_client.post(
        "/api/v1/local/controls",
        json=body,
        headers=authorization,
    )
    assert full.status_code == 503
    assert full.headers["retry-after"] == "1"
    assert full.json()["detail"]["code"] == "LOCAL_CONTROL_EVENT_QUEUE_FULL"

    second_root = tmp_path / "second"
    second_root.mkdir()
    failed_client, _ = api_client(
        second_root,
        reader=StubReader(failure=RuntimeError("private database detail")),
    )
    failed = failed_client.get("/api/v1/local/controls/cmd_api_control_00000001")
    assert failed.status_code == 503
    assert failed.json()["detail"] == {
        "code": "LOCAL_CONTROL_AUDIT_UNAVAILABLE",
        "message": "durable local-control audit could not be read",
    }
    assert "private database detail" not in failed.text


def test_control_api_returns_404_for_unknown_well_formed_command(tmp_path: Path) -> None:
    client, _ = api_client(tmp_path)

    response = client.get("/api/v1/local/controls/cmd_api_missing_00000001")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "LOCAL_CONTROL_RECEIPT_NOT_FOUND"
