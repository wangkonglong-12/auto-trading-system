from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from auto_trading_system.monitoring import (
    DiagnosticArtifact,
    DiagnosticBundleWriter,
    SensitiveValueRedactor,
    protect_log_handler,
)
from auto_trading_system.security import initialize_secure_storage
from auto_trading_system.security.repository_policy import RepositorySecretBoundary

HMAC_SECRET = "integration-hmac-secret-0123456789abcdef"
BROKER_ACCOUNT = "A1029384756"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows ACL support")
def test_real_acl_logging_and_fault_bundle_keep_runtime_secrets_out_of_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime-security"
    storage = initialize_secure_storage(root)
    assert storage.acl.protected_principal_count == 2

    redactor = SensitiveValueRedactor.from_secret_values(
        SecretStr(HMAC_SECRET),
        SecretStr(BROKER_ACCOUNT),
    )
    log_path = root / "logs" / "runtime.log"
    handler = protect_log_handler(
        logging.FileHandler(log_path, encoding="utf-8"),
        redactor,
        fmt="%(levelname)s %(message)s",
    )
    logger = logging.getLogger("integration.local-security")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        logger.error(
            "broker_account_id=%s Authorization=Bearer transient-token hmac=%s",
            BROKER_ACCOUNT,
            HMAC_SECRET,
        )
    finally:
        handler.close()
        logger.handlers = []

    bundle_path = root / "diagnostics" / "fault-0001.ats-diagnostic.zip"
    DiagnosticBundleWriter(redactor).write(
        bundle_path,
        (
            DiagnosticArtifact("runtime.json", {"broker_account_id": BROKER_ACCOUNT}),
            DiagnosticArtifact("runtime-log.txt", log_path.read_text(encoding="utf-8")),
        ),
        created_at=datetime(2026, 9, 4, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    findings = RepositorySecretBoundary((HMAC_SECRET, BROKER_ACCOUNT)).inspect(
        root.resolve(),
        ("logs/runtime.log", "diagnostics/fault-0001.ats-diagnostic.zip"),
    )
    assert findings == ()
    assert HMAC_SECRET not in log_path.read_text(encoding="utf-8")
    assert BROKER_ACCOUNT not in log_path.read_text(encoding="utf-8")
