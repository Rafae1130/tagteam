"""Helpers for reading agent output: tolerant JSON-lines parsing, limit messages, reset times."""

import datetime as _dt
import json
import re
import time
from typing import Iterator, Optional

# Phrases that mean "you are out of quota". Only checked on failed turns, and only against
# error text and stderr, never against what the agent wrote while working.
LIMIT_RE = re.compile(
    r"usage limit|rate[ -]limit|hit your (?:usage )?limit|limit (?:reached|exceeded)|quota|"
    r"too many requests|out of (?:extra )?usage|try again (?:in|at)\b|resets? (?:at |in )?\d",
    re.I,
)

_UNIT_ALT = r"days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s"
_UNITS = r"(?:" + _UNIT_ALT + r")\b"
_EPOCH_RE = re.compile(r"\|\s*(\d{10})\b")
_UNIT_RE = re.compile(r"(\d+)\s*(" + _UNIT_ALT + r")\b", re.I)
_IN_RE = re.compile(r"\b(?:in|after)\s+((?:\d+\s*" + _UNITS + r"[\s,]*(?:and\s+)?)+)", re.I)
_LEAD = r"\b(?:resets?|again|until|available)\s+(?:at\s+)?"
_TZ = r"(?:\s*\(([^)]+)\))?"
_CLOCK12_RE = re.compile(_LEAD + r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b\.?" + _TZ, re.I)
_CLOCK24_RE = re.compile(_LEAD + r"(\d{1,2}):(\d{2})(?!\s*[ap]\.?m)" + _TZ, re.I)
# Codex, when the reset is on another day: "try again at Sep 21st, 2026 5:47 PM."
_DATE12_RE = re.compile(_LEAD + r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(?:(\d{4}),?\s+)?"
                        r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b\.?" + _TZ, re.I)
_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun",
                                           "jul", "aug", "sep", "oct", "nov", "dec"))}


def iter_json_objects(line: str) -> Iterator[dict]:
    """Yield the JSON objects in one output line.

    A clean line is one object. Some CLI versions have written an event into the middle of
    another line, so on a parse failure every '{"type"' start is tried as well.
    """
    text = line.strip()
    if not text:
        return
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            yield obj
        return
    except ValueError:
        pass
    dec = json.JSONDecoder()
    i = 0
    while True:
        j = text.find('{"type"', i)
        if j < 0:
            return
        try:
            obj, end = dec.raw_decode(text, j)
        except ValueError:
            i = j + 1
            continue
        if isinstance(obj, dict):
            yield obj
        i = end


def parse_reset_text(text: str, now: float) -> Optional[float]:
    """Find a reset time in a limit message. Returns epoch seconds or None."""
    if not text:
        return None
    m = _EPOCH_RE.search(text)
    if m and int(m.group(1)) > now - 86400:
        return float(m.group(1))
    m = _IN_RE.search(text)
    if m:
        total = 0
        for num, unit in _UNIT_RE.findall(m.group(1)):
            u = unit.lower()
            mult = 86400 if u.startswith("d") else 3600 if u.startswith("h") else 60 if u.startswith("m") else 1
            total += int(num) * mult
        if total > 0:
            return now + total
    m = _DATE12_RE.search(text)
    if m and m.group(1)[:3].lower() in _MONTHS:
        mon, day = _MONTHS[m.group(1)[:3].lower()], int(m.group(2))
        hour = int(m.group(4)) % 12 + (12 if m.group(6).lower() == "p" else 0)
        base = _dt.datetime.fromtimestamp(now)
        year = int(m.group(3)) if m.group(3) else base.year
        try:
            cand = _dt.datetime(year, mon, day, hour, int(m.group(5) or 0))
            if not m.group(3) and cand.timestamp() < now - 86400:
                cand = cand.replace(year=year + 1)
            return cand.timestamp()
        except ValueError:
            pass
    m = _CLOCK12_RE.search(text)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
        return _next_clock(hour, int(m.group(2) or 0), m.group(4), now)
    m = _CLOCK24_RE.search(text)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return _next_clock(int(m.group(1)), int(m.group(2)), m.group(3), now)
    return None


def _next_clock(hour: int, minute: int, tzname: Optional[str], now: float) -> Optional[float]:
    if hour > 23 or minute > 59:
        return None
    tz = None
    if tzname:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(tzname.strip())
        except Exception:
            tz = None
    base = _dt.datetime.fromtimestamp(now, tz) if tz else _dt.datetime.fromtimestamp(now)
    cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand.timestamp() <= now:
        cand += _dt.timedelta(days=1)
    return cand.timestamp()


def fmt_time(ts: Optional[float]) -> str:
    if not ts:
        return "unknown"
    d = _dt.datetime.fromtimestamp(ts)
    if abs(ts - time.time()) < 20 * 3600:
        return d.strftime("%H:%M")
    return d.strftime("%a %d %b %H:%M")


def fmt_dur(seconds: float) -> str:
    s = max(0, int(seconds))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"
