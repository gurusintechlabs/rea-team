"""rea-team.toml loading and validation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

CONFIG_NAME = "rea-team.toml"

DEFAULT_CONFIG = """\
# rea-team configuration. Paths are relative to this file.

lab_dir = "lab"
clean_dir = "clean"

[run]
max_rounds = 3                 # stop after this many rebuild/verify rounds
retries = 2                    # extra attempts for a failing step
retry_backoff_seconds = 15     # doubles on each retry
check_target_unchanged = true  # hash target/ before and after the analyst

[timeouts]                     # seconds per step attempt
analyst = 3600
spec_writer = 1200
rebuilder = 3600
verifier = 3600
docker = 120

[claude]
command = "claude"
# Extra flags for every `claude -p` call. Headless runs need a permission
# mode that lets agents write files, or they stop waiting for approval.
args = ["--permission-mode", "acceptEdits"]

[container]
name = "rea-clean"
workdir = "/work"
claude_command = "claude"
args = ["--permission-mode", "acceptEdits"]
# Paths that must NOT exist inside the clean container.
forbidden_paths = ["/lab", "/target", "/analysis"]

[leak_check]
# Regexes that must not appear in SPEC.md (case-insensitive).
patterns = [
  '0x[0-9a-f]{5,}',
  'sub_[0-9a-f]+',
  'var_[0-9a-f]+',
  'FUN_[0-9a-f]+',
  'DAT_[0-9a-f]+',
  'LAB_[0-9a-f]+',
]
"""


@dataclass
class Config:
    root: Path
    lab_dir: Path
    clean_dir: Path
    max_rounds: int = 3
    retries: int = 2
    retry_backoff_seconds: float = 15
    check_target_unchanged: bool = True
    timeouts: dict[str, int] = field(default_factory=dict)
    claude_command: str = "claude"
    claude_args: list[str] = field(default_factory=list)
    container_name: str = "rea-clean"
    container_workdir: str = "/work"
    container_claude_command: str = "claude"
    container_claude_args: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    leak_patterns: list[str] = field(default_factory=list)

    @property
    def state_dir(self) -> Path:
        return self.lab_dir / ".rea-team"

    def timeout(self, step: str) -> int:
        return int(self.timeouts.get(step, 3600))


def _get(d: dict, key: str, typ, default):
    val = d.get(key, default)
    wants_bool = typ is bool
    if not isinstance(val, typ) or (isinstance(val, bool) and not wants_bool):
        name = getattr(typ, "__name__", "a number")
        raise ConfigError(f"`{key}` must be {name}, got {val!r}")
    return val


def find_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / CONFIG_NAME).is_file():
            return p
    raise ConfigError(
        f"No {CONFIG_NAME} found in {start} or any parent.",
        hint="Run `rea-team init <dir>` first, then run commands from inside it.",
    )


def load(start: Path | None = None) -> Config:
    root = find_root((start or Path.cwd()).resolve())
    path = root / CONFIG_NAME
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e

    run = raw.get("run", {})
    claude = raw.get("claude", {})
    cont = raw.get("container", {})
    leak = raw.get("leak_check", {})

    cfg = Config(
        root=root,
        lab_dir=(root / _get(raw, "lab_dir", str, "lab")).resolve(),
        clean_dir=(root / _get(raw, "clean_dir", str, "clean")).resolve(),
        max_rounds=_get(run, "max_rounds", int, 3),
        retries=_get(run, "retries", int, 2),
        retry_backoff_seconds=float(_get(run, "retry_backoff_seconds", (int, float), 15)),
        check_target_unchanged=_get(run, "check_target_unchanged", bool, True),
        timeouts={k: int(v) for k, v in raw.get("timeouts", {}).items()},
        claude_command=_get(claude, "command", str, "claude"),
        claude_args=list(_get(claude, "args", list, [])),
        container_name=_get(cont, "name", str, "rea-clean"),
        container_workdir=_get(cont, "workdir", str, "/work"),
        container_claude_command=_get(cont, "claude_command", str, "claude"),
        container_claude_args=list(_get(cont, "args", list, [])),
        forbidden_paths=list(_get(cont, "forbidden_paths", list, [])),
        leak_patterns=list(_get(leak, "patterns", list, [])),
    )

    if cfg.max_rounds < 1:
        raise ConfigError("run.max_rounds must be at least 1")
    if cfg.retries < 0:
        raise ConfigError("run.retries cannot be negative")
    if not cfg.leak_patterns:
        raise ConfigError("leak_check.patterns is empty; the leak check would pass anything")
    if cfg.lab_dir == cfg.clean_dir or cfg.clean_dir.is_relative_to(cfg.lab_dir):
        raise ConfigError("clean_dir must be separate from (and not inside) lab_dir")
    return cfg
