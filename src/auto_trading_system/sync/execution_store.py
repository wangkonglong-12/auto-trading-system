"""Single-worker durable report outbox. It never opens the trading Journal."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from auto_trading_system.protocol.executions.models import (
    Batch,
    Draft,
    Event,
    Identity,
    Model,
    Receipt,
    body_hash,
)
from auto_trading_system.protocol.v2.canonical import parse_bytes

MAX_STATE = 32 * 1024 * 1024


class Stream(Model):
    identity: Identity
    seq: int = Field(default=0, ge=0)
    seen: dict[str, str] = Field(default_factory=dict)


class Stored(Model):
    scope: str
    streams: dict[str, Stream] = Field(default_factory=dict)
    pending: dict[str, Event] = Field(default_factory=dict)
    orders: dict[str, Identity] = Field(default_factory=dict)
    lots: dict[str, Identity] = Field(default_factory=dict)


class ExecutionStore:
    def __init__(self, path: Path, *, scope: str, node_id: str) -> None:
        self.path, self.scope, self.node_id = path, scope, node_id
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError("report store must be a direct private path")
        if path.exists():
            if not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError("report store must be a private regular file")
            with path.open("rb") as source:
                body = source.read(MAX_STATE + 1)
            parse_bytes(body, max_bytes=MAX_STATE)
            self.state = Stored.model_validate_json(body)
            if self.state.scope != scope:
                raise ValueError("retained report scope differs")
            for event_id, event in self.state.pending.items():
                stream = self.state.streams.get(event.identity.execution_id)
                if event_id != event.event_id or stream is None or event.seq > stream.seq:
                    raise ValueError("retained report index is inconsistent")
        else:
            self.state = Stored(scope=scope)

    def _save(self, state: Stored) -> None:
        body = state.model_dump_json().encode()
        if len(body) > MAX_STATE or len(state.pending) > 10000 or len(state.streams) > 10000:
            raise ValueError("report outbox capacity exceeded; maintenance required")
        partial = self.path.with_name(".execution-report-" + uuid4().hex)
        try:
            with partial.open("xb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.replace(partial, self.path)
            self.state = state
        finally:
            partial.unlink(missing_ok=True)

    def append(self, draft: Draft) -> None:
        key = draft.identity.execution_id
        old = self.state.streams.get(key, Stream(identity=draft.identity))
        # Replay observations can have a later observation time; actual trade time cannot change.
        semantic = body_hash(draft if draft.fact.kind == "TRADE" else draft.fact)
        previous = old.seen.get(draft.source_key)
        if previous is not None:
            if previous != semantic:
                raise ValueError("same report source has conflicting content")
            return
        event = Event(**draft.model_dump(), seq=old.seq + 1)
        streams, pending = dict(self.state.streams), dict(self.state.pending)
        streams[key] = Stream(
            identity=draft.identity, seq=event.seq, seen={**old.seen, draft.source_key: semantic}
        )
        pending[event.event_id] = event
        self._save(self.state.model_copy(update={"streams": streams, "pending": pending}))

    def bind(self, *, order_id: str, identity: Identity, lot_id: str | None = None) -> None:
        orders, lots = dict(self.state.orders), dict(self.state.lots)
        if order_id in orders and orders[order_id] != identity:
            raise ValueError("report order belongs to another execution")
        orders[order_id] = identity
        if lot_id is not None:
            if lot_id in lots and lots[lot_id] != identity:
                raise ValueError("report lot belongs to another execution")
            lots[lot_id] = identity
        if orders != self.state.orders or lots != self.state.lots:
            self._save(self.state.model_copy(update={"orders": orders, "lots": lots}))

    def batch(self) -> Batch | None:
        values = sorted(
            self.state.pending.values(), key=lambda e: (e.identity.execution_id, e.seq)
        )[:20]
        if not values:
            return None
        identity = values[0].identity
        return Batch(
            schema_version=1,
            node_id=self.node_id,
            account_id=identity.account_id,
            strategy_id=identity.strategy_id,
            environment=identity.environment,
            events=tuple(values),
        )

    def acknowledge(self, batch: Batch, receipt: Receipt) -> None:
        if batch.node_id != self.node_id:
            raise ValueError("report acknowledgment belongs to another node")
        expected = {e.event_id: body_hash(e) for e in batch.events}
        received = {r.event_id: r.event_hash for r in receipt.acknowledgments}
        if len(received) != len(receipt.acknowledgments) or received != expected:
            raise ValueError("report receipt must confirm the exact whole batch")
        pending = dict(self.state.pending)
        for event in batch.events:
            saved = pending.get(event.event_id)
            if saved is not None and saved != event:
                raise ValueError("receipt differs from retained event")
            pending.pop(event.event_id, None)
        self._save(self.state.model_copy(update={"pending": pending}))
