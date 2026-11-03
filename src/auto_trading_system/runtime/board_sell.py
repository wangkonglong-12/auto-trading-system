"""Owner-thread composition of independently controlled MAIN/CHINEXT exits.

Configuration and restored state are not authority. Enabling a board requires
an authenticated start intent and a durable SELL session grant.
All I/O through the critical store runs on its existing sole writer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from time import monotonic
from typing import Literal, cast
from uuid import uuid4

from pydantic import JsonValue

from auto_trading_system.control.approval_v2 import OneShotApprovals
from auto_trading_system.journal.critical_writer import CriticalStorePort
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.daily_sync import LocalControlRequest, SessionGrant
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.security.operator_session import AuthenticatedOperatorSession
from auto_trading_system.strategies.board_sell_configuration import (
    BoardSellConfigurationError,
    BoardSellConfigurationStore,
    BoardSellDraft,
)
from auto_trading_system.strategies.board_sell_engine import BoardSellEngine, SellRule
from auto_trading_system.strategies.entry_state import EntryInstrument
from auto_trading_system.strategies.exit_context import ExitLotContext, ExitSessionContext
from auto_trading_system.strategies.exit_owner import ExitQuote, ExitStateGate
from auto_trading_system.strategies.exit_state_store import validated_exit_history
from auto_trading_system.strategies.trading_settings import stock_board

Board = Literal["MAIN", "CHINEXT"]
BOARDS: tuple[Board, ...] = ("MAIN", "CHINEXT")


@dataclass
class _Start:
    authenticate: Callable[[], AuthenticatedOperatorSession]
    deadline: float
    preparation: Future[ExitSessionContext] | None = None
    enabling: bool = False


class BoardSellController:
    def __init__(
        self,
        engine: EngineKernel,
        store: BoardSellConfigurationStore,
        instrument_reader: Callable[[str, str], tuple[EntryInstrument, str]],
        clock: Callable[[], datetime],
        strategy_id: str,
        rules_by_board: Mapping[Board, tuple[SellRule, ...]] | None = None,
    ) -> None:
        self.engine, self.store, self.instrument_reader, self.clock = (
            engine,
            store,
            instrument_reader,
            clock,
        )
        self.strategy_id = strategy_id
        self.rules_by_board = dict(rules_by_board or {})
        self.owners: dict[Board, BoardSellEngine] = {}
        self.failures: dict[Board, str] = {}
        self._reads: dict[Board, Future[bytes | None]] = {}
        self._deadline = 0.0
        self._restored = False
        self._authorizing: dict[Board, tuple[str, str | None, float]] = {}
        self._previews: dict[str, tuple[str, Board, str, int, float]] = {}
        self._configuration: BoardSellDraft | None = None
        self._configuration_read: Future[BoardSellDraft | None | str] | None = None
        self._configuration_next = 0.0
        self._configuration_deadline = 0.0
        self._configuration_error: str | None = None
        self._starts: dict[Board, _Start] = {}
        self._start_results: dict[Board, str] = {}

    def start_authenticated(
        self,
        authenticate: Callable[[], AuthenticatedOperatorSession],
        boards: tuple[Board, ...] = BOARDS,
    ) -> dict[str, JsonValue]:
        """Queue explicit login/start intent, independently of any browser polling."""
        authenticate()
        for board in boards:
            if board in self._starts:
                # A fresh login supersedes an expired/logged-out session without
                # discarding the preparation or duplicating a durable control.
                self._starts[board].authenticate = authenticate
                self._starts[board].deadline = monotonic() + 30
            else:
                self._starts[board] = _Start(authenticate, monotonic() + 30)
                self._start_results[board] = "STARTING"
        return self.status()

    def _poll_starts(self) -> None:
        for board, start in tuple(self._starts.items()):
            try:
                session = start.authenticate()
                if monotonic() >= start.deadline:
                    raise ValueError("BOARD_SELL_START_TIMEOUT")
                permission = self.engine.permission.snapshot()
                if permission.halted and permission.reason not in {
                    "STARTUP_PAUSED",
                    "RECOVERY_REQUESTED",
                }:
                    raise ValueError("BOARD_SELL_GLOBAL_PAUSED")
                now = self.clock()
                if now.date() not in self.engine._calendar.sessions or now.time() >= time(14, 55):
                    raise ValueError("BOARD_SELL_OUTSIDE_SESSION")
                failure = self.failures.get(board)
                if failure and failure != "BOARD_SELL_AUTHORIZATION_FAILED":
                    raise ValueError(failure)
                if not self._restored:
                    continue
                if self._configuration_error:
                    raise ValueError(self._configuration_error)
                if self._configuration is None:
                    if self._configuration_read is not None:
                        continue
                    raise ValueError("BOARD_SELL_CONFIGURATION_REQUIRED")
                owner = self.owners.get(board)
                if owner is None:
                    if start.preparation is None:
                        start.preparation = self.prepare(board)
                    if not start.preparation.done():
                        continue
                    self.accept_prepared(start.preparation.result())
                    continue
                if board in self._authorizing:
                    continue
                grant = self.engine._board_exit_grants.get(owner.context.payload_hash)
                if (
                    grant is not None
                    and not permission.halted
                    and grant.valid_from <= now < grant.valid_until
                    and owner.enabled
                ):
                    self._start_results[board] = "STARTED"
                    self._starts.pop(board)
                    continue
                if start.enabling:
                    raise ValueError("BOARD_SELL_AUTHORIZATION_FAILED")
                state = owner.snapshot().state
                if state == "FAILED":
                    raise ValueError("BOARD_SELL_ENGINE_FAILED")
                if state != "READY" or not owner.configuration_ready:
                    continue
                self._enable(session, board)
                start.enabling = True
            except Exception as error:
                code = getattr(error, "code", str(error))
                if not isinstance(code, str) or not code.startswith(("BOARD_SELL_", "OPERATOR_")):
                    code = "BOARD_SELL_START_FAILED"
                self._start_results[board] = code
                self._starts.pop(board)
                if start.enabling:
                    self._authorizing.pop(board, None)
                    self.owners[board].set_enabled(False)

    def _poll_configuration(self) -> None:
        """Read atomic saved settings off the owner thread; apply between decisions."""
        pending = self._configuration_read
        if pending is not None:
            try:
                if not pending.done():
                    if monotonic() < self._configuration_deadline:
                        return
                    raise ValueError("configuration read timed out")
                draft = pending.result()
                if isinstance(draft, str):
                    raise ValueError(draft)
                if draft is None and self.owners:
                    raise ValueError("prepared owners require saved configuration")
                if draft is not None:
                    if (
                        not self.engine.critical_idle
                        or any(owner._pending is not None for owner in self.owners.values())
                        or any(
                            s.status in {"PERSISTING", "DISPATCHING"}
                            for s in self.engine.snapshot().submissions
                        )
                    ):
                        return
                    if self._configuration and (
                        draft.version < self._configuration.version
                        or (
                            draft.version == self._configuration.version
                            and draft != self._configuration
                        )
                    ):
                        raise ValueError("configuration version moved backwards")
                    if draft != self._configuration:
                        self._previews.clear()
                    sources = self.engine._scan.lot_sources if self.engine._scan else {}
                    for owner in self.owners.values():
                        for record in owner.gate.latest.values():
                            marker = (record.state.rule_states or {}).get("BOARD_CONFIGURATION")
                            if marker and int(str(marker["version"])) > draft.version:
                                raise ValueError("configuration predates persisted decisions")
                        if owner.configuration != draft or not owner.configuration_ready:
                            owner.apply_configuration(draft, sources)
                    self._configuration = draft
                self._configuration_error = None
            except Exception:
                self._configuration_error = "BOARD_SELL_CONFIGURATION_UNAVAILABLE"
                self._previews.clear()
                for owner in self.owners.values():
                    owner.configuration_ready = False
            finally:
                if pending.done() or monotonic() >= self._configuration_deadline:
                    self._configuration_read = None
        if self._configuration_read is None and monotonic() >= self._configuration_next:
            self._configuration_next = monotonic() + 0.25
            self._configuration_deadline = monotonic() + 5

            def read(_: CriticalStorePort) -> BoardSellDraft | None | str:
                # Invalid operator settings are not a failure of the trading journal.
                try:
                    return self.store.read()
                except BoardSellConfigurationError as error:
                    return str(error)

            self._configuration_read = self.engine.journal.request(
                read, deadline=self._configuration_deadline
            )

    def _install(self, context: ExitSessionContext) -> BoardSellEngine:
        assert context.board is not None
        gate = ExitStateGate(context, clock=self.clock)
        # The switch is checked again at J1 admission and immediately before SDK dispatch.
        owner_ref: list[BoardSellEngine] = []
        self.engine.install_board_exit(
            context, lambda item: bool(owner_ref and owner_ref[0].allows(item))
        )
        owner = BoardSellEngine(self.engine, gate, rules=self.rules_by_board.get(context.board))
        owner_ref.append(owner)
        owner.configuration_ready = False
        if self._configuration is not None and self._configuration_error is None:
            owner.apply_configuration(
                self._configuration, self.engine._scan.lot_sources if self.engine._scan else {}
            )
        self._configuration_next = 0.0
        self.owners[context.board] = owner
        return owner

    def poll(self) -> None:
        snapshot = self.engine.snapshot()
        if not self._restored:
            self._poll_starts()
            if snapshot.recovery is None or snapshot.recovery.status != "CLEAR":
                return
            if not self._reads:
                self._deadline = monotonic() + 5
                for board in BOARDS:
                    key = f"exit-context:{self.engine.trade_date}:{board}"

                    def read(store: CriticalStorePort, key: str = key) -> bytes | None:
                        return store.read_evidence("recovery_checkpoints", key)

                    self._reads[board] = self.engine.journal.request(
                        read,
                        deadline=self._deadline,
                    )
            if not all(f.done() for f in self._reads.values()):
                if monotonic() >= self._deadline:
                    self.failures.update(dict.fromkeys(BOARDS, "BOARD_SELL_RESTORE_TIMEOUT"))
                    self._restored = True
                return
            for board, future in self._reads.items():
                try:
                    body = future.result()
                    if body is not None:
                        context = ExitSessionContext.model_validate_json(body)
                        if context.board != board:
                            raise ValueError("board context identity mismatch")
                        self._install(context)
                except Exception:
                    self.failures[board] = "BOARD_SELL_RESTORE_FAILED"
            self._restored = True
        self._poll_configuration()
        self._poll_starts()
        for board in tuple(self._authorizing):
            try:
                self._authorize(board)
            except (ValueError, RuntimeError):
                self.owners[board].set_enabled(False)
                self.failures[board] = "BOARD_SELL_AUTHORIZATION_FAILED"
                self._authorizing.pop(board, None)
        for owner in self.owners.values():
            owner.poll()

    def _authorize(self, board: Board) -> None:
        actor, request_id, deadline = self._authorizing[board]
        engine, now = self.engine, self.clock()
        owner = self.owners[board]
        if monotonic() >= deadline or not owner.enabled:
            raise ValueError("BOARD_SELL_AUTHORIZATION_EXPIRED")
        if request_id is not None:
            result = engine.control_result(request_id)
            if result is None or result.status == "PENDING":
                return
            if result.status != "APPLIED":
                raise ValueError("BOARD_SELL_AUTHORIZATION_FAILED")
        snapshot = engine.snapshot()
        grant = engine._grant
        if (
            grant is not None
            and "SELL" in grant.directions
            and not snapshot.halted
            and now < grant.valid_until
        ):
            engine.retain_board_sell_grant(owner.context.payload_hash)
            self._authorizing.pop(board)
            return
        if snapshot.state in {"RECOVERING", "CONTROL_PERSISTING"} or engine._pending is not None:
            return
        report = snapshot.recovery
        if report is None or report.status != "CLEAR" or not snapshot.halted:
            raise ValueError("BOARD_SELL_RECOVERY_REQUIRED")
        if not engine._report_current():
            if snapshot.state != "RECOVERING":
                engine.start_recovery()
            return
        action: Literal["ARM", "CONFIRM_RECOVERY"] = (
            "ARM" if snapshot.state == "READY_PAUSED" else "CONFIRM_RECOVERY"
        )
        sale_grant = None
        if action == "ARM":
            sale_grant = SessionGrant(
                grant_id="board-grant-" + uuid4().hex,
                account_id=engine.account_id,
                engine_boot_id=engine.boot_id,
                session_generation=engine.next_session_generation,
                mode=engine.limits.mode,
                directions=("SELL",),
                max_buy_value="0",
                max_order_value=owner.context.max_order_value,
                max_positions=engine.limits.max_positions,
                valid_from=now,
                valid_until=min(c.valid_until for c in engine.board_exit_contexts.values()),
                confirmed_by=actor,
                recovery_report_hash=report.payload_hash,
            )
        control = LocalControlRequest(
            request_id="board-control-" + uuid4().hex,
            engine_boot_id=engine.boot_id,
            sender_role="agent",
            deadline=now + timedelta(seconds=10),
            expected_state_version=snapshot.state_version,
            action=action,
            grant=sale_grant,
            artifact_hash=report.payload_hash if action == "CONFIRM_RECOVERY" else None,
            operator_authorization_id="board-approval-" + uuid4().hex,
        )
        engine.preview_control(control)
        approvals = engine.approvals
        if not isinstance(approvals, OneShotApprovals):
            raise ValueError("BOARD_SELL_AUTHORIZATION_UNAVAILABLE")
        approvals.approve(control, authenticated_actor=actor)
        if engine.control(control).status != "PENDING":
            raise ValueError("BOARD_SELL_AUTHORIZATION_FAILED")
        self._authorizing[board] = (actor, control.request_id, deadline)

    def quote(self, quote: ExitQuote) -> None:
        board = stock_board(quote.symbol)
        owner = self.owners.get(cast(Board, board)) if board in BOARDS else None
        if owner and any(p.symbol == quote.symbol for p in owner.context.lots):
            previous = owner.gate.quotes.get(quote.symbol)
            if previous is None or quote.sequence > previous.sequence:
                owner.quote(quote)

    def prepare(self, board: Board) -> Future[ExitSessionContext]:
        """Freeze an explicit takeover candidate, without enabling either board."""
        engine, now = self.engine, self.clock()
        if not self._restored or board in self.failures:
            raise ValueError("BOARD_SELL_NOT_READY")
        if board in self.owners:
            raise ValueError("BOARD_SELL_ALREADY_PREPARED")
        if engine.exit_context is not None:
            raise ValueError("BOARD_SELL_LEGACY_CONFLICT")
        draft = self.store.read()
        if draft is None:
            raise ValueError("BOARD_SELL_CONFIGURATION_REQUIRED")
        if now.date() not in engine._calendar.sessions or now.time() >= time(14, 55):
            raise ValueError("BOARD_SELL_OUTSIDE_SESSION")
        portfolio, _, _ = engine.exit_inputs()
        sources = engine._scan.lot_sources if engine._scan else {}
        settings = draft.settings
        lots = tuple(
            lot
            for lot in portfolio.lots
            if lot.remaining
            and lot.strategy_id == self.strategy_id
            and lot.eligible_sell_date <= now.date()
            and stock_board(lot.symbol) == board
            and lot.cost_price is not None
        )
        if not lots:
            raise ValueError("BOARD_SELL_NO_ELIGIBLE_LOTS")
        policies = []
        for lot in lots:
            assert lot.cost_price is not None
            rule, _ = self.instrument_reader(lot.symbol, lot.cost_price or "0")
            policies.append(
                ExitLotContext(
                    lot_id=lot.lot_id,
                    symbol=lot.symbol,
                    cost=lot.cost_price,
                    previous_close=lot.cost_price,
                    open_price=lot.cost_price,
                    price_tick=rule.price_tick,
                    lower_price=rule.lower_price,
                    upper_price=rule.upper_price,
                    sell_step=100,
                    allow_odd_liquidation=True,
                    source_hash=rule.source_hash,
                )
            )
        opening_hash = engine._opening_hash
        if opening_hash is None:
            raise ValueError("BOARD_SELL_OPENING_REQUIRED")
        context = ExitSessionContext(
            context_id="board-exit-" + uuid4().hex,
            account_id=engine.account_id,
            strategy_id=lots[0].strategy_id,
            trade_date=engine.trade_date,
            opening_hash=opening_hash,
            calendar_hash=digest(canonical_bytes(engine._calendar.model_dump(mode="json"))),
            rule_id="BOARD_MAIN_EXIT_V1" if board == "MAIN" else "BOARD_CHINEXT_EXIT_V1",
            board=board,
            board_rule=settings.main if board == "MAIN" else settings.chinext,
            configuration_version=draft.version,
            issued_at=now,
            valid_from=max(now, now.replace(hour=9, minute=30, second=0, microsecond=0)),
            stop_new_at=now.replace(hour=14, minute=55, second=0, microsecond=0),
            cancel_by=now.replace(hour=14, minute=55, second=20, microsecond=0),
            valid_until=now.replace(hour=14, minute=55, second=30, microsecond=0),
            max_order_value=engine.limits.max_order_value,
            max_order_quantity=engine.limits.max_order_quantity,
            cancel_after_seconds=settings.cancel_after_seconds,
            max_children=settings.max_reposts + 1,
            lots=tuple(policies),
        )
        # Recover prior decisions even when a trigger had not yet produced a J1 order.
        sessions = tuple(
            d
            for d in engine._calendar.sessions
            if min(lot.trade_date_acquired for lot in lots) < d < now.date()
        )

        def carry(store: CriticalStorePort) -> ExitSessionContext:
            remaining = {p.lot_id: p for p in policies}
            restored: dict[str, ExitLotContext] = {}
            for day in reversed(sessions):
                body = store.read_evidence("recovery_checkpoints", f"exit-context:{day}:{board}")
                if body is None:
                    continue
                previous = ExitSessionContext.model_validate_json(body)
                if (
                    previous.account_id != engine.account_id
                    or previous.strategy_id != self.strategy_id
                    or previous.board != board
                    or previous.trade_date != day
                ):
                    raise ValueError("BOARD_SELL_HISTORY_INVALID")
                history = validated_exit_history(store, previous)
                latest = {r.lot_id: r.state for r in history}
                for old in previous.lots:
                    if old.lot_id not in remaining:
                        continue
                    current = remaining.pop(old.lot_id)
                    state = latest.get(old.lot_id)
                    restored[old.lot_id] = current.model_copy(
                        update={
                            "board_rule": old.board_rule or previous.board_rule,
                            "configuration_version": old.configuration_version
                            or previous.configuration_version,
                            "prior_target_remaining": state.target_remaining
                            if state
                            else old.prior_target_remaining,
                            "prior_rule_states": state.rule_states
                            if state
                            else old.prior_rule_states,
                            "prior_fired_rules": state.fired_rules
                            if state
                            else old.prior_fired_rules,
                        }
                    )
                if not remaining:
                    break
            selected = tuple(
                restored.get(p.lot_id, p)
                for p in policies
                if p.lot_id in restored
                or (sources.get(p.lot_id) == "AUTOMATIC" and settings.include_automatic)
                or (sources.get(p.lot_id) == "SYSTEM_MANUAL" and settings.include_system_manual)
            )
            if not selected:
                raise ValueError("BOARD_SELL_NO_ELIGIBLE_LOTS")
            return context.model_copy(update={"lots": selected})

        return engine.journal.request(carry, deadline=monotonic() + 5)

    def accept_prepared(self, context: ExitSessionContext) -> None:
        self._install(context)

    def preview(self, session: AuthenticatedOperatorSession, board: Board) -> dict[str, JsonValue]:
        self._previews = {k: p for k, p in self._previews.items() if p[4] > monotonic()}
        if len(self._previews) >= 32:
            raise ValueError("BOARD_SELL_PREVIEW_CAPACITY")
        owner = self.owners.get(board)
        if owner is None or owner.snapshot().state != "READY" or not owner.configuration_ready:
            raise ValueError("BOARD_SELL_NOT_READY")
        identity = "board-sell-" + uuid4().hex
        phrase = f"确认开启{'主板' if board == 'MAIN' else '创业板'}自动卖出 {identity[-8:]}"
        self._previews[identity] = (
            session.session_key,
            board,
            phrase,
            self.engine.snapshot().permission_epoch,
            monotonic() + 30,
        )
        return {
            "preview_id": identity,
            "confirmation_phrase": phrase,
            "context": {
                **owner.context.model_dump(mode="json", exclude_none=True),
                "configuration_version": owner.configuration.version
                if owner.configuration
                else owner.context.configuration_version,
                "board_rule": owner.rule_for(owner.context.lots[0]).model_dump(mode="json"),
            },
            "requires_sell_grant": False,
            "authorization_until": owner.context.valid_until.isoformat(),
        }

    def confirm(
        self, session: AuthenticatedOperatorSession, identity: str, phrase: str
    ) -> dict[str, JsonValue]:
        preview = self._previews.pop(identity, None)
        if (
            preview is None
            or preview[0] != session.session_key
            or preview[2] != phrase
            or preview[4] <= monotonic()
        ):
            raise ValueError("BOARD_SELL_CONFIRMATION_INVALID")
        if preview[3] != self.engine.snapshot().permission_epoch:
            raise ValueError("BOARD_SELL_STATE_CHANGED")
        self._enable(session, preview[1])
        return self.status()

    def _enable(self, session: AuthenticatedOperatorSession, board: Board) -> None:
        owner = self.owners[board]
        current = self.engine._grant
        if (
            current is not None
            and not self.engine.snapshot().halted
            and "SELL" not in current.directions
        ):
            raise ValueError("BOARD_SELL_BUY_SESSION_ACTIVE")
        if self.clock() >= owner.context.stop_new_at:
            raise ValueError("BOARD_SELL_OUTSIDE_SESSION")
        owner.set_enabled(True)
        if self.failures.get(board) == "BOARD_SELL_AUTHORIZATION_FAILED":
            self.failures.pop(board)
        self._authorizing[board] = (session.actor_id, None, monotonic() + 30)

    def pause(self, board: Board) -> dict[str, JsonValue]:
        self._starts.pop(board, None)
        self._start_results[board] = "PAUSED"
        self._authorizing.pop(board, None)
        if board in self.owners:
            self.owners[board].set_enabled(False)
        return self.status()

    def status(self) -> dict[str, JsonValue]:
        grant = self.engine._grant
        authorized = bool(
            grant
            and "SELL" in grant.directions
            and not self.engine.snapshot().halted
            and grant.valid_from <= self.clock() < grant.valid_until
        )
        boards: dict[str, JsonValue] = {}
        for board in BOARDS:
            owner = self.owners.get(board)
            board_grant = (
                self.engine._board_exit_grants.get(owner.context.payload_hash) if owner else None
            )
            board_authorized = bool(
                board_grant
                and not self.engine.snapshot().halted
                and board_grant.valid_from <= self.clock() < board_grant.valid_until
            )
            startup_status = self._start_results.get(board)
            if startup_status == "STARTED":
                if self.engine.snapshot().halted:
                    startup_status = "BOARD_SELL_GLOBAL_PAUSED"
                elif owner is None or not owner.enabled:
                    startup_status = "BOARD_SELL_ENGINE_FAILED"
                elif self._configuration_error:
                    startup_status = self._configuration_error
                elif not board_authorized:
                    startup_status = "BOARD_SELL_AUTHORIZATION_FAILED"
            boards[board] = {
                "startup_status": startup_status,
                "enabled": bool(owner and owner.enabled),
                "authorized": bool(owner and (board_authorized or authorized)),
                "state": (self._configuration_error if owner else None)
                or self.failures.get(board)
                or (
                    "AUTHORIZING"
                    if board in self._authorizing
                    else owner.snapshot().state
                    if owner
                    else "UNPREPARED"
                ),
                "configuration_version": (
                    owner.configuration.version
                    if owner.configuration
                    else owner.context.configuration_version
                )
                if owner
                else None,
                "context_hash": owner.context.payload_hash if owner else None,
                "targets": [
                    {
                        "lot_id": p.lot_id,
                        "symbol": p.symbol,
                        "configuration_version": owner.configuration.version
                        if owner.configuration
                        else p.configuration_version or owner.context.configuration_version,
                        "rule": owner.rule_for(p).model_dump(mode="json"),
                        "target_remaining": owner.gate.latest[p.lot_id].state.target_remaining
                        if p.lot_id in owner.gate.latest
                        else None,
                        "waiting_reason": owner.waiting_reasons.get(p.lot_id)
                        if owner.includes(p.lot_id)
                        else "SOURCE_EXCLUDED",
                        "reasons": list(owner.gate.latest[p.lot_id].state.fired_rules)
                        if p.lot_id in owner.gate.latest
                        else [],
                    }
                    for p in owner.context.lots
                ]
                if owner
                else [],
            }
        return {"supported": True, "restored": self._restored, "boards": boards}
