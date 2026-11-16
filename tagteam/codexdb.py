"""Codex's own records, read-only, plus finding the Codex program and opening threads in its app.

Codex 0.16x keeps its threads in $CODEX_HOME/state_<n>.sqlite (table `threads`) and their turns and
messages in thread_history_<n>.sqlite (`thread_turns`, `thread_items`). Older rollout files are
zstd-compressed (.jsonl.zst), which Python can only read from 3.14, so the databases come first and
the rollout files are only used for the usage snapshot. Databases are opened with mode=ro.
"""

import glob
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

try:
    import sqlite3
    _DB_ERRORS: tuple = (sqlite3.Error,)
except ImportError:  # some minimal Python builds leave sqlite3 out; tagteam must still start
    sqlite3 = None  # type: ignore[assignment]
    _DB_ERRORS = ()

DONE_STATES = ("completed", "failed", "interrupted")
LIMIT_CODES = ("usageLimitExceeded",)


def home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def clean_path(p: str) -> str:
    """Codex on Windows stores folders as \\\\?\\C:\\...; drop that prefix."""
    p = str(p or "")
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def _db_path(prefix: str) -> Optional[str]:
    best, num = None, -1
    for p in glob.glob(os.path.join(glob.escape(home()), prefix + "_*.sqlite")):
        m = re.search(r"_(\d+)\.sqlite$", p)
        if m and int(m.group(1)) > num:
            best, num = p, int(m.group(1))
    return best


def connect(prefix: str):
    p = _db_path(prefix) if sqlite3 is not None else None
    if not p:
        return None
    try:
        c = sqlite3.connect(pathlib.Path(p).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        return c
    except _DB_ERRORS:
        return None


def available() -> bool:
    return sqlite3 is not None and _db_path("state") is not None


def _cols(c, table: str) -> List[str]:
    return [r[1] for r in c.execute(f"pragma table_info({table})")]


@dataclass
class Thread:
    id: str
    cwd: str
    title: str
    first_prompt: str
    updated: float
    rollout_path: str
    model: str
    source: str


def threads(ids: Optional[List[str]] = None) -> List[Thread]:
    """The person's own threads (not Codex's helper subagents, not archived), newest first."""
    c = connect("state")
    if c is None:
        return []
    try:
        have = set(_cols(c, "threads"))
        want = ["id", "cwd", "title", "first_user_message", "updated_at", "updated_at_ms", "rollout_path",
                "model", "source", "archived", "name"]
        sel = ", ".join(k if k in have else f"NULL AS {k}" for k in want)
        sql = f"select {sel} from threads"
        args: list = []
        if ids:
            sql += " where id in (" + ",".join("?" * len(ids)) + ")"
            args = list(ids)
        out = []
        for r in c.execute(sql, args):
            src = str(r["source"] or "")
            if r["archived"] or src.startswith("{"):  # {"subagent": ...} rows are Codex's own helpers
                continue
            upd = (r["updated_at_ms"] or 0) / 1000.0 or float(r["updated_at"] or 0)
            out.append(Thread(str(r["id"]), clean_path(r["cwd"]), str(r["name"] or r["title"] or ""),
                              str(r["first_user_message"] or ""), upd, clean_path(r["rollout_path"]),
                              str(r["model"] or ""), src))
        return sorted(out, key=lambda t: -t.updated)
    except _DB_ERRORS:
        return []
    finally:
        c.close()


@dataclass
class Turn:
    id: str
    status: str
    error: str = ""
    error_code: str = ""
    started: float = 0.0
    final_item: str = ""


def turns(tid: str) -> List[Turn]:
    c = connect("thread_history")
    if c is None:
        return []
    try:
        out = []
        for r in c.execute("select turn_id, status, error_json, started_at, final_agent_item_id from thread_turns "
                           "where thread_id=? order by rollout_ordinal", (tid,)):
            msg = code = ""
            if r["error_json"]:
                try:
                    j = json.loads(r["error_json"])
                    msg, code = str(j.get("message") or ""), str(j.get("codexErrorInfo") or "")
                except ValueError:
                    msg = str(r["error_json"])
            out.append(Turn(str(r["turn_id"]), str(r["status"] or ""), msg, code, float(r["started_at"] or 0),
                            str(r["final_agent_item_id"] or "")))
        return out
    except _DB_ERRORS:
        return []
    finally:
        c.close()


def _item_text(kind: str, raw: str) -> str:
    try:
        j = json.loads(raw)
    except ValueError:
        return ""
    if kind == "agentMessage":
        return str(j.get("text") or "")
    parts = [str(x.get("text") or "") for x in (j.get("content") or []) if isinstance(x, dict)]
    return "\n".join(p for p in parts if p).strip()


def messages(tid: str, turn_id: Optional[str] = None) -> List[tuple]:
    """(turn_id, 'user' | 'assistant', text) in order. Tool calls and reasoning are left out."""
    c = connect("thread_history")
    if c is None:
        return []
    try:
        sql = ("select turn_id, item_type, item_json from thread_items where thread_id=? "
               "and item_type in ('userMessage','agentMessage')")
        args = [tid]
        if turn_id:
            sql += " and turn_id=?"
            args.append(turn_id)
        out = []
        for r in c.execute(sql + " order by rollout_ordinal, created_at_ms", args):
            text = _item_text(r["item_type"], r["item_json"])
            if text:
                out.append((str(r["turn_id"]), "user" if r["item_type"] == "userMessage" else "assistant", text))
        return out
    except _DB_ERRORS:
        return []
    finally:
        c.close()


def final_text(tid: str, turn_id: str) -> str:
    replies = [t for _, role, t in messages(tid, turn_id) if role == "assistant"]
    return replies[-1] if replies else ""


def prompt_counts() -> Dict[str, int]:
    c = connect("thread_history")
    if c is None:
        return {}
    try:
        return {str(r[0]): int(r[1]) for r in
                c.execute("select thread_id, count(*) from thread_items where item_type='userMessage' group by thread_id")}
    except _DB_ERRORS:
        return {}
    finally:
        c.close()


def _read_lines(path: str) -> List[str]:
    if path.endswith(".zst"):
        try:
            from compression import zstd  # Python 3.14+
        except ImportError:
            return []
        with zstd.open(path, "rt", encoding="utf-8", errors="replace") as f:
            return f.readlines()
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.readlines()


def rollout_file(rollout_path: str) -> Optional[str]:
    for p in (rollout_path, rollout_path + ".zst"):
        if p and os.path.isfile(p):
            return p
    return None


def snapshot(rollout_path: str) -> Optional[dict]:
    """The newest rate-limit snapshot in a rollout file (plain, or .zst on Python 3.14+)."""
    p = rollout_file(rollout_path)
    if not p:
        return None
    snap = None
    try:
        for line in _read_lines(p):
            if '"rate_limits"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            pl = o.get("payload") if isinstance(o.get("payload"), dict) else o
            if isinstance(pl.get("rate_limits"), dict):
                snap = pl["rate_limits"]
    except (OSError, EOFError, ValueError):
        return snap
    return snap


def thread_snapshot(tid: str) -> Optional[dict]:
    t = threads([tid])
    return snapshot(t[0].rollout_path) if t else None


# ---- models -----------------------------------------------------------------------

def available_models() -> List[str]:
    """Models this account can use, from Codex's own cache. Empty when unknown."""
    try:
        with open(os.path.join(home(), "models_cache.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    items = data.get("models") if isinstance(data, dict) else data
    out = []
    for m in items or []:
        if isinstance(m, dict) and (m.get("slug") or m.get("id")):
            out.append(str(m.get("slug") or m.get("id")))
    return out


def config_model() -> str:
    """The top-level `model = "..."` in config.toml (read with a regex; tomllib needs 3.11)."""
    try:
        with open(os.path.join(home(), "config.toml"), encoding="utf-8") as f:
            for line in f:
                if line.lstrip().startswith("["):
                    break
                m = re.match(r'\s*model\s*=\s*"([^"]+)"', line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return ""


def pick_model(thread_id: Optional[str] = None) -> Optional[str]:
    """The model to pass with -m, or None to leave it to Codex.

    Found on a real install: config.toml named a model the account had lost, and `exec resume`
    uses the config model rather than the thread's, so both new and resumed turns failed. Use a
    model the account still has: the thread's own, else the config's, else the one the person used
    last, else the first one listed."""
    have = available_models()
    if not have:
        return None
    if thread_id:
        t = threads([thread_id])
        if t and t[0].model in have:
            return t[0].model
    cfg = config_model()
    if cfg in have:
        return None if not thread_id else cfg
    for t in threads():
        if t.model in have:
            return t.model
    return have[0]


# ---- the program and the app ---------------------------------------------------------

def find_codex() -> Optional[str]:
    """codex on PATH, else the copy inside the Codex desktop app or the VS Code extension."""
    p = shutil.which("codex")
    if p:
        return p
    exe = "codex.exe" if os.name == "nt" else "codex"
    cands = []
    if os.name == "nt":
        cands += glob.glob(r"C:\Program Files\WindowsApps\OpenAI.Codex_*\app\resources\codex.exe")
    elif sys.platform == "darwin":
        cands += glob.glob("/Applications/Codex.app/Contents/Resources/codex")
    for ide in (".vscode", ".vscode-insiders", ".cursor", ".windsurf"):
        cands += glob.glob(os.path.join(os.path.expanduser("~"), ide, "extensions", "openai.chatgpt-*", "bin", "*", exe))
    cands = [c for c in cands if os.path.isfile(c)]
    return max(cands, key=os.path.getmtime) if cands else None


def thread_url(tid: str) -> str:
    return f"codex://threads/{tid}"


def open_thread(tid: str) -> bool:
    """Show this thread in the Codex desktop app (starts the app if needed)."""
    url = thread_url(tid)
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, AttributeError):
        return False


def app_installed() -> bool:
    if os.name == "nt":
        try:
            import winreg

            winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\codex").Close()
            return True
        except OSError:
            return bool(glob.glob(r"C:\Program Files\WindowsApps\OpenAI.Codex_*"))
    if sys.platform == "darwin":
        return os.path.isdir("/Applications/Codex.app")
    return False
