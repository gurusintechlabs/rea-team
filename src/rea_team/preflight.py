"""Checks that must pass before any agent runs."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .container import CleanRoom
from .errors import ReaTeamError
from .runner import Runner

REA_PREFIX = "mcp__rea__"
PLACEHOLDER = "{{REA_TOOLS}}"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def agent_tools(path: Path) -> list[str] | None:
    """Return the `tools:` list from an agent file's frontmatter."""
    if not path.is_file():
        return None
    text = path.read_text()
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return []
    for line in m.group(1).splitlines():
        if line.strip().startswith("tools:"):
            return [t.strip() for t in line.split(":", 1)[1].split(",") if t.strip()]
    return []


def node_ok(version: str) -> bool:
    m = re.match(r"v?(\d+)\.(\d+)", version.strip())
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2))
    return (major == 22 and minor >= 19) or (major == 24 and minor >= 11) or major >= 26


def _agent_checks(cfg: Config) -> list[Check]:
    lab_agents = cfg.lab_dir / ".claude" / "agents"
    clean_agents = cfg.clean_dir / ".claude" / "agents"
    out: list[Check] = []

    analyst = agent_tools(lab_agents / "analyst.md")
    if analyst is None:
        out.append(Check("analyst agent", False, f"missing {lab_agents / 'analyst.md'}"))
    elif any(PLACEHOLDER in t or "<" in t for t in analyst):
        out.append(Check("analyst agent", False,
                         "tools line still has a placeholder; run `rea-team set-tools` after `rea doctor --json`"))
    elif not any(t.startswith(REA_PREFIX) for t in analyst):
        out.append(Check("analyst agent", False, f"no {REA_PREFIX}* tools; the analyst cannot read the target"))
    else:
        out.append(Check("analyst agent", True, f"{sum(t.startswith(REA_PREFIX) for t in analyst)} REA tools"))

    for name, forbidden in [("spec-writer", ("Bash", REA_PREFIX)), ("verifier", (REA_PREFIX,))]:
        tools = agent_tools(lab_agents / f"{name}.md")
        if tools is None:
            out.append(Check(f"{name} agent", False, "missing"))
        else:
            bad = [t for t in tools if any(t == f or t.startswith(f) for f in forbidden)]
            out.append(Check(f"{name} agent", not bad, f"must not have: {', '.join(bad)}" if bad else ""))

    rb = agent_tools(clean_agents / "rebuilder.md")
    if rb is None:
        out.append(Check("rebuilder agent", False, f"missing {clean_agents / 'rebuilder.md'}"))
    else:
        bad = [t for t in rb if t.startswith(REA_PREFIX)]
        out.append(Check("rebuilder agent", not bad, f"REA tools in clean room: {bad}" if bad else ""))
    return out


def run_checks(cfg: Config, runner: Runner, *, target: str | None = None,
               include_container: bool = True) -> list[Check]:
    checks: list[Check] = []

    def tool(name: str, cmd: list[str], validate=None):
        if not shutil.which(cmd[0]):
            checks.append(Check(name, False, f"`{cmd[0]}` not on PATH"))
            return
        try:
            r = runner.run(cmd, check=False, timeout=60, log_name="preflight")
        except ReaTeamError as e:
            checks.append(Check(name, False, str(e)))
            return
        out = (r.stdout or r.stderr).strip().splitlines()
        first = out[0] if out else ""
        if r.returncode != 0:
            checks.append(Check(name, False, first or f"exit {r.returncode}"))
        elif validate and not validate(first):
            checks.append(Check(name, False, f"unsupported version {first}"))
        else:
            checks.append(Check(name, True, first))

    tool("node", ["node", "-v"], node_ok)
    tool("claude", [cfg.claude_command, "--version"])
    tool("rea", ["rea", "--version"])
    tool("docker", ["docker", "--version"])

    checks += _agent_checks(cfg)

    if target is not None:
        t = (cfg.lab_dir / target).resolve()
        if not t.is_relative_to(cfg.lab_dir):
            checks.append(Check("target", False, "target must live inside lab/"))
        else:
            checks.append(Check("target", t.exists(), str(t) if t.exists() else f"not found: {t}"))

    if include_container:
        room = CleanRoom(cfg, runner)
        try:
            room.ensure_running()
            checks.append(Check("clean container", True, cfg.container_name))
            room.check_claude()
            checks.append(Check("claude in container", True))
            breaches = room.check_wall()
            checks.append(Check("clean-room wall", not breaches, "; ".join(breaches) or "rea, npm and lab unreachable"))
        except ReaTeamError as e:
            checks.append(Check("clean container", False, f"{e} {e.hint}".strip()))
    return checks


def extract_rea_tools(doctor_json: Path) -> list[str]:
    """Best-effort pull of tool names from `rea doctor --json` output."""
    try:
        data = json.loads(doctor_json.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ReaTeamError(f"Cannot read {doctor_json}: {e}",
                           hint="Run `rea doctor --json > lab/analysis/doctor.json`, or pass --tools.") from e
    names: set[str] = set()

    def walk(node, in_tools=False):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, in_tools or "tool" in k.lower())
            if in_tools and isinstance(node.get("name"), str):
                names.add(node["name"])
        elif isinstance(node, list):
            for item in node:
                if in_tools and isinstance(item, str) and re.fullmatch(r"[\w.-]+", item):
                    names.add(item)
                else:
                    walk(item, in_tools)

    walk(data)
    return sorted(n if n.startswith(REA_PREFIX) else REA_PREFIX + n for n in names)


def set_analyst_tools(cfg: Config, tools: list[str]) -> Path:
    path = cfg.lab_dir / ".claude" / "agents" / "analyst.md"
    text = path.read_text()
    tools = [t if t.startswith(REA_PREFIX) else REA_PREFIX + t for t in tools]
    new_line = "tools: Read, Grep, Glob, Write, " + ", ".join(tools)
    text, n = re.subn(r"^tools:.*$", new_line, text, count=1, flags=re.MULTILINE)
    if n != 1:
        raise ReaTeamError(f"No tools: line in {path}")
    path.write_text(text)
    return path
