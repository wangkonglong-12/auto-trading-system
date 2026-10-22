from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from pydantic import SecretStr

from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.security import (
    LocalOperatorSessionManager,
    OperatorPasswordHasher,
    OperatorSessionError,
    OperatorSessionFailureKind,
    OperatorSessionPolicy,
)

PASSWORD = "Correct-Horse-Battery-2026"
VERIFIER = (
    "ats-scrypt-v1$32768$8$1$AAECAwQFBgcICQoLDA0ODw$f81LIxrvonq2ysChD17nLqfS0_FxQ1_5qy8BxpPy-n4"
)


@dataclass
class MutableClock:
    value: ShanghaiTimestamp = field(
        default_factory=lambda: ShanghaiTimestamp.parse("2026-09-04T10:00:00+08:00")
    )
    elapsed_ns: int = 0

    def now(self) -> ShanghaiTimestamp:
        return self.value

    def monotonic_ns(self) -> int:
        return self.elapsed_ns

    def advance(self, seconds: int) -> None:
        self.elapsed_ns += seconds * 1_000_000_000
        self.value = ShanghaiTimestamp.from_instant(self.value.value + timedelta(seconds=seconds))


class StubPasswordHasher(OperatorPasswordHasher):
    def verify(self, password: str, verifier: str) -> bool:
        return password == PASSWORD and verifier == VERIFIER


class TokenSequence:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"secure_token_{self._value:04d}_" + "x" * 32


def manager(
    clock: MutableClock,
    *,
    policy: OperatorSessionPolicy | None = None,
    token_factory: TokenSequence | None = None,
) -> LocalOperatorSessionManager:
    return LocalOperatorSessionManager(
        SecretStr(VERIFIER),
        actor_id="operator_demo",
        clock=clock,
        monotonic_clock=clock,
        policy=policy,
        password_hasher=StubPasswordHasher(),
        token_factory=token_factory or TokenSequence(),
    )


def test_login_issues_hashed_process_local_session_with_csrf_and_logout() -> None:
    clock = MutableClock()
    sessions = manager(clock)

    issued = sessions.login(SecretStr(PASSWORD))
    authenticated = sessions.authenticate(issued.session_token)
    mutation = sessions.authorize_mutation(issued.session_token, issued.csrf_token)

    assert issued.actor_id == "operator_demo"
    assert issued.expires_at.to_wire() == "2026-09-04T11:00:00+08:00"
    assert authenticated.session_key == mutation.session_key
    assert issued.session_token not in repr(sessions)
    assert issued.csrf_token not in repr(sessions)

    sessions.logout(issued.session_token, issued.csrf_token)
    with pytest.raises(OperatorSessionError) as captured:
        sessions.authenticate(issued.session_token)
    assert captured.value.code == "OPERATOR_SESSION_INVALID"


def test_authentication_and_csrf_fail_closed_with_distinct_http_kinds() -> None:
    sessions = manager(MutableClock())
    issued = sessions.login(SecretStr(PASSWORD))

    with pytest.raises(OperatorSessionError) as missing_session:
        sessions.authenticate(None)
    assert missing_session.value.kind is OperatorSessionFailureKind.UNAUTHORIZED

    with pytest.raises(OperatorSessionError) as missing_csrf:
        sessions.authorize_mutation(issued.session_token, None)
    assert missing_csrf.value.code == "OPERATOR_CSRF_REQUIRED"
    assert missing_csrf.value.kind is OperatorSessionFailureKind.FORBIDDEN

    with pytest.raises(OperatorSessionError) as wrong_csrf:
        sessions.authorize_mutation(issued.session_token, "z" * 40)
    assert wrong_csrf.value.code == "OPERATOR_CSRF_INVALID"
    assert wrong_csrf.value.kind is OperatorSessionFailureKind.FORBIDDEN


def test_idle_and_absolute_expiry_use_monotonic_time() -> None:
    idle_clock = MutableClock()
    idle_sessions = manager(
        idle_clock,
        policy=OperatorSessionPolicy(idle_seconds=300, absolute_seconds=900),
    )
    idle = idle_sessions.login(SecretStr(PASSWORD))
    idle_clock.advance(301)
    with pytest.raises(OperatorSessionError, match="OPERATOR_SESSION_INVALID"):
        idle_sessions.authenticate(idle.session_token)

    absolute_clock = MutableClock()
    absolute_sessions = manager(
        absolute_clock,
        policy=OperatorSessionPolicy(idle_seconds=300, absolute_seconds=900),
    )
    absolute = absolute_sessions.login(SecretStr(PASSWORD))
    for _ in range(3):
        absolute_clock.advance(250)
        absolute_sessions.authenticate(absolute.session_token)
    absolute_clock.advance(151)
    with pytest.raises(OperatorSessionError, match="OPERATOR_SESSION_INVALID"):
        absolute_sessions.authenticate(absolute.session_token)


def test_failed_login_rate_limit_recovers_after_bounded_window() -> None:
    clock = MutableClock()
    sessions = manager(
        clock,
        policy=OperatorSessionPolicy(
            failure_window_seconds=60,
            max_failures=3,
        ),
    )
    for _ in range(3):
        with pytest.raises(OperatorSessionError, match="OPERATOR_LOGIN_FAILED"):
            sessions.login(SecretStr("wrong-password-value"))

    with pytest.raises(OperatorSessionError) as limited:
        sessions.login(SecretStr(PASSWORD))
    assert limited.value.kind is OperatorSessionFailureKind.RATE_LIMITED
    assert limited.value.retry_after_seconds == 60

    clock.advance(61)
    assert sessions.login(SecretStr(PASSWORD)).actor_id == "operator_demo"


def test_session_capacity_evicts_oldest_and_revoke_all_is_counted() -> None:
    clock = MutableClock()
    sessions = manager(
        clock,
        policy=OperatorSessionPolicy(max_sessions=2),
    )
    first = sessions.login(SecretStr(PASSWORD))
    second = sessions.login(SecretStr(PASSWORD))
    third = sessions.login(SecretStr(PASSWORD))

    with pytest.raises(OperatorSessionError, match="OPERATOR_SESSION_INVALID"):
        sessions.authenticate(first.session_token)
    assert sessions.authenticate(second.session_token).actor_id == "operator_demo"
    assert sessions.authenticate(third.session_token).actor_id == "operator_demo"
    assert sessions.revoke_all() == 2
    assert sessions.revoke_all() == 0


def test_invalid_secure_token_generation_is_reported_as_capacity_failure() -> None:
    sessions = LocalOperatorSessionManager(
        SecretStr(VERIFIER),
        actor_id="operator_demo",
        clock=MutableClock(),
        monotonic_clock=MutableClock(),
        password_hasher=StubPasswordHasher(),
        token_factory=lambda: "short",
    )

    with pytest.raises(OperatorSessionError) as captured:
        sessions.login(SecretStr(PASSWORD))
    assert captured.value.code == "OPERATOR_SESSION_TOKEN_FAILED"
    assert captured.value.kind is OperatorSessionFailureKind.CAPACITY


@pytest.mark.parametrize(
    "policy",
    [
        {"idle_seconds": 299},
        {"absolute_seconds": 899},
        {"idle_seconds": 1000, "absolute_seconds": 900},
        {"failure_window_seconds": 59},
        {"max_failures": 2},
        {"max_sessions": 0},
    ],
)
def test_session_policy_rejects_unsafe_bounds(policy: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        OperatorSessionPolicy(**policy)
