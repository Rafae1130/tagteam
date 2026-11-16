---
description: List the Claude Code and Codex sessions in this folder, newest first.
allowed-tools: Bash(python3:*), Bash(python:*), Bash(py:*)
---
From the project folder, run:

   python3 "${CLAUDE_PLUGIN_ROOT}/bin/tagteam.py" sessions -C .

If `python3` is not found or fails, use `python` or `py -3` instead. Show the output to the user unchanged, in a code block, with no commentary.
