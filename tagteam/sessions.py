"""Find the Claude Code and Codex sessions that belong to a folder, and read what they were about.

Claude Code keeps one JSONL transcript per session in ~/.claude/projects/<folder name>/<id>.jsonl
(CLAUDE_CONFIG_DIR moves ~/.claude). Codex keeps ~/.codex/sessions/YYYY/MM/DD/rollout-...-<id>.jsonl
(CODEX_HOME moves ~/.codex); newer Codex keeps its threads in SQLite (see codexdb.py) and compresses
those files, so its databases come first. Sessions are matched on the folder they ran in.
Only reads these files; never writes them.
"""

import glob
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import codexdb
from .textparse import LIMIT_RE, parse_reset_text

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
AGENT_NAMES = {"claude": "Claude", "codex": "Codex"}


@dataclass
class Session:
    agent: str                 # "claude" or "codex"
    id: str
    path: str
    cwd: str = ""
    title: str = ""
    first_prompt: str = ""
    last_prompt: str = ""
    last_reply: str = ""
    prompts: int = 0
    updated: float = 0.0
    limited: bool = False      # the last reply is a usage-limit notice
    resets_at: Optional[float] = None
    turn_open: bool = False    # the transcript does not end with a finished reply
    model: str = ""            # Codex: the model the thread uses
    from_db: bool = False      # Codex: read from Codex's SQLite state

    @property
    def short(self) -> str:
        return self.id[:8]

    @property
    def spec(self) -> str:
        return f"{self.agent}:{self.id}"

    def limited_now(self, now: Optional[float] = None) -> bool:
        return self.limited and (self.resets_at is None or self.resets_at > (now or time.time()))


def claude_home() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def inside(path: str, folder: str) -> bool:
    p, f = norm(path), norm(folder)
    return p == f or p.startswith(f.rstrip(os.sep) + os.sep)


def clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 3] + "..."


def _load(line: str) -> dict:
    try:
        o = json.loads(line)
    except ValueError:
        return {}
    return o if isinstance(o, dict) else {}


def _texts(content) -> List[str]:
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text") and b.get("text"):
                out.append(str(b["text"]))
    return out


def _has_tool_use(content) -> bool:
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)


def _human(text: str) -> bool:
    """A prompt the person typed, not a command echo, an IDE context block or a system note."""
    t = (text or "").strip()
    return bool(t) and not t.startswith(("<", "Caveat:", "[Request interrupted"))


def _limit(reply: str, ref: float) -> Tuple[bool, Optional[float]]:
    if reply and len(reply) < 600 and LIMIT_RE.search(reply):
        return True, parse_reset_text(reply, ref)
    return False, None


_cache: Dict[str, Tuple[float, int, Session]] = {}
_meta: Dict[str, Tuple[str, str]] = {}


def _stat(path):
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    hit = _cache.get(path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return st, hit[2]
    return st, None


# ---- Claude Code ------------------------------------------------------------

def _claude_lines(path):
    """(kind, entry) for the conversation entries of the main thread: kind is 'prompt' (typed by
    the person), 'tool' (a tool result), or 'assistant'. Subagent and bookkeeping lines are skipped."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith('{"type":"'):
                if any(k in line[:40] for k in ('"ai-title"', '"custom-title"', '"summary"')):
                    yield "title", _load(line)
                continue
            if '"isSidechain":true' in line[:400]:
                continue
            if '"type":"assistant"' in line:
                yield "assistant", line
            elif '"type":"user"' in line:
                if '"tool_use_id"' in line:
                    yield "tool", None
                elif '"isMeta":true' not in line and '"isCompactSummary":true' not in line:
                    o = _load(line)
                    if o.get("type") == "user":
                        yield "prompt", o
            elif '"cwd":"' in line:
                yield "cwd", line


def read_claude(path: str) -> Optional[Session]:
    st, hit = _stat(path)
    if st is None or hit:
        return hit
    s = Session("claude", os.path.splitext(os.path.basename(path))[0], path, updated=st.st_mtime)
    titles: Dict[str, str] = {}
    last_kind, last_assistant, last_text = None, None, None
    try:
        for kind, item in _claude_lines(path):
            if kind == "title":
                for typ, key in (("custom-title", "customTitle"), ("ai-title", "aiTitle"), ("summary", "summary")):
                    if item.get("type") == typ and item.get(key):
                        titles[typ] = str(item[key])
                continue
            if kind == "cwd":
                if not s.cwd:
                    s.cwd = str(_load(item).get("cwd") or "")
                continue
            last_kind = kind
            if kind == "assistant":
                last_assistant = item
                if '"type":"text"' in item:
                    last_text = item
            elif kind == "prompt":
                s.cwd = s.cwd or str(item.get("cwd") or "")
                s.id = str(item.get("sessionId") or s.id)
                for t in _texts((item.get("message") or {}).get("content")):
                    if _human(t):
                        s.prompts += 1
                        s.first_prompt = s.first_prompt or t.strip()
                        s.last_prompt = t.strip()
                        break
    except OSError:
        return None
    if last_text:
        s.last_reply = "\n".join(_texts((_load(last_text).get("message") or {}).get("content"))).strip()
    tool_pending = bool(last_assistant) and _has_tool_use((_load(last_assistant).get("message") or {}).get("content"))
    s.turn_open = last_kind != "assistant" or tool_pending
    s.title = clip(titles.get("custom-title") or titles.get("ai-title") or titles.get("summary")
                   or s.first_prompt or "(no prompt yet)", 80)
    s.limited, s.resets_at = _limit(s.last_reply, s.updated)
    _cache[path] = (st.st_mtime, st.st_size, s)
    return s


def claude_sessions(folder: str) -> List[Session]:
    base = os.path.join(claude_home(), "projects")
    try:
        names = os.listdir(base)
    except OSError:
        return []
    # Claude names each folder after the path with every other character turned into '-'. That
    # is lossy (/a/b-c and /a/b/c look alike), so candidates are confirmed by the recorded cwd.
    want = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(folder)).casefold()
    cands = [n for n in names if n.casefold() == want or n.casefold().startswith(want + "-")]
    cands += [n for n in names if len(n) > 150 and n not in cands]  # long paths get shortened
    out = []
    for n in cands:
        for p in glob.glob(os.path.join(base, glob.escape(n), "*.jsonl")):
            s = read_claude(p)
            if s and (inside(s.cwd, folder) if s.cwd else n.casefold() == want):
                out.append(s)
    return out


# ---- Codex --------------------------------------------------------------------

def _codex_meta(path: str) -> Tuple[str, str]:
    if path in _meta:
        return _meta[path]
    sid = cwd = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(5):
                o = _load(f.readline())
                if o.get("type") == "session_meta":
                    pl = o.get("payload") or {}
                    sid, cwd = str(pl.get("id") or ""), str(pl.get("cwd") or "")
                    break
    except OSError:
        pass
    if not sid:
        ids = UUID_RE.findall(os.path.basename(path))
        sid = ids[-1] if ids else os.path.basename(path)
    _meta[path] = (sid, cwd)
    return sid, cwd


def _codex_events(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            for key, kind in (('"user_message"', "user_message"), ('"agent_message"', "agent_message"),
                              ('"task_complete"', "task_complete"), ('"turn_aborted"', "turn_aborted"),
                              ('"rate_limits"', "token_count"), ('"type":"error"', "error")):
                if key in line:
                    pl = _load(line).get("payload") or {}
                    if pl.get("type") == kind:
                        yield kind, pl
                    break


def read_codex(path: str) -> Optional[Session]:
    st, hit = _stat(path)
    if st is None or hit:
        return hit
    sid, cwd = _codex_meta(path)
    s = Session("codex", sid, path, cwd=cwd, updated=st.st_mtime)
    last, snap, error = None, None, ""
    try:
        for kind, pl in _codex_events(path):
            if kind == "user_message":
                last = "user"
                msg = str(pl.get("message") or "")
                if _human(msg):
                    s.prompts += 1
                    s.first_prompt = s.first_prompt or msg.strip()
                    s.last_prompt = msg.strip()
            elif kind == "agent_message" and pl.get("message"):
                s.last_reply, last = str(pl["message"]).strip(), "assistant"
            elif kind in ("task_complete", "turn_aborted"):
                last = "done"
            elif kind == "token_count" and isinstance(pl.get("rate_limits"), dict):
                snap = pl["rate_limits"]
            elif kind == "error":
                error = str(pl.get("message") or "")
    except OSError:
        return None
    s.turn_open = last == "user"
    s.title = clip(s.first_prompt or "(no prompt yet)", 80)
    if snap:
        from .agents import RunResult, apply_codex_snapshot

        r = RunResult()
        apply_codex_snapshot(snap, r, st.st_mtime)
        s.limited, s.resets_at = r.limited, r.resets_at
    if not s.limited:
        s.limited, s.resets_at = _limit(error or s.last_reply, s.updated)
    _cache[path] = (st.st_mtime, st.st_size, s)
    return s


def _from_thread(t, counts: Optional[Dict[str, int]] = None, full: bool = True) -> Session:
    s = Session("codex", t.id, codexdb.rollout_file(t.rollout_path) or t.rollout_path, cwd=t.cwd,
                updated=t.updated, model=t.model, from_db=True)
    s.first_prompt = t.first_prompt.strip()
    s.title = clip(t.title or s.first_prompt or "(no prompt yet)", 80)
    s.prompts = (counts or {}).get(t.id, 0)
    if full:
        msgs = codexdb.messages(t.id)
        users = [m[2] for m in msgs if m[1] == "user" and _human(m[2])]
        replies = [m[2] for m in msgs if m[1] == "assistant"]
        s.prompts = s.prompts or len(users)
        s.last_prompt = users[-1].strip() if users else ""
        s.last_reply = replies[-1].strip() if replies else ""
        ts = codexdb.turns(t.id)
        if ts:
            last = ts[-1]
            s.turn_open = last.status == "inProgress"
            if last.status == "failed" and (last.error_code in codexdb.LIMIT_CODES or LIMIT_RE.search(last.error)):
                s.limited, s.resets_at = True, parse_reset_text(last.error, last.started or t.updated)
    return s


def codex_thread(sid: str) -> Optional[Session]:
    t = codexdb.threads([sid])
    return _from_thread(t[0], codexdb.prompt_counts()) if t else None


def _codex_files() -> List[str]:
    return glob.glob(os.path.join(glob.escape(codex_home()), "sessions", "**", "rollout-*.jsonl"), recursive=True)


def codex_sessions(folder: str) -> List[Session]:
    if codexdb.available():
        counts = codexdb.prompt_counts()
        return [_from_thread(t, counts) for t in codexdb.threads() if t.cwd and inside(t.cwd, folder)]
    out = []
    for p in _codex_files():
        _, cwd = _codex_meta(p)
        if cwd and inside(cwd, folder):
            s = read_codex(p)
            if s:
                out.append(s)
    return out


# ---- both ---------------------------------------------------------------------

def list_sessions(folder: str) -> List[Session]:
    """Every Claude and Codex session that ran in this folder or a folder inside it, newest first."""
    return sorted(claude_sessions(folder) + codex_sessions(folder), key=lambda s: -s.updated)


def read_file(path: str) -> Optional[Session]:
    if os.path.basename(path).startswith("rollout-") or inside(path, codex_home()):
        return read_codex(path)
    return read_claude(path)


def by_id(sid: str, agent: Optional[str] = None) -> Optional[Session]:
    sid = glob.escape(sid)
    if agent in (None, "claude"):
        for p in glob.glob(os.path.join(glob.escape(claude_home()), "projects", "*", sid + "*.jsonl")):
            return read_claude(p)
    if agent in (None, "codex") and codexdb.available():
        hits = [t for t in codexdb.threads() if t.id.lower().startswith(sid.lower())]
        if hits:
            return _from_thread(hits[0], codexdb.prompt_counts())
    if agent in (None, "codex"):
        for p in glob.glob(os.path.join(glob.escape(codex_home()), "sessions", "**", f"rollout-*{sid}*.jsonl"),
                           recursive=True):
            return read_codex(p)
    return None


def find_session(folder: str, spec: Optional[str] = None) -> Session:
    """spec: 'latest' (default), 'claude' or 'codex' (that agent's latest), a session id or the start
    of one (optionally 'claude:<id>'), or the path of a transcript file."""
    spec = (spec or "").strip()
    if not spec or "${" in spec or spec.startswith("$"):  # an unexpanded placeholder means "this chat"
        spec = "latest"
    if spec.endswith(".jsonl") and os.path.isfile(spec):
        s = read_file(spec)
        if s:
            return s
    agent, ident = "", spec
    head, sep, rest = spec.partition(":")
    if sep and head in AGENT_NAMES:
        agent, ident = head, rest
    if ident in AGENT_NAMES:
        agent, ident = ident, "latest"
    found = [s for s in list_sessions(folder) if not agent or s.agent == agent]
    if ident == "latest":
        if not found:
            who = AGENT_NAMES.get(agent, "Claude or Codex")
            raise LookupError(f"no {who} sessions found for {folder}")
        return found[0]
    hits = [s for s in found if s.id.lower() == ident.lower()] or \
           [s for s in found if s.id.lower().startswith(ident.lower())]
    if len(hits) > 1:
        raise LookupError(f"'{ident}' matches {len(hits)} sessions; give more of the id")
    if hits:
        return hits[0]
    s = by_id(ident, agent or None)
    if s:
        return s
    raise LookupError(f"no session '{ident}' found")


def _first_cwd(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if '"cwd":"' in line:
                    return str(_load(line).get("cwd") or "")
                if i > 400:
                    break
    except OSError:
        pass
    return ""


def known_folders(limit: int = 40) -> List[str]:
    """Folders that have Claude or Codex sessions, most recently used first."""
    seen: Dict[str, Tuple[float, str]] = {}

    def add(cwd, mtime):
        if cwd:
            k = norm(cwd)
            if k not in seen or seen[k][0] < mtime:
                seen[k] = (mtime, cwd)

    for d in glob.glob(os.path.join(glob.escape(claude_home()), "projects", "*")):
        files = glob.glob(os.path.join(glob.escape(d), "*.jsonl"))
        if files:
            newest = max(files, key=os.path.getmtime)
            add(_first_cwd(newest), os.path.getmtime(newest))
    if codexdb.available():
        for t in codexdb.threads():
            add(t.cwd, t.updated)
    else:
        for p in _codex_files():
            add(_codex_meta(p)[1], os.path.getmtime(p))
    ordered = sorted(seen.values(), key=lambda v: -v[0])
    return [cwd for _, cwd in ordered if os.path.isdir(cwd)][:limit]


def dialog(s: Session) -> List[Tuple[str, str]]:
    """The conversation as (role, text) pairs, role 'user' or 'assistant'; tool calls left out."""
    out: List[Tuple[str, str]] = []

    def put(role, text):
        text = (text or "").strip()
        if not text:
            return
        if out and out[-1][0] == role:
            out[-1] = (role, out[-1][1] + "\n\n" + text)
        else:
            out.append((role, text))

    try:
        if s.agent == "claude":
            for kind, item in _claude_lines(s.path):
                if kind == "prompt":
                    for t in _texts((item.get("message") or {}).get("content")):
                        if _human(t):
                            put("user", t)
                            break
                elif kind == "assistant" and '"type":"text"' in item:
                    put("assistant", "\n".join(_texts((_load(item).get("message") or {}).get("content"))))
        elif s.from_db:
            for _, role, text in codexdb.messages(s.id):
                if role == "assistant" or _human(text):
                    put(role, text)
        else:
            for kind, pl in _codex_events(s.path):
                if kind == "user_message" and _human(str(pl.get("message") or "")):
                    put("user", str(pl["message"]))
                elif kind == "agent_message":
                    put("assistant", str(pl.get("message") or ""))
    except OSError:
        pass
    return out


def dialog_tail(s: Session, max_chars: int = 6000, per_message: int = 2500) -> str:
    """The end of the conversation as text, newest last, within max_chars."""
    name = AGENT_NAMES.get(s.agent, s.agent)
    parts: List[str] = []
    used = 0
    for role, text in reversed(dialog(s)):
        if len(text) > per_message:
            text = "[...]\n" + text[-per_message:]
        block = f"### {'User' if role == 'user' else name}\n{text}\n"
        if used + len(block) > max_chars and parts:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(reversed(parts))


def ago(t: float, now: Optional[float] = None) -> str:
    d = max(0.0, (now or time.time()) - t)
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{d / 60:.0f} min ago"
    if d < 2 * 86400:
        return f"{d / 3600:.0f} h ago"
    return f"{d / 86400:.0f} days ago"


def state_text(s: Session, now: Optional[float] = None) -> str:
    now = now or time.time()
    if s.limited_now(now):
        if s.resets_at:
            from .textparse import fmt_time

            return f"at limit, resets {fmt_time(s.resets_at)}"
        return "at its usage limit"
    if s.turn_open and now - s.updated < 300:
        return "working now"
    if now - s.updated < 300:
        return "open, idle"
    return ""


def wait_until_quiet(s: Session, quiet_s: float = 30, give_up_s: float = 900, stop=None, sleep=time.sleep) -> None:
    """Return once the session's transcript has not changed for quiet_s seconds and its last turn
    is finished, so tagteam never writes to a session that a chat is still using. After give_up_s
    seconds with no change at all, carry on even if the last turn looks unfinished."""
    reader = read_claude if s.agent == "claude" else read_codex
    while True:
        if stop and stop():
            raise KeyboardInterrupt
        if s.from_db:
            cur = codex_thread(s.id)
            if cur is None:
                return
            idle = time.time() - cur.updated
            if idle >= quiet_s and (not cur.turn_open or idle >= give_up_s):
                return
            sleep(min(2.0, max(0.1, quiet_s / 5)))
            continue
        try:
            idle = time.time() - os.path.getmtime(s.path)
        except OSError:
            return
        if idle >= quiet_s:
            cur = reader(s.path)
            if cur is None or not cur.turn_open or idle >= give_up_s:
                return
        sleep(min(2.0, max(0.1, quiet_s / 5)))
