"""Durable, versioned operator drafts for the simple trading-settings page."""

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
from auto_trading_system.strategies.trading_settings import TradingSettings


class TradingSettingsDraft(WireModel):
    version: int = Field(ge=1)
    saved_at: AwareDatetime
    settings: TradingSettings

    @property
    def payload_hash(self) -> str:
        return digest(canonical_bytes(self.model_dump(mode="json")))


class TradingSettingsDraftError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TradingSettingsDraftStore:
    def __init__(self, data_root: Path) -> None:
        self.path = data_root / "operator" / "trading_settings.json"
        self._lock = threading.Lock()

    def read(self) -> TradingSettingsDraft | None:
        try:
            return TradingSettingsDraft.model_validate_json(self.path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValidationError) as exc:
            raise TradingSettingsDraftError("TRADING_SETTINGS_STORE_INVALID") from exc

    def save(self, settings: TradingSettings, *, expected_version: int) -> TradingSettingsDraft:
        with self._lock:
            previous = self.read()
            version = previous.version if previous else 0
            if expected_version != version:
                raise TradingSettingsDraftError("TRADING_SETTINGS_VERSION_CONFLICT")
            draft = TradingSettingsDraft(
                version=version + 1, saved_at=datetime.now(UTC), settings=settings
            )
            temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with temporary.open("xb") as handle:
                    handle.write(canonical_bytes(draft.model_dump(mode="json")))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            except OSError as exc:
                raise TradingSettingsDraftError("TRADING_SETTINGS_SAVE_FAILED") from exc
            finally:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
            return draft
