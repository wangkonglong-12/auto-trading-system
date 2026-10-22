"""Bounded, read-only component observations, independent of HTTP request volume.

Each component has one probe worker. A successful pipe exchange is not proof of
engine-owner progress: a repeated sequence becomes stale using a monotonic clock.
Failed reads keep the last observation explicitly labelled unavailable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from time import monotonic
from typing import cast

from pydantic import JsonValue

from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.control.ipc_protocol import IPCError

_FIELDS = {
    "engine": frozenset(
        [
            "component",
            "node_id",
            "account_id",
            "node_generation",
            "boot_id",
            "broker",
            "real_trading_enabled",
            "halted",
            "state",
            "state_version",
            "trade_date",
            "recovery",
            "pause_reason",
            "journal_healthy",
            "operator_authorization_enabled",
            "plan_loaded",
            "exit_context_loaded",
            "owner_sequence",
            "published_at",
        ]
    ),
    "agent": frozenset(["component", "boot_id", "invocation_id", "status"]),
    "supervisor": frozenset(
        [
            "component",
            "action",
            "child_process_id",
            "child_boot_id",
            "failures",
            "blocked",
            "reason",
            "child_status_current",
            "real_trading_enabled",
        ]
    ),
}


def _scalars(value: object, keys: frozenset[str]) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        return {}
    return {
        k: v for k, v in value.items() if k in keys and (v is None or type(v) in (str, int, bool))
    }


def _market_status(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, dict):
        return None
    result = _scalars(
        value,
        frozenset(
            {
                "state",
                "plan_hash",
                "subscription_count",
                "latest_quote_count",
                "failure_code",
                "driver_state",
                "configured",
                "exit_state",
                "liquidation_requested",
            }
        ),
    )
    for name, fields, maximum in (
        ("entry_progress", {"symbol", "state"}, 15),
        ("entry_targets", {"symbol", "target_quantity", "reason", "decided_at"}, 15),
        ("recent_activity", {"occurred_at", "category", "state", "symbol", "detail"}, 80),
    ):
        rows = value.get(name)
        result[name] = (
            [_scalars(row, frozenset(fields)) for row in rows[:maximum] if isinstance(row, dict)]
            if isinstance(rows, list)
            else []
        )
    return result


def project_status(
    component: str, value: dict[str, JsonValue], config: NodeProcessConfig
) -> dict[str, JsonValue]:
    """Allowlist display fields; never publish configuration, secrets or paths."""
    if value.get("component") != component:
        raise IPCError("STATUS_IDENTITY_MISMATCH")
    result = _scalars(value, _FIELDS[component])
    if component == "engine":
        for key, expected in {
            "node_id": config.node_id,
            "account_id": config.account_id,
            "node_generation": config.node_generation,
            "broker": config.broker,
            "real_trading_enabled": config.broker == "QMT_TRADING",
        }.items():
            if type(value.get(key)) is not type(expected) or value[key] != expected:
                raise IPCError("STATUS_IDENTITY_MISMATCH")
        if (
            type(value.get("owner_sequence")) is not int
            or int(str(value["owner_sequence"])) < 1
            or not isinstance(value.get("boot_id"), str)
            or not value["boot_id"]
        ):
            raise IPCError("STATUS_PROGRESS_INVALID")
        result["installed_limits"] = _scalars(
            value.get("installed_limits"),
            frozenset(["mode", "max_buy_value", "max_order_value", "max_positions"]),
        )
        result["broker_account"] = (
            _scalars(
                value.get("broker_account"),
                frozenset(
                    [
                        "observed_at",
                        "position_count",
                        "order_count",
                        "trade_count",
                        "coherent_for_recovery",
                    ]
                ),
            )
            if value.get("broker_account") is not None
            else None
        )
        result["paper_session"] = (
            _scalars(
                value.get("paper_session"),
                frozenset(
                    [
                        "mode",
                        "phase",
                        "rule_id",
                        "sell_rules_hash",
                        "as_of",
                        "driver_state",
                    ]
                ),
            )
            if value.get("paper_session") is not None
            else None
        )
        result["market_session"] = _market_status(value.get("market_session"))
        plan = value.get("plan")
        result["plan"] = None
        if isinstance(plan, dict):
            summary = _scalars(plan, frozenset(["plan_id", "strategy_id", "trade_date"]))
            symbols = plan.get("symbols")
            summary["symbols"] = (
                [s for s in symbols[:500] if isinstance(s, str)]
                if isinstance(symbols, list)
                else []
            )
            result["plan"] = summary
    elif component == "agent":
        result["last_result"] = _scalars(value.get("last_result"), frozenset(["status", "reason"]))
    return result


@dataclass
class _Observation:
    data: dict[str, JsonValue] | None = None
    received: float | None = None
    progressed: float | None = None
    observed_at: str | None = None
    error: str | None = "NOT_OBSERVED"


class NodeStatusCache:
    """Three fixed workers, no per-browser polling work on the engine owner."""

    def __init__(
        self,
        config: NodeProcessConfig,
        probes: Mapping[str, Callable[[], dict[str, JsonValue]]],
        *,
        elapsed: Callable[[], float] = monotonic,
    ) -> None:
        if set(probes) != set(_FIELDS):
            raise ValueError("all three component probes are required")
        self.config, self.probes, self.elapsed = config, dict(probes), elapsed
        self._lock, self._stop = Lock(), Event()
        self._observations = {name: _Observation() for name in probes}
        self._threads: list[Thread] = []

    def probe_once(self, component: str) -> None:
        try:
            data = project_status(component, self.probes[component](), self.config)
        except Exception:
            # Never surface raw transport errors, credentials or local paths.
            with self._lock:
                self._observations[component].error = "PROBE_FAILED"
            return
        now = self.elapsed()
        with self._lock:
            current = self._observations[component]
            if (
                component == "engine"
                and current.data is not None
                and (
                    data["boot_id"] == current.data["boot_id"]
                    and cast(int, data["owner_sequence"])
                    < cast(int, current.data["owner_sequence"])
                )
            ):
                current.error = "PROGRESS_REGRESSED"
                return
            if (
                component != "engine"
                or current.data is None
                or (
                    data["boot_id"] != current.data["boot_id"]
                    or cast(int, data["owner_sequence"]) > cast(int, current.data["owner_sequence"])
                )
            ):
                current.progressed = now
            current.data, current.received = data, now
            current.observed_at = datetime.now(UTC).isoformat()
            current.error = None

    def snapshot(self) -> dict[str, JsonValue]:
        now = self.elapsed()
        with self._lock:
            components: dict[str, JsonValue] = {}
            for name, current in self._observations.items():
                age = None if current.received is None else max(0.0, now - current.received)
                stalled = current.progressed is None or now - current.progressed >= 5
                state = (
                    "UNAVAILABLE"
                    if current.error
                    else "STALE"
                    if stalled or age is None or age >= 5
                    else "CURRENT"
                )
                components[name] = {
                    "availability": state,
                    "age_seconds": None if age is None else round(age, 1),
                    "observed_at": current.observed_at,
                    "error": current.error,
                    "data": deepcopy(current.data),
                }
        return {
            "node_id": self.config.node_id,
            "account_id": self.config.account_id,
            "broker": self.config.broker,
            "real_trading_enabled": self.config.broker == "QMT_TRADING",
            "strategy_status": "UNCONFIRMED",
            "operator_access_configured": self.config.operator_access is not None,
            "components": components,
        }

    def start(self) -> None:
        if self._threads or self._stop.is_set():
            raise RuntimeError("status cache cannot be restarted")
        for component in self.probes:
            thread = Thread(
                target=self._run, args=(component,), daemon=True, name="console-status-" + component
            )
            self._threads.append(thread)
            thread.start()

    def _run(self, component: str) -> None:
        while not self._stop.is_set():
            self.probe_once(component)
            self._stop.wait(1)

    def close(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError("console status probe did not stop")
