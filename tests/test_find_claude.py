"""People who only use Claude Code inside VS Code have no `claude` on PATH; tagteam finds the extension's copy."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tagteam import agents  # noqa: E402

EXE = "claude.exe" if os.name == "nt" else "claude"


class FindClaude(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tt-home-")
        env = {"HOME": self.home, "USERPROFILE": self.home}
        self.patches = [mock.patch.dict(os.environ, env), mock.patch.object(agents.shutil, "which", return_value=None)]
        for p in self.patches:
            p.start()
        os.environ.pop("CLAUDE_CODE_EXECPATH", None)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def binary(self, *parts):
        path = os.path.join(self.home, *parts, EXE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("")
        return path

    def extension(self, ide, version):
        return self.binary(ide, "extensions", f"anthropic.claude-code-{version}-x64", "resources", "native-binary")

    def test_newest_extension_copy_is_used(self):
        self.extension(".vscode", "2.1.9")
        newest = self.extension(".vscode", "2.1.10")  # 10 > 9 as a version, not as text
        self.extension(".cursor", "2.0.99")
        self.assertEqual(agents.find_claude(), newest)
        agent = agents.ClaudeAgent("claude", {})
        self.assertTrue(agent.available())
        self.assertEqual(agent.build("hi")[0][0], newest)

    def test_native_install_beats_the_extension(self):
        self.extension(".vscode", "2.1.10")
        local = self.binary(".local", "bin")
        self.assertEqual(agents.find_claude(), local)

    def test_running_copy_is_the_last_resort(self):
        running = self.binary("somewhere")
        os.environ["CLAUDE_CODE_EXECPATH"] = running
        self.assertEqual(agents.find_claude(), running)
        os.environ["CLAUDE_CODE_EXECPATH"] = os.path.join(self.home, "node.exe")  # not a claude binary
        self.assertIsNone(agents.find_claude())

    def test_nothing_found_reports_unavailable(self):
        self.assertIsNone(agents.find_claude())
        self.assertFalse(agents.ClaudeAgent("claude", {}).available())

    def test_a_configured_command_is_never_replaced(self):
        self.extension(".vscode", "2.1.10")
        custom = self.binary("tools")
        agent = agents.ClaudeAgent("claude", {"command": [custom, "--flag"]})
        self.assertEqual(agent.build("hi")[0][:2], [custom, "--flag"])


if __name__ == "__main__":
    unittest.main()
