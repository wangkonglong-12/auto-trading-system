"""Cross-interpreter probe using installed app processes, never production endpoints.

The parent test supplies loopback TLS and an isolated MySQL schema. This probe
publishes only public fixture configuration/plan on stdout, then waits for the
parent to register that account before any client jobs start.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from test_node_authorization import access as access_fixture
from test_node_processes import eventually, node, recovered, rpc
from test_paper_process import exercise_normal_process, normal_process

from auto_trading_system.config.node_v2 import NodeProcessConfig, load_node_config
from auto_trading_system.control.ipc_protocol import RPCClient
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.plans import PlanEnvelope


def run() -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", required=True)
    parser.add_argument("--ca", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if urlsplit(args.origin).hostname != "127.0.0.1" or not args.origin.startswith("https://"):
        raise ValueError("acceptance peer must be loopback HTTPS")
    args.output.mkdir(parents=True, exist_ok=False)
    with pytest.MonkeyPatch.context() as patch:
        fixture = node.__wrapped__(args.output, patch)  # type: ignore[attr-defined]
        access = access_fixture.__wrapped__(fixture, patch)  # type: ignore[attr-defined]
        # Distinct public test-only HTTP value, not the IPC or operator credential.
        patch.setenv("ATS_SECRET_NODE_HTTP", "public-offline-hmac-test-only-0001")
        config_path = access[1]

        def prepare(config: NodeProcessConfig, plan: PlanEnvelope) -> NodeProcessConfig:
            raw = config.model_dump(mode="json")
            raw.update(quant_origin=args.origin, quant_ca_file=str(args.ca.resolve()))
            config = NodeProcessConfig.model_validate_json(canonical_bytes(raw))
            print(
                json.dumps(
                    {
                        "ready": True,
                        "node_id": config.node_id,
                        "account_id": config.account_id,
                        "key_id": config.quant_key_id,
                        "plan": plan.model_dump(mode="json"),
                    }
                ),
                flush=True,
            )
            if sys.stdin.readline().strip() != "REGISTERED":
                raise ValueError("isolated parent did not confirm registration")
            return config

        def job(path: Path, kind: str, *, expect: int = 0) -> dict[str, Any]:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "auto_trading_system.apps.node_process",
                    "--config",
                    str(path),
                    "job",
                    kind,
                ],
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            assert result.returncode == expect, (result.stdout, result.stderr)
            return json.loads(result.stdout)["last_result"]  # type: ignore[no-any-return]

        def idle(config: NodeProcessConfig) -> None:
            client = rpc(config, "jobrunner", "agent")
            eventually(lambda: client.invoke("STATUS")["status"] == "IDLE", seconds=40)

        def load(config: NodeProcessConfig, _: PlanEnvelope, engine: RPCClient) -> None:
            with normal_process(config_path, "agent"):
                idle(config)
                assert job(config_path, "pull-plan")["status"] == "SYNCED"
                rpc(config, "operator", "agent").invoke("STOP")
            requested = rpc(config, "operator", "control").invoke("REQUEST_RECOVERY")
            assert type(requested["state_version"]) is int
            recovered(engine, minimum_version=requested["state_version"])

        invocations: list[dict[str, Any]] = []

        def upload(path: Path) -> None:
            config = load_node_config(path)
            if not invocations:
                # Reserve a non-listening loopback socket: guaranteed unreachable,
                # without changing any real service or machine network setting.
                with socket.socket() as unavailable:
                    unavailable.bind(("127.0.0.1", 0))
                    raw = config.model_dump(mode="json")
                    raw["quant_origin"] = f"https://127.0.0.1:{unavailable.getsockname()[1]}"
                    path.write_bytes(canonical_bytes(raw))
                    try:
                        with normal_process(path, "agent"):
                            idle(config)
                            failed = job(path, "daily-export", expect=1)
                            assert failed["status"] == "PENDING"
                            rpc(config, "operator", "agent").invoke("STOP")
                    finally:
                        path.write_bytes(canonical_bytes(config.model_dump(mode="json")))
            with normal_process(path, "agent"):
                # Startup discovers and retries retained batches from the outage.
                idle(config)
                result = job(path, "daily-export")
                assert result["status"] == "SYNCED"
                assert job(path, "retry")["status"] == "SYNCED"
                invocations.append(result)
                rpc(config, "operator", "agent").invoke("STOP")

        result = exercise_normal_process(
            access, prepare=prepare, plan_loader=load, upload_daily=upload
        )
        result.update(
            https=True,
            outage_retry=True,
            daily_invocations=len(invocations),
            interpreter=sys.version.split()[0],
        )
        (args.output / "result.json").write_bytes(canonical_bytes(result))
        return result


if __name__ == "__main__":
    # Parent never receives the randomly generated local IPC/password secrets.
    os.environ.pop("PYTHONINSPECT", None)
    print(json.dumps(run()), flush=True)
