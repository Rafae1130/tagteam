---
description: Keep this session going on its own. Whenever it hits the usage limit, tagteam sleeps until the reset and resumes it.
argument-hint: "[what to do next]"
allowed-tools: Bash(python3:*), Bash(python:*), Bash(py:*)
---
The user wants this session to continue by itself and be woken up automatically after each usage-limit reset.

Arguments from the user: $ARGUMENTS

1. From the project folder, run:

   python3 "${CLAUDE_PLUGIN_ROOT}/bin/tagteam.py" wakeup -C . --session "${CLAUDE_SESSION_ID}"

   Add `--instruction "<text>"` if the arguments contain any text. If `python3` is not found or fails, use `python` or `py -3` instead.
2. Tell the user in one or two short sentences what tagteam printed, then end your turn. Do no more work in this chat: tagteam continues the session once this chat has been quiet for 30 seconds.
