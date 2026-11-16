"""Agent adapters. Each one builds a headless command and reads usage and limits from its output."""

import glob
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import codexdb, proc
from .config import ConfigError
from .textparse import LIMIT_RE, iter_json_objects, parse_reset_text

# Claude can say "allowed_warning" without a percentage. Treat that as 90% so a main agent
# with the default threshold hands off.
WARNING_ESTIMATE = 90.0
CLAUDE_WINDOWS = {"five_hour": "5h", "seven_day": "7d"}
CODEX_WINDOWS = {300: "5h", 1440: "1d", 10080: "7d", 43200: "30d"}  # a free plan has one 30-day window


@dataclass
class Window:
    used: Optional[float] = None       # percent, 0-100
    resets_at: Optional[float] = None  # epoch seconds


@dataclass
class RunResult:
    ok: bool = False
    limited: bool = False
    resets_at: Optional[float] = None
    windows: Dict[str, Window] = field(default_factory=dict)
    message: str = ""
    timed_out: bool = False
    returncode: Optional[int] = None
    session_id: Optional[str] = None   # the agent's session, to resume on the next turn
    text: str = ""                     # the agent's final reply


def _num(x) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _pct(x) -> Optional[float]:
    v = _num(x)
    if v is None:
        return None
    return v * 100.0 if v <= 1.5 else v  # Claude reports a fraction (0.09 = 9%)


class Agent:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg
        cmd = cfg.get("command") or [name]
        self.base = [cmd] if isinstance(cmd, str) else list(cmd)
        self.args = [str(a) for a in (cfg.get("args") or [])]

    def available(self) -> bool:
        return bool(shutil.which(self.base[0])) or os.path.isfile(self.base[0])

    def _exe(self) -> List[str]:
        # Resolve through PATH/PATHEXT so Windows finds claude.cmd / codex.cmd too.
        return [shutil.which(self.base[0]) or self.base[0]] + self.base[1:]

    def env(self) -> dict:
        env = dict(os.environ)
        for k, v in (self.cfg.get("env") or {}).items():
            env[str(k)] = str(v)
        return env

    supports_resume = False

    def build(self, prompt: str, resume: Optional[str] = None):
        raise NotImplementedError

    def parse(self, out: proc.RunOutput) -> RunResult:
        raise NotImplementedError

    def run(self, prompt: str, cwd: str, timeout_s: float, log_path: str,
            resume: Optional[str] = None, stop=None) -> RunResult:
        """resume: continue this session id instead of starting a new session."""
        cmd, stdin_text = self.build(prompt, resume)
        try:
            out = proc.run(cmd, cwd, stdin_text, timeout_s, log_path, self.env(), stop=stop)
        except OSError as e:
            return RunResult(ok=False, message=f"could not start {cmd[0]}: {e}")
        res = self.parse(out)
        res.returncode = out.returncode
        if out.timed_out:
            res.ok = False
            res.timed_out = True
            res.message = res.message or f"no result within {timeout_s / 60:.0f} minutes"
        return res

    def _backstop(self, res: RunResult, texts: List[str], regex=LIMIT_RE) -> None:
        """On a failed turn, look for a limit message in error text when no structured signal said so."""
        if res.ok:
            return
        blob = "\n".join(t for t in texts if t)
        if regex.search(blob):
            res.limited = True
        if res.limited and res.resets_at is None:
            res.resets_at = parse_reset_text(blob, time.time())
        if not res.message:
            res.message = blob.strip()[-300:]


class ClaudeAgent(Agent):
    """Claude Code in print mode with stream-json output."""

    supports_resume = True

    def build(self, prompt, resume=None):
        cmd = self._exe() + ["-p", "--output-format", "stream-json", "--verbose"]
        if resume:
            cmd += ["--resume", resume]
        return cmd + self.args, prompt

    def env(self):
        env = super().env()
        env.pop("CLAUDECODE", None)  # tagteam may itself be started from a Claude Code terminal
        return env

    def parse(self, out):
        res = RunResult()
        result = None
        loose = [out.stderr]
        for line in out.lines:
            objs = list(iter_json_objects(line))
            if not objs:
                loose.append(line)
            for o in objs:
                t = o.get("type")
                if o.get("session_id") and t in ("system", "result"):
                    res.session_id = str(o["session_id"])
                if t == "rate_limit_event":
                    apply_claude_rate(o.get("rate_limit_info") or {}, res)
                elif t == "result":
                    result = o
        if result is not None:
            is_err = bool(result.get("is_error"))
            res.ok = not is_err and out.returncode == 0
            res.text = str(result.get("result") or "")
            if is_err:
                txt = str(result.get("result") or result.get("subtype") or "")
                res.message = txt.strip()[:500]
                loose.append(txt)
                if result.get("api_error_status") == 429:
                    res.limited = True
        self._backstop(res, loose)
        return res


def apply_claude_rate(info: dict, res: RunResult) -> None:
    status = info.get("status")
    for key, w in (info.get("unifiedWindows") or {}).items():
        if isinstance(w, dict):
            res.windows[CLAUDE_WINDOWS.get(key, key)] = Window(_pct(w.get("utilization")), _num(w.get("resetsAt")))
    rtype = info.get("rateLimitType")
    resets = _num(info.get("resetsAt"))
    name = CLAUDE_WINDOWS.get(rtype, rtype) if rtype else None
    if name:
        win = res.windows.setdefault(name, Window())
        if win.used is None:
            win.used = _pct(info.get("utilization"))
        if win.resets_at is None:
            win.resets_at = resets
        if status == "allowed_warning" and win.used is None:
            win.used = WARNING_ESTIMATE
    if status == "rejected" and not info.get("isUsingOverage"):
        res.limited = True
        if resets:
            res.resets_at = max(res.resets_at or 0, resets)
        if name:
            res.windows[name].used = 100.0


class CodexAgent(Agent):
    """Codex CLI `exec --json`. Usage and limits come from Codex's SQLite state and session file.

    With cfg["gui"] the turns run in the Codex desktop app instead, where the person can watch them:
    the thread is opened with codex://threads/<id> and each prompt goes in with `codex queue`, which
    the app picks up and runs (checked on a real install); the result is read back from SQLite."""

    supports_resume = True
    QUEUE_ARG_MAX = 20000   # longer prompts go through a file (Windows caps a command line at 32k)
    PICKUP_S = 90           # how long the app may take to start a queued turn before we reopen it

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self._opened = set()

    def _exe(self):
        if self.base[0] != "codex" or shutil.which(self.base[0]):
            return super()._exe()
        found = codexdb.find_codex()  # the desktop app and the VS Code extension carry their own codex
        return [found] + self.base[1:] if found else super()._exe()

    def available(self):
        exe = self._exe()[0]
        return bool(shutil.which(exe)) or os.path.isfile(exe)

    def _model_args(self, resume):
        if self.cfg.get("model"):
            return ["-m", str(self.cfg["model"])]
        if any(a in ("-m", "--model") or a.startswith("--model=") for a in self.args):
            return []
        m = codexdb.pick_model(resume)
        return ["-m", m] if m else []

    def build(self, prompt, resume=None):
        cmd = self._exe() + ["exec", "--json", "--skip-git-repo-check"] + self._model_args(resume) + self.args
        if resume:
            cmd += ["resume", resume]  # `codex exec [options] resume <SESSION_ID> -`
        return cmd + ["-"], prompt

    def codex_home(self) -> str:
        return (self.cfg.get("codex_home") or os.environ.get("CODEX_HOME")
                or os.path.join(os.path.expanduser("~"), ".codex"))

    def find_rollout(self, thread_id: Optional[str], started_at: float) -> Optional[str]:
        if thread_id:
            t = codexdb.threads([thread_id])
            if t and codexdb.rollout_file(t[0].rollout_path):
                return codexdb.rollout_file(t[0].rollout_path)
        base = os.path.join(self.codex_home(), "sessions")
        if thread_id:
            hits = glob.glob(os.path.join(base, "**", f"rollout-*{thread_id}*.jsonl"), recursive=True)
            if hits:
                return max(hits, key=os.path.getmtime)
        fresh = [p for p in glob.glob(os.path.join(base, "**", "rollout-*.jsonl"), recursive=True)
                 if os.path.getmtime(p) >= started_at - 2]
        return max(fresh, key=os.path.getmtime) if fresh else None

    def _usage(self, res: RunResult, thread_id: Optional[str], started_at: float) -> None:
        path = self.find_rollout(thread_id, started_at)
        if path:
            snap = codexdb.snapshot(path) if path.endswith(".zst") else read_codex_snapshot(path)
            if snap:
                apply_codex_snapshot(snap, res, os.path.getmtime(path))

    def parse(self, out):
        res = RunResult()
        thread_id, completed, loose = None, False, [out.stderr]
        for line in out.lines:
            objs = list(iter_json_objects(line))
            if not objs:
                loose.append(line)
            for o in objs:
                t = o.get("type")
                if t == "thread.started":
                    thread_id = o.get("thread_id") or thread_id
                elif t == "item.completed" and isinstance(o.get("item"), dict):
                    it = o["item"]
                    if it.get("type") == "agent_message" and it.get("text"):
                        res.text = str(it["text"])
                    # an item of type "error" is a warning (e.g. "resuming with a different model")
                elif t == "turn.completed":
                    completed = True
                elif t in ("turn.failed", "error"):
                    err = o.get("error")
                    msg = o.get("message") or (err.get("message") if isinstance(err, dict) else err) or ""
                    loose.append(str(msg))
                    res.message = str(msg)[:500] or res.message
        res.ok = completed and out.returncode == 0
        res.session_id = thread_id
        self._usage(res, thread_id, out.started_at)
        self._backstop(res, loose)
        return res

    # ---- GUI mode: the turn runs in the Codex desktop app ----------------------------

    def run(self, prompt, cwd, timeout_s, log_path, resume=None, stop=None):
        if not self.cfg.get("gui"):
            return super().run(prompt, cwd, timeout_s, log_path, resume, stop)
        if not resume:
            # A new thread is started headless (the app has no "new thread with this prompt" link),
            # then shown in the app; every later turn runs in the app itself.
            res = super().run(prompt, cwd, timeout_s, log_path, None, stop)
            if res.session_id:
                self._show(res.session_id, log_path)
            return res
        return self._gui_turn(prompt, cwd, timeout_s, log_path, resume, stop)

    @staticmethod
    def _log(log_path, text):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("[" + time.strftime("%H:%M:%S") + "] " + text + "\n")
        except OSError:
            pass

    def _show(self, tid, log_path):
        ok = codexdb.open_thread(tid)
        self._opened.add(tid)
        verb = "opened" if ok else "could not open"
        self._log(log_path, verb + " " + codexdb.thread_url(tid) + " in the Codex app")
        return ok

    def _gui_turn(self, prompt, cwd, timeout_s, log_path, tid, stop):
        before = {t.id for t in codexdb.turns(tid)}
        if tid not in self._opened:
            self._show(tid, log_path)
            time.sleep(3)
        msg = prompt
        if len(prompt) > self.QUEUE_ARG_MAX:
            d = os.path.join(cwd, ".tagteam")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "codex-prompt.md"), "w", encoding="utf-8") as f:
                f.write(prompt)
            msg = ("Read the file .tagteam/codex-prompt.md in this folder and follow it exactly; "
                   "it is the message for this turn.")
        cmd = self._exe() + ["queue", "--thread", tid, "--message", msg, "--resume"]
        try:
            out = proc.run(cmd, cwd, None, 120, log_path, self.env(), stop=stop)
        except OSError as e:
            return RunResult(ok=False, message=f"could not start {cmd[0]}: {e}", session_id=tid)
        if out.returncode != 0:
            text = (out.stderr or "".join(out.lines)).strip()
            return RunResult(ok=False, returncode=out.returncode, session_id=tid,
                             message=("codex queue failed: " + text)[:500])
        self._log(log_path, "queued the message; waiting for the Codex app to run it")
        start = time.time()
        pickup = start + self.PICKUP_S
        reopened = False
        while True:
            if stop and stop():
                raise KeyboardInterrupt
            new = [t for t in codexdb.turns(tid) if t.id not in before]
            if new and new[-1].status in codexdb.DONE_STATES:
                return self._gui_result(tid, new[-1], start)
            now = time.time()
            if not new and now > pickup:
                if reopened:
                    return RunResult(ok=False, session_id=tid,
                                     message="the Codex app did not pick up the message; is it open and signed in?")
                self._log(log_path, "the app has not started the turn yet; showing the thread again")
                self._show(tid, log_path)
                reopened, pickup = True, now + self.PICKUP_S
            if now - start > timeout_s:
                return RunResult(ok=False, timed_out=True, session_id=tid,
                                 message=f"no result within {timeout_s / 60:.0f} minutes")
            time.sleep(2)

    def _gui_result(self, tid, turn, started_at):
        res = RunResult(ok=turn.status == "completed", session_id=tid, returncode=0)
        res.text = codexdb.final_text(tid, turn.id)
        if not res.ok:
            res.message = turn.error[:500] or f"the turn ended as {turn.status}"
            if turn.error_code in codexdb.LIMIT_CODES:
                res.limited = True
        self._usage(res, tid, started_at)
        self._backstop(res, [turn.error])
        return res


def read_codex_snapshot(path: str) -> Optional[dict]:
    snap = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "rate_limits" not in line:
                    continue
                for o in iter_json_objects(line):
                    pl = o.get("payload") if isinstance(o.get("payload"), dict) else o
                    rl = pl.get("rate_limits")
                    if isinstance(rl, dict):
                        snap = rl
    except OSError:
        return None
    return snap


def apply_codex_snapshot(snap: dict, res: RunResult, ref_time: float) -> None:
    for key in ("primary", "secondary"):
        w = snap.get(key)
        if not isinstance(w, dict):
            continue
        mins = _num(w.get("window_minutes"))
        name = CODEX_WINDOWS.get(int(mins), f"{int(mins)}m") if mins else key
        ra = _num(w.get("resets_at"))
        if ra is None and _num(w.get("resets_in_seconds")) is not None:
            ra = ref_time + _num(w.get("resets_in_seconds"))
        res.windows[name] = Window(_num(w.get("used_percent")), ra)
    full = [w for w in res.windows.values() if w.used is not None and w.used >= 100]
    if snap.get("rate_limit_reached_type") or full:
        res.limited = True
        pool = full or sorted(res.windows.values(), key=lambda w: -(w.used or 0))[:1]
        resets = [w.resets_at for w in pool if w.resets_at]
        if resets:
            res.resets_at = max(resets)


class CustomAgent(Agent):
    """Any other CLI: success is exit code 0, limits come from limit_pattern in its output."""

    def build(self, prompt, resume=None):  # custom CLIs have no sessions to resume
        if self.cfg.get("prompt_via", "stdin") == "arg":
            args = [a.replace("{prompt}", prompt) for a in self.args]
            if not any("{prompt}" in a for a in self.args):
                args.append(prompt)
            return self._exe() + args, None
        return self._exe() + self.args, prompt

    def parse(self, out):
        res = RunResult(ok=out.returncode == 0, text="".join(out.lines)[-4000:])
        pat = self.cfg.get("limit_pattern")
        regex = re.compile(pat, re.I) if pat else LIMIT_RE
        self._backstop(res, ["".join(out.lines), out.stderr], regex)
        return res


AGENT_TYPES = {"claude": ClaudeAgent, "codex": CodexAgent, "custom": CustomAgent}


def make_agent(name: str, cfg: dict) -> Agent:
    kind = cfg.get("type", "custom")
    if kind not in AGENT_TYPES:
        raise ConfigError(f"agent '{name}' has unknown type '{kind}' (use claude, codex or custom)")
    return AGENT_TYPES[kind](name, cfg)
