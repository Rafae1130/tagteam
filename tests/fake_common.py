"""Shared helpers for the fake agent CLIs used in tests. They only touch the current
directory (a temp project) and the state file / CODEX_HOME paths the test passes in."""

import json
import os
import re

TASK_RE = re.compile(r"^(\s*[-*+]\s+)\[ \](\s+)(.+)$", re.M)


def load_state(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return dict(default)


def save_state(path, st):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f)


def do_task(agent, partial=False):
    """Check off the first open task in TASKS.md, like a real agent would. Without a task list
    (session mode) just leave a trace of work, so there is something to commit.
    FAKE_IDLE=1 changes nothing, like a turn that only talks."""
    if os.environ.get("FAKE_IDLE") == "1":
        return "nothing"
    if not os.path.exists("TASKS.md"):
        with open("work.log", "a", encoding="utf-8") as f:
            f.write(f"{agent}: step\n")
        return "step"
    with open("TASKS.md", encoding="utf-8") as f:
        text = f.read()
    m = TASK_RE.search(text)
    if not m:
        return None
    task = m.group(3).strip()
    if partial:
        with open("partial.txt", "a", encoding="utf-8") as f:
            f.write(f"{agent} started {task}\n")
        return task
    text = text[:m.start()] + m.group(1) + "[x]" + m.group(2) + m.group(3) + text[m.end():]
    with open("TASKS.md", "w", encoding="utf-8") as f:
        f.write(text)
    with open("work.log", "a", encoding="utf-8") as f:
        f.write(f"{agent}: {task}\n")
    with open("HANDOFF.md", "w", encoding="utf-8") as f:
        f.write(f"{agent} finished: {task}\n")
    return task


def record_prompt(agent, resume, session, prompt):
    """Log what each call received, so tests can check which session every turn resumed."""
    path = os.environ.get("FAKE_PROMPT_LOG")
    if path:
        runs = []  # the kinds of tagteam runs registered for the pet while this call runs
        rdir = os.path.join(os.environ.get("TAGTEAM_HOME") or "-", "runs")
        for name in sorted(os.listdir(rdir)) if os.path.isdir(rdir) else []:
            try:
                with open(os.path.join(rdir, name), encoding="utf-8") as f:
                    runs.append(json.load(f)["kind"])
            except (OSError, ValueError, KeyError):
                pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"agent": agent, "resume": resume, "session": session, "prompt": prompt,
                                "runs": runs}) + "\n")


def session_reply(agent, prompt):
    """What a session-mode or discussion turn answers, or None for ordinary work."""
    if "structured discussion" in prompt:
        return f"{agent} research answer"
    if "VERDICT" in prompt:
        return f"{agent} comments.\nUpdated position: X.\nVERDICT: {os.environ.get('FAKE_VERDICT', 'AGREE')}"
    if "reply with APPROVE" in prompt:
        return os.environ.get("FAKE_SIGNOFF", "APPROVE")
    if "corrected final answer" in prompt:
        return f"REVISED PLAN by {agent}"
    if "final answer for the user" in prompt:
        return f"FINAL PLAN by {agent}"
    if "Do not start anything new" in prompt:  # asked for a handoff note
        with open("HANDOFF.md", "w", encoding="utf-8") as f:
            f.write(f"{agent} note\n")
        return "Wrote the handoff note."
    return None


def done_suffix(prompt):
    on = os.environ.get("FAKE_DONE_ON")
    return "\nTAGTEAM_DONE" if on and on in prompt else ""


def emit(obj):
    print(json.dumps(obj), flush=True)


def env_float(name, default):
    return float(os.environ.get(name, default))
