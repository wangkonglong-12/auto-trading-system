from pathlib import Path

import test_node_processes as node_helpers
from test_node_console import observation

from auto_trading_system.apps.local_console.node_status import project_status
from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.protocol.v2.plans import PlanEnvelope

node_fixture = node_helpers.node


def test_safe_recovery_code_survives_status_projection(
    node_fixture: tuple[NodeProcessConfig, Path, PlanEnvelope],
) -> None:
    config = node_fixture[0]
    source = observation(config)
    source["recovery_error"] = "QMT_TIMESTAMP_OUTSIDE_TRADE_DATE"
    projected = project_status("engine", source, config)
    assert projected["recovery_error"] == "QMT_TIMESTAMP_OUTSIDE_TRADE_DATE"
    assert "must-not-leak" not in str(projected)
