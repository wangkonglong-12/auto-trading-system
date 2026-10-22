"""Small durable supervision state outside SQL, with preserved reset evidence.

An active launch is saved BEFORE process creation. An interrupted supervisor
therefore consumes a failure on the next boot, even if it could not observe its
child's exit. Only an explicit, stopped-node maintenance reset clears the budget.
"""

from __future__ import annotations

import os
from typing import Literal
from uuid import uuid4

from pydantic import Field

from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.sync.artifact_files import ArtifactFiles


class SupervisionState(WireModel):
    schema_major: Literal[1] = Field(default=1, description="监护恢复文件主版本")
    scope_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$", description="节点配置范围摘要")
    failures: int = Field(default=0, ge=0, le=100, description="自上次人工重置后的累计失败数")
    blocked: bool = Field(default=False, description="熔断或安全故障锁定，需停机人工重置")
    active_run: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{32}$", description="已持久化但尚未正常收尾的启动身份"
    )
    reason: str = Field(
        default="INITIAL",
        pattern=r"^[A-Z][A-Z0-9_]{2,63}$",
        description="最近一次状态变化的固定原因码",
    )
    previous_state_hash: str | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$", description="人工重置前状态的归档摘要"
    )


class SupervisionStore:
    def __init__(self, config: NodeProcessConfig) -> None:
        # Recovery state is not placed under logs, which may be rotated independently.
        self.path = config.data_root / "critical" / "supervision-state.json"
        self.scope_hash = digest(config.scope())
        self.artifacts = config.data_root / "artifacts"

    def _validate_path(self) -> None:
        if self.path.is_symlink() or self.path.resolve() != self.path.absolute():
            raise ValueError("supervision state cannot traverse links")

    def read(self) -> SupervisionState:
        self._validate_path()
        if not self.path.exists():
            return SupervisionState(scope_hash=self.scope_hash)
        with self.path.open("rb") as source:
            data = parse_bytes(source.read(8193), max_bytes=8192)
        state = SupervisionState.model_validate_json(canonical_bytes(data))
        if state.scope_hash != self.scope_hash:
            raise ValueError("supervision state scope mismatch")
        return state

    def write(self, state: SupervisionState) -> None:
        self._validate_path()
        if state.scope_hash != self.scope_hash:
            raise ValueError("supervision state scope mismatch")
        data = canonical_bytes(state.model_dump(mode="json"))
        temporary = self.path.with_name(".supervision-" + uuid4().hex)
        # Exclusive temporary creation means cleanup only removes our own file.
        with temporary.open("xb") as target:
            try:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            except BaseException:
                target.close()
                temporary.unlink()
                raise
        try:
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def reset(self) -> None:
        previous = self.read()
        archived = ArtifactFiles(self.artifacts).put(
            canonical_bytes(previous.model_dump(mode="json"))
        )
        self.write(
            SupervisionState(
                scope_hash=self.scope_hash, reason="MANUAL_RESET", previous_state_hash=archived
            )
        )
