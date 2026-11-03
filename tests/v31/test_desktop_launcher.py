"""EXE 启动器的默认权限、冻结分派、真实进程及退出归属验证。"""

from __future__ import annotations

import json
import sys
import webbrowser
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from threading import Event, Thread, get_ident
from time import monotonic, sleep
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import httpx
import pytest

import auto_trading_system.apps.desktop.__main__ as desktop_main
from auto_trading_system.apps.desktop.__main__ import self_test, standard_streams, worker_arguments
from auto_trading_system.apps.desktop.session import (
    DESKTOP_OPERATOR_PASSWORD,
    ConfiguredSession,
    PreviewSession,
    UnconfiguredSession,
    node_command,
    preview_config,
)
from auto_trading_system.apps.desktop.setup import LauncherSettings
from auto_trading_system.config.node_v2 import load_node_config
from auto_trading_system.runtime.engine_v2 import CN
from auto_trading_system.security.operator_password import OperatorPasswordHasher


def test_preview_profile_has_no_trading_and_has_local_page_login(tmp_path: Path) -> None:
    config = preview_config(tmp_path / "new", "test-session", now=datetime(2026, 10, 19, tzinfo=CN))
    assert config.broker == "MOCK" and config.account_id.startswith("paper-")
    assert config.operator_access is not None and config.paper_session is None
    assert len(DESKTOP_OPERATOR_PASSWORD) >= 14
    assert config.calendar.version == "preview-not-for-trading"
    assert len(config.calendar.sessions) == 2
    assert config.quant_origin == "https://127.0.0.1:1"

    unconfigured = UnconfiguredSession(
        tmp_path / "unconfigured", now=datetime(2026, 10, 19, tzinfo=CN)
    )
    assert unconfigured.config.node_id.startswith("unconfigured-")
    assert unconfigured.config.account_id.startswith("qmt-not-configured-")
    assert unconfigured.config.broker == "MOCK"


def test_frozen_dispatch_is_not_a_python_or_arbitrary_module_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = ["--config", "preview.json", "engine"]
    assert node_command(args)[1:4] == ["-m", "auto_trading_system.apps.desktop", "--node"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert node_command(args)[1:] == ["--node", *args]
    assert worker_arguments(("--node", *args)) == args
    assert worker_arguments(["-m", "auto_trading_system.apps.node_process", *args]) == args
    assert worker_arguments(["-m", "untrusted.module"]) is None


def test_missing_windowed_streams_are_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(sys, name, None)
    with standard_streams():
        assert sys.stdin is not None and sys.stdout is not None and sys.stderr is not None
        assert sys.stdin.read() == ""
        print("test-windowed-output")
    assert sys.stdin is None and sys.stdout is None and sys.stderr is None


def test_worker_entry_rejects_bad_external_paths_and_dispatches_valid_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATS_QMT_SDK_ROOT", "relative-sdk")
    assert desktop_main.run_worker(["--config", "ignored.json", "engine"]) == 2

    sdk = tmp_path / "sdk"
    (sdk / "xtquant").mkdir(parents=True)
    (sdk / "xtquant/xttrader.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("ATS_QMT_SDK_ROOT", str(sdk))
    monkeypatch.setenv("ATS_DESKTOP_LOG_DIRECTORY", str(tmp_path / "missing-log"))
    assert desktop_main.run_worker(["--config", "ignored.json", "engine"]) == 2

    monkeypatch.delenv("ATS_QMT_SDK_ROOT")
    monkeypatch.delenv("ATS_DESKTOP_LOG_DIRECTORY")
    import auto_trading_system.apps.node_process.__main__ as node_process_main

    monkeypatch.setattr(node_process_main, "main", lambda arguments: len(arguments))
    assert desktop_main.run_worker(["--config", "node.json", "engine"]) == 3


def test_desktop_main_opens_first_run_settings_and_routes_explicit_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path | None, Path | None, bool]] = []

    def launch(
        *,
        config_path: Path | None = None,
        sdk_root: Path | None = None,
        unconfigured: bool = False,
    ) -> int:
        calls.append((config_path, sdk_root, unconfigured))
        return 7

    monkeypatch.setattr(desktop_main, "launch_window", launch)
    monkeypatch.setattr(desktop_main, "self_test", lambda path: 8 if path == tmp_path else 9)
    monkeypatch.setattr(desktop_main, "run_worker", lambda arguments: 10 + len(arguments))

    assert desktop_main.main(["--preview"]) == 7
    assert calls.pop() == (None, None, False)
    assert desktop_main.main(["--self-test", str(tmp_path)]) == 8
    assert desktop_main.main(["--node", "engine"]) == 11

    config = tmp_path / "node.json"
    sdk = tmp_path / "sdk"
    configured = SimpleNamespace(config_path=config, sdk_root=sdk, validate=lambda: None)
    environment_settings: list[object | None] = [configured]
    monkeypatch.setattr(
        desktop_main, "configured_from_environment", lambda: environment_settings[0]
    )
    monkeypatch.setenv("ATS_LIVE_NODE_CONFIG", str(config))
    monkeypatch.setenv("ATS_QMT_SDK_ROOT", str(sdk))
    assert desktop_main.main([]) == 7
    assert calls.pop() == (config, sdk, False)

    monkeypatch.delenv("ATS_LIVE_NODE_CONFIG")
    monkeypatch.delenv("ATS_QMT_SDK_ROOT")

    environment_settings[0] = None
    monkeypatch.setattr(desktop_main, "read_launcher_settings", lambda _: None)
    assert desktop_main.main([]) == 7
    assert calls.pop() == (None, None, True)

    monkeypatch.setattr(desktop_main, "launch_setup_window", lambda: configured)
    assert desktop_main.main(["--configure"]) == 7
    assert calls.pop() == (config, sdk, False)
    monkeypatch.setattr(desktop_main, "launch_setup_window", lambda: None)
    assert desktop_main.main(["--configure"]) == 0


def test_desktop_main_retries_only_after_explicit_window_recovery(tmp_path, monkeypatch):
    config, sdk = tmp_path / "node.json", tmp_path / "sdk"
    configured = SimpleNamespace(config_path=config, sdk_root=sdk, validate=lambda: None)
    monkeypatch.setattr(desktop_main, "configured_from_environment", lambda: configured)
    results = iter((12, 0))
    calls = []

    def launch(**kwargs):
        calls.append(kwargs)
        return next(results)

    monkeypatch.setattr(desktop_main, "launch_window", launch)
    assert desktop_main.main([]) == 0
    assert calls == [{"config_path": config, "sdk_root": sdk}] * 2


def test_native_locked_window_shows_recovery_without_modal_and_without_clipping(
    tmp_path, monkeypatch
):
    """Real Tk layout/event loop; no SDK, node process, or live profile is used."""
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    root.attributes("-alpha", 0.0)
    root.overrideredirect(True)

    class Session:
        def __init__(self, *_args):
            self.directory = tmp_path
            self.config = SimpleNamespace(broker="QMT_TRADING")
            self.stop_requested = Event()
            self.children = {}

        def start(self):
            self.stop_requested.set()
            raise RuntimeError("SUPERVISOR_BLOCKED")

        def stop(self):
            self.stop_requested.set()

    class Guard:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self):
            pass

        def release(self):
            pass

    monkeypatch.setattr(tk, "Tk", lambda: root)
    monkeypatch.setattr(desktop_main, "ConfiguredSession", Session)
    monkeypatch.setattr(desktop_main, "AccountInstanceGuard", Guard)
    monkeypatch.setattr(desktop_main, "interrupted_start_available", lambda _config: True)
    dialogs = []
    monkeypatch.setattr(messagebox, "showerror", lambda *args, **kwargs: dialogs.append(args))
    failures = []
    geometry = {}

    def inspect():
        try:
            pending = list(root.winfo_children())
            widgets = []
            while pending:
                widget = pending.pop()
                widgets.append(widget)
                pending.extend(widget.winfo_children())
            button = next(
                w
                for w in widgets
                if w.winfo_class() == "TButton" and w.cget("text") == "恢复启动并核验"
            )
            assert not dialogs
            assert str(button.cget("state")) == "normal"
            assert button.winfo_viewable()
            bottom = button.winfo_rooty() - root.winfo_rooty() + button.winfo_height()
            assert bottom <= root.winfo_height()
            assert (
                button.winfo_rootx() - root.winfo_rootx() + button.winfo_width()
                <= root.winfo_width()
            )
            geometry.update(
                width=root.winfo_width(), height=root.winfo_height(), button_bottom=bottom
            )
        except BaseException as exc:
            failures.append(exc)
        finally:
            root.destroy()

    root.after(0, root.deiconify)
    root.after(1000, inspect)
    try:
        assert (
            desktop_main.launch_window(config_path=tmp_path / "unused.json", sdk_root=tmp_path) == 0
        )
        assert not failures, failures
        assert geometry["height"] >= 440
    finally:
        if root.tk is not None:
            with suppress(tk.TclError):
                root.destroy()


def test_desktop_main_switches_from_safe_shell_to_saved_qmt_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "node.json"
    sdk = tmp_path / "sdk"
    configured = SimpleNamespace(config_path=config, sdk_root=sdk, validate=lambda: None)
    reads = iter((None, configured))
    calls: list[tuple[Path | None, Path | None, bool]] = []

    def launch(
        *,
        config_path: Path | None = None,
        sdk_root: Path | None = None,
        unconfigured: bool = False,
    ) -> int:
        calls.append((config_path, sdk_root, unconfigured))
        return 10 if unconfigured else 6

    monkeypatch.setattr(desktop_main, "launch_window", launch)
    monkeypatch.setattr(desktop_main, "configured_from_environment", lambda: None)
    monkeypatch.setattr(desktop_main, "read_launcher_settings", lambda _: next(reads))
    monkeypatch.setattr(desktop_main, "default_settings_path", lambda: tmp_path / "launcher.json")

    assert desktop_main.main([]) == 6
    assert calls == [(None, None, True), (config, sdk, False)]


def test_desktop_main_stays_closed_if_qmt_setup_was_not_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop_main, "configured_from_environment", lambda: None)
    monkeypatch.setattr(desktop_main, "read_launcher_settings", lambda _: None)
    monkeypatch.setattr(desktop_main, "default_settings_path", lambda: tmp_path / "launcher.json")
    monkeypatch.setattr(
        desktop_main,
        "launch_window",
        lambda **kwargs: 10 if kwargs.get("unconfigured") else 7,
    )

    assert desktop_main.main([]) == 0


def test_desktop_reopens_after_normal_day_rollover_without_reusing_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = SimpleNamespace(
        config_path=tmp_path / "node.json", sdk_root=tmp_path / "sdk", validate=lambda: None
    )
    monkeypatch.setattr(desktop_main, "configured_from_environment", lambda: settings)
    outcomes = iter((11, 7))
    calls: list[object] = []

    def launch(**kwargs: object) -> int:
        calls.append(kwargs)
        return next(outcomes)

    monkeypatch.setattr(desktop_main, "launch_window", launch)
    assert desktop_main.main([]) == 7
    assert len(calls) == 2


def test_desktop_self_test_writes_closed_process_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "fresh-self-test"

    class Window:
        tk = SimpleNamespace(call=lambda *_: "8.6.14")

        def withdraw(self) -> None:
            pass

        def update_idletasks(self) -> None:
            pass

        def destroy(self) -> None:
            pass

    tkinter = ModuleType("tkinter")
    tkinter.Tk = Window  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)

    class Session:
        def __init__(self, directory: Path) -> None:
            self.directory = directory
            directory.mkdir(parents=True)
            self.url = "http://127.0.0.1:32100/"
            self.children: dict[str, object] = {"engine": object(), "watchdog": object()}
            self.stopped_cleanly = False

        def start(self) -> dict[str, Any]:
            return {
                "components": {
                    "engine": {"availability": "CURRENT"},
                    "watchdog": {"availability": "CURRENT"},
                }
            }

        def worker_status(self) -> dict[str, Any]:
            return {
                "real_trading_enabled": False,
                "broker": "MOCK",
                "halted": True,
                "order_count": 0,
                "installed_limits": {"max_buy_value": "0"},
            }

        def stop(self) -> None:
            self.children.clear()
            self.stopped_cleanly = True

    monkeypatch.setattr(desktop_main, "PreviewSession", Session)

    class Response:
        def __init__(self, text: str = "", payload: object | None = None) -> None:
            self.status_code = 200
            self.text = text
            self._payload = payload

        def json(self) -> object:
            return self._payload

    class Client:
        def __init__(self, **_: object) -> None:
            pass

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def get(self, url: str) -> Response:
            if url.endswith("/"):
                return Response(
                    "<html>node-test 系统设置 没有启动交易引擎"
                    '<script src="/assets/app.js"></script></html>'
                )
            if url.endswith("trading-settings"):
                return Response(payload={"draft": None})
            return Response("asset")

        def post(self, *_: object, **__: object) -> Response:
            return Response(payload={"ok": True})

    monkeypatch.setattr(httpx, "Client", Client)

    assert self_test(target) == 0
    report = json.loads((target / "self-test.json").read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert report["first_run_settings_page"] is True
    assert report["owned_jobs_closed"] is True and report["owned_job_count"] == 2


def test_preview_window_runs_lifecycle_and_opens_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Signal:
        def __init__(self) -> None:
            self.value = False

        def set(self) -> None:
            self.value = True

        def is_set(self) -> bool:
            return self.value

        def wait(self, _seconds: float) -> bool:
            return True

    sessions: list[object] = []

    class Session:
        def __init__(self, directory: Path) -> None:
            self.directory = directory
            self.url = "http://127.0.0.1:32000/"
            self.stop_requested = Signal()
            self.children: dict[str, object] = {}
            self.environment: dict[str, str] = {}
            self.stop_count = 0
            sessions.append(self)

        def start(self) -> None:
            pass

        def automation_tick(self) -> None:
            raise AssertionError("immediate shutdown must not run a scheduled job")

        def stop(self) -> None:
            self.stop_count += 1

    class Guard:
        def __init__(self, *_: object, **__: object) -> None:
            self.acquired = False
            self.released = False

        def acquire(self) -> None:
            self.acquired = True

        def release(self) -> None:
            self.released = True

    class SynchronousThread:
        def __init__(self, *, target: object, **_: object) -> None:
            self.target = cast(Any, target)

        def start(self) -> None:
            self.target()

        def join(self, timeout: float) -> None:
            assert timeout == 0

    class Variable:
        def __init__(self, value: str) -> None:
            self.value = value

        def set(self, value: str) -> None:
            self.value = value

    widgets: list[Any] = []

    class Widget:
        def __init__(self, *_: object, **kwargs: object) -> None:
            self.state = kwargs.get("state")
            self.text = kwargs.get("text")
            self.command = kwargs.get("command")
            widgets.append(self)

        def pack(self, **_: object) -> None:
            pass

        def configure(self, **kwargs: object) -> None:
            self.state = kwargs.get("state", self.state)

    class Window:
        def title(self, _: str) -> None:
            pass

        def geometry(self, _: str) -> None:
            pass

        def minsize(self, *_: object) -> None:
            pass

        def protocol(self, *_: object) -> None:
            pass

        def after(self, _milliseconds: int, callback: object) -> None:
            cast(Any, callback)()

        def mainloop(self) -> None:
            pass

        def destroy(self) -> None:
            pass

    tkinter = ModuleType("tkinter")
    tkinter.Tk = Window  # type: ignore[attr-defined]
    tkinter.StringVar = Variable  # type: ignore[attr-defined]
    tkinter.ttk = SimpleNamespace(  # type: ignore[attr-defined]
        Frame=Widget,
        Label=Widget,
        Entry=Widget,
        Button=Widget,
    )
    tkinter.messagebox = SimpleNamespace(  # type: ignore[attr-defined]
        showerror=lambda *_args, **_kwargs: None,
        showinfo=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)
    monkeypatch.setattr(desktop_main, "PreviewSession", Session)
    monkeypatch.setattr(desktop_main, "UnconfiguredSession", Session)
    monkeypatch.setattr(desktop_main, "AccountInstanceGuard", Guard)
    monkeypatch.setattr(desktop_main, "WindowsNamedMutexBackend", lambda: object())
    monkeypatch.setattr(desktop_main, "current_sid", lambda: "test-sid")
    monkeypatch.setattr(desktop_main, "Thread", SynchronousThread)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    opened: list[str] = []

    def open_console(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", open_console)

    assert desktop_main.launch_window() == 0
    assert opened == ["http://127.0.0.1:32000/"]
    assert cast(Any, sessions[0]).stop_count == 1

    class Completed:
        @staticmethod
        def is_set() -> bool:
            return True

    class SetupServer:
        url = "http://127.0.0.1:32100/"

        def __init__(self, path: Path) -> None:
            assert path == tmp_path / "AutoTradingSystem" / "launcher.json"
            self.completed = Completed()
            self.started = self.closed = False

        def start(self) -> None:
            self.started = True

        def open(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    servers: list[SetupServer] = []

    def setup_server(path: Path) -> SetupServer:
        server = SetupServer(path)
        servers.append(server)
        return server

    monkeypatch.setattr(desktop_main, "LauncherSetupServer", setup_server)
    assert desktop_main.launch_window(unconfigured=True) == 10
    assert cast(Any, sessions[1]).environment["ATS_DESKTOP_SETUP_URL"] == servers[0].url
    assert servers[0].started and servers[0].closed
    assert opened[-1] == "http://127.0.0.1:32000/"
    with pytest.raises(ValueError, match="同时提供"):
        desktop_main.launch_window(config_path=tmp_path / "node.json")
    with pytest.raises(ValueError, match="未配置模式"):
        desktop_main.launch_window(
            config_path=tmp_path / "node.json", sdk_root=tmp_path / "sdk", unconfigured=True
        )

    errors: list[str] = []
    tkinter.messagebox.showerror = lambda _title, message, **_kwargs: errors.append(message)
    monkeypatch.setattr(Signal, "wait", lambda self, seconds: False)
    monkeypatch.setattr(desktop_main, "trading_day_changed", lambda _: True)
    assert desktop_main.launch_window() == 11
    assert errors == []
    assert cast(Any, sessions[-1]).stop_count == 1

    destroyed: list[bool] = []
    failure_close: list[Any] = []
    monkeypatch.setattr(Window, "destroy", lambda self: destroyed.append(True))
    monkeypatch.setattr(
        Window, "protocol", lambda self, name, handler: failure_close.append(handler)
    )

    def fail_start(self: object) -> None:
        # Real PreviewSession.start cleans up and latches stop_requested before raising.
        cast(Any, self).stop_requested.set()
        raise RuntimeError("DESKTOP_START_TIMEOUT")

    monkeypatch.setattr(Session, "start", fail_start)
    assert desktop_main.launch_window() == 0
    assert len(errors) == 1 and "启动超时" in errors[0]
    assert destroyed == []  # Failed startup remains visible after cleanup.
    failure_close[-1]()
    assert destroyed == [True]  # Already stopped failure window can still close.
    assert "RuntimeError" not in errors[0]

    recovered: list[object] = []

    class LockedSession(Session):
        def __init__(self, *_: object) -> None:
            super().__init__(tmp_path / "locked-live")
            self.config = SimpleNamespace(broker="QMT_TRADING")

        def start(self) -> None:
            self.stop_requested.set()
            raise RuntimeError("SUPERVISOR_BLOCKED")

    monkeypatch.setattr(desktop_main, "ConfiguredSession", LockedSession)
    monkeypatch.setattr(desktop_main, "interrupted_start_available", lambda _: True)
    monkeypatch.setattr(
        desktop_main, "recover_interrupted_start", lambda config: recovered.append(config)
    )
    tkinter.messagebox.askyesno = lambda *_a, **_kw: False

    def recover_from_window(self: object) -> None:
        assert len(errors) == 1  # Eligible interruption uses inline recovery, no second modal.
        button = next(w for w in reversed(widgets) if w.text == "恢复启动并核验")
        assert button.state == "normal"
        button.command()
        assert recovered == []
        tkinter.messagebox.askyesno = lambda *_a, **_kw: True
        button.command()
        assert len(recovered) == 1

    monkeypatch.setattr(Window, "mainloop", recover_from_window)
    assert (
        desktop_main.launch_window(config_path=tmp_path / "node.json", sdk_root=tmp_path / "sdk")
        == 12
    )


@pytest.mark.parametrize("blocked_at", ["start", "tick"])
def test_window_cancel_and_confirm_exit_remain_responsive_with_blocked_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_at: str
) -> None:
    entered, release, stopped = Event(), Event(), Event()
    sessions, windows, buttons, threads, confirmations = [], [], [], [], []
    ui_thread = get_ident()

    class Session:
        def __init__(self, directory):
            self.directory, self.url = directory, "http://127.0.0.1:32000/"
            self.stop_requested, self.children = Event(), {}
            self.closed, self.stop_count = False, 0
            sessions.append(self)

        def start(self):
            if blocked_at == "start":
                entered.set()
                release.wait(8)

        def automation_tick(self):
            entered.set()
            release.wait(8)

        def stop(self):
            assert get_ident() != ui_thread
            if self.closed:
                return
            self.closed = True
            self.stop_count += 1
            stopped.set()

    class Widget:
        def __init__(self, *args, **kwargs):
            self.command, self.text = kwargs.get("command"), kwargs.get("text")
            buttons.append(self)

        def pack(self, **kwargs):
            pass

        def configure(self, **kwargs):
            pass

    class Variable:
        def __init__(self, value):
            self.value = value

        def set(self, value):
            self.value = value

    class Window:
        def __init__(self):
            self.callbacks, self.protocols, self.destroyed = [], {}, False
            windows.append(self)

        def title(self, value):
            pass

        def geometry(self, value):
            pass

        def minsize(self, *args):
            pass

        def protocol(self, key, value):
            self.protocols[key] = value

        def after(self, milliseconds, callback):
            self.callbacks.append(callback)

        def destroy(self):
            self.destroyed = True

        def mainloop(self):
            assert entered.wait(2)
            self.protocols["WM_DELETE_WINDOW"]()  # Cancel the title-bar close.
            assert not sessions[0].stop_requested.is_set() and not stopped.is_set()
            next(b for b in buttons if b.text == "停止并退出").command()
            assert sessions[0].stop_requested.is_set()
            until = monotonic() + 2
            while not self.destroyed and monotonic() < until:
                if self.callbacks:
                    self.callbacks.pop(0)()
                sleep(0.005)
            assert self.destroyed and stopped.is_set()
            assert not release.is_set()  # UI exited while the backend is still blocked.

    def ask(title, message, **kwargs):
        assert get_ident() == ui_thread and kwargs["default"] == "no"
        confirmations.append((title, message))
        return len(confirmations) == 2

    def spawn(**kwargs):
        thread = Thread(**kwargs)
        threads.append(thread)
        return thread

    tk = ModuleType("tkinter")
    tk.Tk, tk.StringVar = Window, Variable
    tk.ttk = SimpleNamespace(Frame=Widget, Label=Widget, Entry=Widget, Button=Widget)
    tk.messagebox = SimpleNamespace(askyesno=ask, showerror=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(desktop_main, "PreviewSession", Session)
    monkeypatch.setattr(desktop_main, "Thread", spawn)
    monkeypatch.setattr(desktop_main, "current_sid", lambda: "test-sid")
    monkeypatch.setattr(desktop_main, "WindowsNamedMutexBackend", lambda: object())
    monkeypatch.setattr(
        desktop_main,
        "AccountInstanceGuard",
        lambda *a, **k: SimpleNamespace(acquire=lambda: None, release=lambda: None),
    )
    monkeypatch.setattr(desktop_main, "trading_day_changed", lambda _: False)

    def blocked_browser(_):
        assert get_ident() != ui_thread
        release.wait(8)

    monkeypatch.setattr(webbrowser, "open", blocked_browser)
    try:
        assert desktop_main.launch_window() == 0
        assert confirmations == [("确认关闭", "后台运行中，是否确认关闭")] * 2
        assert sessions[0].stop_count == 1
    finally:
        release.set()
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()


def test_shutdown_wait_budget_is_shared_and_only_owned_jobs_are_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from auto_trading_system.apps.desktop import session as module

    session = PreviewSession(tmp_path / "bounded-stop")
    elapsed, calls = [0.0], []

    class Client:
        def invoke(self, action):
            calls.append(action)
            elapsed[0] += 2

    class Child:
        terminated, closed = False, False

        def poll(self):
            return 0 if self.terminated else None

        def wait(self, seconds):
            assert 0 <= seconds <= 3
            elapsed[0] += seconds
            if not self.terminated:
                raise TimeoutError()
            return 0

        def terminate(self):
            self.terminated = True

        def close(self):
            self.closed = True

    children = [Child(), Child(), Child()]
    session.children = dict(zip(("agent", "supervise", "console"), children, strict=True))
    monkeypatch.setattr(session, "client", lambda *a, **k: Client())
    monkeypatch.setattr(module, "monotonic", lambda: elapsed[0])
    session.stop()
    assert elapsed[0] <= 10

    assert all(child.closed and child.terminated for child in children)
    assert not session.children and session._stop_done.is_set()
    assert calls[0] == "HALT"
    session.stop()
    assert elapsed[0] <= 10


def test_shutdown_reclaims_a_child_that_finishes_starting_after_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from auto_trading_system.apps.desktop import session as module

    session = PreviewSession(tmp_path / "late-child")
    entered, release = Event(), Event()
    closed, failures = [], []

    def create(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(close=lambda: closed.append(True))

    monkeypatch.setattr(module, "OwnedProcess", create)

    def launch():
        try:
            session._launch("agent")
        except RuntimeError as error:
            failures.append(str(error))

    thread = Thread(target=launch)
    thread.start()
    try:
        assert entered.wait(2)
        session.stop()
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert closed == [True] and failures == ["DESKTOP_STOP_REQUESTED"]
    assert session.closed and not session.children


def test_first_run_window_opens_settings_and_returns_validated_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection = LauncherSettings(config_path=tmp_path / "node.json", sdk_root=tmp_path / "sdk")

    class Completed:
        @staticmethod
        def is_set() -> bool:
            return True

    class Server:
        def __init__(self, path: Path) -> None:
            assert path == tmp_path / "launcher.json"
            self.completed = Completed()
            self.selection = selection
            self.started = self.opened = self.closed = False

        def start(self) -> None:
            self.started = True

        def open(self) -> None:
            self.opened = True

        def close(self) -> None:
            self.closed = True

    servers: list[Server] = []

    def server_factory(path: Path) -> Server:
        server = Server(path)
        servers.append(server)
        return server

    class Guard:
        def __init__(self, *_: object, **__: object) -> None:
            self.acquired = self.released = False

        def acquire(self) -> None:
            self.acquired = True

        def release(self) -> None:
            self.released = True

    guards: list[Guard] = []

    def guard_factory(*args: object, **kwargs: object) -> Guard:
        guard = Guard(*args, **kwargs)
        guards.append(guard)
        return guard

    class Variable:
        def __init__(self, value: str) -> None:
            self.value = value

        def set(self, value: str) -> None:
            self.value = value

    class Widget:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def pack(self, **_: object) -> None:
            pass

    class Window:
        def title(self, _: str) -> None:
            pass

        def geometry(self, _: str) -> None:
            pass

        def minsize(self, *_: object) -> None:
            pass

        def protocol(self, *_: object) -> None:
            pass

        def after(self, _milliseconds: int, callback: object) -> None:
            cast(Any, callback)()

        def mainloop(self) -> None:
            pass

        def destroy(self) -> None:
            pass

    tkinter = ModuleType("tkinter")
    tkinter.Tk = Window  # type: ignore[attr-defined]
    tkinter.StringVar = Variable  # type: ignore[attr-defined]
    tkinter.ttk = SimpleNamespace(  # type: ignore[attr-defined]
        Frame=Widget, Label=Widget, Button=Widget
    )
    tkinter.messagebox = SimpleNamespace(  # type: ignore[attr-defined]
        showinfo=lambda *_args, **_kwargs: None
    )
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)
    monkeypatch.setattr(desktop_main, "LauncherSetupServer", server_factory)
    monkeypatch.setattr(desktop_main, "default_settings_path", lambda: tmp_path / "launcher.json")
    monkeypatch.setattr(desktop_main, "AccountInstanceGuard", guard_factory)
    monkeypatch.setattr(desktop_main, "WindowsNamedMutexBackend", lambda: object())
    monkeypatch.setattr(desktop_main, "current_sid", lambda: "test-sid")

    assert desktop_main.launch_setup_window() is selection
    assert servers[0].started and servers[0].opened and servers[0].closed
    assert guards[0].acquired and guards[0].released


def test_self_test_never_adopts_existing_directory(tmp_path: Path) -> None:
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    assert self_test(tmp_path) == 2
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not (tmp_path / "self-test.json").exists()


def test_cancel_before_launch_does_not_create_a_worker(tmp_path: Path) -> None:
    session = PreviewSession(tmp_path / "cancel")
    session.stop_requested.set()
    with pytest.raises(RuntimeError, match="STOP_REQUESTED"):
        session.start()
    assert session.closed and not session.children


def test_blocked_supervisor_reports_immediately_instead_of_waiting_for_start_timeout(
    tmp_path: Path,
) -> None:
    session = PreviewSession(tmp_path / "blocked")

    class Client:
        def invoke(self, action: str) -> dict[str, object]:
            assert action == "STATUS"
            return {"action": "BLOCKED", "blocked": True}

    cast(Any, session).client = lambda _: Client()
    with pytest.raises(RuntimeError, match="SUPERVISOR_BLOCKED"):
        session._supervisor_ready()


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("CHILD_EXITED", "QMT_READ_ONLY_START_FAILED"),
        ("CHILD_JOURNAL_UNHEALTHY", "SUPERVISOR_BLOCKED"),
    ],
)
def test_readonly_start_failure_explains_retry_without_unlocking_safety_faults(
    tmp_path: Path,
    reason: str,
    expected: str,
) -> None:
    session = PreviewSession(tmp_path / "readonly-retry")
    session.config = session.config.model_copy(update={"broker": "QMT_READ_ONLY"})

    class Client:
        def invoke(self, action: str) -> dict[str, object]:
            return {"action": "BLOCKED", "blocked": True, "reason": reason}

    cast(Any, session).client = lambda _: Client()
    with pytest.raises(RuntimeError, match=expected) as failure:
        session._supervisor_ready()
    message = desktop_main.failure_message(failure.value)
    if expected == "QMT_READ_ONLY_START_FAILED":
        assert "MiniQMT" in message and "重新打开" in message and "无需解锁" in message
    else:
        assert "已锁定" in message


def test_configured_desktop_runs_plan_pull_once_per_minute(tmp_path: Path) -> None:
    session = object.__new__(ConfiguredSession)
    session.config = preview_config(
        tmp_path / "unused", "scheduled-session", now=datetime(2026, 10, 19, tzinfo=CN)
    )
    session._last_automatic_job = None
    calls: list[tuple[str, object]] = []

    class Client:
        def invoke(self, action: str, payload: object) -> dict[str, str]:
            calls.append((action, payload))
            return {"status": "RUNNING"}

    cast(Any, session).client = lambda channel, role: Client()
    at = datetime(2026, 10, 19, 14, 40, 10, tzinfo=CN)
    session.automation_tick(at)
    session.automation_tick(at.replace(second=50))
    session.automation_tick(at.replace(minute=41))

    assert calls == [
        ("RUN_JOB", {"kind": "pull-plan"}),
        ("RUN_JOB", {"kind": "pull-plan"}),
    ]


def test_configured_desktop_runs_export_and_retries_failed_schedule(tmp_path: Path) -> None:
    session = object.__new__(ConfiguredSession)
    session.config = preview_config(
        tmp_path / "unused", "scheduled-export", now=datetime(2026, 10, 19, tzinfo=CN)
    )
    session._last_automatic_job = None
    calls: list[tuple[str, object]] = []
    fail = True

    class Client:
        def invoke(self, action: str, payload: object) -> dict[str, str]:
            nonlocal fail
            calls.append((action, payload))
            if fail:
                fail = False
                raise ValueError("temporary agent failure")
            return {"status": "RUNNING"}

    cast(Any, session).client = lambda channel, role: Client()
    session.automation_tick(datetime(2026, 10, 19, 14, 0, tzinfo=CN))
    session.automation_tick(datetime(2026, 10, 19, 15, 30, tzinfo=CN))
    session.automation_tick(datetime(2026, 10, 19, 15, 30, 30, tzinfo=CN))
    session.automation_tick(datetime(2026, 10, 19, 15, 30, 50, tzinfo=CN))

    assert calls == [
        ("RUN_JOB", {"kind": "daily-export"}),
        ("RUN_JOB", {"kind": "daily-export"}),
    ]


def test_actual_desktop_node_starts_and_closes_only_its_jobs(tmp_path: Path) -> None:
    session = PreviewSession(tmp_path / "desktop")
    try:
        observation = session.start()
        assert all(item["availability"] == "CURRENT" for item in observation["components"].values())
        status = session.worker_status()
        assert status["real_trading_enabled"] is False and status["order_count"] == 0
        assert status["halted"] is True
        assert load_node_config(session.path) == session.config
        body = session.path.read_text(encoding="utf-8")
        assert all(
            session.environment["ATS_SECRET_DESKTOP_" + role] not in body
            for role in ("AGENT", "JOBRUNNER", "OPERATOR", "HTTP")
        )
        verifier = session.environment["ATS_SECRET_DESKTOP_VERIFIER"]
        assert verifier not in body
        assert OperatorPasswordHasher().verify(DESKTOP_OPERATOR_PASSWORD, verifier)
        assert "ATS_NODE_SUPERVISION_ID" not in session.environment
        url = session.url
    finally:
        session.stop()
    assert session.stopped_cleanly and session.closed and not session.children
    with pytest.raises(httpx.TransportError), httpx.Client(trust_env=False, timeout=2) as client:
        client.get(url)
    session.stop()
