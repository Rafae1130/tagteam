"""The pet: a small floating window with three bars, Discussion, Handover and Wakeup.

Each bar counts the runs of that kind that are active on this computer right now, with one
segment per run coloured by what it is doing; hovering over a bar lists each run's status.

Built on tkinter, which ships with Python. Some Python installs leave Tk out (Debian and
Ubuntu put it in the python3-tk package, Homebrew in python-tk). Then the pet does not open:
tagteam prints the one command that installs Tk and keeps working in the terminal.

The pet runs as its own process and only reads the files tagteam writes, so closing it never
affects the work.
"""

import json
import math
import os
import subprocess
import sys
import time

from . import registry, tasks
from .lock import pid_alive
from .textparse import fmt_dur, fmt_time

DEFAULT_COLORS = ["#E07A4F", "#2A9D8F", "#7C6FD6", "#C9A227"]
LIVE_PHASES = ("starting", "working", "sleeping")

CARD, BORDER, FG, MUTED, TRACK, INK, GREY = (
    "#1f2023", "#3d3e43", "#f1f1f1", "#a7a7ad", "#3a3b40", "#26272b", "#9aa0a6")


# ---- status, read from tagteam's files --------------------------------------

def _read_json(path):
    for _ in range(3):
        try:
            with open(path, encoding="utf-8-sig") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            time.sleep(0.05)  # caught mid-write; try again
    return None


def _tail(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def snapshot(sup) -> dict:
    """One folder's run status, read fresh from disk."""
    sup.state = sup._load_state()
    now = time.time()
    live = _read_json(os.path.join(sup.dir, "live.json")) or {}
    phase = live.get("phase") or "idle"
    message = live.get("message") or ""
    if phase in LIVE_PHASES and not pid_alive(int(live.get("pid") or -1)):
        phase, message = "stopped", "tagteam is not running"
    order = live.get("order")  # the run may use --solo / --main / --secondary
    if order and order != sup.order and all(n in sup.cfg["agents"] for n in order):
        sup.order, sup.main = list(order), order[0]
        sup.secondary = order[1] if len(order) > 1 else None
    done, todo = tasks.counts(sup.tasks_path)
    agents = []
    for i, name in enumerate(sup.order):
        a = sup.state["agents"].get(name) or {}
        until, why = sup.blocked(name, now)
        wins = [{"name": k, "used": w.get("used"), "resets_at": w.get("resets_at")}
                for k, w in sorted((a.get("windows") or {}).items())
                if not (w.get("resets_at") and w["resets_at"] <= now)]
        agents.append({
            "name": name, "role": "main" if i == 0 else "secondary",
            "color": sup.cfg["agents"].get(name, {}).get("color") or DEFAULT_COLORS[i % len(DEFAULT_COLORS)],
            "blocked_until": until, "reason": why, "windows": wins,
            "working": phase == "working" and live.get("agent") == name,
        })
    return {
        "now": now, "phase": phase, "agent": live.get("agent"),
        "task": live.get("task") or (todo[0] if todo else ""),
        "until": live.get("until"), "message": message, "switch": live.get("switch"),
        "tasks_done": done, "tasks_total": done + len(todo), "agents": agents,
        "log": _tail(os.path.join(sup.dir, "tagteam.log"), 6), "project": os.path.basename(sup.root),
    }


# ---- can a window open here? -------------------------------------------------

def _os_release_ids():
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            data = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
    except OSError:
        return set()
    ids = set()
    for key in ("ID", "ID_LIKE"):
        ids.update(data.get(key, "").strip('"').lower().split())
    return ids


def install_hint() -> str:
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    if os.name == "nt":
        return "Run the Python installer again, choose Modify, and tick 'tcl/tk and IDLE'."
    if sys.platform == "darwin":
        exe = os.path.realpath(sys.executable).lower()
        if "homebrew" in exe or "/cellar/" in exe:
            return f"Install it with: brew install python-tk@{ver}"
        return f"Install Python from python.org (it includes Tk), or with Homebrew: brew install python-tk@{ver}"
    ids = _os_release_ids()
    if ids & {"debian", "ubuntu"}:
        return "Install it with: sudo apt install python3-tk"
    if ids & {"fedora", "rhel", "centos"}:
        return "Install it with: sudo dnf install python3-tkinter"
    if ids & {"arch", "manjaro"}:
        return "Install it with: sudo pacman -S tk"
    if ids & {"opensuse", "suse", "sles", "opensuse-leap", "opensuse-tumbleweed"}:
        return "Install it with: sudo zypper install python3-tk"
    if "alpine" in ids:
        return "Install it with: sudo apk add py3-tkinter"
    return "Install your system's Tk package for Python (often called python3-tk)."


def check_available():
    """(True, "") if the pet can open here, else (False, a one-line reason)."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False, ("The tagteam pet needs Tk, which this Python doesn't include. " + install_hint()
                       + " tagteam keeps working without the pet.")
    if sys.platform not in ("win32", "darwin") and not os.environ.get("DISPLAY"):
        return False, ("No screen found (for example over SSH), so the pet window can't open. "
                       "tagteam keeps working without it.")
    return True, ""


def spawn(root: str):
    """Start the pet as its own process for `tagteam run --pet`. Never raises."""
    ok, why = check_available()
    if not ok:
        print(why)
        return None
    env = dict(os.environ)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    try:
        return subprocess.Popen([sys.executable, "-m", "tagteam", "pet", "-C", root],
                                stdin=subprocess.DEVNULL, env=env, **kw)
    except OSError as e:
        print(f"The pet could not start ({e}). tagteam keeps working without it.")
        return None


# ---- every active run on this computer, for the three bars ---------------------

KIND_LABELS = (("discussion", "Discussion"), ("handover", "Handover"), ("wakeup", "Wakeup"))
AGENT_COLORS = {"claude": DEFAULT_COLORS[0], "codex": DEFAULT_COLORS[1]}
SLEEP_COLOR = "#6E8CD8"


def agent_color(name) -> str:
    return AGENT_COLORS.get(str(name or "").lower(), DEFAULT_COLORS[2])


def _usage(root: str, names, now: float) -> list:
    """[(agent, % of its fullest current usage window)], from that run's state file."""
    agents = (_read_json(os.path.join(root, ".tagteam", "state.json")) or {}).get("agents") or {}
    out = []
    for n in names or []:
        used = [w.get("used") for w in ((agents.get(n) or {}).get("windows") or {}).values()
                if isinstance(w, dict) and w.get("used") is not None
                and not (w.get("resets_at") and w["resets_at"] <= now)]
        if used:
            out.append((n, float(max(used))))
    return out


def describe(entry: dict, now: float) -> dict:
    """One active run as the pet shows it: a colour for its bar segment and a line of status."""
    root = str(entry.get("root") or "")
    live = _read_json(os.path.join(root, ".tagteam", "live.json")) or {}
    if live.get("pid") != entry.get("pid"):
        live = {}  # left by an earlier run; this one has not written its status yet
    phase, agent = live.get("phase") or "starting", str(live.get("agent") or "")
    if phase == "working":
        state, color = "working", agent_color(agent)
        task = str(live.get("task") or "")
        if entry.get("kind") == "discussion" and task.startswith("discussion: "):
            text = f"{agent.capitalize()}: {task[len('discussion: '):]}"
        else:
            text = f"{agent.capitalize()} is working"
    elif phase == "sleeping":
        until = live.get("until")
        state, color = "sleeping", SLEEP_COLOR
        text = f"sleeping until {fmt_time(until)} (usage limit)" if until else "sleeping until a usage limit resets"
    else:
        state, color = "waiting", GREY
        text = {"waiting": "waiting for the chat to go quiet", "done": "finishing",
                "stopped": "stopping"}.get(phase, "starting")
    usage = _usage(root, live.get("order"), now)
    return {"kind": entry.get("kind"), "folder": os.path.basename(root.rstrip("\\/")) or root,
            "title": str(entry.get("title") or ""), "state": state, "color": color, "agent": agent,
            "text": text, "usage": ", ".join(f"{n} {u:.0f}%" for n, u in usage), "usage_list": usage}


def overview() -> dict:
    """{kind: [described run, ...]} for every active run on this computer."""
    now = time.time()
    out = {k: [] for k, _ in KIND_LABELS}
    for e in registry.active():
        if e.get("kind") in out:
            out[e["kind"]].append(describe(e, now))
    return out


def tip_text(kind: str, items) -> str:
    label = dict(KIND_LABELS)[kind]
    if not items:
        return f"No {label.lower()} running"
    lines = [f"{label}: {len(items)} running"]
    for it in items:
        lines.append("• " + it["folder"] + (f"  ·  {_short(it['title'], 44)}" if it["title"] else ""))
        lines.append("   " + it["text"] + (f"  ·  {it['usage']}" if it["usage"] else ""))
    return "\n".join(lines)


# ---- the window --------------------------------------------------------------

KEY = "#010203"  # painted where the window is see-through (its rounded corners; Windows only)
CARD_BG, ROW_BG, ROW_HOVER, DIM = "#18191d", "#222429", "#2a2d33", "#6d7078"
ACCENTS = {"discussion": "#A78BFA", "handover": "#FF8A5B", "wakeup": "#F5C451"}
START_HINT = {"discussion": "/tagteam:discussion", "handover": "/tagteam:handover", "wakeup": "/tagteam:wakeup"}


def _short(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 3] + "..."


def _mix(a: str, b: str, t: float) -> str:
    """The colour t of the way from a to b."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * max(0.0, min(1.0, t))) for x, y in zip(ca, cb))


def _summaries(items):
    """Ways to sum up a row, longest first; the window uses the first one that fits."""
    if not items:
        return ["none running"]
    n = {}
    for it in items:
        n[it["state"]] = n.get(it["state"], 0) + 1
    states = [s for s in ("working", "sleeping", "waiting") if n.get(s)]
    short = {"working": "work", "sleeping": "sleep", "waiting": "wait"}
    return [" · ".join(f"{n[s]} {s}" for s in states),
            " · ".join(f"{n[s]} {short[s]}" for s in states)]


class Pet:
    W = 256  # sizes before DPI scaling
    TOP = 106  # the creature's area
    ROW_H, ROW_GAP, PAD = 54, 8, 10
    H = TOP + 3 * ROW_H + 2 * ROW_GAP + PAD
    TIP_W = 300

    def __init__(self):
        import tkinter as tk
        import tkinter.font as tkfont

        if os.name == "nt":
            try:
                import ctypes

                ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
            except Exception:
                pass
        self.tk = tk
        self.view = {k: [] for k, _ in KIND_LABELS}
        self.offline = False
        self.hover_kind = None
        self.tip = self.tip_cv = self.tip_kind = None
        self.segs = []
        self.t0 = time.time()
        self.root = tk.Tk()
        self.root.withdraw()  # stay hidden until it is borderless and placed, so it doesn't flash
        self.root.title("tagteam")
        self.k = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        base = tkfont.nametofont("TkDefaultFont")
        self.fonts = {}
        for name, size, weight in (("title", 10, "bold"), ("count", 16, "bold"), ("bold", 9, "bold"),
                                   ("small", 8, "normal")):
            f = base.copy()
            f.configure(size=size, weight=weight)
            self.fonts[name] = f
        self.on_top = tk.BooleanVar(value=True)
        self.rounded = os.name == "nt"
        self.key = KEY if self.rounded else CARD_BG
        self.cur_h = int(self.p(self.H))
        self.root.configure(bg=self.key)
        self.cv = tk.Canvas(self.root, width=int(self.p(self.W)), height=self.cur_h, bg=self.key, bd=0,
                            highlightthickness=0)
        self.cv.pack()
        if self.rounded:
            try:
                self.root.attributes("-transparentcolor", KEY)
            except tk.TclError:
                self.rounded, self.key = False, CARD_BG
                self.root.configure(bg=CARD_BG)
                self.cv.configure(bg=CARD_BG)
        self.rrect(self.cv, 0.5, 0.5, self.W - 0.5, self.H - 0.5, 16, fill=CARD_BG, outline=BORDER, tags="card")
        self.root.update_idletasks()
        self.root.overrideredirect(True)  # no title bar: just the pet
        self.root.attributes("-topmost", True)
        # Our own record of the position: winfo_x/y are not reliable until the window is mapped.
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.x = int(sw - self.p(self.W) - self.p(24))
        self.y = int(sh - self.cur_h - self.p(80))
        self.place()
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_checkbutton(label="Keep on top", variable=self.on_top, command=self.apply_top)
        self.menu.add_separator()
        self.menu.add_command(label="Close pet", command=self.root.destroy)
        self.cv.bind("<ButtonPress-1>", self.drag_start)
        self.cv.bind("<B1-Motion>", self.drag_move)
        self.cv.bind("<Motion>", self.hover)
        self.cv.bind("<Leave>", lambda e: self.set_hover(None))
        for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            self.cv.bind(seq, self.show_menu)
        self.root.deiconify()
        self.root.attributes("-topmost", True)

    # drawing helpers (coordinates are before DPI scaling)
    def p(self, v):
        return v * self.k

    def pts(self, points, dx=0.0, dy=0.0):
        out = []
        for x, y in points:
            out += [self.p(x + dx), self.p(y + dy)]
        return out

    def rrect(self, cv, x0, y0, x1, y1, r, **kw):
        r = max(0.5, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
        pts = [(x0 + r, y0), (x0 + r, y0), (x1 - r, y0), (x1 - r, y0), (x1, y0), (x1, y0 + r), (x1, y0 + r),
               (x1, y1 - r), (x1, y1 - r), (x1, y1), (x1 - r, y1), (x1 - r, y1), (x0 + r, y1), (x0 + r, y1),
               (x0, y1), (x0, y1 - r), (x0, y1 - r), (x0, y0 + r), (x0, y0 + r), (x0, y0)]
        return cv.create_polygon(*self.pts(pts), smooth=True, **kw)

    # interaction
    def drag_start(self, e):
        self.hide_tip()
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def drag_move(self, e):
        self.x, self.y = e.x_root - self._dx, e.y_root - self._dy
        self.place()

    def place(self):
        self.root.geometry(f"{int(self.p(self.W))}x{self.cur_h}+{self.x}+{self.y}")

    def show_menu(self, e):
        self.set_hover(None)
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def apply_top(self):
        self.root.attributes("-topmost", bool(self.on_top.get()))

    def row_top(self, i):
        return self.TOP + i * (self.ROW_H + self.ROW_GAP)

    def row_center(self, kind):
        """Where a bar's row is, in window pixels."""
        i = [k for k, _ in KIND_LABELS].index(kind)
        return int(self.p(self.W / 2)), int(self.p(self.row_top(i) + self.ROW_H / 2))

    def row_at(self, x_px, y_px):
        x, y = x_px / self.k, y_px / self.k
        for i, (kind, _) in enumerate(KIND_LABELS):
            if self.PAD <= x <= self.W - self.PAD and self.row_top(i) <= y <= self.row_top(i) + self.ROW_H:
                return kind
        return None

    def hover(self, e):
        kind = self.row_at(e.x, e.y)
        self.set_hover(kind)
        if kind and kind != self.tip_kind:
            self.show_tip(kind)

    def set_hover(self, kind):
        if kind != self.hover_kind:
            self.hover_kind = kind
            self.draw_info()
        if kind is None:
            self.hide_tip()

    # the creature: shows the busiest of all runs
    def look(self):
        items = [it for v in self.view.values() for it in v]
        if self.offline or not items:
            return GREY, "line", "flat", "still", False
        working = [it for it in items if it["state"] == "working"]
        if working:
            return working[0]["color"], "open", "smile", "bob", True
        if any(it["state"] == "sleeping" for it in items):
            return SLEEP_COLOR, "sleep", "small", "breathe", True
        return GREY, "open", "smile", "still", False

    def draw_pet(self, t):
        color, eyes, mouth, anim, glow = self.look()
        cx, dy, sx = self.W / 2, 0.0, 1.0
        if anim == "bob":
            dy = -3 * (1 + math.sin(t * 4))
        elif anim == "breathe":
            sx = 1 + 0.03 * math.sin(t * 1.5)
        if eyes == "open" and (t % 4.2) < 0.15:
            eyes = "line"  # blink
        cv = self.cv
        cv.delete("pet")
        if glow:  # a soft halo in the creature's colour
            for r, mix in ((50, 0.07), (44, 0.12), (38, 0.18)):
                cv.create_oval(*self.pts([(cx - r, 66 - r * 0.8), (cx + r, 66 + r * 0.8)]),
                               fill=_mix(CARD_BG, color, mix), outline="", tags="pet")
        cv.create_oval(*self.pts([(cx - 31, 96), (cx + 31, 102)]), fill="#101114", outline="", tags="pet")
        blob = [(-34, 94), (-37, 72), (-32, 52), (-18, 39), (0, 35), (18, 39), (32, 52), (37, 72), (34, 94), (0, 98)]
        cv.create_polygon(*self.pts([(cx + x * sx, y) for x, y in blob], dy=dy), smooth=True, fill=color,
                          outline="", tags="pet")
        cv.create_oval(*self.pts([(cx - 23, 45), (cx - 11, 52)], dy=dy), fill=_mix(color, "#ffffff", 0.4),
                       outline="", tags="pet")
        w = max(1, int(self.p(2.6)))
        if eyes == "open":
            for x in (cx - 12, cx + 12):
                cv.create_oval(*self.pts([(x - 4, 58), (x + 4, 66)], dy=dy), fill=INK, outline="", tags="pet")
                cv.create_oval(*self.pts([(x - 1.5, 59.5), (x + 0.5, 61.5)], dy=dy), fill="#ffffff", outline="",
                               tags="pet")
            for x in (cx - 21, cx + 21):  # cheeks
                cv.create_oval(*self.pts([(x - 4, 70), (x + 4, 74)], dy=dy), fill=_mix(color, "#ff5d8f", 0.45),
                               outline="", tags="pet")
        else:
            shape = {"sleep": [(-5, 61), (0, 65), (5, 61)], "line": [(-4, 62), (0, 62), (4, 62)]}[eyes]
            for x in (cx - 12, cx + 12):
                cv.create_line(*self.pts([(x + a, b) for a, b in shape], dy=dy), fill=INK, width=w,
                               smooth=True, capstyle="round", tags="pet")
        m = {"smile": [(-8, 76), (0, 81), (8, 76)], "small": [(-4, 78), (0, 80), (4, 78)],
             "flat": [(-7, 79), (0, 79), (7, 79)]}[mouth]
        cv.create_line(*self.pts([(cx + a, b) for a, b in m], dy=dy), fill=INK, width=w, smooth=True,
                       capstyle="round", tags="pet")
        if eyes == "sleep":
            for i in range(3):
                ph = ((t / 3.0) + i / 3.0) % 1.0
                cv.create_text(self.p(cx + 36 + i * 4 + ph * 10), self.p(46 - ph * 26), text="z",
                               fill=_mix(MUTED, CARD_BG, ph), font=self.fonts["bold"], tags="pet")

    # one icon per kind, drawn so nothing outside the standard library is needed
    def icon(self, kind, cx, cy, color):
        cv, w = self.cv, max(1, int(self.p(1.7)))
        if kind == "discussion":  # two speech bubbles
            self.rrect(cv, cx - 9, cy - 8, cx + 3, cy + 1, 3, fill="", outline=color, width=w, tags="info")
            cv.create_line(*self.pts([(cx - 6, cy + 1), (cx - 7, cy + 4.5), (cx - 3, cy + 1)]), fill=color, width=w,
                           tags="info")
            self.rrect(cv, cx - 2, cy - 3, cx + 9, cy + 6, 3, fill=color, outline="", tags="info")
            cv.create_polygon(*self.pts([(cx + 4, cy + 5), (cx + 7.5, cy + 9), (cx + 7.5, cy + 5)]), fill=color,
                              outline="", tags="info")
        elif kind == "handover":  # passing the work back and forth
            shape = (self.p(5), self.p(5.5), self.p(2.5))
            cv.create_line(*self.pts([(cx - 8, cy - 3.5), (cx + 8, cy - 3.5)]), fill=color, width=w, arrow="last",
                           arrowshape=shape, tags="info")
            cv.create_line(*self.pts([(cx + 8, cy + 3.5), (cx - 8, cy + 3.5)]), fill=color, width=w, arrow="last",
                           arrowshape=shape, tags="info")
        else:  # wakeup: an alarm clock
            cv.create_oval(*self.pts([(cx - 6.5, cy - 4.5), (cx + 6.5, cy + 8.5)]), outline=color, width=w, tags="info")
            cv.create_line(*self.pts([(cx, cy + 2), (cx, cy - 1.5), (cx, cy + 2), (cx + 3, cy + 2)]), fill=color,
                           width=w, tags="info")
            for s in (-1, 1):
                cv.create_oval(*self.pts([(cx + s * 6 - 2.5, cy - 9), (cx + s * 6 + 2.5, cy - 4)]), fill=color,
                               outline="", tags="info")
                cv.create_line(*self.pts([(cx + s * 4, cy + 8), (cx + s * 6, cy + 10.5)]), fill=color, width=w,
                               tags="info")

    # the three rows; refreshed every second and on hover
    def draw_info(self):
        cv = self.cv
        cv.delete("info")
        self.segs = []
        f = self.fonts
        for i, (kind, label) in enumerate(KIND_LABELS):
            y = self.row_top(i)
            items = self.view.get(kind) or []
            accent, hot = ACCENTS[kind], self.hover_kind == kind
            self.rrect(cv, self.PAD, y, self.W - self.PAD, y + self.ROW_H, 11, fill=ROW_HOVER if hot else ROW_BG,
                       outline=accent if hot else "", width=max(1, int(self.p(1.3))), tags="info")
            cx, cy = self.PAD + 22, y + 21
            cv.create_oval(*self.pts([(cx - 13, cy - 13), (cx + 13, cy + 13)]),
                           fill=_mix(ROW_BG, accent, 0.22 if items else 0.08), outline="", tags="info")
            self.icon(kind, cx, cy, accent if items else DIM)
            cv.create_text(self.p(self.PAD + 43), self.p(y + 15), text=label, anchor="w",
                           fill=FG if items else MUTED, font=f["title"], tags="info")
            count = str(len(items))
            room = self.W - self.PAD - 12 - f["count"].measure(count) / self.k - 8 - (self.PAD + 43)
            ways = _summaries(items)
            summary = next((t for t in ways if f["small"].measure(t) <= self.p(room)), None)
            cv.create_text(self.p(self.PAD + 43), self.p(y + 30), anchor="w", fill=MUTED if items else DIM,
                           font=f["small"], tags="info",
                           text=summary if summary is not None else self._fit(ways[-1], f["small"], room))
            cv.create_text(self.p(self.W - self.PAD - 12), self.p(y + 22), text=count, anchor="e",
                           fill=accent if items else DIM, font=f["count"], tags="info")
            x0, x1, by = self.PAD + 10, self.W - self.PAD - 10, y + 44
            self.rrect(cv, x0, by - 3, x1, by + 3, 3, fill=TRACK, outline="", tags="info")
            if items:
                gap = 3
                seg = (x1 - x0 - gap * (len(items) - 1)) / len(items)
                for j, it in enumerate(items):
                    sx = x0 + j * (seg + gap)
                    iid = self.rrect(cv, sx, by - 3, sx + seg, by + 3, 3, fill=it["color"], outline="", tags="info")
                    self.segs.append((sx, by - 3, sx + seg, by + 3, it["state"], it["color"], iid, j))
        if self.offline:
            cv.create_text(self.p(self.W / 2), self.p(self.H - 6), text="Can't read tagteam's status", fill=MUTED,
                           font=f["small"], tags="info")
        if self.tip_kind:
            w, h = self.fill_tip()
            self.tip.geometry(f"{w}x{h}")

    # movement: a shine across working segments, a slow pulse on sleeping ones, the creature
    def tick(self):
        t = time.time() - self.t0
        self.draw_pet(t)
        cv = self.cv
        cv.delete("anim")
        for x0, y0, x1, y1, state, color, iid, j in self.segs:
            if state == "working":
                band = 16
                pos = (t * 70 + j * 41) % (x1 - x0 + 2 * band) - band
                a, b = max(x0 + 1.5, x0 + pos), min(x1 - 1.5, x0 + pos + band)
                if b > a:
                    cv.create_rectangle(*self.pts([(a, y0 + 1), (b, y1 - 1)]), fill=_mix(color, "#ffffff", 0.45),
                                        outline="", tags="anim")
            elif state == "sleeping":
                cv.itemconfigure(iid, fill=_mix(SLEEP_COLOR, ROW_BG, 0.5 * (1 + math.sin(t * 2.2 + j)) / 2))
        self.root.after(50, self.tick)

    # the hover card: every run of one kind with its status and usage
    def show_tip(self, kind):
        """The hover card, docked beside the pet (left of it, or right when there is no room) and level
        with the hovered row, so it never covers the bars."""
        tk = self.tk
        if self.tip is None:
            self.tip = tk.Toplevel(self.root)
            self.tip.withdraw()
            self.tip.overrideredirect(True)
            self.tip.attributes("-topmost", True)
            self.tip.configure(bg=self.key)
            if self.rounded:
                try:
                    self.tip.attributes("-transparentcolor", KEY)
                except tk.TclError:
                    pass
            self.tip_cv = tk.Canvas(self.tip, bg=self.key, highlightthickness=0, bd=0)
            self.tip_cv.pack()
        self.tip_kind = kind
        w, h = self.fill_tip()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        gap = int(self.p(10))
        left, right = self.x - w - gap, self.x + int(self.p(self.W)) + gap
        x = left if left >= 0 or right + w > sw else right
        row_y = self.y + self.row_center(kind)[1]
        y = max(0, min(row_y - h // 2, sh - h - 8))
        self.tip.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
        self.tip.deiconify()
        self.tip.lift()

    def hide_tip(self):
        self.tip_kind = None
        if self.tip is not None:
            self.tip.withdraw()

    def _fit(self, text, font, width):
        """text cut with '...' to fit `width` (before scaling)."""
        text = " ".join(str(text or "").split())
        if font.measure(text) <= self.p(width):
            return text
        while text and font.measure(text + "...") > self.p(width):
            text = text[:-1]
        return text + "..."

    def fill_tip(self):
        kind, cv, f, W = self.tip_kind, self.tip_cv, self.fonts, self.TIP_W
        items = self.view.get(kind) or []
        accent, label = ACCENTS[kind], dict(KIND_LABELS)[kind]
        cv.delete("all")
        cv.create_oval(*self.pts([(14, 14), (24, 24)]), fill=accent, outline="")
        cv.create_text(self.p(31), self.p(19), text=label, anchor="w", fill=FG, font=f["title"])
        cv.create_text(self.p(W - 14), self.p(19), text=f"{len(items)} running" if items else "none running",
                       anchor="e", fill=MUTED, font=f["small"])
        cv.create_line(*self.pts([(14, 34), (W - 14, 34)]), fill=BORDER)
        y, bottom = 50, 50
        if not items:
            cv.create_text(self.p(14), self.p(y), anchor="w", fill=DIM, font=f["small"],
                           text=f"Start one with {START_HINT[kind]}")
        for n, it in enumerate(items):
            cv.create_oval(*self.pts([(14, y - 4), (22, y + 4)]), fill=it["color"], outline="")
            folder = self._fit(it["folder"], f["bold"], W - 44)
            cv.create_text(self.p(30), self.p(y), anchor="w", text=folder, fill=FG, font=f["bold"])
            fx = 30 + f["bold"].measure(folder) / self.k + 8
            if it["title"] and W - 14 - fx > 30:
                cv.create_text(self.p(fx), self.p(y), anchor="w", fill=MUTED, font=f["small"],
                               text=self._fit(it["title"], f["small"], W - 14 - fx))
            tone = {"working": _mix(it["color"], "#ffffff", 0.25),
                    "sleeping": _mix(SLEEP_COLOR, "#ffffff", 0.3)}.get(it["state"], MUTED)
            cv.create_text(self.p(30), self.p(y + 16), anchor="w", fill=tone, font=f["small"],
                           text=self._fit(it["text"], f["small"], W - 44))
            bottom = y + 16
            if it.get("usage_list"):
                x, uy = 30.0, y + 32
                for name, pct in it["usage_list"]:
                    cv.create_text(self.p(x), self.p(uy), anchor="w", text=name, fill=MUTED, font=f["small"])
                    x += f["small"].measure(name) / self.k + 6
                    self.rrect(cv, x, uy - 2.5, x + 44, uy + 2.5, 2.5, fill=TRACK, outline="")
                    if pct > 0:
                        self.rrect(cv, x, uy - 2.5, x + 44 * min(pct, 100) / 100, uy + 2.5, 2.5,
                                   fill=agent_color(name), outline="")
                    x += 50
                    txt = f"{pct:.0f}%"
                    cv.create_text(self.p(x), self.p(uy), anchor="w", text=txt, fill=FG, font=f["small"])
                    x += f["small"].measure(txt) / self.k + 14
                bottom = uy
            if n < len(items) - 1:
                cv.create_line(*self.pts([(30, bottom + 12), (W - 14, bottom + 12)]), fill=BORDER)
            y = bottom + 26
        h = bottom + 16
        self.rrect(cv, 0.5, 0.5, W - 0.5, h - 0.5, 12, fill=CARD_BG, outline=BORDER, tags="bg")
        cv.tag_lower("bg")
        w, hp = int(self.p(W)), int(self.p(h))
        cv.configure(width=w, height=hp)
        return w, hp

    def refresh(self):
        try:
            self.view = overview()
            self.offline = False
        except Exception:
            self.offline = True  # never let a half-written file crash the window
        self.draw_info()
        self.root.after(1000, self.refresh)

    def run(self):
        self.root.after(0, self.refresh)
        self.root.after(0, self.tick)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.root.destroy()


def _single_instance():
    """Hold a lock for as long as this pet is open; None if another pet already holds it.

    An OS file lock rather than a pid file: it is released even when the pet is killed."""
    try:
        os.makedirs(registry.home(), exist_ok=True)
        f = open(os.path.join(registry.home(), "pet.lock"), "a+")
    except OSError:
        return True  # can't tell; open anyway
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def run_pet() -> int:
    ok, why = check_available()
    if not ok:
        print(why)
        return 1
    hold = _single_instance()
    if hold is None:
        print("The tagteam pet is already open.")
        return 0
    try:
        pet = Pet()
    except Exception as e:  # e.g. TclError when no display can be opened
        print(f"The pet window could not open ({e}). tagteam keeps working without it.")
        return 1
    pet.run()
    return 0
