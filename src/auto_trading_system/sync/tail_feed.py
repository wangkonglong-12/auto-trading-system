"""Single-flight simple-feed transport and finite per-day polling."""

from __future__ import annotations

import re
import ssl
from collections.abc import Callable
from datetime import datetime, time
from time import monotonic
from urllib.parse import urlencode, urlsplit

import httpx

from auto_trading_system.config.node_v2 import NodeProcessConfig, resolve_secret, role_keys
from auto_trading_system.config.quant_access import read_access_token
from auto_trading_system.control.ipc_protocol import RPCClient
from auto_trading_system.protocol.tail_picks import CN, TailPicks, TailPicksPreview
from auto_trading_system.protocol.v2.canonical import canonical_bytes, parse_bytes
from auto_trading_system.sync.artifact_files import ArtifactFiles


class FeedRequestError(ValueError):
    def __init__(self, code: str, *, retryable: bool, delay: int | None = None) -> None:
        super().__init__(code)
        self.retryable, self.delay = retryable, delay


class TailFeedHttp:
    def __init__(
        self,
        origin: str,
        token: bytes,
        *,
        ca_file: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parts = urlsplit(origin)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or not re.fullmatch(rb"[A-Za-z0-9_-]{43,256}", token)
        ):
            raise ValueError("invalid tail feed origin or credential")
        self._origin = origin.rstrip("/")
        self._client = httpx.Client(
            transport=transport,
            verify=ssl.create_default_context(cafile=ca_file),
            trust_env=False,
            follow_redirects=False,
            headers={
                "Authorization": "Bearer " + token.decode("ascii"),
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    def close(self) -> None:
        self._client.close()

    def get(self, now: datetime) -> bytes:
        return self._get(now, preview=False)

    def get_preview(self, now: datetime) -> bytes:
        """Diagnostic caller only; this response cannot enter the live feed parser."""
        return self._get(now, preview=True)

    def _get(self, now: datetime, *, preview: bool) -> bytes:
        started = monotonic()
        try:
            with self._client.stream(
                "GET",
                self._origin
                + "/api/auto-trading/tail-picks?"
                + urlencode(
                    {
                        "trade_date": now.astimezone(CN).date().isoformat(),
                        **({"test": "true"} if preview else {}),
                    }
                ),
                timeout=httpx.Timeout(8, connect=3, pool=1),
            ) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > 65536:
                        raise FeedRequestError("FEED_RESPONSE_TOO_LARGE", retryable=False)
                    body.extend(chunk)
                    if monotonic() - started > 8:
                        raise FeedRequestError("FEED_TIMEOUT", retryable=True)
                if response.status_code != 200:
                    retryable = response.status_code in (429, 500, 502, 503, 504)
                    code = "FEED_HTTP_ERROR"
                    try:
                        error = parse_bytes(bytes(body), max_bytes=65536)
                        detail = error.get("error") if isinstance(error, dict) else None
                        if isinstance(detail, dict):
                            error_code = detail.get("code")
                            if error_code in (
                                "INVALID_REQUEST",
                                "UNAUTHORIZED",
                                "FORBIDDEN",
                                "UNSUPPORTED_TRADE_DATE",
                                "NON_TRADING_DAY",
                                "RATE_LIMITED",
                                "SERVICE_UNAVAILABLE",
                                "FEED_DISABLED",
                                "FEED_SUSPENDED",
                            ) and isinstance(error_code, str):
                                code = error_code
                            if detail.get("retryable") is False:
                                retryable = False
                    except ValueError:
                        pass
                    raw_delay = response.headers.get("retry-after", "")
                    delay = (
                        int(raw_delay)
                        if raw_delay.isdecimal() and 1 <= int(raw_delay) <= 60
                        else None
                    )
                    raise FeedRequestError(code, retryable=retryable, delay=delay)
                model = TailPicksPreview if preview else TailPicks
                feed = model.model_validate_json(
                    canonical_bytes(parse_bytes(bytes(body), max_bytes=65536))
                )
                feed.check_current(now)
                return bytes(body)
        except httpx.HTTPError as error:
            raise FeedRequestError("FEED_CONNECTION_FAILED", retryable=True) from error


class TailFeedPuller:
    """Completion comes from durable engine state, not an HTTP success or memory flag."""

    def __init__(
        self,
        config: NodeProcessConfig,
        engine: RPCClient,
        clock: Callable[[], datetime],
        *,
        fetch: Callable[[datetime], bytes] | None = None,
        seconds: Callable[[], float] = monotonic,
    ) -> None:
        self.config, self.engine, self.clock = config, engine, clock
        self.fetch, self.seconds = fetch, seconds
        self.next_due = 0.0
        self.failures = 0
        self.stopped_day: str | None = None

    def due(self) -> bool:
        now = self.clock().astimezone(CN)
        return bool(
            self.config.tail_picks is not None
            and now.date() in self.config.calendar.sessions
            and time(14, 39, 50) <= now.time() < time(14, 55)
            and self.stopped_day != now.date().isoformat()
            and self.seconds() >= self.next_due
        )

    def run(self) -> dict[str, object]:
        now = self.clock().astimezone(CN)
        if not self.due():
            return {"status": "SKIPPED", "reason": "FEED_COMPLETE_OR_OUTSIDE_WINDOW"}
        try:
            status = self.engine.invoke("TAIL_FEED_STATUS")
            if status.get("trade_date") != now.date().isoformat():
                raise ValueError("feed engine day differs")
            if status.get("all_ready") is True or status.get("suspended") is True:
                self.stopped_day = now.date().isoformat()
                return {"status": "SUSPENDED" if status.get("suspended") else "COMPLETE"}
            if self.fetch is not None:
                body = self.fetch(now)
            else:
                assert self.config.tail_picks is not None
                token = read_access_token(self.config.data_root)
                if token is None and self.config.tail_picks.token is not None:
                    token = resolve_secret(self.config.tail_picks.token)
                if token is None:
                    raise ValueError("QUANT_ACCESS_REQUIRED")
                if token in role_keys(self.config, ("agent", "jobrunner", "operator")).values():
                    raise ValueError("selection feed and local IPC credentials must differ")
                client = TailFeedHttp(
                    self.config.quant_origin,
                    token,
                    ca_file=str(self.config.quant_ca_file) if self.config.quant_ca_file else None,
                )
                try:
                    body = client.get(now)
                finally:
                    client.close()
            # Every injected transport follows the same validation as the real HTTPS client.
            feed = TailPicks.model_validate_json(
                canonical_bytes(parse_bytes(body, max_bytes=65536))
            )
            feed.check_current(now)
            files = ArtifactFiles(self.config.data_root / "artifacts")
            reference = files.put(body)
            result = self.engine.invoke("LOAD_TAIL_PICKS", {"artifact_hash": reference})
            if result.get("trade_date") != now.date().isoformat():
                raise ValueError("feed handoff response has another day")
            if result.get("all_ready") is True:
                self.stopped_day = now.date().isoformat()
            self.failures = 0
            self.next_due = self.seconds() + 5
            return {"status": "COMPLETE" if self.stopped_day else "WAITING_PHASES"}
        except FeedRequestError as error:
            if not error.retryable:
                self.engine.invoke("TAIL_FEED_FAULT")
                self.stopped_day = now.date().isoformat()
            self.failures += 1
            self.next_due = self.seconds() + (
                error.delay or min(30, 5 * 2 ** min(self.failures - 1, 3))
            )
            return {"status": "RETRY" if error.retryable else "SUSPENDED", "reason": str(error)}
        except ValueError:
            self.engine.invoke("TAIL_FEED_FAULT")
            self.stopped_day = now.date().isoformat()
            return {"status": "SUSPENDED", "reason": "FEED_VALIDATION_FAILED"}
        finally:
            # Also bounds retry after an IPC timeout/uncertain handoff. The next run
            # consults the engine before making another network request.
            self.next_due = max(self.next_due, self.seconds() + 5)
