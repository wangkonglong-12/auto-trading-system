from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.protocol.v2.daily_sync import (
    DATASETS,
    AppliedReceipt,
    DailyManifest,
    DailyRecord,
    Dataset,
    DatasetStatus,
)
from auto_trading_system.protocol.v2.plans import (
    PlanPayload,
    check_plan_binding,
    sign_plan,
    verify_plan,
)
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.daily_files import (
    DailyIdentity,
    FrozenBatch,
    freeze_daily,
    load_daily,
)
from auto_trading_system.sync.daily_upload import RetryableSyncError, upload_once
from auto_trading_system.sync.plan_files import PlanFiles
from auto_trading_system.testing.daily_receiver import InMemoryDailyReceiver
from auto_trading_system.testing.v31_scenarios import example_plan


def sample_batch(
    files: ArtifactFiles,
    *,
    count: int = 201,
    revision: int = 1,
    unavailable: bool = False,
) -> FrozenBatch:
    now = datetime.fromisoformat("2026-09-07T15:30:00+08:00")
    records: dict[Dataset, tuple[DailyRecord, ...]] = dict.fromkeys(DATASETS, ())
    records["broker_trades"] = tuple(
        DailyRecord(
            record_id=f"trade-{i}",
            data={
                "account_id": "simulation-account",
                "trade_date": now.date().isoformat(),
                "observed_at": now.isoformat(),
                "traded_at": now.replace(hour=14, minute=45).isoformat(),
                "broker_trade_id": f"trade-{i}",
                "broker_order_id": f"order-{i}",
                "symbol": "000001.SZ",
                "market": "SZ",
                "side": "BUY",
                "quantity": 100,
                "price": "10.00",
                "gross_amount": "1000.00",
                "origin": "STRATEGY",
            },
        )
        for i in range(count)
    )
    statuses = tuple(
        DatasetStatus(
            dataset=dataset,
            status="UNAVAILABLE" if unavailable and dataset == "positions" else "COMPLETE",
            record_count=len(records[dataset]),
            reason="query timeout" if unavailable and dataset == "positions" else "",
        )
        for dataset in DATASETS
    )
    return freeze_daily(
        DailyIdentity(
            batch_id=f"batch-{revision}",
            node_id="simulation-node",
            node_generation=1,
            account_id="simulation-account",
            trade_date=now.date(),
            revision=revision,
            supersedes_batch_id=f"batch-{revision - 1}" if revision > 1 else None,
            generated_at=now,
            data_as_of=now,
            journal_generation="journal-1",
            last_record_seq=1,
        ),
        records,
        statuses,
        files,
    )


def test_canonical_nfc_and_duplicate_keys() -> None:
    assert canonical_bytes({"label": "e\u0301"}) == canonical_bytes({"label": "é"})
    for data in (b'{"x":1,"x":2}', b'{"price":1.2}', b'{"x":NaN}'):
        with pytest.raises(ValueError):
            parse_bytes(data)
    with pytest.raises(ValueError, match="normalized"):
        canonical_bytes({"e\u0301": 1, "é": 2})
    with pytest.raises(ValueError, match="byte limit"):
        parse_bytes(b"[1]", max_bytes=2)


def test_signed_plan_tampering_identity_expiry_and_frozen_activation(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    payload = example_plan()
    envelope = sign_plan(payload, "test-key", private)
    body = canonical_bytes(envelope.model_dump(mode="json"))
    keys = {"test-key": private.public_key()}
    assert verify_plan(body, keys) == envelope
    with pytest.raises(ValueError, match="trusted"):
        verify_plan(body, {})
    with pytest.raises(InvalidSignature):
        verify_plan(body, {"test-key": Ed25519PrivateKey.generate().public_key()})
    tampered = body.replace(b'"max_qty":100', b'"max_qty":200')
    with pytest.raises(ValueError, match="hash"):
        verify_plan(tampered, keys)
    with pytest.raises(ValueError, match="mismatch"):
        check_plan_binding(
            payload, account_id="wrong", node_generation=1, now=payload.source.completed_at
        )
    with pytest.raises(ValueError, match="remaining"):
        check_plan_binding(
            payload,
            account_id=payload.account_id,
            node_generation=1,
            now=payload.execution_window.stop_new_buy_at,
        )
    files = ArtifactFiles(tmp_path)
    loader = PlanFiles(files, keys)
    content_hash = loader.load(
        body, account_id=payload.account_id, node_generation=1, now=payload.source.completed_at
    )
    assert files.read(content_hash) == body
    loader.mark_armed()
    assert loader.loaded() == envelope
    changed = sign_plan(
        payload.model_copy(update={"plan_id": "simulation-plan-2"}), "test-key", private
    )
    with pytest.raises(ValueError, match="frozen"):
        loader.load(
            canonical_bytes(changed.model_dump(mode="json")),
            account_id=payload.account_id,
            node_generation=1,
            now=payload.source.completed_at,
        )


def test_bad_plan_semantics_are_rejected() -> None:
    payload = example_plan().model_dump(mode="json")
    payload["allocations"][0]["max_qty"] = True
    with pytest.raises(ValidationError):
        PlanPayload.model_validate_json(canonical_bytes(payload))


def test_immutable_artifacts_detect_corruption_and_reject_paths(tmp_path: Path) -> None:
    files = ArtifactFiles(tmp_path)
    content_hash = files.put(b'{"a":1}')
    assert files.put(b'{"a":1}') == content_hash
    with pytest.raises(ValueError, match="hash"):
        files.read("../user-file")
    with pytest.raises(ValueError, match="size"):
        files.put(b"123", maximum_bytes=2)
    (tmp_path / (content_hash.removeprefix("sha256:") + ".json")).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="corrupt"):
        files.read(content_hash)


def test_daily_parts_resume_after_lost_ack_and_backend_restore(tmp_path: Path) -> None:
    files = ArtifactFiles(tmp_path)
    batch = sample_batch(files)
    assert [part.record_count for part in batch.manifest.parts] == [200, 1]
    assert load_daily(files, batch.artifact_hash) == batch

    class LoseFirstPartAck(InMemoryDailyReceiver):
        lost = False

        def put_part(self, batch_id: str, part_id: str, body: bytes) -> None:
            super().put_part(batch_id, part_id, body)
            if not self.lost:
                self.lost = True
                raise RetryableSyncError("simulated lost acknowledgement")

    remote = LoseFirstPartAck()
    assert upload_once(batch, files, remote).status == "PENDING"
    assert len(remote.missing_parts(batch.manifest.batch_id)) == 1
    result = upload_once(batch, files, remote)
    assert result.status == "SYNCED" and result.receipt_artifact_hash is not None
    assert upload_once(batch, files, remote) == result
    restored_remote = InMemoryDailyReceiver()
    assert upload_once(batch, files, restored_remote).status == "SYNCED"
    assert restored_remote.receipt(batch.manifest.batch_id) is not None


def test_incomplete_query_is_not_empty_success_and_correction_is_atomic(tmp_path: Path) -> None:
    files = ArtifactFiles(tmp_path)
    first = sample_batch(files, count=0, unavailable=True)
    fail = False

    def fail_apply() -> None:
        if fail:
            raise RetryableSyncError("simulated projection crash")

    remote = InMemoryDailyReceiver(before_apply=fail_apply)
    assert upload_once(first, files, remote).status == "SYNCED"
    receipt = remote.current(first.manifest.account_id, "2026-09-07")
    assert receipt is not None
    assert receipt.business_status == "INCOMPLETE" and receipt.financial_status == "PROVISIONAL"
    second = sample_batch(files, revision=2, count=1)
    fail = True
    assert upload_once(second, files, remote).status == "PENDING"
    assert remote.current(first.manifest.account_id, "2026-09-07") == receipt
    fail = False
    assert upload_once(second, files, remote).status == "SYNCED"
    current = remote.current(first.manifest.account_id, "2026-09-07")
    assert current is not None and current.revision == 2 and current.business_status == "COMPLETE"


def test_manifest_hash_count_and_receipt_identity_are_verified(tmp_path: Path) -> None:
    files = ArtifactFiles(tmp_path)
    batch = sample_batch(files, count=0)
    raw = batch.manifest.model_dump(mode="json")
    raw["revision"] = 2
    with pytest.raises(ValidationError):
        DailyManifest.model_validate_json(canonical_bytes(raw))
    with pytest.raises(ValidationError, match="reason"):
        DatasetStatus(dataset="positions", status="UNAVAILABLE", record_count=0)
    now = datetime.fromisoformat("2026-09-07T16:00:00+08:00")
    receipt = AppliedReceipt(
        batch_id=batch.manifest.batch_id,
        account_id="wrong-account",
        trade_date=now.date(),
        revision=1,
        manifest_hash=batch.manifest.manifest_hash,
        business_status="COMPLETE",
        financial_status="PROVISIONAL",
        applied_at=now,
    )
    with pytest.raises(ValueError, match="bind"):
        receipt.check_manifest(batch.manifest)
    assert digest(files.read(batch.artifact_hash)) == batch.artifact_hash
