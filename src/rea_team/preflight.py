"""Checks that must pass before any agent runs."""

from __future__ import annotations

import json
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import features
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


def _agent_checks(cfg: Config, needs_rea: bool = True) -> list[Check]:
    lab_agents = cfg.lab_dir / ".claude" / "agents"
    clean_agents = cfg.clean_dir / ".claude" / "agents"
    out: list[Check] = []

    analyst = agent_tools(lab_agents / "analyst.md")
    if analyst is None:
        out.append(Check("analyst agent", False, f"missing {lab_agents / 'analyst.md'}"))
    elif any(PLACEHOLDER in t or "<" in t for t in analyst):
        if needs_rea:
            out.append(Check("analyst agent", False,
                             "tools line still has a placeholder; run `rea-team set-tools`"))
        else:
            out.append(Check("analyst agent", True, "no REA needed; the unused placeholder is removed at run time"))
    elif needs_rea and not any(t.startswith(REA_PREFIX) for t in analyst):
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

    for name in ("test-writer", "rebuilder"):
        tools = agent_tools(clean_agents / f"{name}.md")
        if tools is None:
            out.append(Check(f"{name} agent", False, f"missing {clean_agents / (name + '.md')}"))
        else:
            bad = [t for t in tools if t.startswith(REA_PREFIX)]
            out.append(Check(f"{name} agent", not bad, f"REA tools in clean room: {bad}" if bad else ""))
    return out


def run_checks(cfg: Config, runner: Runner, *, targets: list[str] | None = None,
               check_features: bool = False, include_container: bool = True,
               needs_rea: bool | None = None) -> list[Check]:
    checks: list[Check] = []
    forced = needs_rea
    needs_rea = True if needs_rea is None else needs_rea

    if check_features:
        try:
            feats = features.load(cfg.features_file, default_mode=cfg.default_mode)
            if forced is None:
                needs_rea = any(f.mode == "rea" for f in feats)
            checks.append(Check("features.toml", True, f"{len(feats)} features, dependency order OK"))
            targets = sorted({*(targets or []), *(f.target for f in feats)})
            if any(f.mode == "decompiled" for f in feats) and not cfg.decompile_command:
                checks.append(Check("decompiler", False, "a feature uses mode=decompiled but "
                                                         "analysis.decompile_command is empty"))
        except ReaTeamError as e:
            checks.append(Check("features.toml", False, str(e)))
        has_contracts = (cfg.lab_dir / "handoff" / "CONTRACTS.md").is_file()
        checks.append(Check("CONTRACTS.md", has_contracts, "" if has_contracts else "run `rea-team plan` first"))

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

    tool("claude", [cfg.claude_command, "--version"])
    if needs_rea:
        tool("node", ["node", "-v"], node_ok)
        tool("rea", ["rea", "--version"])
    tool("docker", ["docker", "--version"])

    checks += _agent_checks(cfg, needs_rea)

    for target in targets or []:
        t = (cfg.lab_dir / target).resolve()
        if not t.is_relative_to(cfg.lab_dir):
            checks.append(Check(f"target {target}", False, "targets must live inside lab/"))
        else:
            checks.append(Check(f"target {target}", t.exists(), "" if t.exists() else f"not found: {t}"))

    if include_container:
        room = CleanRoom(cfg, runner)
        try:
            room.ensure_running()
            checks.append(Check("clean container", True, cfg.container_name))
            claude_ok = room.check_tool(cfg.container_claude_command, "--version")
            checks.append(Check("claude in container", claude_ok,
                                "" if claude_ok else "install Claude Code in the image and sign it in"))
            git_ok = room.check_tool("git", "--version")
            checks.append(Check("git in container", git_ok, "" if git_ok else "install git in the image"))
            breaches = room.check_wall()
            checks.append(Check("clean-room wall", not breaches, "; ".join(breaches) or "rea, npm and lab unreachable"))
        except ReaTeamError as e:
            checks.append(Check("clean container", False, f"{e} {e.hint}".strip()))
    return checks


def list_mcp_tools(command: str = "rea --mcp", timeout: float = 60) -> list[str]:
    """Ask an MCP server over stdio for its tool names (initialize, then tools/list until no cursor)."""
    try:
        proc = subprocess.Popen(shlex.split(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    except OSError as e:
        raise ReaTeamError(f"Cannot start MCP server `{command}`: {e}",
                           hint="Install REA, or pass --from-mcp with the right command, or --tools.") from e
    lines: queue.Queue = queue.Queue()

    def pump() -> None:  # a thread so a silent server cannot block us past the deadline
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def reply(msg_id: int) -> dict:
        while True:
            try:
                line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise ReaTeamError(f"MCP server `{command}` did not answer within {timeout:.0f}s") from None
            if line is None:
                raise ReaTeamError(f"MCP server `{command}` exited before listing its tools")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # stray log output on stdout
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise ReaTeamError(f"MCP server `{command}` returned an error: {msg['error']}")
                return msg.get("result") or {}

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "rea-team", "version": "0"}}})
        reply(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        names: list[str] = []
        cursor, msg_id = None, 2
        while True:
            send({"jsonrpc": "2.0", "id": msg_id, "method": "tools/list",
                  "params": {"cursor": cursor} if cursor else {}})
            result = reply(msg_id)
            names += [t["name"] for t in result.get("tools", []) if isinstance(t.get("name"), str)]
            cursor, msg_id = result.get("nextCursor"), msg_id + 1
            if not cursor:
                break
    except (BrokenPipeError, OSError) as e:
        raise ReaTeamError(f"MCP server `{command}` closed its input: {e}") from e
    finally:
        proc.kill()
        proc.wait()
    return [n if n.startswith(REA_PREFIX) else REA_PREFIX + n for n in names]


def extract_rea_tools(doctor_json: Path) -> list[str]:
    """Best-effort pull of tool names from `rea doctor --json` output.

    Recent REA versions (6.x) only report a tool count there; prefer list_mcp_tools().
    """
    try:
        text = doctor_json.read_text()
    except OSError as e:
        raise ReaTeamError(f"Cannot read {doctor_json}: {e}", hint="Run `rea-team set-tools` with no options.") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        hint = "Run `rea-team set-tools` with no options to ask `rea --mcp` directly."
        if "Extra data" in str(e):
            hint = "The file holds more than one JSON document (written with `>>`?). " + hint
        raise ReaTeamError(f"Cannot parse {doctor_json}: {e}", hint=hint) from e
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


def strip_rea_placeholder(cfg: Config) -> bool:
    """For projects that never use REA: drop the unfilled placeholder from the analyst's tools."""
    path = cfg.lab_dir / ".claude" / "agents" / "analyst.md"
    if not path.is_file():
        return False
    text = path.read_text()
    if PLACEHOLDER not in text:
        return False
    path.write_text(text.replace(", " + PLACEHOLDER, "").replace(PLACEHOLDER, ""))
    return True


def project_needs_rea(cfg: Config) -> bool:
    try:
        return any(f.mode == "rea" for f in features.load(cfg.features_file, default_mode=cfg.default_mode))
    except ReaTeamError:
        return cfg.default_mode == "rea"
