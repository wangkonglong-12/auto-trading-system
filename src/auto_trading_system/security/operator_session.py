"""Bounded in-memory operator sessions for the loopback console."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum
from threading import Lock

from pydantic import SecretStr

from auto_trading_system.domain import Clock, MonotonicClock, ShanghaiTimestamp
from auto_trading_system.security.operator_password import OperatorPasswordHasher

_ACTOR_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{2,95}")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")


class OperatorSessionFailureKind(StrEnum):
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    CAPACITY = "CAPACITY"


class OperatorSessionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        kind: OperatorSessionFailureKind,
        retry_after_seconds: int | None = None,
    ) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code) is None:
            raise ValueError("code has an invalid format")
        if not isinstance(kind, OperatorSessionFailureKind):
            raise ValueError("kind must be an OperatorSessionFailureKind")
        if retry_after_seconds is not None and (
            type(retry_after_seconds) is not int or retry_after_seconds < 1
        ):
            raise ValueError("retry_after_seconds must be positive")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class OperatorSessionPolicy:
    idle_seconds: int = 900
    absolute_seconds: int = 3600
    failure_window_seconds: int = 300
    max_failures: int = 5
    max_sessions: int = 4

    def __post_init__(self) -> None:
        if type(self.idle_seconds) is not int or not 300 <= self.idle_seconds <= 3600:
            raise ValueError("idle_seconds must be between 300 and 3600")
        if (
            type(self.absolute_seconds) is not int
            or not 900 <= self.absolute_seconds <= 14_400
            or self.absolute_seconds < self.idle_seconds
        ):
            raise ValueError("absolute_seconds has an invalid value")
        if (
            type(self.failure_window_seconds) is not int
            or not 60 <= self.failure_window_seconds <= 3600
        ):
            raise ValueError("failure_window_seconds must be between 60 and 3600")
        if type(self.max_failures) is not int or not 3 <= self.max_failures <= 10:
            raise ValueError("max_failures must be between 3 and 10")
        if type(self.max_sessions) is not int or not 1 <= self.max_sessions <= 16:
            raise ValueError("max_sessions must be between 1 and 16")


@dataclass(frozen=True, slots=True)
class IssuedOperatorSession:
    session_token: str
    csrf_token: str
    actor_id: str
    issued_at: ShanghaiTimestamp
    expires_at: ShanghaiTimestamp

    def __post_init__(self) -> None:
        if (
            _TOKEN_PATTERN.fullmatch(self.session_token) is None
            or _TOKEN_PATTERN.fullmatch(self.csrf_token) is None
        ):
            raise ValueError("issued session tokens have an invalid format")
        if _ACTOR_PATTERN.fullmatch(self.actor_id) is None:
            raise ValueError("actor_id has an invalid format")
        if not isinstance(self.issued_at, ShanghaiTimestamp) or not isinstance(
            self.expires_at, ShanghaiTimestamp
        ):
            raise ValueError("session timestamps are invalid")
        if self.expires_at <= self.issued_at:
            raise ValueError("session expiry must follow issue time")


@dataclass(frozen=True, slots=True)
class AuthenticatedOperatorSession:
    session_key: str
    actor_id: str
    issued_at: ShanghaiTimestamp
    expires_at: ShanghaiTimestamp

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.session_key) is None:
            raise ValueError("session_key has an invalid format")
        if _ACTOR_PATTERN.fullmatch(self.actor_id) is None:
            raise ValueError("actor_id has an invalid format")


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    session_key: str
    csrf_hash: bytes
    actor_id: str
    issued_at: ShanghaiTimestamp
    expires_at: ShanghaiTimestamp
    created_ns: int
    last_seen_ns: int


class LocalOperatorSessionManager:
    """Verify a vault-backed password and issue short, process-local sessions."""

    def __init__(
        self,
        password_verifier: SecretStr,
        *,
        actor_id: str,
        clock: Clock,
        monotonic_clock: MonotonicClock,
        policy: OperatorSessionPolicy | None = None,
        password_hasher: OperatorPasswordHasher | None = None,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        if not isinstance(password_verifier, SecretStr):
            raise ValueError("password_verifier must be a SecretStr")
        if _ACTOR_PATTERN.fullmatch(actor_id) is None:
            raise ValueError("actor_id has an invalid format")
        if not callable(getattr(clock, "now", None)) or not callable(
            getattr(monotonic_clock, "monotonic_ns", None)
        ):
            raise ValueError("session clocks are invalid")
        if not callable(token_factory):
            raise ValueError("token_factory must be callable")
        self._password_verifier = password_verifier
        self._actor_id = actor_id
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._policy = policy or OperatorSessionPolicy()
        self._password_hasher = password_hasher or OperatorPasswordHasher()
        self._token_factory = token_factory
        self._sessions: OrderedDict[str, _SessionRecord] = OrderedDict()
        self._failed_logins: deque[int] = deque()
        self._lock = Lock()
        self._login_lock = Lock()

    def login(self, password: SecretStr) -> IssuedOperatorSession:
        if not isinstance(password, SecretStr):
            raise ValueError("password must be a SecretStr")
        with self._login_lock:
            checked_at_ns = self._monotonic_clock.monotonic_ns()
            with self._lock:
                self._expire_failures(checked_at_ns)
                self._raise_if_rate_limited(checked_at_ns)
            verified = self._password_hasher.verify(
                password.get_secret_value(),
                self._password_verifier.get_secret_value(),
            )
            if not verified:
                with self._lock:
                    self._failed_logins.append(checked_at_ns)
                raise OperatorSessionError(
                    "OPERATOR_LOGIN_FAILED",
                    "operator credentials were not accepted",
                    kind=OperatorSessionFailureKind.UNAUTHORIZED,
                )
            with self._lock:
                self._failed_logins.clear()

        session_token = self._new_token()
        csrf_token = self._new_token()
        session_key = _token_key(session_token)
        now = self._clock.now()
        expires_at = ShanghaiTimestamp.from_instant(
            now.value + timedelta(seconds=self._policy.absolute_seconds)
        )
        record = _SessionRecord(
            session_key=session_key,
            csrf_hash=_token_digest(csrf_token),
            actor_id=self._actor_id,
            issued_at=now,
            expires_at=expires_at,
            created_ns=checked_at_ns,
            last_seen_ns=checked_at_ns,
        )
        with self._lock:
            self._expire_sessions(checked_at_ns)
            if len(self._sessions) >= self._policy.max_sessions:
                self._sessions.popitem(last=False)
            self._sessions[session_key] = record
        return IssuedOperatorSession(
            session_token,
            csrf_token,
            self._actor_id,
            now,
            expires_at,
        )

    def authenticate(self, session_token: str | None) -> AuthenticatedOperatorSession:
        token = _validated_token(session_token, code="OPERATOR_SESSION_REQUIRED")
        session_key = _token_key(token)
        now_ns = self._monotonic_clock.monotonic_ns()
        with self._lock:
            record = self._sessions.get(session_key)
            if record is None or self._is_expired(record, now_ns):
                self._sessions.pop(session_key, None)
                raise OperatorSessionError(
                    "OPERATOR_SESSION_INVALID",
                    "operator session is invalid or expired",
                    kind=OperatorSessionFailureKind.UNAUTHORIZED,
                )
            refreshed = replace(record, last_seen_ns=now_ns)
            self._sessions[session_key] = refreshed
            self._sessions.move_to_end(session_key)
        return AuthenticatedOperatorSession(
            refreshed.session_key,
            refreshed.actor_id,
            refreshed.issued_at,
            refreshed.expires_at,
        )

    def authorize_mutation(
        self,
        session_token: str | None,
        csrf_token: str | None,
    ) -> AuthenticatedOperatorSession:
        authenticated = self.authenticate(session_token)
        csrf = _validated_token(
            csrf_token,
            code="OPERATOR_CSRF_REQUIRED",
            kind=OperatorSessionFailureKind.FORBIDDEN,
        )
        with self._lock:
            record = self._sessions.get(authenticated.session_key)
            if record is None or not hmac.compare_digest(record.csrf_hash, _token_digest(csrf)):
                raise OperatorSessionError(
                    "OPERATOR_CSRF_INVALID",
                    "request CSRF proof was not accepted",
                    kind=OperatorSessionFailureKind.FORBIDDEN,
                )
        return authenticated

    def logout(self, session_token: str | None, csrf_token: str | None) -> None:
        authenticated = self.authorize_mutation(session_token, csrf_token)
        with self._lock:
            self._sessions.pop(authenticated.session_key, None)

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            return count

    def _raise_if_rate_limited(self, now_ns: int) -> None:
        if len(self._failed_logins) < self._policy.max_failures:
            return
        retry_ns = self._failed_logins[0] + self._policy.failure_window_seconds * 1_000_000_000
        retry_seconds = max(1, (retry_ns - now_ns + 999_999_999) // 1_000_000_000)
        raise OperatorSessionError(
            "OPERATOR_LOGIN_RATE_LIMITED",
            "operator login is temporarily rate limited",
            kind=OperatorSessionFailureKind.RATE_LIMITED,
            retry_after_seconds=retry_seconds,
        )

    def _expire_failures(self, now_ns: int) -> None:
        cutoff = now_ns - self._policy.failure_window_seconds * 1_000_000_000
        while self._failed_logins and self._failed_logins[0] <= cutoff:
            self._failed_logins.popleft()

    def _expire_sessions(self, now_ns: int) -> None:
        expired = [
            key for key, record in self._sessions.items() if self._is_expired(record, now_ns)
        ]
        for key in expired:
            del self._sessions[key]

    def _is_expired(self, record: _SessionRecord, now_ns: int) -> bool:
        idle_ns = self._policy.idle_seconds * 1_000_000_000
        absolute_ns = self._policy.absolute_seconds * 1_000_000_000
        return now_ns - record.last_seen_ns >= idle_ns or now_ns - record.created_ns >= absolute_ns

    def _new_token(self) -> str:
        token = self._token_factory()
        if not isinstance(token, str) or _TOKEN_PATTERN.fullmatch(token) is None:
            raise OperatorSessionError(
                "OPERATOR_SESSION_TOKEN_FAILED",
                "secure session material could not be generated",
                kind=OperatorSessionFailureKind.CAPACITY,
            )
        return token


def _validated_token(
    value: str | None,
    *,
    code: str,
    kind: OperatorSessionFailureKind = OperatorSessionFailureKind.UNAUTHORIZED,
) -> str:
    if not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None:
        raise OperatorSessionError(
            code,
            "operator session proof is required",
            kind=kind,
        )
    return value


def _token_digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


def _token_key(value: str) -> str:
    return _token_digest(value).hex()
