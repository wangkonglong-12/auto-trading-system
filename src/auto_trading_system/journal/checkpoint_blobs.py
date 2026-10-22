"""Immutable, bounded checkpoint fragments with a manifest published last.

All calls run on the existing Journal owner. A partially written set is not a
checkpoint: readers require the root manifest and verify every fragment, size
and the whole logical payload hash. Small legacy JSON payloads remain byte-for-
byte compatible. No historical row is deleted or overwritten by this module.
"""

from __future__ import annotations

import base64
from typing import Literal

from pydantic import Field, model_validator

from auto_trading_system.journal.critical_writer import CriticalStorePort
from auto_trading_system.protocol.v2.base import Sha256, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes

ROW_BYTES = 64 * 1024
CHUNK_BYTES = 32 * 1024
MAX_CHUNKS = 256
MAX_CHECKPOINT_BYTES = CHUNK_BYTES * MAX_CHUNKS


class CheckpointManifest(WireModel):
    storage_format: Literal["critical-blob-v1"] = Field(
        default="critical-blob-v1", description="本地分片检查点格式标记，保留字段"
    )
    payload_hash: Sha256 = Field(description="完整规范JSON正文的SHA256")
    byte_count: int = Field(
        gt=ROW_BYTES, le=MAX_CHECKPOINT_BYTES, description="完整正文UTF-8字节数"
    )
    chunk_bytes: Literal[32768] = Field(default=32768, description="除末片外的原始分片字节数")
    chunks: tuple[Sha256, ...] = Field(
        min_length=3, max_length=MAX_CHUNKS, description="按正文顺序排列的原始分片摘要"
    )

    @model_validator(mode="after")
    def complete(self) -> CheckpointManifest:
        if len(self.chunks) != (self.byte_count + CHUNK_BYTES - 1) // CHUNK_BYTES:
            raise ValueError("checkpoint fragment count does not match size")
        return self


class CheckpointChunk(WireModel):
    content_base64: str = Field(max_length=43692, description="原始正文分片Base64，最大32768字节")


def chunk_key(content_hash: str) -> str:
    return "checkpoint-chunk:" + content_hash


def write_checkpoint(store: CriticalStorePort, key: str, body: bytes) -> bool:
    """Publish only after all chunks commit; caller still fences late completion."""
    if not key or len(key) > 256 or key.startswith("checkpoint-chunk:"):
        raise ValueError("invalid or reserved checkpoint root key")
    value = parse_bytes(body, max_bytes=MAX_CHECKPOINT_BYTES)
    if isinstance(value, dict) and "storage_format" in value:
        raise ValueError("storage_format is reserved for checkpoint manifests")
    body = canonical_bytes(value)
    if len(body) > MAX_CHECKPOINT_BYTES:
        raise ValueError("canonical checkpoint exceeds byte capacity")
    if len(body) <= ROW_BYTES:
        return store.append_evidence("recovery_checkpoints", key, body)
    hashes: list[str] = []
    for offset in range(0, len(body), CHUNK_BYTES):
        chunk = body[offset : offset + CHUNK_BYTES]
        content_hash = digest(chunk)
        encoded = CheckpointChunk(content_base64=base64.b64encode(chunk).decode("ascii"))
        store.append_evidence(
            "recovery_checkpoints",
            chunk_key(content_hash),
            canonical_bytes(encoded.model_dump(mode="json")),
        )
        hashes.append(content_hash)
    manifest = CheckpointManifest(
        payload_hash=digest(body), byte_count=len(body), chunks=tuple(hashes)
    )
    return store.append_evidence(
        "recovery_checkpoints", key, canonical_bytes(manifest.model_dump(mode="json"))
    )


def read_checkpoint(store: CriticalStorePort, key: str) -> bytes | None:
    """Verify a complete logical payload; root absence never means an empty payload."""
    root = store.read_evidence("recovery_checkpoints", key)
    if root is None:
        return None
    value = parse_bytes(root, max_bytes=ROW_BYTES)
    if not isinstance(value, dict) or "storage_format" not in value:
        return root
    manifest = CheckpointManifest.model_validate_json(root)
    body = bytearray()
    for index, content_hash in enumerate(manifest.chunks):
        raw = store.read_evidence("recovery_checkpoints", chunk_key(content_hash))
        if raw is None:
            raise ValueError("checkpoint fragment is missing")
        chunk = CheckpointChunk.model_validate_json(raw)
        decoded = base64.b64decode(chunk.content_base64, validate=True)
        expected = min(CHUNK_BYTES, manifest.byte_count - index * CHUNK_BYTES)
        if len(decoded) != expected or digest(decoded) != content_hash:
            raise ValueError("checkpoint fragment size or digest mismatch")
        body.extend(decoded)
    result = bytes(body)
    if len(result) != manifest.byte_count or digest(result) != manifest.payload_hash:
        raise ValueError("checkpoint payload size or digest mismatch")
    if canonical_bytes(parse_bytes(result, max_bytes=MAX_CHECKPOINT_BYTES)) != result:
        raise ValueError("checkpoint payload is not canonical JSON")
    return result
