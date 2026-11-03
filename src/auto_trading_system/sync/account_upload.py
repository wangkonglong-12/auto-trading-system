"""Automatic minute reporting. No UI session, trading authorization or manual resume."""

from __future__ import annotations

import ssl
from collections.abc import Callable
from time import monotonic, sleep
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from auto_trading_system.config.node_v2 import NodeProcessConfig, resolve_secret, role_keys
from auto_trading_system.control.ipc_protocol import IPCError, RPCClient
from auto_trading_system.protocol.account_monitoring import MAX_BODY, Batch, Receipt, fingerprint
from auto_trading_system.sync.account_store import AccountStore, read_observation


class AccountHttp:
    def __init__(
        self,
        origin: str,
        token: bytes,
        *,
        ca_file: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        import re

        parts = urlsplit(origin)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or not re.fullmatch(rb"[A-Za-z0-9_-]{43,256}", token)
        ):
            raise ValueError("invalid account upload origin or credential")
        self.url = origin.rstrip("/") + "/api/auto-trading/account-snapshots"
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

    def send(self, batch: Batch) -> Receipt:
        body = batch.model_dump_json().encode()
        if len(body) > MAX_BODY:
            raise ValueError("account batch too large")
        started = monotonic()
        with self.client.stream(
            "POST", self.url, content=body, timeout=httpx.Timeout(8, connect=3, pool=1)
        ) as response:
            output = bytearray()
            for chunk in response.iter_bytes():
                if len(output) + len(chunk) > 4096 or monotonic() - started > 8:
                    raise ValueError("invalid account receipt")
                output.extend(chunk)
            if response.status_code != 200:
                raise ValueError("account upload rejected")
            receipt = Receipt.model_validate_json(bytes(output))
            if receipt.batch_hash != fingerprint(batch):
                raise ValueError("account upload not confirmed")
            return receipt


class AccountUploader:
    def __init__(
        self,
        config: NodeProcessConfig,
        engine: RPCClient,
        *,
        seconds: Callable[[], float] = monotonic,
        send: Callable[[Batch], Receipt] | None = None,
    ) -> None:
        self.config, self.engine, self.seconds, self.send = config, engine, seconds, send
        self.next_due = 0.0

    def due(self) -> bool:
        return self.seconds() >= self.next_due

    def run(self) -> dict[str, JsonValue]:
        if not self.due():
            return {"status": "SKIPPED"}
        self.next_due = self.seconds() + 60
        store = None
        try:
            store = AccountStore(
                self.config.data_root / "jobs/.account-reporting.sqlite3",
                self.config.node_id,
                self.config.account_id,
            )
            collected = True
            try:
                for attempt in range(5):
                    try:
                        reply = self.engine.invoke("GET_ACCOUNT_OBSERVATION")
                        break
                    except IPCError as error:
                        if str(error) != "ACCOUNT_QUERY_BUSY" or attempt == 4:
                            raise
                        # Avoid repeatedly colliding with the engine's periodic SDK query.
                        # This waits only in the independent upload worker, never the owner.
                        sleep(0.25)
                hashed = reply.get("observation_hash")
                if not isinstance(hashed, str):
                    raise ValueError("invalid account handoff")
                observation = read_observation(
                    self.config.data_root / "jobs/.account-observation.json", hashed
                )
                store.capture(observation)
            except Exception:
                # Already durable fills remain deliverable even while QMT is unavailable.
                collected = False
            batch = store.batch()
            if batch is None:
                return {"status": "PENDING", "reason": "ACCOUNT_OBSERVATION_UNAVAILABLE"}
            if self.send is not None:
                receipt = self.send(batch)
            else:
                settings = self.config.account_reporting
                if settings is None:
                    raise ValueError("account reporting not configured")
                token = resolve_secret(settings.token)
                if token in (
                    *role_keys(self.config, ("agent", "operator", "jobrunner")).values(),
                    resolve_secret(self.config.quant_secret),
                ):
                    raise ValueError("account credential must be distinct")
                client = AccountHttp(
                    settings.origin or self.config.quant_origin,
                    token,
                    ca_file=(str(self.config.quant_ca_file) if self.config.quant_ca_file else None),
                )
                try:
                    receipt = client.send(batch)
                finally:
                    client.close()
            store.acknowledge(batch, receipt)
            return {
                "status": "UPLOADED" if collected else "RETAINED_DATA_UPLOADED",
                "received_at": receipt.received_at.isoformat(),
                "captured_at": batch.snapshot.observation.captured_at.isoformat(),
                "trade_count": len(batch.trades),
            }
        except Exception:
            # Retry after credentials/configuration/network recovery; never latch suspended.
            return {"status": "PENDING", "reason": "ACCOUNT_UPLOAD_UNCONFIRMED"}
        finally:
            if store is not None:
                store.close()
