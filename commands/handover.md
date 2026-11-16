---
description: Hand this session to tagteam. It keeps working in the background, switches to the other agent when a usage window reaches 95%, and gives the session back after the reset.
argument-hint: "[codex|claude|solo] [what to do next]"
allowed-tools: Bash(python3:*), Bash(python:*), Bash(py:*), Read, Write, Edit
---
The user wants tagteam to take over this session, so the work continues without this chat.

Arguments from the user: $ARGUMENTS

1. Overwrite `HANDOFF.md` in the project folder with a handoff note for whichever agent continues: the goal, what is done, what is in progress, what should happen next, and anything tricky (files, commands, decisions). Keep it under 60 lines.
2. From the project folder, run:

   python3 "${CLAUDE_PLUGIN_ROOT}/bin/tagteam.py" handover -C . --session "${CLAUDE_SESSION_ID}"

   Add `--to codex` or `--to claude` if the arguments name an agent: that agent takes over only when this session reaches 95% of a usage window, not now. Add `--solo` if they say solo, and `--instruction "<text>"` for any other text in the arguments. If `python3` is not found or fails, use `python` or `py -3` instead.
3. Tell the user in one or two short sentences what tagteam printed, then end your turn. Do no more work in this chat: tagteam takes the session over once this chat has been quiet for 30 seconds.
