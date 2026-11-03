"""Explicit interrupted-start recovery in an isolated node only."""

from contextlib import nullcontext

import pytest
import test_node_processes as helpers

from auto_trading_system.apps.desktop import recovery
from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW
from auto_trading_system.config.daily_auto import DailyAutoConsent, DailyAutoStore
from auto_trading_system.protocol.v2.canonical import digest
from auto_trading_system.runtime.supervision_state import SupervisionStore
from auto_trading_system.runtime.tail_feed import installation_key
from auto_trading_system.sync.artifact_files import ArtifactFiles

node_fixture = helpers.node


@pytest.fixture
def locked(node_fixture):
    config = node_fixture[0].model_copy(update={"broker": "QMT_TRADING"})
    (config.data_root / "node-scope.json").write_bytes(config.scope())
    store = SupervisionStore(config)
    store.write(
        store.read().model_copy(
            update={
                "blocked": True,
                "failures": 3,
                "reason": "SUPERVISOR_INTERRUPTED",
            }
        )
    )
    automatic = DailyAutoStore(
        config.data_root, installation_key(config.data_root), digest(config.scope())
    )
    automatic.write(
        DailyAutoConsent(
            consent_id="auto-test",
            scope_hash=automatic.scope_hash,
            settings_hash=digest(b"settings"),
            enabled=True,
            actor_id="test-operator",
            confirmed_at=DEMO_NOW,
        )
    )
    return config, store, automatic


@pytest.mark.parametrize("reason", ["SUPERVISOR_INTERRUPTED", "CHILD_EXITED"])
def test_recovery_disables_auto_before_unlock_and_archives_original_records(locked, reason):
    config, store, automatic = locked
    store.write(store.read().model_copy(update={"reason": reason}))
    original, consent = store.path.read_bytes(), automatic.path.read_bytes()
    ledger = config.data_root / "critical/journal-v2.db"
    ledger.write_bytes(b"existing-ledger-sentinel")
    assert recovery.interrupted_start_available(config)
    recovery.recover_interrupted_start(config)
    assert not automatic.read().enabled
    assert ledger.read_bytes() == b"existing-ledger-sentinel"
    state = store.read()
    assert not state.blocked and state.failures == 0 and state.reason == "MANUAL_RESET"
    files = ArtifactFiles(config.data_root / "artifacts")
    assert files.read(digest(original)) == original
    assert files.read(digest(consent)) == consent
    assert state.previous_state_hash == digest(original)
    assert not recovery.interrupted_start_available(config)


@pytest.mark.parametrize(
    "reason",
    [
        "CHILD_JOURNAL_UNHEALTHY",
        "CHILD_IDENTITY_MISMATCH",
        "SUPERVISOR_FAILED",
        "DISK_SPACE_LOW",
    ],
)
def test_other_safety_locks_cannot_be_reset_here(locked, reason):
    config, store, automatic = locked
    store.write(store.read().model_copy(update={"reason": reason}))
    before = store.path.read_bytes(), automatic.path.read_bytes()
    assert not recovery.interrupted_start_available(config)
    with pytest.raises(ValueError, match="NOT_INTERRUPTED"):
        recovery.recover_interrupted_start(config)
    assert before == (store.path.read_bytes(), automatic.path.read_bytes())


def test_active_process_guard_rejects_recovery_before_any_write(locked, monkeypatch):
    config, store, automatic = locked
    before = store.path.read_bytes(), automatic.path.read_bytes()

    def guard(*args, engine=False):
        if engine:
            raise RuntimeError("already running")
        return nullcontext()

    monkeypatch.setattr(recovery, "supervisor_guard", guard)
    with pytest.raises(RuntimeError, match="already running"):
        recovery.recover_interrupted_start(config)
    assert before == (store.path.read_bytes(), automatic.path.read_bytes())


def test_consent_write_failure_preserves_startup_lock(locked, monkeypatch):
    config, store, automatic = locked
    before = store.path.read_bytes()

    def failed(*args):
        raise OSError("disk full")

    monkeypatch.setattr(DailyAutoStore, "write", failed)
    with pytest.raises(OSError):
        recovery.recover_interrupted_start(config)
    assert store.path.read_bytes() == before and automatic.read().enabled


def test_missing_key_never_generates_replacement_or_unlocks(locked):
    config, store, automatic = locked
    path = config.data_root / "critical/tail-feed-local-key.bin"
    path.unlink()
    before = store.path.read_bytes(), automatic.path.read_bytes()
    with pytest.raises(ValueError, match="KEY_UNAVAILABLE"):
        recovery.recover_interrupted_start(config)
    assert not path.exists()
    assert before == (store.path.read_bytes(), automatic.path.read_bytes())


def test_unlock_failure_leaves_auto_disabled_and_does_not_start_anything(locked, monkeypatch):
    config, store, automatic = locked

    def failed(*args):
        raise OSError("reset storage failed")

    monkeypatch.setattr(SupervisionStore, "reset", failed)
    with pytest.raises(OSError):
        recovery.recover_interrupted_start(config)
    assert not automatic.read().enabled and store.read().blocked
