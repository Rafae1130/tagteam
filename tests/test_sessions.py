"""Session mode, the handoff/wakeup commands, discussions and the window, against fake agents
and fake Claude/Codex session files in temp folders (CLAUDE_CONFIG_DIR and CODEX_HOME point there)."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tagteam import sessions  # noqa: E402
from tagteam.config import build_config  # noqa: E402
from tagteam.discuss import readonly_cfg, run_discussion, verdict  # noqa: E402
from tagteam.gui import phase_text, resolve_drop  # noqa: E402
from tagteam.supervisor import DEFAULT_INSTRUCTION, EXIT_DONE, EXIT_INTERRUPTED, Supervisor  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


def _enc(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def _line(obj):
    return json.dumps(obj, separators=(",", ":")) + "\n"  # Claude Code writes compact JSON


def claude_session(home, cwd, sid, items, title=None, age=None):
    d = os.path.join(home, "projects", _enc(cwd))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, sid + ".jsonl")
    base = {"cwd": cwd, "sessionId": sid}
    with open(path, "w", encoding="utf-8") as f:
        for kind, text in items:
            if kind == "user":
                obj = {"parentUuid": None, "isSidechain": False, "type": "user",
                       "message": {"role": "user", "content": text}}
            elif kind == "assistant":
                obj = {"parentUuid": "p", "isSidechain": False, "type": "assistant",
                       "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
            elif kind == "tool_use":
                obj = {"parentUuid": "p", "isSidechain": False, "type": "assistant",
                       "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                                     "input": {}}]}}
            else:  # tool_result
                obj = {"parentUuid": "p", "isSidechain": False, "type": "user",
                       "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                                "content": "ok"}]}}
            obj.update(base)
            f.write(_line(obj))
        if title:
            f.write(_line({"type": "ai-title", "aiTitle": title, "sessionId": sid}))
    if age is not None:
        t = time.time() - age
        os.utime(path, (t, t))
    return path


def codex_session(home, cwd, sid, items, snap=None, age=None):
    day = os.path.join(home, "sessions", "2026", "11", "13")
    os.makedirs(day, exist_ok=True)
    path = os.path.join(day, f"rollout-2026-11-13T10-00-00-{sid}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_line({"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}))
        for kind, text in items:
            f.write(_line({"type": "event_msg", "payload": {"type": kind, "message": text}}))
        if snap:
            f.write(_line({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": snap}}))
    if age is not None:
        t = time.time() - age
        os.utime(path, (t, t))
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tagteam-test-")
        self.saved_env = dict(os.environ)
        self.chome = os.path.join(self.tmp, "claude-home")
        self.xhome = os.path.join(self.tmp, "codex-home")
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj)
        self.log = os.path.join(self.tmp, "prompts.jsonl")
        os.environ.update({
            "CLAUDE_CONFIG_DIR": self.chome, "CODEX_HOME": self.xhome, "FAKE_PROMPT_LOG": self.log,
            "FAKE_CLAUDE_STATE": os.path.join(self.tmp, "claude.json"),
            "FAKE_CODEX_STATE": os.path.join(self.tmp, "codex.json"),
            "TAGTEAM_HOME": os.path.join(self.tmp, "tagteam-home"),
        })
        sessions._cache.clear()
        sessions._meta.clear()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def calls(self):
        try:
            with open(self.log, encoding="utf-8") as f:
                return [json.loads(ln) for ln in f if ln.strip()]
        except OSError:
            return []

    def config(self, solo=False):
        cfg = build_config(None, {})
        cfg["agents"]["claude"].update(command=[sys.executable, os.path.join(HERE, "fake_claude.py")], args=[])
        cfg["agents"]["codex"].update(command=[sys.executable, os.path.join(HERE, "fake_codex.py")], args=[])
        cfg.update(reset_buffer_seconds=0, keep_awake=False, unknown_reset_retry_minutes=0.05)
        if solo:
            cfg["secondary"] = ""
        return cfg

    def git_init(self):
        subprocess.run(["git", "init", "-q"], cwd=self.proj, check=True)


class Discovery(Base):
    def setUp(self):
        super().setUp()
        self.sub = os.path.join(self.proj, "sub")
        self.sib = self.proj + "-x"  # its folder name in ~/.claude/projects starts like proj's
        os.makedirs(self.sub)
        os.makedirs(self.sib)
        self.a = "aaaaaaaa-0000-0000-0000-000000000001"
        self.b = "bbbbbbbb-0000-0000-0000-000000000002"
        self.c = "cccccccc-0000-0000-0000-000000000003"
        self.d = "dddddddd-0000-0000-0000-000000000004"
        self.pa = claude_session(self.chome, self.proj, self.a, [
            ("user", "<command-name>/clear</command-name>"), ("user", "Build the widget"),
            ("assistant", "Working on it")], title="Widget work", age=300)
        claude_session(self.chome, self.sub, self.b, [("user", "Fix the tests"), ("assistant", "Done")], age=200)
        claude_session(self.chome, self.sib, self.c, [("user", "Other project")], age=100)
        codex_session(self.xhome, self.proj, self.d, [("user_message", "Write docs"), ("agent_message", "Docs written")],
                      age=50)

    def test_folder_and_subfolders_but_not_a_lookalike_sibling(self):
        found = {s.id: s for s in sessions.list_sessions(self.proj)}
        self.assertEqual(set(found), {self.a, self.b, self.d})
        a = found[self.a]
        self.assertEqual((a.title, a.first_prompt, a.prompts, a.turn_open), ("Widget work", "Build the widget", 1, False))
        self.assertEqual(a.last_reply, "Working on it")
        d = found[self.d]
        self.assertEqual((d.agent, d.title, d.last_reply), ("codex", "Write docs", "Docs written"))
        self.assertEqual([s.id for s in sessions.list_sessions(self.proj)][0], self.d)  # newest first

    def test_limit_notice_and_unfinished_turn(self):
        future = int(time.time()) + 3600
        e = claude_session(self.chome, self.proj, "eeeeeeee-0000-0000-0000-000000000005",
                           [("user", "Go"), ("assistant", f"Claude AI usage limit reached|{future}")])
        f = claude_session(self.chome, self.proj, "ffffffff-0000-0000-0000-000000000006",
                           [("user", "Go"), ("tool_use", ""), ("tool_result", "")])
        g = codex_session(self.xhome, self.proj, "99999999-0000-0000-0000-000000000007", [("user_message", "Go")],
                          snap={"primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": future}})
        se, sf, sg = sessions.read_claude(e), sessions.read_claude(f), sessions.read_codex(g)
        self.assertTrue(se.limited_now())
        self.assertEqual(se.resets_at, future)
        self.assertTrue(sf.turn_open)
        self.assertTrue(sg.limited_now())
        self.assertIn("resets", sessions.state_text(se))

    def test_find_session(self):
        self.assertEqual(sessions.find_session(self.proj).id, self.d)
        self.assertEqual(sessions.find_session(self.proj, "${CLAUDE_SESSION_ID}").id, self.d)
        self.assertEqual(sessions.find_session(self.proj, "claude").id, self.b)
        self.assertEqual(sessions.find_session(self.proj, self.a[:6]).id, self.a)
        self.assertEqual(sessions.find_session(self.proj, "claude:" + self.a).id, self.a)
        self.assertEqual(sessions.find_session(self.proj, self.c).id, self.c)  # by id, even from elsewhere
        self.assertEqual(sessions.find_session(self.proj, self.pa).id, self.a)  # a transcript path
        with self.assertRaises(LookupError):
            sessions.find_session(self.proj, "nope")

    def test_dialog_tail_and_known_folders(self):
        tail = sessions.dialog_tail(sessions.read_claude(self.pa))
        self.assertIn("Build the widget", tail)
        self.assertIn("Working on it", tail)
        self.assertNotIn("command-name", tail)
        known = {sessions.norm(p) for p in sessions.known_folders()}
        self.assertTrue({sessions.norm(self.proj), sessions.norm(self.sub), sessions.norm(self.sib)} <= known)

    def test_dropped_paths(self):
        self.assertEqual(resolve_drop(self.proj), (self.proj, None))
        self.assertEqual(resolve_drop(self.pa), (self.proj, self.a))
        text, running = phase_text({"phase": "working", "pid": os.getpid(), "agent": "claude", "task": "x"})
        self.assertEqual((text, running), ("Working: claude - x", True))
        self.assertFalse(phase_text({"phase": "working", "pid": 999999999})[1])


class SessionRelay(Base):
    def setUp(self):
        super().setUp()
        self.git_init()
        self.path = claude_session(self.chome, self.proj, SID, [("user", "Build the widget"), ("assistant", "Started")],
                                   age=600)

    def test_handoff_and_handback_resume_the_right_sessions(self):
        os.environ.update(FAKE_STEP="0.48", FAKE_RESET="4", FAKE_CODEX_DELAY="0.7",
                          FAKE_DONE_ON="While you were paused")
        s = sessions.find_session(self.proj, SID)
        out = []
        rc = Supervisor(self.proj, self.config(), echo=out.append, session=s).run()
        self.assertEqual(rc, EXIT_DONE, "\n".join(out))
        calls = self.calls()
        self.assertEqual((calls[0]["agent"], calls[0]["resume"]), ("claude", SID))
        self.assertTrue(calls[0]["prompt"].startswith(DEFAULT_INSTRUCTION))
        notes = [c for c in calls if "Do not start anything new" in c["prompt"]]
        self.assertEqual([(c["agent"], c["resume"]) for c in notes][0], ("claude", SID))
        codex = [c for c in calls if c["agent"] == "codex"]
        first = codex[0]
        self.assertIsNone(first["resume"])
        for text in ("taking over", "Build the widget", "Read HANDOFF.md first"):
            self.assertIn(text, first["prompt"])
        self.assertTrue(all(c["resume"] == first["session"] for c in codex[1:]))
        self.assertIn(("codex", first["session"]), [(c["agent"], c["resume"]) for c in notes])
        last = calls[-1]
        self.assertEqual((last["agent"], last["resume"]), ("claude", SID))
        self.assertIn("While you were paused", last["prompt"])
        self.assertIn("git log --stat", last["prompt"])
        subjects = subprocess.run(["git", "log", "--format=%s"], cwd=self.proj, capture_output=True,
                                  text=True).stdout
        self.assertIn("handoff note", subjects)

    def test_session_already_at_its_limit_goes_straight_to_the_other_agent(self):
        future = int(time.time()) + 3600
        claude_session(self.chome, self.proj, SID, [("user", "Build the widget"),
                                                   ("assistant", f"Claude AI usage limit reached|{future}")], age=600)
        os.environ.update(FAKE_DONE_ON="taking over")
        out = []
        rc = Supervisor(self.proj, self.config(), echo=out.append, session=sessions.find_session(self.proj, SID)).run()
        self.assertEqual(rc, EXIT_DONE, "\n".join(out))
        calls = self.calls()
        self.assertEqual([c["agent"] for c in calls], ["codex"])

    def test_handoff_command(self):
        with open(os.path.join(self.proj, "tagteam.json"), "w", encoding="utf-8") as f:
            json.dump({"keep_awake": False, "reset_buffer_seconds": 0, "agents": {
                "claude": {"command": [sys.executable, os.path.join(HERE, "fake_claude.py")], "args": []},
                "codex": {"command": [sys.executable, os.path.join(HERE, "fake_codex.py")], "args": []}}}, f)
        with open(os.path.join(self.proj, "HANDOFF.md"), "w", encoding="utf-8") as f:
            f.write("chat note\n")
        # Naming codex only picks who takes over. Claude keeps the session while its 5-hour window
        # reads 47% and 94%, and Codex gets it only after Claude reports 141% (over the 95% default).
        env = dict(os.environ, PYTHONPATH=ROOT, FAKE_DONE_ON="taking over", FAKE_STEP="0.47", FAKE_RESET="3600")
        r = subprocess.run([sys.executable, "-m", "tagteam", "handover", "-C", self.proj, "--session", SID,
                            "--to", "codex", "--foreground", "--quiet-seconds", "0.3"],
                           capture_output=True, text=True, env=env, timeout=120)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        agents = [c["agent"] for c in self.calls()]
        self.assertEqual(agents[:3], ["claude", "claude", "claude"], out)
        self.assertEqual(agents[-1], "codex", out)
        self.assertIn("Handing off to codex (claude: 5h window at 141%)", out)
        self.assertEqual(self.calls()[0]["runs"], ["handover"])  # the pet counts it under Handover

    def test_wakeup_is_counted_as_wakeup(self):
        with open(os.path.join(self.proj, "tagteam.json"), "w", encoding="utf-8") as f:
            json.dump({"keep_awake": False, "agents": {
                "claude": {"command": [sys.executable, os.path.join(HERE, "fake_claude.py")], "args": []},
                "codex": {"command": [sys.executable, os.path.join(HERE, "fake_codex.py")], "args": []}}}, f)
        env = dict(os.environ, PYTHONPATH=ROOT, FAKE_DONE_ON="Continue", FAKE_STEP="0.01")
        r = subprocess.run([sys.executable, "-m", "tagteam", "wakeup", "-C", self.proj, "--session", SID,
                            "--foreground", "--quiet-seconds", "0.3"], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls()[0]["runs"], ["wakeup"], r.stdout + r.stderr)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "tagteam-home", "runs")), [])  # removed at the end

    def progress_run(self, **env):
        # Solo Claude with commits off, five turns at most, and a session that never says it is done.
        os.environ.update(FAKE_STEP="0.01", FAKE_RESET="3600", **env)
        cfg = self.config(solo=True)
        cfg.update(git_commit=False, max_iterations=5)
        out = []
        Supervisor(self.proj, cfg, echo=out.append, session=sessions.find_session(self.proj, SID)).run()
        return "\n".join(out)

    def test_work_without_commits_counts_as_progress(self):
        # Found with real agents: with commits off, every turn counted as "nothing changed" and
        # tagteam stopped after three turns although the agents were writing files.
        out = self.progress_run()
        self.assertNotIn("without progress", out)
        self.assertNotIn("without changing anything", out)
        self.assertEqual(len(self.calls()), 5, out)

    def test_work_outside_git_counts_as_progress(self):
        shutil.rmtree(os.path.join(self.proj, ".git"))
        out = self.progress_run()
        self.assertNotIn("without progress", out)
        self.assertEqual(len(self.calls()), 5, out)

    def test_turns_that_change_nothing_still_stop(self):
        out = self.progress_run(FAKE_IDLE="1")
        self.assertIn("Stopping after 3 turns in a row without progress", out)
        self.assertEqual(len(self.calls()), 3, out)

    def test_handover_to_its_own_agent_is_refused(self):
        with open(os.path.join(self.proj, "tagteam.json"), "w", encoding="utf-8") as f:
            json.dump({"keep_awake": False, "agents": {
                "claude": {"command": [sys.executable, os.path.join(HERE, "fake_claude.py")], "args": []},
                "codex": {"command": [sys.executable, os.path.join(HERE, "fake_codex.py")], "args": []}}}, f)
        r = subprocess.run([sys.executable, "-m", "tagteam", "handover", "-C", self.proj, "--session", SID,
                            "--to", "claude", "--foreground"], capture_output=True, text=True,
                           env=dict(os.environ, PYTHONPATH=ROOT), timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already runs on claude", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_waits_for_the_chat_to_go_quiet(self):
        claude_session(self.chome, self.proj, SID, [("user", "Build the widget")])  # turn still open
        s = sessions.read_claude(self.path)

        def finish():
            time.sleep(1.2)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(_line({"parentUuid": "p", "isSidechain": False, "type": "assistant", "cwd": self.proj,
                               "message": {"content": [{"type": "text", "text": "done"}]}}))

        threading.Thread(target=finish).start()
        t0 = time.time()
        sessions.wait_until_quiet(s, quiet_s=0.4, give_up_s=60)
        self.assertGreaterEqual(time.time() - t0, 1.5)

    def test_stop_file_ends_a_sleeping_run(self):
        os.environ.update(FAKE_STEP="1.0", FAKE_RESET="120")
        sup = Supervisor(self.proj, self.config(solo=True), echo=lambda _: None,
                         session=sessions.find_session(self.proj, SID))

        def stop():
            time.sleep(2.5)
            with open(sup.stop_path, "w") as f:
                f.write("x")

        threading.Thread(target=stop).start()
        t0 = time.time()
        self.assertEqual(sup.run(), EXIT_INTERRUPTED)
        self.assertLess(time.time() - t0, 30)
        self.assertFalse(os.path.exists(sup.stop_path))


class Discussion(Base):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_STEP"] = "0.01"

    def test_both_research_then_review_then_agree(self):
        out = []
        rc = run_discussion(self.proj, self.config(), "Which database?", echo=out.append)
        self.assertEqual(rc, EXIT_DONE, "\n".join(out))
        calls = self.calls()
        steps = [(c["agent"], c["resume"] is None) for c in calls]
        self.assertEqual(steps[:2], [("claude", True), ("codex", True)])  # independent research, fresh sessions
        self.assertTrue(all(not fresh for _, fresh in steps[2:]))  # later steps resume their own sessions
        self.assertIn("codex research answer", calls[2]["prompt"])  # claude reviews codex's answer
        self.assertIn("its review of your answer", calls[3]["prompt"])
        self.assertIn("FINAL PLAN by claude", calls[-1]["prompt"])  # codex signs off claude's final
        self.assertTrue(all(c["runs"] == ["discussion"] for c in calls))  # the pet counts it under Discussion
        finals = [n for n in os.listdir(os.path.join(self.proj, ".tagteam", "discussions")) if n.endswith("-final.md")]
        self.assertEqual(len(finals), 1)
        self.assertEqual(set(os.listdir(self.proj)), {".tagteam"})  # read-only: nothing else written
        self.assertIn("both agents agreed", "\n".join(out))

    def test_round_limit_and_corrections(self):
        os.environ.update(FAKE_VERDICT="DISAGREE", FAKE_SIGNOFF="CHANGES\nfix step 2")
        out = []
        rc = run_discussion(self.proj, self.config(), "Which database?", rounds=2, echo=out.append)
        self.assertEqual(rc, EXIT_DONE)
        text = "\n".join(out)
        self.assertIn("REVISED PLAN by claude", text)
        self.assertIn("no full agreement after 2 rounds", text)

    def test_read_only_permissions_and_verdicts(self):
        c = readonly_cfg({"type": "claude", "args": ["--permission-mode", "acceptEdits"]})["args"]
        self.assertNotIn("acceptEdits", c)
        self.assertIn("Edit,Write,NotebookEdit", c)
        x = readonly_cfg({"type": "codex", "args": ["--sandbox", "workspace-write", "--full-auto"]})["args"]
        self.assertEqual(x, ["--sandbox", "read-only"])
        self.assertTrue(verdict("blah\nVERDICT: AGREE"))
        self.assertTrue(verdict("**VERDICT:** AGREE"))
        self.assertFalse(verdict("VERDICT: AGREE\nlater... VERDICT: DISAGREE"))
        self.assertFalse(verdict("no verdict"))


class Window(Base):
    def test_window_lists_the_sessions(self):
        from tagteam.gui import Picker, check_gui

        ok, why = check_gui()
        if not ok:
            self.skipTest(why)
        claude_session(self.chome, self.proj, SID, [("user", "Build the widget"), ("assistant", "Started")])
        try:
            p = Picker(self.proj)
        except Exception as e:  # e.g. Tk present but no usable display
            self.skipTest(f"no window here: {e}")
        p.root.withdraw()
        try:
            for _ in range(80):
                p.root.update()
                if p.tree.get_children():
                    break
                time.sleep(0.1)
            self.assertEqual(len(p.tree.get_children()), 1)
            self.assertEqual(p.selected().id, SID)
        finally:
            p.root.destroy()



class DoneMark(unittest.TestCase):
    def test_only_a_final_line_counts(self):
        from tagteam.supervisor import says_done
        # Real Claude reply from an end-to-end run: it names the mark while refusing to finish.
        self.assertFalse(says_done("I did not finish, so I'm not ending with TAGTEAM_DONE.\n\n- Current state: ..."))
        self.assertTrue(says_done("All three lines are in notes.txt.\n\nTAGTEAM_DONE"))
        self.assertTrue(says_done("Done.\n**TAGTEAM_DONE**\n"))
        self.assertFalse(says_done("TAGTEAM_DONE\nbut one more thing is left"))


if __name__ == "__main__":
    unittest.main()
