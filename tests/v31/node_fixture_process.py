"""Subprocess fixture: real process/pipe components, fixed closing clock and HTTP test peer.

Only tests invoke this helper. Production commands have no clock override and
never import this file or the in-memory receiver. No sockets/real broker calls.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from auto_trading_system.apps.node_process.__main__ import emit
from auto_trading_system.config.node_v2 import load_node_config, resolve_secret
from auto_trading_system.protocol.v2.plans import sign_plan
from auto_trading_system.runtime.node_engine import run_engine
from auto_trading_system.sync.http_v2 import HttpQuantTransport, NodeCredentials, SignedHttpClient
from auto_trading_system.sync.node_agent import run_agent
from auto_trading_system.testing.business_remote import BusinessRemote
from auto_trading_system.testing.v31_scenarios import example_plan


def main() -> None:
    config = load_node_config(Path(sys.argv[1]))
    instant = datetime.fromisoformat("2026-09-07T15:31:00+08:00")
    if sys.argv[2] == "engine":
        run_engine(config, clock=lambda: instant, ready=emit)
    else:
        credentials = NodeCredentials(
            config.node_id, config.quant_key_id, resolve_secret(config.quant_secret)
        )
        plan = sign_plan(
            example_plan().model_copy(update={"account_id": config.account_id}),
            "test-unused",
            Ed25519PrivateKey.generate(),
        )
        remote = BusinessRemote(plan, credentials, lambda: instant.timestamp())
        client = SignedHttpClient(
            config.quant_origin,
            credentials,
            transport=httpx.MockTransport(remote.handle),
            clock=lambda: instant.timestamp(),
        )
        try:
            run_agent(config, clock=lambda: instant, remote=HttpQuantTransport(client), ready=emit)
        finally:
            client.close()


if __name__ == "__main__":
    main()
