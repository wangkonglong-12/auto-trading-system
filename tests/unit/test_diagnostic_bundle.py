from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from auto_trading_system.monitoring import (
    DiagnosticArtifact,
    DiagnosticBundleError,
    DiagnosticBundleWriter,
    SensitiveValueRedactor,
)

SECRET = "diagnostic-secret-value-0123456789"
ACCOUNT = "A99887766"
NOW = datetime(2026, 9, 4, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def writer() -> DiagnosticBundleWriter:
    return DiagnosticBundleWriter(
        SensitiveValueRedactor.from_secret_values(SecretStr(SECRET), SecretStr(ACCOUNT))
    )


def test_bundle_contains_only_redacted_artifacts_and_integrity_manifest(tmp_path: Path) -> None:
    destination = tmp_path / "fault-0001.zip"
    artifacts = (
        DiagnosticArtifact(
            "runtime.json",
            {
                "state": "HALT",
                "broker_account_id": ACCOUNT,
                "error": f"signature={SECRET}",
                "account_id": "acct_alpha",
            },
        ),
        DiagnosticArtifact(
            "recent-logs.txt",
            f"Authorization: Bearer transient-token\nexception contained {SECRET}",
        ),
    )

    result = writer().write(destination, artifacts, created_at=NOW)

    assert result.artifact_count == 2
    assert len(result.bundle_sha256) == 64
    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == {"manifest.json", "runtime.json", "recent-logs.txt"}
        all_content = b"".join(archive.read(name) for name in archive.namelist())
        assert SECRET.encode() not in all_content
        assert ACCOUNT.encode() not in all_content
        assert b"transient-token" not in all_content
        runtime = json.loads(archive.read("runtime.json"))
        assert runtime["account_id"] == "acct_alpha"
        assert runtime["broker_account_id"] == "[REDACTED]"
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["bundle_version"] == "1.0"
        assert manifest["created_at"] == NOW.isoformat()
        assert [item["name"] for item in manifest["artifacts"]] == [
            "runtime.json",
            "recent-logs.txt",
        ]


@pytest.mark.parametrize(
    "name",
    ["../secret.txt", "nested/log.txt", "manifest.json", "bad.exe", " hidden.txt"],
)
def test_bundle_rejects_unsafe_or_reserved_artifact_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(DiagnosticBundleError) as exc_info:
        writer().write(
            tmp_path / "fault.zip",
            (DiagnosticArtifact(name, "content"),),
            created_at=NOW,
        )

    assert exc_info.value.code == "DIAGNOSTIC_ARTIFACT_NAME_INVALID"


def test_bundle_never_overwrites_existing_evidence(tmp_path: Path) -> None:
    destination = tmp_path / "fault.zip"
    destination.write_bytes(b"existing evidence")

    with pytest.raises(DiagnosticBundleError) as exc_info:
        writer().write(
            destination,
            (DiagnosticArtifact("health.json", {"healthy": False}),),
            created_at=NOW,
        )

    assert exc_info.value.code == "DIAGNOSTIC_DESTINATION_EXISTS"
    assert destination.read_bytes() == b"existing evidence"
    assert list(tmp_path.glob(".ats-diagnostic-*.tmp")) == []


def test_bundle_rejects_naive_time_and_oversized_content(tmp_path: Path) -> None:
    with pytest.raises(DiagnosticBundleError, match="DIAGNOSTIC_TIME_INVALID"):
        writer().write(
            tmp_path / "time.zip",
            (DiagnosticArtifact("health.json", {}),),
            created_at=datetime(2026, 9, 4),  # noqa: DTZ001 - deliberately invalid input
        )
    oversized = {f"{number:05d}-{'k' * 120}": number for number in range(9_000)}
    with pytest.raises(DiagnosticBundleError, match="DIAGNOSTIC_ARTIFACT_TOO_LARGE"):
        writer().write(
            tmp_path / "large.zip",
            (DiagnosticArtifact("large.json", oversized),),
            created_at=NOW,
        )
