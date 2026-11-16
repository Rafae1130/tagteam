import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tagteam.agents import ClaudeAgent, CodexAgent  # noqa: E402
from tagteam.proc import RunOutput  # noqa: E402
from tagteam.textparse import iter_json_objects, parse_reset_text  # noqa: E402


def out(lines, rc=0, stderr=""):
    return RunOutput(rc, [json.dumps(x) + "\n" if isinstance(x, dict) else x for x in lines],
                     stderr, False, time.time() - 1)


class TextParsing(unittest.TestCase):
    def test_event_written_into_another_line(self):
        line = ('{"type":"assistant","message":{"content":[{"type":"text","text":"hi'
                '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}' '"}]}}')
        types = [o.get("type") for o in iter_json_objects(line)]
        self.assertIn("rate_limit_event", types)

    def test_reset_epoch(self):
        self.assertEqual(parse_reset_text("Claude AI usage limit reached|1790000000", 1789990000), 1790000000)

    def test_reset_duration(self):
        now = 1_000_000.0
        self.assertEqual(parse_reset_text("Try again in 2 hours 5 minutes.", now), now + 7500)
        self.assertEqual(parse_reset_text("try again in 4 days 3 hours 2 minutes", now),
                         now + 4 * 86400 + 3 * 3600 + 120)
        self.assertEqual(parse_reset_text("resets in 45m", now), now + 2700)

    def test_reset_clock(self):
        now = time.time()
        for text in ("You've hit your limit · resets 5pm", "try again at 3:05 PM", "resets 17:30"):
            t = parse_reset_text(text, now)
            self.assertIsNotNone(t, text)
            self.assertTrue(now < t <= now + 86400, text)

    def test_no_reset(self):
        self.assertIsNone(parse_reset_text("something went wrong", time.time()))


class ClaudeParsing(unittest.TestCase):
    agent = ClaudeAgent("claude", {"command": ["claude"]})

    def test_allowed_with_windows(self):
        r = self.agent.parse(out([
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "allowed", "resetsAt": 1790578200, "rateLimitType": "five_hour",
                "unifiedWindows": {"five_hour": {"utilization": 0.09, "resetsAt": 1790578200},
                                   "seven_day": {"utilization": 0.15, "resetsAt": 1791036000}}}},
            {"type": "result", "is_error": False, "result": "OK"}]))
        self.assertTrue(r.ok)
        self.assertFalse(r.limited)
        self.assertAlmostEqual(r.windows["5h"].used, 9.0)
        self.assertAlmostEqual(r.windows["7d"].used, 15.0)
        self.assertEqual(r.windows["7d"].resets_at, 1791036000)

    def test_warning_without_percentage(self):
        r = self.agent.parse(out([
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "allowed_warning", "resetsAt": 1790578200, "rateLimitType": "five_hour"}},
            {"type": "result", "is_error": False, "result": "OK"}]))
        self.assertEqual(r.windows["5h"].used, 90.0)

    def test_rejected(self):
        r = self.agent.parse(out([
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "resetsAt": 1790578200, "rateLimitType": "five_hour"}},
            {"type": "result", "is_error": True, "result": "You've hit your limit"}], rc=1))
        self.assertFalse(r.ok)
        self.assertTrue(r.limited)
        self.assertEqual(r.resets_at, 1790578200)

    def test_rejected_but_paid_overage(self):
        r = self.agent.parse(out([
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "resetsAt": 1790578200, "isUsingOverage": True}},
            {"type": "result", "is_error": False, "result": "OK"}]))
        self.assertTrue(r.ok)
        self.assertFalse(r.limited)

    def test_limit_text_only(self):
        future = int(time.time()) + 3600
        r = self.agent.parse(out([f"Claude AI usage limit reached|{future}\n"], rc=1))
        self.assertTrue(r.limited)
        self.assertEqual(r.resets_at, future)

    def test_ordinary_failure_is_not_a_limit(self):
        r = self.agent.parse(out([{"type": "result", "is_error": True, "result": "Tool crashed"}], rc=1))
        self.assertFalse(r.ok)
        self.assertFalse(r.limited)


class CodexParsing(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tagteam-codex-")
        self.agent = CodexAgent("codex", {"command": ["codex"], "codex_home": self.home})

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def rollout(self, tid, snap):
        d = os.path.join(self.home, "sessions", "2026", "11", "13")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"rollout-2026-11-13T10-00-00-{tid}.jsonl"), "w") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": snap}}) + "\n")

    def test_snapshot(self):
        self.rollout("abc", {"primary": {"used_percent": 42.0, "window_minutes": 300, "resets_at": 1790000000},
                             "secondary": {"used_percent": 7.0, "window_minutes": 10080, "resets_at": 1790500000}})
        r = self.agent.parse(out([{"type": "thread.started", "thread_id": "abc"},
                                  {"type": "turn.completed", "usage": {}}]))
        self.assertTrue(r.ok)
        self.assertEqual(r.windows["5h"].used, 42.0)
        self.assertEqual(r.windows["7d"].resets_at, 1790500000)

    def test_limit_reached(self):
        self.rollout("xyz", {"primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": 1790000000},
                             "rate_limit_reached_type": "primary"})
        r = self.agent.parse(out([{"type": "thread.started", "thread_id": "xyz"},
                                  {"type": "turn.failed", "error": {"message": "usage limit"}}], rc=1))
        self.assertFalse(r.ok)
        self.assertTrue(r.limited)
        self.assertEqual(r.resets_at, 1790000000)


class RealCodexLimitMessages(unittest.TestCase):
    """Limit messages copied from a real Codex install (thread_turns.error_json, usageLimitExceeded)."""

    MSG = ("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
           "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at {}.")

    def at(self, tail, started):
        import datetime as dt
        now = dt.datetime(*started).timestamp()
        r = parse_reset_text(self.MSG.format(tail), now)
        return dt.datetime.fromtimestamp(r) if r else None

    def test_same_day_clock(self):
        self.assertEqual(self.at("1:54 PM", (2026, 9, 7, 8, 54)).strftime("%Y-%m-%d %H:%M"), "2026-09-07 13:54")

    def test_other_day_with_date(self):
        # Before the fix this returned None and tagteam waited without knowing the reset.
        self.assertEqual(self.at("Sep 21st, 2026 5:47 PM", (2026, 9, 20, 0, 49)).strftime("%Y-%m-%d %H:%M"),
                         "2026-09-21 17:47")
        self.assertEqual(self.at("Sep 23rd, 2026 3:58 AM", (2026, 9, 22, 23, 9)).strftime("%Y-%m-%d %H:%M"),
                         "2026-09-23 03:58")


if __name__ == "__main__":
    unittest.main()
