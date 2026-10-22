"""Engine-owned plan activation fence; downloading never implies arming."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from threading import get_ident

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from auto_trading_system.protocol.v2.api import PlanLookup, PlanReceipt
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PolicyVersion, verify_plan
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.plan_files import PlanFiles


class EnginePlanControl:
    """Called on the engine owner thread after authenticating the local peer.

    pause_for_change is the engine's immediate permission-narrowing operation.
    Acknowledgment caching keeps receipt bodies immutable across network retries.
    The separate session-grant path is the only caller allowed to mark armed.
    """

    def __init__(
        self,
        files: ArtifactFiles,
        keys: Mapping[str, Ed25519PublicKey],
        *,
        node_id: str,
        account_id: str,
        node_generation: int,
        engine_boot_id: str,
        policies: tuple[PolicyVersion, ...],
        rulebook: str,
        clock: Callable[[], datetime],
        pause_for_change: Callable[[], None],
    ) -> None:
        self._files = files
        self._keys = dict(keys)
        self._owner = get_ident()
        self.plans = PlanFiles(files, keys)
        self.node_id = node_id
        self.account_id = account_id
        self.generation = node_generation
        self.boot_id = engine_boot_id
        self._policies = {item.component: item for item in policies}
        self._rulebook = rulebook
        self._clock = clock
        self._pause = pause_for_change
        self._armed = False
        self._receipts: dict[str, PlanReceipt] = {}
        self._latest_version: dict[str, int] = {}
        self._latest_etag: dict[str, str] = {}

    def mark_armed(self) -> None:
        """Only call after successful durable local authorization; never from the agent."""
        self._require_owner()
        self.plans.mark_armed()
        self._armed = True

    def restore_for_recovery(self, lookup: PlanLookup, artifact_hash: str) -> None:
        """Restore the last locally accepted immutable plan, with activation frozen."""
        self._require_owner()
        if lookup.state != "PUBLISHED" or lookup.plan is None:
            raise ValueError("recovery requires a published local plan")
        data = self._files.read(artifact_hash)
        payload = lookup.plan.payload
        if (
            data != canonical_bytes(lookup.plan.model_dump(mode="json"))
            or (lookup.account_id, lookup.strategy_id, lookup.trade_date, lookup.plan_id)
            != (self.account_id, payload.strategy_id, payload.trade_date, payload.plan_id)
            or payload.rulebook_version != self._rulebook
            or self._policies != {p.component: p for p in payload.policy_bundle}
        ):
            raise ValueError("recovery plan policy or artifact differs")
        self.plans.restore_for_recovery(
            data, account_id=self.account_id, node_generation=self.generation, now=self._clock()
        )
        self._armed = True
        scope = lookup.strategy_id + ":" + lookup.trade_date.isoformat()
        self._latest_version[scope], self._latest_etag[scope] = lookup.state_version, lookup.etag

    def apply(self, lookup: PlanLookup, artifact_hash: str | None) -> PlanReceipt:
        self._require_owner()
        if lookup.account_id != self.account_id:
            raise ValueError("plan belongs to another account")
        scope = lookup.strategy_id + ":" + lookup.trade_date.isoformat()
        if lookup.state_version < self._latest_version.get(scope, 0):
            raise ValueError("plan lookup version moved backwards")
        if (
            lookup.state_version == self._latest_version.get(scope)
            and lookup.etag != self._latest_etag[scope]
        ):
            raise ValueError("same lookup version has conflicting content")
        prior = self._receipts.get(lookup.etag)
        if prior is not None:
            return prior
        if len(self._receipts) >= 256:
            raise ValueError("plan-control receipt budget exhausted")
        loaded = self.plans.loaded()
        fixed = verify_plan(self._files.read(artifact_hash), self._keys) if artifact_hash else None
        if fixed and (
            fixed.payload.plan_id,
            fixed.payload.account_id,
            fixed.payload.strategy_id,
            fixed.payload.trade_date,
            fixed.payload.target_node_generation,
        ) != (
            lookup.plan_id,
            self.account_id,
            lookup.strategy_id,
            lookup.trade_date,
            self.generation,
        ):
            raise ValueError("fixed plan identity differs from lookup")
        plan_hash = (
            lookup.plan.envelope.payload_hash
            if lookup.plan
            else (
                fixed.envelope.payload_hash
                if fixed
                else (
                    loaded.envelope.payload_hash
                    if loaded and loaded.payload.plan_id == lookup.plan_id
                    else None
                )
            )
        )
        if plan_hash is None:
            raise ValueError("withdrawal does not reference a known local plan")
        status: str
        reason: str
        try:
            if lookup.state == "WITHDRAWN":
                if self._armed:
                    self._pause_safely()
                    status, reason = "CHANGE_REQUIRES_PAUSE", "ARMED_PLAN_WITHDRAWN"
                else:
                    self.plans.clear_unarmed()
                    status, reason = "WITHDRAWN", "CANDIDATE_REMOVED"
            else:
                if artifact_hash is None or lookup.plan is None:
                    raise ValueError("published plan artifact required")
                data = self._files.read(artifact_hash)
                if data != canonical_bytes(lookup.plan.model_dump(mode="json")):
                    raise ValueError("lookup and immutable plan file differ")
                payload = lookup.plan.payload
                if self._rulebook != payload.rulebook_version or self._policies != {
                    item.component: item for item in payload.policy_bundle
                }:
                    raise ValueError("plan policy or rulebook is unsupported")
                if self._armed and loaded != lookup.plan:
                    self._pause_safely()
                    status, reason = "CHANGE_REQUIRES_PAUSE", "ARMED_PLAN_FROZEN"
                else:
                    self.plans.load(
                        data,
                        account_id=self.account_id,
                        node_generation=self.generation,
                        now=self._clock(),
                    )
                    status, reason = "LOADED", "VERIFIED_AND_LOADED"
        except (ValueError, InvalidSignature):
            status, reason = "REJECTED", "PLAN_VALIDATION_FAILED"
        # This receipt states what the engine did, not what the agent downloaded.
        receipt = PlanReceipt.model_validate_json(
            canonical_bytes(
                {
                    "receipt_id": "load-"
                    + digest(canonical_bytes({"boot": self.boot_id, "etag": lookup.etag}))[7:],
                    "plan_id": lookup.plan_id,
                    "payload_hash": plan_hash,
                    "node_id": self.node_id,
                    "node_generation": self.generation,
                    "account_id": self.account_id,
                    "engine_boot_id": self.boot_id,
                    "status": status,
                    "reason_code": reason,
                    "observed_at": self._clock().isoformat(),
                }
            )
        )
        self._receipts[lookup.etag] = receipt
        self._latest_version[scope] = lookup.state_version
        self._latest_etag[scope] = lookup.etag
        return receipt

    def _require_owner(self) -> None:
        if get_ident() != self._owner:
            raise RuntimeError("plan state belongs to the engine owner thread")

    def _pause_safely(self) -> None:
        try:
            self._pause()
        except Exception as error:
            # A failed permission reduction is an engine fault. It must not be
            # swallowed as an ordinary invalid-plan rejection/acknowledgment.
            raise RuntimeError("engine permission reduction failed") from error
