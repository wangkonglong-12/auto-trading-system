"""Independent single-flight reporter. A retry only sends facts, never broker commands."""

from __future__ import annotations

import re
import ssl
from collections.abc import Callable
from time import monotonic
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from auto_trading_system.config.credentials import SecretLoadError
from auto_trading_system.config.node_v2 import NodeProcessConfig, resolve_secret, role_keys
from auto_trading_system.config.quant_access import read_access_token
from auto_trading_system.control.ipc_protocol import RPCClient
from auto_trading_system.protocol.executions.models import Batch, Receipt, body_hash
from auto_trading_system.protocol.v2.canonical import canonical_bytes, parse_bytes
from auto_trading_system.sync.artifact_files import ArtifactFiles


class ExecutionUploadError(ValueError):
    def __init__(self, *, retryable: bool, delay: int = 0) -> None:
        super().__init__("execution upload not confirmed")
        self.retryable, self.delay = retryable, delay


class ExecutionHttp:
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
            raise ValueError("invalid execution origin or credential")
        self.url = origin.rstrip("/") + "/api/auto-trading/execution-events"
        self.client = httpx.Client(
            verify=ssl.create_default_context(cafile=ca_file),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            headers={
                "Authorization": "Bearer " + token.decode("ascii"),
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            },
        )

    def close(self) -> None:
        self.client.close()

    def send(self, body: bytes) -> bytes:
        batch = Batch.model_validate_json(canonical_bytes(parse_bytes(body, max_bytes=65536)))
        started = monotonic()
        try:
            with self.client.stream(
                "POST", self.url, content=body, timeout=httpx.Timeout(8, connect=3, pool=1)
            ) as reply:
                output = bytearray()
                for chunk in reply.iter_bytes():
                    if len(output) + len(chunk) > 65536:
                        raise ExecutionUploadError(retryable=False)
                    output.extend(chunk)
                    if monotonic() - started > 8:
                        raise ExecutionUploadError(retryable=True)
                if reply.status_code != 200:
                    retryable = reply.status_code in {429, 500, 502, 503, 504}
                    if reply.status_code == 409:
                        try:
                            error = parse_bytes(bytes(output), max_bytes=65536)
                            detail = error.get("error") if isinstance(error, dict) else None
                            retryable = (
                                isinstance(detail, dict)
                                and detail.get("code") == "EVENT_SEQUENCE_GAP"
                            )
                        except ValueError:
                            pass
                    delay = reply.headers.get("retry-after", "")
                    raise ExecutionUploadError(
                        retryable=retryable,
                        delay=int(delay) if delay.isdecimal() and 1 <= int(delay) <= 60 else 0,
                    )
                receipt = Receipt.model_validate_json(
                    canonical_bytes(parse_bytes(bytes(output), max_bytes=65536))
                )
                wanted = {e.event_id: body_hash(e) for e in batch.events}
                if {r.event_id: r.event_hash for r in receipt.acknowledgments} != wanted or len(
                    receipt.acknowledgments
                ) != len(wanted):
                    raise ExecutionUploadError(retryable=False)
                return bytes(output)
        except httpx.HTTPError as error:
            raise ExecutionUploadError(retryable=True) from error


class ExecutionUploader:
    def __init__(
        self,
        config: NodeProcessConfig,
        engine: RPCClient,
        *,
        send: Callable[[bytes], bytes] | None = None,
        seconds: Callable[[], float] = monotonic,
    ) -> None:
        self.config, self.engine, self.send, self.seconds = config, engine, send, seconds
        self.next_due = 0.0
        self.failures = 0
        self.suspended = False

    def due(self) -> bool:
        return (
            self.config.execution_reporting is not None
            and not self.suspended
            and self.seconds() >= self.next_due
        )

    def run(self) -> dict[str, JsonValue]:
        if not self.due():
            return {"status": "SKIPPED"}
        try:
            info = self.engine.invoke("GET_EXECUTION_EVENTS")
            if info.get("status") == "EMPTY":
                self.next_due = self.seconds() + 1
                return {"status": "SYNCED"}
            reference = info.get("artifact_hash")
            if info.get("status") != "READY" or not isinstance(reference, str):
                raise ValueError("invalid report handoff")
            files = ArtifactFiles(self.config.data_root / "artifacts")
            body = files.read(reference, maximum_bytes=65536)
            batch = Batch.model_validate_json(canonical_bytes(parse_bytes(body, max_bytes=65536)))
            if (batch.node_id, batch.account_id, batch.strategy_id, batch.environment) != (
                self.config.node_id,
                self.config.account_id,
                self.config.strategy_id,
                "REAL" if self.config.broker == "QMT_TRADING" else "TEST",
            ):
                raise ValueError("report scope differs")
            if self.send is not None:
                answer = self.send(body)
            else:
                assert self.config.execution_reporting is not None
                shared = read_access_token(self.config.data_root)
                token = shared
                if token is None and self.config.execution_reporting.token is not None:
                    token = resolve_secret(self.config.execution_reporting.token)
                if token is None:
                    raise ValueError("QUANT_ACCESS_REQUIRED")
                forbidden = list(
                    role_keys(self.config, ("agent", "operator", "jobrunner")).values()
                )
                forbidden.append(resolve_secret(self.config.quant_secret))
                if (
                    shared is None
                    and self.config.tail_picks is not None
                    and self.config.tail_picks.token is not None
                ):
                    forbidden.append(resolve_secret(self.config.tail_picks.token))
                if token in forbidden:
                    raise ValueError("report upload requires a distinct credential")
                client = ExecutionHttp(
                    self.config.quant_origin,
                    token,
                    ca_file=str(self.config.quant_ca_file) if self.config.quant_ca_file else None,
                )
                try:
                    answer = client.send(body)
                finally:
                    client.close()
            receipt = Receipt.model_validate_json(
                canonical_bytes(parse_bytes(answer, max_bytes=65536))
            )
            if {r.event_id: r.event_hash for r in receipt.acknowledgments} != {
                e.event_id: body_hash(e) for e in batch.events
            } or len(receipt.acknowledgments) != len(batch.events):
                raise ValueError("unconfirmed report receipt")
            receipt_hash = files.put(answer, maximum_bytes=65536)
            result = self.engine.invoke(
                "ACK_EXECUTION_EVENTS", {"batch_hash": reference, "receipt_hash": receipt_hash}
            )
            if result.get("status") != "ACKNOWLEDGED":
                raise ValueError("local report acknowledgment failed")
            self.failures = 0
            self.next_due = self.seconds() + 1
            return {"status": "UPLOADED", "event_count": len(batch.events)}
        except ExecutionUploadError as error:
            self.failures += 1
            self.suspended = not error.retryable
            self.next_due = self.seconds() + (error.delay or min(60, 2 ** min(self.failures, 6)))
            return {"status": "PENDING" if error.retryable else "NEEDS_ATTENTION"}
        except (ValueError, SecretLoadError):
            self.suspended = True
            return {"status": "NEEDS_ATTENTION"}
        finally:
            self.next_due = max(self.next_due, self.seconds() + 1)
