"""Signed, local operator consent for recurring BUY-only tail execution."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import AwareDatetime, Field

from auto_trading_system.protocol.v2.base import Sha256, WireId, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.sync.artifact_files import ArtifactFiles

DOMAIN = b"ATS_DAILY_AUTO_CONSENT_V1\x00"


class DailyAutoConsent(WireModel):
    consent_id: WireId
    scope_hash: Sha256
    settings_hash: Sha256
    enabled: bool
    actor_id: WireId
    confirmed_at: AwareDatetime
    disabled_reason: WireId | None = None
    disabled_detail: WireId | None = None


class SignedDailyAutoConsent(WireModel):
    payload: DailyAutoConsent
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")


class DailyAutoStore:
    def __init__(self, root: Path, key: Ed25519PrivateKey, scope_hash: str) -> None:
        self.path = root / "critical" / "daily-auto-consent.json"
        self.artifacts = ArtifactFiles(root / "artifacts")
        self.key, self.scope_hash = key, scope_hash

    def _check_path(self) -> None:
        if self.path.is_symlink() or self.path.resolve() != self.path.absolute():
            raise ValueError("daily auto consent must use a direct private path")

    def read(self) -> DailyAutoConsent | None:
        self._check_path()
        try:
            with self.path.open("rb") as stream:
                body = parse_bytes(stream.read(8193), max_bytes=8192)
        except FileNotFoundError:
            return None
        signed = SignedDailyAutoConsent.model_validate_json(canonical_bytes(body))
        if signed.payload.scope_hash != self.scope_hash:
            raise ValueError("daily auto consent belongs to another profile")
        try:
            self.key.public_key().verify(
                bytes.fromhex(signed.signature),
                DOMAIN + canonical_bytes(signed.payload.model_dump(mode="json")),
            )
        except InvalidSignature as error:
            raise ValueError("daily auto consent signature invalid") from error
        return signed.payload

    def _body(self, value: DailyAutoConsent) -> bytes:
        return canonical_bytes(
            SignedDailyAutoConsent(
                payload=value,
                signature=self.key.sign(
                    DOMAIN + canonical_bytes(value.model_dump(mode="json"))
                ).hex(),
            ).model_dump(mode="json")
        )

    def artifact_hash(self, value: DailyAutoConsent) -> str:
        return digest(self._body(value))

    def write(self, value: DailyAutoConsent) -> None:
        self._check_path()
        if value.scope_hash != self.scope_hash:
            raise ValueError("daily auto consent scope differs")
        body = self._body(value)
        self.artifacts.put(body)
        temporary = self.path.with_name(".daily-auto-" + uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
