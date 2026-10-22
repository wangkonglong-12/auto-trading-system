"""Agent-side immutable plan loading with boot-local activation fencing."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from auto_trading_system.protocol.v2.plans import PlanEnvelope, check_plan_binding, verify_plan
from auto_trading_system.sync.artifact_files import ArtifactFiles


class PlanFiles:
    def __init__(self, files: ArtifactFiles, keys: Mapping[str, Ed25519PublicKey]) -> None:
        self._files = files
        self._keys = dict(keys)
        self._loaded: PlanEnvelope | None = None
        self._armed = False

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
        if (
            payload.account_id != account_id
            or payload.target_node_generation != node_generation
            or now.date() != payload.trade_date
        ):
            raise ValueError("recovery plan scope differs")
        self._loaded, self._armed = plan, True
        return plan

    def loaded(self) -> PlanEnvelope | None:
        return self._loaded

    def clear_unarmed(self) -> None:
        if self._armed:
            raise ValueError("armed plan remains frozen until explicit reconciliation")
        self._loaded = None
