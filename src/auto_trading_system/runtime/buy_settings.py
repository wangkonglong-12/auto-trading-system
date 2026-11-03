"""Owner-thread activation of saved BUY settings, independent from SELL grants."""

from __future__ import annotations

from concurrent.futures import Future
from typing import TYPE_CHECKING

from pydantic import JsonValue

from auto_trading_system.journal.critical_writer import CriticalStorePort

if TYPE_CHECKING:
    from auto_trading_system.runtime.node_engine import EngineEndpoint
from time import monotonic

from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.runtime.engine_v2 import EngineLimits
from auto_trading_system.runtime.qmt_trading_runtime import QmtTradingRuntime
from auto_trading_system.strategies.trading_settings_drafts import (
    TradingSettingsDraft,
    TradingSettingsDraftStore,
)


class BuySettingsController:
    def __init__(self, endpoint: EngineEndpoint, store: TradingSettingsDraftStore) -> None:
        self.endpoint, self.store = endpoint, store
        self.active = store.read()
        self.pending: Future[TradingSettingsDraft | str | None] | None = None
        self.next_read = 0.0
        self.reason: str | None = None
        self.supported = endpoint.engine.local_tail_feed is not None
        if self.supported and self.active:
            self._apply(self.active)

    def _apply(self, draft: TradingSettingsDraft) -> None:
        endpoint, settings = self.endpoint, draft.settings
        engine, market, auto = endpoint.engine, endpoint.market, endpoint.daily_auto
        assert isinstance(market, QmtTradingRuntime)
        old = market.settings
        if old is not None and (
            settings.sell != old.sell or settings.sell_execution != old.sell_execution
        ):
            raise ValueError("BUY_SETTINGS_RESTART_REQUIRED")
        if old is not None and settings.buy.signal_mode != old.buy.signal_mode:
            raise ValueError("BUY_SETTINGS_RESTART_REQUIRED")
        settings_hash = digest(canonical_bytes(settings.model_dump(mode="json")))
        if (
            auto is not None
            and auto.consent is not None
            and auto.consent.settings_hash != settings_hash
        ):
            consent = auto.consent.model_copy(update={"settings_hash": settings_hash})
            if auto.state != "FAULT" and not auto.consent.consent_id.startswith("debug-"):
                auto.store.write(consent)
            auto.consent = consent
            if auto.state == "PAUSED" and auto.reason == "SETTINGS_CHANGED" and consent.enabled:
                auto.state, auto.reason = "WAITING", "WAITING_WINDOW"
        limits = EngineLimits(
            mode=engine.limits.mode,
            max_buy_value=settings.buy.max_daily_buy_value,
            max_symbol_buy_value=settings.buy.max_symbol_buy_value,
            max_order_value=settings.buy.max_order_value,
            max_positions=settings.safety.max_positions,
            max_order_quantity=settings.advanced.max_order_quantity,
            max_daily_loss=settings.safety.max_daily_loss,
            max_total_position_pct=settings.safety.max_total_position_pct,
            max_daily_symbols=settings.buy.max_daily_symbols,
            allowed_boards=settings.buy.allowed_boards,
        )
        engine.buy_limits, engine.buy_settings = limits, settings
        engine.buy_settings_version = draft.version
        if engine.buy_settings_error != "MAX_DAILY_LOSS_REACHED":
            engine.buy_settings_error = None
        grant = engine.current_buy_grant()
        if grant is not None:
            engine._buy_grant = grant.model_copy(
                update={
                    "max_buy_value": limits.max_buy_value,
                    "max_order_value": limits.max_order_value,
                    "max_positions": limits.max_positions,
                }
            )
        market.settings = settings
        if auto is not None:
            auto.settings, auto.settings_hash = settings, settings_hash
        if endpoint.manual_buy is not None:
            endpoint.manual_buy.settings_hash = settings_hash
            endpoint.manual_buy.previews.clear()
        if endpoint.authorization is not None:
            endpoint.authorization._auto_previews.clear()
        self.active, self.reason = draft, None

    def poll(self) -> None:
        if not self.supported:
            return
        engine = self.endpoint.engine
        if self.pending is not None and self.pending.done():
            try:
                value = self.pending.result()
            except Exception:
                value = "BUY_SETTINGS_LOAD_FAILED"
            self.pending = None
            if isinstance(value, str) or value is None:
                self.reason = "BUY_SETTINGS_LOAD_FAILED"
                engine.buy_settings_error = self.reason
            elif self.active is None or value != self.active:
                self.reason = "BUY_SETTINGS_APPLYING"
                # Activation occurs only when no SDK call or J1 command can straddle it.
                market = self.endpoint.market
                assert isinstance(market, QmtTradingRuntime)
                owners = tuple(market.phase_owners.values())
                if (
                    engine.critical_idle
                    and engine.dispatcher.idle
                    and engine._pending is None
                    and all(o._pending is None and o._restore is None for o in owners)
                    and not any(
                        s.status in {"PERSISTING", "DISPATCHING"}
                        for s in engine.snapshot().submissions
                    )
                ):
                    try:
                        if self.active and value.version <= self.active.version:
                            raise ValueError("BUY_SETTINGS_VERSION_INVALID")
                        self._apply(value)
                    except (OSError, ValueError):
                        self.reason = "BUY_SETTINGS_APPLY_FAILED"
                        engine.buy_settings_error = self.reason
            else:
                if self.reason in {"BUY_SETTINGS_LOAD_FAILED", "BUY_SETTINGS_APPLY_FAILED"}:
                    self.reason = None
                    if engine.buy_settings_error != "MAX_DAILY_LOSS_REACHED":
                        engine.buy_settings_error = None
        if self.pending is None and monotonic() >= self.next_read:
            self.next_read = monotonic() + 0.25

            def read(_: CriticalStorePort) -> TradingSettingsDraft | str | None:
                try:
                    return self.store.read()
                except (OSError, ValueError, RuntimeError):
                    return "BUY_SETTINGS_LOAD_FAILED"

            self.pending = engine.journal.request(read, deadline=monotonic() + 5)

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            "supported": self.supported,
            "active_version": self.active.version if self.active else None,
            "reason": self.reason,
            "state": "PENDING" if self.reason else "APPLIED",
        }
