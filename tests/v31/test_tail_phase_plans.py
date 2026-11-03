"""Three immutable signed batches share authority and survive a paused restart."""

from datetime import datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from auto_trading_system.execution.plan_control import EnginePlanControl
from auto_trading_system.protocol.v2.api import PlanLookup
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanEnvelope, PlanPayload, sign_plan
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.testing.v31_scenarios import example_plan

NOW = datetime.fromisoformat("2026-09-07T14:51:00+08:00")


def phase_plan(phase: str, key: Ed25519PrivateKey, *, symbol: str = "000001.SZ") -> PlanEnvelope:
    base = example_plan().model_dump(mode="json")
    base["plan_id"] = "phase-" + phase
    source = base["source"]
    assert isinstance(source, dict)
    source.update(
        {
            "decision_phase": phase,
            "selection_rule": "TAIL_EXCELLENT_V1",
            "source_run_id": "run-" + phase,
            "scheduled_at": f"2026-09-07T14:{phase[2:]}:00+08:00",
            "market_data_cutoff": f"2026-09-07T14:{phase[2:]}:00+08:00",
            "completed_at": f"2026-09-07T14:{phase[2:]}:01+08:00",
            "published_at": f"2026-09-07T14:{phase[2:]}:01+08:00",
        }
    )
    allocations = base["allocations"]
    assert isinstance(allocations, list) and isinstance(allocations[0], dict)
    allocations[0]["symbol"] = symbol
    return sign_plan(PlanPayload.model_validate_json(canonical_bytes(base)), "test-key", key)


def control(root: Path, key: Ed25519PrivateKey, *, phased: bool = True) -> EnginePlanControl:
    example = example_plan()
    return EnginePlanControl(
        ArtifactFiles(root / "artifacts"),
        {"test-key": key.public_key()},
        node_id="simulation-node",
        account_id=example.account_id,
        node_generation=1,
        engine_boot_id="test-boot",
        policies=example.policy_bundle,
        rulebook=example.rulebook_version,
        clock=lambda: NOW,
        pause_for_change=lambda: None,
        tail_phases=phased,
    )


def apply(owner: EnginePlanControl, plan: PlanEnvelope, version: int) -> str:
    lookup = PlanLookup(
        account_id=plan.payload.account_id,
        strategy_id=plan.payload.strategy_id,
        trade_date=plan.payload.trade_date,
        plan_id=plan.payload.plan_id,
        state="PUBLISHED",
        state_version=version,
        etag=digest(canonical_bytes({"hash": plan.envelope.payload_hash})),
        plan=plan,
    )
    files = ArtifactFiles(owner.plans._files.root)
    return owner.apply(lookup, files.put(canonical_bytes(plan.model_dump(mode="json")))).status


def test_armed_session_accepts_three_phases_without_replacing_previous_plan(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    owner = control(tmp_path, key)
    first = phase_plan("1440", key)
    assert apply(owner, first, 1) == "LOADED"
    owner.mark_armed()
    second = phase_plan("1445", key, symbol="600000.SH")
    third = phase_plan("1450", key, symbol="000002.SZ")
    assert apply(owner, second, 2) == apply(owner, third, 3) == "LOADED"
    assert owner.plans.accepted() == (first, second, third)
    restored = control(tmp_path, key)
    assert restored.plans.accepted() == (first, second, third)
    assert restored.plans.loaded() == third


def test_same_phase_cannot_change_and_changed_authority_is_rejected(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    owner = control(tmp_path, key)
    first = phase_plan("1440", key)
    assert apply(owner, first, 1) == "LOADED"
    changed = phase_plan("1440", key, symbol="600000.SH")
    assert apply(owner, changed, 2) == "REJECTED"
    second = phase_plan("1445", key)
    payload = second.payload.model_copy(
        update={
            "risk_budget": second.payload.risk_budget.model_copy(update={"max_buy_value": "999999"})
        }
    )
    assert apply(owner, sign_plan(payload, "test-key", key), 3) == "REJECTED"
    assert owner.plans.accepted() == (first,)


def test_legacy_plans_remain_frozen_and_new_mode_rejects_unrated_source(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    legacy = control(tmp_path / "legacy", key, phased=False)
    first = phase_plan("1440", key)
    assert apply(legacy, first, 1) == "LOADED"
    legacy.mark_armed()
    assert apply(legacy, phase_plan("1445", key), 2) == "CHANGE_REQUIRES_PAUSE"
    modern = control(tmp_path / "modern", key)
    assert apply(modern, sign_plan(example_plan(), "test-key", key), 1) == "REJECTED"


def test_tampered_history_and_wrong_account_never_restore(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    owner = control(tmp_path, key)
    assert apply(owner, phase_plan("1440", key), 1) == "LOADED"
    path = tmp_path / "critical/tail-phase-plans.json"
    path.write_bytes(path.read_bytes().replace(b'"simulation-account"', b'"attacker-account"'))
    with pytest.raises(ValueError, match="hash mismatch"):
        control(tmp_path, key)


def test_withdrawal_survives_restart_and_old_phase_cannot_reactivate(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    owner = control(tmp_path, key)
    plan = phase_plan("1440", key)
    assert apply(owner, plan, 1) == "LOADED"
    owner.mark_armed()
    withdrawn = PlanLookup(
        account_id=plan.payload.account_id,
        strategy_id=plan.payload.strategy_id,
        trade_date=plan.payload.trade_date,
        plan_id=plan.payload.plan_id,
        state="WITHDRAWN",
        state_version=2,
        etag=digest(b"withdrawn"),
        plan=None,
    )
    artifact = owner.plans._files.put(canonical_bytes(plan.model_dump(mode="json")))
    assert owner.apply(withdrawn, artifact).status == "WITHDRAWN"
    restored = control(tmp_path, key)
    assert restored.plans.loaded() is None
    assert restored.plans.accepted() == (plan,)
    assert apply(restored, plan, 3) == "REJECTED"
    assert restored.plans.loaded() is None
    assert apply(restored, phase_plan("1445", key), 4) == "LOADED"
