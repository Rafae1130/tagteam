"""The pet's status reading and its fail-safe checks. No window is opened here."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tagteam import pet, registry  # noqa: E402
from tagteam.config import build_config  # noqa: E402
from tagteam.supervisor import Supervisor  # noqa: E402


class PetBars(unittest.TestCase):
    """The three bars: active runs on this computer, counted by kind, with each run's status."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tagteam-bars-")
        self.saved = os.environ.get("TAGTEAM_HOME")
        os.environ["TAGTEAM_HOME"] = os.path.join(self.tmp, "home")
        os.makedirs(registry.runs_dir())
        self.n = 0

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("TAGTEAM_HOME", None)
        else:
            os.environ["TAGTEAM_HOME"] = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_entry(self, kind, folder, title="", pid=None, live=None, state=None):
        """An active run as tagteam registers it, with its folder's live.json and state.json."""
        root = os.path.join(self.tmp, folder)
        os.makedirs(os.path.join(root, ".tagteam"), exist_ok=True)
        pid = os.getpid() if pid is None else pid
        if live is not None:
            with open(os.path.join(root, ".tagteam", "live.json"), "w", encoding="utf-8") as f:
                json.dump(dict(live, pid=pid), f)
        if state is not None:
            with open(os.path.join(root, ".tagteam", "state.json"), "w", encoding="utf-8") as f:
                json.dump(state, f)
        self.n += 1
        path = os.path.join(registry.runs_dir(), f"entry{self.n}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"kind": kind, "root": root, "title": title, "pid": pid, "host": socket.gethostname(),
                       "started": self.n}, f)
        return path

    def test_counts_and_status_per_kind(self):
        far = 4102444800  # 2100: a window that has not reset
        self.run_entry("handover", "blogs", "Fix the parser",
                       live={"phase": "working", "agent": "claude", "order": ["claude", "codex"]},
                       state={"agents": {"claude": {"windows": {"5h": {"used": 41.0, "resets_at": far}}},
                                         "codex": {"windows": {"30d": {"used": 3.0, "resets_at": far}}}}})
        self.run_entry("handover", "site", "Write the docs", live={"phase": "sleeping", "until": far,
                                                                   "order": ["claude", "codex"]})
        self.run_entry("discussion", "research", "Which database?",
                       live={"phase": "working", "agent": "codex", "task": "discussion: independent research"})
        view = pet.overview()
        self.assertEqual({k: len(v) for k, v in view.items()}, {"discussion": 1, "handover": 2, "wakeup": 0})
        work, sleep = view["handover"]
        self.assertEqual((work["state"], work["color"], work["text"]), ("working", pet.agent_color("claude"),
                                                                        "Claude is working"))
        self.assertEqual(work["usage"], "claude 41%, codex 3%")
        self.assertEqual((sleep["state"], sleep["color"]), ("sleeping", pet.SLEEP_COLOR))
        self.assertTrue(sleep["text"].startswith("sleeping until "))
        self.assertEqual(view["discussion"][0]["text"], "Codex: independent research")
        tip = pet.tip_text("handover", view["handover"])
        for part in ("Handover: 2 running", "blogs", "Fix the parser", "Claude is working", "claude 41%",
                     "site", "sleeping until"):
            self.assertIn(part, tip)
        self.assertEqual(pet.tip_text("wakeup", []), "No wakeup running")

    def test_killed_runs_are_dropped(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        path = self.run_entry("wakeup", "gone", pid=dead.pid, live={"phase": "working", "agent": "claude"})
        self.assertEqual(pet.overview()["wakeup"], [])
        self.assertFalse(os.path.exists(path))

    def test_live_file_of_an_earlier_run_is_ignored(self):
        self.run_entry("wakeup", "proj", live={"phase": "working", "agent": "claude"})
        with open(os.path.join(self.tmp, "proj", ".tagteam", "live.json"), "w", encoding="utf-8") as f:
            json.dump({"phase": "working", "agent": "codex", "pid": -5}, f)
        self.assertEqual(pet.overview()["wakeup"][0]["text"], "starting")

    def test_registered_lasts_as_long_as_the_run(self):
        with registry.registered("handover", self.tmp, "t"):
            self.assertEqual([e["kind"] for e in registry.active()], ["handover"])
        self.assertEqual(registry.active(), [])


class PetStatus(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp(prefix="tagteam-pet-")
        with open(os.path.join(self.proj, "TASKS.md"), "w", encoding="utf-8") as f:
            f.write("- [x] one\n- [ ] two\n- [ ] three\n")
        self.sup = Supervisor(self.proj, build_config(None, {}), echo=lambda s: None)

    def tearDown(self):
        shutil.rmtree(self.proj, ignore_errors=True)

    def live(self, **data):
        with open(os.path.join(self.proj, ".tagteam", "live.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_idle(self):
        s = pet.snapshot(self.sup)
        self.assertEqual(s["phase"], "idle")
        self.assertEqual((s["tasks_done"], s["tasks_total"]), (1, 3))
        self.assertEqual([a["name"] for a in s["agents"]], ["claude", "codex"])

    def test_working(self):
        self.live(phase="working", agent="codex", task="two", pid=os.getpid())
        s = pet.snapshot(self.sup)
        self.assertEqual(s["phase"], "working")
        self.assertEqual([a["working"] for a in s["agents"]], [False, True])

    def test_dead_run_shows_stopped(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.live(phase="working", agent="claude", task="two", pid=dead.pid)
        self.assertEqual(pet.snapshot(self.sup)["phase"], "stopped")

    def test_solo_run_order(self):
        self.live(phase="sleeping", until=0, pid=os.getpid(), order=["claude"])
        s = pet.snapshot(self.sup)
        self.assertEqual([a["name"] for a in s["agents"]], ["claude"])

    def test_fail_safe_messages(self):
        ok, why = pet.check_available()
        self.assertIsInstance(ok, bool)
        if not ok:
            self.assertIn("keeps working", why)
        self.assertTrue(pet.install_hint())


class PythonVersionGuard(unittest.TestCase):
    def test_old_python_gets_one_clear_line(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = "import sys; sys.version_info = (3, 8, 0, 'final', 0); import tagteam"
        r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("needs Python 3.9 or newer, and this is Python 3.8", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
