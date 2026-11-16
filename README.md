# tagteam

Run AI coding agents in shifts around their usage limits.

- **Two agents:** your main agent (say Claude Code) works through a task list. Shortly before it
  runs out of quota, tagteam hands the work to a secondary agent (say Codex). When the main
  agent's limit resets, the work is handed back.
- **One agent:** when the agent hits its limit, tagteam sleeps until the limit resets and carries on.
- **Sessions:** instead of a task list, pick one of your Claude Code or Codex conversations (in a
  window, or with `/tagteam:handover` inside the chat). tagteam resumes that exact session, lends the
  work to the other agent while its limit is used up, and gives it back after the reset.
- **Discussion:** Claude and Codex research a question separately, then trade answers and comments
  until they agree, and you get one joint decision or plan.

Only one agent ever works at a time. tagteam starts an agent, waits for its process to exit,
commits, and only then decides who goes next. Switches therefore only happen between tasks,
right after a git commit. If a hard limit cuts a turn short, that turn is committed as `WIP` and
the next agent is told to continue it. A lock file (`.tagteam/run.lock`) stops a second
`tagteam run` from starting in the same project. A lock left behind by a crashed run is
detected and replaced. If tagteam itself is killed, even with `taskkill /F` or `kill -9`, the
agent it started is stopped with it, so a new run never works next to a leftover agent.
tagteam can't see your own interactive sessions, so don't point another agent at the project
while tagteam is running.

No Python packages are needed. tagteam uses only the standard library and runs on Windows,
macOS and Linux with Python 3.9 or newer.

## Requirements

- Python 3.9+
- git
- The agents you want to use, installed and logged in:
  [Claude Code](https://code.claude.com/docs) and/or [Codex](https://github.com/openai/codex).
  The VS Code extensions are enough: when `claude` or `codex` is not on PATH, tagteam uses the copy
  that the Claude Code extension, the Codex extension or the Codex app carries. Other CLIs can be
  added as [custom agents](#other-agents).

## Install

**VS Code:** [![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_tagteam-0098FF?logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect?url=vscode%3A%2F%2Fanthropic.claude-code%2Finstall-plugin%3Fplugin%3Dtagteam%26marketplace%3DRafae1130%2Ftagteam)

Click the button and VS Code opens the Claude Code plugin dialog with tagteam filled in. Press
**Add Marketplace & Continue**, then install, and start a new chat to get the `/tagteam:` commands.
Or, in the Claude chat, type `/`, pick **Manage plugins**, add the marketplace `Rafae1130/tagteam`
and install tagteam from it. You don't need the Claude Code terminal app.

**Claude Code in a terminal:** `/plugin install tagteam --marketplace Rafae1130/tagteam`

More ways, and the full command list: [Claude Code plugin](#claude-code-plugin).

**Only the `tagteam` terminal command**, without Claude Code plugins:

```sh
pipx install git+https://github.com/Rafae1130/tagteam      # or: pip install .
```

You can also skip installing: from a clone, run `python -m tagteam ...` instead of `tagteam ...`.

## Quick start

```sh
cd my-project
tagteam init                 # writes tagteam.json and TASKS.md, runs git init if needed
#   edit TASKS.md: one "- [ ] task" line per task, small enough for one turn
tagteam check claude         # one tiny prompt: confirms the CLI works and shows your usage
tagteam check codex
tagteam run
```

Single agent:

```sh
tagteam init --solo          # or keep the config and use: tagteam run --solo
tagteam run --solo
```

`tagteam status` shows the tasks left, each agent's usage, and when a limited agent is free again.
Press Ctrl+C (or run `tagteam stop`) at any time. `tagteam run` picks up where it stopped.

## Continue a session

```sh
tagteam sessions                      # the Claude Code and Codex sessions of this folder, newest first
tagteam run --session 3f2a91c0        # continue that one (an id, its start, 'latest', 'claude' or 'codex')
tagteam gui                           # or pick one in a window
```

tagteam finds sessions where the CLIs already keep them: Claude Code's transcripts in
`~/.claude/projects`, and Codex's SQLite databases in `~/.codex` (`state_<n>.sqlite` and
`thread_history_<n>.sqlite`; older Codex versions without them are read from `~/.codex/sessions`).
Sessions are matched on the folder they ran in, including folders inside it. tagteam opens all of
these read-only. Then:

1. The session's own agent resumes it (`claude -p --resume <id>` / `codex exec resume <id>`), so it
   keeps its whole conversation.
2. Near its limit (95% of a usage window by default), that agent is asked to write `HANDOFF.md`, and
   the other agent starts with the note, the session's original request and the end of the
   conversation. It keeps its own session for its later turns.
3. After the reset, the original session gets the work back, with a pointer to the note and to the
   commits made in the meantime.
4. The agent ends its reply with `TAGTEAM_DONE` when everything asked for in the session is
   finished, and tagteam stops.

Without a second agent (`--solo`), tagteam sleeps through each limit and resumes the session after
the reset. If the chat that owns the session is still open, `--wait-quiet` makes tagteam wait until
it has been quiet for 30 seconds, so two agents never write to one session at once; after that, use
the session only through tagteam until it stops.

## The window

`tagteam gui` opens a plain window (tkinter, nothing to install). Drop a project folder on it (or a
session file), press **Open folder...**, or pick one under **Recent**: every folder that has sessions
is listed there. Pick a session, choose who takes over at the limit and at what percentage, and press
**Continue this session**. tagteam then runs in the background; the window shows what it is doing,
**Stop** stops it, and closing the window asks whether to keep it running. Dropping folders works on
Windows as is, and on Linux and macOS when the `tkdnd` Tk extension is installed; the buttons work
everywhere.

## Claude Code plugin

The repository is also a Claude Code plugin. Install it whichever way fits; the terminal and the
VS Code extension share their plugins, so installing once gives you the commands in both (start a new
chat afterwards).

- **Claude Code in a terminal:** type `/plugin install tagteam --marketplace Rafae1130/tagteam`.
  Older versions: `/plugin marketplace add Rafae1130/tagteam`, then `/plugin install tagteam@tagteam`.
- **VS Code:** the extension's chat has no `/plugin` command. Type `/` and pick **Manage plugins**
  to add the marketplace `Rafae1130/tagteam` and install tagteam, or open
  [this install link](https://vscode.dev/redirect?url=vscode%3A%2F%2Fanthropic.claude-code%2Finstall-plugin%3Fplugin%3Dtagteam%26marketplace%3DRafae1130%2Ftagteam)
  and VS Code opens the Claude Code plugin dialog with tagteam filled in. You can also paste
  `vscode://anthropic.claude-code/install-plugin?plugin=tagteam&marketplace=Rafae1130/tagteam` into
  your browser's address bar.
- **Any shell, including VS Code's terminal:** `claude plugin install tagteam --marketplace Rafae1130/tagteam`
- **A settings file:** put this in `~/.claude/settings.json`, or in a project's
  `.claude/settings.json` so everyone who opens that project is offered tagteam:

  ```json
  {
    "extraKnownMarketplaces": {
      "tagteam": { "source": { "source": "github", "repo": "Rafae1130/tagteam" } }
    },
    "enabledPlugins": { "tagteam@tagteam": true }
  }
  ```

| Command | What it does |
|---|---|
| `/tagteam:handover [codex\|claude\|solo] [instruction]` | The chat writes `HANDOFF.md`, then tagteam takes this session over in the background once the chat goes quiet: it hands over when a usage window reaches 95% and gets the session back after the reset. Naming an agent picks who takes over then; it never switches right away. Codex runs headless. |
| `/tagteam:handover_gui [codex\|claude\|solo] [instruction]` | The same, but Codex works in the Codex desktop app, where you can watch it and type into its thread. |
| `/tagteam:wakeup [instruction]` | This session keeps going on its own: at each usage limit tagteam sleeps until the reset and resumes it. |
| `/tagteam:discussion <question>` | Claude and Codex discuss the question until they agree; the chat shows you the final answer. Codex runs headless. |
| `/tagteam:discussion_gui <question>` | The same, but Codex's side of the discussion runs in the Codex desktop app. |
| `/tagteam:pet` | Opens the floating status window: how many discussions, handovers and wakeups are running, with each one's status when you hover over its bar. |
| `/tagteam:gui` | Opens the session window. |
| `/tagteam:sessions`, `/tagteam:status`, `/tagteam:stop` | List sessions, show progress, stop the run. |

The commands run the plugin's own copy of tagteam (`bin/tagteam.py`), so nothing else needs to be
installed besides Python 3.9+. The same things work from a terminal: `tagteam handover`,
`tagteam wakeup` (both take `--session <id>`), `tagteam discuss "question"`. Add `--codex-gui` to any
of them (and to `tagteam run`) for the Codex app mode; the window has a checkbox for it.

### Codex in its desktop app (`--codex-gui`)

Windows and macOS, with the Codex desktop app installed and signed in. tagteam opens the thread in
the app (`codex://threads/<id>`) and sends each prompt with `codex queue`; the app runs the turn,
and tagteam reads the result and the usage back from Codex's databases. A brand-new Codex thread is
started headless first, because the app has no link that starts a thread with a given prompt; from
then on every Codex turn runs in the app.

### Which Codex and which model

If `codex` is not on PATH, tagteam uses the copy inside the Codex desktop app or the ChatGPT/Codex
extension for VS Code (and its forks). It passes `-m` with a model your account still has, taken
from Codex's own model list: the thread's model when resuming, otherwise the one in `config.toml`,
otherwise the one you used last. This matters because `codex exec resume` uses the `config.toml`
model rather than the thread's, and a model your account has lost makes every turn fail. Set
`"model"` on the codex agent in `tagteam.json` to choose one yourself.

## Discussion

```sh
tagteam discuss "Should we move the build to CMake presets?"     # or: --file question.md
```

1. Both agents research the question on their own (each in its own session, read-only: they can read
   files and search, not change anything).
2. Each reviews the other's answer, then they reply to each other's comments, one at a time, ending
   every message with `VERDICT: AGREE` or `VERDICT: DISAGREE`.
3. When both agree (or after `--rounds`, default 4), the main agent writes the final decision or plan,
   the other signs it off or asks for corrections, and tagteam prints it.

Everything is saved in `.tagteam/discussions/`: the final answer and the whole exchange. A usage
limit pauses the discussion until the reset (`--no-wait` stops instead).

## The pet

```sh
tagteam pet                  # open it, any time
tagteam run --pet            # or open it when a run starts
```

The pet is a small window that floats on top of your other windows. It has three bars,
Discussion, Handover and Wakeup, and each one counts the tagteam runs of that kind that are
active on this computer right now, in any folder. Each run is one segment of its bar: in the
working agent's colour, blue while it sleeps through a usage limit, grey while it waits (for
example for the chat to go quiet). Hover over a bar to see each run's folder, session, status
and usage. The creature on top shows the busiest of them. A plain `tagteam run` counts as a
handover when it has a second agent and as a wakeup when it runs solo.

Drag the pet to move it and right-click it for a menu. Only one pet opens at a time. It runs as
its own process and only reads tagteam's files (every run registers itself in `~/.tagteam/runs`
while it works), so closing it never affects the work.

It uses tkinter, which ships with Python. Some installs leave Tk out: Debian and Ubuntu put it
in `python3-tk`, Homebrew in `python-tk@3.x`. In that case, and on machines with no screen
(for example over SSH), the pet doesn't open. tagteam prints one line with the exact install
command and keeps working in the terminal.

## How it decides who works

Before every task, tagteam picks the first agent in priority order (main, then secondary) that
is under its usage threshold and not limited:

| Agent | Where usage comes from |
|---|---|
| Claude Code | The `rate_limit_event` in `claude -p --output-format stream-json` output: 5-hour and weekly utilization and reset times. Older CLI versions send only the status and reset time, so there the hand-off happens at the hard limit instead of before it. |
| Codex | The `rate_limits` snapshot (`used_percent` and `resets_at` per window: 5-hour and weekly, or one 30-day window on the free plan) that Codex writes to the thread's session file, found through its SQLite record (`.jsonl`, or `.jsonl.zst` on Python 3.14+). A failed turn whose error is `usageLimitExceeded` counts as a limit; its "try again at ..." time is the reset. |
| Fallback | If neither structured signal shows up, limit messages in the error output, including reset times like "resets 5pm", "try again in 2 hours", or an epoch timestamp. |

- The **main** agent stops taking new tasks at **95%** of any window when a secondary exists,
  and at 100% otherwise. The secondary runs until its hard limit. Set `max_usage` per agent to
  change this.
- **Hand-back** happens at the first task boundary after the main agent's window reset time.
- When **every** agent is limited, tagteam sleeps until the earliest reset (plus
  `reset_buffer_seconds`). Turn this off with `--no-wait` or `"wait_for_reset": false`.
- While it runs, tagteam asks the OS not to idle-sleep (Windows execution state, `caffeinate` on
  macOS, `systemd-inhibit` on Linux). Closing a laptop lid still suspends it. Waiting uses the
  wall clock, so after a resume the wait ends on time.

Each turn gets the same instructions: read `TASKS.md` and `HANDOFF.md`, do the first unchecked
task, check it off, and write a short `HANDOFF.md` note for the next agent. tagteam commits
afterwards.

## Configuration

`tagteam.json` in the project folder. Every key is optional.

| Key | Default | Meaning |
|---|---|---|
| `main` | `"claude"` | Agent that works whenever it has quota |
| `secondary` | `"codex"` | Agent that covers for main; `""` for single-agent mode |
| `wait_for_reset` | `true` | Sleep until a reset when every agent is limited |
| `keep_awake` | `true` | Keep the computer from idle-sleeping while running |
| `reset_buffer_seconds` | `120` | Extra wait after a reported reset time |
| `unknown_reset_retry_minutes` | `30` | Retry interval when a limit gives no reset time |
| `task_timeout_minutes` | `60` | A turn running longer than this is stopped |
| `max_iterations` | `200` | Turns per `tagteam run` |
| `max_consecutive_failures` | `3` | Stop after this many turns in a row without progress |
| `tasks_file` / `handoff_file` | `TASKS.md` / `HANDOFF.md` | Shared files |
| `git_commit` | `true` | Commit after every turn |
| `prompt_file` | `""` | Your own turn prompt; placeholders `{agent}`, `{tasks_file}`, `{handoff_file}`, `{note}` |
| `notify_command` | `[]` | Run on hand-offs, waits and completion, with the message as the last argument, e.g. `["notify-send", "tagteam"]` |
| `agents.<name>` | see below | `type`, `command`, `args`, `max_usage`, `env` |

```json
{
  "main": "claude",
  "secondary": "codex",
  "agents": {
    "claude": {"type": "claude", "command": ["claude"], "args": ["--permission-mode", "acceptEdits"], "max_usage": 85},
    "codex":  {"type": "codex",  "command": ["codex"],  "args": ["--sandbox", "workspace-write"]}
  }
}
```

tagteam adds the flags it needs itself: `-p --output-format stream-json --verbose` for Claude and
`exec --json --skip-git-repo-check -` for Codex. The prompt is passed on stdin, so long prompts
work on Windows too. Use `args` for your own flags, such as a model choice.

## Permissions and safety

The agents run unattended, so nobody is there to approve a command. The defaults are cautious:

- **Claude:** `--permission-mode acceptEdits` lets it edit files in the project but not run shell
  commands. To let it run tests, add for example `"--allowedTools", "Bash(npm test)"` to `args`.
- **Codex:** `--sandbox workspace-write` confines writes to the project folder.

Avoid broad bypass flags (`--dangerously-skip-permissions`, `danger-full-access`) unless you
run inside a container or VM. tagteam itself only runs the agent CLIs and git. It keeps its state
and logs in `.tagteam/`, which it adds to `.git/info/exclude` instead of editing your
`.gitignore`.

## Other agents

```json
"agents": {
  "gemini": {
    "type": "custom",
    "command": ["gemini"],
    "args": ["-p", "{prompt}"],
    "prompt_via": "arg",
    "limit_pattern": "quota|rate limit|429"
  }
}
```

A custom agent's turn succeeds when the process exits with 0. A failed turn whose output
matches `limit_pattern` counts as a usage limit.

## Exit codes

`0` all tasks done · `1` setup error or no progress · `2` reached `max_iterations` ·
`3` every agent limited and waiting is off · `130` stopped with Ctrl+C or `tagteam stop`

## Limitations

- Usage readings come from what each CLI reports, and those formats can change between versions.
  The parsers ignore fields they don't recognise and fall back to error-message parsing, but check
  `tagteam check <agent>` after a CLI update.
- Hand-off happens between tasks. A very long task can still run into the hard limit. It is then
  committed as `WIP` and continued by the other agent (or by the same one after the reset).
- tagteam can't wake a suspended computer.
- Codex compresses older session files with zstd, which Python reads only from 3.14. On older
  Pythons the usage snapshot of such a thread is unknown until its next turn writes a fresh one;
  limits are still caught from the turn's error.
- The Codex desktop app mode needs Windows or macOS.
- Unattended runs need a sign-in that lasts: for Claude Code, `claude setup-token`.

## Development

```sh
python -m unittest discover -s tests -v
```

The tests run the real supervisor against fake `claude` and `codex` scripts that imitate
their output formats. That includes rate-limit events, Codex session files, hard limits
mid-task and resets. They need no network and no accounts. Fakes only prove tagteam's own logic;
check changes that touch a CLI against the real one (the parser tests include limit messages taken
from a real Codex install).
