"""Independent operator configuration; saving it never enables an execution owner."""

from __future__ import annotations

import os
import threading
from contextlib import suppress
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes


class BoardSellRule(WireModel):
    mode: Literal["TIME_ONLY", "TAKE_PROFIT_OR_TIME"]
    take_profit_pct: str | None = Field(
        default=None, pattern=r"^(0|[1-9][0-9]{0,2})(\.[0-9]{1,2})?$"
    )
    sell_at: str = Field(pattern=r"^(09|10|11|13|14):[0-5][0-9]$")

    @model_validator(mode="after")
    def valid_rule(self) -> BoardSellRule:
        if self.mode == "TAKE_PROFIT_OR_TIME":
            if self.take_profit_pct is None or not 0 < Decimal(self.take_profit_pct) <= 100:
                raise ValueError("止盈模式需填写大于0且不超过100%的止盈比例，最多两位小数。")
        elif self.take_profit_pct is not None:
            raise ValueError("定时退出不应携带止盈比例。")
        clock = time.fromisoformat(self.sell_at)
        if not (time(9, 30) <= clock <= time(11, 25) or time(13) <= clock <= time(14, 50)):
            raise ValueError("退出时间须为09:30至11:25或13:00至14:50。")
        return self


class BoardSellConfiguration(WireModel):
    main: BoardSellRule = Field(
        default_factory=lambda: BoardSellRule(
            mode="TAKE_PROFIT_OR_TIME", take_profit_pct="7.00", sell_at="10:30"
        )
    )
    chinext: BoardSellRule = Field(
        default_factory=lambda: BoardSellRule(
            mode="TIME_ONLY", take_profit_pct=None, sell_at="10:50"
        )
    )
    include_automatic: bool = True
    include_system_manual: bool = True
    cancel_after_seconds: int = Field(default=5, ge=1, le=300)
    max_reposts: int = Field(default=3, ge=0, le=100)


class BoardSellDraft(WireModel):
    version: int = Field(ge=1)
    saved_at: AwareDatetime
    settings: BoardSellConfiguration


class BoardSellConfigurationError(RuntimeError):
    pass


class BoardSellConfigurationStore:
    def __init__(self, root: Path) -> None:
        self.path = root / "operator" / "board_sell_configuration.json"
        self._lock = threading.Lock()

    def read(self) -> BoardSellDraft | None:
        try:
            return BoardSellDraft.model_validate_json(self.path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise BoardSellConfigurationError("SELL_CONFIGURATION_STORE_INVALID") from error

    def save(self, settings: BoardSellConfiguration, expected_version: int) -> BoardSellDraft:
        with self._lock:
            previous = self.read()
            if expected_version != (previous.version if previous else 0):
                raise BoardSellConfigurationError("SELL_CONFIGURATION_VERSION_CONFLICT")
            draft = BoardSellDraft(
                version=expected_version + 1, saved_at=datetime.now(UTC), settings=settings
            )
            temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with temporary.open("xb") as output:
                    output.write(canonical_bytes(draft.model_dump(mode="json")))
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            except OSError as error:
                raise BoardSellConfigurationError("SELL_CONFIGURATION_SAVE_FAILED") from error
            finally:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
            return draft
