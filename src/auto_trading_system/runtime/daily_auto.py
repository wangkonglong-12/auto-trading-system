"""Owner-only recurring tail BUY controller. A saved switch is never a live grant.

Every engine boot and trading day requires new broker recovery, durable control
records and a current local plan. Only authenticated operator consent may enable
this controller; it never receives browser-supplied trading limits.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from datetime import datetime, time, timedelta
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import JsonValue

from auto_trading_system.config.daily_auto import DailyAutoConsent, DailyAutoStore
from auto_trading_system.control.approval_v2 import OneShotApprovals
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.daily_sync import LocalControlRequest, SessionGrant
from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference
from auto_trading_system.runtime.engine_v2 import CN
from auto_trading_system.strategies.trading_settings import TradingSettings

if TYPE_CHECKING:
    from auto_trading_system.runtime.engine_v2 import EngineKernel


class DailyAutoController:
    def __init__(
        self,
        engine: EngineKernel,
        store: DailyAutoStore,
        settings: TradingSettings,
        *,
        clock: Callable[[], datetime],
        save_opening: Callable[[OpeningReference], None],
        elapsed: Callable[[], float] = monotonic,
        sealed_today: bool = False,
    ) -> None:
        self.engine, self.store, self.settings = engine, store, settings
        self.clock, self.elapsed, self.save_opening = clock, elapsed, save_opening
        self.settings_hash = digest(canonical_bytes(settings.model_dump(mode="json")))
        self.consent: DailyAutoConsent | None = None
        self.state, self.reason = "DISABLED", "NOT_ENABLED"
        self._step: str | None = None
        self._request: str | None = None
        self._deadline = 0.0
        self._grant_id: str | None = None
        self._closing: Future[OpeningReference] | None = None
        self._closed = sealed_today
        self._stop_requested = Event()
        try:
            self.consent = store.read()
            if self.consent and self.consent.enabled:
                if self.consent.settings_hash != self.settings_hash:
                    self.state, self.reason = "PAUSED", "SETTINGS_CHANGED"
                else:
                    self.state, self.reason = "WAITING", "WAITING_WINDOW"
            elif self.consent:
                self.reason = self.consent.disabled_reason or "NOT_ENABLED"
        except (OSError, ValueError):
            self.state, self.reason = "FAULT", "CONSENT_INVALID"

    @property
    def enabled(self) -> bool:
        return bool(
            self.consent
            and self.consent.enabled
            and self.consent.settings_hash == self.settings_hash
            and self.state not in {"FAULT", "PAUSED"}
            and not self._stop_requested.is_set()
        )

    def check_enable(self, *, debug: bool = False) -> None:
        feed = self.engine.local_tail_feed
        if not debug and feed is not None and feed.debug_plan is not None:
            raise ValueError("DEBUG_PICK_DAY_LOCKED")
        if self.engine.market_data_failure is not None:
            raise ValueError("DAILY_AUTO_MARKET_UNAVAILABLE")
        snapshot = self.engine.snapshot()
        if (
            self.engine.limits.mode != "LIVE"
            or self.engine.plans.plans.tail_history is None
            or (not snapshot.halted and self.engine.current_buy_grant() is None)
            or not self.engine.critical_idle
            or not self.settings.buy.enabled
            or self.settings.buy.signal_mode != "TAIL_EXCELLENT_3_PHASES"
            or self.settings.sell is not None
            or not isinstance(self.engine.approvals, OneShotApprovals)
            or self.engine._calendar.sessions[-1] <= self.clock().astimezone(CN).date()
        ):
            raise ValueError("DAILY_AUTO_NOT_READY")

    def enable(
        self, actor: str, *, consent_id: str | None = None, expected_epoch: int | None = None
    ) -> None:
        self.check_enable()
        epoch = (
            self.engine.permission.snapshot().epoch if expected_epoch is None else expected_epoch
        )
        self._stop_requested.clear()
        if self.engine.permission.snapshot().epoch != epoch:
            self._stop_requested.set()
            raise ValueError("DAILY_AUTO_CONFIRMATION_INVALID")
        consent = DailyAutoConsent(
            consent_id=consent_id or "auto-" + uuid4().hex,
            scope_hash=self.store.scope_hash,
            settings_hash=self.settings_hash,
            enabled=True,
            actor_id=actor,
            confirmed_at=self.clock(),
        )
        self.store.write(consent)
        self.consent = consent
        self.engine.buy_enabled = True
        self.state, self.reason = "WAITING", "WAITING_WINDOW"
        self._step, self._request, self._grant_id = None, None, None
        grant = self.engine.current_buy_grant()
        if (
            not self.engine.snapshot().halted
            and grant
            and grant.valid_from <= self.clock() < grant.valid_until
        ):
            self.state, self.reason, self._grant_id = "RUNNING", "BUY_ONLY", grant.grant_id

    def start_debug_once(self, actor: str, *, expected_epoch: int) -> None:
        """Boot-local one-target consent; never save recurring authorization."""
        self.check_enable(debug=True)
        if self.engine.permission.snapshot().epoch != expected_epoch:
            raise ValueError("DEBUG_PICK_CONFIRMATION_INVALID")
        self.consent = DailyAutoConsent(
            consent_id="debug-" + uuid4().hex,
            scope_hash=self.store.scope_hash,
            settings_hash=self.settings_hash,
            enabled=True,
            actor_id=actor,
            confirmed_at=self.clock(),
        )
        self._stop_requested.clear()
        if self.engine.permission.snapshot().epoch != expected_epoch:
            self._stop_requested.set()
            raise ValueError("DEBUG_PICK_CONFIRMATION_INVALID")
        self._step, self._request, self._grant_id = None, None, None
        self.state, self.reason = "WAITING", "DEBUG_PICK_ONCE"

    def request_stop(self) -> None:
        """Emergency thread may only tighten permission and latch the stop."""
        self.engine.emergency_halt("AUTHENTICATED_OPERATOR_HALT")
        self._stop_requested.set()

    def disable(self, reason: str = "OPERATOR_DISABLED") -> None:
        normal_stop = reason in {"OPERATOR_DISABLED", "SETTINGS_CHANGED"}
        snapshot = self.engine.snapshot()
        # An idle, already-disabled controller has no authority to revoke.
        # Keep its checked account state and any unrelated fault reason intact.
        needs_halt = (
            not normal_stop
            or bool(self.consent and self.consent.enabled)
            or self._step is not None
            or not snapshot.halted
            or snapshot.grant_id is not None
            or snapshot.state == "CONTROL_PERSISTING"
            or bool(snapshot.in_flight)
            or any(s.status in {"PERSISTING", "DISPATCHING"} for s in snapshot.submissions)
        )
        raw_detail = self.engine.permission.snapshot().reason
        detail = (
            raw_detail
            if not normal_stop and re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", raw_detail)
            else None
        )
        self.engine.buy_enabled = False
        if needs_halt and reason != "OPERATOR_DISABLED":
            existing_fault = (
                normal_stop
                and snapshot.state == "HALTED"
                and raw_detail
                not in {"DAILY_AUTO_OPERATOR_DISABLED", "DAILY_AUTO_SETTINGS_CHANGED"}
            )
            self.engine.emergency_halt(raw_detail if existing_fault else "DAILY_AUTO_" + reason)
        self.state, self.reason = "DISABLED" if normal_stop else "PAUSED", reason
        self._step, self._request, self._grant_id = None, None, None
        if self.consent is not None:
            consent = self.consent.model_copy(
                update={
                    "enabled": False,
                    "disabled_reason": reason,
                    "disabled_detail": detail,
                }
            )
            self.consent = consent
            try:
                self.store.write(consent)
            except (OSError, ValueError):
                self.state, self.reason = "FAULT", "CONSENT_SAVE_FAILED"
                raise

    def snapshot(self) -> dict[str, JsonValue]:
        recovery = self.engine.snapshot().recovery
        return {
            "supported": True,
            "mode": "DEBUG_ONCE"
            if self.engine.local_tail_feed and self.engine.local_tail_feed.debug_plan is not None
            else "CONTINUOUS",
            "enabled": self.enabled,
            "configured": bool(self.consent and self.consent.enabled),
            "consent_id": self.consent.consent_id if self.consent else None,
            "state": self.state,
            "reason": self.reason,
            "settings_hash": self.settings_hash,
            "calendar_through": self.engine._calendar.sessions[-1].isoformat(),
            "recovery_issues": list(recovery.issue_codes) if recovery else [],
            "pause_reason": self.consent.disabled_detail
            if self.consent and self.consent.disabled_detail
            else self.engine.permission.snapshot().reason,
            "limits": {
                "daily_value": self.settings.buy.max_daily_buy_value,
                "symbol_value": self.settings.buy.max_symbol_buy_value,
                "order_value": self.settings.buy.max_order_value,
                "daily_symbols": self.settings.buy.max_daily_symbols,
                "positions": self.settings.safety.max_positions,
                "position_pct": self.settings.safety.max_total_position_pct,
                "daily_loss": self.settings.safety.max_daily_loss,
            },
        }

    def _control(self, action: Literal["CONFIRM_RECOVERY", "ARM"]) -> None:
        engine, now = self.engine, self.clock()
        snapshot = engine.snapshot()
        report = snapshot.recovery
        if report is None or report.status != "CLEAR" or not self.consent:
            raise ValueError("RECOVERY_NOT_CLEAR")
        grant = None
        if action == "ARM":
            scopes = [scope for scope in engine._execution_scopes() if scope.kind == "ENTRY_PLAN"]
            if not scopes or any(scope.trade_date != engine.trade_date for scope in scopes):
                raise ValueError("CURRENT_PLAN_REQUIRED")
            self._grant_id = "auto-grant-" + uuid4().hex
            grant = SessionGrant(
                grant_id=self._grant_id,
                account_id=engine.account_id,
                engine_boot_id=engine.boot_id,
                session_generation=engine.next_session_generation,
                mode="LIVE",
                directions=("BUY",),
                max_buy_value=engine.buy_limits.max_buy_value,
                max_order_value=engine.buy_limits.max_order_value,
                max_positions=engine.buy_limits.max_positions,
                valid_from=now,
                valid_until=min(scope.valid_until for scope in scopes),
                confirmed_by=self.consent.actor_id,
                recovery_report_hash=report.payload_hash,
            )
        request = LocalControlRequest(
            request_id="auto-control-" + uuid4().hex,
            engine_boot_id=engine.boot_id,
            sender_role="agent",
            deadline=now + timedelta(seconds=10),
            expected_state_version=snapshot.state_version,
            action=action,
            grant=grant,
            artifact_hash=report.payload_hash if action == "CONFIRM_RECOVERY" else None,
            operator_authorization_id="auto-"
            + self.store.artifact_hash(self.consent)[7:]
            + "-"
            + uuid4().hex[:8],
        )
        engine.preview_control(request)
        approvals = engine.approvals
        if not isinstance(approvals, OneShotApprovals):
            raise ValueError("automatic authorization adapter unavailable")
        approvals.approve(request, authenticated_actor=self.consent.actor_id)
        result = engine.control(request)
        if result.status != "PENDING":
            raise ValueError("AUTO_CONTROL_REJECTED")
        self._request = request.request_id
        self._step = "CONFIRMING" if action == "CONFIRM_RECOVERY" else "ARMING"
        self._deadline = self.elapsed() + 10
        self.state, self.reason = self._step, "AUTO_RECOVERY"

    def _recover(self, *, closing: bool = False) -> None:
        if (
            not self.engine.critical_idle
            or not self.engine.dispatcher.idle
            or self.engine.snapshot().state in {"RECOVERING", "CONTROL_PERSISTING"}
        ):
            return
        self.engine.start_recovery()
        self._step = "CLOSING_RECOVERY" if closing else "VERIFYING"
        self._deadline = self.elapsed() + 30
        self.state, self.reason = self._step, "AUTO_RECOVERY"

    def poll(self) -> None:
        if self._stop_requested.is_set():
            if self.state not in {"PAUSED", "FAULT"}:
                with suppress(OSError, ValueError):
                    self.disable()
            return
        if not self.enabled:
            return
        try:
            self._poll()
        except (OSError, ValueError, RuntimeError, TimeoutError):
            with suppress(OSError, ValueError):
                self.disable("AUTO_CHECK_FAILED")

    def _poll(self) -> None:
        engine, now = self.engine, self.clock().astimezone(CN)
        if now.date() != engine.trade_date:
            return  # The existing desktop rollover starts a new checked engine.
        if self._step:
            if self.elapsed() >= self._deadline:
                raise TimeoutError("automatic check expired")
            if self._step in {"VERIFYING", "CLOSING_RECOVERY"}:
                snapshot = engine.snapshot()
                if snapshot.state in {"RECOVERY_BLOCKED", "HALTED"}:
                    self.disable("RECOVERY_BLOCKED")
                elif snapshot.state == "REVIEW_REQUIRED":
                    if self._step == "CLOSING_RECOVERY":
                        self._closing = engine.seal_portfolio()
                        self._step, self._deadline = "SEALING", self.elapsed() + 10
                    else:
                        self._control("CONFIRM_RECOVERY")
            elif self._step == "SEALING":
                assert self._closing is not None
                if self._closing.done():
                    self.save_opening(self._closing.result())
                    self._closed, self._step = True, None
                    self.state, self.reason = "WAITING", "WAITING_NEXT_DAY"
            else:
                assert self._request is not None
                result = engine.control_result(self._request)
                if result is not None and result.status != "PENDING":
                    if result.status != "APPLIED":
                        raise ValueError("automatic control failed")
                    if self._step == "CONFIRMING":
                        self._control("ARM")
                    else:
                        self._step = None
                        self.state, self.reason = "RUNNING", "BUY_ONLY"
            return
        if now.date() not in engine._calendar.sessions:
            self.state, self.reason = "WAITING", "NON_TRADING_DAY"
            return
        if now.time() >= time(14, 56):
            if not engine.snapshot().halted:
                engine.emergency_halt("DAILY_AUTO_WINDOW_ENDED")
            self.state, self.reason = "WAITING", "WAITING_NEXT_DAY"
            if now.time() >= time(15) and not self._closed:
                self._recover(closing=True)
            return
        if self.state == "RUNNING":
            snapshot = engine.snapshot()
            if snapshot.halted or snapshot.grant_id != self._grant_id:
                self.disable("ENGINE_PAUSED")
            return
        if not time(14, 40) <= now.time() < time(14, 55):
            self.state, self.reason = "WAITING", "WAITING_WINDOW"
            return
        try:
            engine._calendar.next_session(engine.trade_date)
        except ValueError:
            self.disable("CALENDAR_COVERAGE_MISSING")
            return
        plans = engine.plans.plans.accepted()
        if not any(
            p.payload.trade_date == engine.trade_date and p.payload.allocations for p in plans
        ):
            self.state, self.reason = "WAITING", "WAITING_FORMAL_PICKS"
            return
        self._recover()
