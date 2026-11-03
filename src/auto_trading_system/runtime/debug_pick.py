"""Authenticated, boot-local consent for one frozen debug selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta
from time import monotonic
from uuid import uuid4

from pydantic import Field, JsonValue, field_validator

from auto_trading_system.control.ipc_protocol import IPCError
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.runtime.daily_auto import DailyAutoController
from auto_trading_system.runtime.engine_v2 import CN, EngineKernel
from auto_trading_system.runtime.manual_buy import ManualBuyController, ManualBuyInput, ManualFacts
from auto_trading_system.security.operator_session import AuthenticatedOperatorSession


class DebugPickInput(WireModel):
    symbol: str = Field(min_length=6, max_length=9)

    @field_validator("symbol")
    @classmethod
    def normalize(cls, value: str) -> str:
        return ManualBuyInput.symbol_market(value)


class DebugPickConfirm(WireModel):
    preview_id: str = Field(pattern=r"^debug-[a-f0-9]{32}$")
    phrase: str = Field(max_length=128)


@dataclass
class Preview:
    session: str
    symbol: str
    name: str
    phrase: str
    epoch: int
    expires: float
    result: dict[str, JsonValue] | None = None


class DebugPickController:
    def __init__(
        self, engine: EngineKernel, automatic: DailyAutoController, manual: ManualBuyController
    ) -> None:
        self.engine, self.automatic, self.manual = engine, automatic, manual
        self.previews: dict[str, Preview] = {}

    def check(self) -> None:
        engine, auto = self.engine, self.automatic
        feed = engine.local_tail_feed
        now = engine._now().astimezone(CN)
        if feed is None:
            raise IPCError("DEBUG_PICK_UNAVAILABLE")
        if feed.debug_plan is not None:
            raise IPCError("DEBUG_PICK_ALREADY_USED")
        if not time(14, 40) <= now.time() < time(14, 55):
            raise IPCError("DEBUG_PICK_OUTSIDE_WINDOW")
        if auto.enabled or (auto.consent and auto.consent.enabled):
            raise IPCError("DEBUG_PICK_DISABLE_AUTO_FIRST")
        if not self.manual.settings_current():
            raise IPCError("MANUAL_BUY_SETTINGS_CHANGED")
        auto.check_enable()
        engine.check_manual_ready()
        if (
            not engine.dispatcher.idle
            or not engine.critical_idle
            or any(p.payload.allocations for p in engine.plans.plans.accepted())
            or engine._broker_snapshot is None
            or any(not o.is_terminal for o in engine._broker_snapshot.orders)
            or any(
                s.action_type == "NEW" and s.context_type == "ENTRY_PLAN"
                for s in engine._known.values()
            )
        ):
            raise IPCError("DEBUG_PICK_FORMAL_CONFLICT")

    def preview(
        self, session: AuthenticatedOperatorSession, value: DebugPickInput, facts: ManualFacts
    ) -> dict[str, JsonValue]:
        self.check()
        self.engine.stage_manual_observation(facts.account)
        if facts.instrument.symbol != value.symbol or not self.engine.buy_board_allowed(
            value.symbol
        ):
            raise IPCError("DEBUG_PICK_BOARD_BLOCKED")
        self.previews = {k: v for k, v in self.previews.items() if v.expires > monotonic()}
        if len(self.previews) >= 32:
            raise IPCError("CONSOLE_BUSY")
        identity = "debug-" + uuid4().hex
        phrase = f"确认真实自动买入 {value.symbol}"
        self.previews[identity] = Preview(
            session.session_key,
            value.symbol,
            facts.name,
            phrase,
            self.engine.permission.snapshot().epoch,
            monotonic() + 30,
        )
        settings = self.automatic.settings
        return {
            "preview_id": identity,
            "symbol": value.symbol,
            "name": facts.name,
            "phrase": phrase,
            "cash": facts.account.account.cash,
            "symbol_limit": settings.buy.max_symbol_buy_value,
            "daily_limit": settings.buy.max_daily_buy_value,
            "daily_symbols_limit": settings.buy.max_daily_symbols,
            "position_limit": settings.safety.max_positions,
            "cancel_after_seconds": settings.buy.cancel_after_seconds,
            "max_retries": settings.buy.max_retries,
            "expires_at": (self.engine._now() + timedelta(seconds=30)).isoformat(),
        }

    def confirm(
        self, session: AuthenticatedOperatorSession, value: DebugPickConfirm
    ) -> dict[str, JsonValue]:
        item = self.previews.get(value.preview_id)
        if item is None or item.session != session.session_key:
            raise IPCError("DEBUG_PICK_CONFIRMATION_INVALID")
        if item.result is not None:
            return item.result
        if (
            item.expires <= monotonic()
            or value.phrase != item.phrase
            or item.epoch != self.engine.permission.snapshot().epoch
        ):
            raise IPCError("DEBUG_PICK_CONFIRMATION_INVALID")
        self.check()
        feed = self.engine.local_tail_feed
        assert feed is not None
        # Consume before persistence: an uncertain write must never silently retry.
        item.result = {"status": "CHECK_REQUIRED"}
        try:
            plan_id = feed.start_debug(
                item.symbol, item.name, session.actor_id, self.engine._now().astimezone(CN)
            )
            self.automatic.start_debug_once(session.actor_id, expected_epoch=item.epoch)
        except Exception:
            self.engine.emergency_halt("DEBUG_PICK_START_FAILED")
            raise
        item.result = {
            "status": "STARTED",
            "plan_id": plan_id,
            "symbol": item.symbol,
            "name": item.name,
        }
        return item.result
