"""Run one agent process: prompt on stdin, stdout streamed to a log, timeout, kill the whole tree."""

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class RunOutput:
    returncode: Optional[int]
    lines: List[str]
    stderr: str
    timed_out: bool
    started_at: float


def kill_tree(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        try:
            p.kill()
        except OSError:
            pass


CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_SUSPENDED = 0x00000004

# When tagteam dies, even by a force kill that no code in it can catch (taskkill /F, kill -9, the
# OOM killer), the agent it started must die too; otherwise the next run starts a second agent next
# to the orphan. Windows: every agent runs in a job object that the kernel kills when tagteam's
# handle to it closes, which happens however tagteam ends. Elsewhere: a tiny watchdog process waits
# on a pipe that only tagteam holds; end of file without a goodbye means tagteam is gone, and the
# watchdog kills the agent's process group.
_job_lock = threading.Lock()
_job = None  # None: not made yet; 0: unavailable on this system


def _kernel32():
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def _make_job():
    try:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_ulonglong * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k = _kernel32()
        job = k.CreateJobObjectW(None, None)
        if not job:
            return None
        info = Extended()
        # KILL_ON_JOB_CLOSE only. No BREAKAWAY_OK: Git Bash (MSYS/Cygwin) starts every program outside the
        # job whenever the job allows it, and those would outlive tagteam.
        info.BasicLimitInformation.LimitFlags = 0x2000
        if not k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # extended limits
            k.CloseHandle(job)
            return None
        return job
    except (OSError, AttributeError, ValueError):
        return None


def _agent_job():
    """The kill-on-close job agents run in, made once per tagteam process; None where unavailable."""
    global _job
    with _job_lock:
        if _job is None:
            _job = _make_job() or 0
        return _job or None


def _resume(handle) -> bool:
    try:
        import ctypes

        nt = ctypes.WinDLL("ntdll")
        nt.NtResumeProcess.restype = ctypes.c_long
        nt.NtResumeProcess.argtypes = [ctypes.c_void_p]
        return nt.NtResumeProcess(handle) == 0
    except (OSError, AttributeError):
        return False


def _popen_in_job(cmd: List[str], flags: int, **kw) -> subprocess.Popen:
    """Windows: start the agent suspended, put it in the job, then let it run, so nothing it starts can
    slip out of the job first. Any step refused (an old Windows, a locked-down job): a plain start."""
    job = _agent_job()
    if job is None:
        return subprocess.Popen(cmd, creationflags=flags, **kw)
    p = subprocess.Popen(cmd, creationflags=flags | CREATE_SUSPENDED, **kw)
    resumed = False
    try:
        k = _kernel32()
        h = k.OpenProcess(0x0100 | 0x0001 | 0x0800, False, p.pid)  # SET_QUOTA | TERMINATE | SUSPEND_RESUME
        if h:
            try:
                k.AssignProcessToJobObject(job, h)
                resumed = _resume(h)
            finally:
                k.CloseHandle(h)
    except (OSError, AttributeError):
        pass
    if resumed:
        return p
    p.kill()  # never leave it suspended; start it the old way
    p.wait()
    for f in (p.stdin, p.stdout, p.stderr):
        if f:
            f.close()
    return subprocess.Popen(cmd, creationflags=flags, **kw)


_WATCHDOG = """
import os, signal, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
fd, group = int(sys.argv[1]), int(sys.argv[2])
while True:
    try:
        b = os.read(fd, 1)
    except OSError:
        b = b""
    if b == b"d":
        sys.exit(0)
    if not b:
        break
try:
    os.killpg(group, signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.1)
        os.killpg(group, 0)
    os.killpg(group, signal.SIGKILL)
except OSError:
    pass
"""


def _start_watchdog(group: int):
    """POSIX: a process that kills the agent's process group if tagteam dies without saying goodbye.
    Returns (watchdog, write end of its pipe), or (None, None) if it could not start."""
    try:
        r, w = os.pipe()
    except OSError:
        return None, None
    try:
        wd = subprocess.Popen([python_exe(), "-I", "-c", _WATCHDOG, str(r), str(group)], pass_fds=(r,),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
    except OSError:
        os.close(w)
        return None, None
    finally:
        os.close(r)
    return wd, w


def _stop_watchdog(wd, w) -> None:
    if w is None:
        return
    try:
        os.write(w, b"d")  # goodbye: the agent run is over, stand down
    except OSError:
        pass
    try:
        os.close(w)
    except OSError:
        pass
    try:
        wd.wait(5)
    except subprocess.TimeoutExpired:
        wd.kill()
        wd.wait()


def python_exe() -> str:
    """The console Python (not pythonw), so child processes share one hidden console on Windows."""
    exe = sys.executable
    if os.name == "nt" and os.path.basename(exe).lower() == "pythonw.exe":
        alt = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.isfile(alt):
            return alt
    return exe


def module_env() -> dict:
    """Environment in which `python -m tagteam` finds this copy of tagteam, installed or not."""
    env = dict(os.environ)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def spawn_detached(argv: List[str], log_path: str, cwd: Optional[str] = None,
                   env: Optional[dict] = None) -> subprocess.Popen:
    """Start a process that keeps running after the terminal, window or chat that started it closes."""
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    log = open(log_path, "ab")
    try:
        kw = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        if os.name != "nt":
            return subprocess.Popen(argv, start_new_session=True, **kw)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:  # leave the job of the terminal or IDE that started us, so closing it does not kill us
            return subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
        except OSError:  # that job does not allow breaking away
            return subprocess.Popen(argv, creationflags=flags, **kw)
    finally:
        log.close()


def run(cmd: List[str], cwd: str, stdin_text: Optional[str], timeout_s: float,
        log_path: str, env: Optional[dict] = None, stop=None) -> RunOutput:
    """stop: optional callable; when it returns True the agent is killed and KeyboardInterrupt raised."""
    started = time.time()
    lines: List[str] = []
    err_path = log_path + ".stderr"
    timed_out = False
    with open(log_path, "wb") as log, open(err_path, "wb") as err:
        kw = dict(cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err)
        wd = w = None
        if os.name == "nt":
            p = _popen_in_job(cmd, subprocess.CREATE_NEW_PROCESS_GROUP, **kw)  # our Ctrl+C is not forwarded
        else:
            p = subprocess.Popen(cmd, start_new_session=True, **kw)  # own process group: the tree can be killed
            wd, w = _start_watchdog(p.pid)
        try:
            def feed():
                try:
                    if stdin_text is not None:
                        p.stdin.write(stdin_text.encode("utf-8"))
                except OSError:
                    pass
                finally:
                    try:
                        p.stdin.close()
                    except OSError:
                        pass

            def read():
                for raw in iter(p.stdout.readline, b""):
                    log.write(raw)
                    log.flush()
                    lines.append(raw.decode("utf-8", "replace"))

            threading.Thread(target=feed, daemon=True).start()
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            try:
                # Poll instead of wait(): wait() on Windows does not let Ctrl+C through.
                while p.poll() is None:
                    if timeout_s and time.time() - started > timeout_s:
                        timed_out = True
                        kill_tree(p)
                        break
                    if stop is not None and stop():
                        raise KeyboardInterrupt
                    time.sleep(0.2)
                p.wait()
            except BaseException:  # Ctrl+C, a stop request, a signal or any error: never leave the agent running
                kill_tree(p)
                raise
            reader.join(5)
            if not reader.is_alive():
                p.stdout.close()
        finally:
            _stop_watchdog(wd, w)
    try:
        with open(err_path, "rb") as f:
            stderr = f.read().decode("utf-8", "replace")
    except OSError:
        stderr = ""
    return RunOutput(p.returncode, lines, stderr, timed_out, started)
