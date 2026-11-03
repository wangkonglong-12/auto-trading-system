"""Password rotation uses an isolated vault substitute and no real credentials."""

from __future__ import annotations

from time import time_ns

import pytest
from pydantic import SecretStr

from auto_trading_system.config.credentials import SecretLoadError
from auto_trading_system.config.models import EnvironmentSecretReference
from auto_trading_system.config.operator_password_store import OperatorPasswordStore
from auto_trading_system.control.ipc_protocol import IPCError, Request
from auto_trading_system.control.node_authorization import AUTH_ACTIONS, NodeAuthorization
from auto_trading_system.domain.clock import SystemClock
from auto_trading_system.security.operator_password import OperatorPasswordHasher
from auto_trading_system.security.operator_session import (
    LocalOperatorSessionManager,
    OperatorSessionError,
)

OLD = "Old-password-for-tests-123"
NEW = "New-password-for-tests-456"


def sessions() -> LocalOperatorSessionManager:
    return LocalOperatorSessionManager(
        SecretStr(OperatorPasswordHasher().hash_password(OLD)),
        actor_id="test-operator",
        clock=SystemClock(),
        monotonic_clock=SystemClock(),
    )


def test_rotation_revokes_all_sessions_and_old_password() -> None:
    manager = sessions()
    first, second = manager.login(SecretStr(OLD)), manager.login(SecretStr(OLD))
    saved = []
    manager.change_password(
        first.session_token, first.csrf_token, SecretStr(OLD), SecretStr(NEW), saved.append
    )
    assert OperatorPasswordHasher().verify(NEW, saved[0])
    for session in (first, second):
        with pytest.raises(OperatorSessionError, match="OPERATOR_SESSION_INVALID"):
            manager.authorize_mutation(session.session_token, session.csrf_token)
    with pytest.raises(OperatorSessionError, match="OPERATOR_LOGIN_FAILED"):
        manager.login(SecretStr(OLD))
    manager.login(SecretStr(NEW))
    restarted = LocalOperatorSessionManager(
        SecretStr(saved[0]),
        actor_id="test-operator",
        clock=SystemClock(),
        monotonic_clock=SystemClock(),
    )
    restarted.login(SecretStr(NEW))


def test_failure_csrf_and_rate_limit_leave_password_unchanged() -> None:
    manager = sessions()
    issued = manager.login(SecretStr(OLD))
    saved = []
    with pytest.raises(OperatorSessionError, match="CSRF"):
        manager.change_password(
            issued.session_token, "z" * 43, SecretStr(OLD), SecretStr(NEW), saved.append
        )
    for _ in range(5):
        with pytest.raises(OperatorSessionError, match="OPERATOR_LOGIN_FAILED"):
            manager.change_password(
                issued.session_token,
                issued.csrf_token,
                SecretStr("wrong"),
                SecretStr(NEW),
                saved.append,
            )
    with pytest.raises(OperatorSessionError, match="RATE_LIMITED"):
        manager.change_password(
            issued.session_token, issued.csrf_token, SecretStr(OLD), SecretStr(NEW), saved.append
        )
    assert saved == []


def test_persist_failure_does_not_revoke_sessions_or_replace_password() -> None:
    manager = sessions()
    issued = manager.login(SecretStr(OLD))

    def fail(_: str) -> None:
        raise SecretLoadError("WINDOWS_CREDENTIAL_WRITE_FAILED", "test failure")

    with pytest.raises(SecretLoadError):
        manager.change_password(
            issued.session_token, issued.csrf_token, SecretStr(OLD), SecretStr(NEW), fail
        )
    manager.authorize_mutation(issued.session_token, issued.csrf_token)
    manager.login(SecretStr(OLD))
    for invalid in ("short", " " + NEW, NEW + "\x00"):
        with pytest.raises(ValueError):
            manager.change_password(
                issued.session_token, issued.csrf_token, SecretStr(OLD), SecretStr(invalid), fail
            )


def test_auth_action_requires_session_and_runs_without_owner_mailbox() -> None:
    assert "CHANGE_PASSWORD" in AUTH_ACTIONS
    auth = object.__new__(NodeAuthorization)
    auth.sessions = sessions()
    saved = []
    auth.password_writer = saved.append
    issued = auth.sessions.login(SecretStr(OLD))

    def message(**payload: str) -> Request:
        return Request(
            request_id="test-change",
            engine_boot_id="test-boot",
            sender_role="operator",
            deadline_ms=time_ns() // 1_000_000 + 7000,
            action="CHANGE_PASSWORD",
            payload=payload,
        )

    with pytest.raises(IPCError, match="OPERATOR_SESSION_REQUIRED"):
        auth.handle(message(current_password=OLD, new_password=NEW))
    proof = {"session_token": issued.session_token, "csrf_token": issued.csrf_token}
    with pytest.raises(IPCError, match="PASSWORD_CURRENT_INVALID"):
        auth.handle(message(**proof, current_password="wrong", new_password=NEW))
    assert auth.handle(message(**proof, current_password=OLD, new_password=NEW)) == {
        "password_changed": True
    }
    assert len(saved) == 1


def test_environment_profile_reads_rotated_verifier_from_fake_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OperatorPasswordStore(
        EnvironmentSecretReference(source="ENVIRONMENT", variable="ATS_SECRET_TEST_PASSWORD")
    )
    initial = OperatorPasswordHasher().hash_password(OLD)
    monkeypatch.setenv("ATS_SECRET_TEST_PASSWORD", initial)
    values = {}

    class Vault:
        def read(self, ref):
            if ref.target not in values:
                raise SecretLoadError("SECRET_NOT_FOUND", "missing")
            return SecretStr(values[ref.target])

        def write(self, ref, verifier):
            values[ref.target] = verifier

    store.vault = Vault()
    assert store.read().get_secret_value() == initial
    rotated = OperatorPasswordHasher().hash_password(NEW)
    store.write(rotated)
    monkeypatch.setenv("ATS_SECRET_TEST_PASSWORD", initial)
    assert store.read().get_secret_value() == rotated
