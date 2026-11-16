---
description: Like /tagteam:discussion, but Codex works in the Codex app where you can watch it. Claude and Codex research a question separately, then trade answers and comments until they agree, and you get their joint decision or plan.
argument-hint: "<question or task to discuss>"
allowed-tools: Bash(python3:*), Bash(python:*), Bash(py:*), Read, Write
---
Topic from the user: $ARGUMENTS

If the topic is empty, ask the user what the agents should discuss, and stop.

Otherwise:

1. Write the topic to `.tagteam/discussion-topic.md` in the project folder, exactly as the user gave it. If earlier parts of this conversation hold context the agents need (decisions, constraints, file names), add it below the topic under a heading "Context from the conversation".
2. From the project folder, run this in the background, because it can take many minutes:

   python3 "${CLAUDE_PLUGIN_ROOT}/bin/tagteam.py" discuss -C . --file .tagteam/discussion-topic.md --codex-gui

   If `python3` is not found or fails, use `python` or `py -3` instead.
3. Tell the user in one sentence that the two agents are researching the question and that Codex's side shows in the Codex app.
4. When the command finishes, read the final-answer file it names and give the user the final answer in full, then one line saying whether both agents agreed and where the full discussion is saved. If it stopped early, say why, using its output.
