"""Framework-only V2 types shared by Windows and the quant module."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

WireId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,96}$")]
Sha256 = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
