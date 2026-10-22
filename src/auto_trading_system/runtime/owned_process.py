"""Windows child ownership: suspended creation, Job assignment, then execution.

No PID discovery or attachment is provided. Closing the private Job handle also
terminates its children if the supervisor itself crashes. Handles are never
inherited, and an assignment failure is resolved before the child can execute.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import pywintypes
import win32api
import win32con
import win32event
import win32job
import win32process


class OwnedChild(Protocol):
    """A handle-owned child, never a process found by name or borrowed PID."""

    @property
    def process_id(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout_seconds: float) -> int: ...

    def close(self) -> None: ...

    def owns_process(self, process_id: int) -> bool: ...


class OwnedProcess:
    def __init__(self, command: Sequence[str], *, environment: Mapping[str, str]) -> None:
        if not command or not Path(command[0]).is_absolute() or not Path(command[0]).is_file():
            raise ValueError("child executable must be an existing absolute file")
        if any("\x00" in value for value in command):
            raise ValueError("child arguments must be NUL-free")
        win32api.SetLastError(0)
        self._job: Any = win32job.CreateJobObject(None, "AutoTrading.Owned." + uuid4().hex)
        if win32api.GetLastError() == 183:
            self._job.Close()
            self._job = None
            raise RuntimeError("owned job identity collision")
        self._process: Any = None
        self._pid = 0
        thread: Any = None
        try:
            limits = win32job.QueryInformationJobObject(
                self._job, win32job.JobObjectExtendedLimitInformation
            )
            limits["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                self._job, win32job.JobObjectExtendedLimitInformation, limits
            )
            self._process, thread, self._pid, _ = win32process.CreateProcess(
                command[0],
                subprocess.list2cmdline(command),
                None,
                None,
                False,
                win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW,
                dict(environment),
                None,
                win32process.STARTUPINFO(),
            )
            win32job.AssignProcessToJobObject(self._job, self._process)
            win32process.ResumeThread(thread)
        except BaseException:
            try:
                if self._process is not None:
                    win32process.TerminateProcess(self._process, 3)
            finally:
                self.close()
            raise
        finally:
            if thread is not None:
                thread.Close()

    @property
    def process_id(self) -> int:
        return int(self._pid)

    def poll(self) -> int | None:
        # Exit code 259 is a valid process exit code, not a liveness guarantee.
        result = win32event.WaitForSingleObject(self._process, 0)
        if result == win32event.WAIT_TIMEOUT:
            return None
        if result != win32event.WAIT_OBJECT_0:
            raise RuntimeError("owned child poll failed")
        return int(win32process.GetExitCodeProcess(self._process))

    def owns_process(self, process_id: int) -> bool:
        # A Windows venv python.exe may be a launcher for the real interpreter.
        # Authenticate the reply AND require Job membership; never adopt its PID.
        if type(process_id) is not int or process_id < 1:
            return False
        try:
            handle = win32api.OpenProcess(0x1000, False, process_id)
        except (OSError, pywintypes.error):
            return False
        try:
            return bool(win32job.IsProcessInJob(handle, self._job))
        finally:
            handle.Close()

    def terminate(self) -> None:
        win32job.TerminateJobObject(self._job, 3)

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout_seconds: float) -> int:
        result = win32event.WaitForSingleObject(self._process, int(timeout_seconds * 1000))
        if result == win32event.WAIT_TIMEOUT:
            raise TimeoutError("owned child did not exit")
        code = self.poll()
        if code is None:
            raise RuntimeError("owned child wait failed")
        return code

    def close(self) -> None:
        if self._job is not None:
            self._job.Close()
            self._job = None
        if self._process is not None:
            self._process.Close()
            self._process = None


def child_environment(supervision_id: str) -> dict[str, str]:
    environment = dict(os.environ)
    environment["ATS_NODE_SUPERVISION_ID"] = supervision_id
    return environment
