"""Codex builds differ: older `codex queue` has no --resume, and the newest copy is chosen by version."""

import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tagteam import agents, codexdb  # noqa: E402

EXE = "codex.exe" if os.name == "nt" else "codex"


def fake_codex(folder, help_text):
    """A stand-in codex whose `queue --help` prints help_text."""
    os.makedirs(folder, exist_ok=True)
    if os.name == "nt":
        path = os.path.join(folder, "codex.cmd")
        body = "@echo off\r\n" + "".join(f"echo {line}\r\n" for line in help_text.splitlines())
    else:
        path = os.path.join(folder, "codex")
        body = "#!/bin/sh\n" + "".join(f"echo '{line}'\n" for line in help_text.splitlines())
    with open(path, "w", newline="") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


class QueueResume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tt-codex-")
        codexdb._queue_resume.clear()

    def tearDown(self):
        codexdb._queue_resume.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_newer_codex_gets_resume(self):
        exe = fake_codex(os.path.join(self.tmp, "new"), "Usage: codex queue\n--thread\n--message\n--resume")
        self.assertTrue(codexdb.queue_supports_resume(exe))

    def test_older_codex_does_not(self):
        exe = fake_codex(os.path.join(self.tmp, "old"), "Usage: codex queue\n--thread\n--message\n--remote")
        self.assertFalse(codexdb.queue_supports_resume(exe))

    def test_missing_codex_does_not(self):
        self.assertFalse(codexdb.queue_supports_resume(os.path.join(self.tmp, "nothing-here")))


class NewestExtension(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tt-home-")
        self.patches = [mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home}),
                        mock.patch.object(codexdb.shutil, "which", return_value=None),
                        mock.patch.object(codexdb, "app_codex", return_value=None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def extension(self, version, mtime):
        d = os.path.join(self.home, ".vscode", "extensions", f"openai.chatgpt-{version}-x64", "bin", "x")
        os.makedirs(d)
        path = os.path.join(d, EXE)
        with open(path, "w") as f:
            f.write("")
        os.utime(path, (mtime, mtime))
        return path

    def test_newest_version_wins_even_if_an_older_one_was_touched_later(self):
        newest = self.extension("26.1106.51428", mtime=1_000_000)
        self.extension("26.1021.22027", mtime=2_000_000)
        self.assertEqual(codexdb.find_codex(), newest)

    def test_app_mode_prefers_the_apps_own_codex(self):
        app = os.path.join(self.home, "app-codex")
        with mock.patch.object(codexdb, "app_codex", return_value=app), \
                mock.patch.object(agents.shutil, "which", return_value="/usr/bin/codex"):
            self.assertEqual(agents.CodexAgent("codex", {"gui": True})._exe()[0], app)
            self.assertEqual(agents.CodexAgent("codex", {})._exe()[0], "/usr/bin/codex")


if __name__ == "__main__":
    unittest.main()
