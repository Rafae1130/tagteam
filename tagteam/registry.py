"""Which tagteam runs are active on this computer, for the pet.

Every handover, wakeup and discussion writes one small file under ~/.tagteam/runs (or
$TAGTEAM_HOME/runs) while it works and removes it when it ends. A run that was killed leaves its
file behind; readers skip files whose process is gone and remove them.
"""

import glob
import json
import os
import socket
import time
from contextlib import contextmanager
from typing import List, Optional

from .lock import pid_alive

KINDS = ("discussion", "handover", "wakeup")


def home() -> str:
    return os.environ.get("TAGTEAM_HOME") or os.path.join(os.path.expanduser("~"), ".tagteam")


def runs_dir() -> str:
    return os.path.join(home(), "runs")


def _read(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


@contextmanager
def registered(kind: str, root: str, title: str = ""):
    """Mark this process as an active run of `kind` in `root` for as long as the block runs.

    Never raises: if the file can't be written, the run still works; the pet just doesn't see it."""
    path = os.path.join(runs_dir(), f"{socket.gethostname()}-{os.getpid()}.json")
    entry = {"kind": kind, "root": os.path.abspath(root), "title": title, "pid": os.getpid(),
             "host": socket.gethostname(), "started": time.time()}
    try:
        os.makedirs(runs_dir(), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f)
        os.replace(tmp, path)
    except OSError:
        path = None
    try:
        yield
    finally:
        if path:
            _remove(path)


def active() -> List[dict]:
    """The runs that are active on this computer, oldest first. Leftovers of killed runs are removed."""
    host, out = socket.gethostname(), []
    for path in glob.glob(os.path.join(glob.escape(runs_dir()), "*.json")):
        e = _read(path)
        if not e or e.get("host") != host:
            continue
        if not pid_alive(int(e.get("pid") or -1)):
            _remove(path)
            continue
        out.append(e)
    return sorted(out, key=lambda e: e.get("started") or 0)
