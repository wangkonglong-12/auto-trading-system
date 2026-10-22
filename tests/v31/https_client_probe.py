"""Subprocess helper for the cross-project TLS test; only ephemeral loopback servers."""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue

from auto_trading_system.execution.plan_control import EnginePlanControl
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.runtime.single_instance import WindowsNamedMutexBackend
from auto_trading_system.sync.agent_jobs import AgentJobs
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.http_v2 import HttpQuantTransport, NodeCredentials, SignedHttpClient
from auto_trading_system.sync.job_store import JobStore
from auto_trading_system.testing.v31_scenarios import example_plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--ca", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    origin = urlsplit(args.origin)
    if origin.scheme != "https" or origin.hostname != "127.0.0.1" or not origin.port:
        raise ValueError("acceptance helper requires an explicit loopback TLS listener")
    args.output.mkdir(exist_ok=False, parents=True)
    # The pytest parent supplies only a disposable fixture secret. This helper
    # never resolves application environment files or production credentials.
    secret = base64.b64decode(os.environ["ATS_ISOLATED_TEST_SECRET"], validate=True)
    credentials = NodeCredentials("test-node", "test-key", secret)
    client = SignedHttpClient(
        args.origin, credentials, tls_context=ssl.create_default_context(cafile=str(args.ca))
    )
    try:
        remote = HttpQuantTransport(client)
        remote.manifest()
        files = ArtifactFiles(args.output / "artifacts")
        plan = example_plan()
        keys = {
            "public-test-key": Ed25519PrivateKey.from_private_bytes(bytes(range(32))).public_key()
        }
        now = datetime.fromisoformat("2026-09-07T14:41:00+08:00")
        engine = EnginePlanControl(
            files,
            keys,
            node_id="test-node",
            account_id=plan.account_id,
            node_generation=1,
            engine_boot_id="isolated-python311",
            policies=plan.policy_bundle,
            rulebook=plan.rulebook_version,
            clock=lambda: now,
            pause_for_change=lambda: None,
        )
        store = JobStore(
            args.output / "jobs",
            node_id="test-node",
            account_id=plan.account_id,
            backend=WindowsNamedMutexBackend(),
        )
        agent = AgentJobs(
            files,
            store,
            remote,
            engine,
            node_id="test-node",
            account_id=plan.account_id,
            generation=1,
            strategy_id=plan.strategy_id,
            trusted_keys=keys,
            clock=lambda: now,
            is_trading_day=lambda day: day == now.date(),
        )
        with store:
            job = agent.pull_plan()
            assert job is not None and job.state == "SYNCED"
            assert engine.plans.loaded() is not None
        result: dict[str, JsonValue] = {
            "python": list(sys.version_info[:3]),
            "plan_loaded": True,
            "receipt_synced": True,
            "real_trading_enabled": False,
            "transport": "LOOPBACK_TLS_VERIFIED",
        }
        (args.output / "result.json").write_bytes(canonical_bytes(result))
        print(json.dumps(result))
    finally:
        client.close()


if __name__ == "__main__":
    main()
