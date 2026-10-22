"""Operator-edited rule drafts. Saving never grants an engine trading permission."""

from __future__ import annotations

import os
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import AwareDatetime, Field, ValidationError

from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.strategies.sell_rules import SellRules


class SellRuleDraft(WireModel):
    version: int = Field(ge=1)
    saved_at: AwareDatetime
    rules: SellRules

    @property
    def payload_hash(self) -> str:
        return digest(canonical_bytes(self.model_dump(mode="json")))


class SellRuleDraftError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SellRuleDraftStore:
    def __init__(self, data_root: Path) -> None:
        self.path = data_root / "operator" / "sell_rules.json"
        self._lock = threading.Lock()

    def read(self) -> SellRuleDraft | None:
        try:
            return SellRuleDraft.model_validate_json(self.path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValidationError) as exc:
            raise SellRuleDraftError("RULE_STORE_INVALID") from exc

    def save(self, rules: SellRules, *, expected_version: int) -> SellRuleDraft:
        with self._lock:
            previous = self.read()
            version = previous.version if previous else 0
            if expected_version != version:
                raise SellRuleDraftError("RULE_VERSION_CONFLICT")
            draft = SellRuleDraft(version=version + 1, saved_at=datetime.now(UTC), rules=rules)
            temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with temporary.open("xb") as handle:
                    handle.write(canonical_bytes(draft.model_dump(mode="json")))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            except OSError as exc:
                raise SellRuleDraftError("RULE_SAVE_FAILED") from exc
            finally:
                # A stale .tmp file is never read. Cleanup errors must not mask
                # a committed draft or the original write error.
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
            return draft
