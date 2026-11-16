---
description: Open the tagteam pet, a small floating window with three bars counting the discussions, handovers and wakeups running on this computer; hover over a bar for each one's status.
allowed-tools: Bash(python3:*), Bash(python:*), Bash(py:*)
---
From the project folder, run:

   python3 "${CLAUDE_PLUGIN_ROOT}/bin/tagteam.py" pet --detach -C .

If `python3` is not found or fails, use `python` or `py -3` instead. Then tell the user in one sentence what it printed.
