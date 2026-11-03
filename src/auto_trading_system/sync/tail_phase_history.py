"""Signed, immutable per-phase plans; an append never broadens session ceilings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from auto_trading_system.protocol.v2.canonical import canonical_bytes, parse_bytes
from auto_trading_system.protocol.v2.plans import PlanEnvelope, verify_plan


def compatible_phase(previous: PlanEnvelope, incoming: PlanEnvelope) -> bool:
    left, right = previous.payload, incoming.payload
    fields = (
        "account_id",
        "strategy_id",
        "trade_date",
        "execution_window",
        "policy_bundle",
        "risk_budget",
        "offline_authorization",
        "rulebook_version",
        "target_node_generation",
    )
    return bool(
        left.source.decision_phase is not None
        and right.source.decision_phase is not None
        and left.source.selection_rule == right.source.selection_rule == "TAIL_EXCELLENT_V1"
        and all(getattr(left, field) == getattr(right, field) for field in fields)
    )


class TailPhaseHistory:
    def __init__(self, path: Path, *, allow_late: bool = False) -> None:
        self.path = path
        self.plans: dict[str, PlanEnvelope] = {}
        self.suspended = False
        self.allow_late = allow_late

    def validate(self, plan: PlanEnvelope) -> None:
        phase = plan.payload.source.decision_phase
        if phase is None or plan.payload.source.selection_rule != "TAIL_EXCELLENT_V1":
            raise ValueError("three-phase mode requires an excellent-only phase plan")
        prior = self.plans.get(phase)
        if prior is not None and self.suspended:
            raise ValueError("withdrawn phase cannot be reactivated")
        if prior is not None and prior != plan:
            raise ValueError("the same tail phase cannot change")
        if self.plans and not compatible_phase(next(iter(self.plans.values())), plan):
            raise ValueError("tail phase changes frozen authority")
        if not self.allow_late and prior is None and self.plans and phase < max(self.plans):
            raise ValueError("tail phase moved backwards")

    def append(self, plan: PlanEnvelope) -> None:
        self.validate(plan)
        phase = plan.payload.source.decision_phase
        assert phase is not None
        if phase in self.plans:
            return
        updated = {**self.plans, phase: plan}
        self._save(updated, suspended=False)
        self.plans, self.suspended = updated, False

    def suspend(self) -> None:
        self._save(self.plans, suspended=True)
        self.suspended = True

    def _save(self, plans: Mapping[str, PlanEnvelope], *, suspended: bool) -> None:
        body = canonical_bytes(
            {
                "plans": [value.model_dump(mode="json") for value in plans.values()],
                "suspended": suspended,
            }
        )
        if (
            len(body) > 512 * 1024
            or self.path.is_symlink()
            or self.path.resolve() != self.path.absolute()
            or (self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink != 1))
        ):
            raise ValueError("invalid phase history")
        partial = self.path.with_name(".tail-history-" + uuid4().hex)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with partial.open("xb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.replace(partial, self.path)
        finally:
            partial.unlink(missing_ok=True)

    def restore(
        self, keys: Mapping[str, Ed25519PublicKey], *, account_id: str, generation: int, today: date
    ) -> None:
        if not self.path.exists():
            return
        if (
            self.path.is_symlink()
            or self.path.resolve() != self.path.absolute()
            or not self.path.is_file()
            or self.path.stat().st_nlink != 1
        ):
            raise ValueError("invalid phase history path")
        with self.path.open("rb") as source:
            body = source.read(512 * 1024 + 1)
        records = parse_bytes(body, max_bytes=512 * 1024)
        suspended = False
        if isinstance(records, dict):
            if set(records) != {"plans", "suspended"} or type(records["suspended"]) is not bool:
                raise ValueError("invalid phase history state")
            suspended = records["suspended"]
            records = records["plans"]
        if not isinstance(records, list) or len(records) > 3:
            raise ValueError("invalid phase history capacity")
        restored: dict[str, PlanEnvelope] = {}
        for record in records:
            plan = verify_plan(canonical_bytes(record), keys)
            if plan.payload.trade_date < today:
                continue
            if (
                plan.payload.account_id,
                plan.payload.target_node_generation,
                plan.payload.trade_date,
            ) != (account_id, generation, today):
                raise ValueError("phase history scope differs")
            self.plans = restored
            self.validate(plan)
            phase = plan.payload.source.decision_phase
            assert phase is not None
            restored[phase] = plan
        self.plans = restored
        self.suspended = suspended if restored else False
