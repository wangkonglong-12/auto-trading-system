from __future__ import annotations

import base64
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from auto_trading_system.journal.apsw_store import APSWCriticalStore, EvidenceTable
from auto_trading_system.journal.checkpoint_blobs import (
    CHUNK_BYTES,
    MAX_CHECKPOINT_BYTES,
    CheckpointChunk,
    CheckpointManifest,
    chunk_key,
    read_checkpoint,
    write_checkpoint,
)
from auto_trading_system.journal.critical_models import CriticalStoreError
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest


def payload(size: int = 200_000) -> bytes:
    return canonical_bytes({"evidence": "中A" * size, "scope": "simulation-account"})


@pytest.mark.parametrize("size", [0, 1, 32760, 32768, 65536, 400_000])
def test_checkpoint_small_compatibility_large_roundtrip_and_idempotence(
    tmp_path: Path, size: int
) -> None:
    body = payload(size)
    store = APSWCriticalStore(tmp_path / "journal.db", "simulation-account")
    store.open()
    try:
        assert read_checkpoint(store, "missing") is None
        assert write_checkpoint(store, "root", body)
        assert not write_checkpoint(store, "root", body)
        assert read_checkpoint(store, "root") == body
        root = store.read_evidence("recovery_checkpoints", "root")
        assert root is not None
        if len(body) <= 64 * 1024:
            assert root == body
        else:
            manifest = CheckpointManifest.model_validate_json(root)
            assert manifest.payload_hash == digest(body)
            assert manifest.byte_count == len(body)
            for content_hash in manifest.chunks:
                raw = store.read_evidence("recovery_checkpoints", chunk_key(content_hash))
                assert raw is not None and len(raw) <= 64 * 1024
        with pytest.raises(CriticalStoreError):
            write_checkpoint(store, "root", canonical_bytes({"changed": True}))
    finally:
        store.close()
    store.open()
    try:
        assert read_checkpoint(store, "root") == body
    finally:
        store.close()


@pytest.mark.parametrize(
    "case", ["reserved", "invalid-json", "too-large", "blank-key", "long-key", "chunk-key"]
)
def test_checkpoint_limits_reject_before_publication(tmp_path: Path, case: str) -> None:
    store = APSWCriticalStore(tmp_path / "limit.db", "simulation-account")
    store.open()
    body, key = b"{}", "root"
    if case == "reserved":
        body = b'{"storage_format":"user-value"}'
    elif case == "invalid-json":
        body = b"invalid"
    elif case == "too-large":
        body = b'"' + b"a" * MAX_CHECKPOINT_BYTES + b'"'
    elif case == "blank-key":
        key = ""
    elif case == "long-key":
        key = "x" * 257
    else:
        key = "checkpoint-chunk:reserved"
    try:
        with pytest.raises(ValueError):
            write_checkpoint(store, key, body)
        assert store.read_evidence("recovery_checkpoints", "root") is None
    finally:
        store.close()


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "base64",
        "chunk-hash",
        "chunk-size",
        "whole-hash",
        "count",
        "format",
        "row-corrupt",
        "not-canonical",
    ],
)
def test_each_fragment_and_manifest_are_verified(tmp_path: Path, case: str) -> None:
    class InjectedRead(APSWCriticalStore):
        inject = False

        def read_evidence(self, table: EvidenceTable, key: str) -> bytes | None:
            raw = super().read_evidence(table, key)
            if not self.inject or raw is None:
                return raw
            if key == "root":
                manifest = CheckpointManifest.model_validate_json(raw)
                if case == "whole-hash":
                    return (
                        manifest.model_copy(update={"payload_hash": "sha256:" + "f" * 64})
                        .model_dump_json()
                        .encode()
                    )
                if case == "count":
                    return (
                        manifest.model_copy(update={"chunks": manifest.chunks[:-1]})
                        .model_dump_json()
                        .encode()
                    )
                if case == "format":
                    return raw.replace(b"critical-blob-v1", b"critical-blob-v9")
            elif key.startswith("checkpoint-chunk:"):
                if case == "missing":
                    return None
                if case == "base64":
                    return b'{"content_base64":"!invalid!"}'
                if case in ("chunk-hash", "chunk-size"):
                    value = b"z" * (CHUNK_BYTES if case == "chunk-hash" else 1)
                    return (
                        CheckpointChunk(content_base64=base64.b64encode(value).decode())
                        .model_dump_json()
                        .encode()
                    )
                if case == "row-corrupt":
                    raise CriticalStoreError("JOURNAL_PAYLOAD_CORRUPT")
            return raw

    store = InjectedRead(tmp_path / "corrupt.db", "simulation-account")
    store.open()
    try:
        if case == "not-canonical":
            body = b'{ "evidence": "' + b"x" * 80000 + b'" }'
            chunks = tuple(body[i : i + CHUNK_BYTES] for i in range(0, len(body), CHUNK_BYTES))
            for part in chunks:
                store.append_evidence(
                    "recovery_checkpoints",
                    chunk_key(digest(part)),
                    CheckpointChunk(content_base64=base64.b64encode(part).decode())
                    .model_dump_json()
                    .encode(),
                )
            root = CheckpointManifest(
                payload_hash=digest(body), byte_count=len(body), chunks=tuple(map(digest, chunks))
            )
            store.append_evidence("recovery_checkpoints", "root", root.model_dump_json().encode())
        else:
            write_checkpoint(store, "root", payload())
        store.inject = True
        with pytest.raises((ValueError, CriticalStoreError)):
            read_checkpoint(store, "root")
    finally:
        store.close()


@pytest.mark.parametrize("stage", ["first-fragment", "middle-fragment", "root"])
def test_failed_writes_leave_no_published_checkpoint_and_can_resume(
    tmp_path: Path, stage: str
) -> None:
    class FailedWrite(APSWCriticalStore):
        calls = 0
        fail = True

        def append_evidence(self, table: EvidenceTable, key: str, body: bytes) -> bool:
            self.calls += 1
            if self.fail and (
                (stage == "first-fragment" and self.calls == 1)
                or (stage == "middle-fragment" and self.calls == 3)
                or (stage == "root" and key == "root")
            ):
                raise OSError("injected checkpoint failure")
            return super().append_evidence(table, key, body)

    store = FailedWrite(tmp_path / "failed.db", "simulation-account")
    store.open()
    try:
        with pytest.raises(OSError):
            write_checkpoint(store, "root", payload())
        assert read_checkpoint(store, "root") is None
        store.fail = False
        assert write_checkpoint(store, "root", payload())
        assert read_checkpoint(store, "root") == payload()
    finally:
        store.close()


@pytest.mark.parametrize("stage", ["chunk", "root"])
def test_actual_child_process_kill_preserves_manifest_publication_boundary(
    tmp_path: Path, stage: str
) -> None:
    # Only this test-owned Python child and its private DB are terminated. The
    # child pauses immediately after a committed fragment or root, allowing a
    # deterministic OS kill without timing assumptions or touching live services.
    program = """
import sys
from pathlib import Path
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.checkpoint_blobs import write_checkpoint
from auto_trading_system.protocol.v2.canonical import canonical_bytes
class KillPoint(APSWCriticalStore):
    def append_evidence(self, table, key, body):
        fresh = super().append_evidence(table, key, body)
        if (sys.argv[2] == "chunk" and key.startswith("checkpoint-chunk:")) or key == "root":
            print("COMMITTED", flush=True)
            sys.stdin.readline()
        return fresh
store = KillPoint(Path(sys.argv[1]), "simulation-account")
store.open()
write_checkpoint(store, "root", canonical_bytes({"evidence": "x" * 100000}))
"""
    path = tmp_path / "killed.db"
    with subprocess.Popen(
        [sys.executable, "-c", program, str(path), stage],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        assert child.stdout is not None
        with ThreadPoolExecutor(max_workers=1) as reader:
            ready = reader.submit(child.stdout.readline)
            try:
                assert ready.result(timeout=10).strip() == "COMMITTED"
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
    store = APSWCriticalStore(path, "simulation-account")
    store.open()
    body = canonical_bytes({"evidence": "x" * 100000})
    try:
        assert read_checkpoint(store, "root") == (body if stage == "root" else None)
        assert write_checkpoint(store, "root", body) == (stage == "chunk")
        assert read_checkpoint(store, "root") == body
    finally:
        store.close()


def test_manifest_fields_and_bounds_are_explicit() -> None:
    for model in (CheckpointManifest, CheckpointChunk):
        assert all(field.description for field in model.model_fields.values())
    with pytest.raises(ValidationError):
        CheckpointManifest(
            payload_hash="sha256:" + "a" * 64,
            byte_count=MAX_CHECKPOINT_BYTES + 1,
            chunks=("sha256:" + "b" * 64,) * 256,
        )
    assert json.loads(CheckpointChunk(content_base64="").model_dump_json()) == {
        "content_base64": ""
    }
