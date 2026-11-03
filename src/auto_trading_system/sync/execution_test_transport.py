"""Direct HTTP test transport; SSH remains an explicitly invoked diagnostic only."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Protocol
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import Field

from auto_trading_system.config.models import SecretReference
from auto_trading_system.config.node_v2 import resolve_secret
from auto_trading_system.config.quant_access import read_access_token
from auto_trading_system.protocol.executions.models import Batch, Id, Model
from auto_trading_system.protocol.tail_picks import CN
from auto_trading_system.protocol.v2.canonical import canonical_bytes, parse_bytes


class TestTransport(Protocol):
    def call(self, operation: str, payload: dict[str, object]) -> bytes: ...


class TestConnection(Model):
    host: str | None = Field(default=None, pattern=r"^[A-Za-z0-9.-]{1,253}$")
    port: int = Field(default=22, ge=1, le=65535)
    user: str | None = Field(default=None, pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    private_key: Path | None = None
    known_hosts: Path | None = None
    python: str | None = Field(default=None, pattern=r"^/[A-Za-z0-9_./-]+$")
    script: str | None = Field(default=None, pattern=r"^/[A-Za-z0-9_./-]+$")
    node_id: Id
    account_id: Id
    strategy_id: Id
    storage_root: Path | None = None
    read_token: SecretReference | None = Field(default=None, exclude_if=lambda value: value is None)
    upload_token: SecretReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    def storage_scope(self) -> str:
        # Credential storage changes must not strand existing immutable test events.
        body = json.dumps(
            self.model_dump(mode="json", exclude={"read_token", "upload_token"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        return "sha256:" + hashlib.sha256(body).hexdigest()


def connection_path() -> Path:
    return (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        / "AutoTradingSystem/execution-test.json"
    )


def load_connection() -> TestConnection | None:
    path = connection_path()
    if not path.exists():
        return None
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("invalid test connection path")
    with path.open("rb") as source:
        body = source.read(8193)
    parse_bytes(body, max_bytes=8192)
    return TestConnection.model_validate_json(body)


class SshTestTransport:
    def __init__(self, connection: TestConnection) -> None:
        self.connection = connection

    def call(self, operation: str, payload: dict[str, object]) -> bytes:
        if operation not in {"PREVIEW", "SEND", "READ"}:
            raise ValueError("invalid test operation")
        c = self.connection
        if (
            c.private_key is None
            or c.known_hosts is None
            or c.host is None
            or c.user is None
            or c.python is None
            or c.script is None
        ):
            raise ValueError("SSH diagnostic connection is incomplete")
        for path in (c.private_key, c.known_hosts):
            if not path.is_absolute() or path.resolve() != path or not path.is_file():
                raise ValueError("invalid SSH trust configuration")
        body = json.dumps({"operation": operation, "payload": payload}, ensure_ascii=False).encode()
        if len(body) > 65536:
            raise ValueError("test request too large")
        reply = subprocess.run(
            [
                "ssh",
                "-i",
                str(c.private_key),
                "-o",
                "UserKnownHostsFile=" + str(c.known_hosts),
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "ConnectTimeout=5",
                "-p",
                str(c.port),
                c.user + "@" + c.host,
                c.python + " -B " + c.script,
            ],
            input=body,
            capture_output=True,
            timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if reply.returncode or len(reply.stdout) > 1024 * 1024:
            raise ValueError("test server unavailable")
        value = parse_bytes(reply.stdout, max_bytes=1024 * 1024)
        if not isinstance(value, dict) or value.get("status") != "OK" or "data" not in value:
            raise ValueError("test server rejected operation")
        return canonical_bytes(value["data"])


class HttpTestTransport:
    def __init__(
        self,
        origin: str,
        connection: TestConnection,
        *,
        ca_file: str | None = None,
        transport: httpx.BaseTransport | None = None,
        access_root: Path | None = None,
    ) -> None:
        parts = urlsplit(origin)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError("invalid test HTTP origin")
        self.origin, self.connection = origin.rstrip("/"), connection
        self.ca_file, self.transport = ca_file, transport
        self.access_root = access_root

    def call(self, operation: str, payload: dict[str, object]) -> bytes:
        c = self.connection
        shared = read_access_token(self.access_root) if self.access_root is not None else None
        if operation == "PREVIEW" and payload == {}:
            if shared is None and c.read_token is None:
                raise ValueError("TEST_HTTP_CREDENTIALS_REQUIRED")
            token = shared if shared is not None else resolve_secret(c.read_token)
            method, path, body = (
                "GET",
                "/api/auto-trading/tail-picks?"
                + urlencode({"trade_date": datetime.now(CN).date().isoformat(), "test": "true"}),
                None,
            )
        elif operation in {"SEND", "READ"}:
            if shared is None and c.upload_token is None:
                raise ValueError("TEST_HTTP_CREDENTIALS_REQUIRED")
            token = shared if shared is not None else resolve_secret(c.upload_token)
            if operation == "SEND":
                batch = Batch.model_validate_json(canonical_bytes(payload))
                if (batch.node_id, batch.account_id, batch.strategy_id, batch.environment) != (
                    c.node_id,
                    c.account_id,
                    c.strategy_id,
                    "TEST",
                ):
                    raise ValueError("test upload scope mismatch")
            elif (
                set(payload) != {"ids"}
                or not isinstance(payload["ids"], list)
                or not 1 <= len(payload["ids"]) <= 20
                or not all(
                    isinstance(key, str) and re.fullmatch(r"exec-[a-f0-9]{64}", key)
                    for key in payload["ids"]
                )
            ):
                raise ValueError("invalid test readback identities")
            method, path = (
                "POST",
                (
                    "/api/auto-trading/execution-events"
                    if operation == "SEND"
                    else "/api/auto-trading/execution-test/readback"
                ),
            )
            body = canonical_bytes(payload)
        else:
            raise ValueError("invalid test operation")
        if not re.fullmatch(rb"[A-Za-z0-9_-]{43,256}", token) or (
            body is not None and len(body) > 65536
        ):
            raise ValueError("invalid test credential or body")
        started = monotonic()
        with (
            httpx.Client(
                transport=self.transport,
                verify=self.ca_file or True,
                trust_env=False,
                follow_redirects=False,
            ) as client,
            client.stream(
                method,
                self.origin + path,
                content=body,
                timeout=httpx.Timeout(8, connect=3),
                headers={
                    "Authorization": "Bearer " + token.decode("ascii"),
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as reply,
        ):
            output = bytearray()
            for chunk in reply.iter_bytes():
                if monotonic() - started > 8:
                    raise ValueError("test HTTP request timed out")
                if len(output) + len(chunk) > 1024 * 1024:
                    raise ValueError("test response too large")
                output.extend(chunk)
            if reply.status_code != 200:
                raise ValueError("test HTTP request rejected")
            parse_bytes(bytes(output), max_bytes=1024 * 1024)
            return bytes(output)
