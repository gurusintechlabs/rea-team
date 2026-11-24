"""End to end through the real CLI and real subprocesses, with stand-in binaries."""

import json
import os
import stat
from pathlib import Path

import pytest

from rea_team.cli import main

FAKE = Path(__file__).parent / "fakebin" / "fake.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    FAKE.chmod(FAKE.stat().st_mode | stat.S_IXUSR)
    for n in ["claude", "docker", "node", "rea"]:
        (bindir / n).symlink_to(FAKE)
    home = tmp_path / "fakehome"
    home.mkdir()
    monkeypatch.setenv("FAKE_HOME", str(home))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    root = tmp_path / "ws"
    assert main(["init", str(root)]) == 0
    (root / "lab/target/Notes.app").mkdir(parents=True)
    (root / "lab/target/Notes.app/bin").write_text("ORIGINAL")
    monkeypatch.chdir(root)
    cfg = root / "rea-team.toml"
    cfg.write_text(cfg.read_text().replace("retry_backoff_seconds = 15", "retry_backoff_seconds = 0")
                                  .replace("analyst = 3600", "analyst = 2"))
    return root, home


def test_doctor_flags_placeholder_then_passes(env, capsys):
    root, _ = env
    assert main(["doctor"]) != 0
    assert "placeholder" in capsys.readouterr().out
    assert main(["set-tools", "--tools", "open_binary,xrefs"]) == 0
    assert main(["doctor", "--target", "target/Notes.app"]) == 0


def test_full_run_two_rounds(env, capsys):
    root, _ = env
    main(["set-tools", "--tools", "open_binary"])
    assert main(["run", "lab/target/Notes.app", "how search ranks results", "--authorized"]) == 0
    out = capsys.readouterr().out
    assert "PASSED" in out and "round 2: wrong=0" in out
    run_dir = next((root / "lab/.rea-team/runs").iterdir())
    st = json.loads((run_dir / "state.json").read_text())
    assert st["status"] == "passed" and st["round"] == 2
    assert (run_dir / "round-1/report.md").exists() and (run_dir / "round-1-QUESTIONS.md").exists()
    assert (run_dir / "logs/analyst.log").exists()


def test_timeout_then_resume(env, capsys):
    root, home = env
    main(["set-tools", "--tools", "open_binary"])
    (home / "hang_analyst").touch()
    code = main(["run", "target/Notes.app", "search", "--authorized"])
    assert code == 21  # StepFailed after retries
    err = capsys.readouterr().err
    assert "Timed out" in err and "rea-team resume" in err

    (home / "hang_analyst").unlink()
    assert main(["resume"]) == 0
    assert "PASSED" in capsys.readouterr().out
    assert main(["status"]) == 0
