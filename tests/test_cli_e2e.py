"""End to end through the real CLI as a subprocess, with stand-in claude/docker on PATH."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .fakes import TEST_COMMAND

FAKE = Path(__file__).parent / "fakebin" / "fake.py"


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for n in ["claude", "docker", "node", "rea"]:
        (bindir / n).symlink_to(FAKE)
    root = tmp_path / "ws"
    e = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
             FAKE_HOME=str(tmp_path), FAKE_LAB=str(root / "lab"))

    def cli(*args, check=True, **kw):
        r = subprocess.run([sys.executable, "-m", "rea_team", *args], cwd=kw.pop("cwd", root),
                           env=kw.pop("env", e), capture_output=True, text=True, timeout=300)
        if check and r.returncode != 0:
            raise AssertionError(f"exit {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    root.mkdir()
    cli("init", str(root), cwd=tmp_path)
    (root / "lab/target/app").mkdir(parents=True)
    (root / "lab/target/app/main.py").write_text("legacy\n")
    toml = root / "rea-team.toml"
    toml.write_text(toml.read_text()
                    .replace('test_command = "npm test --prefix src && npx playwright test tests/acceptance"',
                             f"test_command = '{TEST_COMMAND}'")
                    .replace("retry_backoff_seconds = 15", "retry_backoff_seconds = 0"))
    return root, cli, e


def test_full_workflow(env):
    root, cli, _ = env
    # before planning, doctor flags the REA tools placeholder? Not when no feature uses rea:
    r = cli("doctor", check=False)
    assert "clean-room wall" in r.stdout

    # run before plan: clear error, no traceback
    r = cli("run", "--authorized", check=False)
    assert r.returncode == 10 and "no [[feature]]" in r.stderr

    # plan without authorization is refused (stdin is not a tty)
    r = cli("plan", "target/app", "--mode", "source", check=False)
    assert r.returncode == 12

    r = cli("plan", "target/app", "--mode", "source", "--authorized", "--accept", "--force")
    assert "create-site" in r.stdout and "Saved as features.toml" in r.stdout

    r = cli("doctor")
    assert "2 features, dependency order OK" in r.stdout

    r = cli("run")
    assert "✓ create-site" in r.stdout and "✓ add-page" in r.stdout and "2 passed" in r.stdout

    r = cli("status", "add-page")
    assert "passed" in r.stdout and "tag rea/add-page" in r.stdout

    # reset one feature and run just it again
    cli("reset", "add-page")
    r = cli("run", "--feature", "add-page")
    assert "2 passed" in r.stdout

    state = json.loads((root / "lab/.rea-team/project.json").read_text())
    assert state["authorized_at"] and state["features"]["add-page"]["commit"]
    assert (root / "lab/.rea-team/logs/add-page").is_dir()


def test_sigterm_mid_run_then_continue(env):
    root, cli, e = env
    cli("plan", "target/app", "--mode", "source", "--authorized", "--accept", "--force")
    slow = dict(e, FAKE_SLOW="20")
    p = subprocess.Popen([sys.executable, "-m", "rea_team", "run"], cwd=root, env=slow,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(3)
    p.send_signal(signal.SIGTERM)
    out, err = p.communicate(timeout=60)
    assert p.returncode == 130, err
    assert "continue where it stopped" in err
    st = json.loads((root / "lab/.rea-team/project.json").read_text())
    assert st["features"]["create-site"]["status"] == "interrupted"

    # a second run while the first holds the lock is refused
    holder = subprocess.Popen([sys.executable, "-m", "rea_team", "run"], cwd=root, env=slow,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(2)
    r = cli("run", check=False)
    assert r.returncode == 13 and "locked" in r.stderr
    holder.send_signal(signal.SIGTERM)
    holder.communicate(timeout=60)

    r = cli("run")
    assert "2 passed" in r.stdout


def test_failure_message_and_exit_code(env):
    root, cli, _ = env
    cli("plan", "target/app", "--mode", "source", "--authorized", "--accept", "--force")
    (root / "lab/target/app").rename(root / "lab/target/gone")
    r = cli("run", check=False)
    assert r.returncode == 11  # preflight
    assert "target target/app" in r.stdout and "not found" in r.stdout
