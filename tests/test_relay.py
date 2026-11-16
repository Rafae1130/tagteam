"""End-to-end runs of the supervisor against fake agent CLIs, in a temp git repo."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from tagteam import tasks as tasklib  # noqa: E402
from tagteam.config import build_config  # noqa: E402
from tagteam.lock import project_lock  # noqa: E402
from tagteam.supervisor import EXIT_BLOCKED, EXIT_DONE, EXIT_ERROR, Supervisor  # noqa: E402


class Relay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tagteam-test-")
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj)
        subprocess.run(["git", "init", "-q"], cwd=self.proj, check=True)
        self.saved_env = dict(os.environ)
        os.environ.update({
            "FAKE_CLAUDE_STATE": os.path.join(self.tmp, "claude.json"),
            "FAKE_CODEX_STATE": os.path.join(self.tmp, "codex.json"),
            "CODEX_HOME": os.path.join(self.tmp, "codex-home"),
            "TAGTEAM_HOME": os.path.join(self.tmp, "tagteam-home"),
        })

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tasks(self, n):
        with open(os.path.join(self.proj, "TASKS.md"), "w", encoding="utf-8") as f:
            f.write("# Tasks\n\n" + "".join(f"- [ ] task {i + 1}\n" for i in range(n)))

    def config(self, solo=False, **over):
        cfg = build_config(None, {})
        cfg["agents"]["claude"].update(command=[sys.executable, os.path.join(HERE, "fake_claude.py")], args=[])
        cfg["agents"]["codex"].update(command=[sys.executable, os.path.join(HERE, "fake_codex.py")], args=[])
        cfg.update(reset_buffer_seconds=0, keep_awake=False, unknown_reset_retry_minutes=0.05)
        if solo:
            cfg["secondary"] = ""
        cfg.update(over)
        return cfg

    def run_supervisor(self, cfg):
        lines = []
        rc = Supervisor(self.proj, cfg, echo=lines.append).run()
        return rc, "\n".join(lines)

    def worklog(self):
        try:
            with open(os.path.join(self.proj, "work.log"), encoding="utf-8") as f:
                return [ln.split(":")[0] for ln in f if ln.strip()]
        except OSError:
            return []

    def git_subjects(self):
        r = subprocess.run(["git", "log", "--format=%s"], cwd=self.proj, capture_output=True, text=True)
        return r.stdout.splitlines()

    def open_tasks(self):
        return tasklib.unchecked(os.path.join(self.proj, "TASKS.md"))

    def test_handoff_and_handback(self):
        # Claude's 5-hour window fills after two tasks (90% threshold) and resets 5 s after the
        # first call; Codex takes ~1 s per task, so Claude must come back before the list ends.
        os.environ.update(FAKE_STEP="0.5", FAKE_RESET="5", FAKE_CODEX_DELAY="1", FAKE_CODEX_STEP="5")
        self.tasks(9)
        rc, log = self.run_supervisor(self.config())
        seq = self.worklog()
        self.assertEqual(rc, EXIT_DONE, log)
        self.assertEqual(self.open_tasks(), [], log)
        self.assertEqual(seq[0], "claude", log)
        first_codex = seq.index("codex")
        self.assertIn("claude", seq[first_codex:], f"no hand-back: {seq}\n{log}")
        self.assertIn("Handing off to codex", log)
        self.assertIn("Handing back to claude", log)
        self.assertGreaterEqual(len(self.git_subjects()), 9)
        with open(os.path.join(self.proj, ".tagteam", "live.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["phase"], "done")

    def test_single_agent_waits_for_reset(self):
        # No percentages (older CLI): Claude runs into the hard limit mid-task, leaves partial work,
        # tagteam commits it as WIP, sleeps until the reset and finishes the list.
        os.environ.update(FAKE_STEP="0.5", FAKE_RESET="3", FAKE_LEGACY="1", FAKE_PARTIAL="1")
        self.tasks(4)
        start = time.time()
        rc, log = self.run_supervisor(self.config(solo=True))
        self.assertEqual(rc, EXIT_DONE, log)
        self.assertEqual(self.open_tasks(), [], log)
        self.assertGreaterEqual(time.time() - start, 2.5, log)
        self.assertIn("Sleeping until", log)
        self.assertTrue(any("WIP" in s for s in self.git_subjects()), self.git_subjects())
        self.assertEqual(set(self.worklog()), {"claude"})

    def test_no_wait_exits_when_limited(self):
        os.environ.update(FAKE_STEP="0.5", FAKE_RESET="60", FAKE_LEGACY="1")
        self.tasks(4)
        rc, log = self.run_supervisor(self.config(solo=True, wait_for_reset=False))
        self.assertEqual(rc, EXIT_BLOCKED, log)
        self.assertEqual(len(self.open_tasks()), 2, log)

    def test_both_limited_then_main_first(self):
        # Both agents hit limits; tagteam sleeps, and after the reset the main agent goes first.
        os.environ.update(FAKE_STEP="0.5", FAKE_RESET="3", FAKE_CODEX_STEP="100", FAKE_CODEX_RESET="60")
        self.tasks(4)
        rc, log = self.run_supervisor(self.config())
        seq = self.worklog()
        self.assertEqual(rc, EXIT_DONE, log)
        self.assertEqual(seq[:3], ["claude", "claude", "codex"], log)
        self.assertEqual(seq[3], "claude", log)
        self.assertIn("Sleeping until", log)

    def test_second_run_is_refused(self):
        self.tasks(1)
        state_dir = os.path.join(self.proj, ".tagteam")
        os.makedirs(state_dir, exist_ok=True)
        with project_lock(state_dir):  # stands in for a run that is still going
            rc, log = self.run_supervisor(self.config(solo=True))
        self.assertEqual(rc, EXIT_ERROR, log)
        self.assertIn("already working", log)
        self.assertEqual(self.worklog(), [])
        self.assertEqual(len(self.open_tasks()), 1)

    def test_stale_lock_is_replaced(self):
        self.tasks(1)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        state_dir = os.path.join(self.proj, ".tagteam")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "run.lock"), "w", encoding="utf-8") as f:
            json.dump({"pid": dead.pid, "host": socket.gethostname(), "started": 0}, f)
        rc, log = self.run_supervisor(self.config(solo=True))
        self.assertEqual(rc, EXIT_DONE, log)
        self.assertFalse(os.path.exists(os.path.join(state_dir, "run.lock")))


if __name__ == "__main__":
    unittest.main()
