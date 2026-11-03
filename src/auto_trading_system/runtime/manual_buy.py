"""Authenticated one-shot purchases, with exact J1 ownership and no automatic retries."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal
from time import monotonic
from uuid import uuid4

from pydantic import Field, JsonValue, field_validator

from auto_trading_system.control.ipc_protocol import IPCError
from auto_trading_system.domain import SecurityCode
from auto_trading_system.gateways.broker.port import BrokerGateway
from auto_trading_system.journal.critical_models import ActionTarget, ManualBuyBinding, Submission
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.runtime.recovery_v2 import BrokerRecoverySnapshot
from auto_trading_system.security.operator_session import AuthenticatedOperatorSession
from auto_trading_system.strategies.entry_state import EntryInstrument


class ManualBuyInput(WireModel):
    symbol: str = Field(min_length=6, max_length=9)
    limit_price: str = Field(pattern=r"^(0|[1-9][0-9]{0,7})(\.[0-9]{1,4})?$")
    quantity: int = Field(strict=True, gt=0, le=1_000_000)

    @field_validator("symbol")
    @classmethod
    def symbol_market(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) == 6 and value.isascii() and value.isdigit():
            value += ".SH" if value.startswith("6") else ".SZ"
        return str(SecurityCode.parse(value))

    @field_validator("limit_price")
    @classmethod
    def positive(cls, value: str) -> str:
        if Decimal(value) <= 0:
            raise ValueError("price must be positive")
        return value


class ManualBuyConfirm(WireModel):
    preview_id: str = Field(pattern=r"^manual-[a-f0-9]{32}$")


class ManualBuyResult(WireModel):
    client_order_id: str = Field(pattern=r"^ord_[A-Za-z0-9_-]{8,64}$")


@dataclass(frozen=True)
class ManualFacts:
    account: BrokerRecoverySnapshot
    instrument: EntryInstrument
    name: str


@dataclass
class _Preview:
    session_key: str
    item: Submission
    expires: float
    confirmed: bool = False


class ManualBuyController:
    def __init__(
        self,
        engine: EngineKernel,
        *,
        strategy_id: str,
        settings_hash: str,
        instrument_reader: Callable[[str, str], tuple[EntryInstrument, str]],
        settings_current: Callable[[], bool],
        save_opening: Callable[[OpeningReference], None] | None = None,
    ) -> None:
        self.engine, self.strategy_id = engine, strategy_id
        self.settings_hash, self.instrument_reader = settings_hash, instrument_reader
        self.settings_current = settings_current
        self.previews: dict[str, _Preview] = {}
        self.save_opening = save_opening
        self._closing: Future[OpeningReference] | None = None
        self._closing_started = False
        self._closed = False

    def query(self, value: ManualBuyInput) -> Future[ManualFacts]:
        self.engine.check_manual_ready()
        if not self.engine.dispatcher.idle or any(
            s.status in {"PERSISTING", "DISPATCHING"} for s in self.engine.snapshot().submissions
        ):
            raise IPCError("ACCOUNT_QUERY_BUSY")

        def capture(broker: BrokerGateway) -> ManualFacts:
            instrument, name = self.instrument_reader(value.symbol, value.limit_price)
            return ManualFacts(self.engine._capture(broker), instrument, name)

        return self.engine.dispatcher.observe(capture)

    def preview(
        self,
        session: AuthenticatedOperatorSession,
        value: ManualBuyInput,
        facts: ManualFacts,
    ) -> dict[str, JsonValue]:
        engine = self.engine
        engine.check_manual_ready()
        self.previews = {k: v for k, v in self.previews.items() if v.expires > monotonic()}
        if len(self.previews) >= 32:
            raise IPCError("CONSOLE_BUSY")
        engine.stage_manual_observation(facts.account)
        instrument = facts.instrument
        if instrument.symbol != value.symbol:
            raise IPCError("MANUAL_BUY_INSTRUMENT_INVALID")
        now = engine._now()
        binding = ManualBuyBinding(
            strategy_id=self.strategy_id,
            actor_id=session.actor_id,
            settings_hash=self.settings_hash,
            symbol=value.symbol,
            name=facts.name,
            quantity=value.quantity,
            limit_price=value.limit_price,
            confirmed_at=now,
            lower_price=instrument.lower_price,
            upper_price=instrument.upper_price,
            price_tick=instrument.price_tick,
            min_quantity=instrument.min_quantity,
            quantity_step=instrument.quantity_step,
        )
        remark = uuid4().hex[:20]
        identity = "manual-" + uuid4().hex
        amount = Decimal(value.limit_price) * value.quantity
        item = Submission(
            client_order_id="ord_" + remark,
            account_id=engine.account_id,
            trade_date=engine.trade_date,
            action_type="NEW",
            context_type="MANUAL_ENTRY",
            plan_hash=digest(canonical_bytes(binding.model_dump(mode="json"))),
            symbol=value.symbol,
            side="BUY",
            quantity=value.quantity,
            limit_price=value.limit_price,
            maximum_amount=format(amount + Decimal("5.00"), ".2f"),
            remark_token=remark,
            engine_boot_id=engine.boot_id,
            permission_epoch=engine.snapshot().permission_epoch,
            target=ActionTarget(
                action_id=identity, target_remaining=value.quantity, rule_id="manual-buy-v1"
            ),
            manual_binding=binding,
        )
        self.previews[identity] = _Preview(session.session_key, item, monotonic() + 30)
        return {
            "preview_id": identity,
            "client_order_id": item.client_order_id,
            "symbol": value.symbol,
            "name": facts.name,
            "limit_price": value.limit_price,
            "quantity": value.quantity,
            "amount": format(amount, ".2f"),
            "reserved_amount": item.maximum_amount,
            "cash": facts.account.account.cash,
            "expires_at": (now + timedelta(seconds=30)).isoformat(),
            "sell_state": "RULES_NOT_CONFIGURED",
        }

    def confirm(
        self,
        session: AuthenticatedOperatorSession,
        identity: str,
    ) -> dict[str, JsonValue]:
        preview = self.previews.get(identity)
        if preview is None or preview.session_key != session.session_key:
            raise IPCError("MANUAL_BUY_PREVIEW_NOT_FOUND")
        if preview.confirmed:
            return self.result(session, preview.item.client_order_id)
        if monotonic() >= preview.expires:
            raise IPCError("MANUAL_BUY_PREVIEW_EXPIRED")
        self.engine.poll()
        if (
            not self.engine.critical_idle
            or not self.engine.dispatcher.idle
            or any(
                s.status in {"PERSISTING", "DISPATCHING"}
                for s in self.engine.snapshot().submissions
            )
        ):
            raise IPCError("MANUAL_BUY_BUSY")
        item = preview.item
        assert item.manual_binding is not None
        binding = item.manual_binding.model_copy(update={"confirmed_at": self.engine._now()})
        item = item.model_copy(
            update={
                "manual_binding": binding,
                "plan_hash": digest(canonical_bytes(binding.model_dump(mode="json"))),
            }
        )
        preview.item = item
        try:
            self.engine.authorize_manual(item, valid_until=min(preview.expires, monotonic() + 2))
        except ValueError:
            raise IPCError("MANUAL_BUY_REJECTED") from None
        # The same confirmation is consumed even if persistence/dispatch becomes uncertain.
        preview.confirmed = True
        try:
            self.engine.submit(item, valid_until=min(preview.expires, monotonic() + 2))
        except ValueError:
            if item.client_order_id not in self.engine._known:
                raise IPCError("MANUAL_BUY_REJECTED") from None
        return self.result(session, item.client_order_id)

    def result(
        self,
        session: AuthenticatedOperatorSession,
        identity: str,
    ) -> dict[str, JsonValue]:
        item = self.engine._known.get(identity)
        if item is None:
            engine = self.engine
            # A lost HTTP request is recoverable only after its confirmation window
            # is closed and a coherent checked account explains every local intent.
            engine._manual_watching_until = monotonic() + 5
            unexpired = any(
                p.item.client_order_id == identity and p.expires > monotonic()
                for p in self.previews.values()
            )
            broker = engine._broker_snapshot
            try:
                engine.check_manual_ready()
            except ValueError:
                raise IPCError("MANUAL_BUY_RESULT_UNKNOWN") from None
            if not unexpired and engine.critical_idle and broker is not None:
                try:
                    engine._check_observation(broker, engine._order_state)
                except ValueError:
                    raise IPCError("MANUAL_BUY_RESULT_UNKNOWN") from None
                return {"client_order_id": identity, "status": "NOT_SUBMITTED"}
            raise IPCError("MANUAL_BUY_RESULT_UNKNOWN")
        if item.manual_binding is None or item.manual_binding.actor_id != session.actor_id:
            raise IPCError("MANUAL_BUY_RESULT_UNKNOWN")
        states = {s.client_order_id: s.status for s in self.engine.snapshot().submissions}
        return {"client_order_id": identity, "status": states.get(identity, "RECOVERED")}

    def poll(self) -> None:
        """Seal owned acquisitions after close even when continuous buying is disabled."""
        engine = self.engine
        if self._closed or self.save_opening is None or engine._now().time() < time(15):
            return
        if not any(
            s.manual_binding is not None or s.manual_sell_binding is not None
            for s in engine._known.values()
        ):
            return
        if engine._sealed and self._closing is None:
            self._closed = True
            return
        try:
            if self._closing is not None:
                if self._closing.done():
                    self.save_opening(self._closing.result())
                    self._closed = True
                return
            if not self._closing_started:
                if not engine.critical_idle or not engine.dispatcher.idle:
                    return
                engine.start_recovery()
                self._closing_started = True
            elif engine.snapshot().state == "REVIEW_REQUIRED":
                self._closing = engine.seal_portfolio()
            elif engine.snapshot().state in {"RECOVERY_BLOCKED", "HALTED"}:
                raise ValueError("manual holdings closing recovery blocked")
        except (ValueError, RuntimeError, OSError):
            self._closed = True
            engine.emergency_halt("MANUAL_HOLDINGS_SEAL_FAILED")
