---
description: Show what tagteam is doing in this folder - the session or tasks, each agent's usage, and when limits reset.
allowed-tools: Bash(python3:*), Bash(python:*), Bash(py:*)
---
From the project folder, run:

   python3 "${CLAUDE_PLUGIN_ROOT}/bin/tagteam.py" status -C .

If `python3` is not found or fails, use `python` or `py -3` instead. Show the output to the user unchanged, in a code block.
