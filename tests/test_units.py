"""Pure functions: features graph, checks, retries, config, state."""

import sys

import pytest

from rea_team import checks, config, features as featmod
from rea_team.cli import main
from rea_team.errors import (ConfigError, ContractViolation, FeatureGraphError, LeakDetected,
                             ReportFormatError, StepFailed, TransientError)
from rea_team.errors import ReaTeamError
from rea_team.preflight import agent_tools, extract_rea_tools, list_mcp_tools, node_ok
from rea_team.runner import Runner, with_retries
from rea_team.state import FeatureState, ProjectState

F = """[[feature]]
id = "{id}"
title = "t"
scope = "s"
target = "target/x"
depends_on = {deps}
"""


def feats(*pairs):
    return "\n".join(F.format(id=i, deps=d) for i, d in pairs)


def test_feature_order_respects_dependencies():
    out = featmod.order(featmod.parse(feats(("c", '["b"]'), ("b", '["a"]'), ("a", "[]"))))
    assert [f.id for f in out] == ["a", "b", "c"]


@pytest.mark.parametrize("text,msg", [
    (feats(("a", '["b"]'), ("b", '["a"]')), "cycle"),
    (feats(("a", '["zzz"]')), "unknown feature"),
    (feats(("a", "[]"), ("a", "[]")), "Duplicate"),
    (feats(("Bad_ID", "[]")), "lowercase"),
    ('[[feature]]\nid = "a"\n', "required"),
    ('[[feature]]\nid="a"\ntitle="t"\nscope="s"\ntarget="t"\nmode="magic"\n', "mode"),
    ('[[feature]]\nid="a"\ntitle="t"\nscope="s"\ntarget="t"\ncolour="red"\n', "unknown keys"),
    ("not toml [", "features.toml"),
])
def test_feature_errors(text, msg):
    with pytest.raises(FeatureGraphError, match=msg):
        featmod.order(featmod.parse(text))


def test_dependents_transitive():
    fs = featmod.parse(feats(("a", "[]"), ("b", '["a"]'), ("c", '["b"]'), ("d", "[]")))
    assert featmod.dependents(fs, "a") == {"b", "c"}


def test_leak_scan():
    pats = ["0x[0-9a-f]{5,}", "sub_[0-9a-f]+", "FUN_[0-9a-f]+"]
    hits = checks.leak_scan("ok\ncalls FUN_00401000\nat 0x100012ab0\nport 0x1f\n", pats)
    assert [n for n, _ in hits] == [2, 3]


def test_report_parsing():
    r = checks.parse_report("edge 36 / 40 90%\nRESULT wrong=4 crash=0\n")
    assert (r.wrong, r.crash, r.spec_wrong, r.passed) == (4, 0, 0, False)
    r = checks.parse_report("RESULT wrong=0 crash=0 spec_wrong=2\n")
    assert r.spec_wrong == 2 and not r.passed
    assert checks.parse_report("RESULT wrong=0 crash=0 spec_wrong=0").passed
    for bad in ["", "total 1/1 100%\n", "RESULT wrong=x crash=0"]:
        with pytest.raises(ReportFormatError):
            checks.parse_report(bad)


def test_contracts_append_only():
    checks.assert_append_only("a\nb\n", "a\nb\nc\n")
    with pytest.raises(ContractViolation, match="line 2"):
        checks.assert_append_only("a\nb\n", "a\nB\nc\n")
    with pytest.raises(ContractViolation):
        checks.assert_append_only("a\nb\n", "a\n")


def test_retries():
    calls = []

    def flaky():
        calls.append(1)
        raise TransientError("x")
    with pytest.raises(StepFailed):
        with_retries(flaky, what="x", retries=2, backoff=0, sleep=lambda s: None)
    assert len(calls) == 3

    def leak():
        raise LeakDetected("never retried")
    with pytest.raises(LeakDetected):
        with_retries(leak, what="x", retries=5, backoff=0, sleep=lambda s: None)


def test_runner_timeout_and_missing_command(tmp_path):
    r = Runner(tmp_path)
    with pytest.raises(TransientError, match="Timed out"):
        r.run(["sleep", "5"], timeout=0.3, log_name="t")
    assert "TIMEOUT" in (tmp_path / "t.log").read_text()
    with pytest.raises(StepFailed, match="not found"):
        r.run(["definitely-not-a-command-xyz"])
    assert r.run(["sh", "-c", "exit 3"], check=False).returncode == 3
    with pytest.raises(TransientError, match="Exit 3"):
        r.run(["sh", "-c", "echo boom >&2; exit 3"])


def test_node_versions():
    assert node_ok("v22.19.0") and node_ok("v24.11.1") and node_ok("v26.0.0")
    assert not node_ok("v22.18.0") and not node_ok("v20.10.0") and not node_ok("v25.1.0")


def test_feature_state_reset():
    fs = FeatureState("a", status="failed", round=3, done=["analyst", "spec", "r1.leak", "r2.build"],
                      results=[{"round": 1}, {"round": 2}])
    fs.reset("r2.build")
    assert fs.done == ["analyst", "spec", "r1.leak"] and fs.round == 2 and fs.results == [{"round": 1}]
    fs.reset(None)
    assert fs.done == [] and fs.round == 1
    with pytest.raises(Exception):
        fs.reset("nope")


def test_state_roundtrip_and_corruption(tmp_path):
    s = ProjectState(authorized_at="x")
    s.feature("a").done.append("analyst")
    s.save(tmp_path)
    assert ProjectState.load(tmp_path).features["a"].done == ["analyst"]
    (tmp_path / "project.json").write_text("{nope")
    with pytest.raises(Exception, match="corrupt"):
        ProjectState.load(tmp_path)


def test_config_validation(tmp_path, monkeypatch):
    main(["init", str(tmp_path / "w")])
    monkeypatch.chdir(tmp_path / "w")
    cfg_file = tmp_path / "w/rea-team.toml"
    base = cfg_file.read_text()
    assert config.load().test_command
    for bad in [base.replace('clean_dir = "clean"', 'clean_dir = "lab/clean"'),
                base.replace("max_rounds = 3", "max_rounds = 0"),
                base.replace('default_mode = "rea"', 'default_mode = "x"'),
                base.replace("fix_attempts = 3", 'fix_attempts = "3"'),
                base.split("test_command")[0] + 'test_command = ""\n' + base.split("test_command")[1].split("\n", 1)[1]]:
        cfg_file.write_text(bad)
        with pytest.raises(ConfigError):
            config.load()


def test_set_tools(tmp_path, monkeypatch):
    main(["init", str(tmp_path / "w")])
    monkeypatch.chdir(tmp_path / "w")
    doc = tmp_path / "doctor.json"
    doc.write_text('{"mcp": {"tools": [{"name": "open_binary"}, {"name": "analyze_javascript_application"}]}}')
    assert extract_rea_tools(doc) == ["mcp__rea__analyze_javascript_application", "mcp__rea__open_binary"]
    assert main(["set-tools", "--from-doctor", str(doc)]) == 0
    tools = agent_tools(tmp_path / "w/lab/.claude/agents/analyst.md")
    assert "mcp__rea__open_binary" in tools and "Read" in tools


FAKE_MCP = r"""
import json, sys
pages = {None: (["open_binary", "list_strings"], "p2"), "p2": (["decompile"], None)}
print("starting up")  # stray stdout noise must be skipped
sys.stdout.flush()
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
    else:
        names, nxt = pages[m["params"].get("cursor")]
        r = {"tools": [{"name": n} for n in names], **({"nextCursor": nxt} if nxt else {})}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}))
    sys.stdout.flush()
"""


def test_set_tools_from_mcp(tmp_path, monkeypatch):
    server = tmp_path / "fake_mcp.py"
    server.write_text(FAKE_MCP)
    cmd = f"{sys.executable} -I {server}"
    assert list_mcp_tools(cmd, timeout=20) == ["mcp__rea__open_binary", "mcp__rea__list_strings",
                                               "mcp__rea__decompile"]
    main(["init", str(tmp_path / "w")])
    monkeypatch.chdir(tmp_path / "w")
    assert main(["set-tools", "--from-mcp", cmd]) == 0
    assert "mcp__rea__decompile" in agent_tools(tmp_path / "w/lab/.claude/agents/analyst.md")


def test_list_mcp_tools_failures(tmp_path):
    with pytest.raises(ReaTeamError, match="Cannot start"):
        list_mcp_tools(str(tmp_path / "no-such-server"))
    silent = tmp_path / "silent.py"
    silent.write_text("import time; time.sleep(30)")
    with pytest.raises(ReaTeamError, match="did not answer"):
        list_mcp_tools(f"{sys.executable} -I {silent}", timeout=1)
    with pytest.raises(ReaTeamError, match="exited"):
        list_mcp_tools(f"{sys.executable} -I -c pass", timeout=10)


def test_doctor_json_appended_twice(tmp_path):
    doc = tmp_path / "doctor.json"
    doc.write_text('{"a": 1}{"a": 1}')
    with pytest.raises(ReaTeamError) as e:
        extract_rea_tools(doc)
    assert "more than one JSON document" in e.value.hint


def test_commented_example_is_not_a_feature(tmp_path):
    p = tmp_path / "features.toml"
    p.write_text(featmod.EXAMPLE)
    assert not featmod.has_entries(p)
    p.write_text(F.format(id="a", deps="[]"))
    assert featmod.has_entries(p)


def test_doctor_before_plan_in_source_mode(tmp_path, monkeypatch, capsys):
    main(["init", str(tmp_path / "w")])
    monkeypatch.chdir(tmp_path / "w")
    toml = tmp_path / "w/rea-team.toml"
    toml.write_text(toml.read_text().replace('default_mode = "rea"', 'default_mode = "source"'))
    main(["doctor"])
    out = capsys.readouterr().out
    assert "features.toml" not in out and "placeholder;" not in out
