"""Runtime composition helpers applied before any trading infrastructure starts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal

import httpx

from auto_trading_system.config import (
    EnvironmentProfile,
    LoadedConfiguration,
    RuntimeConfig,
    RuntimeSecrets,
)
from auto_trading_system.control import LocalControlCoordinator
from auto_trading_system.domain import Clock, Money, Ratio
from auto_trading_system.execution import (
    CompositeOrderExecutionGuard,
    HardSafetyOrderExecutionGuard,
    OrderIntentPersistenceGate,
    RuntimeOrderExecutionGuard,
)
from auto_trading_system.gateways.broker import BrokerGateway
from auto_trading_system.journal.sqlite import SQLiteJournalSettings, SQLiteLocalJournal
from auto_trading_system.journal.writer import PersistenceWriter
from auto_trading_system.monitoring import DiagnosticBundleWriter, SensitiveValueRedactor
from auto_trading_system.risk import HardSafetyEvaluator, HardSafetyFacts, HardSafetyLimits
from auto_trading_system.runtime import (
    AccountInstanceGuard,
    DailySchedule,
    EventPriority,
    ExternalHaltMarker,
    ExternalHaltMarkerStore,
    InstanceLockBackend,
    IntervalSchedule,
    RuntimeCapability,
    RuntimeHeartbeatStore,
    RuntimeState,
    RuntimeStateMachine,
    RuntimeWatchdogBridge,
    ScheduledJobDefinition,
    ScheduledTask,
    WindowsNamedMutexBackend,
    enforce_external_halt_at_startup,
)
from auto_trading_system.runtime.state_machine import TransitionDecision
from auto_trading_system.security.storage import DirectoryAclHardener, initialize_secure_storage
from auto_trading_system.sync import (
    DurableOutboxWorker,
    EpochClock,
    OutboxDispatcher,
    OutboxPressureSignal,
    OutboxWorkerSettings,
    SignedEventBatchHttpTransport,
)

_PROFILE_CAPABILITIES = {
    EnvironmentProfile.READ_ONLY: RuntimeCapability.OBSERVE_ONLY,
    EnvironmentProfile.PAPER: RuntimeCapability.FULL,
    EnvironmentProfile.SHADOW: RuntimeCapability.FULL,
    EnvironmentProfile.ASSISTED: RuntimeCapability.FULL,
    EnvironmentProfile.LIVE_EXIT_ONLY: RuntimeCapability.EXIT_ONLY,
    EnvironmentProfile.LIVE: RuntimeCapability.FULL,
}


@dataclass(frozen=True, slots=True)
class RuntimeWatchdogBoundary:
    """Runtime-owned half of the independent watchdog protocol."""

    heartbeat_store: RuntimeHeartbeatStore
    marker_store: ExternalHaltMarkerStore
    bridge: RuntimeWatchdogBridge
    startup_decision: TransitionDecision | None


def runtime_capability_for_profile(profile: EnvironmentProfile) -> RuntimeCapability:
    """Translate a validated deployment profile into its hard Runtime ceiling."""
    if not isinstance(profile, EnvironmentProfile):
        raise ValueError("profile must be an EnvironmentProfile")
    return _PROFILE_CAPABILITIES[profile]


def build_runtime_jobs(
    settings: RuntimeConfig, *, active_cadence: bool = False
) -> tuple[ScheduledJobDefinition, ...]:
    """Build the one in-process schedule from validated machine configuration."""
    if type(active_cadence) is not bool:
        raise ValueError("active_cadence must be a boolean")
    sync = settings.sync
    reconciliation = settings.reconciliation
    plan_seconds = (
        sync.plan_active_interval_seconds if active_cadence else sync.plan_idle_interval_seconds
    )
    heartbeat_seconds = (
        sync.heartbeat_active_interval_seconds
        if active_cadence
        else sync.heartbeat_idle_interval_seconds
    )
    return (
        ScheduledJobDefinition(
            "plan-sync", ScheduledTask.PLAN_SYNC, IntervalSchedule(plan_seconds)
        ),
        ScheduledJobDefinition(
            "command-sync",
            ScheduledTask.COMMAND_SYNC,
            IntervalSchedule(sync.command_active_interval_seconds),
        ),
        ScheduledJobDefinition(
            "event-upload",
            ScheduledTask.EVENT_UPLOAD,
            IntervalSchedule(sync.event_upload_interval_seconds),
        ),
        ScheduledJobDefinition(
            "heartbeat", ScheduledTask.HEARTBEAT, IntervalSchedule(heartbeat_seconds)
        ),
        ScheduledJobDefinition(
            "position-snapshot",
            ScheduledTask.POSITION_SNAPSHOT,
            IntervalSchedule(Decimal(reconciliation.position_interval_seconds)),
        ),
        ScheduledJobDefinition(
            "asset-snapshot",
            ScheduledTask.ASSET_SNAPSHOT,
            IntervalSchedule(Decimal(reconciliation.asset_interval_seconds)),
        ),
        ScheduledJobDefinition(
            "active-order-reconciliation",
            ScheduledTask.ACTIVE_ORDER_RECONCILIATION,
            IntervalSchedule(Decimal(reconciliation.active_orders_interval_seconds)),
            EventPriority.CRITICAL,
        ),
        ScheduledJobDefinition(
            "post-close-reconciliation",
            ScheduledTask.POST_CLOSE_RECONCILIATION,
            DailySchedule(15, 10),
            EventPriority.CRITICAL,
            misfire_grace_seconds=1800,
        ),
        ScheduledJobDefinition(
            "wal-checkpoint",
            ScheduledTask.WAL_CHECKPOINT,
            DailySchedule(15, 20),
            misfire_grace_seconds=1800,
        ),
        ScheduledJobDefinition(
            "journal-backup",
            ScheduledTask.JOURNAL_BACKUP,
            DailySchedule(15, 30),
            misfire_grace_seconds=1800,
        ),
        ScheduledJobDefinition(
            "tick-retention",
            ScheduledTask.TICK_RETENTION,
            DailySchedule(15, 40),
            misfire_grace_seconds=1800,
        ),
    )


def build_instance_guard(
    configuration: LoadedConfiguration,
    *,
    backend: InstanceLockBackend | None = None,
    namespace: str = "Global",
) -> AccountInstanceGuard:
    """Bind the OS lock to the resolved brokerage account, never its display alias."""
    effective_backend = WindowsNamedMutexBackend() if backend is None else backend
    account_key = configuration.secrets.broker_account_id.get_secret_value()
    return AccountInstanceGuard(account_key, backend=effective_backend, namespace=namespace)


def build_runtime_watchdog_boundary(
    settings: RuntimeConfig,
    state_machine: RuntimeStateMachine,
    *,
    clock: Clock,
    boot_id: str,
    halt_sink: Callable[[ExternalHaltMarker], Awaitable[None]],
    process_id: int | None = None,
    environment: Mapping[str, str] | None = None,
) -> RuntimeWatchdogBoundary:
    """Apply the external latch before QMT and create the Runtime heartbeat worker."""
    if not isinstance(settings, RuntimeConfig):
        raise ValueError("settings must be a RuntimeConfig")
    if not isinstance(state_machine, RuntimeStateMachine):
        raise ValueError("state_machine must be a RuntimeStateMachine")
    heartbeat_store = RuntimeHeartbeatStore.from_storage_root(
        settings.storage.root,
        node_id=settings.node.node_id,
        account_id=settings.node.account_id,
    )
    marker_store = ExternalHaltMarkerStore.from_storage_root(
        settings.storage.root,
        node_id=settings.node.node_id,
        account_id=settings.node.account_id,
    )
    startup_decision = enforce_external_halt_at_startup(
        state_machine=state_machine,
        marker_store=marker_store,
        force_halt_environment=environment,
    )
    bridge = RuntimeWatchdogBridge(
        heartbeat_store=heartbeat_store,
        marker_store=marker_store,
        clock=clock,
        node_id=settings.node.node_id,
        account_id=settings.node.account_id,
        runtime_state=lambda: state_machine.state,
        halt_sink=halt_sink,
        interval_seconds=settings.watchdog.heartbeat_interval_seconds,
        process_id=process_id,
        boot_id=boot_id,
    )
    return RuntimeWatchdogBoundary(
        heartbeat_store=heartbeat_store,
        marker_store=marker_store,
        bridge=bridge,
        startup_decision=startup_decision,
    )


def build_runtime_redactor(secrets: RuntimeSecrets) -> SensitiveValueRedactor:
    """Register every resolved runtime credential with each diagnostic boundary."""
    if not isinstance(secrets, RuntimeSecrets):
        raise ValueError("secrets must be RuntimeSecrets")
    return SensitiveValueRedactor.from_secret_values(
        secrets.node_hmac_secret,
        secrets.broker_account_id,
        secrets.operator_password_verifier,
    )


def build_diagnostic_bundle_writer(secrets: RuntimeSecrets) -> DiagnosticBundleWriter:
    """Build the only supported fault-package writer with runtime secrets registered."""
    return DiagnosticBundleWriter(build_runtime_redactor(secrets))


def build_persistence_writer(
    settings: RuntimeConfig,
    *,
    clock: Clock | None = None,
    queue_capacity: int = 1024,
    storage_acl_hardener: DirectoryAclHardener | None = None,
) -> PersistenceWriter:
    """Protect storage, then bind the account-scoped journal to its sole writer."""
    if not isinstance(settings, RuntimeConfig):
        raise ValueError("settings must be a RuntimeConfig")
    initialize_secure_storage(settings.storage.root, hardener=storage_acl_hardener)
    journal = SQLiteLocalJournal(
        SQLiteJournalSettings(
            database_path=(settings.storage.root / "data" / "local_journal.db").resolve(),
            account_id=settings.node.account_id,
            minimum_free_bytes=settings.storage.minimum_free_space_mb * 1024 * 1024,
            maximum_pending_outbox=settings.sync.outbox_critical_watermark,
        ),
        clock=clock,
    )
    return PersistenceWriter(journal, capacity=queue_capacity)


def build_order_intent_gate(
    settings: RuntimeConfig,
    persistence_writer: PersistenceWriter,
    broker_gateway: BrokerGateway,
    *,
    owner_guard: Callable[[], None],
    state_supplier: Callable[[], RuntimeState],
    hard_safety_facts_supplier: Callable[[], HardSafetyFacts],
) -> OrderIntentPersistenceGate:
    """Bind the production broker path to state, Hard Safety, and persistence gates."""
    return OrderIntentPersistenceGate(
        persistence_writer,
        broker_gateway,
        owner_guard=owner_guard,
        execution_guard=build_order_execution_guard(
            settings,
            state_supplier=state_supplier,
            hard_safety_facts_supplier=hard_safety_facts_supplier,
        ),
    )


def build_hard_safety_limits(settings: RuntimeConfig) -> HardSafetyLimits:
    """Copy immutable machine-local ceilings out of validated RuntimeConfig."""
    if not isinstance(settings, RuntimeConfig):
        raise ValueError("settings must be a RuntimeConfig")
    hard = settings.hard_safety
    return HardSafetyLimits(
        policy_version="WINDOWS_HARD_SAFETY_V1",
        max_positions=hard.max_positions,
        hard_max_exposure=Ratio(hard.hard_max_exposure),
        max_single_position_exposure=Ratio(hard.max_single_position_exposure),
        max_active_orders=hard.max_active_orders,
        active_order_per_symbol_side=hard.active_order_per_symbol_side,
        max_single_order_notional=Money(hard.max_single_order_notional),
        max_daily_buy_notional=Money(hard.max_daily_buy_notional),
        max_tick_age_seconds=hard.max_tick_age_seconds,
        max_clock_skew_seconds=hard.max_clock_skew_seconds,
    )


def build_order_execution_guard(
    settings: RuntimeConfig,
    *,
    state_supplier: Callable[[], RuntimeState],
    hard_safety_facts_supplier: Callable[[], HardSafetyFacts],
) -> CompositeOrderExecutionGuard:
    """Compose Runtime permission and Windows Hard Safety for every broker call."""
    limits = build_hard_safety_limits(settings)
    return CompositeOrderExecutionGuard(
        (
            RuntimeOrderExecutionGuard(state_supplier),
            HardSafetyOrderExecutionGuard(
                HardSafetyEvaluator(limits),
                facts_supplier=hard_safety_facts_supplier,
            ),
        )
    )


def build_local_control_coordinator(
    settings: RuntimeConfig,
    persistence_writer: PersistenceWriter,
    state_machine: RuntimeStateMachine,
    *,
    boot_id: str,
    clock: Clock,
    owner_guard: Callable[[], None],
) -> LocalControlCoordinator:
    """Bind local operator controls to the account journal and sole state owner."""
    if not isinstance(settings, RuntimeConfig):
        raise ValueError("settings must be a RuntimeConfig")
    return LocalControlCoordinator(
        persistence_writer,
        state_machine,
        node_id=settings.node.node_id,
        account_id=settings.node.account_id,
        boot_id=boot_id,
        clock=clock,
        owner_guard=owner_guard,
    )


def build_signed_event_transport(
    settings: RuntimeConfig,
    secrets: RuntimeSecrets,
    client: httpx.AsyncClient,
    *,
    clock: Clock,
    epoch_clock: EpochClock | None = None,
) -> SignedEventBatchHttpTransport:
    """Bind the event endpoint to node identity and redaction-safe HMAC credentials."""
    if not isinstance(settings, RuntimeConfig):
        raise ValueError("settings must be a RuntimeConfig")
    if not isinstance(secrets, RuntimeSecrets):
        raise ValueError("secrets must be RuntimeSecrets")
    return SignedEventBatchHttpTransport(
        client,
        base_url=str(settings.central_api.base_url),
        node_id=settings.node.node_id,
        account_id=settings.node.account_id,
        key_id=settings.security.node_key_id,
        secret=secrets.node_hmac_secret.get_secret_value().encode("utf-8"),
        clock=clock,
        epoch_clock=epoch_clock,
    )


def build_central_http_client(settings: RuntimeConfig) -> httpx.AsyncClient:
    """Create the TLS-verifying, bounded HTTP client owned by the Sync Worker phase."""
    if not isinstance(settings, RuntimeConfig):
        raise ValueError("settings must be a RuntimeConfig")
    central = settings.central_api
    return httpx.AsyncClient(
        verify=True,
        timeout=httpx.Timeout(
            connect=float(central.connect_timeout_seconds),
            read=float(central.read_timeout_seconds),
            write=float(central.read_timeout_seconds),
            pool=float(central.connect_timeout_seconds),
        ),
        limits=httpx.Limits(
            max_connections=central.max_connections,
            max_keepalive_connections=central.max_connections,
        ),
        follow_redirects=False,
    )


def build_outbox_worker(
    settings: RuntimeConfig,
    persistence_writer: PersistenceWriter,
    dispatcher: OutboxDispatcher,
    *,
    clock: Clock,
    pressure_sink: Callable[[OutboxPressureSignal], None],
) -> DurableOutboxWorker:
    """Build the scheduler-driven Outbox worker from validated runtime limits."""
    if not isinstance(settings, RuntimeConfig):
        raise ValueError("settings must be a RuntimeConfig")
    sync = settings.sync
    return DurableOutboxWorker(
        persistence_writer,
        dispatcher,
        clock=clock,
        settings=OutboxWorkerSettings(
            batch_size=sync.event_batch_size,
            retry_base_seconds=sync.outbox_retry_base_seconds,
            retry_max_seconds=sync.outbox_retry_max_seconds,
            high_watermark=sync.outbox_high_watermark,
            critical_watermark=sync.outbox_critical_watermark,
        ),
        pressure_sink=pressure_sink,
    )
