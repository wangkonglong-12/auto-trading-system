from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import cast

import httpx
import pytest

from auto_trading_system.apps.desktop.session import (
    DESKTOP_OPERATOR_PASSWORD,
    ConfiguredSession,
    preview_config,
)
from auto_trading_system.apps.desktop.setup import (
    LauncherSettings,
    LauncherSetupServer,
    configured_from_environment,
    default_settings_path,
    detect_sdk_root,
    detect_userdata_mini,
    provision_qmt_read_only,
    read_launcher_settings,
    write_launcher_settings,
)
from auto_trading_system.config.node_v2 import load_node_config
from auto_trading_system.gateways.broker.cicc_qmt import DiscoveredQmtAccount
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.runtime.engine_v2 import CN
from auto_trading_system.security.operator_password import OperatorPasswordHasher
from auto_trading_system.strategies.trading_settings import TradingSettings
from auto_trading_system.strategies.trading_settings_drafts import TradingSettingsDraftStore


def live_files(tmp_path: Path) -> LauncherSettings:
    sdk = tmp_path / "sdk"
    (sdk / "xtquant").mkdir(parents=True)
    (sdk / "xtquant" / "xttrader.py").write_text("", encoding="utf-8")
    userdata = tmp_path / "userdata_mini"
    userdata.mkdir()
    account = "example-stock-account"
    fingerprint = hashlib.sha256(account.encode()).hexdigest()[:16]
    config = preview_config(
        tmp_path / "node-data", "setup-test", now=datetime(2026, 10, 21, tzinfo=CN)
    )
    raw = config.model_dump(mode="json")
    raw.update(
        broker="QMT_TRADING",
        account_id="qmt-" + fingerprint,
        qmt_read_only={
            "userdata_mini": str(userdata),
            "account_fingerprint": fingerprint,
            "session_base": 6102301,
        },
    )
    path = tmp_path / "node.json"
    path.write_bytes(canonical_bytes(raw))
    return LauncherSettings(path, sdk)


def test_launcher_settings_round_trip_and_reject_invalid_paths(tmp_path: Path) -> None:
    settings = live_files(tmp_path)
    target = tmp_path / "settings" / "launcher.json"
    write_launcher_settings(target, settings)
    assert read_launcher_settings(target) == settings

    (settings.sdk_root / "xtquant" / "xttrader.py").unlink()
    assert read_launcher_settings(target) is None


def test_launcher_settings_reject_missing_invalid_and_non_trading_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="完整路径"):
        LauncherSettings(Path("node.json"), Path("sdk")).validate()
    with pytest.raises(ValueError, match="没有找到"):
        LauncherSettings(tmp_path / "missing.json", tmp_path).validate()

    sdk = tmp_path / "sdk"
    (sdk / "xtquant").mkdir(parents=True)
    (sdk / "xtquant" / "xttrader.py").write_text("", encoding="utf-8")
    broken = tmp_path / "broken.json"
    broken.write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="无法读取"):
        LauncherSettings(broken, sdk).validate()

    preview = preview_config(
        tmp_path / "preview", "not-live", now=datetime(2026, 10, 21, tzinfo=CN)
    )
    mock = tmp_path / "mock.json"
    mock.write_bytes(canonical_bytes(preview.model_dump(mode="json")))
    with pytest.raises(ValueError, match="不是 QMT"):
        LauncherSettings(mock, sdk).validate()


def test_auto_detected_qmt_connection_creates_read_only_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = tmp_path / "sdk"
    (sdk / "xtquant").mkdir(parents=True)
    (sdk / "xtquant" / "xttrader.py").write_text("", encoding="utf-8")
    userdata = tmp_path / "userdata_mini"
    (userdata / "up_queue_xtquant").mkdir(parents=True)
    monkeypatch.setenv("ATS_QMT_USERDATA_MINI", str(userdata))
    assert detect_userdata_mini() == userdata
    monkeypatch.setattr(
        "auto_trading_system.apps.desktop.setup.discover_qmt_accounts",
        lambda _config: (DiscoveredQmtAccount("example-stock-account", 2),),
    )

    target = tmp_path / "settings" / "launcher.json"
    target.parent.mkdir()
    settings = provision_qmt_read_only(target, sdk_root=sdk, userdata_mini=userdata)
    config = load_node_config(settings.config_path)

    assert settings == read_launcher_settings(target)
    assert config.broker == "QMT_READ_ONLY"
    assert config.qmt_read_only is not None
    assert config.qmt_read_only.userdata_mini == userdata
    assert config.operator_access is not None
    session = ConfiguredSession(settings.config_path, sdk)
    assert session.config.broker == "QMT_READ_ONLY"
    assert OperatorPasswordHasher().verify(
        DESKTOP_OPERATOR_PASSWORD, session.environment["ATS_SECRET_DESKTOP_VERIFIER"]
    )
    session._prepare_files()
    session._validate_ready_snapshot({"real_trading_enabled": False})
    with pytest.raises(ValueError, match="wrong mode"):
        session._validate_ready_snapshot({"real_trading_enabled": True})
    session.automation_tick()
    assert session.children == {}
    assert session._last_automatic_job is None
    session.stop()


@pytest.mark.parametrize("same_account", [True, False])
def test_qmt_reconnect_preserves_settings_only_for_the_same_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_account: bool
) -> None:
    sdk = tmp_path / "sdk"
    (sdk / "xtquant").mkdir(parents=True)
    (sdk / "xtquant" / "xttrader.py").write_text("", encoding="utf-8")
    userdata = tmp_path / "userdata_mini"
    (userdata / "up_queue_xtquant").mkdir(parents=True)
    account = "example-stock-account"
    monkeypatch.setattr(
        "auto_trading_system.apps.desktop.setup.discover_qmt_accounts",
        lambda _config: (DiscoveredQmtAccount(account, 2),),
    )
    target = tmp_path / "launcher.json"
    first = provision_qmt_read_only(target, sdk_root=sdk, userdata_mini=userdata)
    config = load_node_config(first.config_path)
    value = TradingSettings.model_validate_json(
        b'{"buy":{"order_method":"ASK1_PROTECTED_LIMIT","max_daily_buy_value":"0",'
        b'"max_symbol_buy_value":"0","max_order_value":"0",'
        b'"cancel_after_seconds":20,"max_retries":3},'
        b'"sell":{"take_profit_pct":"5","stop_loss_pct":"5","sell_by":"14:40"},'
        b'"safety":{"max_daily_loss":"20000","max_positions":3,'
        b'"emergency_liquidation_enabled":false},'
        b'"advanced":{"max_order_quantity":10000,"max_chase_bps":50,'
        b'"max_spread_bps":30,"quote_max_age_ms":2000}}'
    )
    store = TradingSettingsDraftStore(config.data_root)
    saved = store.save(value, expected_version=0)
    original_bytes = store.path.read_bytes()
    original_config = first.config_path.read_bytes()
    # A changed SDK installation must not make an existing profile disappear.
    moved_sdk = tmp_path / "moved-sdk"
    sdk.rename(moved_sdk)
    if not same_account:
        account = "another-stock-account"
    second = provision_qmt_read_only(target, sdk_root=moved_sdk, userdata_mini=userdata)
    reopened = read_launcher_settings(target)
    assert reopened == second
    new_config = load_node_config(second.config_path)
    restored = TradingSettingsDraftStore(new_config.data_root).read()
    assert (second.config_path == first.config_path) is same_account
    assert restored == (saved if same_account else None)
    assert store.path.read_bytes() == original_bytes
    assert first.config_path.read_bytes() == original_config


def test_configured_session_preserves_formal_credentials_and_rejects_bad_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = live_files(tmp_path)
    monkeypatch.setenv("ATS_SECRET_DESKTOP_VERIFIER", "external-credential-must-be-preserved")
    session = ConfiguredSession(settings.config_path, settings.sdk_root)
    assert (
        session.environment["ATS_SECRET_DESKTOP_VERIFIER"]
        == "external-credential-must-be-preserved"
    )
    session._validate_ready_snapshot({"real_trading_enabled": True})
    with pytest.raises(ValueError, match="wrong mode"):
        session._validate_ready_snapshot({"real_trading_enabled": False})
    for config_path, sdk_root in (
        (Path("relative.json"), settings.sdk_root),
        (settings.config_path, Path("relative-sdk")),
    ):
        with pytest.raises(ValueError, match="absolute path"):
            ConfiguredSession(config_path, sdk_root)
    (settings.sdk_root / "xtquant" / "xttrader.py").unlink()
    with pytest.raises(ValueError, match="contain xtquant"):
        ConfiguredSession(settings.config_path, settings.sdk_root)


def test_qmt_connection_rejects_invalid_local_paths(tmp_path: Path) -> None:
    userdata = tmp_path / "userdata_mini"
    (userdata / "up_queue_xtquant").mkdir(parents=True)
    with pytest.raises(ValueError, match="完整路径"):
        provision_qmt_read_only(
            tmp_path / "launcher.json",
            sdk_root=Path("relative-sdk"),
            userdata_mini=userdata,
        )

    sdk = tmp_path / "sdk"
    sdk.mkdir()
    with pytest.raises(ValueError, match="XtQuant SDK"):
        provision_qmt_read_only(tmp_path / "launcher.json", sdk_root=sdk, userdata_mini=userdata)

    (sdk / "xtquant").mkdir()
    (sdk / "xtquant" / "xttrader.py").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="MiniQMT"):
        provision_qmt_read_only(
            tmp_path / "launcher.json", sdk_root=sdk, userdata_mini=tmp_path / "other"
        )


def test_qmt_connection_reports_discovery_failure_and_restores_import_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = tmp_path / "sdk"
    (sdk / "xtquant").mkdir(parents=True)
    (sdk / "xtquant" / "xttrader.py").write_text("", encoding="utf-8")
    userdata = tmp_path / "userdata_mini"
    (userdata / "up_queue_xtquant").mkdir(parents=True)

    def fail_discovery(_config: object) -> tuple[()]:
        raise RuntimeError("QMT offline")

    monkeypatch.setattr(
        "auto_trading_system.apps.desktop.setup.discover_qmt_accounts", fail_discovery
    )
    with pytest.raises(ValueError, match="连接MiniQMT失败"):
        provision_qmt_read_only(tmp_path / "launcher.json", sdk_root=sdk, userdata_mini=userdata)
    assert str(sdk) not in __import__("sys").path


@pytest.mark.parametrize(
    ("accounts", "message"),
    [
        ((DiscoveredQmtAccount("credit-account", 3),), "没有返回股票账户"),
        (
            (
                DiscoveredQmtAccount("stock-account-one", 2),
                DiscoveredQmtAccount("stock-account-two", 2),
            ),
            "发现多个股票账户",
        ),
    ],
)
def test_qmt_connection_requires_exactly_one_stock_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    accounts: tuple[DiscoveredQmtAccount, ...],
    message: str,
) -> None:
    sdk = tmp_path / "sdk"
    (sdk / "xtquant").mkdir(parents=True)
    (sdk / "xtquant" / "xttrader.py").write_text("", encoding="utf-8")
    userdata = tmp_path / "userdata_mini"
    (userdata / "up_queue_xtquant").mkdir(parents=True)
    monkeypatch.setattr(
        "auto_trading_system.apps.desktop.setup.discover_qmt_accounts",
        lambda _config: accounts,
    )

    with pytest.raises(ValueError, match=message):
        provision_qmt_read_only(tmp_path / "launcher.json", sdk_root=sdk, userdata_mini=userdata)


def test_default_and_environment_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(RuntimeError, match="LOCALAPPDATA"):
        default_settings_path()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_settings_path() == tmp_path / "AutoTradingSystem" / "launcher.json"

    monkeypatch.delenv("ATS_LIVE_NODE_CONFIG", raising=False)
    monkeypatch.delenv("ATS_QMT_SDK_ROOT", raising=False)
    assert configured_from_environment() is None
    settings = live_files(tmp_path / "live")
    monkeypatch.setenv("ATS_LIVE_NODE_CONFIG", str(settings.config_path))
    monkeypatch.setenv("ATS_QMT_SDK_ROOT", str(settings.sdk_root))
    assert configured_from_environment() == settings
    monkeypatch.setenv("ATS_QMT_SDK_ROOT", str(tmp_path / "bad-sdk"))
    assert configured_from_environment() is None


def test_detection_handles_unreadable_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable(_path: Path) -> Path:
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "absolute", unreadable)
    assert detect_sdk_root() is None
    assert detect_userdata_mini() is None


def test_launcher_settings_reject_unknown_schema(tmp_path: Path) -> None:
    path = tmp_path / "launcher.json"
    path.write_text('{"schema":2}', encoding="utf-8")
    assert read_launcher_settings(path) is None


def test_first_run_page_stays_safe_until_valid_settings_are_saved(tmp_path: Path) -> None:
    settings = live_files(tmp_path)
    target = tmp_path / "launcher.json"
    server = LauncherSetupServer(target)
    server.start()
    try:
        with httpx.Client(trust_env=False, timeout=3) as client:
            page = client.get(server.url)
            assert page.status_code == 200
            assert "系统设置" in page.text
            assert "没有启动交易引擎" in page.text

            origin = server.url.rstrip("/")
            invalid = client.post(
                server.url + "save",
                data={"config_path": str(settings.config_path), "sdk_root": str(tmp_path)},
                headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"},
            )
            assert invalid.status_code == 400
            assert not server.completed.is_set() and server.selection is None

            saved = client.post(
                server.url + "save",
                data={
                    "config_path": str(settings.config_path),
                    "sdk_root": str(settings.sdk_root),
                },
                headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"},
            )
            assert saved.status_code == 200 and "配置检查通过" in saved.text
        assert server.completed.wait(1)
        selected = cast(LauncherSettings | None, server.selection)
        assert isinstance(selected, LauncherSettings) and selected == settings
        assert read_launcher_settings(target) == settings
    finally:
        server.close()


def test_first_run_page_connects_qmt_without_a_node_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = tmp_path / "sdk"
    (sdk / "xtquant").mkdir(parents=True)
    (sdk / "xtquant" / "xttrader.py").write_text("", encoding="utf-8")
    userdata = tmp_path / "userdata_mini"
    (userdata / "up_queue_xtquant").mkdir(parents=True)
    monkeypatch.setattr(
        "auto_trading_system.apps.desktop.setup.discover_qmt_accounts",
        lambda _config: (DiscoveredQmtAccount("example-stock-account", 2),),
    )
    target = tmp_path / "settings" / "launcher.json"
    target.parent.mkdir()
    server = LauncherSetupServer(target, sdk_root_hint=sdk)
    server.start()
    try:
        with httpx.Client(trust_env=False, timeout=3) as client:
            origin = server.url.rstrip("/")
            response = client.post(
                server.url + "connect",
                data={"userdata_mini": str(userdata), "sdk_root": str(sdk)},
                headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"},
            )
        assert response.status_code == 200 and "QMT连接成功" in response.text
        assert server.completed.wait(1)
        assert server.selection is not None
        assert load_node_config(server.selection.config_path).broker == "QMT_READ_ONLY"
    finally:
        server.close()


def test_first_run_post_rejects_cross_origin_request(tmp_path: Path) -> None:
    settings = live_files(tmp_path)
    server = LauncherSetupServer(tmp_path / "launcher.json")
    server.start()
    try:
        with httpx.Client(trust_env=False, timeout=3) as client:
            response = client.post(
                server.url + "save",
                data={
                    "config_path": str(settings.config_path),
                    "sdk_root": str(settings.sdk_root),
                },
                headers={"Origin": "http://attacker.invalid", "Sec-Fetch-Site": "cross-site"},
            )
        assert response.status_code == 403
        assert not server.completed.is_set() and not (tmp_path / "launcher.json").exists()
    finally:
        server.close()


def test_setup_page_rejects_incomplete_connect(tmp_path: Path) -> None:
    settings = live_files(tmp_path)
    server = LauncherSetupServer(tmp_path / "launcher.json")
    server.start()
    try:
        with httpx.Client(trust_env=False, timeout=3) as client:
            headers = {
                "Origin": server.url.rstrip("/"),
                "Sec-Fetch-Site": "same-origin",
            }
            incomplete = client.post(
                server.url + "connect", data={"sdk_root": str(settings.sdk_root)}, headers=headers
            )
            assert incomplete.status_code == 400
    finally:
        server.close()


def test_setup_server_rejects_unknown_pages_and_duplicate_start(tmp_path: Path) -> None:
    server = LauncherSetupServer(tmp_path / "launcher.json")
    server.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            server.start()
        with httpx.Client(trust_env=False, timeout=3) as client:
            assert client.get(server.url + "unknown").status_code == 404
            response = client.post(
                server.url + "save",
                data={"config_path": "only-one-field"},
                headers={
                    "Origin": server.url.rstrip("/"),
                    "Sec-Fetch-Site": "same-origin",
                },
            )
            assert response.status_code == 400
            assert "请填写完整" in response.text
    finally:
        server.close()


def test_setup_server_can_close_before_it_is_started(tmp_path: Path) -> None:
    server = LauncherSetupServer(tmp_path / "launcher.json")
    server.close()
