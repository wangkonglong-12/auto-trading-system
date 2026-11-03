"""Local console preferences; backend changes take effect on process restart."""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator

from auto_trading_system.config.models import StrictConfigModel


class SystemSettings(StrictConfigModel):
    version: int = Field(default=0, ge=0)
    close_warning: bool = True
    quant_scheme: Literal["http", "https"] = "https"
    quant_host: str | None = Field(default=None, max_length=253)
    quant_port: int = Field(default=443, ge=1, le=65535)

    @field_validator("quant_host")
    @classmethod
    def host_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            pass
        value = value.encode("idna").decode("ascii")
        if len(value) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in value.split(".")
        ):
            raise ValueError("backend host must be a domain or IP without URL components")
        return value

    @property
    def quant_origin(self) -> str | None:
        if self.quant_host is None:
            return None
        host = f"[{self.quant_host}]" if ":" in self.quant_host else self.quant_host
        return f"{self.quant_scheme}://{host}:{self.quant_port}"


class SystemSettingsError(RuntimeError):
    pass


class SystemSettingsStore:
    def __init__(self, root: Path) -> None:
        self.path = root / "system-settings.json"
        self._lock = Lock()

    def read(self) -> SystemSettings:
        try:
            with self.path.open("rb") as source:
                return SystemSettings.model_validate_json(source.read(4097))
        except FileNotFoundError:
            return SystemSettings()
        except (OSError, ValueError):
            raise SystemSettingsError("SYSTEM_SETTINGS_STORE_INVALID") from None

    def save(self, value: SystemSettings) -> SystemSettings:
        with self._lock:
            current = self.read()
            if current.version != value.version:
                raise SystemSettingsError("SYSTEM_SETTINGS_VERSION_CONFLICT")
            updated = value.model_copy(update={"version": current.version + 1})
            temporary = self.path.with_suffix("." + uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as output:
                    output.write(updated.model_dump_json().encode("utf-8"))
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            except OSError:
                raise SystemSettingsError("SYSTEM_SETTINGS_SAVE_FAILED") from None
            finally:
                temporary.unlink(missing_ok=True)
            return updated
