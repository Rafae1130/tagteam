---
description: Stop the tagteam run in this folder (it stops its agent and saves its state).
allowed-tools: Bash(python3:*), Bash(python:*), Bash(py:*)
---
From the project folder, run:

   python3 "${CLAUDE_PLUGIN_ROOT}/bin/tagteam.py" stop -C .

If `python3` is not found or fails, use `python` or `py -3` instead. Then tell the user in one sentence what it printed.
