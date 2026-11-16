"""Two agents discuss a question until they agree, then hand the user one joint answer.

Each researches the question on its own first. Then they swap answers and reply to each other's
comments, one agent at a time, until both say they agree or the round limit is reached. One writes
the final decision or plan and the other signs it off. Both run read-only: they may read files and
search, not change anything. Each keeps its own session, so it remembers its research between
steps. The whole exchange is saved under .tagteam/discussions/.
"""

import os
import re
import time
from typing import List, Optional

from . import registry
from .agents import make_agent
from .config import ConfigError
from .keepawake import keep_awake
from .lock import LockHeld, project_lock
from .sessions import clip
from .supervisor import EXIT_BLOCKED, EXIT_DONE, EXIT_ERROR, EXIT_INTERRUPTED, write_json
from .textparse import fmt_dur, fmt_time

VERDICT_RE = re.compile(r"VERDICT:\s*\**\s*(AGREE|DISAGREE)", re.I)

RESEARCH = """\
You are {me} in a structured discussion with another AI agent, {other}. The topic:

{topic}

First research it on your own. Look at the files in this folder if they are relevant, and use \
any read-only tools you have (reading, searching). Do not create, edit or delete files. Then write \
your answer with these sections: Answer (your conclusion or plan), Reasoning, Evidence (files, \
sources and facts you checked), Risks and open questions. {other} is researching the same topic \
separately; you will compare answers next."""

VERDICT_RULE = """End with exactly one line: "VERDICT: AGREE" if you and {other} now agree on the \
conclusion with no material disagreement left, otherwise "VERDICT: DISAGREE"."""

REVIEW = """\
Here is {other}'s independent answer{also}:

<<<
{theirs}
>>>

Compare it with yours. Say where you agree, where you disagree and why (with evidence), and what \
either answer missed. Then write your updated position in full under "Updated position". """ + VERDICT_RULE

REPLY = """\
{other} replied:

<<<
{theirs}
>>>

Answer its comments: accept the points that are right, push back with evidence on the ones that \
are not, and write your updated position in full under "Updated position". """ + VERDICT_RULE

FINAL = """\
The discussion is over{status}. Write the final answer for the user as the joint result of you \
and {other}:
1. Decision or plan: concrete and actionable (numbered steps if it is a plan).
2. Why: the key reasons and evidence.
3. Where you and {other} disagreed, and how it was settled.
4. Remaining risks and open questions{open}.
Write only the final answer, with no preamble."""

SIGNOFF = """\
{other} wrote this final answer for the user, as your joint result:

<<<
{final}
>>>

If it fairly reflects what you agreed, reply with APPROVE on the first line. Otherwise reply with \
CHANGES on the first line, followed by the corrections that are needed."""

REVISE = """\
{other} asked for these corrections to the final answer:

<<<
{changes}
>>>

Write the corrected final answer in full, with no preamble."""


class _Stop(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _drop(args: List[str], flags, singles=()) -> List[str]:
    """Remove '--flag value', '--flag=value' and the single flags from an argument list."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in flags:
            skip = True
        elif a in singles or any(a.startswith(f + "=") for f in flags):
            pass
        else:
            out.append(a)
    return out


def readonly_cfg(cfg: dict) -> dict:
    """The agent's config with permissions that allow reading and searching but no changes."""
    c = dict(cfg)
    args = [str(a) for a in (cfg.get("args") or [])]
    kind = cfg.get("type")
    if kind == "claude":
        args = _drop(args, ("--permission-mode", "--allowedTools", "--allowed-tools"),
                     ("--dangerously-skip-permissions",))
        args += ["--permission-mode", "default", "--allowedTools", "Read,Grep,Glob,WebSearch,WebFetch",
                 "--disallowedTools", "Edit,Write,NotebookEdit"]
    elif kind == "codex":
        args = _drop(args, ("--sandbox", "-s"), ("--full-auto", "--dangerously-bypass-approvals-and-sandbox"))
        args += ["--sandbox", "read-only"]
    c["args"] = args
    return c


def verdict(text: str) -> bool:
    found = VERDICT_RE.findall(text or "")
    return bool(found) and found[-1].upper() == "AGREE"


def run_discussion(root: str, cfg: dict, topic: str, agents: Optional[List[str]] = None, rounds: int = 4,
                   wait: Optional[bool] = None, echo=print) -> int:
    root = os.path.abspath(root)
    names = agents or [cfg["main"], cfg.get("secondary") or cfg["main"]]
    if len(names) != 2:
        raise ConfigError("a discussion needs exactly two agents, e.g. --agents claude,codex")
    for n in names:
        if n not in cfg["agents"]:
            raise ConfigError(f"no agent called '{n}' in the config")
    labels = list(names) if names[0] != names[1] else [names[0] + "-A", names[0] + "-B"]
    ags = [make_agent(n, readonly_cfg(cfg["agents"][n])) for n in names]
    for n, ag in zip(names, ags):
        if not ag.available():
            raise ConfigError(f"agent '{n}': command '{ag.base[0]}' was not found")
    wait = cfg["wait_for_reset"] if wait is None else wait
    state_dir = os.path.join(root, ".tagteam")
    logs = os.path.join(state_dir, "logs")
    ddir = os.path.join(state_dir, "discussions")
    os.makedirs(logs, exist_ok=True)
    os.makedirs(ddir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:40] or "topic"
    base = os.path.join(ddir, f"{stamp}-{slug}")
    stop_path = os.path.join(state_dir, "stop")
    timeout = float(cfg["task_timeout_minutes"]) * 60
    sids: List[Optional[str]] = [None, None]
    transcript = [f"# Discussion\n\n{topic}\n\nAgents: {labels[0]} and {labels[1]}\n"]
    step = [0]

    def save():
        with open(base + ".md", "w", encoding="utf-8") as f:
            f.write("\n".join(transcript))

    def live(phase, **info):
        data = {"phase": phase, "pid": os.getpid(), "updated": time.time(), "switch": None,
                "order": list(dict.fromkeys(names))}
        data.update(info)
        write_json(os.path.join(state_dir, "live.json"), data, required=False)

    def stopped():
        return os.path.exists(stop_path)

    def ask(i: int, prompt: str, title: str) -> str:
        failures = 0
        while True:
            if stopped():
                raise KeyboardInterrupt
            step[0] += 1
            echo(f"[{time.strftime('%H:%M:%S')}] {labels[i]}: {title}")
            live("working", agent=names[i], task=f"discussion: {title}", since=time.time())
            log = os.path.join(logs, f"discuss-{stamp}-{step[0]:02d}-{labels[i]}.jsonl")
            res = ags[i].run(prompt, root, timeout, log, resume=sids[i], stop=stopped)
            sids[i] = res.session_id or sids[i]
            if res.ok and res.text.strip():
                transcript.append(f"\n## {labels[i]}: {title}\n\n{res.text.strip()}\n")
                save()
                return res.text.strip()
            if res.limited:
                if not wait:
                    raise _Stop(EXIT_BLOCKED, f"{labels[i]} is at its usage limit"
                                + (f" until {fmt_time(res.resets_at)}" if res.resets_at else ""))
                until = (res.resets_at if res.resets_at and res.resets_at > time.time()
                         else time.time() + float(cfg["unknown_reset_retry_minutes"]) * 60)
                until += float(cfg["reset_buffer_seconds"])
                echo(f"{labels[i]} is at its usage limit; waiting until {fmt_time(until)} "
                     f"({fmt_dur(until - time.time())}).")
                live("sleeping", until=until)
                while time.time() < until:
                    if stopped():
                        raise KeyboardInterrupt
                    time.sleep(min(2.0, max(0.05, until - time.time())))
                continue
            failures += 1
            if failures >= 2:
                raise _Stop(EXIT_ERROR, f"{labels[i]} failed twice: {res.message[:200] or 'no answer'} "
                                        f"(log: {log})")

    try:
        with project_lock(state_dir), registry.registered("discussion", root, clip(topic, 80)), \
                keep_awake(bool(cfg.get("keep_awake", True))):
            try:
                os.remove(stop_path)
            except OSError:
                pass
            save()
            a, b = labels
            first = [ask(0, RESEARCH.format(me=a, other=b, topic=topic), "independent research"),
                     ask(1, RESEARCH.format(me=b, other=a, topic=topic), "independent research")]
            msg_a = ask(0, REVIEW.format(other=b, also="", theirs=first[1]), "review (round 1)")
            msg_b = ask(1, REVIEW.format(other=a, also=", followed by its review of your answer",
                                         theirs=first[0] + "\n\n--- its review of your answer ---\n\n" + msg_a),
                        "review (round 1)")
            agree = [verdict(msg_a), verdict(msg_b)]
            rnd = 1
            while not all(agree) and rnd < rounds:
                rnd += 1
                msg_a = ask(0, REPLY.format(other=b, theirs=msg_b), f"reply (round {rnd})")
                agree[0] = verdict(msg_a)
                if all(agree):
                    break
                msg_b = ask(1, REPLY.format(other=a, theirs=msg_a), f"reply (round {rnd})")
                agree[1] = verdict(msg_b)
            agreed = all(agree)
            status = "" if agreed else f" without full agreement after {rnd} rounds"
            open_ = "" if agreed else ", including each point you still disagree on, with both positions"
            final = ask(0, FINAL.format(status=status, other=b, open=open_), "final answer")
            signoff = ask(1, SIGNOFF.format(other=a, final=final), "sign-off")
            approved = signoff.lstrip("*# ").upper().startswith("APPROVE")
            if not approved:
                final = ask(0, REVISE.format(other=b, changes=signoff), "revised final answer")
            outcome = ("both agents agreed" if agreed else f"no full agreement after {rnd} rounds") + \
                      ("" if approved else f"; revised after {b}'s corrections")
            with open(base + "-final.md", "w", encoding="utf-8") as f:
                f.write(f"# {clip(topic, 100)}\n\n{final}\n\n---\n{labels[0]} and {labels[1]}: {outcome}.\n")
            transcript.append(f"\n## Final answer ({outcome})\n\n{final}\n")
            save()
            live("done")
            echo(f"\n{final}\n\n({labels[0]} and {labels[1]}: {outcome}.)")
            echo(f"Final answer: {base}-final.md\nFull discussion: {base}.md")
            return EXIT_DONE
    except LockHeld as e:
        echo(f"{e} A discussion would run a second agent at the same time, so it did not start.")
        return EXIT_ERROR
    except _Stop as e:
        live("stopped", message=str(e))
        echo(f"Discussion stopped: {e}. What was said so far: {base}.md")
        return e.code
    except KeyboardInterrupt:
        try:
            os.remove(stop_path)
        except OSError:
            pass
        live("stopped", message="Stopped by user")
        echo(f"Discussion stopped by user. What was said so far: {base}.md")
        return EXIT_INTERRUPTED
