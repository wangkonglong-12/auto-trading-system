"""Local-only Windows named pipes with explicit ACL and cancellable I/O.

One persistent first-instance handle reserves each endpoint until shutdown.
Overlapped operations retain their buffers until completion, including timeout
cancellation. No pickle, sockets, remote pipes or unbounded client threads.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from contextlib import suppress
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32security

from auto_trading_system.control.ipc_protocol import MAX_FRAME, IPCError


def current_sid() -> str:
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        return str(win32security.ConvertSidToStringSid(sid))
    finally:
        token.Close()


def security_attributes(sid: str) -> Any:
    # Parse SID before placing it in SDDL; arbitrary ACE text cannot be injected.
    normalized = win32security.ConvertSidToStringSid(win32security.ConvertStringSidToSid(sid))
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.bInheritHandle = False
    attributes.SECURITY_DESCRIPTOR = (
        win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            f"D:P(A;;GA;;;SY)(A;;GA;;;{normalized})",
            win32security.SDDL_REVISION_1,
        )
    )
    return attributes


def _wait(handle: Any, overlap: Any, deadline: float, stop: Event) -> int:
    try:
        while not stop.is_set() and monotonic() < deadline:
            if win32event.WaitForSingleObject(overlap.hEvent, 25) == win32event.WAIT_OBJECT_0:
                return int(win32file.GetOverlappedResult(handle, overlap, False))
        raise IPCError("PIPE_TIMEOUT")
    except BaseException:
        # CancelIo operates on the same worker that issued this I/O. Drain the
        # cancellation before releasing OVERLAPPED or the Python read buffer.
        win32file.CancelIo(handle)
        with suppress(pywintypes.error):
            win32file.GetOverlappedResult(handle, overlap, True)
        raise


def _read(handle: Any, size: int, deadline: float, stop: Event) -> bytes:
    result = bytearray()
    while len(result) < size:
        overlap = pywintypes.OVERLAPPED()
        overlap.hEvent = win32event.CreateEvent(None, True, False, None)
        buffer = win32file.AllocateReadBuffer(size - len(result))
        try:
            code, _ = win32file.ReadFile(handle, buffer, overlap)
            count = (
                _wait(handle, overlap, deadline, stop)
                if code == 997
                else int(win32file.GetOverlappedResult(handle, overlap, False))
            )
            if not count:
                raise IPCError("PIPE_CLOSED")
            result.extend(buffer[:count])
        finally:
            overlap.hEvent.Close()
    return bytes(result)


def _write(handle: Any, body: bytes, deadline: float, stop: Event) -> None:
    if not 0 < len(body) <= MAX_FRAME:
        raise IPCError("FRAME_TOO_LARGE")
    frame = struct.pack(">I", len(body)) + body
    _raw_write(handle, frame, deadline, stop)


def _raw_write(handle: Any, frame: bytes, deadline: float, stop: Event) -> None:
    overlap = pywintypes.OVERLAPPED()
    overlap.hEvent = win32event.CreateEvent(None, True, False, None)
    try:
        code, _ = win32file.WriteFile(handle, frame, overlap)
        count = (
            _wait(handle, overlap, deadline, stop)
            if code == 997
            else int(win32file.GetOverlappedResult(handle, overlap, False))
        )
        if count != len(frame):
            raise IPCError("PIPE_SHORT_WRITE")
    finally:
        overlap.hEvent.Close()


def _frame(handle: Any, deadline: float, stop: Event) -> bytes:
    size = struct.unpack(">I", _read(handle, 4, deadline, stop))[0]
    if not 0 < size <= MAX_FRAME:
        raise IPCError("FRAME_TOO_LARGE")
    return _read(handle, size, deadline, stop)


def _peer_sid(handle: Any) -> str:
    win32security.ImpersonateNamedPipeClient(handle)
    try:
        token = win32security.OpenThreadToken(
            win32api.GetCurrentThread(),
            win32con.TOKEN_QUERY,
            True,
        )
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            return str(win32security.ConvertSidToStringSid(sid))
        finally:
            token.Close()
    finally:
        win32security.RevertToSelf()


def _validate_name(name: str) -> None:
    import re

    if not re.fullmatch(r"\\\\\.\\pipe\\AutoTrading\.v2\.[a-f0-9]{32}\.[a-z-]{1,24}", name):
        raise ValueError("invalid local pipe name")


class PipeServer:
    def __init__(self, name: str, sid: str, handler: Callable[[bytes], bytes]) -> None:
        _validate_name(name)
        if current_sid() != sid:
            raise IPCError("SERVICE_IDENTITY_MISMATCH")
        self._handler, self._sid = handler, sid
        self._stop = Event()
        self._handle = win32pipe.CreateNamedPipe(
            name,
            win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED | 0x80000,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT | 8,  # reject remote clients
            1,
            MAX_FRAME + 4,
            MAX_FRAME + 4,
            1000,
            security_attributes(sid),
        )
        self._thread = Thread(target=self._serve, name="local-pipe", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            overlap = pywintypes.OVERLAPPED()
            overlap.hEvent = win32event.CreateEvent(None, True, False, None)
            try:
                try:
                    code = win32pipe.ConnectNamedPipe(self._handle, overlap)
                    if code == 997:
                        _wait(self._handle, overlap, monotonic() + 1, self._stop)
                except pywintypes.error as exc:
                    if exc.winerror != 535:  # Client connected before ConnectNamedPipe.
                        raise
                deadline = monotonic() + 7
                # Shutdown stops new accepts, but drains an accepted bounded
                # reply (especially STOP) before closing the pipe handle.
                connection_stop = Event()
                body = _frame(self._handle, min(deadline, monotonic() + 2), connection_stop)
                if _peer_sid(self._handle) != self._sid:
                    raise IPCError("PEER_IDENTITY_MISMATCH")
                reply = self._handler(body)
                _write(self._handle, reply, deadline, connection_stop)
                # Client sends one byte after reading the reply, avoiding an
                # unbounded FlushFileBuffers and premature disconnect data loss.
                if _read(self._handle, 1, deadline, connection_stop) != b"\x01":
                    raise IPCError("PIPE_ACK_INVALID")
            except (IPCError, ValueError, OSError, pywintypes.error):
                pass  # Connection failure is isolated; no unauthenticated error body.
            finally:
                overlap.hEvent.Close()
                with suppress(pywintypes.error):
                    win32pipe.DisconnectNamedPipe(self._handle)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=8)
        if self._thread.is_alive():
            raise IPCError("PIPE_WORKER_STILL_ACTIVE")
        self._handle.Close()

    @property
    def healthy(self) -> bool:
        return self._thread.is_alive()


def exchange(name: str, body: bytes, *, timeout_seconds: float = 7) -> bytes:
    _validate_name(name)
    if not 0 < timeout_seconds <= 7:
        raise ValueError("pipe timeout must be positive and at most seven seconds")
    deadline, stop = monotonic() + timeout_seconds, Event()
    handle = None
    try:
        while handle is None:
            try:
                handle = win32file.CreateFile(
                    name,
                    win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                    0,
                    None,
                    win32con.OPEN_EXISTING,
                    win32file.FILE_FLAG_OVERLAPPED | 0x100000 | 0x10000,
                    None,
                )  # SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION; no impersonation rights.
            except pywintypes.error as exc:
                if exc.winerror not in (2, 231) or monotonic() >= deadline:
                    raise IPCError("PIPE_UNAVAILABLE") from None
                sleep(0.01)
        _write(handle, body, deadline, stop)
        reply = _frame(handle, deadline, stop)
        _raw_write(handle, b"\x01", deadline, stop)
        return reply
    except pywintypes.error:
        raise IPCError("PIPE_IO_FAILED") from None
    finally:
        if handle is not None:
            handle.Close()
