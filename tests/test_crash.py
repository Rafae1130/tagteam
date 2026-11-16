"""A force-killed tagteam must not leave its agent, or anything the agent started, running.

taskkill /F and kill -9 give tagteam no chance to run any code, so this only passes if the
operating system itself ends the agent: the job object on Windows, the watchdog elsewhere."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tagteam.lock import pid_alive  # noqa: E402

AGENT = """
import os, subprocess, sys, time
d = sys.argv[1]
sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
try:  # like Git Bash on Windows: start outside the job when the job allows it
    child = subprocess.Popen(sleeper, creationflags=0x01000000 if os.name == "nt" else 0)
except OSError:
    child = subprocess.Popen(sleeper)
with open(os.path.join(d, "pids.tmp"), "w") as f:
    f.write("%d %d" % (os.getpid(), child.pid))
os.replace(os.path.join(d, "pids.tmp"), os.path.join(d, "pids"))
time.sleep(120)
"""

TAGTEAM = """
import os, sys
sys.path.insert(0, sys.argv[1])
from tagteam import proc
d = sys.argv[2]
proc.run([sys.executable, os.path.join(d, "agent.py"), d], d, None, 300, os.path.join(d, "agent.log"))
"""


def force_kill(pid):
    """Kill only this process, the way taskkill /F or kill -9 would; its children are left alone."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def gone(pid):
    if not pid_alive(pid):
        return True
    try:  # Linux: a zombie waiting to be reaped has already stopped running
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


class ForceKilledTagteam(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tt-crash-")
        self.pids = []

    def tearDown(self):
        for pid in self.pids:  # never leave anything behind, even when the test fails
            if not gone(pid):
                force_kill(pid)
        shutil.rmtree(self.d, ignore_errors=True)

    def start(self):
        for name, text in (("agent.py", AGENT), ("tagteam.py", TAGTEAM)):
            with open(os.path.join(self.d, name), "w") as f:
                f.write(text)
        host = subprocess.Popen([sys.executable, os.path.join(self.d, "tagteam.py"), ROOT, self.d],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.pids.append(host.pid)
        path = os.path.join(self.d, "pids")
        deadline = time.time() + 60
        while not os.path.exists(path):
            self.assertLess(time.time(), deadline, "the agent never started")
            self.assertIsNone(host.poll(), "the tagteam process ended early")
            time.sleep(0.05)
        with open(path) as f:
            agent, child = (int(x) for x in f.read().split())
        self.pids += [agent, child]
        return host, agent, child

    def test_agent_and_its_children_die_with_a_force_killed_tagteam(self):
        host, agent, child = self.start()
        self.assertFalse(gone(agent) or gone(child))
        force_kill(host.pid)
        host.wait(10)
        deadline = time.time() + 10
        while time.time() < deadline and not (gone(agent) and gone(child)):
            time.sleep(0.05)
        self.assertTrue(gone(agent), "the agent kept running after tagteam was force-killed")
        self.assertTrue(gone(child), "a process the agent started kept running after tagteam was force-killed")


if __name__ == "__main__":
    unittest.main()
