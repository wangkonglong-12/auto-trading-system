"""Engine-owned local execution contexts from the simple HTTPS selection feed.

The feed never supplies account permissions or budgets. Local contexts are signed
by a private installation key, retained for recovery, and fenced by normal grants.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.protocol.tail_picks import CN, TailBatch, TailPicks
from auto_trading_system.protocol.v2.api import PlanLookup, PlanReceipt
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.protocol.v2.plans import (
    Allocation,
    ExecutionWindow,
    OfflineAuthorization,
    PlanEnvelope,
    PlanPayload,
    RiskBudget,
    SourceRevision,
    sign_plan,
)
from auto_trading_system.strategies.trading_settings import TradingSettings, stock_board
from auto_trading_system.sync.artifact_files import ArtifactFiles

if TYPE_CHECKING:
    from auto_trading_system.execution.plan_control import EnginePlanControl

LOCAL_FEED_KEY_ID = "local-tail-feed-v1"


def read_private(path: Path, maximum: int) -> bytes | None:
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("tail feed path must be direct")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("tail feed file must be private and regular")
    with path.open("rb") as source:
        body = source.read(maximum + 1)
    if len(body) > maximum:
        raise ValueError("tail feed file exceeds its byte budget")
    return body


def write_private(path: Path, body: bytes) -> None:
    read_private(path, 131072)
    partial = path.with_name(".tail-feed-" + uuid4().hex)
    try:
        with partial.open("xb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def installation_key(root: Path) -> Ed25519PrivateKey:
    path = root / "critical/tail-feed-local-key.bin"
    body = read_private(path, 32)
    if body is None:
        if (root / "critical/tail-feed-state.json").exists() or (
            root / "critical/tail-feed-phase-plans.json"
        ).exists():
            raise ValueError("local feed key is missing for retained feed evidence")
        key = Ed25519PrivateKey.generate()
        body = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        write_private(path, body)
    if len(body) != 32:
        raise ValueError("invalid local feed key")
    return Ed25519PrivateKey.from_private_bytes(body)


class LocalTailFeed:
    def __init__(
        self,
        config: NodeProcessConfig,
        settings: TradingSettings,
        plans: EnginePlanControl,
        key: Ed25519PrivateKey,
    ) -> None:
        if (
            config.tail_picks is None
            or settings.sell is not None
            or settings.buy.signal_mode != "TAIL_EXCELLENT_3_PHASES"
            or not settings.buy.enabled
            or plans.plans.tail_history is None
        ):
            raise ValueError("simple feed requires installed buy-only three-phase settings")
        self.config, self.settings, self.plans, self.key = config, settings, plans, key
        self.path = config.data_root / "critical/tail-feed-state.json"
        self.scope = digest(
            canonical_bytes(
                {
                    "node": config.scope().decode(),
                    "origin": config.quant_origin,
                    "feed": config.tail_picks.model_dump(mode="json"),
                    "settings": settings.model_dump(mode="json"),
                    "policies": [p.model_dump(mode="json") for p in config.policies],
                    "rulebook": config.rulebook,
                }
            )
        )
        self.debug_path = config.data_root / "critical/debug-pick.json"
        self.debug_plan: PlanEnvelope | None = None
        self.snapshot: TailPicks | None = None
        self.suspended = False
        self.last_received: datetime | None = None
        self.apply_context: Callable[[PlanLookup, str | None], PlanReceipt] = plans.apply

    def _restore_settings_scope(self, scope: object) -> None:
        if scope == self.scope:
            return
        from auto_trading_system.strategies.trading_settings_drafts import TradingSettingsDraft

        history = self.config.data_root / "operator" / "trading_settings_versions"
        candidates = sorted(history.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if len(candidates) > 1024:
            raise ValueError("settings history exceeds recovery bound")
        for path in candidates:
            previous = TradingSettingsDraft.model_validate_json(path.read_bytes()).settings
            assert self.config.tail_picks is not None
            candidate = digest(
                canonical_bytes(
                    {
                        "node": self.config.scope().decode(),
                        "origin": self.config.quant_origin,
                        "feed": self.config.tail_picks.model_dump(mode="json"),
                        "settings": previous.model_dump(mode="json"),
                        "policies": [p.model_dump(mode="json") for p in self.config.policies],
                        "rulebook": self.config.rulebook,
                    }
                )
            )
            if candidate == scope:
                self.settings, self.scope = previous, candidate
                return
        raise ValueError("retained feed scope has no verified settings history")

    def restore(self, now: datetime) -> None:
        self.restore_debug(now)
        if self.debug_plan is not None:
            return
        body = read_private(self.path, 131072)
        if body is None:
            return
        value = parse_bytes(body, max_bytes=131072)
        if not isinstance(value, dict) or set(value) != {"scope", "feed", "suspended"}:
            raise ValueError("invalid retained feed state")
        feed = TailPicks.model_validate_json(canonical_bytes(value["feed"]))
        if feed.trade_date < now.astimezone(CN).date():
            return
        self._restore_settings_scope(value["scope"])
        if feed.trade_date != now.astimezone(CN).date() or value["scope"] != self.scope:
            raise ValueError("retained feed scope differs from installed local settings")
        if type(value["suspended"]) is not bool:
            raise ValueError("invalid retained feed suspension")
        self.snapshot, self.suspended = feed, value["suspended"]
        self._load_batches(feed, now)

    @property
    def complete(self) -> bool:
        return self.debug_plan is not None or (
            self.snapshot is not None and self.snapshot.all_ready
        )

    def allows_buy(self, now: datetime) -> bool:
        if self.debug_plan is not None:
            return not self.suspended and (
                self.debug_plan.payload.trade_date == now.astimezone(CN).date()
                and now < self.debug_plan.payload.execution_window.stop_new_buy_at
            )
        return bool(
            not self.suspended
            and self.snapshot is not None
            and self.snapshot.trade_date == now.astimezone(CN).date()
            and (
                self.complete
                or (
                    self.last_received is not None
                    and 0 <= (now - self.last_received).total_seconds() <= 15
                )
            )
        )

    def _save(self, feed: TailPicks, suspended: bool) -> None:
        write_private(
            self.path,
            canonical_bytes(
                {
                    "scope": self.scope,
                    "feed": feed.model_dump(mode="json"),
                    "suspended": suspended,
                }
            ),
        )

    def fault(self) -> None:
        self.suspended = True
        if self.snapshot is not None:
            self._save(self.snapshot, True)

    def accept(self, body: bytes, now: datetime) -> None:
        if self.debug_plan is not None:
            return  # Debug source remains frozen for this day; never blend remote picks.
        if self.suspended:
            raise ValueError("feed is suspended")
        feed = TailPicks.model_validate_json(canonical_bytes(parse_bytes(body, max_bytes=65536)))
        feed.check_current(now)
        if self.snapshot is not None:
            for before, after in zip(self.snapshot.batches, feed.batches, strict=True):
                if before.status == "READY" and before != after:
                    self.fault()
                    raise ValueError("previously ready feed batch changed")
        self._load_batches(feed, now)
        # Completion is published only after contexts AND the raw snapshot are durable.
        self._save(feed, False)
        self.snapshot, self.last_received = feed, now

    def _load_batches(self, feed: TailPicks, now: datetime) -> None:
        accepted = {p.payload.source.decision_phase: p for p in self.plans.plans.accepted()}
        for batch in feed.batches:
            if batch.status != "READY":
                continue
            published_at = (
                accepted[batch.phase].payload.source.published_at
                if batch.phase in accepted
                else now.replace(microsecond=0)
            )
            payload = self._payload(feed, batch, published_at)
            fixed = sign_plan(payload, LOCAL_FEED_KEY_ID, self.key)
            if batch.phase in accepted:
                if accepted[batch.phase] != fixed:
                    raise ValueError("local context differs from retained source")
                continue
            if now >= payload.execution_window.stop_new_buy_at:
                continue  # Keep a late response for display; never turn it into a late buy.
            lookup = PlanLookup(
                account_id=payload.account_id,
                strategy_id=payload.strategy_id,
                trade_date=payload.trade_date,
                plan_id=payload.plan_id,
                state="PUBLISHED",
                state_version=1,
                etag=fixed.envelope.payload_hash,
                plan=fixed,
            )
            artifact = ArtifactFiles(self.config.data_root / "artifacts").put(
                canonical_bytes(fixed.model_dump(mode="json"))
            )
            receipt = self.apply_context(lookup, artifact)
            if receipt.status != "LOADED":
                raise ValueError("engine rejected the local feed context")

    def _payload(self, feed: TailPicks, batch: TailBatch, published_at: datetime) -> PlanPayload:
        assert batch.source_run_id is not None and batch.completed_at is not None
        day = feed.trade_date
        basis = datetime.combine(day, time(0), CN)
        scheduled = datetime.combine(day, time(14, int(batch.phase[2:])), CN)
        stop = datetime.combine(day, time(14, 55), CN)
        cancel = datetime.combine(day, time(14, 56), CN)
        settings = self.settings
        source_hash = digest(canonical_bytes(batch.model_dump(mode="json")))
        return PlanPayload(
            plan_id="local-tail-"
            + digest(
                canonical_bytes(
                    {
                        "scope": self.scope,
                        "day": day.isoformat(),
                        "batch": source_hash,
                    }
                )
            )[7:],
            account_id=self.config.account_id,
            strategy_id=self.config.strategy_id,
            trade_date=day,
            target_node_generation=self.config.node_generation,
            source=SourceRevision(
                decision_phase=batch.phase,
                selection_rule="TAIL_EXCELLENT_V1",
                source_run_id=batch.source_run_id,
                source_revision=1,
                source_hash=source_hash,
                algorithm_version="local-tail-feed-adapter-v1",
                scheduled_at=scheduled,
                completed_at=batch.completed_at,
                market_data_cutoff=batch.completed_at,
                published_at=published_at,
                market_coverage_bps=0,
                run_status="SUCCEEDED",
                data_completeness="FULL",
                publication_status="FINAL",
                finalized=True,
            ),
            execution_window=ExecutionWindow(
                entry_not_before=datetime.combine(day, time(14, 40), CN),
                stop_new_buy_at=stop,
                cancel_by=cancel,
                min_execution_seconds=1,
            ),
            allocations=tuple(
                Allocation(
                    allocation_id=f"local-{stock.rank}",
                    symbol=stock.code,
                    rank=stock.rank,
                    target_value=settings.buy.max_symbol_buy_value,
                    max_value=settings.buy.max_symbol_buy_value,
                    max_qty=settings.advanced.max_order_quantity * (settings.buy.max_retries + 1),
                )
                for stock in batch.stocks
                if stock_board(stock.code) in settings.buy.allowed_boards
            ),
            risk_budget=RiskBudget(
                max_buy_value=settings.buy.max_daily_buy_value,
                budget_basis_as_of=basis,
            ),
            offline_authorization=OfflineAuthorization(directions=("BUY",), valid_until=cancel),
            policy_bundle=self.config.policies,
            rulebook_version=self.config.rulebook,
        )

    def restore_debug(self, now: datetime) -> None:
        from auto_trading_system.protocol.v2.plans import verify_plan

        body = read_private(self.debug_path, 131072)
        if body is None:
            return
        value = parse_bytes(body, max_bytes=131072)
        if not isinstance(value, dict) or set(value) != {"plan", "scope", "actor", "name"}:
            raise ValueError("invalid debug selection record")
        plan = verify_plan(
            canonical_bytes(value["plan"]), {LOCAL_FEED_KEY_ID: self.key.public_key()}
        )
        if plan.payload.trade_date < now.astimezone(CN).date():
            return
        self._restore_settings_scope(value["scope"])
        if (
            value["scope"] != self.scope
            or plan.payload.trade_date != now.astimezone(CN).date()
            or plan.payload.account_id != self.config.account_id
            or plan.payload.target_node_generation != self.config.node_generation
            or plan.payload.source.algorithm_version != "local-debug-pick-v1"
        ):
            raise ValueError("debug pick scope differs")
        self._install_debug(plan)

    def _install_debug(self, plan: PlanEnvelope) -> None:
        self.plans.plans.select_debug(plan)
        self.suspended = False
        self.debug_plan = plan

    def start_debug(self, symbol: str, name: str, actor: str, now: datetime) -> str:
        from auto_trading_system.protocol.tail_picks.models import pending_batch

        if now.tzinfo is None or not time(14, 40) <= now.astimezone(CN).time() < time(14, 55):
            raise ValueError("DEBUG_PICK_OUTSIDE_WINDOW")
        if self.debug_plan is not None:
            raise ValueError("DEBUG_PICK_ALREADY_USED")
        if any(p.payload.allocations for p in self.plans.plans.accepted()):
            raise ValueError("DEBUG_PICK_FORMAL_CONFLICT")
        batch = TailBatch.model_validate_json(
            canonical_bytes(
                {
                    "phase": "1440",
                    "status": "READY",
                    "source_run_id": "debug-" + uuid4().hex,
                    "completed_at": now.replace(microsecond=0).isoformat(),
                    "error_code": None,
                    "stocks": [{"code": symbol, "name": name, "rank": 1}],
                }
            )
        )
        feed = TailPicks(
            schema_version=1,
            trade_date=now.date(),
            server_time=now.replace(microsecond=0),
            batches=(batch, pending_batch("1445"), pending_batch("1450")),
        )
        payload = self._payload(feed, batch, now.replace(microsecond=0))
        payload = payload.model_copy(
            update={
                "plan_id": "debug-pick-" + uuid4().hex,
                "source": payload.source.model_copy(
                    update={"algorithm_version": "local-debug-pick-v1"}
                ),
            }
        )
        if len(payload.allocations) != 1:
            raise ValueError("DEBUG_PICK_BOARD_BLOCKED")
        plan = sign_plan(payload, LOCAL_FEED_KEY_ID, self.key)
        # Persist the source before making it visible to any strategy/authorization.
        write_private(
            self.debug_path,
            canonical_bytes(
                {
                    "scope": self.scope,
                    "actor": actor,
                    "name": name,
                    "plan": plan.model_dump(mode="json"),
                }
            ),
        )
        self._install_debug(plan)
        return plan.payload.plan_id
