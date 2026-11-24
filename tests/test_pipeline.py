from pathlib import Path

import pytest

from rea_team import checks, config
from rea_team.cli import main
from rea_team.errors import (ConfigError, IntegrityError, LeakDetected, ParityNotReached,
                             ReportFormatError, StepFailed, TransientError, WallBreach)
from rea_team.pipeline import Pipeline
from rea_team.preflight import agent_tools, extract_rea_tools, node_ok
from rea_team.runner import with_retries
from rea_team.state import RunState

from .fakes import GOOD_SPEC, FakeWorld


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    assert main(["init", str(root)]) == 0
    (root / "lab/target/app.bin").write_text("ORIGINAL")
    monkeypatch.chdir(root)
    cfg = config.load()
    cfg.retry_backoff_seconds = 0
    return cfg


def new_state():
    return RunState(run_id=RunState.new_id(), target="target/app.bin",
                    feature="how search ranks results", authorized_at="now")


def make(cfg, world, state=None):
    return Pipeline(cfg, state or new_state(), runner=world, sleep=lambda s: None)


# --- pure checks -------------------------------------------------------------------

def test_leak_scan_finds_addresses_and_ghidra_names():
    pats = ["0x[0-9a-f]{5,}", "sub_[0-9a-f]+", "FUN_[0-9a-f]+"]
    hits = checks.leak_scan("ok line\ncalls FUN_00401000\nat 0x100012ab0\nport 0x1f\n", pats)
    assert [n for n, _ in hits] == [2, 3]


def test_report_parse_and_errors():
    r = checks.parse_report("edge 36 / 40 90%\n\nRESULT wrong=4 crash=0\n")
    assert (r.wrong, r.crash, r.passed) == (4, 0, False)
    assert r.groups["edge"]["total"] == 40
    with pytest.raises(ReportFormatError):
        checks.parse_report("total 1/1 100%\n")
    with pytest.raises(ReportFormatError):
        checks.parse_report("")


def test_spec_sections():
    assert checks.missing_spec_sections(GOOD_SPEC) == []
    assert "Test Cases" in checks.missing_spec_sections("## Architecture\n")


def test_retries_then_gives_up():
    n = {"calls": 0}

    def flaky():
        n["calls"] += 1
        raise TransientError("boom")

    with pytest.raises(StepFailed):
        with_retries(flaky, what="x", retries=2, backoff=0, sleep=lambda s: None)
    assert n["calls"] == 3


def test_retries_do_not_swallow_safety_errors():
    def leak():
        raise LeakDetected("leak")
    with pytest.raises(LeakDetected):
        with_retries(leak, what="x", retries=5, backoff=0, sleep=lambda s: None)


def test_node_versions():
    assert node_ok("v22.19.0") and node_ok("v24.11.1") and node_ok("v26.0.0")
    assert not node_ok("v22.18.0") and not node_ok("v20.10.0") and not node_ok("v25.1.0")


def test_set_tools_from_doctor(ws, tmp_path):
    doc = tmp_path / "doctor.json"
    doc.write_text('{"mcp": {"tools": [{"name": "open_binary"}, {"name": "xrefs"}]}}')
    tools = extract_rea_tools(doc)
    assert tools == ["mcp__rea__open_binary", "mcp__rea__xrefs"]
    assert main(["set-tools", "--from-doctor", str(doc)]) == 0
    assert "mcp__rea__xrefs" in agent_tools(ws.lab_dir / ".claude/agents/analyst.md")


def test_config_rejects_clean_inside_lab(ws):
    p = ws.root / "rea-team.toml"
    p.write_text(p.read_text().replace('clean_dir = "clean"', 'clean_dir = "lab/clean"'))
    with pytest.raises(ConfigError):
        config.load()


# --- pipeline ---------------------------------------------------------------------

def test_happy_path(ws, tmp_path):
    w = FakeWorld(tmp_path)
    st = make(ws, w).run()
    assert st.status == "passed"
    assert w.calls == ["analyst", "spec-writer", "rebuilder", "verifier"]
    assert (ws.lab_dir / "rebuild/main.py").exists()


def test_converges_over_rounds(ws, tmp_path):
    w = FakeWorld(tmp_path, verify_results=[(3, 1), (1, 0), (0, 0)])
    st = make(ws, w).run()
    assert st.status == "passed" and st.round == 3
    assert w.calls.count("spec-update") == 2
    assert "new case" in (ws.lab_dir / "handoff/SPEC.md").read_text()


def test_no_parity(ws, tmp_path):
    w = FakeWorld(tmp_path, verify_results=[(2, 0)])
    with pytest.raises(ParityNotReached):
        make(ws, w).run()


def test_transient_failures_are_retried(ws, tmp_path):
    w = FakeWorld(tmp_path, fail={"analyst": 1, "rebuilder": 2})
    assert make(ws, w).run().status == "passed"
    assert w.calls.count("analyst") == 2 and w.calls.count("rebuilder") == 3


def test_leak_is_auto_fixed_before_handoff(ws, tmp_path):
    w = FakeWorld(tmp_path, leak_first_spec=True)
    assert make(ws, w).run().status == "passed"
    assert "leak-fix" in w.calls
    assert "0x1000" not in (w.container_fs / "work/SPEC.md").read_text()


def test_leak_that_wont_go_away_never_crosses(ws, tmp_path):
    w = FakeWorld(tmp_path)
    p = make(ws, w)
    p._claude_lab = lambda *a, **k: None  # spec-writer "fix" does nothing
    (ws.lab_dir / "handoff").mkdir(exist_ok=True)
    (ws.lab_dir / "handoff/SPEC.md").write_text(GOOD_SPEC + "sub_401000\n")
    p.state.done = ["analyst", "spec_writer"]
    with pytest.raises(LeakDetected):
        p.run()
    assert not (w.container_fs / "work/SPEC.md").exists()
    assert p.state.status == "failed" and p.state.error["step"] == "r1.leak_check"


def test_wall_breach_stops_before_rebuild(ws, tmp_path):
    w = FakeWorld(tmp_path, wall_breach={"npm"})
    with pytest.raises(WallBreach):
        make(ws, w).run()
    assert "rebuilder" not in w.calls


def test_verifier_editing_rebuild_is_caught(ws, tmp_path):
    w = FakeWorld(tmp_path, verifier_edits_rebuild=True)
    with pytest.raises(IntegrityError):
        make(ws, w).run()


def test_analyst_modifying_target_is_caught(ws, tmp_path):
    w = FakeWorld(tmp_path, analyst_touches_target=True)
    with pytest.raises(IntegrityError):
        make(ws, w).run()


def test_resume_skips_finished_steps(ws, tmp_path):
    w = FakeWorld(tmp_path, fail={"verifier": 99})
    p = make(ws, w)
    with pytest.raises(StepFailed):
        p.run()
    st = RunState.load(ws.state_dir, p.state.run_id)
    assert st.status == "failed" and st.error["step"] == "r1.verify"
    assert "r1.rebuild" in st.done

    w2 = FakeWorld(tmp_path / "again")
    shutil_copy_container(w, w2)
    assert make(ws, w2, st).run().status == "passed"
    assert w2.calls == ["verifier"]  # analyst/spec/rebuild not repeated


def shutil_copy_container(a, b):
    import shutil
    shutil.rmtree(b.container_fs)
    shutil.copytree(a.container_fs, b.container_fs)


def test_cli_run_requires_authorization(ws, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["run", "target/app.bin", "search", "--skip-preflight"]) == 12
