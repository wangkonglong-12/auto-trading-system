"""Agent-side immutable plan loading with boot-local activation fencing."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.plans import PlanEnvelope, check_plan_binding, verify_plan
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.tail_phase_history import TailPhaseHistory


class PlanFiles:
    def __init__(self, files: ArtifactFiles, keys: Mapping[str, Ed25519PublicKey]) -> None:
        self._files = files
        self._keys = dict(keys)
        self._loaded: PlanEnvelope | None = None
        self._armed = False
        self.debug_plan: PlanEnvelope | None = None
        self.tail_history: TailPhaseHistory | None = None

    def accepted(self) -> tuple[PlanEnvelope, ...]:
        if self.debug_plan is not None:
            return (self.debug_plan,)
        if self.tail_history is not None:
            return tuple(self.tail_history.plans.values())
        return (self._loaded,) if self._loaded is not None else ()

    def select_debug(self, plan: PlanEnvelope) -> None:
        verified = verify_plan(canonical_bytes(plan.model_dump(mode="json")), self._keys)
        if (
            verified.payload.source.algorithm_version != "local-debug-pick-v1"
            or len(verified.payload.allocations) != 1
            or (self.debug_plan is not None and self.debug_plan != verified)
            or self.tail_history is None
            or any(p.payload.allocations for p in self.tail_history.plans.values())
        ):
            raise ValueError("invalid or conflicting debug plan")
        self.debug_plan = verified
        self._loaded = verified

    def load_tail_phase(
        self, data: bytes, *, account_id: str, node_generation: int, now: datetime
    ) -> str:
        if self.tail_history is None:
            raise ValueError("three-phase mode is not installed")
        plan = verify_plan(data, self._keys)
        check_plan_binding(
            plan.payload, account_id=account_id, node_generation=node_generation, now=now
        )
        if plan.payload.source.published_at > now or len(plan.payload.allocations) > 15:
            raise ValueError("invalid or future tail phase")
        artifact_hash = self._files.put(data)
        self.tail_history.append(plan)
        self._loaded = self.tail_history.plans[max(self.tail_history.plans)]
        return artifact_hash

    def load(
        self,
        data: bytes,
        *,
        account_id: str,
        node_generation: int,
        now: datetime,
    ) -> str:
        plan = verify_plan(data, self._keys)
        check_plan_binding(
            plan.payload, account_id=account_id, node_generation=node_generation, now=now
        )
        if self._armed and self._loaded != plan:
            raise ValueError("armed plan is frozen; local pause and reconciliation are required")
        artifact_hash = self._files.put(data)
        self._loaded = plan
        return artifact_hash

    def mark_armed(self) -> None:
        """Called only after the engine confirms durable local session authorization."""
        if self._loaded is None:
            raise ValueError("no verified plan has been loaded")
        self._armed = True

    def restore_for_recovery(
        self, data: bytes, *, account_id: str, node_generation: int, now: datetime
    ) -> PlanEnvelope:
        """Restore signed ownership after cutoff; this NEVER restores permission."""
        plan = verify_plan(data, self._keys)
        payload = plan.payload
        if self.tail_history is not None and plan not in self.tail_history.plans.values():
            raise ValueError("tail recovery plan is not in the durable phase history")
        if (
            payload.account_id != account_id
            or payload.target_node_generation != node_generation
            or now.date() != payload.trade_date
        ):
            raise ValueError("recovery plan scope differs")
        self._loaded = (
            self.tail_history.plans[max(self.tail_history.plans)]
            if self.tail_history is not None
            else plan
        )
        if self.tail_history is not None and self.tail_history.suspended:
            self._loaded = None
        self._armed = True
        return plan

    def suspend_tail(self) -> None:
        if self.tail_history is None:
            raise ValueError("three-phase mode is not installed")
        self.tail_history.suspend()
        self._loaded = None

    def loaded(self) -> PlanEnvelope | None:
        return self._loaded

    def clear_unarmed(self) -> None:
        if self._armed:
            raise ValueError("armed plan remains frozen until explicit reconciliation")
        self._loaded = None
