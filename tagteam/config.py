"""Configuration: defaults and tagteam.json loading.

JSON only, on purpose: it is in the standard library of every supported Python version,
so there is nothing to install and no version-dependent parser.
"""

import copy
import json
import os
from typing import Optional

CONFIG_NAME = "tagteam.json"


class ConfigError(Exception):
    pass


DEFAULTS = {
    "main": "claude",
    "secondary": "codex",
    "wait_for_reset": True,
    "keep_awake": True,
    "reset_buffer_seconds": 120,
    "unknown_reset_retry_minutes": 30,
    "task_timeout_minutes": 60,
    "max_iterations": 200,
    "max_consecutive_failures": 3,
    "tasks_file": "TASKS.md",
    "handoff_file": "HANDOFF.md",
    "git_commit": True,
    "prompt_file": "",
    "notify_command": [],
    "agents": {
        "claude": {"type": "claude", "command": ["claude"], "args": ["--permission-mode", "acceptEdits"]},
        "codex": {"type": "codex", "command": ["codex"], "args": ["--sandbox", "workspace-write"]},
    },
}

KNOWN_KEYS = set(DEFAULTS)


def find_config(root: Optional[str]) -> Optional[str]:
    if not root:
        return None
    p = os.path.join(root, CONFIG_NAME)
    return p if os.path.isfile(p) else None


def build_config(root: Optional[str], overrides: Optional[dict] = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = find_config(root)
    if path:
        try:
            with open(path, encoding="utf-8-sig") as f:
                user = json.load(f)
        except (OSError, ValueError) as e:
            raise ConfigError(f"cannot read {path}: {e}")
        if not isinstance(user, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        unknown = sorted(k for k in user if k not in KNOWN_KEYS and not k.startswith("_"))
        if unknown:
            raise ConfigError(f"unknown key(s) in {path}: {', '.join(unknown)}")
        agents = user.pop("agents", {}) or {}
        cfg.update(user)
        for name, a in agents.items():
            merged = dict(cfg["agents"].get(name, {}))
            merged.update(a)
            cfg["agents"][name] = merged
    for k, v in (overrides or {}).items():
        if v is not None:
            cfg[k] = v
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    if not cfg.get("main"):
        raise ConfigError("no main agent configured")
    for role in ("main", "secondary"):
        n = cfg.get(role)
        if n and n not in cfg["agents"]:
            raise ConfigError(f"{role} agent '{n}' has no entry under \"agents\" in the config")
    for n, a in cfg["agents"].items():
        a.setdefault("type", n if n in ("claude", "codex") else "custom")


def write_default_config(root: str, main: str, secondary: str) -> str:
    data = {
        "main": main,
        "secondary": secondary,
        "wait_for_reset": DEFAULTS["wait_for_reset"],
        "keep_awake": DEFAULTS["keep_awake"],
        "task_timeout_minutes": DEFAULTS["task_timeout_minutes"],
        "max_iterations": DEFAULTS["max_iterations"],
        "agents": copy.deepcopy(DEFAULTS["agents"]),
    }
    path = os.path.join(root, CONFIG_NAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return path
