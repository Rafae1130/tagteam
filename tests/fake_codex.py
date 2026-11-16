"""Stands in for `codex exec --json`. Writes a session rollout file with a token_count
rate_limits snapshot under $CODEX_HOME (a temp folder in tests), like the real CLI."""

import math
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_common import (do_task, done_suffix, emit, env_float, load_state, record_prompt,  # noqa: E402
                         save_state, session_reply)
import json  # noqa: E402


def main():
    prompt = sys.stdin.read()
    argv = sys.argv[1:]
    resume = argv[argv.index("resume") + 1] if "resume" in argv else None
    home, path = os.environ["CODEX_HOME"], os.environ["FAKE_CODEX_STATE"]
    step, reset = env_float("FAKE_CODEX_STEP", 10), env_float("FAKE_CODEX_RESET", 3600)
    delay = env_float("FAKE_CODEX_DELAY", 0)
    st = load_state(path, {"used": 0.0, "resets_at": 0})
    now = time.time()
    if st["resets_at"] and now >= st["resets_at"]:
        st = {"used": 0.0, "resets_at": 0}
    if not st["resets_at"]:
        st["resets_at"] = now + reset
    tid = resume or str(uuid.uuid4())
    record_prompt("codex", resume, tid, prompt)
    emit({"type": "thread.started", "thread_id": tid})
    emit({"type": "turn.started"})
    limited = st["used"] >= 100
    text = None
    if not limited:
        time.sleep(delay)
        text = session_reply("codex", prompt)
        if text is None:
            text = f"Did {do_task('codex')}" + done_suffix(prompt)
        st["used"] += step
    day = os.path.join(home, "sessions", time.strftime("%Y"), time.strftime("%m"), time.strftime("%d"))
    os.makedirs(day, exist_ok=True)
    snap = {"limit_id": "codex", "primary": {"used_percent": min(st["used"], 100.0), "window_minutes": 300,
                                             "resets_at": math.ceil(st["resets_at"])},
            "secondary": {"used_percent": 2.0, "window_minutes": 10080, "resets_at": math.ceil(now + 7 * 86400)},
            "rate_limit_reached_type": "primary" if limited else None}
    with open(os.path.join(day, f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S')}-{tid}.jsonl"), "w") as f:
        f.write(json.dumps({"type": "session_meta", "payload": {"id": tid}}) + "\n")
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": None,
                                                              "rate_limits": snap}}) + "\n")
    save_state(path, st)
    if limited:
        emit({"type": "error", "message": "You've hit your usage limit. Try again in 3 minutes."})
        emit({"type": "turn.failed", "error": {"message": "usage limit reached"}})
        return 1
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
