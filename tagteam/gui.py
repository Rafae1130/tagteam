"""The session picker window: choose a folder (drop it on the window, open it, or pick a recent
one), choose one of its Claude Code or Codex sessions, and let tagteam continue that session.

tkinter only, so there is nothing to install. Dropping folders works on Windows out of the box
(through the Win32 API) and elsewhere when the tkdnd Tk extension is installed; the Open folder
button and the Recent list work everywhere.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from . import gitops, proc, sessions
from .config import ConfigError, build_config
from .lock import pid_alive
from .supervisor import DEFAULT_INSTRUCTION
from .textparse import fmt_time

NOBODY = "Nobody: sleep until the reset"


def check_gui() -> Tuple[bool, str]:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        from .pet import install_hint

        return False, ("The tagteam window needs Tk, which this Python doesn't include. " + install_hint()
                       + " Everything else works without it: tagteam sessions, tagteam run --session ID.")
    if sys.platform not in ("win32", "darwin") and not os.environ.get("DISPLAY") \
            and not os.environ.get("WAYLAND_DISPLAY"):
        return False, ("No screen found (for example over SSH), so the window can't open. Use "
                       "tagteam sessions and tagteam run --session ID instead.")
    return True, ""


def resolve_drop(path: str) -> Tuple[str, Optional[str]]:
    """A dropped folder or transcript file -> (folder, session id to select or None)."""
    path = path.strip().strip("{}").strip('"')
    if os.path.isdir(path):
        return path, None
    if path.endswith(".jsonl") and os.path.isfile(path):
        s = sessions.read_file(path)
        if s and s.cwd:
            return s.cwd, s.id
    return os.path.dirname(path) or path, None


def _win_drop(root, on_paths) -> bool:
    """Accept folders dropped from Explorer, through the Win32 API (no extra package)."""
    import ctypes
    from ctypes import wintypes

    user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
    lresult = ctypes.c_ssize_t
    wndproc = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    setlong = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    setlong.restype, setlong.argtypes = ctypes.c_void_p, [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    user32.CallWindowProcW.restype = lresult
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                       wintypes.LPARAM]
    user32.GetParent.restype, user32.GetParent.argtypes = wintypes.HWND, [wintypes.HWND]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_wchar_p, wintypes.UINT]
    shell32.DragFinish.argtypes = [ctypes.c_void_p]
    shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
    pending: List[str] = []
    old = [None]

    def handler(h, msg, wp, lp):
        if msg == 0x0233:  # WM_DROPFILES
            for i in range(shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)):
                size = shell32.DragQueryFileW(wp, i, None, 0) + 1
                buf = ctypes.create_unicode_buffer(size)
                shell32.DragQueryFileW(wp, i, buf, size)
                pending.append(buf.value)
            shell32.DragFinish(wp)
            return 0
        return user32.CallWindowProcW(old[0], h, msg, wp, lp)

    cb = wndproc(handler)
    old[0] = setlong(hwnd, -4, ctypes.cast(cb, ctypes.c_void_p))  # GWLP_WNDPROC
    if not old[0]:
        return False
    shell32.DragAcceptFiles(hwnd, True)
    root._tagteam_drop = (cb, old, hwnd)  # keep the callback alive as long as the window

    def poll():
        if pending:
            paths = pending[:]
            del pending[:]
            on_paths(paths)
        root.after(150, poll)

    poll()
    return True


def _tkdnd_drop(root, on_paths) -> bool:
    """Drag and drop through the tkdnd extension, where it is installed (Linux, macOS)."""
    root.tk.call("package", "require", "tkdnd")
    root.tk.call("tkdnd::drop_target", "register", root._w, ("DND_Files",))

    def dropped(data):
        on_paths(list(root.tk.splitlist(data)))
        return "copy"

    root.tk.call("bind", root._w, "<<Drop:DND_Files>>", root.register(dropped) + " %D")
    return True


def _open_path(path: str) -> None:
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


def phase_text(data: Optional[dict]) -> Tuple[str, bool]:
    """The live state of a run as one line, and whether that run is still going."""
    if not data:
        return "", False
    alive = pid_alive(int(data.get("pid") or 0))
    ph = data.get("phase")
    if ph in ("done", "stopped") or not alive:
        if ph == "done":
            return "Finished.", False
        return ("Stopped: " + str(data.get("message") or "the run ended")), False
    if ph == "working":
        return f"Working: {data.get('agent')} - {sessions.clip(data.get('task'), 70)}", True
    if ph == "sleeping":
        return f"Sleeping until {fmt_time(data.get('until'))}, then continuing.", True
    if ph == "waiting":
        return str(data.get("message") or "Waiting..."), True
    return "Starting...", True


class Picker:
    def __init__(self, folder: Optional[str] = None):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk, self.ttk, self.filedialog, self.messagebox = tk, ttk, filedialog, messagebox
        if os.name == "nt":
            try:
                import ctypes

                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                pass
        self.root = tk.Tk()
        self.root.title("tagteam")
        self.root.geometry("1000x660")
        self.root.minsize(760, 520)
        self.folder = tk.StringVar(value=os.path.abspath(folder) if folder else "")
        self.items: Dict[str, sessions.Session] = {}
        self.work_dir: Optional[str] = None
        self.select_id: Optional[str] = None
        self.q: "queue.Queue" = queue.Queue()
        self._build()
        drop = False
        try:
            drop = _win_drop(self.root, self.on_drop) if os.name == "nt" else _tkdnd_drop(self.root, self.on_drop)
        except Exception:  # no drag and drop here; the buttons still work
            drop = False
        self.hint.config(text="Drop a project folder (or a session file) anywhere on this window, or use Open folder."
                         if drop else "Open a project folder, or pick a recent one.")
        threading.Thread(target=lambda: self.q.put(("recent", sessions.known_folders())), daemon=True).start()
        if folder:
            self.load(folder)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(300, self._tick)

    # ---- layout -------------------------------------------------------------

    def _build(self):
        tk, ttk, r = self.tk, self.ttk, self.root
        top = ttk.Frame(r)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="Project folder").pack(side="left")
        entry = ttk.Entry(top, textvariable=self.folder)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _e: self.load(self.folder.get()))
        ttk.Button(top, text="Open folder...", command=self.browse).pack(side="left")
        ttk.Button(top, text="Refresh", command=lambda: self.load(self.folder.get())).pack(side="left", padx=(6, 0))
        rec = ttk.Frame(r)
        rec.pack(fill="x", padx=10, pady=2)
        ttk.Label(rec, text="Recent").pack(side="left")
        self.recent = ttk.Combobox(rec, state="readonly")
        self.recent.pack(side="left", fill="x", expand=True, padx=(28, 0))
        self.recent.bind("<<ComboboxSelected>>", lambda _e: self.load(self.recent.get()))
        self.hint = ttk.Label(r, foreground="gray")
        self.hint.pack(fill="x", padx=10, pady=(2, 4))

        mid = ttk.Frame(r)
        mid.pack(fill="both", expand=True, padx=10, pady=4)
        cols = (("agent", "Agent", 70, "w", False), ("when", "Last active", 100, "w", False),
                ("prompts", "Prompts", 70, "e", False), ("state", "State", 170, "w", False),
                ("title", "Session", 520, "w", True))
        self.tree = ttk.Treeview(mid, columns=[c[0] for c in cols], show="headings", selectmode="browse")
        for key, label, width, anchor, stretch in cols:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor=anchor, stretch=stretch)
        bar = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_details())
        self.tree.bind("<Double-1>", lambda _e: self.start())

        self.details = tk.Text(r, height=6, wrap="word", relief="flat", borderwidth=0,
                               background=r.cget("background"))
        self.details.pack(fill="x", padx=12, pady=4)
        self.details.configure(state="disabled")

        opt = ttk.LabelFrame(r, text="When this session's agent reaches its usage limit")
        opt.pack(fill="x", padx=10, pady=4)
        row = ttk.Frame(opt)
        row.pack(fill="x", padx=8, pady=4)
        ttk.Label(row, text="Hand over to").pack(side="left")
        self.other = ttk.Combobox(row, state="readonly", width=30, values=[NOBODY])
        self.other.set(NOBODY)
        self.other.pack(side="left", padx=6)
        ttk.Label(row, text="at").pack(side="left")
        self.at = tk.Spinbox(row, from_=50, to=100, increment=5, width=5)
        self.at.delete(0, "end")
        self.at.insert(0, "95")
        self.at.pack(side="left", padx=4)
        ttk.Label(row, text="% of a usage window").pack(side="left")
        row2 = ttk.Frame(opt)
        row2.pack(fill="x", padx=8, pady=2)
        self.wait = tk.BooleanVar(value=True)
        self.quiet = tk.BooleanVar(value=True)
        self.commit = tk.BooleanVar(value=True)
        self.pet = tk.BooleanVar(value=False)
        from . import codexdb

        self.codex_gui = tk.BooleanVar(value=False)
        boxes = [("Sleep through limits and continue after the reset", self.wait),
                 ("Wait until the chat goes quiet first", self.quiet),
                 ("Commit each turn to git", self.commit), ("Show the floating pet", self.pet)]
        if codexdb.app_installed():
            boxes.append(("Codex works in the Codex app", self.codex_gui))
        for text, var in boxes:
            ttk.Checkbutton(row2, text=text, variable=var).pack(side="left", padx=(0, 14))
        row3 = ttk.Frame(opt)
        row3.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Label(row3, text="Instruction").pack(side="left")
        self.instr = ttk.Entry(row3)
        self.instr.insert(0, DEFAULT_INSTRUCTION)
        self.instr.pack(side="left", fill="x", expand=True, padx=6)

        bot = ttk.Frame(r)
        bot.pack(fill="x", padx=10, pady=(4, 12))
        self.start_btn = ttk.Button(bot, text="Continue this session", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bot, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(bot, text="Open log", command=self.open_log).pack(side="left")
        self.status = ttk.Label(bot, text="Choose a folder to see its sessions.")
        self.status.pack(side="left", padx=12)

    # ---- folders and sessions -------------------------------------------------

    def browse(self):
        d = self.filedialog.askdirectory(initialdir=self.folder.get() or os.path.expanduser("~"),
                                         title="Choose a project folder")
        if d:
            self.load(d)

    def on_drop(self, paths: List[str]):
        if paths:
            folder, sid = resolve_drop(paths[0])
            self.load(folder, select=sid)

    def load(self, folder: str, select: Optional[str] = None):
        folder = (folder or "").strip()
        if not folder:
            return
        if not os.path.isdir(folder):
            self.status.config(text=f"Not a folder: {folder}")
            return
        folder = os.path.abspath(folder)
        self.folder.set(folder)
        self.select_id = select
        self.status.config(text="Looking for sessions...")
        threading.Thread(target=lambda: self.q.put(("sessions", folder, sessions.list_sessions(folder))),
                         daemon=True).start()
        if not self.work_dir or not self._running():
            self.work_dir = folder

    def _fill(self, folder: str, found: List[sessions.Session]):
        if sessions.norm(folder) != sessions.norm(self.folder.get()):
            return  # an older search finished late
        self.tree.delete(*self.tree.get_children())
        self.items = {}
        now = time.time()
        for s in found:
            where = "" if sessions.norm(s.cwd or folder) == sessions.norm(folder) else \
                f"   [{os.path.relpath(s.cwd, folder)}]"
            self.items[s.path] = s
            self.tree.insert("", "end", iid=s.path, values=(
                sessions.AGENT_NAMES.get(s.agent, s.agent), sessions.ago(s.updated, now), s.prompts,
                sessions.state_text(s, now), s.title + where))
        if found:
            pick = next((s for s in found if self.select_id and s.id == self.select_id), found[0])
            self.tree.selection_set(pick.path)
            self.tree.see(pick.path)
            self.status.config(text=f"{len(found)} session(s). Pick one and press Continue this session.")
        else:
            self._details("")
            self.status.config(text="No Claude Code or Codex sessions in this folder yet.")

    def selected(self) -> Optional[sessions.Session]:
        sel = self.tree.selection()
        return self.items.get(sel[0]) if sel else None

    def _details(self, text: str):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def show_details(self):
        s = self.selected()
        if not s:
            return
        name = sessions.AGENT_NAMES.get(s.agent, s.agent)
        self._details(f"{s.title}\n\nStarted with: {sessions.clip(s.first_prompt, 300)}\n"
                      f"Last reply: {sessions.clip(s.last_reply, 300) or '-'}\n\n"
                      f"{name} session {s.id} in {s.cwd or '?'}")
        self.quiet.set(time.time() - s.updated < 600)
        try:
            cfg = build_config(s.cwd if s.cwd and os.path.isdir(s.cwd) else self.folder.get())
            others = [n for n, a in cfg["agents"].items() if a.get("type") != s.agent]
        except ConfigError:
            others = []
        self.other.configure(values=others + [NOBODY])
        self.other.set(others[0] if others else NOBODY)

    # ---- running ----------------------------------------------------------------

    def _live(self) -> Optional[dict]:
        if not self.work_dir:
            return None
        try:
            with open(os.path.join(self.work_dir, ".tagteam", "live.json"), encoding="utf-8-sig") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _running(self) -> bool:
        return phase_text(self._live())[1]

    def start(self):
        s = self.selected()
        if not s:
            self.messagebox.showinfo("tagteam", "Pick a session first.")
            return
        work = s.cwd if s.cwd and os.path.isdir(s.cwd) else self.folder.get()
        live_now = self.work_dir
        self.work_dir = work
        if self._running():
            self.messagebox.showinfo("tagteam", f"tagteam is already running in {work}. Stop it first.")
            return
        self.work_dir = live_now
        commit = self.commit.get()
        if commit and gitops.available() and not gitops.is_repo(work):
            ans = self.messagebox.askyesnocancel(
                "Not a git repository",
                f"{work} is not a git repository. tagteam commits after every turn, so you can review or undo "
                "each agent's work.\n\nCreate a repository now?\n(No: continue without commits)")
            if ans is None:
                return
            if ans:
                gitops.init(work)
                gitops.exclude(work)
            else:
                commit = False
        elif commit and not gitops.available():
            commit = False
        argv = ["run", "-C", work, "--session", s.spec]
        instr = self.instr.get().strip()
        if instr and instr != DEFAULT_INSTRUCTION:
            argv += ["--instruction", instr]
        other = self.other.get()
        if other == NOBODY or not other:
            argv.append("--solo")
        else:
            argv += ["--secondary", other, "--handoff-at", self.at.get()]
        if not self.wait.get():
            argv.append("--no-wait")
        if self.quiet.get():
            argv.append("--wait-quiet")
        if not commit:
            argv.append("--no-commit")
        if self.codex_gui.get():
            argv.append("--codex-gui")
        base = [proc.python_exe(), "-m", "tagteam"]
        kw = {"creationflags": proc.CREATE_NO_WINDOW} if os.name == "nt" else {}
        chk = subprocess.run(base + argv + ["--dry-run"], capture_output=True, text=True, env=proc.module_env(),
                             cwd=work, timeout=60, **kw)
        if chk.returncode != 0:
            self.messagebox.showerror("tagteam", (chk.stdout + chk.stderr).strip() or "The setup check failed.")
            return
        if self.pet.get():
            argv.append("--pet")
        proc.spawn_detached(base + argv, os.path.join(work, ".tagteam", "gui-run.log"), cwd=work,
                            env=proc.module_env())
        self.work_dir = work
        self.status.config(text="Starting...")

    def stop(self):
        if self.work_dir and self._running():
            with open(os.path.join(self.work_dir, ".tagteam", "stop"), "w", encoding="utf-8") as f:
                f.write(str(time.time()))
            self.status.config(text="Stopping...")

    def open_log(self):
        d = self.work_dir or self.folder.get()
        path = os.path.join(d, ".tagteam", "tagteam.log") if d else ""
        if path and os.path.isfile(path):
            _open_path(path)
        else:
            self.messagebox.showinfo("tagteam", "No log yet for this folder.")

    def _tick(self):
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            if item[0] == "sessions":
                self._fill(item[1], item[2])
            elif item[0] == "recent":
                self.recent.configure(values=item[1])
        text, running = phase_text(self._live())
        if text:
            self.status.config(text=text)
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.root.after(1000, self._tick)

    def close(self):
        if self._running():
            ans = self.messagebox.askyesnocancel(
                "tagteam is still running",
                "Keep tagteam working in the background after this window closes?\n\n"
                "Yes: keep it running.   No: stop it.")
            if ans is None:
                return
            if ans is False:
                self.stop()
        self.root.destroy()


def run_gui(folder: Optional[str] = None) -> int:
    ok, why = check_gui()
    if not ok:
        print(why)
        return 1
    Picker(folder).root.mainloop()
    return 0
