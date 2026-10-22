from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr

from auto_trading_system.apps.trade_runtime.bootstrap import (
    build_central_http_client,
    build_diagnostic_bundle_writer,
    build_local_control_coordinator,
    build_outbox_worker,
    build_persistence_writer,
    build_runtime_redactor,
    build_signed_event_transport,
)
from auto_trading_system.config import RuntimeConfig, RuntimeSecrets
from auto_trading_system.control import LocalControlCoordinator
from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.runtime import RuntimeCapability, RuntimeStateMachine
from auto_trading_system.sync import DurableOutboxWorker, SignedEventBatchHttpTransport


@dataclass(frozen=True, slots=True)
class FixedClock:
    value: ShanghaiTimestamp = field(
        default_factory=lambda: ShanghaiTimestamp.parse("2026-09-04T10:00:00+08:00")
    )

    def now(self) -> ShanghaiTimestamp:
        return self.value


class FixedEpochClock:
    def now_epoch_seconds(self) -> int:
        return 1_788_508_800


class FixedMonotonicClock:
    def monotonic_ns(self) -> int:
        return 1


def runtime_settings(tmp_path: Path) -> RuntimeConfig:
    example_path = Path(__file__).parents[2] / "config" / "v1" / "runtime.read-only.example.json"
    payload = json.loads(example_path.read_text(encoding="utf-8"))
    payload["storage"]["root"] = str(tmp_path.resolve())
    return RuntimeConfig.model_validate(payload)


def test_sync_bootstrap_binds_validated_config_and_redacts_credentials(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = runtime_settings(tmp_path)
        secrets = RuntimeSecrets(
            node_hmac_secret=SecretStr("bootstrap-secret-with-more-than-32-bytes"),
            broker_account_id=SecretStr("A123456"),
            operator_password_verifier=SecretStr(
                "ats-scrypt-v1$32768$8$1$AAECAwQFBgcICQoLDA0ODw$"
                "f81LIxrvonq2ysChD17nLqfS0_FxQ1_5qy8BxpPy-n4"
            ),
        )
        client = build_central_http_client(settings)
        try:
            assert client.follow_redirects is False
            assert client.timeout.connect == 3.0
            assert client.timeout.read == 10.0
            event_transport = build_signed_event_transport(
                settings,
                secrets,
                client,
                clock=FixedClock(),
                epoch_clock=FixedEpochClock(),
            )
            assert isinstance(event_transport, SignedEventBatchHttpTransport)
            assert "bootstrap-secret" not in repr(event_transport)
            redactor = build_runtime_redactor(secrets)
            assert redactor.redact_text("bootstrap-secret-with-more-than-32-bytes") == (
                "[REDACTED]"
            )
            diagnostics = build_diagnostic_bundle_writer(secrets)
            assert "bootstrap-secret" not in repr(diagnostics)
            assert "ats-scrypt-v1" not in repr(redactor)

            writer = build_persistence_writer(settings)
            state_machine = RuntimeStateMachine(
                capability=RuntimeCapability.FULL,
                monotonic_clock=FixedMonotonicClock(),
                owner_guard=lambda: None,
            )
            control = build_local_control_coordinator(
                settings,
                writer,
                state_machine,
                boot_id="boot_11111111111111111111111111111111",
                clock=FixedClock(),
                owner_guard=lambda: None,
            )
            assert isinstance(control, LocalControlCoordinator)
            outbox_worker = build_outbox_worker(
                settings,
                writer,
                event_transport,
                clock=FixedClock(),
                pressure_sink=lambda _: None,
            )
            assert isinstance(outbox_worker, DurableOutboxWorker)
        finally:
            await client.aclose()

    asyncio.run(scenario())
