"""Operator consent gates; fake daily controller cannot send any order."""

from time import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW, open_offline_engine
from auto_trading_system.control.ipc_protocol import IPCError, Request
from auto_trading_system.control.node_authorization import NodeAuthorization
from auto_trading_system.control.owner_mailbox import OwnerMailbox
from auto_trading_system.domain import ShanghaiTimestamp, SystemClock
from auto_trading_system.protocol.v2.canonical import digest
from auto_trading_system.security.operator_password import OperatorPasswordHasher
from auto_trading_system.security.operator_session import LocalOperatorSessionManager


class Manager:
    settings_hash = digest(b"installed settings")
    consent = None

    def __init__(self):
        self.calls = []
        self.disable_reasons = []

    def check_enable(self):
        pass

    def snapshot(self):
        return {
            "supported": True,
            "enabled": self.consent is not None,
            "consent_id": self.consent.consent_id if self.consent else None,
            "state": "WAITING",
        }

    def enable(self, actor, *, consent_id, expected_epoch):
        assert isinstance(expected_epoch, int)
        self.calls.append(actor)
        self.consent = SimpleNamespace(consent_id=consent_id)

    def disable(self, reason="OPERATOR_DISABLED"):
        self.disable_reasons.append(reason)
        self.consent = None


@pytest.fixture
def context(tmp_path):
    with open_offline_engine(tmp_path) as rt:
        sessions = LocalOperatorSessionManager(
            SecretStr(OperatorPasswordHasher().hash_password("test-auto-password-123")),
            actor_id="test-operator",
            clock=SimpleNamespace(now=lambda: ShanghaiTimestamp(DEMO_NOW)),
            monotonic_clock=SystemClock(),
        )
        issued = sessions.login(SecretStr("test-auto-password-123"))
        session = sessions.authorize_mutation(issued.session_token, issued.csrf_token)
        seconds = [0.0]
        manager = Manager()
        auth = NodeAuthorization(
            rt.kernel,
            OwnerMailbox(),
            sessions,
            clock=lambda: DEMO_NOW,
            elapsed=lambda: seconds[0],
            daily_auto=manager,
        )
        yield rt, auth, manager, session, seconds


def test_preview_never_enables_and_exact_confirmation_is_idempotent(context):
    rt, auth, manager, session, _ = context
    preview = auth._automatic(session, "PREVIEW_DAILY_AUTO", {})
    assert not manager.calls and rt.kernel.snapshot().grant_id is None
    data = {"preview_id": preview["preview_id"], "phrase": preview["confirmation_phrase"]}
    assert auth._automatic(session, "CONFIRM_DAILY_AUTO", data)["enabled"]
    assert auth._automatic(session, "CONFIRM_DAILY_AUTO", data)["enabled"]
    assert manager.calls == [session.actor_id]
    assert rt.kernel.snapshot().grant_id is None  # Saving consent is not a live grant.


@pytest.mark.parametrize("case", ["phrase", "session", "expired", "settings", "halt", "extra"])
def test_confirmation_cannot_outlive_its_exact_preview(context, case):
    rt, auth, manager, session, seconds = context
    preview = auth._automatic(session, "PREVIEW_DAILY_AUTO", {})
    data = {"preview_id": preview["preview_id"], "phrase": preview["confirmation_phrase"]}
    if case == "phrase":
        data["phrase"] = "不正确的确认文字"
    if case == "session":
        session = SimpleNamespace(session_key="other-login", actor_id=session.actor_id)
    if case == "expired":
        seconds[0] = 31
    if case == "settings":
        manager.settings_hash = digest(b"changed")
    if case == "halt":
        rt.kernel.emergency_halt("OPERATOR_STOP")
    if case == "extra":
        data["max_buy_value"] = "99999999"
    with pytest.raises(IPCError):
        auth._automatic(session, "CONFIRM_DAILY_AUTO", data)
    assert not manager.calls and rt.kernel.snapshot().grant_id is None


@pytest.mark.parametrize("role", ["agent", "jobrunner", "supervisor"])
def test_non_operator_cannot_enable_automatic_consent(context, role):
    rt, auth, manager, _, _ = context
    if role == "supervisor":
        with pytest.raises(ValidationError, match="sender_role"):
            Request(
                request_id="request-" + uuid4().hex,
                engine_boot_id=rt.kernel.boot_id,
                sender_role=role,
                action="PREVIEW_DAILY_AUTO",
                deadline_ms=int(time() * 1000) + 3000,
                payload={},
            )
        return
    request = Request(
        request_id="request-" + uuid4().hex,
        engine_boot_id=rt.kernel.boot_id,
        sender_role=role,
        action="PREVIEW_DAILY_AUTO",
        deadline_ms=int(time() * 1000) + 3000,
        payload={},
    )
    with pytest.raises(IPCError, match="ROLE_DENIED"):
        auth.handle(request)
    assert not manager.calls


def test_missing_session_and_browser_supplied_limits_are_rejected(context):
    rt, auth, manager, session, _ = context
    request = Request(
        request_id="request-" + uuid4().hex,
        engine_boot_id=rt.kernel.boot_id,
        sender_role="operator",
        action="CONFIRM_DAILY_AUTO",
        deadline_ms=int(time() * 1000) + 3000,
        payload={"preview_id": "guessed", "phrase": "guessed"},
    )
    with pytest.raises(IPCError, match="OPERATOR_SESSION_REQUIRED"):
        auth.handle(request)
    with pytest.raises(IPCError):
        auth._automatic(session, "PREVIEW_DAILY_AUTO", {"max_buy_value": "99999999"})
    assert not manager.calls


def test_confirmation_reports_state_change_instead_of_phrase_error(context):
    rt, auth, manager, session, _ = context
    preview = auth._automatic(session, "PREVIEW_DAILY_AUTO", {})
    rt.kernel.emergency_halt("QMT_TICK_CALLBACK_INVALID")
    with pytest.raises(IPCError, match="DAILY_AUTO_STATE_CHANGED"):
        auth._automatic(
            session,
            "CONFIRM_DAILY_AUTO",
            {"preview_id": preview["preview_id"], "phrase": preview["confirmation_phrase"]},
        )
    assert not manager.calls


def test_preview_reports_market_fault_without_misleading_confirmation(context):
    _, auth, manager, session, _ = context

    def blocked():
        raise ValueError("DAILY_AUTO_MARKET_UNAVAILABLE")

    manager.check_enable = blocked
    with pytest.raises(IPCError, match="DAILY_AUTO_MARKET_UNAVAILABLE"):
        auth._automatic(session, "PREVIEW_DAILY_AUTO", {})
    assert not manager.calls


def test_settings_change_invalidates_preview_even_without_permission_change(context):
    rt, auth, manager, session, _ = context
    preview = auth._automatic(session, "PREVIEW_DAILY_AUTO", {})
    epoch = rt.kernel.snapshot().permission_epoch
    auth._automatic(session, "DISABLE_DAILY_AUTO", {"reason": "SETTINGS_CHANGED"})
    assert manager.disable_reasons == ["SETTINGS_CHANGED"]
    assert rt.kernel.snapshot().permission_epoch == epoch
    with pytest.raises(IPCError, match="DAILY_AUTO_CONFIRMATION_INVALID"):
        auth._automatic(
            session,
            "CONFIRM_DAILY_AUTO",
            {"preview_id": preview["preview_id"], "phrase": preview["confirmation_phrase"]},
        )
    assert not manager.calls


@pytest.mark.parametrize(
    "value", [{"reason": "UNKNOWN"}, {"reason": "SETTINGS_CHANGED", "enabled": True}]
)
def test_disable_rejects_arbitrary_reasons_or_extra_arguments(context, value):
    _, auth, manager, session, _ = context
    with pytest.raises(IPCError, match="INVALID_ARGUMENTS"):
        auth._automatic(session, "DISABLE_DAILY_AUTO", value)
    assert not manager.disable_reasons
