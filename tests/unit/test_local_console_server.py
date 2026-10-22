from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from auto_trading_system.apps.local_console import (
    LocalConsoleServer,
    LocalConsoleServerError,
    LocalConsoleServerStatus,
    build_local_control_api_runtime,
)
from auto_trading_system.config.credentials import RuntimeSecrets
from auto_trading_system.config.models import RuntimeConfig
from auto_trading_system.control import (
    LocalControlCoordinator,
    LocalControlImpactFacts,
    LocalControlRequest,
)
from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.journal.sqlite import SQLiteJournalSettings, SQLiteLocalJournal
from auto_trading_system.journal.writer import PersistenceWriter
from auto_trading_system.runtime import (
    BoundedEventIngress,
    LifecyclePhase,
    RuntimeCapability,
    RuntimeState,
    RuntimeStateMachine,
)

OPERATOR_VERIFIER = (
    "ats-scrypt-v1$32768$8$1$AAECAwQFBgcICQoLDA0ODw$f81LIxrvonq2ysChD17nLqfS0_FxQ1_5qy8BxpPy-n4"
)


@dataclass
class FakeUvicornServer:
    mode: str = "healthy"
    started: bool = False
    should_exit: bool = False
    force_exit: bool = False
    serve_calls: int = 0

    async def serve(self, sockets: list[Any] | None = None) -> None:
        self.serve_calls += 1
        if self.mode == "startup-failure":
            raise RuntimeError("private bind detail")
        if self.mode != "never-start":
            self.started = True
        while not self.should_exit:
            await asyncio.sleep(0)
        if self.mode == "stop-failure":
            raise RuntimeError("private shutdown detail")
        if self.mode == "never-stop":
            while True:
                await asyncio.sleep(0)


@dataclass
class IncrementingClock:
    value: int = 0

    def monotonic_ns(self) -> int:
        self.value += 1
        return self.value


@dataclass(frozen=True, slots=True)
class FixedClock:
    value: ShanghaiTimestamp = field(
        default_factory=lambda: ShanghaiTimestamp.parse("2026-09-04T10:00:00+08:00")
    )

    def now(self) -> ShanghaiTimestamp:
        return self.value


def uvicorn_config() -> uvicorn.Config:
    return uvicorn.Config(FastAPI(), host="127.0.0.1", port=0, access_log=False)


def runtime_settings(tmp_path: Path) -> RuntimeConfig:
    config_path = (
        Path(__file__).resolve().parents[2] / "config" / "v1" / "runtime.read-only.example.json"
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["storage"]["root"] = str((tmp_path / "runtime-data").resolve())
    return RuntimeConfig.model_validate(payload)


def test_local_console_server_starts_stops_and_exposes_control_api_lifecycle_phase() -> None:
    fake = FakeUvicornServer()
    server = LocalConsoleServer(uvicorn_config(), server_factory=lambda _: fake)

    async def exercise() -> None:
        await server.start()
        assert server.status is LocalConsoleServerStatus.RUNNING
        component = server.runtime_component()
        assert component.name == "local-control-api"
        assert component.phase is LifecyclePhase.CONTROL_API
        await server.stop()
        await server.stop()

    asyncio.run(exercise())
    assert server.status is LocalConsoleServerStatus.STOPPED
    assert fake.serve_calls == 1
    assert fake.should_exit is True


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("startup-failure", "LOCAL_CONSOLE_START_FAILED"),
        ("never-start", "LOCAL_CONSOLE_START_FAILED"),
    ],
)
def test_local_console_server_maps_start_failures_without_internal_details(
    mode: str, code: str
) -> None:
    fake = FakeUvicornServer(mode=mode)
    server = LocalConsoleServer(
        uvicorn_config(),
        startup_timeout_seconds=0.1,
        server_factory=lambda _: fake,
    )

    with pytest.raises(LocalConsoleServerError) as captured:
        asyncio.run(server.start())

    assert captured.value.code == code
    assert "private bind detail" not in str(captured.value)
    assert server.status is LocalConsoleServerStatus.FAILED


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("stop-failure", "LOCAL_CONSOLE_STOP_FAILED"),
        ("never-stop", "LOCAL_CONSOLE_STOP_TIMEOUT"),
    ],
)
def test_local_console_server_maps_stop_failures_and_forces_task_cleanup(
    mode: str, code: str
) -> None:
    fake = FakeUvicornServer(mode=mode)
    server = LocalConsoleServer(
        uvicorn_config(),
        shutdown_timeout_seconds=0.1,
        server_factory=lambda _: fake,
    )

    async def exercise() -> None:
        await server.start()
        with pytest.raises(LocalConsoleServerError) as captured:
            await server.stop()
        assert captured.value.code == code
        assert "private shutdown detail" not in str(captured.value)

    asyncio.run(exercise())
    assert server.status is LocalConsoleServerStatus.FAILED


def test_created_console_server_can_stop_without_starting_and_cannot_restart() -> None:
    server = LocalConsoleServer(uvicorn_config(), server_factory=lambda _: FakeUvicornServer())

    asyncio.run(server.stop())

    assert server.status is LocalConsoleServerStatus.STOPPED
    with pytest.raises(LocalConsoleServerError, match="LOCAL_CONSOLE_NOT_STARTABLE"):
        asyncio.run(server.start())


@pytest.mark.parametrize("value", [0.0, 0.09, 301.0, True])
def test_local_console_server_rejects_invalid_timeouts(value: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        LocalConsoleServer(uvicorn_config(), startup_timeout_seconds=value)


def test_control_api_composition_uses_one_registry_and_control_lifecycle_phase(
    tmp_path: Path,
) -> None:
    settings = runtime_settings(tmp_path)
    clock = FixedClock()
    writer = PersistenceWriter(
        SQLiteLocalJournal(
            SQLiteJournalSettings(
                (tmp_path / "composition.db").resolve(), settings.node.account_id
            ),
            clock=clock,
        )
    )
    machine = RuntimeStateMachine(
        capability=RuntimeCapability.OBSERVE_ONLY,
        monotonic_clock=IncrementingClock(),
        owner_guard=lambda: None,
    )
    coordinator = LocalControlCoordinator(
        writer,
        machine,
        node_id=settings.node.node_id,
        account_id=settings.node.account_id,
        boot_id="boot_11111111111111111111111111111111",
        clock=clock,
        owner_guard=lambda: None,
    )
    ingress = BoundedEventIngress[LocalControlRequest](
        normal_capacity=4,
        critical_capacity=4,
        monotonic_clock=IncrementingClock(),
    )

    runtime = build_local_control_api_runtime(
        settings,
        writer,
        coordinator,
        ingress,
        RuntimeSecrets(
            node_hmac_secret=SecretStr("h" * 32),
            broker_account_id=SecretStr("A123456"),
            operator_password_verifier=SecretStr(OPERATOR_VERIFIER),
        ),
        clock=clock,
        monotonic_clock=IncrementingClock(),
        impact_facts_supplier=lambda: LocalControlImpactFacts(
            RuntimeState.BOOTING,
            0,
            0,
            0,
            0,
            False,
            False,
            False,
            False,
        ),
        receipt_capacity=16,
    )

    assert runtime.lifecycle_component.phase is LifecyclePhase.CONTROL_API
    assert runtime.lifecycle_component.name == "local-control-api"
    assert runtime.server.status is LocalConsoleServerStatus.CREATED
    assert runtime.submission_service is not None
    assert runtime.decision_handler is not None
    assert runtime.console_security is not None
