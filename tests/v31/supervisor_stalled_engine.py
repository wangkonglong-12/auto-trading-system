"""Test-only fault fixture: block the owner while authenticated pipe stays alive."""

from __future__ import annotations

import sys
from pathlib import Path
from time import sleep

from pydantic import JsonValue

from auto_trading_system.config.node_v2 import load_node_config
from auto_trading_system.runtime.engine_v2 import EngineKernel, EngineSnapshot
from auto_trading_system.runtime.node_engine import run_engine


def stalled(self: EngineKernel) -> EngineSnapshot:
    # No production runtime fault switches are introduced for this test.
    sleep(120)
    raise RuntimeError("test fixture timed out")


def ready(_: dict[str, JsonValue]) -> None:
    EngineKernel.poll = stalled  # type: ignore[method-assign]


if __name__ == "__main__":
    run_engine(load_node_config(Path(sys.argv[1])), ready=ready)
