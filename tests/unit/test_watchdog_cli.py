from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_trading_system.apps.watchdog.__main__ import build_parser, main
from auto_trading_system.config import RuntimeConfig
from auto_trading_system.version import __version__


def test_watchdog_cli_reports_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"auto-trading-watchdog {__version__}"


def test_watchdog_cli_requires_runtime_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(tmp_path / "missing.json")]) == 2
    assert "runtime command is required" in capsys.readouterr().err


def test_watchdog_cli_rejects_missing_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "--config",
                str(tmp_path / "missing.json"),
                "--",
                str(Path(__file__).resolve()),
            ]
        )
        == 2
    )
    assert "CONFIG_READ_FAILED" in capsys.readouterr().err


def test_watchdog_cli_composes_without_resolving_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    import json

    payload = json.loads(
        (project_root / "config" / "v1" / "runtime.read-only.example.json").read_text(
            encoding="utf-8"
        )
    )
    payload["storage"]["root"] = str(tmp_path)
    settings = RuntimeConfig.model_validate(payload)
    observed: dict[str, object] = {}

    class FakeOwnership:
        def __enter__(self) -> FakeOwnership:
            observed["owned"] = True
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeSupervisor:
        def __init__(self, **kwargs: object) -> None:
            observed["supervisor"] = kwargs

        def run(self, stop_requested: object) -> None:
            observed["ran"] = callable(stop_requested)

    monkeypatch.setattr(
        "auto_trading_system.apps.watchdog.__main__.load_runtime_settings",
        lambda _path: settings,
    )
    monkeypatch.setattr(
        "auto_trading_system.apps.watchdog.__main__.SubprocessRuntimeLauncher",
        lambda command: SimpleNamespace(command=command),
    )
    monkeypatch.setattr(
        "auto_trading_system.apps.watchdog.__main__.WatchdogSupervisor", FakeSupervisor
    )
    monkeypatch.setattr(
        "auto_trading_system.apps.watchdog.__main__.create_account_instance_guard",
        lambda _key: FakeOwnership(),
    )
    monkeypatch.setattr("auto_trading_system.apps.watchdog.__main__.signal.signal", lambda *_: None)

    assert (
        main(
            [
                "--config",
                str(tmp_path / "runtime.json"),
                "--",
                str(project_root / "pyproject.toml"),
            ]
        )
        == 0
    )
    assert observed["owned"] is True
    assert observed["ran"] is True
