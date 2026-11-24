"""rea-team.toml loading and validation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

CONFIG_NAME = "rea-team.toml"

DEFAULT_CONFIG = """\
# rea-team configuration. Paths are relative to this file.
# Full reference: docs/USER_GUIDE.md, section "rea-team.toml reference".

lab_dir = "lab"
clean_dir = "clean"

[project]
name = "my-legacy-app"
# What the rebuild is written in. The test-writer and rebuilder both read this,
# so tests and code agree on tools. Be specific.
stack = "TypeScript on Node 22. Unit tests with Vitest. Functional tests with Playwright."
# Runs inside the clean container from /work. Must run unit tests AND every
# feature's acceptance tests in tests/acceptance/. Exit 0 = everything passes.
test_command = "npm test --prefix src && npx playwright test tests/acceptance"
max_spec_chars = 60000       # a feature spec bigger than this should be split
fix_attempts = 3             # rebuilder fix sessions when the test gate fails
keep_going = false           # on a failed feature: false = stop, true = skip its dependents and continue

[analysis]
default_mode = "rea"         # rea | source | decompiled (overridable per feature)
# For mode = "decompiled": run once per target before the analyst. {input} and
# {output} are replaced. Example for Jira plugin JARs with Vineflower:
# decompile_command = "java -jar /opt/vineflower.jar {input} {output}"
decompile_command = ""

[verify]
# Plain-language instructions the verifier gets for running each side.
original = "The original is installed under lab/target/. Describe how to start it and where it listens."
rebuild = "The rebuild is in lab/rebuild/src. Install with `npm ci`, start with `npm start`, it listens on http://localhost:3000."
# Acceptance tests read the app location from APP_URL (web) or APP_CMD (desktop/CLI),
# so the verifier can run the same tests against the original and the rebuild.

[run]
max_rounds = 3                 # analyse/spec/build/verify rounds per feature
retries = 2                    # extra attempts for a failing step
retry_backoff_seconds = 15     # doubles on each retry
check_target_unchanged = true  # hash the target before and after the analyst

[timeouts]                     # seconds per step attempt
analyst = 3600
spec_writer = 1200
test_writer = 1800
rebuilder = 3600
gate = 1800
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
# Regexes that must not appear in anything sent to the clean room (case-insensitive).
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
    project_name: str = "project"
    stack: str = ""
    test_command: str = ""
    max_spec_chars: int = 60000
    fix_attempts: int = 3
    keep_going: bool = False
    default_mode: str = "rea"
    decompile_command: str = ""
    verify_original: str = ""
    verify_rebuild: str = ""
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

    @property
    def features_file(self) -> Path:
        return self.root / "features.toml"

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

    proj = raw.get("project", {})
    ana = raw.get("analysis", {})
    ver = raw.get("verify", {})
    run = raw.get("run", {})
    claude = raw.get("claude", {})
    cont = raw.get("container", {})
    leak = raw.get("leak_check", {})

    cfg = Config(
        root=root,
        lab_dir=(root / _get(raw, "lab_dir", str, "lab")).resolve(),
        clean_dir=(root / _get(raw, "clean_dir", str, "clean")).resolve(),
        project_name=_get(proj, "name", str, "project"),
        stack=_get(proj, "stack", str, ""),
        test_command=_get(proj, "test_command", str, ""),
        max_spec_chars=_get(proj, "max_spec_chars", int, 60000),
        fix_attempts=_get(proj, "fix_attempts", int, 3),
        keep_going=_get(proj, "keep_going", bool, False),
        default_mode=_get(ana, "default_mode", str, "rea"),
        decompile_command=_get(ana, "decompile_command", str, ""),
        verify_original=_get(ver, "original", str, ""),
        verify_rebuild=_get(ver, "rebuild", str, ""),
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
    if cfg.retries < 0 or cfg.fix_attempts < 0:
        raise ConfigError("run.retries and project.fix_attempts cannot be negative")
    if not cfg.leak_patterns:
        raise ConfigError("leak_check.patterns is empty; the leak check would pass anything")
    if not cfg.test_command.strip():
        raise ConfigError("project.test_command is empty; there would be no regression gate")
    if cfg.default_mode not in ("rea", "source", "decompiled"):
        raise ConfigError("analysis.default_mode must be rea, source or decompiled")
    if cfg.lab_dir == cfg.clean_dir or cfg.clean_dir.is_relative_to(cfg.lab_dir) \
            or cfg.lab_dir.is_relative_to(cfg.clean_dir):
        raise ConfigError("clean_dir and lab_dir must be separate folders, neither inside the other")
    return cfg
