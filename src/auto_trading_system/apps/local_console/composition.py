"""Production composition helpers for the local control HTTP boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from auto_trading_system.apps.local_console.app import (
    build_local_console_app,
    build_local_console_uvicorn_config,
)
from auto_trading_system.apps.local_console.security_api import LocalConsoleSecurityController
from auto_trading_system.apps.local_console.server import LocalConsoleServer
from auto_trading_system.config.credentials import RuntimeSecrets
from auto_trading_system.config.models import RuntimeConfig
from auto_trading_system.control import (
    JournalLocalControlReceiptReader,
    LocalControlDecisionHandler,
    LocalControlImpactFacts,
    LocalControlPreviewManager,
    LocalControlReceiptRegistry,
    LocalControlRequest,
    LocalControlSubmissionService,
)
from auto_trading_system.control.service import LocalControlCoordinator
from auto_trading_system.domain import Clock, MonotonicClock
from auto_trading_system.journal.writer import PersistenceWriter
from auto_trading_system.runtime import (
    BoundedEventIngress,
    RuntimeComponent,
    RuntimeHealth,
    RuntimeRecoveryPermit,
)
from auto_trading_system.security import LocalOperatorSessionManager, OperatorSessionPolicy


@dataclass(frozen=True, slots=True)
class LocalControlApiRuntime:
    submission_service: LocalControlSubmissionService
    decision_handler: LocalControlDecisionHandler
    console_security: LocalConsoleSecurityController
    server: LocalConsoleServer
    lifecycle_component: RuntimeComponent


def build_local_control_api_runtime(
    settings: RuntimeConfig,
    persistence_writer: PersistenceWriter,
    coordinator: LocalControlCoordinator,
    ingress: BoundedEventIngress[LocalControlRequest],
    runtime_secrets: RuntimeSecrets,
    *,
    clock: Clock,
    monotonic_clock: MonotonicClock,
    impact_facts_supplier: Callable[[], LocalControlImpactFacts],
    health_supplier: Callable[[], RuntimeHealth | None] = lambda: None,
    permit_supplier: Callable[[], RuntimeRecoveryPermit | None] = lambda: None,
    receipt_capacity: int = 1024,
) -> LocalControlApiRuntime:
    """Bind one registry to HTTP admission, decision handling, Journal, and lifecycle."""
    if not isinstance(settings, RuntimeConfig):
        raise ValueError("settings must be a RuntimeConfig")
    if not isinstance(persistence_writer, PersistenceWriter):
        raise ValueError("persistence_writer must be a PersistenceWriter")
    if not isinstance(coordinator, LocalControlCoordinator):
        raise ValueError("coordinator must be a LocalControlCoordinator")
    if not isinstance(ingress, BoundedEventIngress):
        raise ValueError("ingress must be a BoundedEventIngress")
    if not isinstance(runtime_secrets, RuntimeSecrets):
        raise ValueError("runtime_secrets must be RuntimeSecrets")
    if not callable(getattr(clock, "now", None)):
        raise ValueError("clock must implement Clock")
    if not callable(getattr(monotonic_clock, "monotonic_ns", None)):
        raise ValueError("monotonic_clock must implement MonotonicClock")
    if not callable(impact_facts_supplier):
        raise ValueError("impact_facts_supplier must be callable")
    registry = LocalControlReceiptRegistry(capacity=receipt_capacity)
    reader = JournalLocalControlReceiptReader(
        persistence_writer,
        account_id=settings.node.account_id,
        clock=clock,
    )
    submission = LocalControlSubmissionService(
        reader=reader,
        ingress=ingress,
        registry=registry,
        clock=clock,
    )
    decision_handler = LocalControlDecisionHandler(
        coordinator,
        registry,
        clock=clock,
        health_supplier=health_supplier,
        permit_supplier=permit_supplier,
    )
    session_policy = OperatorSessionPolicy(
        idle_seconds=settings.local_api.session_idle_seconds,
        absolute_seconds=settings.local_api.session_absolute_seconds,
        failure_window_seconds=settings.local_api.login_failure_window_seconds,
        max_failures=settings.local_api.login_max_failures,
    )
    sessions = LocalOperatorSessionManager(
        runtime_secrets.operator_password_verifier,
        actor_id=settings.local_api.operator_id,
        clock=clock,
        monotonic_clock=monotonic_clock,
        policy=session_policy,
    )
    previews = LocalControlPreviewManager(
        node_id=settings.node.node_id,
        account_id=settings.node.account_id,
        account_alias=settings.node.account_alias,
        profile=settings.profile,
        clock=clock,
        monotonic_clock=monotonic_clock,
        facts_supplier=impact_facts_supplier,
        ttl_seconds=settings.local_api.control_preview_ttl_seconds,
    )
    console_security = LocalConsoleSecurityController(
        sessions,
        previews,
        expected_origin=f"http://127.0.0.1:{settings.local_api.port}",
        session_max_age_seconds=settings.local_api.session_absolute_seconds,
    )
    app = build_local_console_app(
        settings,
        control_service=submission,
        console_security=console_security,
    )
    server = LocalConsoleServer(build_local_console_uvicorn_config(settings, app))
    return LocalControlApiRuntime(
        submission_service=submission,
        decision_handler=decision_handler,
        console_security=console_security,
        server=server,
        lifecycle_component=server.runtime_component(),
    )
