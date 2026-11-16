"""Stands in for `claude -p --output-format stream-json`. Each call uses FAKE_STEP of the
5-hour window; at 100% it answers with a rejected rate_limit_event until the window resets.
FAKE_LEGACY=1 leaves out the per-window utilization, like older CLI versions.
FAKE_PARTIAL=1 leaves a half-done file behind when it is cut off."""

import math
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_common import (do_task, done_suffix, emit, env_float, load_state, record_prompt,  # noqa: E402
                         save_state, session_reply)


def main():
    prompt = sys.stdin.read()
    argv = sys.argv[1:]
    resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
    sid = resume or str(uuid.uuid4())
    record_prompt("claude", resume, sid, prompt)
    path = os.environ["FAKE_CLAUDE_STATE"]
    step, reset = env_float("FAKE_STEP", 0.5), env_float("FAKE_RESET", 5)
    st = load_state(path, {"util": 0.0, "resets_at": 0})
    now = time.time()
    if st["resets_at"] and now >= st["resets_at"]:
        st = {"util": 0.0, "resets_at": 0}
    if not st["resets_at"]:
        st["resets_at"] = now + reset
    resets = math.ceil(st["resets_at"])
    emit({"type": "system", "subtype": "init", "session_id": sid})
    if st["util"] >= 1.0:
        if os.environ.get("FAKE_PARTIAL") == "1":
            do_task("claude", partial=True)
        emit({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": resets, "rateLimitType": "five_hour", "isUsingOverage": False}})
        emit({"type": "result", "subtype": "success", "is_error": True,
              "result": "You've hit your limit · resets 5pm", "session_id": sid})
        save_state(path, st)
        return 1
    text = session_reply("claude", prompt)
    if text is None:
        text = f"Did {do_task('claude')}" + done_suffix(prompt)
    st["util"] += step
    info = {"status": "allowed", "resetsAt": resets, "rateLimitType": "five_hour", "isUsingOverage": False}
    if os.environ.get("FAKE_LEGACY") != "1":
        info["unifiedWindows"] = {"five_hour": {"utilization": st["util"], "resetsAt": resets},
                                  "seven_day": {"utilization": 0.05, "resetsAt": math.ceil(now + 7 * 86400)}}
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    emit({"type": "rate_limit_event", "rate_limit_info": info})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid})
    save_state(path, st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
