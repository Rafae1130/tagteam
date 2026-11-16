"""Command line: tagteam init | run | sessions | handover | wakeup | discuss | gui | pet | status | stop | check."""

import argparse
import os
import signal
import socket
import sys
import time
from typing import Optional

from . import __version__, gitops, proc, sessions, tasks
from .config import ConfigError, build_config, find_config, write_default_config
from .lock import LOCK_NAME, _read, pid_alive
from .supervisor import EXIT_ERROR, Supervisor


def _as_interrupt(*_):
    raise KeyboardInterrupt


def graceful_signals() -> None:
    """A plain kill (SIGTERM), a closed terminal or SSH session (SIGHUP) and Ctrl+Break stop tagteam
    the way Ctrl+C does: the running agent is stopped with it and the state is saved. SIGHUP is
    left alone when it is ignored, so `nohup tagteam run &` keeps working."""
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            if name == "SIGHUP" and signal.getsignal(sig) == signal.SIG_IGN:
                continue
            signal.signal(sig, _as_interrupt)
        except (OSError, ValueError):
            pass


def active_run(root: str):
    """The lock of a tagteam run that is alive in this folder, or None."""
    data = _read(os.path.join(root, ".tagteam", LOCK_NAME))
    if data and data.get("host") == socket.gethostname() and pid_alive(int(data.get("pid") or 0)):
        return data
    return None


def cmd_init(root: str, args) -> int:
    os.makedirs(root, exist_ok=True)
    secondary = "" if args.solo else args.secondary
    if find_config(root):
        print(f"Config already exists: {find_config(root)}")
    else:
        print(f"Wrote {write_default_config(root, args.main, secondary)}")
    tpath = os.path.join(root, "TASKS.md")
    if os.path.exists(tpath):
        print(f"Kept existing {tpath}")
    else:
        with open(tpath, "w", encoding="utf-8") as f:
            f.write(tasks.TEMPLATE)
        print(f"Wrote {tpath}")
    if gitops.available() and not gitops.is_repo(root):
        if gitops.init(root):
            print(f"Initialised a git repository in {root}")
    if gitops.available() and gitops.is_repo(root):
        gitops.exclude(root)
    print("Edit TASKS.md, then run: tagteam run")
    return 0


def cmd_sessions(root: str, args) -> int:
    if args.all:
        folders = sessions.known_folders()
        print("\n".join(folders) if folders else "No folders with Claude Code or Codex sessions found.")
        return 0
    found = sessions.list_sessions(root)
    if not found:
        print(f"No Claude Code or Codex sessions found for {root}")
        return 0
    now = time.time()
    print(f"{'#':>2}  {'agent':6}  {'last active':11}  {'prompts':>7}  {'state':24}  {'id':8}  title")
    for i, s in enumerate(found, 1):
        where = "" if sessions.norm(s.cwd or root) == sessions.norm(root) else f"  [{os.path.relpath(s.cwd, root)}]"
        print(f"{i:>2}  {s.agent:6}  {sessions.ago(s.updated, now):11}  {s.prompts:>7}  "
              f"{sessions.state_text(s, now):24}  {s.short}  {s.title}{where}")
    print("\nContinue one: tagteam run --session <id>    Pick one in a window: tagteam gui")
    return 0


def _find(root: str, spec):
    try:
        return sessions.find_session(root, spec)
    except LookupError as e:
        print(f"tagteam: {e}", file=sys.stderr)
        return None


def codex_gui(cfg: dict) -> Optional[str]:
    """Run Codex turns in the Codex desktop app. Returns an error text when that cannot work here."""
    from . import codexdb

    if not codexdb.app_installed():
        return "the Codex desktop app is not installed here (it exists for Windows and macOS); drop the GUI option"
    for a in cfg["agents"].values():
        if a.get("type") == "codex":
            a["gui"] = True
    return None


def cmd_handoff(root: str, args, wakeup: bool = False) -> int:
    """Called from inside a chat: tagteam takes over that chat's session once the chat goes quiet."""
    s = _find(root, args.session)
    if not s:
        return EXIT_ERROR
    work = s.cwd if s.cwd and os.path.isdir(s.cwd) else root
    solo = wakeup or getattr(args, "solo", False)
    to = None if wakeup else args.to  # who takes over at the limit; never an instant switch
    at = None if solo else getattr(args, "handoff_at", None)
    argv = ["run", "-C", work, "--session", s.spec, "--wait-quiet", "--quiet-seconds", str(args.quiet_seconds)]
    if solo:
        argv.append("--solo")
    argv += ["--kind", "wakeup" if wakeup else "handover"]
    if to:
        argv += ["--secondary", to]
    if at is not None:
        argv += ["--handoff-at", str(at)]
    if not wakeup:
        argv.append("--note-written")
    if args.instruction:
        argv += ["--instruction", args.instruction]
    if args.no_commit:
        argv.append("--no-commit")
    if args.codex_gui:
        argv.append("--codex-gui")
    # Check the setup here, where someone reads the answer, not later in the background.
    try:
        cfg = build_config(work, {"secondary": "" if solo else to, "git_commit": False if args.no_commit else None})
        err = codex_gui(cfg) if args.codex_gui else None
        sup = Supervisor(work, cfg, echo=lambda _: None, session=s, handoff_at=at)
        err = err or sup.check_setup()
        if not err and to and to != sup.secondary:
            err = f"this session already runs on {to}; name the agent that should take over at the limit"
    except ConfigError as e:
        err = str(e)
    if err:
        print(f"tagteam: {err}", file=sys.stderr)
        return EXIT_ERROR
    if active_run(work):
        print(f"tagteam: a tagteam run is already active in {work}. Stop it first: tagteam stop -C \"{work}\"",
              file=sys.stderr)
        return EXIT_ERROR
    print(f'Session: {s.agent} {s.short} "{s.title}"')
    if args.foreground:
        return main(argv)
    log = os.path.join(work, ".tagteam", "handoff.log")
    p = proc.spawn_detached([proc.python_exe(), "-m", "tagteam"] + argv, log, cwd=work, env=proc.module_env())
    lead, other = sup.main, sup.secondary
    if wakeup:
        plan = (f"{lead} continues this session; whenever it reaches its usage limit, tagteam sleeps until the "
                "reset and wakes it up again")
    elif other:
        plan = (f"{lead} continues this session; when one of its usage windows reaches "
                f"{sup.max_usage(lead):.0f}%, {other} takes over, and {lead} gets the session back after its reset")
    else:
        plan = f"{lead} continues this session and sleeps through its usage limits"
    print(f"tagteam (pid {p.pid}) starts once this chat has been quiet for {args.quiet_seconds:.0f} s: {plan}.")
    if not wakeup:
        print(f"Before this chat stops: overwrite {cfg['handoff_file']} with a handoff note (goal, done, in progress, "
              "next steps). Then stop working in this chat; tagteam continues the session.")
    else:
        print("Stop working in this chat now; tagteam continues the session.")
    if args.codex_gui:
        print("Codex works in the Codex app: its thread opens there, so you can watch it and type into it.")
    print(f'Watch: tagteam pet -C "{work}"   Stop: tagteam stop -C "{work}"   Log: {log}')
    if args.pet:
        cmd_pet_detached(work)
    return 0


def cmd_stop(root: str) -> int:
    data = active_run(root)
    if not data:
        print(f"No tagteam run is active in {root}.")
        return 0
    with open(os.path.join(root, ".tagteam", "stop"), "w", encoding="utf-8") as f:
        f.write(str(time.time()))
    print(f"Asked tagteam (pid {data.get('pid')}) to stop; it stops its agent and exits within a few seconds.")
    return 0


def cmd_pet_detached(root: str) -> int:
    from .pet import check_available

    ok, why = check_available()
    if not ok:
        print(why)
        return 0
    proc.spawn_detached([proc.python_exe(), "-m", "tagteam", "pet", "-C", root],
                        os.path.join(root, ".tagteam", "pet.log"), cwd=root, env=proc.module_env())
    print("The tagteam pet is open (bottom right of the screen).")
    return 0


def cmd_gui(root: str, args) -> int:
    from .gui import check_gui, run_gui

    ok, why = check_gui()
    if not ok:
        print(why)
        return EXIT_ERROR
    if args.detach:
        log = os.path.join(os.path.expanduser("~"), ".tagteam-gui.log")
        proc.spawn_detached([proc.python_exe(), "-m", "tagteam", "gui", "-C", root], log, env=proc.module_env())
        print("The tagteam window is open.")
        return 0
    return run_gui(root)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(
        prog="tagteam",
        description="Run coding agents in shifts around their usage limits: hand work to a second "
                    "agent before the main one runs out, take it back when the limit resets, or "
                    "sleep until the reset with a single agent.")
    p.add_argument("--version", action="version", version=f"tagteam {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", "--dir", default=".", help="project directory (default: current directory)")
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser("init", parents=[common], help="create tagteam.json and TASKS.md")
    pi.add_argument("--main", default="claude")
    pi.add_argument("--secondary", default="codex")
    pi.add_argument("--solo", action="store_true", help="single agent: no secondary")

    pr = sub.add_parser("run", parents=[common], help="work through TASKS.md, or continue a session")
    pr.add_argument("--main", help="override the main agent")
    pr.add_argument("--secondary", help="override the secondary agent")
    pr.add_argument("--solo", action="store_true", help="single agent: wait for resets instead of handing off")
    g = pr.add_mutually_exclusive_group()
    g.add_argument("--wait", dest="wait", action="store_true", default=None,
                   help="sleep until a limit resets and continue (default)")
    g.add_argument("--no-wait", dest="wait", action="store_false", help="exit when every agent is limited")
    pr.add_argument("--max-iterations", type=int)
    pr.add_argument("--pet", action="store_true", help="also open the floating status window")
    pr.add_argument("--session", help="continue this Claude/Codex session instead of TASKS.md: an id (or its "
                                      "start), 'latest', 'claude' or 'codex' (see: tagteam sessions)")
    pr.add_argument("--instruction", help="what the session should do (default: continue where it left off)")
    pr.add_argument("--handoff-at", type=float, help="hand over at this %% of a usage window (default 95)")
    pr.add_argument("--no-commit", action="store_true", help="do not commit after each turn")
    pr.add_argument("--wait-quiet", action="store_true", help="first wait until the session's chat goes quiet")
    pr.add_argument("--quiet-seconds", type=float, default=30, help=argparse.SUPPRESS)
    pr.add_argument("--first", help="agent for the first turn")
    pr.add_argument("--kind", choices=["handover", "wakeup"], help=argparse.SUPPRESS)
    pr.add_argument("--note-written", action="store_true", help=argparse.SUPPRESS)
    pr.add_argument("--dry-run", action="store_true", help="check the setup and exit")
    pr.add_argument("--codex-gui", action="store_true", help="run Codex in the Codex desktop app (Windows/macOS) so you can watch it, instead of headless")

    ps = sub.add_parser("sessions", parents=[common], help="list the Claude Code and Codex sessions of a folder")
    ps.add_argument("--all", action="store_true", help="list the folders that have sessions instead")

    for name, text in (("handover", "hand this chat's session to tagteam, which continues it in the background"),
                       ("wakeup", "resume this chat's session automatically after each usage-limit reset")):
        ph = sub.add_parser(name, parents=[common], help=text, aliases=["handoff"] if name == "handover" else [])
        ph.add_argument("--session", default="latest", help="session id (default: the newest one in the folder)")
        if name == "handover":
            ph.add_argument("--to", help="agent that takes over when this session's agent reaches the "
                                         "threshold (default: the other configured agent); it does not switch now")
            ph.add_argument("--handoff-at", type=float, help="hand over at this %% of a usage window (default 95)")
            ph.add_argument("--solo", action="store_true", help="no second agent: sleep through limits")
        ph.add_argument("--instruction", help="what to do next (default: continue where it left off)")
        ph.add_argument("--no-commit", action="store_true", help="do not commit after each turn")
        ph.add_argument("--pet", action="store_true", help="also open the floating status window")
        ph.add_argument("--foreground", action="store_true", help="run here instead of in the background")
        ph.add_argument("--quiet-seconds", type=float, default=30, help=argparse.SUPPRESS)
        ph.add_argument("--codex-gui", action="store_true", help="run Codex in the Codex desktop app (Windows/macOS) so you can watch it, instead of headless")

    pd = sub.add_parser("discuss", parents=[common], help="two agents discuss a question until they agree")
    pd.add_argument("topic", nargs="*", help="the question or task to discuss")
    pd.add_argument("--file", help="read the topic from this file")
    pd.add_argument("--agents", help="the two agents, e.g. claude,codex (default: main and secondary)")
    pd.add_argument("--rounds", type=int, default=4, help="most rounds of replies before the final answer")
    pd.add_argument("--no-wait", dest="wait", action="store_false", default=None,
                    help="stop instead of waiting when an agent hits its usage limit")
    pd.add_argument("--codex-gui", action="store_true", help="run Codex in the Codex desktop app (Windows/macOS) so you can watch it, instead of headless")

    pg = sub.add_parser("gui", parents=[common], help="open the session picker window")
    pg.add_argument("--detach", action="store_true", help="open it and return at once")
    pp = sub.add_parser("pet", parents=[common], help="open the floating status window")
    pp.add_argument("--details", action="store_true", help=argparse.SUPPRESS)  # kept so old scripts still work
    pp.add_argument("--detach", action="store_true", help="open it and return at once")
    sub.add_parser("status", parents=[common], help="show tasks left and each agent's usage")
    sub.add_parser("stop", parents=[common], help="stop the tagteam run in this folder")
    pc = sub.add_parser("check", parents=[common], help="send one agent a tiny prompt to test the setup")
    pc.add_argument("agent")

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return EXIT_ERROR
    root = os.path.abspath(args.dir)
    if args.cmd == "init":
        return cmd_init(root, args)
    if args.cmd == "sessions":
        return cmd_sessions(root, args)
    if args.cmd in ("handover", "handoff", "wakeup"):
        return cmd_handoff(root, args, wakeup=args.cmd == "wakeup")
    if args.cmd == "stop":
        return cmd_stop(root)
    if args.cmd == "gui":
        return cmd_gui(root, args)
    if args.cmd == "pet" and args.detach:
        return cmd_pet_detached(root)
    if args.cmd == "pet":
        from .pet import run_pet

        return run_pet()
    overrides, session = {}, None
    if args.cmd == "run":
        if args.session:
            session = _find(root, args.session)
            if not session:
                return EXIT_ERROR
            if session.cwd and os.path.isdir(session.cwd):
                root = session.cwd  # resume where the session ran, or the agent cannot find it
        overrides = {"main": args.main, "secondary": "" if args.solo else args.secondary,
                     "wait_for_reset": args.wait, "max_iterations": args.max_iterations,
                     "git_commit": False if args.no_commit else None}
    if args.cmd == "discuss":
        overrides = {"wait_for_reset": args.wait}
    try:
        cfg = build_config(root, overrides)
        if getattr(args, "codex_gui", False):
            err = codex_gui(cfg)
            if err:
                raise ConfigError(err)
        if args.cmd == "discuss":
            from .discuss import run_discussion

            topic = " ".join(args.topic).strip()
            if args.file:
                with open(args.file, encoding="utf-8-sig") as f:
                    topic = (topic + "\n\n" + f.read()).strip()
            if not topic:
                print("tagteam: give a topic, e.g. tagteam discuss \"Which database should we use?\"", file=sys.stderr)
                return EXIT_ERROR
            graceful_signals()
            names = [n.strip() for n in args.agents.split(",")] if args.agents else None
            return run_discussion(root, cfg, topic, agents=names, rounds=max(1, args.rounds))
        if args.cmd == "run":
            sup = Supervisor(root, cfg, session=session, instruction=args.instruction, wait_quiet=args.wait_quiet,
                             quiet_s=args.quiet_seconds, first=args.first, note_written=args.note_written,
                             handoff_at=args.handoff_at, kind=args.kind)
        else:
            sup = Supervisor(root, cfg)
    except (ConfigError, OSError) as e:
        print(f"tagteam: {e}", file=sys.stderr)
        return EXIT_ERROR
    if args.cmd == "run":
        if args.dry_run:
            err = sup.check_setup()
            print(f"tagteam: {err}" if err else "Setup OK.")
            return EXIT_ERROR if err else 0
        graceful_signals()
        if args.pet:
            from .pet import spawn

            spawn(root)  # prints one line and carries on if the window can't open
        return sup.run()
    if args.cmd == "status":
        print(sup.status_text())
        return 0
    return sup.check_agent(args.agent)
