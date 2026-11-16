"""The few git operations tagteam needs."""

import hashlib
import os
import shutil
import subprocess
import time


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def available() -> bool:
    return shutil.which("git") is not None


def is_repo(root) -> bool:
    r = _git(root, "rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def init(root) -> bool:
    return _git(root, "init").returncode == 0


def exclude(root, pattern=".tagteam/") -> None:
    """Keep tagteam's own state out of commits without touching the user's .gitignore."""
    r = _git(root, "rev-parse", "--git-path", "info/exclude")
    if r.returncode != 0:
        return
    path = r.stdout.strip()
    if not os.path.isabs(path):
        path = os.path.join(root, path)
    try:
        existing = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                existing = f.read()
        if pattern not in existing.split():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(("" if existing.endswith("\n") or not existing else "\n") + pattern + "\n")
    except OSError:
        pass


def head(root):
    """Short hash of the current commit, or None (no repo, or no commits yet)."""
    r = _git(root, "rev-parse", "--short", "HEAD")
    return r.stdout.strip() or None if r.returncode == 0 else None


def _identity(root):
    if _git(root, "config", "user.email").stdout.strip():
        return []
    return ["-c", "user.name=tagteam", "-c", "user.email=tagteam@localhost"]


SKIP_DIRS = (".git", ".tagteam")
WALK_MAX_FILES = 100000
WALK_MAX_SECONDS = 10.0


def fingerprint(root):
    """A value that changes whenever the work in this folder changes, or None when unknown.

    Used to tell whether a turn did anything, with or without commits. In a git repo it is the
    current commit plus the size and modification time of every changed or untracked file (files
    that .gitignore leaves out do not count). Without git it covers every file in the folder; a
    folder too big to walk quickly gives None, which counts as changed."""
    h = hashlib.sha1()
    if available() and is_repo(root):
        top = _git(root, "rev-parse", "--show-toplevel").stdout.strip()
        r = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".")
        if top and r.returncode == 0:
            h.update((head(root) or "").encode())
            for token in r.stdout.split("\0"):
                if not token or ".tagteam/" in token:
                    continue
                rel = token[3:] if len(token) > 3 and token[2] == " " else token
                try:
                    st = os.stat(os.path.join(top, rel))
                    h.update(f"{token}\0{st.st_size}\0{st.st_mtime_ns}\n".encode("utf-8", "surrogateescape"))
                except OSError:
                    h.update(f"{token}\0gone\n".encode("utf-8", "surrogateescape"))
            return h.hexdigest()
    start, count = time.time(), 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            h.update(f"{os.path.relpath(p, root)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode("utf-8", "surrogateescape"))
            count += 1
            if count > WALK_MAX_FILES or time.time() - start > WALK_MAX_SECONDS:
                return None
    return h.hexdigest()


def commit_all(root, message) -> bool:
    """Stage everything and commit. Returns True if a commit was made."""
    _git(root, "add", "-A")
    if _git(root, "diff", "--cached", "--quiet").returncode == 0:
        return False
    return _git(root, *_identity(root), "commit", "-q", "-m", message).returncode == 0
