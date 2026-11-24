#!/usr/bin/env python3
"""Stand-in for claude / docker / node / rea on PATH, for CLI end-to-end tests.

Reuses tests/fakes.FakeWorld so behaviour matches the in-process tests.
FAKE_HOME holds the fake container's /work; FAKE_LAB is the lab folder.
FAKE_SLOW=<seconds> makes lab agents sleep (to test interrupts).
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.fakes import FakeWorld  # noqa: E402

name = Path(sys.argv[0]).name
args = sys.argv[1:]

if name == "node":
    print("v24.11.0"); sys.exit(0)
if name == "rea":
    print("rea 6.7.0"); sys.exit(0)
if args == ["--version"]:
    print(f"{name} 1.0 (fake)"); sys.exit(0)

w = FakeWorld(Path(os.environ["FAKE_HOME"]), Path(os.environ["FAKE_LAB"]))
if name == "claude":
    if os.environ.get("FAKE_SLOW") and "analyst agent" in args[1]:
        time.sleep(float(os.environ["FAKE_SLOW"]))
    w.run(["claude", "-p", args[1]])
    print("done")
elif name == "docker":
    r = w._docker(args)
    sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
    sys.exit(r.returncode)
