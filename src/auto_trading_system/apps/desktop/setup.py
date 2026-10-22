"""Safe first-run launcher settings served on loopback before any trading process starts."""

# The embedded page is deliberately self-contained for the single-file Windows build.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import threading
import webbrowser
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import randbelow
from tempfile import NamedTemporaryFile
from typing import Final
from urllib.parse import parse_qs
from uuid import uuid4

from auto_trading_system.config.node_v2 import load_node_config
from auto_trading_system.control.windows_pipe import current_sid
from auto_trading_system.gateways.broker.cicc_qmt import discover_qmt_accounts
from auto_trading_system.monitoring.qmt_readonly import QmtProbeConfig
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.runtime.engine_v2 import CN
from auto_trading_system.runtime.node_layout import create_private_directory, initialize_layout

from .session import preview_config

_MAX_BODY: Final = 16_384


@dataclass(frozen=True, slots=True)
class LauncherSettings:
    config_path: Path
    sdk_root: Path

    def validate(self) -> None:
        for value, label in (
            (self.config_path, "QMT 实盘节点配置"),
            (self.sdk_root, "XtQuant SDK 目录"),
        ):
            if not value.is_absolute() or value.resolve() != value.absolute():
                raise ValueError(f"{label}必须使用完整路径")
        if not self.config_path.is_file():
            raise ValueError("没有找到 QMT 实盘节点配置文件")
        if not (self.sdk_root / "xtquant" / "xttrader.py").is_file():
            raise ValueError("所选目录中没有找到 xtquant/xttrader.py")
        try:
            config = load_node_config(self.config_path)
        except Exception as exc:
            raise ValueError("QMT 实盘节点配置无法读取或内容不完整") from exc
        if config.broker not in {"QMT_READ_ONLY", "QMT_TRADING"}:
            raise ValueError("所选配置不是 QMT 连接配置")


def default_settings_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise RuntimeError("LOCALAPPDATA_UNAVAILABLE")
    return Path(base).absolute() / "AutoTradingSystem" / "launcher.json"


def detect_sdk_root() -> Path | None:
    """Find a prepared XtQuant SDK without scanning the whole computer."""
    configured = os.environ.get("ATS_QMT_SDK_ROOT")
    candidates: list[Path] = [Path(configured)] if configured else []
    executable = Path(sys.executable).resolve().parent
    source_root = Path(__file__).resolve().parents[4]
    for root in (executable, executable.parent, source_root):
        candidates.extend((root / "sdk", root / "xtquant-sdk"))
    prepared = source_root / ".codex_tmp"
    if prepared.is_dir():
        candidates.extend(
            path / "sdk"
            for path in sorted(
                prepared.glob("qmt-readonly-verified-*"),
                key=lambda item: item.name,
                reverse=True,
            )
        )
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            absolute = candidate.absolute()
            if absolute in seen:
                continue
            seen.add(absolute)
            if absolute.resolve() == absolute and (absolute / "xtquant" / "xttrader.py").is_file():
                return absolute
        except OSError:
            continue
    return None


def detect_userdata_mini() -> Path | None:
    """Find the local MiniQMT data directory from bounded known locations."""
    configured = os.environ.get("ATS_QMT_USERDATA_MINI")
    candidates = [Path(configured)] if configured else []
    for drive in ("C", "D", "E", "F"):
        candidates.extend(
            (
                Path(f"{drive}:/中金财富QMT个人版交易端/userdata_mini"),
                Path(f"{drive}:/迅投极速交易终端/userdata_mini"),
                Path(f"{drive}:/国金QMT交易端/userdata_mini"),
            )
        )
    for candidate in candidates:
        try:
            absolute = candidate.absolute()
            if (
                absolute.resolve() == absolute
                and absolute.name.casefold() == "userdata_mini"
                and (absolute / "up_queue_xtquant").exists()
            ):
                return absolute
        except OSError:
            continue
    return None


def provision_qmt_read_only(
    settings_path: Path, *, sdk_root: Path, userdata_mini: Path
) -> LauncherSettings:
    """Discover the real QMT stock account and create a safe local connection profile."""
    if not sdk_root.is_absolute() or sdk_root.resolve() != sdk_root.absolute():
        raise ValueError("XtQuant SDK目录必须使用完整路径")
    if not (sdk_root / "xtquant" / "xttrader.py").is_file():
        raise ValueError("没有找到可用的XtQuant SDK")
    if (
        not userdata_mini.is_absolute()
        or userdata_mini.resolve() != userdata_mini.absolute()
        or userdata_mini.name.casefold() != "userdata_mini"
        or not (userdata_mini / "up_queue_xtquant").exists()
    ):
        raise ValueError("没有找到可用的MiniQMT数据目录")

    sdk_text = str(sdk_root)
    session_base = 10_000_000 + randbelow(2_000_000_000)
    inserted = sdk_text not in sys.path
    if inserted:
        sys.path.insert(0, sdk_text)
    try:
        accounts = tuple(
            item
            for item in discover_qmt_accounts(
                QmtProbeConfig(userdata_mini, session_base, "STOCK", 3)
            )
            if item.account_type == 2
        )
    except Exception as exc:
        raise ValueError("连接MiniQMT失败，请确认交易端已经启动并登录") from exc
    finally:
        if inserted:
            with suppress(ValueError):
                sys.path.remove(sdk_text)
    if not accounts:
        raise ValueError("MiniQMT没有返回股票账户，请确认已登录股票资金账户")
    if len(accounts) != 1:
        raise ValueError("发现多个股票账户，自动选择已停止；稍后将在页面提供账户选择")

    account_fingerprint = hashlib.sha256(accounts[0].account_id.encode()).hexdigest()[:16]
    # Reconnecting the same local account must not replace its durable data root.
    # Validate against the newly selected SDK so relocating the SDK also preserves settings.
    reusable: LauncherSettings | None = None
    try:
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        if isinstance(saved, dict) and saved.get("schema") == 1:
            existing = LauncherSettings(Path(saved["config_path"]), sdk_root)
            existing.validate()
            previous = load_node_config(existing.config_path)
            binding = previous.qmt_read_only
            if (
                previous.broker == "QMT_READ_ONLY"
                and previous.service_sid == current_sid()
                and binding is not None
                and binding.account_fingerprint == account_fingerprint
                and binding.userdata_mini == userdata_mini
            ):
                reusable = existing
    except (KeyError, OSError, TypeError, ValueError):
        pass
    if reusable is not None:
        write_launcher_settings(settings_path, reusable)
        return reusable
    identity = uuid4().hex
    profile_root = settings_path.parent / "QmtConnected" / ("profile-" + identity)
    profile_root.parent.mkdir(parents=True, exist_ok=True)
    create_private_directory(profile_root, current_sid())
    config = preview_config(profile_root, identity, now=datetime.now(CN))
    raw = config.model_dump(mode="json")
    raw.update(
        broker="QMT_READ_ONLY",
        node_id="qmt-connected-" + identity,
        account_id="qmt-" + account_fingerprint,
        strategy_id="tail-entry",
        data_root=str(profile_root / "node-data"),
        qmt_read_only={
            "userdata_mini": str(userdata_mini),
            "account_fingerprint": account_fingerprint,
            "session_base": session_base,
        },
    )
    connected = type(config).model_validate_json(canonical_bytes(raw))
    initialize_layout(connected)
    config_path = profile_root / "node.json"
    with config_path.open("xb") as output:
        output.write(canonical_bytes(connected.model_dump(mode="json")))
        output.flush()
        os.fsync(output.fileno())
    settings = LauncherSettings(config_path, sdk_root)
    write_launcher_settings(settings_path, settings)
    return settings


def read_launcher_settings(path: Path) -> LauncherSettings | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is not dict or raw.get("schema") != 1:
            return None
        settings = LauncherSettings(Path(raw["config_path"]), Path(raw["sdk_root"]))
        settings.validate()
        return settings
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def write_launcher_settings(path: Path, settings: LauncherSettings) -> None:
    settings.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "schema": 1,
            "config_path": str(settings.config_path),
            "sdk_root": str(settings.sdk_root),
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "x", encoding="utf-8", dir=path.parent, prefix="launcher-", suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def configured_from_environment() -> LauncherSettings | None:
    config = os.environ.get("ATS_LIVE_NODE_CONFIG")
    sdk = os.environ.get("ATS_QMT_SDK_ROOT")
    if not config or not sdk:
        return None
    settings = LauncherSettings(Path(config), Path(sdk))
    try:
        settings.validate()
    except ValueError:
        return None
    return settings


def _page(
    *,
    config_path: str = "",
    sdk_root: str = "",
    userdata_mini: str = "",
    message: str = "",
) -> bytes:
    notice = f'<div class="notice" role="alert">{html.escape(message)}</div>' if message else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>自动交易系统 · 系统设置</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f7fb;color:#172033;font-family:"Microsoft YaHei UI",sans-serif}}
header{{background:#10213a;color:white;padding:18px 28px}}header b{{font-size:20px}}header span{{margin-left:12px;color:#b8c7dc}}
main{{max-width:860px;margin:36px auto;padding:0 22px}}.card{{background:white;border:1px solid #dbe3ee;border-radius:14px;padding:28px;box-shadow:0 8px 28px #20324d12}}
h1{{font-size:26px;margin:0 0 8px}}p{{line-height:1.7;color:#506078}}.lock{{background:#fff7e8;border:1px solid #f0cf91;border-radius:9px;padding:13px 15px;color:#7a5110}}
label{{display:block;font-weight:700;margin-top:22px}}input{{width:100%;margin-top:8px;padding:12px;border:1px solid #bfcbdc;border-radius:8px;font-size:15px}}
small{{display:block;color:#6c7b90;margin-top:6px}}button{{margin-top:26px;background:#2463d4;color:white;border:0;border-radius:8px;padding:12px 22px;font-size:16px;font-weight:700;cursor:pointer}}
.notice{{margin:18px 0;background:#fff0ef;border:1px solid #e9aaa5;color:#8f2920;border-radius:8px;padding:12px}}details{{margin-top:24px;color:#506078}}
</style></head><body>
<header><b>自动交易</b><span>系统设置</span></header>
<main><section class="card"><h1>连接本机 QMT</h1>
<p>系统会自动识别本机 MiniQMT 和股票账户。你不需要寻找或填写节点配置文件。</p>
<div class="lock">当前没有启动交易引擎。本步骤只连接并读取账户，不会启动自动交易，也不会发送、撤销任何委托。</div>{notice}
<form method="post" action="/connect">
<label for="userdata">MiniQMT 数据目录</label>
<input id="userdata" name="userdata_mini" value="{html.escape(userdata_mini, quote=True)}" placeholder="系统未识别时再选择 userdata_mini 目录" required>
<small>一般已经自动识别。目录名称必须是 userdata_mini。</small>
<label for="sdk">XtQuant SDK 目录</label>
<input id="sdk" name="sdk_root" value="{html.escape(sdk_root, quote=True)}" placeholder="目录下应包含 xtquant\\xttrader.py" required>
<small>系统已经尽量自动识别；这是 SDK 所在目录。</small>
<button type="submit">连接并读取 QMT 账户</button></form>
<details><summary>已有正式节点配置 - 高级</summary>
<p>只有量化后台已经生成正式 QMT_TRADING 配置时才使用这里。普通使用不需要填写。</p>
<form method="post" action="/save"><label for="config">正式配置文件</label>
<input id="config" name="config_path" value="{html.escape(config_path, quote=True)}" placeholder="量化后台生成的 node.json">
<input type="hidden" name="sdk_root" value="{html.escape(sdk_root, quote=True)}">
<button type="submit">导入正式配置</button></form></details>
</section></main></body></html>""".encode()


def _success_page(*, trading: bool) -> bytes:
    heading = "配置检查通过" if trading else "QMT连接成功"
    message = (
        "正式配置检查通过，系统正在启动QMT实盘节点。新页面打开后仍保持暂停。"
        if trading
        else "MiniQMT账户连接成功。系统正在切换到真实账户只读核验页面，不会发送委托。"
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>连接完成</title><style>body{{font-family:"Microsoft YaHei UI",sans-serif;background:#f4f7fb;color:#172033;margin:0}}.card{{max-width:680px;margin:80px auto;background:white;border:1px solid #dbe3ee;border-radius:14px;padding:32px}}h1{{color:#18754a}}p{{line-height:1.8;color:#506078}}</style></head><body><main class="card"><h1>{heading}</h1><p>{message}</p></main></body></html>""".encode()


class LauncherSetupServer:
    def __init__(self, settings_path: Path, *, sdk_root_hint: Path | None = None) -> None:
        self.settings_path = settings_path
        self.sdk_root_hint = sdk_root_hint or detect_sdk_root()
        self.userdata_mini_hint = detect_userdata_mini()
        self.completed = threading.Event()
        self.selection: LauncherSettings | None = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _trusted(self) -> bool:
                return self.headers.get("Host") == f"127.0.0.1:{owner._server.server_port}"

            def _send(self, status: HTTPStatus, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
                )
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if not self._trusted() or self.path != "/":
                    self._send(HTTPStatus.NOT_FOUND, b"")
                    return
                saved = read_launcher_settings(owner.settings_path)
                self._send(
                    HTTPStatus.OK,
                    _page(
                        config_path=str(saved.config_path) if saved else "",
                        sdk_root=str(saved.sdk_root) if saved else str(owner.sdk_root_hint or ""),
                        userdata_mini=str(owner.userdata_mini_hint or ""),
                    ),
                )

            def do_POST(self) -> None:
                expected_origin = f"http://127.0.0.1:{owner._server.server_port}"
                length = self.headers.get("Content-Length", "")
                if (
                    not self._trusted()
                    or self.path not in {"/connect", "/save"}
                    or self.headers.get("Origin") != expected_origin
                    or self.headers.get("Sec-Fetch-Site") != "same-origin"
                    or not re.fullmatch(r"[0-9]{1,5}", length)
                    or int(length) > _MAX_BODY
                ):
                    self._send(HTTPStatus.FORBIDDEN, b"")
                    return
                raw = self.rfile.read(int(length))
                try:
                    values = parse_qs(raw.decode("utf-8", errors="strict"), strict_parsing=True)
                    if self.path == "/connect":
                        if set(values) != {"userdata_mini", "sdk_root"}:
                            raise ValueError("请填写完整的QMT目录信息")
                        userdata_text = values["userdata_mini"][0].strip()
                        sdk_text = values["sdk_root"][0].strip()
                        settings = provision_qmt_read_only(
                            owner.settings_path,
                            sdk_root=Path(sdk_text),
                            userdata_mini=Path(userdata_text),
                        )
                        trading = False
                    else:
                        if set(values) != {"config_path", "sdk_root"}:
                            raise ValueError("请填写完整的正式配置信息")
                        config_text = values["config_path"][0].strip()
                        sdk_text = values["sdk_root"][0].strip()
                        settings = LauncherSettings(Path(config_text), Path(sdk_text))
                        settings.validate()
                        if load_node_config(settings.config_path).broker != "QMT_TRADING":
                            raise ValueError("高级导入只接受量化后台生成的正式实盘配置")
                        write_launcher_settings(owner.settings_path, settings)
                        trading = True
                except (IndexError, UnicodeError, ValueError, OSError) as exc:
                    message = str(exc) if str(exc) else "配置保存失败"
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        _page(
                            config_path=locals().get("config_text", ""),
                            sdk_root=locals().get("sdk_text", ""),
                            userdata_mini=locals().get(
                                "userdata_text", str(owner.userdata_mini_hint or "")
                            ),
                            message=message,
                        ),
                    )
                    return
                owner.selection = settings
                self._send(HTTPStatus.OK, _success_page(trading=trading))
                owner.completed.set()

        return Handler

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("setup server already started")
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="launcher-setup", daemon=True
        )
        self._thread.start()

    def open(self) -> None:
        webbrowser.open(self.url)

    def close(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
