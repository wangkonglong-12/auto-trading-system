"""Durable reference to a locally accepted plan, for paused boot reconstruction."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from auto_trading_system.execution.plan_control import EnginePlanControl
from auto_trading_system.protocol.v2.api import PlanLookup
from auto_trading_system.protocol.v2.base import Sha256, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.sync.artifact_files import ArtifactFiles


class PlanRecoveryReference(WireModel):
    lookup_hash: Sha256 = Field(description="已接收计划查询响应正文摘要")
    artifact_hash: Sha256 | None = Field(description="已接收计划签名正文摘要；撤回为空")
    trade_date: date = Field(description="该本地引用所属交易日")


def save_reference(path: Path, reference: PlanRecoveryReference) -> None:
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("plan recovery reference must be direct")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("plan recovery reference must be regular")
    partial = path.with_name(".pending-plan-" + uuid4().hex)
    with partial.open("xb") as output:
        output.write(canonical_bytes(reference.model_dump(mode="json")))
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)


def restore_reference(path: Path, plans: EnginePlanControl, today: date) -> None:
    if not path.exists():
        return
    if path.is_symlink() or path.resolve() != path.absolute() or path.stat().st_nlink != 1:
        raise ValueError("invalid plan recovery reference path")
    with path.open("rb") as source:
        body = source.read(4097)
    if len(body) > 4096:
        raise ValueError("plan recovery reference exceeds capacity")
    reference = PlanRecoveryReference.model_validate_json(body)
    if reference.trade_date > today:
        raise ValueError("future plan recovery reference")
    if reference.trade_date < today or reference.artifact_hash is None:
        return
    files = ArtifactFiles(path.parent.parent / "artifacts")
    lookup = PlanLookup.model_validate_json(files.read(reference.lookup_hash))
    if lookup.trade_date != today:
        raise ValueError("plan recovery reference date differs")
    plans.restore_for_recovery(lookup, reference.artifact_hash)
