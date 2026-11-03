"""Interactive local approvals, isolated from the emergency-control pipe.

Password work runs on the authorization pipe worker. Only short, authenticated
operations cross the owner mailbox. Start intents or exact previews are checked
on the engine owner; a pipe role or caller-supplied actor never grants authority.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic, time_ns
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from pydantic import Field, JsonValue, SecretStr, model_validator

from auto_trading_system.config.credentials import SecretLoadError
from auto_trading_system.control.approval_v2 import OneShotApprovals
from auto_trading_system.control.ipc_protocol import IPCError, Request
from auto_trading_system.control.owner_mailbox import OwnerMailbox
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.daily_sync import LocalControlRequest, SessionGrant
from auto_trading_system.protocol.v2.records import MoneyText
from auto_trading_system.runtime.debug_pick import (
    DebugPickConfirm,
    DebugPickController,
    DebugPickInput,
)
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.runtime.execution_scope import execution_scope
from auto_trading_system.runtime.manual_buy import (
    ManualBuyConfirm,
    ManualBuyController,
    ManualBuyInput,
    ManualBuyResult,
    ManualFacts,
)
from auto_trading_system.runtime.manual_sell import (
    ManualSellController,
    ManualSellInput,
    ManualSellLookup,
    ManualSellOperation,
)
from auto_trading_system.security.operator_session import (
    AuthenticatedOperatorSession,
    LocalOperatorSessionManager,
    OperatorSessionError,
)
from auto_trading_system.strategies.exit_context import ExitSessionContext

if TYPE_CHECKING:
    from auto_trading_system.runtime.board_sell import BoardSellController
    from auto_trading_system.runtime.daily_auto import DailyAutoController

AUTH_ACTIONS = frozenset(
    {
        "PREPARE_BOARD_SELL",
        "BOARD_SELL_STATUS",
        "PREVIEW_BOARD_SELL",
        "CONFIRM_BOARD_SELL",
        "PAUSE_BOARD_SELL",
        "START_BOARD_SELL",
        "HELLO",
        "LOGIN",
        "SESSION",
        "LOGOUT",
        "CHANGE_PASSWORD",
        "PREVIEW_CONTROL",
        "CONFIRM_CONTROL",
        "CONTROL_RESULT",
        "DAILY_AUTO_STATUS",
        "PREVIEW_DAILY_AUTO",
        "CONFIRM_DAILY_AUTO",
        "DISABLE_DAILY_AUTO",
        "PREVIEW_DEBUG_PICK",
        "CONFIRM_DEBUG_PICK",
        "PREVIEW_MANUAL_BUY",
        "CONFIRM_MANUAL_BUY",
        "MANUAL_BUY_RESULT",
        "PREVIEW_MANUAL_SELL",
        "CONFIRM_MANUAL_SELL",
        "MANUAL_SELL_RESULT",
    }
)


class GrantProposal(WireModel):
    directions: tuple[Literal["BUY", "SELL"], ...] = Field(
        min_length=1, max_length=2, description="申请方向"
    )
    max_buy_value: MoneyText = Field(description="会话累计买入金额上限，元")
    max_order_value: MoneyText = Field(description="单笔金额上限，元")
    max_positions: int = Field(ge=0, le=15, description="账户持仓标的上限")
    duration_seconds: int = Field(ge=1, le=900, description="本次交易授权有效秒数，最多15分钟")

    @model_validator(mode="after")
    def unique(self) -> GrantProposal:
        if len(set(self.directions)) != len(self.directions):
            raise ValueError("duplicate grant direction")
        return self


class PreviewProposal(WireModel):
    action: Literal["CONFIRM_RECOVERY", "ARM", "RESUME"] = Field(description="待人工确认的具体动作")
    grant: GrantProposal | None = Field(default=None, description="ARM/RESUME的额度与期限申请")

    @model_validator(mode="after")
    def match(self) -> PreviewProposal:
        if (self.action != "CONFIRM_RECOVERY") != (self.grant is not None):
            raise ValueError("grant must match the proposed action")
        return self


@dataclass
class _Preview:
    request: LocalControlRequest
    session_key: str
    actor: str
    phrase: str
    permission_epoch: int
    expires: float
    confirmed: bool = False


@dataclass
class _AutoPreview:
    session_key: str
    phrase: str
    settings_hash: str
    permission_epoch: int
    expires: float
    confirmed: bool = False


class NodeAuthorization:
    """One pipe worker owns authentication; one engine owner owns all previews."""

    def __init__(
        self,
        engine: EngineKernel,
        mailbox: OwnerMailbox[object],
        sessions: LocalOperatorSessionManager,
        *,
        clock: Callable[[], datetime],
        elapsed: Callable[[], float] = monotonic,
        capacity: int = 128,
        password_writer: Callable[[str], None] | None = None,
        daily_auto: DailyAutoController | None = None,
        debug_pick: DebugPickController | None = None,
        manual_buy: ManualBuyController | None = None,
        manual_sell: ManualSellController | None = None,
        board_sell: BoardSellController | None = None,
    ) -> None:
        if not isinstance(engine.approvals, OneShotApprovals) or not 1 <= capacity <= 256:
            raise ValueError("authorization composition invalid")
        self.engine, self.mailbox, self.sessions = engine, mailbox, sessions
        self.approvals = engine.approvals
        self.clock, self.elapsed, self.capacity = clock, elapsed, capacity
        self._previews: dict[str, _Preview] = {}
        self.password_writer = password_writer
        self.daily_auto = daily_auto
        self.debug_pick = debug_pick
        self.manual_buy = manual_buy
        self.manual_sell = manual_sell
        self.board_sell = board_sell
        self._auto_previews: dict[str, _AutoPreview] = {}

    def handle(self, message: Request) -> dict[str, JsonValue]:
        if message.sender_role != "operator":
            raise IPCError("ROLE_DENIED")
        value = dict(message.payload)
        try:
            if message.action == "LOGIN":
                if (
                    set(value) not in ({"password"}, {"password", "start_board_sell"})
                    or not isinstance(value.get("password"), str)
                    or type(value.get("start_board_sell", False)) is not bool
                ):
                    raise IPCError("INVALID_ARGUMENTS")
                password = cast(str, value["password"])
                if len(password) > 128:
                    raise IPCError("INVALID_ARGUMENTS")
                issued = self.sessions.login(SecretStr(password))
                if value.get("start_board_sell") and self.board_sell is not None:

                    def start_selling() -> dict[str, JsonValue]:
                        if time_ns() // 1_000_000 >= message.deadline_ms:
                            raise IPCError("REQUEST_EXPIRED")
                        assert self.board_sell is not None
                        return self.board_sell.start_authenticated(
                            lambda: self.sessions.authorize_mutation(
                                issued.session_token, issued.csrf_token
                            )
                        )

                    self.mailbox.call(start_selling, timeout=2)
                return {
                    "session_token": issued.session_token,
                    "csrf_token": issued.csrf_token,
                    "actor_id": issued.actor_id,
                    "expires_at": issued.expires_at.to_wire(),
                    "issued_at": issued.issued_at.to_wire(),
                }
            if message.action == "SESSION":
                if set(value) != {"session_token", "csrf_token"} or not all(
                    isinstance(item, str) for item in value.values()
                ):
                    raise IPCError("INVALID_ARGUMENTS")
                session = self.sessions.authorize_mutation(
                    str(value["session_token"]), str(value["csrf_token"])
                )
                return {
                    "actor_id": session.actor_id,
                    "issued_at": session.issued_at.to_wire(),
                    "expires_at": session.expires_at.to_wire(),
                }
            token, csrf = value.pop("session_token", None), value.pop("csrf_token", None)
            if not isinstance(token, str) or not isinstance(csrf, str):
                raise IPCError("OPERATOR_SESSION_REQUIRED")
            # Validate once on receipt, then again when the queued owner operation
            # executes. A timeout/expired login never becomes a delayed grant.
            self.sessions.authorize_mutation(token, csrf)
            prepared_board = None
            if message.action == "PREPARE_BOARD_SELL":
                if (
                    self.board_sell is None
                    or set(value) != {"board"}
                    or value["board"] not in {"MAIN", "CHINEXT"}
                ):
                    raise IPCError("INVALID_ARGUMENTS")
                board = cast(Literal["MAIN", "CHINEXT"], value["board"])
                manager_board = self.board_sell

                def prepare_board() -> Future[ExitSessionContext]:
                    self.sessions.authorize_mutation(token, csrf)
                    return manager_board.prepare(board)

                future_board = cast(
                    Future[ExitSessionContext], self.mailbox.call(prepare_board, timeout=2)
                )
                prepared_board = future_board.result(timeout=5)
            manual_facts = None
            sell_facts = None
            sell_operation = None
            if message.action in {
                "PREVIEW_MANUAL_SELL",
                "CONFIRM_MANUAL_SELL",
                "MANUAL_SELL_RESULT",
            }:
                seller = self.manual_sell
                if seller is None:
                    raise IPCError("MANUAL_SELL_UNAVAILABLE")
                if message.action != "MANUAL_SELL_RESULT" and not seller.settings_current():
                    raise IPCError("MANUAL_SELL_SETTINGS_CHANGED")
                session = self.sessions.authorize_mutation(token, csrf)
                if message.action == "MANUAL_SELL_RESULT":
                    sale_lookup = ManualSellLookup.model_validate_json(canonical_bytes(value))
                    read = cast(
                        Future[bytes | None],
                        self.mailbox.call(
                            lambda: seller.lookup(sale_lookup.operation_id), timeout=2
                        ),
                    )
                    try:
                        body = read.result(timeout=2)
                        sell_operation = (
                            ManualSellOperation.model_validate_json(body) if body else None
                        )
                    except Exception:
                        raise IPCError("MANUAL_SELL_RESULT_UNKNOWN") from None
                else:
                    sell_query: Callable[[], Future[ManualFacts] | None]
                    if message.action == "PREVIEW_MANUAL_SELL":
                        sell_input = ManualSellInput.model_validate_json(canonical_bytes(value))
                    else:
                        sale_lookup = ManualSellLookup.model_validate_json(canonical_bytes(value))

                    def sell_query() -> Future[ManualFacts] | None:
                        self.sessions.authorize_mutation(token, csrf)
                        if message.action == "PREVIEW_MANUAL_SELL":
                            return seller.query(sell_input)
                        return seller.query_confirmation(session, sale_lookup.operation_id)

                    future_sale = cast(
                        Future[ManualFacts] | None, self.mailbox.call(sell_query, timeout=2)
                    )
                    if future_sale is not None:
                        try:
                            sell_facts = future_sale.result(timeout=3)
                        except Exception:
                            raise IPCError("MANUAL_SELL_INSTRUMENT_UNAVAILABLE") from None
            if message.action == "PREVIEW_DEBUG_PICK":
                if self.debug_pick is None or self.manual_buy is None:
                    raise IPCError("DEBUG_PICK_UNAVAILABLE")
                debug_input = DebugPickInput.model_validate_json(canonical_bytes(value))

                def query_debug() -> Future[ManualFacts]:
                    self.sessions.authorize_mutation(token, csrf)
                    assert self.debug_pick is not None and self.manual_buy is not None
                    self.debug_pick.check()
                    return self.manual_buy.query(
                        ManualBuyInput(symbol=debug_input.symbol, limit_price="1", quantity=100)
                    )

                debug_future = cast(Future[ManualFacts], self.mailbox.call(query_debug, timeout=2))
                try:
                    debug_facts = debug_future.result(timeout=3)
                except Exception:
                    raise IPCError("MANUAL_BUY_INSTRUMENT_UNAVAILABLE") from None
            if message.action in {"PREVIEW_MANUAL_BUY", "CONFIRM_MANUAL_BUY", "MANUAL_BUY_RESULT"}:
                manager = self.manual_buy
                if manager is None:
                    raise IPCError("MANUAL_BUY_UNAVAILABLE")
                if message.action != "MANUAL_BUY_RESULT" and not manager.settings_current():
                    raise IPCError("MANUAL_BUY_SETTINGS_CHANGED")
                if message.action == "PREVIEW_MANUAL_BUY":
                    manual_input = ManualBuyInput.model_validate_json(canonical_bytes(value))

                    def query_manual() -> object:
                        self.sessions.authorize_mutation(token, csrf)
                        return manager.query(manual_input)

                    future = cast(Future[ManualFacts], self.mailbox.call(query_manual, timeout=2))
                    try:
                        manual_facts = future.result(timeout=3)
                    except Exception:
                        raise IPCError("MANUAL_BUY_INSTRUMENT_UNAVAILABLE") from None
            if message.action == "CHANGE_PASSWORD":
                if set(value) != {"current_password", "new_password"} or not all(
                    isinstance(item, str) and 1 <= len(item) <= 128 for item in value.values()
                ):
                    raise IPCError("INVALID_ARGUMENTS")
                if self.password_writer is None:
                    raise IPCError("PASSWORD_CHANGE_UNAVAILABLE")
                try:
                    self.sessions.change_password(
                        token,
                        csrf,
                        SecretStr(str(value["current_password"])),
                        SecretStr(str(value["new_password"])),
                        self.password_writer,
                    )
                except ValueError:
                    raise IPCError("INVALID_ARGUMENTS") from None
                return {"password_changed": True}
            if message.action == "LOGOUT":
                if value:
                    raise IPCError("INVALID_ARGUMENTS")
                self.sessions.logout(token, csrf)
                return {"logged_out": True}

            def operation() -> dict[str, JsonValue]:
                session = self.sessions.authorize_mutation(token, csrf)
                if message.action in {
                    "PREPARE_BOARD_SELL",
                    "BOARD_SELL_STATUS",
                    "PREVIEW_BOARD_SELL",
                    "CONFIRM_BOARD_SELL",
                    "PAUSE_BOARD_SELL",
                    "START_BOARD_SELL",
                }:
                    if self.board_sell is None:
                        raise IPCError("BOARD_SELL_UNAVAILABLE")
                    manager_board = self.board_sell
                    if message.action == "PREPARE_BOARD_SELL":
                        assert prepared_board is not None
                        manager_board.accept_prepared(prepared_board)
                        return manager_board.status()
                    if message.action == "BOARD_SELL_STATUS":
                        if value:
                            raise IPCError("INVALID_ARGUMENTS")
                        return manager_board.status()
                    if message.action == "CONFIRM_BOARD_SELL":
                        if set(value) != {"preview_id", "phrase"} or not all(
                            isinstance(v, str) for v in value.values()
                        ):
                            raise IPCError("INVALID_ARGUMENTS")
                        return manager_board.confirm(
                            session, str(value["preview_id"]), str(value["phrase"])
                        )
                    if set(value) != {"board"} or value["board"] not in {"MAIN", "CHINEXT"}:
                        raise IPCError("INVALID_ARGUMENTS")
                    board = cast(Literal["MAIN", "CHINEXT"], value["board"])
                    if message.action == "START_BOARD_SELL":
                        return manager_board.start_authenticated(
                            lambda: self.sessions.authorize_mutation(token, csrf), (board,)
                        )
                    return (
                        manager_board.pause(board)
                        if message.action == "PAUSE_BOARD_SELL"
                        else manager_board.preview(session, board)
                    )
                if message.action == "PREVIEW_DEBUG_PICK":
                    assert self.debug_pick is not None
                    return self.debug_pick.preview(session, debug_input, debug_facts)
                if message.action == "CONFIRM_DEBUG_PICK":
                    if self.debug_pick is None:
                        raise IPCError("DEBUG_PICK_UNAVAILABLE")
                    return self.debug_pick.confirm(
                        session, DebugPickConfirm.model_validate_json(canonical_bytes(value))
                    )
                if message.action == "PREVIEW_MANUAL_SELL":
                    assert self.manual_sell is not None and sell_facts is not None
                    return self.manual_sell.preview(session, sell_input, sell_facts)
                if message.action == "CONFIRM_MANUAL_SELL":
                    assert self.manual_sell is not None
                    return self.manual_sell.confirm(session, sale_lookup.operation_id, sell_facts)
                if message.action == "MANUAL_SELL_RESULT":
                    assert self.manual_sell is not None
                    return self.manual_sell.result(
                        session, sale_lookup.operation_id, sell_operation
                    )
                if message.action == "PREVIEW_MANUAL_BUY":
                    assert self.manual_buy is not None and manual_facts is not None
                    return self.manual_buy.preview(session, manual_input, manual_facts)
                if message.action == "CONFIRM_MANUAL_BUY":
                    assert self.manual_buy is not None
                    confirmation = ManualBuyConfirm.model_validate_json(canonical_bytes(value))
                    return self.manual_buy.confirm(session, confirmation.preview_id)
                if message.action == "MANUAL_BUY_RESULT":
                    assert self.manual_buy is not None
                    lookup = ManualBuyResult.model_validate_json(canonical_bytes(value))
                    return self.manual_buy.result(session, lookup.client_order_id)
                if message.action in {
                    "DAILY_AUTO_STATUS",
                    "PREVIEW_DAILY_AUTO",
                    "CONFIRM_DAILY_AUTO",
                    "DISABLE_DAILY_AUTO",
                }:
                    return self._automatic(session, message.action, value)
                if message.action == "PREVIEW_CONTROL":
                    proposal = PreviewProposal.model_validate_json(canonical_bytes(value))
                    return self._preview(session, proposal)
                if message.action == "CONFIRM_CONTROL":
                    if set(value) != {"preview_id", "phrase"} or not all(
                        isinstance(v, str) for v in value.values()
                    ):
                        raise IPCError("INVALID_ARGUMENTS")
                    return self._confirm(session, str(value["preview_id"]), str(value["phrase"]))
                if message.action == "CONTROL_RESULT":
                    if set(value) != {"request_id"} or not isinstance(value["request_id"], str):
                        raise IPCError("INVALID_ARGUMENTS")
                    return self._result(session.actor_id, value["request_id"])
                raise IPCError("ACTION_DENIED")

            remaining = min(4.0, (message.deadline_ms - time_ns() // 1_000_000) / 1000)
            if remaining <= 0:
                raise IPCError("REQUEST_EXPIRED")
            return cast(dict[str, JsonValue], self.mailbox.call(operation, timeout=remaining))
        except OperatorSessionError as exc:
            if message.action == "CHANGE_PASSWORD" and exc.code == "OPERATOR_LOGIN_FAILED":
                raise IPCError("PASSWORD_CURRENT_INVALID") from None
            raise IPCError(exc.code) from None
        except ValueError as exc:
            code = str(exc)
            if code.startswith("BOARD_SELL_") and code.replace("_", "").isalnum():
                raise IPCError(code) from None
            raise
        except SecretLoadError:
            raise IPCError("PASSWORD_SAVE_FAILED") from None

    def authorize_control_channel(self, message: Request) -> str:
        """Fast existing-session check for the independent emergency pipe.

        No password hashing or trading permission is performed on this channel.
        Both session and CSRF proofs are checked by the engine, not trusted from
        an HTTP proxy's declaration that a user has logged in.
        """
        if message.sender_role != "operator" or set(message.payload) != {
            "session_token",
            "csrf_token",
        }:
            raise IPCError("INVALID_ARGUMENTS")
        token, csrf = message.payload["session_token"], message.payload["csrf_token"]
        if not isinstance(token, str) or not isinstance(csrf, str):
            raise IPCError("INVALID_ARGUMENTS")
        try:
            return self.sessions.authorize_mutation(token, csrf).actor_id
        except OperatorSessionError as exc:
            raise IPCError(exc.code) from None

    def _automatic(
        self,
        session: AuthenticatedOperatorSession,
        action: str,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        manager = self.daily_auto
        if manager is None:
            if action == "DAILY_AUTO_STATUS" and not value:
                return {"supported": False, "enabled": False, "state": "UNAVAILABLE"}
            raise IPCError("DAILY_AUTO_UNAVAILABLE")
        if action == "DISABLE_DAILY_AUTO":
            if value and value != {"reason": "SETTINGS_CHANGED"}:
                raise IPCError("INVALID_ARGUMENTS")
            # Invalidate pending consent even when no global HALT is needed.
            self._auto_previews.clear()
            if value:
                self._previews = {key: p for key, p in self._previews.items() if p.confirmed}
                manager.disable("SETTINGS_CHANGED")
            else:
                manager.disable()
            return manager.snapshot()
        if action == "DAILY_AUTO_STATUS":
            if value:
                raise IPCError("INVALID_ARGUMENTS")
            return manager.snapshot()
        self._auto_previews = {
            key: item for key, item in self._auto_previews.items() if item.expires > self.elapsed()
        }
        if action == "PREVIEW_DAILY_AUTO":
            if value or len(self._auto_previews) >= 32:
                raise IPCError("INVALID_ARGUMENTS")
            try:
                manager.check_enable()
            except ValueError as exc:
                code = (
                    str(exc)
                    if str(exc) in {"DAILY_AUTO_MARKET_UNAVAILABLE", "DEBUG_PICK_DAY_LOCKED"}
                    else "DAILY_AUTO_NOT_READY"
                )
                raise IPCError(code) from None
            identity = "daily-auto-" + uuid4().hex
            phrase = "ENABLE DAILY AUTO BUY " + identity[-8:]
            self._auto_previews[identity] = _AutoPreview(
                session.session_key,
                phrase,
                manager.settings_hash,
                self.engine.snapshot().permission_epoch,
                self.elapsed() + 30,
            )
            return {
                "preview_id": identity,
                "confirmation_phrase": phrase,
                "settings_hash": manager.settings_hash,
                "state": manager.snapshot(),
                "expires_at": (self.clock() + timedelta(seconds=30)).isoformat(),
                "buy_only": True,
            }
        if set(value) != {"preview_id", "phrase"} or not all(
            isinstance(item, str) for item in value.values()
        ):
            raise IPCError("INVALID_ARGUMENTS")
        identity = str(value["preview_id"])
        preview = self._auto_previews.get(identity)
        if (
            preview is None
            or preview.session_key != session.session_key
            or preview.settings_hash != manager.settings_hash
            or not hmac.compare_digest(str(value["phrase"]).encode(), preview.phrase.encode())
        ):
            raise IPCError("DAILY_AUTO_CONFIRMATION_INVALID")
        if preview.confirmed:
            if manager.consent is None or manager.consent.consent_id != identity:
                raise IPCError("DAILY_AUTO_SAVE_FAILED")
            return manager.snapshot()
        if preview.permission_epoch != self.engine.snapshot().permission_epoch:
            raise IPCError("DAILY_AUTO_STATE_CHANGED")
        preview.confirmed = True
        try:
            manager.enable(
                session.actor_id, consent_id=identity, expected_epoch=preview.permission_epoch
            )
        except (OSError, ValueError) as exc:
            if str(exc) in {"DAILY_AUTO_MARKET_UNAVAILABLE", "DEBUG_PICK_DAY_LOCKED"}:
                raise IPCError(str(exc)) from None
            raise IPCError("DAILY_AUTO_SAVE_FAILED") from None
        return manager.snapshot()

    def _preview(
        self, session: AuthenticatedOperatorSession, proposal: PreviewProposal
    ) -> dict[str, JsonValue]:
        snapshot = self.engine.snapshot()  # also checks owner identity
        self._previews = {
            k: v for k, v in self._previews.items() if v.confirmed or v.expires > self.elapsed()
        }
        if len(self._previews) >= self.capacity:
            raise IPCError("APPROVAL_CAPACITY_EXHAUSTED")
        report = snapshot.recovery
        if not report:
            raise IPCError("RECOVERY_REQUIRED")
        now = self.clock()
        deadline = min(now + timedelta(seconds=20), report.observed_at + timedelta(seconds=30))
        grant = None
        scope = execution_scope(self.engine.plans.plans.loaded(), self.engine.exit_context)
        scopes = self.engine._execution_scopes()
        scope = scope or (scopes[0] if scopes else None)
        if proposal.grant:
            if self.daily_auto and self.daily_auto.enabled and "BUY" in proposal.grant.directions:
                raise IPCError("DAILY_AUTO_ALREADY_ENABLED")
            if not scopes or self.engine.limits.mode not in {"PAPER", "LIVE"}:
                raise IPCError("EXECUTION_SCOPE_REQUIRED")
            p = proposal.grant
            mode = self.engine.limits.mode
            relevant = tuple(
                item for item in scopes if set(item.directions).intersection(p.directions)
            )
            if not relevant:
                raise IPCError("EXECUTION_SCOPE_REQUIRED")
            grant = SessionGrant(
                grant_id="grant-" + uuid4().hex,
                account_id=self.engine.account_id,
                engine_boot_id=self.engine.boot_id,
                session_generation=self.engine.next_session_generation,
                mode=mode,
                directions=p.directions,
                max_buy_value=p.max_buy_value,
                max_order_value=p.max_order_value,
                max_positions=p.max_positions,
                valid_from=now,
                valid_until=min(
                    now + timedelta(seconds=p.duration_seconds),
                    *(item.valid_until for item in relevant),
                ),
                confirmed_by=session.actor_id,
                recovery_report_hash=report.payload_hash,
            )
            deadline = min(deadline, grant.valid_until)
        request = LocalControlRequest(
            request_id="control-" + uuid4().hex,
            engine_boot_id=self.engine.boot_id,
            sender_role="agent",
            deadline=deadline,
            expected_state_version=snapshot.state_version,
            action=proposal.action,
            grant=grant,
            artifact_hash=report.payload_hash if proposal.action == "CONFIRM_RECOVERY" else None,
            operator_authorization_id="approval-" + uuid4().hex,
        )
        self.engine.preview_control(request)
        identity = "preview-" + uuid4().hex
        phrase = f"CONFIRM {self.engine.limits.mode} {proposal.action} {identity[-8:]}"
        self._previews[identity] = _Preview(
            request,
            session.session_key,
            session.actor_id,
            phrase,
            snapshot.permission_epoch,
            self.elapsed() + (deadline - now).total_seconds(),
        )
        return {
            "preview_id": identity,
            "confirmation_phrase": phrase,
            "actor_id": session.actor_id,
            "request": request.model_dump(mode="json"),
            "account_id": self.engine.account_id,
            "trade_date": self.engine.trade_date.isoformat(),
            "permission_epoch": snapshot.permission_epoch,
            "execution_scope": {
                "kind": scope.kind,
                "payload_hash": scope.payload_hash,
                "strategy_id": scope.strategy_id,
                "trade_date": scope.trade_date.isoformat(),
                "valid_until": scope.valid_until.isoformat(),
                "directions": list(scope.directions),
            }
            if scope
            else None,
            "real_trading_enabled": self.engine.limits.mode == "LIVE",
            "installed_limits": self.engine.limits.model_dump(mode="json"),
            "recovery": report.model_dump(mode="json"),
        }

    def _confirm(
        self, session: AuthenticatedOperatorSession, identity: str, phrase: str
    ) -> dict[str, JsonValue]:
        snapshot = self.engine.snapshot()
        preview = self._previews.get(identity)
        if preview is None or preview.session_key != session.session_key:
            raise IPCError("PREVIEW_NOT_FOUND")
        if not hmac.compare_digest(phrase.encode("utf-8"), preview.phrase.encode("utf-8")):
            raise IPCError("CONFIRMATION_MISMATCH")
        if preview.confirmed:
            return self._result(session.actor_id, preview.request.request_id)
        if (
            self.elapsed() >= preview.expires
            or snapshot.permission_epoch != preview.permission_epoch
        ):
            raise IPCError("PREVIEW_EXPIRED_OR_FENCED")
        self.engine.preview_control(preview.request)
        self.approvals.approve(preview.request, authenticated_actor=session.actor_id)
        # Reserve this identity before entering the durable-control path. Errors
        # remain uncertain and require result lookup, never a second approval.
        preview.confirmed = True
        return self.engine.control(preview.request).model_dump(mode="json")

    def _result(self, actor: str, request_id: str) -> dict[str, JsonValue]:
        if not any(
            p.actor == actor and p.confirmed and p.request.request_id == request_id
            for p in self._previews.values()
        ):
            raise IPCError("CONTROL_RESULT_UNKNOWN")
        result = self.engine.control_result(request_id)
        if result is None:
            raise IPCError("CONTROL_RESULT_UNCERTAIN")
        return result.model_dump(mode="json")
