"""Project pipeline: plan, multi-feature builds, regression gate, rollback, resume."""

import subprocess

import pytest

from rea_team import config, features as featmod
from rea_team.cli import main
from rea_team.errors import (ContractViolation, GateFailed, IntegrityError, LeakDetected,
                             ParityNotReached, SpecTooLarge, StepFailed, WallBreach)
from rea_team.pipeline import Project
from rea_team.state import ProjectState

from .fakes import PROPOSED, TEST_COMMAND, Behaviour, FakeWorld

THREE = PROPOSED + """
[[feature]]
id = "publish"
title = "Publish"
scope = "User publishes a site."
target = "target/app"
mode = "source"
depends_on = ["create-site"]
"""


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    assert main(["init", str(root)]) == 0
    (root / "lab/target/app").mkdir(parents=True)
    (root / "lab/target/app/main.py").write_text("print('legacy')\n")
    toml = root / "rea-team.toml"
    toml.write_text(toml.read_text()
                    .replace('test_command = "npm test --prefix src && npx playwright test tests/acceptance"',
                             f"test_command = '{TEST_COMMAND}'")
                    .replace("retry_backoff_seconds = 15", "retry_backoff_seconds = 0"))
    monkeypatch.chdir(root)
    return config.load()


def world(ws, tmp_path, **kw):
    return FakeWorld(tmp_path, ws.lab_dir, **kw)


def project(ws, w, state=None):
    return Project(ws, state or ProjectState(authorized_at="now"), runner=w, sleep=lambda s: None)


def plan_and_accept(ws, w, text=PROPOSED):
    p = project(ws, w)
    p.plan(["target/app"], "source")
    ws.features_file.write_text(text)
    return p


def git_log(w):
    return subprocess.run(["git", "log", "--oneline"], cwd=w.work, capture_output=True, text=True).stdout


# --- plan ---------------------------------------------------------------------------------

def test_plan_writes_survey_proposal_and_contracts(ws, tmp_path):
    w = world(ws, tmp_path)
    p = project(ws, w)
    p.plan(["target/app"], "source")
    assert w.calls == ["survey", "contracts"]
    assert (ws.root / "features.proposed.toml").exists()
    assert (ws.lab_dir / "handoff/CONTRACTS.md").exists()
    assert (ws.state_dir / "contracts/approved.md").exists()
    p.plan(["target/app"], "source")  # idempotent: nothing re-runs
    assert w.calls == ["survey", "contracts"]


def test_plan_rejects_missing_target(ws, tmp_path):
    with pytest.raises(StepFailed):
        project(ws, world(ws, tmp_path)).plan(["target/nope"], "source")


# --- building features ---------------------------------------------------------------------

def test_two_features_happy_path_with_commits(ws, tmp_path):
    w = world(ws, tmp_path)
    p = plan_and_accept(ws, w)
    out = p.run(featmod.load(ws.features_file))
    assert out == {"create-site": "passed", "add-page": "passed"}
    log = git_log(w)
    assert "feature create-site" in log and "feature add-page" in log and "baseline" in log
    assert (w.work / "specs/features/add-page.md").exists()
    # CONTRACTS grew, never changed
    c = (w.work / "specs/CONTRACTS.md").read_text()
    assert "Additions (create-site)" in c and "Additions (add-page)" in c
    # rebuild + tests copied back for the verifier; BUILD_STATE archived
    assert (ws.lab_dir / "rebuild/tests/acceptance/add-page/test.sh").exists()
    assert (ws.state_dir / "features/add-page/round-1/BUILD_STATE.md").exists()
    assert w.calls.index("rebuilder:create-site") < w.calls.index("analyst:add-page")


def test_regression_caught_and_fixed_by_gate(ws, tmp_path):
    w = world(ws, tmp_path, behaviours={"add-page": Behaviour(break_previous=True)})
    p = plan_and_accept(ws, w)
    p.run(featmod.load(ws.features_file))
    fs = p.state.features["add-page"]
    assert [g["exit"] != 0 for g in fs.gate] == [True, False]  # failed, then fixed
    assert "fix:add-page" in w.calls
    assert (ws.state_dir / "features/add-page/round-1/gate-0.txt").exists()


def test_gate_failure_rolls_back_clean_room(ws, tmp_path):
    w = world(ws, tmp_path, behaviours={"add-page": Behaviour(never_passes_gate=True)})
    p = plan_and_accept(ws, w)
    with pytest.raises(GateFailed):
        p.run(featmod.load(ws.features_file))
    fs = p.state.features["add-page"]
    assert fs.status == "failed" and fs.error["type"] == "GateFailed"
    assert "rolled_back_to" in fs.error
    assert fs.done == ["analyst", "spec"]  # lab work kept, clean-room steps to redo
    # rolled back: add-page's tests and specs are gone, create-site intact
    assert not (w.work / "tests/acceptance/add-page").exists()
    assert (w.work / "src/create-site.txt").exists()
    assert len([g for g in fs.gate if g["exit"]]) == ws.fix_attempts + 1


def test_keep_going_blocks_dependents_only(ws, tmp_path):
    w = world(ws, tmp_path, behaviours={"create-site": Behaviour(never_passes_gate=True)})
    p = plan_and_accept(ws, w, THREE)
    ws.keep_going = True
    with pytest.raises(GateFailed):
        p.run(featmod.load(ws.features_file))
    st = {k: v.status for k, v in p.state.features.items()}
    assert st == {"create-site": "failed", "add-page": "blocked", "publish": "blocked"}


def test_keep_going_continues_independent_feature(ws, tmp_path):
    w = world(ws, tmp_path, behaviours={"add-page": Behaviour(never_passes_gate=True)})
    p = plan_and_accept(ws, w, THREE)
    ws.keep_going = True
    with pytest.raises(GateFailed):
        p.run(featmod.load(ws.features_file))
    st = {k: v.status for k, v in p.state.features.items()}
    assert st == {"create-site": "passed", "add-page": "failed", "publish": "passed"}


def test_rounds_until_parity(ws, tmp_path):
    w = world(ws, tmp_path, behaviours={"create-site": Behaviour(verify=[(2, 0), (0, 0)])})
    p = plan_and_accept(ws, w)
    p.run(featmod.load(ws.features_file), only=["create-site"])
    fs = p.state.features["create-site"]
    assert fs.status == "passed" and fs.round == 2
    assert "spec-update:create-site" in w.calls
    assert w.calls.count("test-writer:create-site") == 2  # tests updated for new cases
    assert "TC-new" in (w.work / "specs/features/create-site.md").read_text()


def test_no_parity_updates_spec_then_extra_round_passes(ws, tmp_path):
    w = world(ws, tmp_path, behaviours={"create-site": Behaviour(verify=[(1, 0)] * 3 + [(0, 0)])})
    p = plan_and_accept(ws, w)
    with pytest.raises(ParityNotReached):
        p.run(featmod.load(ws.features_file))
    fs = p.state.features["create-site"]
    assert fs.round == 4 and w.calls.count("spec-update:create-site") == 3
    ws.max_rounds = 4
    assert p.run(featmod.load(ws.features_file))["create-site"] == "passed"


def test_transient_failures_retried(ws, tmp_path):
    w = world(ws, tmp_path, behaviours={"create-site": Behaviour(
        fail_times={"analyst": 1, "test-writer": 1, "rebuilder": 2, "verifier": 1})})
    p = plan_and_accept(ws, w)
    assert p.run(featmod.load(ws.features_file))["create-site"] == "passed"


def test_resume_skips_finished_work(ws, tmp_path):
    w = world(ws, tmp_path, behaviours={"add-page": Behaviour(fail_times={"verifier": 99})})
    p = plan_and_accept(ws, w)
    with pytest.raises(StepFailed):
        p.run(featmod.load(ws.features_file))
    assert p.state.features["create-site"].status == "passed"
    w.beh("add-page").fail_times["verifier"] = 0
    w.calls.clear()
    state = ProjectState.load(ws.state_dir)
    project(ws, w, state).run(featmod.load(ws.features_file))
    assert not any(c.endswith(":create-site") for c in w.calls)          # passed feature untouched
    assert "analyst:add-page" not in w.calls and "spec:add-page" not in w.calls  # lab work kept


# --- safety walls ------------------------------------------------------------------------------

def test_leak_auto_fixed(ws, tmp_path):
    w = world(ws, tmp_path, behaviours={"create-site": Behaviour(leaks=True)})
    p = plan_and_accept(ws, w)
    p.run(featmod.load(ws.features_file), only=["create-site"])
    assert "leak-fix" in w.calls
    assert "0x1000" not in (w.work / "specs/features/create-site.md").read_text()


def test_leak_never_crosses(ws, tmp_path, monkeypatch):
    w = world(ws, tmp_path, behaviours={"create-site": Behaviour(leaks=True)})
    p = plan_and_accept(ws, w)
    orig = w._lab_agent
    monkeypatch.setattr(w, "_lab_agent", lambda pr: None if "leak implementation" in pr else orig(pr))
    with pytest.raises(LeakDetected):
        p.run(featmod.load(ws.features_file))
    assert not (w.work / "specs").exists()


def test_contract_rewrite_is_blocked(ws, tmp_path):
    w = world(ws, tmp_path, behaviours={"add-page": Behaviour(rewrites_contracts=True)})
    p = plan_and_accept(ws, w)
    with pytest.raises(ContractViolation):
        p.run(featmod.load(ws.features_file))
    assert p.state.features["create-site"].status == "passed"


def test_rebuilder_editing_tests_is_caught(ws, tmp_path):
    w = world(ws, tmp_path, behaviours={"create-site": Behaviour(edits_tests=True)})
    with pytest.raises(IntegrityError):
        plan_and_accept(ws, w).run(featmod.load(ws.features_file))


def test_wall_breach_stops_before_any_clean_agent(ws, tmp_path):
    w = world(ws, tmp_path, wall_breach=True)
    p = plan_and_accept(ws, w)
    ws.keep_going = True  # fatal errors ignore keep_going
    with pytest.raises(WallBreach):
        p.run(featmod.load(ws.features_file))
    assert not any(c.startswith(("test-writer", "rebuilder")) for c in w.calls)


def test_huge_spec_asks_to_split(ws, tmp_path):
    w = world(ws, tmp_path, behaviours={"create-site": Behaviour(huge_spec=True)})
    with pytest.raises(SpecTooLarge):
        plan_and_accept(ws, w).run(featmod.load(ws.features_file))


def test_analyst_modifying_target_is_caught(ws, tmp_path, monkeypatch):
    w = world(ws, tmp_path)
    p = plan_and_accept(ws, w)
    orig = w._lab_agent

    def evil(prompt):
        orig(prompt)
        if "analyst agent" in prompt:
            (ws.lab_dir / "target/app/main.py").write_text("patched")
    monkeypatch.setattr(w, "_lab_agent", evil)
    with pytest.raises(IntegrityError):
        p.run(featmod.load(ws.features_file))


def test_decompiled_mode_runs_decompiler_once(ws, tmp_path):
    ws.decompile_command = "cp -R {input} {output}/out"
    w = world(ws, tmp_path)
    text = PROPOSED.replace('mode = "source"', 'mode = "decompiled"')
    p = plan_and_accept(ws, w, text)
    p.run(featmod.load(ws.features_file))
    assert (ws.lab_dir / "analysis/decompiled/target_app/out/main.py").exists()


def test_run_only_pulls_in_dependencies(ws, tmp_path):
    w = world(ws, tmp_path)
    p = plan_and_accept(ws, w, THREE)
    out = p.run(featmod.load(ws.features_file), only=["add-page"])
    assert set(out) == {"create-site", "add-page"}


def test_interrupted_feature_leftovers_not_committed_by_another(ws, tmp_path):
    w = world(ws, tmp_path)
    p = plan_and_accept(ws, w, THREE)
    feats = featmod.load(ws.features_file)
    p.run(feats, only=["create-site"])
    # simulate: add-page was interrupted after its rebuild wrote code
    fs = p.state.feature("add-page")
    fs.status, fs.done = "interrupted", ["analyst", "spec", "r1.leak", "r1.handoff", "r1.wall", "r1.tests", "r1.build"]
    (w.work / "src/add-page.txt").write_text("half done\n")
    p.run(feats, only=["publish"])
    assert "add-page" not in subprocess.run(["git", "show", "--stat", "rea/publish"], cwd=w.work,
                                            capture_output=True, text=True).stdout
    assert p.state.features["add-page"].done == ["analyst", "spec"]


def test_verifier_running_the_rebuild_is_not_tampering(ws, tmp_path, monkeypatch):
    """npm ci / playwright create node_modules and test-results: allowed. Editing source: not."""
    w = world(ws, tmp_path)
    p = plan_and_accept(ws, w)
    orig = w._lab_agent

    def verifier_that_runs_things(prompt):
        orig(prompt)
        if "verifier agent" in prompt:
            nm = ws.lab_dir / "rebuild/src/node_modules/pkg"
            nm.mkdir(parents=True, exist_ok=True)
            (nm / "index.js").write_text("x")
            (ws.lab_dir / "rebuild/test-results").mkdir(exist_ok=True)
            (ws.lab_dir / "rebuild/test-results/run.json").write_text("{}")
    monkeypatch.setattr(w, "_lab_agent", verifier_that_runs_things)
    assert p.run(featmod.load(ws.features_file))["create-site"] == "passed"


def test_verifier_editing_rebuild_source_is_caught(ws, tmp_path, monkeypatch):
    w = world(ws, tmp_path)
    p = plan_and_accept(ws, w)
    orig = w._lab_agent

    def evil(prompt):
        orig(prompt)
        if "verifier agent" in prompt:
            (ws.lab_dir / "rebuild/src/create-site.txt").write_text("patched by verifier")
    monkeypatch.setattr(w, "_lab_agent", evil)
    with pytest.raises(IntegrityError, match="verifier"):
        p.run(featmod.load(ws.features_file))
