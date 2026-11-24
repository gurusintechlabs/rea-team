"""A fake world for the project pipeline.

- `claude -p` in the lab and in the container is played by small Python functions
  that write the files real agents would write.
- `docker exec` of anything else REALLY runs in a local folder standing in for the
  container's /work: real sh, find, sha256sum, chmod and git. So hashing, read-only
  locks, the regression test gate, commits and rollbacks are exercised for real.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from rea_team.errors import TransientError
from rea_team.runner import Result

CONTRACTS = """# Contracts
## Conventions
Errors are JSON {error: string}.
## Data Model
- Site: id, name
## Public API
- POST /sites
"""

FEATURE_SPEC = """# {fid}
## Architecture
Uses Site from CONTRACTS.md.
## Data Model
none
## Public API
see contracts
## File Formats
none
## Errors
none
## Test Cases
- TC-1 {fid} works
## Open questions
- none
"""

# Real regression gate: every feature's acceptance test must pass.
TEST_COMMAND = 'for t in tests/acceptance/*/test.sh; do [ -e "$t" ] || continue; sh "$t" || exit 1; done'

PROPOSED = """[[feature]]
id = "create-site"
title = "Create site"
scope = "User creates a site."
target = "target/app"
mode = "source"

[[feature]]
id = "add-page"
title = "Add page"
scope = "User adds a page."
target = "target/app"
mode = "source"
depends_on = ["create-site"]
"""


@dataclass
class Behaviour:
    verify: list[tuple[int, int]] = field(default_factory=lambda: [(0, 0)])  # (wrong, crash) per round
    fail_times: dict[str, int] = field(default_factory=dict)   # step -> transient failures
    break_previous: bool = False      # first build deletes an earlier feature's code (regression)
    never_passes_gate: bool = False   # implementation never satisfies its tests
    edits_tests: bool = False         # rebuilder tampers with acceptance tests
    rewrites_contracts: bool = False  # spec-writer edits an existing contracts line
    leaks: bool = False               # spec contains an address the first time
    huge_spec: bool = False


class FakeWorld:
    def __init__(self, tmp: Path, lab: Path, behaviours: dict[str, Behaviour] | None = None,
                 wall_breach: bool = False):
        self.work = tmp / "container-work"
        self.work.mkdir(parents=True, exist_ok=True)
        self.lab = lab
        self.b = behaviours or {}
        self.wall_breach = wall_breach
        self.calls: list[str] = []
        self.rounds: dict[str, int] = {}
        self.log_dir = None
        self._broke: set[str] = set()

    def beh(self, fid: str) -> Behaviour:
        return self.b.setdefault(fid, Behaviour())

    def _fail(self, fid: str, step: str):
        b = self.beh(fid)
        if b.fail_times.get(step, 0) > 0:
            b.fail_times[step] -= 1
            raise TransientError(f"simulated {step} failure")

    # --- Runner interface ------------------------------------------------------------

    def run(self, cmd, *, cwd=None, timeout=None, log_name=None, check=True, stdin=None):
        cmd = list(cmd)
        if cmd[:2] == ["claude", "-p"]:
            self._lab_agent(cmd[2])
            return Result(0, "ok", "")
        if cmd[0] == "docker":
            res = self._docker(cmd[1:])
            if check and res.returncode != 0:
                raise TransientError(f"docker {' '.join(cmd[1:4])} failed: {res.stderr[-300:]}")
            return res
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)  # e.g. decompiler
        if check and res.returncode != 0:
            raise TransientError(f"exit {res.returncode}")
        return Result(res.returncode, res.stdout, res.stderr)

    # --- lab agents ------------------------------------------------------------------------

    @staticmethod
    def _fid(prompt: str) -> str:
        for marker in ("Feature id: ", "handoff/features/", "analysis/features/"):
            if marker in prompt:
                rest = prompt.split(marker, 1)[1]
                return rest.split(".")[0].split()[0].rstrip(".").removesuffix(".md").split("/")[0]
        return ""

    def _lab_agent(self, prompt: str):
        lab = self.lab
        if "SURVEY" in prompt:
            self.calls.append("survey")
            (lab / "analysis/survey.md").write_text("observation: app with sites and pages\n")
            (lab.parent / "features.proposed.toml").write_text(PROPOSED)
            return
        if "handoff/CONTRACTS.md from analysis/survey.md" in prompt:
            self.calls.append("contracts")
            (lab / "handoff/CONTRACTS.md").write_text(CONTRACTS)
            return
        if "leak implementation details" in prompt:
            self.calls.append("leak-fix")
            for p in (lab / "handoff/features").glob("*.md"):
                p.write_text(p.read_text().replace("at 0x100012ab0", "at startup"))
            return
        fid = self._fid(prompt)
        b = self.beh(fid)
        if "analyst agent" in prompt:
            self.calls.append(f"analyst:{fid}")
            self._fail(fid, "analyst")
            d = lab / "analysis/features" / fid
            d.mkdir(parents=True, exist_ok=True)
            (d / "evidence.md").write_text("observation a\nunknown b\n")
        elif "verifier agent" in prompt:
            self.calls.append(f"verifier:{fid}")
            self._fail(fid, "verifier")
            n = self.rounds.get(fid, 0)
            self.rounds[fid] = n + 1
            wrong, crash = b.verify[min(n, len(b.verify) - 1)]
            (lab / "analysis/features" / fid / "report.md").write_text(
                f"ordinary 10 / 10 100%\nRESULT wrong={wrong} crash={crash} spec_wrong=0\n")
        elif "to update handoff/features" in prompt:
            self.calls.append(f"spec-update:{fid}")
            p = lab / "handoff/features" / f"{fid}.md"
            p.write_text(p.read_text().replace("## Open questions", f"- TC-new{self.rounds.get(fid)}\n## Open questions"))
        elif "spec-writer agent to write handoff/features" in prompt:
            self.calls.append(f"spec:{fid}")
            self._fail(fid, "spec")
            spec = FEATURE_SPEC.format(fid=fid)
            if b.leaks:
                spec += "weights at 0x100012ab0\n"
            if b.huge_spec:
                spec += "x" * 70000
            (lab / "handoff/features").mkdir(parents=True, exist_ok=True)
            (lab / "handoff/features" / f"{fid}.md").write_text(spec)
            c = lab / "handoff/CONTRACTS.md"
            if b.rewrites_contracts:
                c.write_text(c.read_text().replace("- Site: id, name", "- Site: id, title"))
            else:
                c.write_text(c.read_text() + f"\n## Additions ({fid})\n- endpoint for {fid}\n")

    # --- docker ----------------------------------------------------------------------------

    def _map(self, p: str) -> str:
        return str(self.work / p[len("/work/"):]) if p.startswith("/work/") else (
            str(self.work) if p == "/work" else p)

    def _docker(self, a: list[str]) -> Result:
        if a[0] == "inspect":
            return Result(0, "true\n", "")
        if a[0] == "cp":
            src, dst = a[1], a[2]
            if src.startswith("rea-clean:"):
                s = Path(self._map(src.split(":", 1)[1]))
                shutil.copytree(s, dst) if s.is_dir() else shutil.copy2(s, dst)
            else:
                d = Path(self._map(dst.split(":", 1)[1]))
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, d)
            return Result(0, "", "")
        assert a[0] == "exec"
        rest = a[a.index("rea-clean") + 1:]
        if rest[:2] == ["claude", "-p"]:
            self._clean_agent(rest[2])
            return Result(0, "ok", "")
        if rest == ["claude", "--version"]:
            return Result(0, "2.0", "")
        # wall probes
        if rest[:2] == ["sh", "-c"] and rest[2] == "command -v rea":
            return Result(0 if self.wall_breach else 1, "", "")
        if rest[0] in ("npx", "npm"):
            return Result(1, "", "blocked")
        if rest[0] == "test" and rest[2] in ("/lab", "/target", "/analysis"):
            return Result(1, "", "")
        args = [self._map(x) for x in rest]
        r = subprocess.run(args, cwd=self.work, capture_output=True, text=True)
        return Result(r.returncode, r.stdout, r.stderr)

    # --- clean agents --------------------------------------------------------------------------

    def _clean_agent(self, prompt: str):
        fid = self._fid(prompt)
        b = self.beh(fid)
        w = self.work
        if "test-writer agent" in prompt:
            self.calls.append(f"test-writer:{fid}")
            self._fail(fid, "test-writer")
            d = w / "tests/acceptance" / fid
            d.mkdir(parents=True, exist_ok=True)
            (d / "test.sh").write_text(f'grep -q "impl {fid}" src/{fid}.txt\n')
        elif "rebuilder agent" in prompt:
            fixing = "fails" in prompt
            self.calls.append(f"{'fix' if fixing else 'rebuilder'}:{fid}")
            self._fail(fid, "rebuilder")
            (w / "src").mkdir(exist_ok=True)
            if not b.never_passes_gate:
                (w / f"src/{fid}.txt").write_text(f"impl {fid}\n")
            if b.break_previous and not fixing and fid not in self._broke:
                self._broke.add(fid)
                for p in (w / "src").glob("*.txt"):
                    if p.name != f"{fid}.txt":
                        p.unlink()  # regression: an earlier feature stops working
            if fixing:
                # restore anything broken, the way a real fix session would
                for d in (w / "tests/acceptance").iterdir():
                    if d.is_dir() and not d.name.startswith("_") and not b.never_passes_gate:
                        (w / f"src/{d.name}.txt").write_text(f"impl {d.name}\n")
            if b.edits_tests:
                t = w / "tests/acceptance" / fid / "test.sh"
                t.chmod(0o644)
                t.write_text("true\n")
            state = w / "BUILD_STATE.md"
            prev = state.read_text() if state.exists() else "# Build state\n## Features done\n"
            state.write_text(prev + f"- {fid}\n")
