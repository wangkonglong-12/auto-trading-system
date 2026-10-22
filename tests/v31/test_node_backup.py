"""Real Windows locks, SQLite WAL snapshots and non-destructive restore fences."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import apsw
import pytest
from test_node_processes import fixture_child
from test_node_processes import node as node_fixture

from auto_trading_system.apps.node_process.__main__ import main
from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.journal import apsw_backup
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanEnvelope
from auto_trading_system.runtime import node_backup
from auto_trading_system.runtime.node_backup import (
    BackupFile,
    create_backup,
    restore_candidate,
    verify_backup,
)
from auto_trading_system.runtime.node_layout import initialize_layout, validate_layout
from auto_trading_system.runtime.node_supervisor import supervisor_guard
from auto_trading_system.runtime.single_instance import WindowsNamedMutexBackend
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.job_store import JobStore

Node = tuple[NodeProcessConfig, Path, PlanEnvelope]

# Re-export the shared fixture without shadowing an imported name in test args.
node = node_fixture


def populate(config: NodeProcessConfig, submission: Submission) -> Path:
    path = config.data_root / "critical/journal-v2.db"
    store = APSWCriticalStore(path, config.account_id)
    store.open()
    try:
        store.commit_submission(submission.model_copy(update={"account_id": config.account_id}))
        store.append_evidence("broker_facts", "sample", b'{"sample":true}')
    finally:
        store.close()
    ArtifactFiles(config.data_root / "artifacts").put(b'{"source":"mock"}')
    return path


def test_backup_restore_roundtrip_preserves_evidence_and_fences_startup(
    node: Node,
    submission: Submission,
    tmp_path: Path,
) -> None:
    config, _, _ = node
    original = populate(config, submission)
    before = original.read_bytes()
    bundle, restored = tmp_path / "backup", tmp_path / "restored"
    pin = create_backup(config, bundle)
    manifest = verify_backup(config, bundle, pin)
    assert manifest.journal.submission_count == manifest.journal.last_record_seq == 1
    assert manifest.journal.evidence_count == 1
    assert original.read_bytes() == before
    bundle_before = {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    assert restore_candidate(config, bundle, pin, restored) == manifest
    assert (restored / "restore-verified.json").exists()
    candidate = config.model_copy(update={"data_root": restored})
    with pytest.raises(ValueError, match="RESTORE_REVIEW_REQUIRED"):
        validate_layout(candidate)
    with pytest.raises(FileExistsError):
        initialize_layout(candidate)
    assert original.read_bytes() == before
    assert all(p.read_bytes() == body for p, body in bundle_before.items())
    assert not (bundle / "critical/journal-v2.db-wal").exists()


def test_backup_api_includes_committed_wal(
    node: Node, submission: Submission, tmp_path: Path
) -> None:
    config, _, _ = node
    path = config.data_root / "critical/journal-v2.db"
    store = APSWCriticalStore(path, config.account_id)
    store.open()
    try:
        store.commit_submission(submission.model_copy(update={"account_id": config.account_id}))
        assert Path(str(path) + "-wal").stat().st_size > 0
        pin = create_backup(config, tmp_path / "backup")
        assert verify_backup(config, tmp_path / "backup", pin).journal.last_record_seq == 1
    finally:
        store.close()


@pytest.mark.parametrize("engine", [True, False])
def test_running_engine_or_supervisor_blocks_before_output_creation(
    node: Node,
    submission: Submission,
    tmp_path: Path,
    engine: bool,
) -> None:
    config, _, _ = node
    populate(config, submission)
    with supervisor_guard(config, engine=engine), pytest.raises(RuntimeError):
        create_backup(config, tmp_path / "blocked")
    assert not (tmp_path / "blocked").exists()
    assert create_backup(config, tmp_path / "after-stop").startswith("sha256:")


def test_running_agent_job_blocks_backup(
    node: Node, submission: Submission, tmp_path: Path
) -> None:
    config, _, _ = node
    populate(config, submission)
    with (
        JobStore(
            config.data_root / "jobs",
            node_id=config.node_id,
            account_id=config.account_id,
            backend=WindowsNamedMutexBackend(),
        ),
        pytest.raises(RuntimeError),
    ):
        create_backup(config, tmp_path / "blocked")
    assert not (tmp_path / "blocked").exists()


@pytest.mark.parametrize("kind", ["existing", "nested", "ancestor", "relative"])
def test_backup_rejects_unsafe_targets(
    node: Node,
    submission: Submission,
    tmp_path: Path,
    kind: str,
) -> None:
    config, _, _ = node
    path = populate(config, submission)
    before = path.read_bytes()
    targets = {
        "existing": config.data_root,
        "nested": config.data_root / "new-backup",
        "ancestor": tmp_path,
        "relative": Path("relative-backup"),
    }
    with pytest.raises(ValueError):
        create_backup(config, targets[kind])
    assert path.read_bytes() == before


@pytest.mark.parametrize("tamper", ["file", "manifest", "extra", "missing"])
def test_corrupt_bundle_rejected_before_restore_directory_exists(
    node: Node,
    submission: Submission,
    tmp_path: Path,
    tamper: str,
) -> None:
    config, _, _ = node
    populate(config, submission)
    bundle = tmp_path / "bundle"
    pin = create_backup(config, bundle)
    if tamper == "manifest":
        (bundle / "manifest.json").write_bytes(b"{}")
    elif tamper == "extra":
        (bundle / "unexpected.json").write_bytes(b"{}")
    elif tamper == "missing":
        (bundle / "critical/journal-v2.db").rename(bundle / "critical/misplaced.db")
    else:
        next((bundle / "artifacts").iterdir()).write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        restore_candidate(config, bundle, pin, tmp_path / "candidate")
    assert not (tmp_path / "candidate").exists()


@pytest.mark.parametrize("field", ["account_id", "node_generation", "journal_generation"])
def test_scope_mismatch_does_not_restore(
    node: Node,
    submission: Submission,
    tmp_path: Path,
    field: str,
) -> None:
    config, _, _ = node
    populate(config, submission)
    pin = create_backup(config, tmp_path / "bundle")
    wrong = config.model_copy(update={field: 2 if field == "node_generation" else "wrong-scope"})
    with pytest.raises(ValueError, match="scope"):
        verify_backup(wrong, tmp_path / "bundle", pin)


@pytest.mark.parametrize("corruption", ["schema", "payload", "account", "target"])
def test_journal_semantic_corruption_has_no_completion_marker(
    node: Node,
    submission: Submission,
    tmp_path: Path,
    corruption: str,
) -> None:
    config, _, _ = node
    path = populate(config, submission)
    db = apsw.Connection(str(path))
    try:
        if corruption == "schema":
            db.execute("CREATE TABLE foreign_data(id INTEGER)")
        elif corruption == "payload":
            db.execute("UPDATE broker_facts SET payload_hash=?", ("sha256:" + "0" * 64,))
        elif corruption == "target":
            db.execute("UPDATE action_targets SET target_remaining=target_remaining+1")
        else:
            db.execute("UPDATE store_meta SET account_id='wrong'")
    finally:
        db.close()
    with pytest.raises(ValueError):
        create_backup(config, tmp_path / "bad-backup")
    assert not (tmp_path / "bad-backup/manifest.json").exists()


def test_interrupted_backup_and_restore_preserve_partial_evidence_and_fence(
    node: Node,
    submission: Submission,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _, _ = node
    source = populate(config, submission)
    before = source.read_bytes()
    bundle = tmp_path / "good"
    pin = create_backup(config, bundle)

    def fail(*args: Any) -> None:
        raise OSError("injected disk failure")

    monkeypatch.setattr(node_backup, "_copy", fail)
    with pytest.raises(OSError):
        create_backup(config, tmp_path / "partial")
    assert (tmp_path / "partial").exists()
    assert not (tmp_path / "partial/manifest.json").exists()
    candidate = tmp_path / "candidate"
    with pytest.raises(OSError):
        restore_candidate(config, bundle, pin, candidate)
    assert (candidate / "restore-pending.json").exists()
    assert not (candidate / "restore-verified.json").exists()
    with pytest.raises(ValueError, match="RESTORE_REVIEW_REQUIRED"):
        validate_layout(config.model_copy(update={"data_root": candidate}))
    assert source.read_bytes() == before


def test_hardlink_and_unexpected_source_file_rejected(
    node: Node,
    submission: Submission,
    tmp_path: Path,
) -> None:
    config, _, _ = node
    populate(config, submission)
    artifact = next((config.data_root / "artifacts").iterdir())
    os.link(artifact, tmp_path / "linked.json")
    with pytest.raises(ValueError, match="links"):
        create_backup(config, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("path", ["../quant.db", "critical/../../a", "C:/a", "jobs/a.json"])
def test_manifest_paths_are_fixed_allowlist(path: str) -> None:
    with pytest.raises(ValueError):
        BackupFile(path=path, byte_length=1, sha256=digest(b"x"))


def test_cli_backup_verify_and_interactive_restore(
    node: Node,
    submission: Submission,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, config_path, _ = node
    populate(config, submission)
    common = ["--config", str(config_path)]
    bundle = tmp_path / "bundle"
    assert main([*common, "backup", "--destination", str(bundle)]) == 0
    pin = digest((bundle / "manifest.json").read_bytes())
    args = ["--bundle", str(bundle), "--manifest-hash", pin]
    assert main([*common, "verify-backup", *args]) == 0
    candidate = tmp_path / "candidate"
    restore = [*common, "restore-backup", *args, "--destination", str(candidate)]
    assert main(restore) == 2
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "wrong")
    assert main(restore) == 2 and not candidate.exists()
    monkeypatch.setattr("builtins.input", lambda _: "RESTORE CANDIDATE " + config.account_id)
    assert main(restore) == 0
    restored_config = tmp_path / "restored-config.json"
    restored_config.write_bytes(
        canonical_bytes(config.model_copy(update={"data_root": candidate}).model_dump(mode="json"))
    )
    assert main(["--config", str(restored_config), "engine"]) == 2
    assert '"startup_blocked": true' in capsys.readouterr().out


def test_real_child_engine_excludes_backup_until_process_exit(node: Node, tmp_path: Path) -> None:
    config, path, _ = node
    with fixture_child(path, "engine"), pytest.raises(RuntimeError):
        create_backup(config, tmp_path / "while-running")
    assert not (tmp_path / "while-running").exists()
    pin = create_backup(config, tmp_path / "stopped")
    assert verify_backup(config, tmp_path / "stopped", pin).journal.last_record_seq == 0


def test_restore_when_original_volume_is_unavailable(
    node: Node,
    submission: Submission,
    tmp_path: Path,
) -> None:
    config, _, _ = node
    populate(config, submission)
    bundle = tmp_path / "bundle"
    pin = create_backup(config, bundle)
    # Only this test's isolated fixture is moved; keep the original bytes intact.
    config.data_root.rename(tmp_path / "original-preserved")
    proof = restore_candidate(config, bundle, pin, tmp_path / "candidate")
    assert proof.journal.last_record_seq == 1
    assert (tmp_path / "original-preserved/critical/journal-v2.db").exists()


@pytest.mark.parametrize("kind", ["total", "timeout"])
def test_backup_resource_budget_failures_never_publish_completion(
    node: Node,
    submission: Submission,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    config, _, _ = node
    populate(config, submission)
    if kind == "total":
        monkeypatch.setattr(node_backup, "MAX_TOTAL_BYTES", 1)
    else:
        times = iter((0.0, 61.0))
        monkeypatch.setattr(apsw_backup, "monotonic", lambda: next(times))
    with pytest.raises(ValueError, match="budget"):
        create_backup(config, tmp_path / "bounded")
    assert not (tmp_path / "bounded/manifest.json").exists()


def test_successful_restore_cannot_overwrite_existing_directory(
    node: Node,
    submission: Submission,
    tmp_path: Path,
) -> None:
    config, _, _ = node
    populate(config, submission)
    bundle = tmp_path / "bundle"
    pin = create_backup(config, bundle)
    existing = tmp_path / "user-files"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_bytes(b"user content")
    with pytest.raises(ValueError):
        restore_candidate(config, bundle, pin, existing)
    assert marker.read_bytes() == b"user content"


def test_missing_journal_and_unknown_source_file_are_visible(
    node: Node,
    submission: Submission,
    tmp_path: Path,
) -> None:
    config, _, _ = node
    with pytest.raises(ValueError, match="Journal is required"):
        create_backup(config, tmp_path / "empty")
    populate(config, submission)
    (config.data_root / "critical/unrecognized.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="unexpected file"):
        create_backup(config, tmp_path / "unknown")
    assert not (tmp_path / "unknown").exists()


@pytest.mark.parametrize("operation", ["copy", "inspect"])
def test_storage_driver_errors_are_translated_at_adapter_boundary(
    tmp_path: Path, operation: str
) -> None:
    invalid = tmp_path / "not-a-database.db"
    invalid.write_bytes(b"invalid database")
    with pytest.raises(ValueError, match="storage"):
        if operation == "copy":
            apsw_backup.copy_journal(invalid, tmp_path / "copy.db", max_bytes=1024)
        else:
            apsw_backup.inspect_journal(invalid, "mock-account")


def test_storage_copy_never_overwrites_existing_file(tmp_path: Path) -> None:
    existing = tmp_path / "keep.db"
    existing.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        apsw_backup.copy_journal(tmp_path / "source.db", existing, max_bytes=1024)
    assert existing.read_bytes() == b"original"
