"""Append-only date/revision references, independent of the mutable job index.

Revision one retains the dev50 filename. Later revisions get separate immutable
references; there is no replaceable 'latest' pointer to lose during a crash.
The engine publishes full frozen observations. Agents discover every revision,
including predecessors after an index loss, without touching the Journal.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from itertools import islice
from pathlib import Path
from uuid import uuid4

from auto_trading_system.protocol.v2.canonical import canonical_bytes, parse_bytes
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.daily_files import FrozenBatch, check_daily_successor, load_daily

MAX_DAILY_REVISIONS = 32
_REFERENCE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:\.r(\d{4}))?\.json")


def closing_slot(now: datetime) -> datetime:
    """One fresh observation per 15-minute closing slot, including 18:00."""
    if now.utcoffset() is None:
        raise ValueError("closing clock must be timezone aware")
    local = now.astimezone(timezone(timedelta(hours=8)))
    if not time(15, 30) <= local.time() < time(18, 1):
        raise ValueError("outside closing refresh window")
    return local.replace(minute=local.minute // 15 * 15, second=0, microsecond=0)


class DailyCatalog:
    def __init__(
        self, root: Path, files: ArtifactFiles, *, node_id: str, account_id: str, generation: int
    ) -> None:
        self.root, self.files = root, files
        self.scope = (node_id, account_id, generation)

    def _references(self) -> list[tuple[date, int, Path]]:
        with os.scandir(self.root) as entries:
            names = list(islice((e.name for e in entries if _REFERENCE.fullmatch(e.name)), 4001))
        if len(names) > 4000:
            raise ValueError("daily catalog retention review required")
        references: list[tuple[date, int, Path]] = []
        for name in names:
            match = _REFERENCE.fullmatch(name)
            assert match is not None
            day, suffix = match.groups()
            revision = int(suffix) if suffix else 1
            if suffix and not 2 <= revision <= MAX_DAILY_REVISIONS:
                raise ValueError("invalid daily reference revision")
            references.append((date.fromisoformat(day), revision, self.root / name))
        return sorted(references)

    def _read(self, day: date, revision: int, path: Path) -> FrozenBatch:
        if path.is_symlink():
            raise ValueError("catalog links are invalid")
        with path.open("rb") as source:
            value = parse_bytes(source.read(1025), max_bytes=1024)
        if (
            not isinstance(value, dict)
            or set(value) != {"artifact_hash"}
            or not isinstance(value["artifact_hash"], str)
        ):
            raise ValueError("invalid daily reference")
        batch = load_daily(self.files, value["artifact_hash"])
        manifest = batch.manifest
        if (
            (
                manifest.node_id,
                manifest.account_id,
                manifest.node_generation,
            )
            != self.scope
            or manifest.trade_date != day
            or manifest.revision != revision
        ):
            raise ValueError("daily reference scope differs")
        return batch

    def _chain(self, references: list[tuple[date, int, Path]]) -> Iterator[FrozenBatch]:
        prior: FrozenBatch | None = None
        for day, revision, path in references:
            batch = self._read(day, revision, path)
            if prior is None or prior.manifest.trade_date != day:
                if revision != 1:
                    raise ValueError("daily correction chain is missing its first revision")
            else:
                check_daily_successor(prior.manifest, batch.manifest)
            yield batch
            prior = batch

    def get(self, day: date) -> FrozenBatch | None:
        """Read the latest complete chain; corruption is never an empty day."""
        latest = None
        for item in self._chain([ref for ref in self._references() if ref[0] == day]):
            latest = item
        return latest

    def publish(self, batch: FrozenBatch) -> FrozenBatch:
        manifest = batch.manifest
        if (
            manifest.node_id,
            manifest.account_id,
            manifest.node_generation,
        ) != self.scope or load_daily(self.files, batch.artifact_hash) != batch:
            raise ValueError("cannot publish foreign or incomplete export")
        references = self._references()
        prior = None
        for item in self._chain([ref for ref in references if ref[0] == manifest.trade_date]):
            if item.manifest.revision == manifest.revision:
                if item != batch:
                    raise ValueError("daily revision already has different frozen content")
                return item
            prior = item
        if prior is None:
            if manifest.revision != 1:
                raise ValueError("daily correction requires its published predecessor")
        else:
            check_daily_successor(prior.manifest, manifest)
        if manifest.revision > MAX_DAILY_REVISIONS or len(references) >= 4000:
            raise ValueError("daily catalog retention review required")
        suffix = "" if manifest.revision == 1 else f".r{manifest.revision:04d}"
        target = self.root / (manifest.trade_date.isoformat() + suffix + ".json")
        pending = self.root / (".pending-" + uuid4().hex)
        try:
            with pending.open("xb") as output:
                output.write(canonical_bytes({"artifact_hash": batch.artifact_hash}))
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(pending, target)
            except FileExistsError:
                if self._read(manifest.trade_date, manifest.revision, target) != batch:
                    raise ValueError("concurrent daily revision conflict") from None
        finally:
            # Remove only our scratch reference; published revisions are retained.
            pending.unlink(missing_ok=True)
        return batch

    def discover(self) -> Iterator[FrozenBatch]:
        # Keep one parsed manifest at a time, rather than years of record bodies.
        yield from self._chain(self._references())
