"""One tagteam run per project, so two agents can never work in the same files at once."""

import json
import os
import socket
import time
from contextlib import contextmanager

from .textparse import fmt_time

LOCK_NAME = "run.lock"


class LockHeld(Exception):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a harmless probe on Windows, so ask the kernel instead.
        import ctypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied: it exists
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


pid_alive = _pid_alive


def _read(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


@contextmanager
def project_lock(state_dir: str):
    path = os.path.join(state_dir, LOCK_NAME)
    me = {"pid": os.getpid(), "host": socket.gethostname(), "started": time.time()}
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            other = _read(path)
            stale = (other is not None and other.get("host") == me["host"]
                     and not _pid_alive(int(other.get("pid") or -1)))
            if stale:
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            who = (f"pid {other.get('pid')} on {other.get('host')}, started {fmt_time(other.get('started'))}"
                   if other else "unknown process")
            raise LockHeld(f"Another tagteam run is already working in this project ({who}). Only one agent "
                           f"may work at a time, so this one will not start. If that run is gone, delete {path}")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(me, f)
        break
    else:
        raise LockHeld(f"Could not take the project lock {path}")
    try:
        yield
    finally:
        cur = _read(path)
        if cur and cur.get("pid") == me["pid"] and cur.get("host") == me["host"]:
            try:
                os.remove(path)
            except OSError:
                pass
