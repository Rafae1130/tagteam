"""The relay loop: pick whose turn it is, run one turn, commit, repeat.

Task mode works through TASKS.md, one task per turn. Session mode continues one chosen Claude
Code or Codex conversation: the session's own agent resumes it, the other agent takes over (with
a handoff note and the end of the conversation) before the first one runs out, and the original
session gets the work back after its limit resets. Only one agent runs at any time.
"""

import json
import os
import subprocess
import time
from typing import Optional, Tuple

from . import gitops, registry, sessions, tasks
from .agents import RunResult, make_agent
from .config import ConfigError
from .keepawake import keep_awake
from .lock import LockHeld, project_lock
from .textparse import fmt_dur, fmt_time

EXIT_DONE, EXIT_ERROR, EXIT_MAX_ITER, EXIT_BLOCKED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130
DONE_MARK = "TAGTEAM_DONE"


def says_done(text: str) -> bool:
    """True when the reply ends with the done mark on a line of its own. A real Claude reply said
    "I'm not ending with TAGTEAM_DONE", so finding the word anywhere is not enough."""
    lines = [ln.strip().strip("*`_. ") for ln in (text or "").splitlines() if ln.strip()]
    return bool(lines) and lines[-1] == DONE_MARK
DEFAULT_INSTRUCTION = "Continue the work from this conversation where it left off."

DEFAULT_PROMPT = """\
You are taking a turn as {agent} in a tagteam relay: AI coding agents take turns on this \
repository, and another agent may continue after you. The files here are the only memory \
you share with them.

{note}Steps:
1. Read {tasks_file} (the task list) and {handoff_file} (notes from the previous turn) if it exists.
2. Work on the first unchecked task, the first line starting with "- [ ]". Do only that one task.
3. When it is finished, change its "- [ ]" to "- [x]" in {tasks_file}. If you cannot finish it, \
leave it unchecked and say why in {handoff_file}.
4. Overwrite {handoff_file} with a short note for the next agent: what you did, which files \
changed, decisions you made, open problems, and what should happen next.
5. Do not commit; tagteam commits your changes after your turn.
"""

CHECK_PROMPT = "Reply with the single word OK. Do not use any tools."

SESSION_RULES = """\
You are running unattended under tagteam: nobody is watching this chat, so do not ask \
questions; make sensible choices and mention them in your reply. tagteam commits your changes \
after each turn, and another agent may continue after you, so keep {handoff_file} up to date \
with what the next agent must know. Stop at a good checkpoint. Only when the whole goal of this \
session is finished, including any part meant for another agent, end your reply with a line \
containing only {done}. If anything is still left for anyone, do not write {done}; tagteam passes \
the session on."""

START_PROMPT = "{instruction}\n\n{rules}"

CONTINUE_PROMPT = "Continue.\n\n{rules}"

HANDBACK_PROMPT = """\
While you were paused, {other} continued this work in the same folder. Read {handoff_file} \
for its notes{since}, then continue.

{rules}"""

TAKEOVER_PROMPT = """\
You are taking over a coding session from {other}, which is paused. Continue its work in this \
folder. {note}

{rules}

## The user's original request
{goal}

## The end of {other}'s conversation
{tail}"""

NOTE_PROMPT = """\
{other} is taking over this work now. Do not start anything new. Overwrite {handoff_file} with \
a handoff note for {other}: the goal, what is done, what is in progress, what should happen \
next, and anything tricky (files, commands, decisions). Then reply with one short line."""


def write_json(path: str, data, required: bool = True) -> None:
    """Atomic write. On Windows a reader (the pet) holding the file open blocks the rename
    for a moment, so retry briefly before falling back."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    try:
        if required:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


class Supervisor:
    def __init__(self, root: str, cfg: dict, echo=print, session: Optional[sessions.Session] = None,
                 instruction: Optional[str] = None, wait_quiet: bool = False, quiet_s: float = 30,
                 first: Optional[str] = None, note_written: bool = False, handoff_at: Optional[float] = None,
                 kind: Optional[str] = None):
        """session: continue this conversation instead of working through TASKS.md.
        wait_quiet: first wait until the chat that owns the session has gone quiet.
        first: agent for the first turn. note_written: the chat already wrote the handoff note."""
        self.root = os.path.abspath(root)
        self.cfg = cfg
        self.echo = echo
        self.dir = os.path.join(self.root, ".tagteam")
        self.logs = os.path.join(self.dir, "logs")
        os.makedirs(self.logs, exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        self.stop_path = os.path.join(self.dir, "stop")
        self.state = self._load_state()
        self.session = session
        self.instruction = instruction or DEFAULT_INSTRUCTION
        self.custom_instruction = bool(instruction)
        self.wait_quiet, self.quiet_s = wait_quiet, quiet_s
        self.first = first
        self._note_written = note_written
        main, sec = cfg["main"], cfg.get("secondary") or None
        if session:
            main, sec = self._session_roles(session.agent, main, sec)
        self.main = main
        self.secondary = sec if sec and sec != main else None
        self.order = [self.main] + ([self.secondary] if self.secondary else [])
        # What the pet counts this run as: a handover when another agent can take over, else a wakeup.
        self.kind = kind or ("handover" if self.secondary else "wakeup")
        self.agents = {n: make_agent(n, cfg["agents"][n]) for n in self.order}
        if handoff_at is not None and self.secondary:
            cfg["agents"][self.main]["max_usage"] = float(handoff_at)
        self.tasks_path = os.path.join(self.root, cfg["tasks_file"])
        self._switch = None
        self._turned = set()
        if session:
            self._start_session(session)

    # ---- state and output -------------------------------------------------

    def _load_state(self) -> dict:
        st = {"iteration": 0, "current": None, "last_outcome": None, "agents": {}}
        try:
            with open(self.state_path, encoding="utf-8-sig") as f:
                st.update(json.load(f))
        except (OSError, ValueError):
            pass
        return st

    def save(self) -> None:
        write_json(self.state_path, self.state)

    def live(self, phase: str, **info) -> None:
        """What the pet window shows: phase, agent, task, wake-up time."""
        data = {"phase": phase, "pid": os.getpid(), "updated": time.time(), "switch": self._switch,
                "order": self.order, "session": self.state.get("focus") if self.session else None}
        data.update(info)
        write_json(os.path.join(self.dir, "live.json"), data, required=False)

    def say(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.echo(line)
        try:
            with open(os.path.join(self.dir, "tagteam.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def notify(self, msg: str) -> None:
        cmd = self.cfg.get("notify_command")
        if not cmd:
            return
        try:
            subprocess.Popen(list(cmd) + [msg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self.say(f"notify_command failed: {e}")

    def stop_requested(self) -> bool:
        """`tagteam stop` and the window's Stop button leave this file for the running tagteam."""
        return os.path.exists(self.stop_path)

    def _clear_stop(self) -> None:
        try:
            os.remove(self.stop_path)
        except OSError:
            pass

    # ---- sessions ---------------------------------------------------------

    def _session_roles(self, kind: str, main: str, sec: Optional[str]):
        """The session's own agent leads; the other configured agent (if any) is the secondary."""
        names = [n for n in (main, sec) if n] + [n for n in self.cfg["agents"] if n not in (main, sec)]
        focus = next((n for n in names if self.cfg["agents"][n].get("type") == kind), None)
        if not focus:
            raise ConfigError(f"this is a {kind} session, but no {kind} agent is configured")
        if not sec:
            other = None
        elif focus == sec:
            other = main
        elif focus == main:
            other = sec
        else:
            other = main
        return focus, other

    def _start_session(self, s: sessions.Session) -> None:
        if (self.state.get("focus") or {}).get("id") != s.id:
            self.state.update(current=None, last_outcome=None, sessions={}, left_at={})
        self.state["focus"] = {"agent": self.main, "type": s.agent, "id": s.id, "title": s.title, "path": s.path}
        self.state.setdefault("sessions", {})[self.main] = s.id
        self.state["session_done"] = False
        if s.limited_now():
            # The chat stopped at its limit: the other agent starts, with no note to ask for.
            self.record(self.main, RunResult(limited=True, resets_at=s.resets_at), time.time())
            self.state["current"], self.state["last_outcome"] = self.main, "limited"
        elif self._note_written:
            self.state["current"], self.state["last_outcome"] = self.main, "done"

    def session_prompt(self, name: str, prev: Optional[str]) -> Tuple[str, Optional[str]]:
        """The prompt for this turn and the session id to resume (None = a new session)."""
        hf = self.cfg["handoff_file"]
        rules = SESSION_RULES.format(handoff_file=hf, done=DONE_MARK)
        sid = self.state["sessions"].get(name)
        extra = f"\n\n## The user's instruction for tagteam\n{self.instruction}" if self.custom_instruction else ""
        if not sid:
            src = prev if prev and self.state["sessions"].get(prev) else self.main
            kind = self.cfg["agents"][src].get("type")
            s = sessions.by_id(self.state["sessions"][src], kind)
            foc = self.state["focus"]
            fs = s if s and s.id == foc["id"] else sessions.by_id(foc["id"], foc["type"])
            goal = (fs.first_prompt if fs else "") or "(not recorded)"
            if len(goal) > 4000:
                goal = goal[:4000] + "\n[...]"
            note = (f"Read {hf} first: it is {src}'s handoff note."
                    if os.path.isfile(os.path.join(self.root, hf))
                    else "There is no handoff note, so the conversation below is your context.")
            tail = sessions.dialog_tail(s) if s else "(not available)"
            return TAKEOVER_PROMPT.format(other=src, note=note, rules=rules, goal=goal, tail=tail) + extra, None
        if prev and prev != name:
            base = (self.state.get("left_at") or {}).get(name)
            since = f" and run `git log --stat {base}..HEAD` to see what changed" if base else ""
            return HANDBACK_PROMPT.format(other=prev, handoff_file=hf, since=since, rules=rules) + extra, sid
        if name in self._turned:
            return CONTINUE_PROMPT.format(rules=rules), sid
        return START_PROMPT.format(instruction=self.instruction, rules=rules), sid

    def _before_switch(self, prev: str, name: str) -> None:
        """Let the outgoing agent write a handoff note in its own session, if it still can."""
        if self._note_written:
            self._note_written = False
        elif self.state.get("last_outcome") != "limited" and self.state["sessions"].get(prev):
            self.say(f"Asking {prev} for a handoff note before {name} takes over.")
            prompt = NOTE_PROMPT.format(other=name, handoff_file=self.cfg["handoff_file"])
            self.turn(prev, "handoff note", prompt, self.state["sessions"][prev])
        if self.cfg.get("git_commit", True):
            self.state.setdefault("left_at", {})[prev] = gitops.head(self.root)

    # ---- usage bookkeeping ------------------------------------------------

    def max_usage(self, name: str) -> float:
        v = self.cfg["agents"].get(name, {}).get("max_usage")
        if v is not None:
            return float(v)
        return 95.0 if (name == self.main and self.secondary) else 100.0

    def _retry(self) -> float:
        return float(self.cfg["unknown_reset_retry_minutes"]) * 60

    def blocked(self, name: str, now: float) -> Tuple[Optional[float], str]:
        """When this agent may take a turn again (None = now) and why."""
        a = self.state["agents"].get(name) or {}
        limit = self.max_usage(name)
        until, reason = None, ""
        lu = a.get("limited_until")
        if lu and lu > now:
            until, reason = lu, "hit its usage limit"
        for wname, w in (a.get("windows") or {}).items():
            used, ra = w.get("used"), w.get("resets_at")
            if used is None or used < limit:
                continue
            if ra is None:
                ra = (a.get("updated") or now) + self._retry()
            if ra > now and (until is None or ra > until):
                until, reason = ra, f"{wname} window at {used:.0f}%"
        return until, reason

    def choose(self, now: float) -> Tuple[Optional[str], Optional[float]]:
        """The first agent in priority order that is free; otherwise the earliest time one will be."""
        earliest = None
        for name in self.order:
            until, _ = self.blocked(name, now)
            if until is None:
                return name, None
            earliest = until if earliest is None else min(earliest, until)
        return None, earliest

    def record(self, name: str, res: RunResult, now: float) -> None:
        a = self.state["agents"].setdefault(name, {"windows": {}, "limited_until": None})
        wins = a.setdefault("windows", {})
        for k in [k for k, w in wins.items() if w.get("resets_at") and w["resets_at"] <= now]:
            del wins[k]  # that window has rolled over
        for k, w in res.windows.items():
            old = wins.get(k) or {}
            same = old.get("resets_at") and w.resets_at and abs(old["resets_at"] - w.resets_at) < 120
            used = w.used if w.used is not None else (old.get("used") if same else None)
            wins[k] = {"used": used, "resets_at": w.resets_at or old.get("resets_at")}
        if res.limited:
            until = res.resets_at
            if not until or until <= now:
                until = now + self._retry()
            a["limited_until"] = until
        elif res.ok:
            a["limited_until"] = None
        a["updated"] = now

    def usage_summary(self, name: str) -> str:
        a = self.state["agents"].get(name) or {}
        now = time.time()
        parts = []
        for k, w in sorted((a.get("windows") or {}).items()):
            ra, used = w.get("resets_at"), w.get("used")
            if ra and ra <= now:
                continue
            s = f"{k} {used:.0f}%" if used is not None else f"{k} ?%"
            if ra:
                s += f" (resets {fmt_time(ra)})"
            parts.append(s)
        return ", ".join(parts)

    # ---- one turn ---------------------------------------------------------

    def prompt(self, name: str, prev: Optional[str]) -> str:
        lo = self.state.get("last_outcome")
        by = f" by {prev}" if prev else ""
        note = ""
        if lo == "limited":
            note = (f"Note: the previous turn{by} was cut off by a usage limit before it finished, so the "
                    "first unchecked task may be half done. Look at the current files (and git status / "
                    "git diff if you can run git) and continue that work instead of starting over.\n\n")
        elif lo == "failed":
            note = (f"Note: the previous turn{by} ended with an error, so the first unchecked task may be "
                    "partly done. Check the current files before you start.\n\n")
        tpl = DEFAULT_PROMPT
        pf = self.cfg.get("prompt_file")
        if pf:
            with open(os.path.join(self.root, pf), encoding="utf-8") as f:
                tpl = f.read()
        vals = {"agent": name, "note": note, "tasks_file": self.cfg["tasks_file"],
                "handoff_file": self.cfg["handoff_file"]}
        for k, v in vals.items():
            tpl = tpl.replace("{" + k + "}", v)
        return tpl

    def turn(self, name: str, task: str, prompt: str, resume: Optional[str] = None):
        self.state["iteration"] = int(self.state.get("iteration", 0)) + 1
        n = self.state["iteration"]
        self.say(f"#{n} {name}: {task}")
        self.live("working", agent=name, task=task, since=time.time())
        log_path = os.path.join(self.logs, f"{n:04d}-{name}.jsonl")
        timeout = float(self.cfg["task_timeout_minutes"]) * 60
        before = gitops.fingerprint(self.root)
        res = self.agents[name].run(prompt, self.root, timeout, log_path, resume=resume, stop=self.stop_requested)
        after = gitops.fingerprint(self.root)
        # Progress is any change to the files, so it also counts with commits off or without git.
        changed = before is None or after is None or before != after
        self.record(name, res, time.time())
        if self.session and res.session_id:
            self.state.setdefault("sessions", {})[name] = res.session_id
        if res.ok:
            outcome, label, detail = "done", "", "finished"
        elif res.limited:
            outcome, label, detail = "limited", " WIP (stopped by usage limit)", "stopped by a usage limit"
        else:
            outcome, label, detail = "failed", " (failed turn)", "failed"
        if res.timed_out:
            detail = "timed out"
        committed = False
        if self.cfg.get("git_commit", True):
            committed = gitops.commit_all(self.root, f"tagteam #{n} {name}{label}: {task}")
        line = f"#{n} {name} {detail}"
        usage = self.usage_summary(name)
        if usage:
            line += f" [{usage}]"
        if not res.ok and res.message:
            line += f": {res.message[:200]}"
        self.say(line)
        self.state["current"] = name
        self.state["last_outcome"] = outcome
        self.save()
        return res, committed or changed

    def wait_until(self, target: float) -> None:
        self.save()
        now = time.time()
        msg = f"Every agent is at its limit. Sleeping until {fmt_time(target)} ({fmt_dur(target - now)})."
        self.say(msg)
        self.notify("tagteam: " + msg)
        self.live("sleeping", until=target)
        last = now
        # Short sleeps against the wall clock, so a laptop that was suspended resumes correctly.
        while True:
            now = time.time()
            if now >= target:
                break
            if self.stop_requested():
                raise KeyboardInterrupt
            time.sleep(max(0.05, min(2.0, target - now)))
            if time.time() - last >= 1800:
                self.say(f"Still waiting, {fmt_dur(target - time.time())} left.")
                last = time.time()
        self.say("Usage window has reset; resuming.")

    # ---- entry points -----------------------------------------------------

    def check_setup(self) -> Optional[str]:
        if not self.session and not os.path.isfile(self.tasks_path):
            return (f"{self.cfg['tasks_file']} not found in {self.root}. Run 'tagteam init' or create it "
                    "with lines like '- [ ] first task'.")
        for n, ag in self.agents.items():
            if not ag.available():
                return (f"Agent '{n}': command '{ag.base[0]}' was not found. Install it, or set "
                        f"agents.{n}.command in tagteam.json.")
        if self.session and not self.agents[self.main].supports_resume:
            return f"Agent '{self.main}' cannot resume sessions (only claude and codex agents can)."
        if self.cfg.get("git_commit", True):
            if not gitops.available():
                return "git was not found. Install git, or set \"git_commit\": false in tagteam.json."
            if not gitops.is_repo(self.root):
                return (f"{self.root} is not a git repository. Run 'git init' (or 'tagteam init'), use "
                        "--no-commit, or set \"git_commit\": false in tagteam.json.")
            gitops.exclude(self.root)
        return None

    def run(self) -> int:
        err = self.check_setup()
        if err:
            self.say(err)
            return EXIT_ERROR
        try:
            title = (self.state.get("focus") or {}).get("title", "") if self.session else self.cfg["tasks_file"]
            with project_lock(self.dir), registry.registered(self.kind, self.root, title), \
                    keep_awake(bool(self.cfg.get("keep_awake", True))):
                self._clear_stop()
                return self._loop()
        except LockHeld as e:
            self.say(str(e))
            return EXIT_ERROR
        except KeyboardInterrupt:
            self._clear_stop()
            self.save()
            self.live("stopped", message="Stopped by user")
            self.say("Stopped by user. Run 'tagteam run' again to continue.")
            return EXIT_INTERRUPTED

    def _loop(self) -> int:
        failures = turns = 0
        max_turns = int(self.cfg["max_iterations"])
        max_fail = int(self.cfg["max_consecutive_failures"])
        buffer = float(self.cfg["reset_buffer_seconds"])
        mode = f"{self.main}, then {self.secondary}" if self.secondary else f"{self.main} only"
        waits = f"waiting for resets {'on' if self.cfg['wait_for_reset'] else 'off'}"
        if self.session:
            self.say(f'tagteam started on {self.session.agent} session {self.session.short} '
                     f'"{self.session.title}" in {self.root} ({mode}; {waits}).')
        else:
            self.say(f"tagteam started in {self.root} ({mode}; {waits}).")
        self.live("starting")
        if self.session and self.wait_quiet:
            self.say("Waiting until the chat that owns this session goes quiet...")
            self.live("waiting", message="Waiting for the chat to go quiet")
            sessions.wait_until_quiet(self.session, quiet_s=self.quiet_s, stop=self.stop_requested)
        first = self.first
        while True:
            if self.stop_requested():
                raise KeyboardInterrupt
            todo = None
            if self.session:
                if self.state.get("session_done"):
                    self.say("The agent reports that the session's work is finished.")
                    self.notify("tagteam: session finished")
                    self.live("done")
                    return EXIT_DONE
                label = self.state["focus"]["title"]
            else:
                todo = tasks.unchecked(self.tasks_path)
                if not todo:
                    self.say("All tasks are checked off.")
                    self.notify("tagteam: all tasks done")
                    self.live("done")
                    return EXIT_DONE
                label = todo[0]
            if turns >= max_turns:
                self.say(f"Stopped after {turns} turns (max_iterations).")
                self.live("stopped", message=f"Stopped after {turns} turns (max_iterations)")
                return EXIT_MAX_ITER
            now = time.time()
            name, until = self.choose(now)
            if first:
                if first in self.agents and self.blocked(first, now)[0] is None:
                    name, until = first, None
                first = None
            if name is None:
                if not self.cfg["wait_for_reset"]:
                    self.say(f"Every agent is at its limit; the earliest reset is {fmt_time(until)}. "
                             "Exiting because waiting for resets is off.")
                    self.live("stopped", message=f"All agents limited until {fmt_time(until)}")
                    return EXIT_BLOCKED
                self.wait_until(until + buffer)
                continue
            prev = self.state.get("current")
            if prev and prev != name and prev in self.agents:
                _, why = self.blocked(prev, now)
                verb = "Handing back to" if name == self.main else "Handing off to"
                msg = f"{verb} {name}" + (f" ({prev}: {why})" if why else "")
                self._switch = {"from": prev, "to": name, "at": now, "back": name == self.main}
                self.say(msg)
                self.notify("tagteam: " + msg)
                if self.session:
                    self._before_switch(prev, name)
            if self.session:
                prompt, resume = self.session_prompt(name, prev)
            else:
                prompt, resume = self.prompt(name, prev), None
            res, changed = self.turn(name, label, prompt, resume)
            self._turned.add(name)
            turns += 1
            if self.session:
                progressed = changed
                if res.ok and says_done(res.text):
                    self.state["session_done"] = True
                    self.save()
                    continue
            else:
                progressed = len(tasks.unchecked(self.tasks_path)) < len(todo)
            if res.ok:
                if progressed or changed:
                    failures = 0
                else:
                    failures += 1
                    self.say(f"{name} finished without changing anything.")
            elif not res.limited:
                failures += 1
            if failures >= max_fail:
                self.say(f"Stopping after {failures} turns in a row without progress. Logs: {self.logs}")
                self.notify("tagteam stopped: no progress")
                self.live("stopped", message="No progress; see .tagteam/logs")
                return EXIT_ERROR

    def status_text(self) -> str:
        now = time.time()
        foc = self.state.get("focus")
        lines = [f"Project: {self.root}",
                 f"Agents: {self.main}" + (f" (main), {self.secondary} (secondary)" if self.secondary else " (single)")]
        if foc and (self.session or not os.path.isfile(self.tasks_path)):
            lines.append(f"Session: {foc.get('type')} {str(foc.get('id'))[:8]} \"{foc.get('title')}\"")
        else:
            todo = tasks.unchecked(self.tasks_path)
            lines.append(f"Tasks left: {len(todo)}" + (f"; next: {todo[0]}" if todo else ""))
        for n in self.order:
            until, why = self.blocked(n, now)
            st = f"blocked until {fmt_time(until)} ({why})" if until else "available"
            lines.append(f"{n}: {st}; usage: {self.usage_summary(n) or 'not seen yet'}")
        if self.state.get("current"):
            lines.append(f"Last turn: #{self.state.get('iteration')} by {self.state['current']} "
                         f"({self.state.get('last_outcome')})")
        return "\n".join(lines)

    def check_agent(self, name: str) -> int:
        if name not in self.cfg["agents"]:
            self.say(f"No agent called '{name}' in the config.")
            return EXIT_ERROR
        ag = self.agents.get(name) or make_agent(name, self.cfg["agents"][name])
        if not ag.available():
            self.say(f"Command '{ag.base[0]}' for agent '{name}' was not found.")
            return EXIT_ERROR
        log = os.path.join(self.logs, f"check-{name}.jsonl")
        self.say(f"Sending {name} a one-line test prompt...")
        try:
            with project_lock(self.dir):
                res = ag.run(CHECK_PROMPT, self.root, 300, log)
        except LockHeld as e:
            self.say(str(e))
            return EXIT_ERROR
        self.record(name, res, time.time())
        self.save()
        state = "ok" if res.ok else "failed"
        if res.limited:
            state += f", at its limit until {fmt_time(res.resets_at)}"
        self.say(f"{name}: {state}; usage: {self.usage_summary(name) or 'not reported'}")
        if not res.ok and res.message:
            self.say(res.message[:300])
        self.say(f"Raw output: {log}")
        return 0 if res.ok or res.limited else EXIT_ERROR
