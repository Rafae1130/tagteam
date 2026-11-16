"""tagteam: run coding agents in shifts around their usage limits."""

# Written so that even very old Pythons can parse this file, which runs before any other
# tagteam code: an old Python gets one clear line instead of a confusing traceback.
import sys as _sys

if _sys.version_info < (3, 9):
    _sys.stderr.write("tagteam needs Python 3.9 or newer, and this is Python %d.%d. Install a newer "
                      "Python (python.org) and run tagteam with it.\n" % tuple(_sys.version_info[:2]))
    raise SystemExit(1)

__version__ = "0.1.0"
