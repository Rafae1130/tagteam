"""TASKS.md: one Markdown checkbox per task."""

import re
from typing import List, Tuple

UNCHECKED = re.compile(r"^\s*[-*+]\s+\[ \]\s+(.+?)\s*$", re.M)

TEMPLATE = """\
# Tasks

The agents work through this list from the top. Each turn takes the first unchecked
task, does it, and checks it off. Keep each task small enough to finish in one turn.

- [ ] Describe your first task here
"""


CHECKED = re.compile(r"^\s*[-*+]\s+\[[xX]\]\s+(.+?)\s*$", re.M)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def unchecked(path: str) -> List[str]:
    return UNCHECKED.findall(_read(path))


def counts(path: str) -> Tuple[int, List[str]]:
    """(number of checked tasks, list of unchecked tasks)."""
    text = _read(path)
    return len(CHECKED.findall(text)), UNCHECKED.findall(text)
