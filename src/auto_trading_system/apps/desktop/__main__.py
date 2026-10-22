"""Windows 桌面启动入口，兼容冻结后的独立节点和监督子进程分派。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import webbrowser
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import date, datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any
from uuid import uuid4

from auto_trading_system.apps.desktop.session import (
    DESKTOP_OPERATOR_PASSWORD,
    ConfiguredSession,
    PreviewSession,
    UnconfiguredSession,
)
from auto_trading_system.apps.desktop.setup import (
    LauncherSettings,
    LauncherSetupServer,
    configured_from_environment,
    default_settings_path,
    read_launcher_settings,
)
from auto_trading_system.control.windows_pipe import current_sid
from auto_trading_system.runtime.engine_v2 import CN
from auto_trading_system.runtime.single_instance import (
    AccountInstanceGuard,
    WindowsNamedMutexBackend,
)
from auto_trading_system.version import __version__


def trading_day_changed(started_on: date, *, now: datetime | None = None) -> bool:
    return (now or datetime.now(CN)).astimezone(CN).date() != started_on


def failure_message(exc: Exception) -> str:
    messages = {
        "DESKTOP_CHILD_EXITED": "本机后台进程已退出，请重新启动系统。",
        "DESKTOP_START_TIMEOUT": "等待后台启动超时，请检查QMT是否已启动并登录。",
        "QMT_DISCOVERY_CONNECT_FAILED": "无法连接MiniQMT，请先启动并登录QMT交易端。",
        "QMT_ACCOUNT_BINDING_UNAVAILABLE": "QMT未返回已绑定的股票账户，请检查交易端登录账户。",
        "CHILD_IDENTITY_MISMATCH": "后台进程身份核验失败，系统已停止自动交易。",
        "CHILD_JOURNAL_UNHEALTHY": "本机交易记录存储异常，系统已停止自动交易。",
        "DISK_SPACE_LOW": "数据盘可用空间不足，系统已停止。",
        "SUPERVISOR_BLOCKED": "后台连续异常后已锁定，需要检查运行记录。",
    }
    return messages.get(str(exc), "本机后台运行异常，系统已停止。请查看运行记录中的原因码。")


def worker_arguments(arguments: Sequence[str]) -> list[str] | None:
    """既有 supervisor 的 -m 调用也交给同一 EXE 执行，避免再次打开窗口。"""
    if list(arguments[:1]) == ["--node"]:
        return list(arguments[1:])
    if list(arguments[:2]) == ["-m", "auto_trading_system.apps.node_process"]:
        return list(arguments[2:])
    return None


def run_worker(arguments: list[str]) -> int:
    with standard_streams(), ExitStack() as stack:
        sdk_value = os.environ.get("ATS_QMT_SDK_ROOT")
        if sdk_value:
            sdk_root = Path(sdk_value)
            if (
                not sdk_root.is_absolute()
                or sdk_root.resolve() != sdk_root.absolute()
                or not (sdk_root / "xtquant/xttrader.py").is_file()
            ):
                return 2
            sys.path.insert(0, str(sdk_root))
        directory = os.environ.get("ATS_DESKTOP_LOG_DIRECTORY")
        if directory:
            # 只写当前节点受保护目录；不使用全局日志，不记录环境秘密。
            path = Path(directory)
            if path.resolve() != path.absolute() or not path.is_dir():
                return 2
            log = stack.enter_context(
                (path / f"desktop-worker-{os.getpid()}.log").open("a", encoding="utf-8")
            )
            stack.enter_context(redirect_stdout(log))
            stack.enter_context(redirect_stderr(log))
        from auto_trading_system.apps.node_process.__main__ import main as node_main

        return node_main(arguments)


@contextmanager
def standard_streams() -> Iterator[None]:
    """窗口版 EXE 没有控制台；给 CLI/日志库提供临时空流并原样恢复。"""
    originals = {name: getattr(sys, name) for name in ("stdin", "stdout", "stderr")}
    with ExitStack() as stack:
        try:
            for name, original in originals.items():
                if original is None:
                    setattr(
                        sys,
                        name,
                        stack.enter_context(open(os.devnull, "r" if name == "stdin" else "w")),
                    )
            yield
        finally:
            for name, original in originals.items():
                setattr(sys, name, original)


def self_test(directory: Path) -> int:
    """验证真实冻结程序的 GUI 依赖、三进程、页面及退出；不发送任何交易。"""
    import tkinter as tk

    if directory.exists():
        return 2  # 验收目录必须是全新的，不能向调用者原有目录追加报告。
    session = PreviewSession(directory)
    report: dict[str, Any] = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False))}
    children = []
    setup_server: LauncherSetupServer | None = None
    try:
        window = tk.Tk()
        window.withdraw()
        window.update_idletasks()
        report["tk_version"] = window.tk.call("info", "patchlevel")
        window.destroy()
        observed = session.start()
        status = session.worker_status()
        assert status["real_trading_enabled"] is False and status["broker"] == "MOCK"
        assert status["halted"] is True and status["order_count"] == 0
        limits = status["installed_limits"]
        assert isinstance(limits, dict) and limits["max_buy_value"] == "0"
        import httpx

        with httpx.Client(trust_env=False, timeout=5) as client:
            page = client.get(session.url)
            assert page.status_code == 200 and "node-" in page.text
            import re

            assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', page.text)
            assert assets and all(
                client.get(session.url.rstrip("/") + item).status_code == 200 for item in assets
            )
            origin = session.url.rstrip("/")
            login = client.post(
                session.url + "api/v2/node/session",
                json={"password": DESKTOP_OPERATOR_PASSWORD},
                headers={"origin": origin, "sec-fetch-site": "same-origin"},
            )
            assert login.status_code == 200
            settings = client.get(session.url + "api/v2/node/trading-settings")
            assert settings.status_code == 200 and settings.json()["draft"] is None
            setup_server = LauncherSetupServer(directory / "launcher.json")
            setup_server.start()
            setup_page = client.get(setup_server.url)
            assert setup_page.status_code == 200
            assert "系统设置" in setup_page.text and "没有启动交易引擎" in setup_page.text
        report.update(
            {
                "status": "PASS",
                "components": {
                    name: item["availability"] for name, item in observed["components"].items()
                },
                "assets": len(assets),
                "real_trading_enabled": False,
                "order_count": 0,
                "operator_access_configured": True,
                "first_run_settings_page": True,
            }
        )
        children = list(session.children.values())
    except Exception as exc:
        report.update({"status": "FAIL", "error_type": type(exc).__name__})
    finally:
        if setup_server is not None:
            setup_server.close()
        session.stop()
        # Job 关闭即回收进程，不根据其他程序的名称或 PID 推断退出。
        report["owned_jobs_closed"] = not session.children and session.stopped_cleanly
        report["owned_job_count"] = len(children)
        if not report["owned_jobs_closed"]:
            report["status"] = "FAIL"
        if session.directory.is_dir():
            with (session.directory / "self-test.json").open("x", encoding="utf-8") as output:
                json.dump(report, output, ensure_ascii=False, indent=2)
    return 0 if report.get("status") == "PASS" else 1


def launch_window(
    *,
    config_path: Path | None = None,
    sdk_root: Path | None = None,
    unconfigured: bool = False,
) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    live = config_path is not None or sdk_root is not None
    if unconfigured and live:
        raise ValueError("未配置模式不能同时提供实盘配置")
    if live and (config_path is None or sdk_root is None):
        raise ValueError("实盘启动需要同时提供节点配置和QMT SDK目录")
    if live:
        assert config_path is not None and sdk_root is not None
        session: PreviewSession = ConfiguredSession(config_path, sdk_root)
    elif unconfigured:
        base = Path(os.environ["LOCALAPPDATA"]) / "AutoTradingSystem" / "Unconfigured"
        base.mkdir(parents=True, exist_ok=True)
        session = UnconfiguredSession(base / ("session-" + uuid4().hex))
    else:
        base = Path(os.environ["LOCALAPPDATA"]) / "AutoTradingSystem" / "DesktopPreview"
        base.mkdir(parents=True, exist_ok=True)
        session = PreviewSession(base / ("session-" + uuid4().hex))
    setup_server = LauncherSetupServer(default_settings_path()) if unconfigured else None
    guard = AccountInstanceGuard(
        hashlib.sha256(("desktop:" + current_sid()).encode()).hexdigest(),
        backend=WindowsNamedMutexBackend(),
    )
    window = tk.Tk()
    qmt_trading = live and session.config.broker == "QMT_TRADING"
    qmt_connected = live and session.config.broker == "QMT_READ_ONLY"
    window.title(
        "自动交易系统 · QMT实盘"
        if qmt_trading
        else "自动交易系统 · QMT已连接"
        if qmt_connected
        else "自动交易系统 · 尚未连接QMT"
        if unconfigured
        else "自动交易系统 · 离线模拟版"
    )
    window.geometry("660x340")
    window.minsize(600, 320)
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="自动交易系统", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
    ttk.Label(
        frame,
        text=(
            f"{__version__}  ·  QMT实盘"
            if qmt_trading
            else f"{__version__}  ·  QMT已连接 - 只读核验"
            if qmt_connected
            else f"{__version__}  ·  尚未连接QMT"
            if unconfigured
            else f"{__version__}  ·  离线模拟版"
        ),
    ).pack(anchor="w", pady=(8, 12))
    ttk.Label(
        frame,
        text=(
            "本程序启动QMT交易节点和本机操作台。交易仍需在页面登录并明确启动。"
            if qmt_trading
            else "MiniQMT真实账户已经连接。本阶段只读取账户，不会发送或撤销委托。"
            if qmt_connected
            else (
                "系统可以正常打开，自动交易保持锁定。\n请在系统设置中完成本机 QMT 接入。"
                if unconfigured
                else f"本程序启动完整模拟交易节点。\n页面登录口令: {DESKTOP_OPERATOR_PASSWORD}"
            )
        ),
        justify="left",
    ).pack(anchor="w")
    state = tk.StringVar(value="正在启动引擎、监护及同步代理…")
    ttk.Label(frame, textvariable=state).pack(anchor="w", pady=(16, 8))
    address = tk.StringVar(value="启动后显示本机访问地址")
    ttk.Entry(frame, textvariable=address, state="readonly").pack(fill="x")
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(18, 0))
    open_button = ttk.Button(
        buttons, text="打开操作台", state="disabled", command=lambda: webbrowser.open(session.url)
    )
    open_button.pack(side="left")
    data_button = ttk.Button(
        buttons,
        text="打开本次数据目录",
        state="disabled",
        command=lambda: os.startfile(session.directory),
    )
    data_button.pack(side="left", padx=8)
    if setup_server is not None:
        setup_button = ttk.Button(buttons, text="连接QMT", command=setup_server.open)
        setup_button.pack(side="left", padx=8)
    events: Queue[tuple[str, str]] = Queue(maxsize=4)
    rolled_over = False

    def close() -> None:
        state.set("正在暂停并停止交易节点…")
        session.stop_requested.set()
        open_button.configure(state="disabled")
        exit_button.configure(state="disabled")

    exit_button = ttk.Button(buttons, text="停止并退出", command=close)
    exit_button.pack(side="right")
    window.protocol("WM_DELETE_WINDOW", close)

    def work() -> None:
        nonlocal rolled_over
        started_on = datetime.now(CN).date()
        try:
            session.start()
            events.put(("ready", session.url))
            while not session.stop_requested.wait(1):
                if setup_server is not None and setup_server.completed.is_set():
                    events.put(("configured", ""))
                    session.stop_requested.set()
                    break
                if trading_day_changed(started_on):
                    rolled_over = True
                    events.put(("rollover", ""))
                    break
                session.automation_tick()
                if any(child.poll() is not None for child in session.children.values()):
                    raise RuntimeError("DESKTOP_CHILD_EXITED")
        except Exception as exc:
            if str(exc) != "DESKTOP_STOP_REQUESTED":
                # Only fixed codes are persisted; arbitrary exception text may
                # contain account data or submitted credentials.
                code = str(exc)
                safe_code = (
                    code
                    if code in {"DESKTOP_CHILD_EXITED", "DESKTOP_START_TIMEOUT"}
                    else type(exc).__name__
                )
                try:
                    log = session.directory / "logs" / "desktop-lifecycle.log"
                    with log.open("a", encoding="utf-8") as output:
                        output.write(f"{datetime.now(CN).isoformat()} {safe_code}\n")
                except OSError:
                    pass
                events.put(("error", failure_message(exc)))
        finally:
            session.stop()
            events.put(("closed", ""))

    def observe() -> None:
        try:
            kind, detail = events.get_nowait()
        except Empty:
            window.after(150, observe)
            return
        if kind == "closed":
            window.destroy()
            return
        if kind == "configured":
            state.set("QMT连接配置已保存，正在切换到实盘节点…")
            open_button.configure(state="disabled")
            exit_button.configure(state="disabled")
            window.after(150, observe)
            return
        if kind == "rollover":
            state.set("交易日已切换，正在重新连接账户；自动交易保持暂停…")
            open_button.configure(state="disabled")
            window.after(150, observe)
            return
        if kind == "error":
            messagebox.showerror(
                "启动未完成",
                f"{detail}\n运行记录保留在: {session.directory / 'logs'}",
                parent=window,
            )
        elif not session.stop_requested.is_set():
            state.set(
                "QMT实盘节点已启动；请登录后检查设置并手动启动。"
                if qmt_trading
                else "QMT真实账户已连接；当前为只读核验，不会发送委托。"
                if qmt_connected
                else "系统已打开；QMT连接完成前，自动交易保持锁定。"
                if unconfigured
                else "模拟节点已启动。"
            )
            address.set(detail)
            open_button.configure(state="normal")
            data_button.configure(state="normal")
            webbrowser.open(detail)
        window.after(150, observe)

    try:
        guard.acquire()
    except RuntimeError:
        messagebox.showinfo("自动交易系统", "启动器已经打开，请查看现有窗口。", parent=window)
        window.destroy()
        return 2
    try:
        if setup_server is not None:
            setup_server.start()
            session.environment["ATS_DESKTOP_SETUP_URL"] = setup_server.url
        thread = Thread(target=work, name="desktop-lifecycle", daemon=True)
        thread.start()
        window.after(150, observe)
        window.mainloop()
        session.stop_requested.set()
        thread.join(timeout=20)
        if setup_server is not None and setup_server.completed.is_set():
            return 10
        return 11 if rolled_over else 0
    finally:
        if setup_server is not None:
            setup_server.close()
        guard.release()


def launch_setup_window() -> LauncherSettings | None:
    """Open the safe first-run settings page without starting any trading component."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    server = LauncherSetupServer(default_settings_path())
    guard = AccountInstanceGuard(
        hashlib.sha256(("desktop:" + current_sid()).encode()).hexdigest(),
        backend=WindowsNamedMutexBackend(),
    )
    window = tk.Tk()
    window.title("自动交易系统 · 系统设置")
    window.geometry("660x300")
    window.minsize(600, 280)
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="自动交易系统", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
    ttk.Label(frame, text=f"{__version__}  ·  首次设置").pack(anchor="w", pady=(8, 12))
    ttk.Label(
        frame,
        text="尚未完成 QMT 实盘接入。交易引擎没有启动，所有自动交易功能保持锁定。",
        justify="left",
    ).pack(anchor="w")
    state = tk.StringVar(value="正在打开系统设置页面…")
    ttk.Label(frame, textvariable=state).pack(anchor="w", pady=(16, 8))
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(18, 0))
    open_button = ttk.Button(buttons, text="打开系统设置", command=server.open)
    open_button.pack(side="left")
    closed = False

    def close() -> None:
        nonlocal closed
        closed = True
        window.destroy()

    ttk.Button(buttons, text="退出", command=close).pack(side="right")
    window.protocol("WM_DELETE_WINDOW", close)

    def observe() -> None:
        if server.completed.is_set():
            state.set("配置检查通过，正在启动 QMT 实盘节点…")
            window.after(150, window.destroy)
            return
        window.after(150, observe)

    try:
        guard.acquire()
    except RuntimeError:
        messagebox.showinfo("自动交易系统", "启动器已经打开，请查看现有窗口。", parent=window)
        window.destroy()
        return None
    try:
        server.start()
        server.open()
        window.after(150, observe)
        window.mainloop()
        return None if closed else server.selection
    finally:
        server.close()
        guard.release()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    worker = worker_arguments(arguments)
    if worker is not None:
        return run_worker(worker)
    parser = argparse.ArgumentParser(description="自动交易系统 Windows 启动器")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--self-test", type=Path, help="在全新专属目录执行打包验收")
    parser.add_argument("--config", type=Path, help="已初始化的QMT实盘节点配置")
    parser.add_argument("--sdk-root", type=Path, help="包含xtquant目录的QMT SDK目录")
    parser.add_argument("--preview", action="store_true", help="明确启动离线模拟预览")
    parser.add_argument("--configure", action="store_true", help="打开QMT首次设置页面")
    args = parser.parse_args(arguments)
    if args.self_test:
        return self_test(args.self_test)
    if args.preview:
        while True:
            result = launch_window()
            if result != 11:
                return result
    explicit = (
        LauncherSettings(args.config, args.sdk_root)
        if args.config is not None and args.sdk_root is not None
        else None
    )
    configured = (
        explicit or configured_from_environment() or read_launcher_settings(default_settings_path())
    )
    if args.configure:
        configured = launch_setup_window()
    elif configured is None:
        result = launch_window(unconfigured=True)
        while result == 11:
            result = launch_window(unconfigured=True)
        if result != 10:
            return result
        configured = read_launcher_settings(default_settings_path())
    if configured is None:
        return 0
    configured.validate()
    while True:
        result = launch_window(config_path=configured.config_path, sdk_root=configured.sdk_root)
        if result != 11:
            return result


if __name__ == "__main__":
    raise SystemExit(main())
