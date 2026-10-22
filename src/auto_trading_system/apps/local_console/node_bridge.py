"""Small HTTP-to-IPC boundary; operator keys never reach the browser."""

from __future__ import annotations

from collections.abc import Callable
from threading import BoundedSemaphore

from pydantic import JsonValue

from auto_trading_system.config.node_v2 import NodeProcessConfig, role_keys
from auto_trading_system.control.ipc_protocol import IPCError, RPCClient
from auto_trading_system.control.windows_pipe import exchange

from .node_status import NodeStatusCache


class NodeConsoleBridge:
    def __init__(self, config: NodeProcessConfig) -> None:
        self.key = role_keys(config, ("operator",))["operator"]
        self.config = config
        # Emergency controls have their own channel and admission slot. Password
        # hashing and pending grants cannot queue ahead of an emergency halt here.
        self._gates = {
            "authorization": BoundedSemaphore(2),
            "control": BoundedSemaphore(1),
            "query": BoundedSemaphore(1),
        }
        self.status = NodeStatusCache(
            config,
            {
                name: self._probe(channel)
                for name, channel in (
                    ("engine", "control"),
                    ("agent", "agent"),
                    ("supervisor", "supervisor"),
                )
            },
        )

    def _client(self, channel: str, timeout: float) -> RPCClient:
        return RPCClient(
            lambda body: exchange(self.config.pipe_name(channel), body, timeout_seconds=timeout),
            "operator",
            self.key,
        )

    def _probe(self, channel: str) -> Callable[[], dict[str, JsonValue]]:
        return lambda: self._client(channel, 0.5).invoke("STATUS")

    def invoke(
        self, channel: str, action: str, payload: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        gate = self._gates[channel]
        if not gate.acquire(blocking=False):
            raise IPCError("CONSOLE_BUSY")
        try:
            return self._client(channel, 7).invoke(action, payload)
        finally:
            gate.release()
