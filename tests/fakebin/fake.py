#!/usr/bin/env python3
"""Stand-in for claude / docker / node / rea, chosen by argv[0] name.

State lives in $FAKE_HOME so it survives between processes.
"""
import os
import shutil
import sys
import time
from pathlib import Path

HOME = Path(os.environ["FAKE_HOME"])
CFS = HOME / "container"
name = Path(sys.argv[0]).name
args = sys.argv[1:]

SPEC = """## Architecture\nx\n## Data Model\nx\n## Public API\nx\n## File Formats\nx\n## Errors\nx
## Test Cases\n- a\n## Open questions\n- b\n"""


def cpath(p):
    return CFS / p.lstrip("/")


if name == "node":
    print("v24.11.0")
elif name == "rea":
    print("rea 6.7.0")
elif name == "claude":
    if args == ["--version"]:
        print("2.0.0 (Claude Code)")
        sys.exit(0)
    prompt = args[1]
    if "analyst" in prompt:
        if (HOME / "hang_analyst").exists():
            time.sleep(30)
        Path("analysis").mkdir(exist_ok=True)
        Path("analysis/evidence.md").write_text("observation a\nunknown b\n")
    elif "add every wrong" in prompt:
        Path("handoff/SPEC.md").write_text(Path("handoff/SPEC.md").read_text() + "\n- extra\n")
    elif "spec-writer" in prompt:
        Path("handoff").mkdir(exist_ok=True)
        Path("handoff/SPEC.md").write_text(SPEC)
    elif "verifier" in prompt:
        n = int((HOME / "verify_count").read_text()) if (HOME / "verify_count").exists() else 0
        (HOME / "verify_count").write_text(str(n + 1))
        wrong = 1 if n == 0 else 0
        Path("report.md").write_text(f"ordinary 5 / 5 100%\nRESULT wrong={wrong} crash=0\n")
    print("ok")
elif name == "docker":
    if args[0] == "--version":
        print("Docker version 27.0.0")
    elif args[0] == "inspect":
        print("true")
    elif args[0] == "cp":
        src, dst = args[1], args[2]
        if src.startswith("rea-clean:"):
            s = cpath(src.split(":", 1)[1])
            shutil.copytree(s, dst) if s.is_dir() else shutil.copy2(s, dst)
        else:
            d = cpath(dst.split(":", 1)[1])
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, d)
    elif args[0] == "exec":
        rest = args[args.index("rea-clean") + 1:]
        if rest[0] == "claude":
            if rest[1] == "--version":
                print("2.0.0")
            else:
                (cpath("/work/src")).mkdir(parents=True, exist_ok=True)
                (cpath("/work/src/main.py")).write_text("print('rebuilt')\n")
                (cpath("/work/QUESTIONS.md")).write_text("- default weights?\n")
        elif rest[0] in ("sh", "npx", "npm"):
            sys.exit(1)
        elif rest[0] == "test":
            sys.exit(0 if cpath(rest[2]).exists() and not rest[2].startswith(("/lab", "/target", "/analysis")) else 1)
        elif rest[0] == "rm":
            cpath(rest[-1]).unlink(missing_ok=True)
else:
    sys.exit(f"unknown fake {name}")
