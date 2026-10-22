"""Long-lived network agent with single-flight jobs and restart discovery.

Task Runner submits a small authenticated job request. A dedicated worker owns
the job-store lease and HTTPS session; pipe/state and engine loops remain free.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, time
from threading import Event, Lock
from time import monotonic, sleep
from uuid import uuid4

from pydantic import JsonValue

from auto_trading_system.config.node_v2 import NodeProcessConfig, resolve_secret, role_keys
from auto_trading_system.control.ipc_protocol import IPCError, Request, RPCClient, RPCService
from auto_trading_system.control.windows_pipe import PipeServer, exchange
from auto_trading_system.protocol.v2.api import PlanLookup, PlanReceipt
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.runtime.engine_v2 import CN
from auto_trading_system.runtime.node_layout import validate_layout
from auto_trading_system.runtime.single_instance import WindowsNamedMutexBackend
from auto_trading_system.sync.agent_jobs import AgentJobs, QuantAgentTransport
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.daily_catalog import DailyCatalog, closing_slot
from auto_trading_system.sync.daily_upload import RetryableSyncError
from auto_trading_system.sync.http_v2 import HttpQuantTransport, NodeCredentials, SignedHttpClient
from auto_trading_system.sync.job_store import JobStore

JOB_KINDS = frozenset({"pull-plan", "daily-export", "retry", "catch-up"})


class EnginePlanClient:
    def __init__(self, files: ArtifactFiles, rpc: RPCClient) -> None:
        self.files, self.rpc = files, rpc

    def apply(self, lookup: PlanLookup, artifact_hash: str | None) -> PlanReceipt:
        reference = self.files.put(canonical_bytes(lookup.model_dump(mode="json")))
        try:
            reply = self.rpc.invoke(
                "LOAD_PLAN", {"lookup_hash": reference, "artifact_hash": artifact_hash}
            )
        except IPCError as exc:
            raise RetryableSyncError("ENGINE_HANDOFF_UNCERTAIN") from exc
        return PlanReceipt.model_validate_json(canonical_bytes(reply))


class AgentWorker:
    def __init__(
        self,
        config: NodeProcessConfig,
        engine: RPCClient,
        *,
        clock: Callable[[], datetime],
        remote: QuantAgentTransport | None = None,
        stop: Event | None = None,
    ) -> None:
        self.config, self.engine, self.clock, self.remote = config, engine, clock, remote
        self.stop = stop or Event()

    def _refresh_closing(self, catalog: DailyCatalog, now: datetime) -> None:
        prior = catalog.get(now.date())
        if prior is not None and prior.manifest.data_as_of >= closing_slot(now):
            return
        requested = self.engine.invoke("RECOVER_DAILY")
        version = requested["state_version"]
        if type(version) is not int:
            raise IPCError("INVALID_ENGINE_RESPONSE")
        until = monotonic() + 30
        while monotonic() < until:
            if self.stop.is_set():
                raise IPCError("AGENT_STOPPING")
            status = self.engine.invoke("STATUS")
            state_version = status.get("state_version")
            if (
                type(state_version) is int
                and state_version >= version
                and status["state"] in ("REVIEW_REQUIRED", "RECOVERY_BLOCKED", "READY_PAUSED")
            ):
                self.engine.invoke("REFRESH_DAILY")
                if status["state"] == "REVIEW_REQUIRED":
                    self.engine.invoke("SEAL_DAY")
                return
            sleep(0.05)
        raise IPCError("CLOSING_RECOVERY_PENDING")

    def run(self, kind: str) -> dict[str, JsonValue]:
        if kind not in JOB_KINDS:
            raise IPCError("INVALID_JOB")
        config, now = self.config, self.clock().astimezone(CN)
        if kind in ("pull-plan", "daily-export") and now.date() not in config.calendar.sessions:
            return {"status": "SKIPPED", "reason": "NON_TRADING_DAY"}
        if kind == "daily-export" and not time(15, 30) <= now.time() < time(18, 1):
            return {"status": "SKIPPED", "reason": "OUTSIDE_EXPORT_WINDOW"}
        if kind == "pull-plan" and not time(14, 30) <= now.time() <= time(14, 55, 59):
            return {"status": "SKIPPED", "reason": "OUTSIDE_PLAN_WINDOW"}
        files = ArtifactFiles(config.data_root / "artifacts")
        catalog = DailyCatalog(
            config.data_root / "daily-catalog",
            files,
            node_id=config.node_id,
            account_id=config.account_id,
            generation=config.node_generation,
        )
        client: SignedHttpClient | None = None
        deadline = monotonic() + 90

        def admit() -> None:
            if self.stop.is_set() or monotonic() >= deadline:
                raise RetryableSyncError("INVOCATION_BUDGET_EXHAUSTED")

        try:
            remote = self.remote
            if remote is None:
                context = (
                    ssl.create_default_context(cafile=str(config.quant_ca_file))
                    if config.quant_ca_file
                    else None
                )
                secret = resolve_secret(config.quant_secret)
                if secret in role_keys(config, ("agent", "jobrunner", "operator")).values():
                    raise ValueError("HTTP and IPC credentials must be different")
                client = SignedHttpClient(
                    config.quant_origin,
                    NodeCredentials(config.node_id, config.quant_key_id, secret),
                    tls_context=context,
                    admission=admit,
                )
                remote = HttpQuantTransport(client)
            with JobStore(
                config.data_root / "jobs",
                node_id=config.node_id,
                account_id=config.account_id,
                backend=WindowsNamedMutexBackend(),
            ) as store:
                jobs = AgentJobs(
                    files,
                    store,
                    remote,
                    EnginePlanClient(files, self.engine),
                    node_id=config.node_id,
                    account_id=config.account_id,
                    generation=config.node_generation,
                    strategy_id=config.strategy_id,
                    trusted_keys=config.public_keys(),
                    clock=self.clock,
                    is_trading_day=lambda day: day in config.calendar.sessions,
                )
                if kind == "pull-plan":
                    result = jobs.pull_plan()
                    return {
                        "status": "SKIPPED" if result is None else result.state,
                        "job_id": result.job_id if result else None,
                    }
                refresh_pending = False
                local_issue: str | None = None
                if kind == "daily-export":
                    try:
                        self._refresh_closing(catalog, now)
                    except IPCError:
                        # Fresh broker observations depend on the engine. Retained
                        # complete revisions can still upload when it is offline.
                        refresh_pending = True
                    except (OSError, ValueError):
                        refresh_pending = True
                        local_issue = "LOCAL_EXPORT_REVIEW"
                try:
                    for batch in catalog.discover():
                        admit()
                        jobs.register_daily(batch)
                except (OSError, ValueError):
                    # A corrupt new reference must be reported, but previously
                    # registered valid jobs can still make independent progress.
                    local_issue = "DAILY_CATALOG_REVIEW"
                results = jobs.retry_retained(maximum_jobs=8)
                states = {item.state for item in store.list()}
                return {
                    "status": "NEEDS_ATTENTION"
                    if local_issue or states & {"NEEDS_ATTENTION", "LOCAL_STORAGE_ERROR"}
                    else "PENDING"
                    if "PENDING" in states or refresh_pending
                    else "SYNCED",
                    "fresh_export": "PENDING" if refresh_pending else "NOT_PENDING",
                    "reason_code": local_issue,
                    "jobs": [{"job_id": item.job_id, "state": item.state} for item in results],
                }
        finally:
            if client is not None:
                client.close()


class AgentEndpoint:
    def __init__(self, worker: AgentWorker, stop: Event, boot_id: str) -> None:
        self.worker, self.stop, self.boot_id = worker, stop, boot_id
        self._lock = Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="node-agent-job")
        self._active: Future[dict[str, JsonValue]] | None = None
        self._last: dict[str, JsonValue] = {"status": "IDLE"}
        self._id: str | None = None

    def handle(self, request: Request) -> dict[str, JsonValue]:
        with self._lock:
            if self._active is not None and self._active.done():
                try:
                    self._last = self._active.result()
                except Exception:
                    self._last = {"status": "PENDING", "reason": "JOB_FAILED_RETRY_REQUIRED"}
                self._active = None
            if request.action == "RUN_JOB":
                if (
                    set(request.payload) != {"kind"}
                    or not isinstance(request.payload["kind"], str)
                    or request.payload["kind"] not in JOB_KINDS
                ):
                    raise IPCError("INVALID_JOB")
                if self.stop.is_set():
                    raise IPCError("AGENT_STOPPING")
                if self._active is not None:
                    raise IPCError("JOB_BUSY")
                self._id = "invocation-" + uuid4().hex
                self._active = self._pool.submit(self.worker.run, str(request.payload["kind"]))
                return {"invocation_id": self._id, "status": "RUNNING"}
            if request.payload:
                raise IPCError("INVALID_ARGUMENTS")
            if request.action == "STATUS":
                return {
                    "component": "agent",
                    "boot_id": self.boot_id,
                    "invocation_id": self._id,
                    "status": "RUNNING" if self._active is not None else "IDLE",
                    "last_result": self._last,
                }
            if request.action == "STOP":
                self.stop.set()
                return {"stopping": True}
            raise IPCError("ACTION_DENIED")

    def close(self) -> None:
        # Network budgets are enforced by the transport. The lease is held by
        # its worker until actual completion, never released on a UI timeout.
        self.stop.set()
        self._pool.shutdown(wait=True, cancel_futures=True)

    def catch_up_on_start(self) -> None:
        with self._lock:
            if self._active is not None:
                raise IPCError("JOB_BUSY")
            self._id = "startup-" + uuid4().hex
            self._active = self._pool.submit(self.worker.run, "catch-up")


def run_agent(
    config: NodeProcessConfig,
    *,
    stop: Event | None = None,
    clock: Callable[[], datetime] | None = None,
    ready: Callable[[dict[str, JsonValue]], None] = lambda _: None,
    remote: QuantAgentTransport | None = None,
) -> None:
    validate_layout(config)
    if clock is None:
        session = config.paper_session
        clock = (lambda: session.as_of) if session else lambda: datetime.now(CN)
    stopped = stop or Event()
    keys = role_keys(config, ("agent", "jobrunner", "operator"))
    engine = RPCClient(
        lambda data: exchange(config.pipe_name("engine"), data), "agent", keys["agent"]
    )
    boot = "agent-" + uuid4().hex
    endpoint = AgentEndpoint(
        AgentWorker(config, engine, clock=clock, remote=remote, stop=stopped), stopped, boot
    )
    service = RPCService(
        boot,
        keys,
        {
            "jobrunner": frozenset({"HELLO", "STATUS", "RUN_JOB"}),
            "operator": frozenset({"HELLO", "STATUS", "STOP"}),
        },
        endpoint.handle,
    )
    server: PipeServer | None = None
    try:
        server = PipeServer(config.pipe_name("agent"), config.service_sid, service.handle)
        # Startup catch-up needs no live engine; discovers exports left between
        # freezing and agent registration and repairs missing remote receipts.
        endpoint.catch_up_on_start()
        ready({"component": "agent", "boot_id": boot, "real_trading_enabled": False})
        while not stopped.wait(0.1):
            if not server.healthy:
                raise IPCError("PIPE_WORKER_FAILED")
    finally:
        if server is not None:
            server.close()
        endpoint.close()
