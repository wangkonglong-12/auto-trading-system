"""Explicit stopped-node recovery for an interrupted supervisor or exited child."""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from auto_trading_system.config.daily_auto import DailyAutoStore
from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.runtime.node_layout import validate_layout
from auto_trading_system.runtime.node_supervisor import supervisor_guard
from auto_trading_system.runtime.supervision_state import SupervisionStore
from auto_trading_system.runtime.tail_feed import read_private
from auto_trading_system.sync.artifact_files import ArtifactFiles


def interrupted_start_available(config: NodeProcessConfig) -> bool:
    if config.broker != "QMT_TRADING":
        return False
    state = SupervisionStore(config).read()
    return (
        state.blocked
        and state.reason in {"SUPERVISOR_INTERRUPTED", "CHILD_EXITED"}
        and state.active_run is None
    )


def recover_interrupted_start(config: NodeProcessConfig) -> None:
    """Call only after a native, explicit user confirmation; never during boot."""
    validate_layout(config)
    # The same guards used by maintenance prevent racing an active supervisor/engine.
    with supervisor_guard(config), supervisor_guard(config, engine=True):
        if not interrupted_start_available(config):
            raise ValueError("DESKTOP_RECOVERY_NOT_INTERRUPTED")
        supervision = SupervisionStore(config)
        artifacts = ArtifactFiles(config.data_root / "artifacts")
        artifacts.put(canonical_bytes(supervision.read().model_dump(mode="json")))
        # Do not generate a replacement installation key during recovery.
        body = read_private(config.data_root / "critical/tail-feed-local-key.bin", 32)
        if body is None or len(body) != 32:
            raise ValueError("DESKTOP_RECOVERY_KEY_UNAVAILABLE")
        key = Ed25519PrivateKey.from_private_bytes(body)
        automatic = DailyAutoStore(config.data_root, key, digest(config.scope()))
        consent = automatic.read()
        if consent is not None:
            previous = read_private(automatic.path, 8192)
            if previous is None:
                raise ValueError("DESKTOP_RECOVERY_CONSENT_CHANGED")
            artifacts.put(previous)
            automatic.write(
                consent.model_copy(
                    update={
                        "enabled": False,
                        "disabled_reason": "OPERATOR_DISABLED",
                        "disabled_detail": None,
                    }
                )
            )
            checked = automatic.read()
            if checked is None or checked.enabled:
                raise ValueError("DESKTOP_RECOVERY_CONSENT_NOT_DISABLED")
        # Unlock last. Any preceding failure preserves the original startup fence.
        supervision.reset()
