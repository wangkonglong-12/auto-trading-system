"""双击启动所需的模拟节点编排，不改变既有交易内核和授权规则。

每次启动创建新的受保护目录和临时认证秘密。子进程归属 Windows Job，
关闭启动器只回收自己创建的进程；旧目录和其他程序始终保留。
"""

from __future__ import annotations

import os
import socket
import sys
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, time, timedelta
from pathlib import Path
from secrets import token_hex
from threading import Event
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue

from auto_trading_system.config.node_v2 import NodeProcessConfig, load_node_config, resolve_secret
from auto_trading_system.control.ipc_protocol import IPCError, Role, RPCClient
from auto_trading_system.control.windows_pipe import current_sid, exchange
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.runtime.engine_v2 import CN
from auto_trading_system.runtime.node_layout import (
    create_private_directory,
    initialize_layout,
    validate_layout,
)
from auto_trading_system.runtime.owned_process import OwnedProcess
from auto_trading_system.security.operator_password import OperatorPasswordHasher

DESKTOP_OPERATOR_PASSWORD = "paper-demo-2026"


def node_command(arguments: list[str]) -> list[str]:
    """冻结后使用自身 EXE 分派工作进程；源码运行保留 Python 模块入口。"""
    executable = str(Path(sys.executable).resolve())
    if getattr(sys, "frozen", False):
        return [executable, "--node", *arguments]
    return [executable, "-m", "auto_trading_system.apps.desktop", "--node", *arguments]


def preview_config(
    directory: Path, identity: str, *, now: datetime, unconfigured: bool = False
) -> NodeProcessConfig:
    """构造零额度安全会话；未配置会话只用于打开操作台和接入页面。"""
    day = now.astimezone(CN).date()

    def reference(role: str) -> dict[str, JsonValue]:
        return {"source": "ENVIRONMENT", "variable": "ATS_SECRET_DESKTOP_" + role}

    return NodeProcessConfig.model_validate_json(
        canonical_bytes(
            {
                "schema_major": 2,
                "broker": "MOCK",
                "node_id": ("unconfigured-" if unconfigured else "desktop-") + identity,
                "account_id": ("qmt-not-configured-" if unconfigured else "paper-desktop-")
                + identity,
                "node_generation": 1,
                "journal_generation": "desktop-journal-1",
                "strategy_id": "not-configured" if unconfigured else "preview-unconfirmed",
                "service_sid": current_sid(),
                "data_root": str(directory / "node-data"),
                "ipc_secrets": {
                    name.lower(): reference(name) for name in ("AGENT", "JOBRUNNER", "OPERATOR")
                },
                "operator_access": {
                    "actor_id": "desktop-preview-operator",
                    "password_verifier": reference("VERIFIER"),
                },
                "quant_origin": "https://127.0.0.1:1",
                "quant_key_id": "preview-disabled",
                "quant_secret": reference("HTTP"),
                "trusted_plan_keys": {
                    "preview-only": Ed25519PrivateKey.generate()
                    .public_key()
                    .public_bytes_raw()
                    .hex()
                },
                "policies": [
                    {
                        "component": name,
                        "version": "preview-unconfirmed",
                        "payload_hash": digest(("preview-unconfirmed-" + name).encode()),
                    }
                    for name in ("entry", "exit", "execution", "risk")
                ],
                "rulebook": "preview-unconfirmed",
                "calendar": {
                    "version": "preview-not-for-trading",
                    "sessions": [day.isoformat(), (day + timedelta(days=1)).isoformat()],
                },
                "mock_cash": "10000.00",
            }
        )
    )


class PreviewSession:
    """单个生命周期工作线程拥有所有子进程，GUI 仅发出停止信号。"""

    def __init__(self, directory: Path, *, now: datetime | None = None) -> None:
        self.directory = directory.absolute()
        if self.directory.resolve() != self.directory:
            raise ValueError("desktop directory cannot traverse links")
        self.config = preview_config(self.directory, uuid4().hex, now=now or datetime.now(CN))
        self.path = self.directory / "node.json"
        self.environment = dict(os.environ)
        # 新会话不用旧监督身份，也不从外部环境继承模拟授权。
        self.environment.pop("ATS_NODE_SUPERVISION_ID", None)
        for role in ("AGENT", "JOBRUNNER", "OPERATOR", "HTTP"):
            self.environment["ATS_SECRET_DESKTOP_" + role] = token_hex(32)
        self.environment["ATS_SECRET_DESKTOP_VERIFIER"] = OperatorPasswordHasher().hash_password(
            DESKTOP_OPERATOR_PASSWORD
        )
        self.environment["ATS_DESKTOP_LOG_DIRECTORY"] = str(self.config.data_root / "logs")
        self.children: dict[str, OwnedProcess] = {}
        self.stop_requested = Event()
        self.started = False
        self.closed = False
        self.stopped_cleanly = False
        self.port = 0

    @property
    def url(self) -> str:
        if not self.port:
            raise RuntimeError("desktop console has not started")
        return f"http://127.0.0.1:{self.port}/"

    def client(self, channel: str, role: Role = "operator") -> RPCClient:
        key = resolve_secret(getattr(self.config.ipc_secrets, role), environment=self.environment)
        return RPCClient(
            lambda body: exchange(self.config.pipe_name(channel), body, timeout_seconds=0.5),
            role,
            key,
        )

    def automation_tick(self, now: datetime | None = None) -> None:
        """Preview sessions have no remote jobs."""

    def _launch(self, component: str, *arguments: str) -> None:
        if self.stop_requested.is_set():
            raise RuntimeError("DESKTOP_STOP_REQUESTED")
        self.children[component] = OwnedProcess(
            node_command(["--config", str(self.path), component, *arguments]),
            environment=self.environment,
        )

    def _wait(self, probe: Callable[[], bool], *, seconds: float = 60) -> None:
        until = monotonic() + seconds
        while monotonic() < until:
            if self.stop_requested.is_set():
                raise RuntimeError("DESKTOP_STOP_REQUESTED")
            if any(child.poll() is not None for child in self.children.values()):
                raise RuntimeError("DESKTOP_CHILD_EXITED")
            try:
                if probe():
                    return
            except (IPCError, OSError, ValueError, httpx.HTTPError):
                pass
            self.stop_requested.wait(0.1)
        raise RuntimeError("DESKTOP_START_TIMEOUT")

    def _prepare_files(self) -> None:
        create_private_directory(self.directory, self.config.service_sid)
        initialize_layout(self.config)
        with self.path.open("xb") as output:
            output.write(canonical_bytes(self.config.model_dump(mode="json")))
            output.flush()
            os.fsync(output.fileno())

    def _validate_ready_snapshot(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("real_trading_enabled") is not False:
            raise ValueError("desktop preview cannot enable real trading")

    def _supervisor_ready(self) -> bool:
        status = self.client("supervisor").invoke("STATUS")
        if status.get("blocked") is True or status.get("action") in {"BLOCKED", "FAULT"}:
            raise RuntimeError("SUPERVISOR_BLOCKED")
        return status.get("action") == "HEALTHY"

    def start(self) -> dict[str, Any]:
        if self.started or self.closed:
            raise RuntimeError("desktop session cannot be reused")
        self.started = True
        try:
            self._prepare_files()
            self._launch("supervise")
            self._wait(self._supervisor_ready)
            self._launch("agent")
            self._wait(lambda: self.client("agent").invoke("STATUS").get("component") == "agent")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                self.port = int(listener.getsockname()[1])
            self._launch("console", "--port", str(self.port))
            result: dict[str, Any] = {}

            def ready() -> bool:
                with httpx.Client(trust_env=False, timeout=2) as client:
                    response = client.get(self.url + "api/v2/node/status")
                    response.raise_for_status()
                    snapshot = response.json()
                if snapshot.get("node_id") != self.config.node_id:
                    raise ValueError("desktop HTTP node differs")
                self._validate_ready_snapshot(snapshot)
                if all(
                    snapshot["components"][name]["availability"] == "CURRENT"
                    for name in ("engine", "agent", "supervisor")
                ):
                    result.update(snapshot)
                    return True
                return False

            self._wait(ready)
            return result
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        """先请求暂停/正常退出，再以自有 Job 句柄回收；不按名称查杀进程。"""
        if self.closed:
            return
        self.closed = True
        self.stop_requested.set()
        if self.children:
            with suppress(IPCError, OSError, ValueError):
                self.client("control").invoke("HALT")
        for component, channel in (("agent", "agent"), ("supervise", "supervisor")):
            child = self.children.get(component)
            if child is not None:
                try:
                    self.client(channel).invoke("STOP")
                    child.wait(8)
                except (IPCError, OSError, ValueError, TimeoutError):
                    pass
        clean = True
        for child in reversed(tuple(self.children.values())):
            try:
                if child.poll() is None:
                    child.terminate()
                    child.wait(5)
            except (OSError, RuntimeError, TimeoutError):
                clean = False
            finally:
                try:
                    child.close()
                except Exception:
                    clean = False  # 继续关闭其他自有 Job，不能提前中断清理。
        self.children.clear()
        self.stopped_cleanly = clean

    def worker_status(self) -> dict[str, JsonValue]:
        return self.client("control").invoke("STATUS")


class UnconfiguredSession(PreviewSession):
    """Open the real application shell while every trading capability stays locked."""

    def __init__(self, directory: Path, *, now: datetime | None = None) -> None:
        super().__init__(directory, now=now)
        identity = uuid4().hex
        self.config = preview_config(
            self.directory,
            identity,
            now=now or datetime.now(CN),
            unconfigured=True,
        )


class ConfiguredSession(PreviewSession):
    """Launch an initialized QMT connection without replacing its config."""

    def __init__(self, config_path: Path, sdk_root: Path) -> None:
        if not config_path.is_absolute() or config_path.resolve() != config_path.absolute():
            raise ValueError("live config path must be a direct absolute path")
        if not sdk_root.is_absolute() or sdk_root.resolve() != sdk_root.absolute():
            raise ValueError("QMT SDK path must be a direct absolute path")
        config = load_node_config(config_path)
        if config.broker not in {"QMT_READ_ONLY", "QMT_TRADING"}:
            raise ValueError("desktop QMT launcher requires a QMT connection config")
        if not (sdk_root / "xtquant/xttrader.py").is_file():
            raise ValueError("QMT SDK path does not contain xtquant")
        self.directory = config.data_root
        self.config = config
        self.path = config_path
        self.environment = dict(os.environ)
        self.environment.pop("ATS_NODE_SUPERVISION_ID", None)
        if config.broker == "QMT_READ_ONLY":
            # The auto-detected read-only profile uses fresh per-launch IPC keys.
            # They only authenticate this launch's local child processes.
            for role in ("AGENT", "JOBRUNNER", "OPERATOR", "HTTP"):
                self.environment["ATS_SECRET_DESKTOP_" + role] = token_hex(32)
            if config.operator_access is not None:
                self.environment["ATS_SECRET_DESKTOP_VERIFIER"] = (
                    OperatorPasswordHasher().hash_password(DESKTOP_OPERATOR_PASSWORD)
                )
        source_root = Path(__file__).resolve().parents[3]
        paths = [str(sdk_root)]
        if not getattr(sys, "frozen", False):
            paths.insert(0, str(source_root))
        inherited = self.environment.get("PYTHONPATH")
        if inherited:
            paths.append(inherited)
        self.environment["PYTHONPATH"] = os.pathsep.join(paths)
        self.environment["ATS_QMT_SDK_ROOT"] = str(sdk_root)
        self.environment["ATS_DESKTOP_LOG_DIRECTORY"] = str(config.data_root / "logs")
        self.children = {}
        self.stop_requested = Event()
        self.started = False
        self.closed = False
        self.stopped_cleanly = False
        self.port = 0
        self._last_automatic_job: tuple[str, str] | None = None

    def _prepare_files(self) -> None:
        validate_layout(self.config)

    def _validate_ready_snapshot(self, snapshot: dict[str, Any]) -> None:
        expected = self.config.broker == "QMT_TRADING"
        if snapshot.get("real_trading_enabled") is not expected:
            raise ValueError("configured QMT node started in the wrong mode")

    def automation_tick(self, now: datetime | None = None) -> None:
        """Run the simple daily pull/export schedule while this desktop session is open."""
        if self.config.broker == "QMT_READ_ONLY":
            return
        current = (now or datetime.now(CN)).astimezone(CN)
        local_time = current.timetz().replace(tzinfo=None)
        key: tuple[str, str] | None = None
        if current.date() in self.config.calendar.sessions:
            if time(14, 30) <= local_time <= time(14, 55, 59):
                key = ("pull-plan", current.strftime("%Y-%m-%dT%H:%M"))
            elif time(15, 30) <= local_time < time(18, 1) and current.minute % 15 == 0:
                key = ("daily-export", current.strftime("%Y-%m-%dT%H:%M"))
        if key is None or key == self._last_automatic_job:
            return
        try:
            self.client("agent", "jobrunner").invoke("RUN_JOB", {"kind": key[0]})
        except (IPCError, OSError, ValueError):
            return
        self._last_automatic_job = key
