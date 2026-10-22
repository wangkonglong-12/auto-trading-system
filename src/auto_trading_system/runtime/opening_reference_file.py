"""Atomic local pointer to the last sealed portfolio checkpoint."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference


def load_opening_reference(path: Path) -> OpeningReference | None:
    try:
        return OpeningReference.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        return None


def save_opening_reference(path: Path, reference: OpeningReference) -> None:
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb") as handle:
            handle.write(canonical_bytes(reference.model_dump(mode="json")))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
