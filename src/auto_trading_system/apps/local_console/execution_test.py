"""Authenticated console test job, separate from live plans, slots and QMT."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Event as StopEvent
from threading import Lock
from time import sleep
from typing import Literal, cast
from uuid import uuid4

from pydantic import Field, JsonValue

from auto_trading_system.protocol.executions.models import Draft, Event, Model, Receipt, body_hash
from auto_trading_system.protocol.executions.projection import State, Summary, apply_event, summary
from auto_trading_system.protocol.tail_picks import CN, TailPicksPreview
from auto_trading_system.protocol.v2.canonical import canonical_bytes, parse_bytes
from auto_trading_system.runtime.single_instance import (
    AccountInstanceGuard,
    WindowsNamedMutexBackend,
)
from auto_trading_system.sync.execution_store import ExecutionStore
from auto_trading_system.sync.execution_test_scenarios import scenario
from auto_trading_system.sync.execution_test_transport import (
    HttpTestTransport,
    TestConnection,
    TestTransport,
    load_connection,
)


class TestRecord(Model):
    preview: TailPicksPreview
    maximum: int = Field(ge=1, le=200)
    amount: str = Field(pattern=r"^(0|[1-9][0-9]{0,11})\.[0-9]{2}$")
    events: tuple[Event, ...] = Field(default=(), max_length=10000)
    verified_at: datetime | None = None


class ExecutionTestService:
    def __init__(
        self,
        root: Path,
        *,
        connection: TestConnection | None = None,
        transport: TestTransport | None = None,
        origin: str | None = None,
        ca_file: str | None = None,
    ) -> None:
        self.root = root / "execution-test"
        self._access_root = root
        self._connection, self._transport = connection, transport
        self._origin, self._ca_file = origin, ca_file
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="console-execution-test")
        self._lock, self._stop = Lock(), StopEvent()
        self._busy = False
        self._value: dict[str, JsonValue] = {"state": "IDLE", "rows": [], "error": None}
        self._record: TestRecord | None = None
        self._error: Exception | None = None
        self._pool.submit(self._restore)

    def _path(self, name: str) -> Path:
        if self.root.resolve() != self.root.absolute() or self.root.is_symlink():
            raise ValueError("invalid test directory")
        self.root.mkdir(exist_ok=True)
        path = self.root / name
        if path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
            raise ValueError("invalid test file")
        return path

    def _write(self, name: str, body: bytes) -> None:
        path = self._path(name)
        temporary = self._path(".pending-" + uuid4().hex)
        try:
            with temporary.open("xb") as out:
                out.write(body)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self, name: str) -> bytes:
        with self._path(name).open("rb") as source:
            body = source.read(16 * 1024 * 1024 + 1)
        parse_bytes(body, max_bytes=16 * 1024 * 1024)
        return body

    def _restore(self) -> None:
        try:
            path = self._path("active.json")
            if path.exists():
                value = parse_bytes(self._read("active.json"), max_bytes=4096)
                if not isinstance(value, dict) or set(value) != {"date"}:
                    raise ValueError("invalid test index")
                if value["date"] is None:
                    self._publish("EMPTY")
                    return
                from datetime import date

                day = date.fromisoformat(str(value["date"]))
                self._record = TestRecord.model_validate_json(self._read(str(day) + ".json"))
                self._publish("SYNCED" if self._record.verified_at else "READY")
        except Exception:
            self._record = None
            self._publish("ERROR", "TEST_STORAGE_INVALID")

    def _save(self) -> None:
        assert self._record is not None
        day = self._record.preview.trade_date
        self._write(str(day) + ".json", self._record.model_dump_json().encode())
        self._write("active.json", canonical_bytes({"date": str(day)}))

    def _states(self) -> dict[str, State]:
        values: dict[str, State] = {}
        if self._record:
            for event in self._record.events:
                if (
                    event.identity.environment != "TEST"
                    or event.identity.selection_date != self._record.preview.trade_date
                    or event.identity.symbol
                    not in {s.code for b in self._record.preview.batches for s in b.stocks}
                ):
                    raise ValueError("invalid retained test identity")
                key = event.identity.execution_id
                values[key] = apply_event(values.get(key, State(identity=event.identity)), event)
        return values

    def _publish(self, state: str, error: str | None = None, *, uploaded: int = 0) -> None:
        record = self._record
        states = self._states()
        rows: list[JsonValue] = []
        if record:
            for symbol, name in {
                s.code: s.name for b in record.preview.batches for s in b.stocks
            }.items():
                found = next((v for v in states.values() if v.identity.symbol == symbol), None)
                main = summary(found) if found else None
                rows.append(
                    cast(
                        JsonValue,
                        {
                            "symbol": symbol,
                            "name": name,
                            "execution": main.model_dump(mode="json") if main else None,
                            "events": [
                                e.model_dump(mode="json")
                                for e in record.events
                                if e.identity.symbol == symbol
                            ],
                        },
                    )
                )
        value: dict[str, JsonValue] = {
            "state": state,
            "error": error,
            "mode": "TEST",
            "executable": False,
            "selection_date": str(record.preview.trade_date) if record else None,
            "requested_date": str(record.preview.requested_date) if record else None,
            "maximum": record.maximum if record else None,
            "amount": record.amount if record else None,
            "rows": rows,
            "uploaded_events": uploaded,
            "verified_at": record.verified_at.isoformat()
            if record and record.verified_at
            else None,
            "phases": [b.phase for b in record.preview.batches if b.status == "READY"]
            if record
            else [],
        }
        with self._lock:
            self._value = value

    def snapshot(self) -> dict[str, JsonValue]:
        with self._lock:
            return cast(
                dict[str, JsonValue], json.loads(json.dumps({**self._value, "busy": self._busy}))
            )

    def submit(
        self, action: Literal["FETCH", "RUN", "RETRY"], *, maximum: int, amount: str
    ) -> None:
        with self._lock:
            if self._busy or self._stop.is_set():
                raise ValueError("test job busy")
            self._busy = True
        self._pool.submit(self._run, action, maximum, amount)

    def _run(self, action: str, maximum: int, amount: str) -> None:
        guard: AccountInstanceGuard | None = None
        try:
            connection = self._connection or load_connection()
            if connection is None and self._origin is not None:
                connection = TestConnection(
                    node_id="operator-console-test",
                    account_id="operator-console-test",
                    strategy_id="TAIL_CLOSE",
                )
            if connection is None:
                self._publish("ERROR", "TEST_CONNECTION_REQUIRED")
                return
            if self._transport is None and self._origin is None:
                raise ValueError("TEST_HTTP_ORIGIN_REQUIRED")
            remote = self._transport or HttpTestTransport(
                self._origin or "", connection, ca_file=self._ca_file, access_root=self._access_root
            )
            scope = connection.storage_scope()
            guard = AccountInstanceGuard(
                "TEST" + scope[7:67], backend=WindowsNamedMutexBackend(), namespace="Local"
            )
            guard.acquire()
            if connection.storage_root is not None and self.root != connection.storage_root:
                self.root = connection.storage_root
                self._record = None
                self._restore()
            store = ExecutionStore(
                self._path("outbox.json"), scope=scope, node_id=connection.node_id
            )
            if action == "FETCH":
                if store.state.pending:
                    self._publish("ERROR", "TEST_PENDING_UPLOAD")
                    return
                self._publish("FETCHING")
                preview = TailPicksPreview.model_validate_json(remote.call("PREVIEW", {}))
                preview.check_current(datetime.now(CN))
                if preview.status == "NOT_FOUND":
                    self._record = None
                    self._write("active.json", canonical_bytes({"date": None}))
                    self._publish("EMPTY")
                    return
                if len({s.code for b in preview.batches for s in b.stocks}) > 200:
                    self._publish("ERROR", "TEST_TOO_MANY_STOCKS")
                    return
                path = self._path(str(preview.trade_date) + ".json")
                if path.exists():
                    record = TestRecord.model_validate_json(self._read(path.name))
                    if record.preview.batches != preview.batches:
                        raise ValueError("retained historical picks changed")
                    self._record = record
                else:
                    self._record = TestRecord(preview=preview, maximum=maximum, amount=amount)
                self._save()
                self._publish("SYNCED" if self._record.verified_at else "READY")
                return
            if self._record is None:
                self._publish("ERROR", "TEST_FETCH_REQUIRED")
                return
            if not self._record.events:
                if action != "RUN":
                    self._publish("ERROR", "TEST_RUN_REQUIRED")
                    return
                self._publish("SIMULATING")
                events = scenario(
                    self._record.preview,
                    node_account=connection.account_id,
                    strategy=connection.strategy_id,
                    maximum=self._record.maximum,
                    amount=self._record.amount,
                    now=datetime.now(CN),
                )
                self._record = self._record.model_copy(update={"events": events})
                self._save()  # Freeze times/IDs before the first network request.
            self._record = self._record.model_copy(update={"verified_at": None})
            self._save()
            for event in self._record.events:
                if (
                    event.identity.account_id,
                    event.identity.strategy_id,
                    event.identity.environment,
                ) != (connection.account_id, connection.strategy_id, "TEST"):
                    raise ValueError("test scope changed")
                store.append(Draft(**event.model_dump(exclude={"seq"})))
            self._publish("UPLOADING")
            uploaded = 0
            repeated = False
            while batch := store.batch():
                if self._stop.is_set():
                    raise ValueError("test closing")
                body = batch.model_dump(mode="json")
                receipt = Receipt.model_validate_json(remote.call("SEND", body))
                if not repeated:
                    sleep(0.3)
                    duplicate = Receipt.model_validate_json(remote.call("SEND", body))
                    if duplicate != receipt:
                        raise ValueError("idempotent receipt mismatch")
                    repeated = True
                store.acknowledge(batch, receipt)
                uploaded += len(batch.events)
                self._publish("UPLOADING", uploaded=uploaded)
                sleep(0.3)
            states = self._states()
            ids = list(states)
            for start in range(0, len(ids), 20):
                if self._stop.is_set():
                    raise ValueError("test closing")
                response = parse_bytes(
                    remote.call("READ", {"ids": ids[start : start + 20]}), max_bytes=1024 * 1024
                )
                if not isinstance(response, list) or len(response) != len(ids[start : start + 20]):
                    raise ValueError("incomplete test readback")
                seen: set[str] = set()
                for item in response:
                    if not isinstance(item, dict) or not isinstance(item.get("execution"), dict):
                        raise ValueError("invalid test readback")
                    main = Summary.model_validate_json(canonical_bytes(item["execution"]))
                    if (
                        main.execution_id not in ids[start : start + 20]
                        or main.execution_id in seen
                    ):
                        raise ValueError("unexpected test readback identity")
                    seen.add(main.execution_id)
                    if main != summary(states[main.execution_id]):
                        raise ValueError("test master readback mismatch")
                    expected = {
                        e.event_id: body_hash(e)
                        for e in self._record.events
                        if e.identity.execution_id == main.execution_id
                    }
                    if item.get("event_hashes") != expected:
                        raise ValueError("test child readback mismatch")
            self._record = self._record.model_copy(update={"verified_at": datetime.now(CN)})
            self._save()
            self._publish("SYNCED", uploaded=uploaded)
        except Exception as error:
            self._error = error
            code = str(error)
            self._publish(
                "ERROR",
                code
                if code in {"TEST_HTTP_CREDENTIALS_REQUIRED", "TEST_HTTP_ORIGIN_REQUIRED"}
                else "TEST_REQUEST_FAILED",
            )
        finally:
            if guard is not None and guard.is_acquired:
                guard.release()
            with self._lock:
                self._busy = False

    def close(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=True, cancel_futures=True)
