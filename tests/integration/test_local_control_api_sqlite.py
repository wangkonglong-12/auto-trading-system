from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from auto_trading_system.apps.local_console import (
    CONTROL_AUTHORIZATION_HEADER_NAME,
    CSRF_HEADER_NAME,
    LocalConsoleSecurityController,
    build_local_console_app,
)
from auto_trading_system.config.models import RuntimeConfig
from auto_trading_system.control import (
    JournalLocalControlReceiptReader,
    LocalControlCoordinator,
    LocalControlDecisionHandler,
    LocalControlImpactFacts,
    LocalControlPreviewManager,
    LocalControlReceiptRegistry,
    LocalControlRequest,
    LocalControlSubmissionService,
)
from auto_trading_system.domain import ShanghaiTimestamp, TradingDate
from auto_trading_system.journal.sqlite import SQLiteJournalSettings, SQLiteLocalJournal
from auto_trading_system.journal.writer import PersistenceWriter
from auto_trading_system.runtime import (
    BoundedEventIngress,
    DecisionEventLoop,
    DispatchKind,
    RuntimeCapability,
    RuntimeDispatch,
    RuntimeHealth,
    RuntimeRecoveryPermit,
    RuntimeState,
    RuntimeStateMachine,
    TransitionAuthority,
)
from auto_trading_system.security import LocalOperatorSessionManager, OperatorPasswordHasher

ACCOUNT_ID = "acct_demo_001"
NOW = ShanghaiTimestamp.parse("2026-09-04T14:40:01+08:00")
OPERATOR_PASSWORD = "Correct-Horse-Battery-2026"
OPERATOR_VERIFIER = (
    "ats-scrypt-v1$32768$8$1$AAECAwQFBgcICQoLDA0ODw$f81LIxrvonq2ysChD17nLqfS0_FxQ1_5qy8BxpPy-n4"
)


@dataclass(frozen=True, slots=True)
class FixedClock:
    value: ShanghaiTimestamp = field(default=NOW)

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


def runtime_settings(tmp_path: Path) -> RuntimeConfig:
    config_path = (
        Path(__file__).resolve().parents[2] / "config" / "v1" / "runtime.read-only.example.json"
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["storage"]["root"] = str((tmp_path / "runtime-data").resolve())
    return RuntimeConfig.model_validate(payload)


def healthy_runtime() -> RuntimeHealth:
    return RuntimeHealth(True, True, True, True, True, False, False, True, True, True)


def full_permit() -> RuntimeRecoveryPermit:
    return RuntimeRecoveryPermit("recon_control_api_0001", True, True)


def ready_auto_machine() -> RuntimeStateMachine:
    machine = RuntimeStateMachine(
        capability=RuntimeCapability.FULL,
        monotonic_clock=IncrementingClock(),
        owner_guard=lambda: None,
    )
    machine.request_transition(
        RuntimeState.CONNECTING,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="START_CONNECTING",
    )
    machine.request_transition(
        RuntimeState.RECONCILING,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="START_RECONCILING",
    )
    machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="START_RECONCILED",
        health=healthy_runtime(),
    )
    machine.request_transition(
        RuntimeState.AUTO,
        authority=TransitionAuthority.RUNTIME,
        reason_code="AUTO_ACTIVATED",
        health=healthy_runtime(),
        permit=full_permit(),
    )
    return machine


def writer_at(database_path: Path) -> PersistenceWriter:
    return PersistenceWriter(
        SQLiteLocalJournal(
            SQLiteJournalSettings(database_path, ACCOUNT_ID, minimum_free_bytes=0),
            clock=FixedClock(),
        ),
        capacity=8,
    )


def security_controller(
    settings: RuntimeConfig,
    facts_supplier: Callable[[], LocalControlImpactFacts],
) -> LocalConsoleSecurityController:
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
        facts_supplier=facts_supplier,
    )
    return LocalConsoleSecurityController(
        sessions,
        previews,
        expected_origin=f"http://127.0.0.1:{settings.local_api.port}",
        session_max_age_seconds=settings.local_api.session_absolute_seconds,
    )


def impact_facts(state: RuntimeState) -> LocalControlImpactFacts:
    return LocalControlImpactFacts(state, 0, 0, 0, 0, True, True, True, False)


async def login(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/local/session",
        json={"password": OPERATOR_PASSWORD},
    )
    assert response.status_code == 200
    client.headers[CSRF_HEADER_NAME] = response.json()["csrf_token"]


async def authorized_halt(
    client: AsyncClient,
) -> tuple[dict[str, object], dict[str, str]]:
    preview_response = await client.post(
        "/api/v1/local/control-previews",
        json={
            "action": "EMERGENCY_HALT",
            "reason_code": "LOCAL_EMERGENCY_HALT",
            "cancel_active_orders": True,
        },
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    confirmation_response = await client.post(
        f"/api/v1/local/control-previews/{preview['preview_id']}/confirm",
        json={"phrase": preview["confirmation_phrase"]},
    )
    assert confirmation_response.status_code == 200
    confirmed = confirmation_response.json()
    body = confirmed["command_template"]
    body.update(
        {
            "command_id": "cmd_api_sqlite_00000001",
            "correlation_id": "corr_api_sqlite_00000001",
        }
    )
    return body, {
        CONTROL_AUTHORIZATION_HEADER_NAME: confirmed["authorization_token"],
    }


async def exercise_control_api_round_trip(database_path: Path, settings: RuntimeConfig) -> None:
    writer = writer_at(database_path)
    await writer.start()
    machine = ready_auto_machine()
    ingress = BoundedEventIngress[LocalControlRequest](
        normal_capacity=4,
        critical_capacity=4,
        monotonic_clock=IncrementingClock(),
    )
    completed = asyncio.Event()
    decision_handler: LocalControlDecisionHandler | None = None

    async def dispatch_handler(dispatch: RuntimeDispatch[LocalControlRequest]) -> None:
        assert dispatch.kind is DispatchKind.EVENT
        assert dispatch.envelope is not None
        assert decision_handler is not None
        try:
            await decision_handler.handle(dispatch.envelope.payload)
        finally:
            completed.set()

    loop = DecisionEventLoop(ingress=ingress, handler=dispatch_handler)
    registry = LocalControlReceiptRegistry(capacity=8)
    coordinator = LocalControlCoordinator(
        writer,
        machine,
        node_id=settings.node.node_id,
        account_id=ACCOUNT_ID,
        boot_id="boot_11111111111111111111111111111111",
        clock=FixedClock(),
        owner_guard=loop.assert_owner,
        event_id_factory=lambda: "evt_55555555555555555555555555555555",
    )
    decision_handler = LocalControlDecisionHandler(
        coordinator,
        registry,
        clock=FixedClock(),
        health_supplier=healthy_runtime,
        permit_supplier=full_permit,
    )
    reader = JournalLocalControlReceiptReader(
        writer,
        account_id=ACCOUNT_ID,
        clock=FixedClock(),
    )
    service = LocalControlSubmissionService(
        reader=reader,
        ingress=ingress,
        registry=registry,
        clock=FixedClock(),
    )
    console_security = security_controller(settings, lambda: impact_facts(machine.state))
    app = build_local_console_app(
        settings,
        control_service=service,
        console_security=console_security,
    )
    loop_task = asyncio.create_task(loop.run())
    await asyncio.sleep(0)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={
                "origin": f"http://127.0.0.1:{settings.local_api.port}",
                "sec-fetch-site": "same-origin",
            },
        ) as client:
            await login(client)
            halt_body, authorization = await authorized_halt(client)
            accepted = await client.post(
                "/api/v1/local/controls",
                json=halt_body,
                headers=authorization,
            )
            assert accepted.status_code == 202
            assert accepted.json()["status"] == "ACCEPTED"
            assert accepted.json()["final"] is False

            await asyncio.wait_for(completed.wait(), timeout=2)
            final = await client.get(accepted.json()["status_url"])
            assert final.status_code == 200
            assert final.json()["status"] == "APPLIED"
            assert final.json()["final"] is True
            assert final.json()["previous_state"] == "AUTO"
            assert final.json()["current_state"] == "HALT"
            assert final.json()["transition_sequence"] is not None
            assert final.json()["poll_after_ms"] is None
            assert machine.state is RuntimeState.HALT

        recovery = await writer.load_recovery(
            account_id=ACCOUNT_ID,
            trade_date=TradingDate.parse("2026-09-04"),
        )
        assert len(recovery.controls) == 2
        outbox = await writer.fetch_outbox(limit=10, available_at=NOW)
        assert len(outbox) == 1
        assert outbox[0].document.to_mapping()["payload"]["command_id"] == halt_body["command_id"]
    finally:
        loop.request_stop()
        await loop_task
        await writer.stop()

    restarted = writer_at(database_path)
    await restarted.start()
    try:
        restarted_service = LocalControlSubmissionService(
            reader=JournalLocalControlReceiptReader(
                restarted,
                account_id=ACCOUNT_ID,
                clock=FixedClock(),
            ),
            ingress=BoundedEventIngress[LocalControlRequest](
                normal_capacity=1,
                critical_capacity=1,
                monotonic_clock=IncrementingClock(),
            ),
            registry=LocalControlReceiptRegistry(capacity=1),
            clock=FixedClock(),
        )
        restarted_security = security_controller(
            settings,
            lambda: impact_facts(RuntimeState.HALT),
        )
        restarted_app = build_local_console_app(
            settings,
            control_service=restarted_service,
            console_security=restarted_security,
        )
        async with AsyncClient(
            transport=ASGITransport(app=restarted_app),
            base_url="http://127.0.0.1",
            headers={
                "origin": f"http://127.0.0.1:{settings.local_api.port}",
                "sec-fetch-site": "same-origin",
            },
        ) as client:
            await login(client)
            receipt = await client.get("/api/v1/local/controls/cmd_api_sqlite_00000001")
        assert receipt.status_code == 200
        assert receipt.json()["status"] == "APPLIED"
        assert receipt.json()["updated_at"] == "2026-09-04T14:40:01+08:00"
    finally:
        await restarted.stop()


def test_local_control_http_receipt_survives_sqlite_restart(tmp_path: Path) -> None:
    settings = runtime_settings(tmp_path)
    asyncio.run(
        exercise_control_api_round_trip(
            (tmp_path / "local-control-api.db").resolve(),
            settings,
        )
    )
