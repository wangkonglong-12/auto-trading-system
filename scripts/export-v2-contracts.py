"""Export deterministic V2 schema/signature fixtures; --check detects drift.

Only this repository's contracts/v2 is generated. Peer-project synchronization
is an explicit reviewed change, never an automatic action during application startup.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from auto_trading_system.journal.checkpoint_blobs import CheckpointChunk, CheckpointManifest
from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.protocol.v2.authentication import (
    RequestIdentity,
    request_preimage,
    sign_message,
)
from auto_trading_system.protocol.v2.daily_sync import (
    AppliedReceipt,
    DailyManifest,
    LocalControlRequest,
    LocalControlResult,
    SessionGrant,
    StatementImport,
)
from auto_trading_system.protocol.v2.openapi import document
from auto_trading_system.protocol.v2.plans import PlanEnvelope, sign_plan
from auto_trading_system.protocol.v2.records import RECORD_MODELS
from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference, PortfolioCheckpoint
from auto_trading_system.strategies.entry_state import EntryPolicy, EntryTarget
from auto_trading_system.strategies.exit_context import SignedExitContext
from auto_trading_system.strategies.exit_state_store import ExitTransition
from auto_trading_system.testing.v31_scenarios import example_plan

ROOT = Path(__file__).resolve().parents[1] / "contracts" / "v2"


def outputs() -> dict[str, bytes]:
    def encode(value: object) -> bytes:
        return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()

    models = {
        "plan-envelope": PlanEnvelope,
        "daily-manifest": DailyManifest,
        "applied-receipt": AppliedReceipt,
        "local-control": LocalControlRequest,
        "local-control-result": LocalControlResult,
        "session-grant": SessionGrant,
        "statement-import": StatementImport,
        "submission-record": Submission,
        "portfolio-checkpoint": PortfolioCheckpoint,
        "opening-reference": OpeningReference,
        "signed-exit-context": SignedExitContext,
        "exit-transition": ExitTransition,
        "entry-policy": EntryPolicy,
        "entry-target": EntryTarget,
        "checkpoint-manifest": CheckpointManifest,
        "checkpoint-chunk": CheckpointChunk,
    }
    models.update({"daily-" + name: model for name, model in RECORD_MODELS.items()})
    files = {
        f"schemas/{name}.schema.json": encode(model.model_json_schema())
        for name, model in models.items()
    }
    files["openapi.json"] = encode(document())
    # This intentionally public test seed is never used by an application key provider.
    seed = bytes(range(32))
    key = Ed25519PrivateKey.from_private_bytes(seed)
    plan = sign_plan(example_plan(), "public-test-key", key)
    files["examples/signed-plan.json"] = encode(plan.model_dump(mode="json"))
    identity = RequestIdentity("test-node", "public-test-key", 1000, "test-nonce", "test-request")
    target = "/api/v2/trading/plans/current?strategy_id=tail&account_id=test-account"
    preimage = request_preimage("GET", target, identity, b"")
    files["examples/signature-vector.json"] = encode(
        {
            "notice": "Public deterministic TEST key only. Never deploy this key.",
            "test_key_hex": seed.hex(),
            "ed25519_public_key_base64": base64.b64encode(
                key.public_key().public_bytes_raw()
            ).decode(),
            "request": {
                "method": "GET",
                "target": target,
                "node_id": identity.node_id,
                "key_id": identity.key_id,
                "timestamp": identity.timestamp,
                "nonce": identity.nonce,
                "request_id": identity.request_id,
                "body": "",
            },
            "request_preimage_utf8": preimage.decode(),
            "hmac_sha256": sign_message(seed, preimage),
            "plan_payload_hash": plan.envelope.payload_hash,
            "plan_signature": plan.envelope.signature,
        }
    )
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, body in outputs().items():
        path = ROOT / name
        if args.check:
            if not path.exists() or path.read_bytes() != body:
                raise SystemExit(f"V2 contract differs: {name}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
    print(
        "V2 contract schemas and public signature vector verified"
        if args.check
        else "V2 contract artifacts exported"
    )


if __name__ == "__main__":
    main()
