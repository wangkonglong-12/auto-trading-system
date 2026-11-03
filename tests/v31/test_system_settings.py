"""Local system settings HTTP/persistence boundary; no network or real vault writes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from auto_trading_system.apps.desktop.__main__ import confirm_desktop_close
from auto_trading_system.apps.desktop.session import preview_config
from auto_trading_system.apps.local_console.node_app import build_node_console_app
from auto_trading_system.config.node_v2 import load_node_config
from auto_trading_system.config.system_settings import (
    SystemSettings,
    SystemSettingsError,
    SystemSettingsStore,
)

ORIGIN = "http://127.0.0.1:8780"
HEADERS = {"origin": ORIGIN, "sec-fetch-site": "same-origin", "X-CSRF-Token": "test-csrf"}
PREFIX = "/api/v2/node/"


@pytest.mark.parametrize(
    "host",
    [
        "https://example.com",
        "host:8443",
        "host/path",
        "host?x=1",
        "u@host",
        "",
        "-bad.com",
        "bad..com",
        "host#fragment",
        "host\\path",
    ],
)
def test_rejects_non_host_inputs(host: str) -> None:
    with pytest.raises(ValueError):
        SystemSettings(quant_host=host)


@pytest.mark.parametrize(
    "host, expected",
    [
        ("Example.COM", "https://example.com:8443"),
        ("127.0.0.1", "https://127.0.0.1:8443"),
        ("::1", "https://[::1]:8443"),
    ],
)
def test_valid_host_and_port(host: str, expected: str) -> None:
    assert SystemSettings(quant_host=host, quant_port=8443).quant_origin == expected
    for invalid in (0, 65536, True, "443", 1.2):
        with pytest.raises(ValidationError):
            SystemSettings(quant_host=host, quant_port=invalid)


def test_atomic_preferences_conflicts_failure_and_desktop_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SystemSettingsStore(tmp_path)
    assert store.read().close_warning
    ask = Mock(return_value=False)
    assert not confirm_desktop_close(tmp_path, ask)
    ask.assert_called_once()
    saved = store.save(SystemSettings(quant_host="quant.test", close_warning=False))
    assert saved.version == 1
    assert SystemSettingsStore(tmp_path).read() == saved
    ask.reset_mock()
    assert not confirm_desktop_close(tmp_path, ask)
    ask.assert_called_once()
    assert confirm_desktop_close(tmp_path, Mock(return_value=True))
    with pytest.raises(SystemSettingsError, match="VERSION_CONFLICT"):
        store.save(SystemSettings())

    def fail(*_):
        raise OSError("test failure")

    monkeypatch.setattr("auto_trading_system.config.system_settings.os.replace", fail)
    with pytest.raises(SystemSettingsError, match="SAVE_FAILED"):
        store.save(saved)
    assert store.read() == saved
    store.path.write_text("corrupted", encoding="utf-8")
    with pytest.raises(SystemSettingsError, match="STORE_INVALID"):
        store.read()
    assert not confirm_desktop_close(tmp_path, Mock(return_value=False))


def test_http_password_csrf_and_backend_applies_only_after_reload(tmp_path: Path) -> None:
    config = preview_config(tmp_path, "test-system", now=datetime.now().astimezone())
    config.data_root.mkdir()
    path = tmp_path / "node.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    status = SimpleNamespace(start=Mock(), close=Mock(), snapshot=Mock(return_value={}))
    invoke = Mock(return_value={"password_changed": True})
    bridge = SimpleNamespace(status=status, invoke=invoke)
    with TestClient(build_node_console_app(config, bridge=bridge), base_url=ORIGIN) as client:
        assert client.get(PREFIX + "system-settings").status_code == 401
        client.cookies.set("ats_node_session", "test-session", domain="127.0.0.1", path="/")
        client.cookies.set("ats_node_csrf", "test-csrf", domain="127.0.0.1", path="/")
        first = client.get(PREFIX + "system-settings").json()
        assert first["quant_host"] == "127.0.0.1" and first["quant_port"] == 1
        body = {
            "version": 0,
            "quant_scheme": "http",
            "quant_host": "backend.example.com",
            "quant_port": 8443,
            "close_warning": False,
        }
        assert (
            client.post(
                PREFIX + "system-settings",
                json=body,
                headers={"origin": ORIGIN, "sec-fetch-site": "same-origin"},
            ).status_code
            == 401
        )
        response = client.post(PREFIX + "system-settings", json=body, headers=HEADERS)
        assert response.status_code == 200 and response.json()["restart_required"]
        assert config.quant_origin == "https://127.0.0.1:1"
        assert load_node_config(path).quant_origin == "http://backend.example.com:8443"
        assert (
            client.post(PREFIX + "system-settings", json=body, headers=HEADERS).status_code == 409
        )
        body["version"] = 1
        body["quant_host"] = "https://not-a-host/secret"
        invalid = client.post(PREFIX + "system-settings", json=body, headers=HEADERS)
        assert invalid.status_code == 400 and "not-a-host" not in invalid.text
        secret = {"current_password": "old-password-1234", "new_password": "new-password-1234"}
        denied = client.post(
            PREFIX + "password", json=secret, headers={**HEADERS, "origin": "https://other.test"}
        )
        assert denied.status_code == 403
        invoke.reset_mock()
        changed = client.post(PREFIX + "password", json=secret, headers=HEADERS)
        assert changed.status_code == 200
        invoke.assert_called_once_with(
            "authorization",
            "CHANGE_PASSWORD",
            {"session_token": "test-session", "csrf_token": "test-csrf", **secret},
        )
        assert "ats_node_session" not in client.cookies
        assert "ats_node_csrf" not in client.cookies
        assert secret["new_password"] not in changed.text


def test_api_token_saved_once_to_fake_vault_not_json_or_http_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import SecretStr

    from auto_trading_system.config import quant_access
    from auto_trading_system.config.credentials import SecretLoadError

    vault = {}

    class Vault:
        def read(self, reference):
            if reference.target not in vault:
                raise SecretLoadError("SECRET_NOT_FOUND", "missing")
            return SecretStr(vault[reference.target])

        def write(self, reference, token):
            vault[reference.target] = token

    monkeypatch.setattr(quant_access, "WindowsCredentialManagerSecretProvider", Vault)
    config = preview_config(tmp_path, "shared-access", now=datetime.now().astimezone())
    config.data_root.mkdir()
    status = SimpleNamespace(start=Mock(), close=Mock(), snapshot=Mock(return_value={}))
    bridge = SimpleNamespace(status=status, invoke=Mock(return_value={}))
    with TestClient(build_node_console_app(config, bridge=bridge), base_url=ORIGIN) as client:
        client.cookies.set("ats_node_session", "test-session", domain="127.0.0.1", path="/")
        client.cookies.set("ats_node_csrf", "test-csrf", domain="127.0.0.1", path="/")
        token = "s" * 43
        path = PREFIX + "system-settings/access-token"
        assert (
            client.post(
                path, json={"token": token}, headers={**HEADERS, "X-CSRF-Token": ""}
            ).status_code
            == 401
        )
        assert not vault
        answer = client.post(path, json={"token": token}, headers=HEADERS)
        assert answer.status_code == 200 and token not in answer.text
        assert quant_access.read_access_token(config.data_root) == token.encode()
        result = client.get(PREFIX + "system-settings")
        assert result.json()["access_token_configured"] and token not in result.text
        assert not (config.data_root / "system-settings.json").exists()
        assert client.post(path, json={"token": "short"}, headers=HEADERS).status_code == 400
        assert list(vault.values()) == [token]
