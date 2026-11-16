"""Keep the computer from idling to sleep while tagteam runs or waits for a reset."""

import os
import shutil
import subprocess
import sys
from contextlib import contextmanager

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


@contextmanager
def keep_awake(enabled: bool = True):
    if not enabled:
        yield
        return
    helper = None
    windows = False
    try:
        if os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
            windows = True
        elif sys.platform == "darwin" and shutil.which("caffeinate"):
            helper = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        elif shutil.which("systemd-inhibit"):
            helper = subprocess.Popen(
                ["systemd-inhibit", "--what=idle:sleep", "--who=tagteam",
                 "--why=Waiting for coding-agent usage limits to reset", "--mode=block",
                 "sleep", "infinity"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass  # best effort; tagteam still works, the machine just may sleep
    try:
        yield
    finally:
        if windows:
            try:
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
            except Exception:
                pass
        if helper is not None:
            try:
                helper.terminate()
            except OSError:
                pass
