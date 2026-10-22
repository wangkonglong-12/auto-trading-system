from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from auto_trading_system.apps.qmt_order_preflight.__main__ import run
from auto_trading_system.config.models import SecretReference, SecretSource
from auto_trading_system.domain import SHANGHAI_TIMEZONE, ShanghaiTimestamp

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "v1" / "runtime.read-only.example.json"
EXAMPLE_APPROVAL = PROJECT_ROOT / "config" / "v1" / "qmt-order-poc-approval.example.json"
ACCOUNT_ID = "A123456789"
HOST_FINGERPRINT = "0123456789abcdef"


@dataclass
class FakeClock:
    wall_time: datetime = datetime(2026, 9, 4, 14, 50, tzinfo=SHANGHAI_TIMEZONE)

    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp(self.wall_time)


class BrokerAccountProvider:
    source = SecretSource.WINDOWS_CREDENTIAL_MANAGER

    def __init__(self, account_id: str = ACCOUNT_ID) -> None:
        self.account_id = account_id
        self.targets: list[str] = []

    def read(self, reference: SecretReference) -> SecretStr:
        self.targets.append(getattr(reference, "target", ""))
        return SecretStr(self.account_id)


def write_config(tmp_path: Path, *, profile: str = "ASSISTED") -> Path:
    payload = copy.deepcopy(json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8")))
    userdata = tmp_path / "userdata_mini"
    (userdata / "up_queue_xtquant").mkdir(parents=True)
    payload["profile"] = profile
    payload["qmt"]["userdata_mini"] = str(userdata)
    for field, suffix in (
        ("node_hmac_secret", "node-hmac"),
        ("broker_account_id", "broker-account"),
        ("operator_password_verifier", "operator-password"),
    ):
        payload["security"][field] = {
            "source": "WINDOWS_CREDENTIAL_MANAGER",
            "target": f"AutoTrading/node-dev-01/{suffix}",
        }
    path = (tmp_path / f"runtime-{profile.casefold()}.json").resolve()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_approval(tmp_path: Path, *, account_id: str = ACCOUNT_ID) -> tuple[Path, str]:
    payload = json.loads(EXAMPLE_APPROVAL.read_text(encoding="utf-8"))
    payload["target_host_fingerprint"] = HOST_FINGERPRINT
    payload["target_account_fingerprint"] = hashlib.sha256(account_id.encode("ascii")).hexdigest()[
        :16
    ]
    path = (tmp_path / "approval.json").resolve()
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def arguments(config: Path, approval: Path, digest: str, output: Path) -> list[str]:
    return [
        "--config",
        str(config),
        "--approval",
        str(approval),
        "--approval-sha256",
        digest,
        "--host-fingerprint",
        HOST_FINGERPRINT,
        "--output",
        str(output),
    ]


def test_cli_validates_assisted_approval_without_qmt_and_writes_private_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path)
    approval, digest = write_approval(tmp_path)
    output = (tmp_path / "preflight.json").resolve()
    provider = BrokerAccountProvider()

    result = run(
        arguments(config, approval, digest, output),
        providers={SecretSource.WINDOWS_CREDENTIAL_MANAGER: provider},
        clock=FakeClock(),
    )

    assert result == 0
    assert provider.targets == ["AutoTrading/node-dev-01/broker-account"]
    summary = json.loads(capsys.readouterr().out)
    assert summary["outcome"] == "PASS"
    assert summary["exit_code"] == 0
    report_text = output.read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["collection_mode"] == "OFFLINE_APPROVAL_VALIDATION"
    assert report["work_item"] == "QMT-004"
    assert report["live_order_submitted"] is False
    assert ACCOUNT_ID not in report_text
    assert "600000.SH" not in report_text
    assert "0123456789abcdef0123456789abcdef" not in report_text
    assert "Q4_20260904_0001" not in report_text


def test_cli_writes_blocked_binding_result_with_exit_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path)
    approval, digest = write_approval(tmp_path)
    output = (tmp_path / "blocked.json").resolve()
    args = arguments(config, approval, digest, output)
    args[args.index(HOST_FINGERPRINT)] = "f" * 16

    result = run(
        args,
        providers={SecretSource.WINDOWS_CREDENTIAL_MANAGER: BrokerAccountProvider()},
        clock=FakeClock(),
    )

    assert result == 2
    assert json.loads(capsys.readouterr().out)["failure_code"] == "QMT_ORDER_HOST_MISMATCH"
    assert json.loads(output.read_text(encoding="utf-8"))["outcome"] == "BLOCKED"


def test_cli_rejects_non_assisted_profile_before_resolving_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path, profile="READ_ONLY")
    approval, digest = write_approval(tmp_path)
    output = (tmp_path / "wrong-profile.json").resolve()
    provider = BrokerAccountProvider()

    result = run(
        arguments(config, approval, digest, output),
        providers={SecretSource.WINDOWS_CREDENTIAL_MANAGER: provider},
    )

    assert result == 2
    assert capsys.readouterr().err.strip() == "QMT_ORDER_PREFLIGHT_PROFILE_NOT_ASSISTED"
    assert provider.targets == []
    assert not output.exists()


def test_cli_rejects_hash_mismatch_without_leaking_approval(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path)
    approval, _ = write_approval(tmp_path)
    output = (tmp_path / "wrong-hash.json").resolve()

    result = run(
        arguments(config, approval, "0" * 64, output),
        providers={SecretSource.WINDOWS_CREDENTIAL_MANAGER: BrokerAccountProvider()},
    )

    captured = capsys.readouterr()
    assert result == 3
    assert captured.err.strip() == "QMT_ORDER_APPROVAL_HASH_MISMATCH"
    assert ACCOUNT_ID not in captured.out + captured.err
    assert not output.exists()


def test_cli_requires_absolute_inputs_and_a_new_absolute_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path)
    approval, digest = write_approval(tmp_path)

    assert run(arguments(Path("relative.json"), approval, digest, tmp_path / "one.json")) == 2
    assert capsys.readouterr().err.strip() == "QMT_ORDER_PREFLIGHT_PATH_INVALID"

    output = (tmp_path / "existing.json").resolve()
    output.write_text("preserve", encoding="utf-8")
    assert run(arguments(config, approval, digest, output)) == 3
    assert capsys.readouterr().err.strip() == "QMT_PROBE_OUTPUT_EXISTS"
    assert output.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("account_id", ["BAD-ACCOUNT", "账户1234"])
def test_cli_rejects_invalid_account_without_echoing_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    account_id: str,
) -> None:
    config = write_config(tmp_path)
    approval, digest = write_approval(tmp_path)
    output = (tmp_path / "invalid-account.json").resolve()

    result = run(
        arguments(config, approval, digest, output),
        providers={SecretSource.WINDOWS_CREDENTIAL_MANAGER: BrokerAccountProvider(account_id)},
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.err.strip() == "QMT_ORDER_PREFLIGHT_INPUT_INVALID"
    assert account_id not in captured.out + captured.err
    assert not output.exists()
