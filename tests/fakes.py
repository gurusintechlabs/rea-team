"""A fake Runner that plays claude, docker and the clean container."""

from __future__ import annotations

import shutil
from pathlib import Path

from rea_team.errors import StepFailed, TransientError
from rea_team.runner import Result

GOOD_SPEC = """# SPEC
## Architecture
Search module.
## Data Model
Items with title, body, modified date.
## Public API
search(query) -> list of items
## File Formats
None.
## Errors
Empty index returns empty list.
## Test Cases
- query "a" returns items containing "a"
## Open questions
- default weight values
"""


class FakeWorld:
    def __init__(self, tmp: Path, *, verify_results=None, fail=None, leak_first_spec=False,
                 wall_breach=None, verifier_edits_rebuild=False, analyst_touches_target=False,
                 container_running=True):
        self.container_fs = tmp / "container"
        (self.container_fs / "work").mkdir(parents=True)
        self.verify_results = list(verify_results or [(0, 0)])
        self.fail = dict(fail or {})           # step -> number of times to fail
        self.leak_first_spec = leak_first_spec
        self.wall_breach = set(wall_breach or [])
        self.verifier_edits_rebuild = verifier_edits_rebuild
        self.analyst_touches_target = analyst_touches_target
        self.container_running = container_running
        self.calls: list[str] = []
        self.verify_round = 0
        self.log_dir = None

    def _cpath(self, p: str) -> Path:
        return self.container_fs / p.lstrip("/")

    def _maybe_fail(self, step: str):
        if self.fail.get(step, 0) > 0:
            self.fail[step] -= 1
            raise TransientError(f"simulated failure in {step}")

    def run(self, cmd, *, cwd=None, timeout=None, log_name=None, check=True, stdin=None):
        cmd = list(cmd)
        ok = Result(0, "", "")
        bad = Result(1, "", "nope")

        if cmd[0] == "claude" and cmd[1] == "-p":
            return self._claude_lab(cmd[2], Path(cwd))
        if cmd[0] == "docker":
            res = self._docker(cmd[1:])
            if check and res.returncode != 0:
                raise TransientError(f"docker {' '.join(cmd[1:])} failed")
            return res
        if cmd[0] == "missing":
            raise StepFailed("Command not found")
        return ok if True else bad

    def _claude_lab(self, prompt: str, lab: Path) -> Result:
        if "analyst" in prompt:
            self.calls.append("analyst")
            self._maybe_fail("analyst")
            (lab / "analysis").mkdir(exist_ok=True)
            (lab / "analysis/evidence.md").write_text("## F-014 rank\nobservation x\nunknown y\n")
            if self.analyst_touches_target:
                (lab / "target/app.bin").write_text("PATCHED")
        elif "leaks implementation" in prompt:
            self.calls.append("leak-fix")
            (lab / "handoff/SPEC.md").write_text(GOOD_SPEC)
        elif "add every wrong" in prompt:
            self.calls.append("spec-update")
            self._maybe_fail("spec-update")
            spec = lab / "handoff/SPEC.md"
            spec.write_text(spec.read_text().replace("## Open questions",
                                                     f"- new case {self.verify_round}\n## Open questions"))
        elif "spec-writer" in prompt:
            self.calls.append("spec-writer")
            self._maybe_fail("spec-writer")
            (lab / "handoff").mkdir(exist_ok=True)
            text = GOOD_SPEC + ("\nweights at 0x100012ab0\n" if self.leak_first_spec else "")
            self.leak_first_spec = False
            (lab / "handoff/SPEC.md").write_text(text)
        elif "verifier" in prompt:
            self.calls.append("verifier")
            self._maybe_fail("verifier")
            wrong, crash = self.verify_results[min(self.verify_round, len(self.verify_results) - 1)]
            self.verify_round += 1
            if self.verifier_edits_rebuild:
                (lab / "rebuild/main.py").write_text("hacked")
            (lab / "report.md").write_text(
                f"ordinary   10 / 10   100%\nedge   {10 - wrong} / 10   {100 - wrong * 10}%\n"
                f"RESULT wrong={wrong} crash={crash}\n")
        return Result(0, "done", "")

    def _docker(self, a: list[str]) -> Result:
        ok, bad = Result(0, "", ""), Result(1, "", "err")
        if a[0] == "inspect":
            return Result(0, "true\n" if self.container_running else "false\n", "")
        if a[0] == "cp":
            src, dst = a[1], a[2]
            if src.startswith("rea-clean:"):
                s = self._cpath(src.split(":", 1)[1])
                if s.is_dir():
                    shutil.copytree(s, dst)
                else:
                    shutil.copy2(s, dst)
            else:
                d = self._cpath(dst.split(":", 1)[1])
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, d)
            return ok
        if a[0] == "exec":
            rest = a[a.index("rea-clean") + 1:]
            if rest[:2] == ["claude", "-p"]:
                self.calls.append("rebuilder")
                self._maybe_fail("rebuilder")
                src = self._cpath("/work/src")
                src.mkdir(exist_ok=True)
                (src / "main.py").write_text(f"# rebuild {self.verify_round}\n")
                return ok
            if rest == ["claude", "--version"]:
                return Result(0, "2.0.0", "")
            if rest[:2] == ["sh", "-c"]:
                return ok if "rea" in self.wall_breach else bad
            if rest[0] == "npx":
                return ok if "npx" in self.wall_breach else bad
            if rest[0] == "npm":
                return ok if "npm" in self.wall_breach else bad
            if rest[0] == "test":
                p = rest[2]
                if p in ("/lab", "/target", "/analysis"):
                    return ok if "lab" in self.wall_breach else bad
                return ok if self._cpath(p).exists() else bad
            if rest[0] == "rm":
                self._cpath(rest[-1]).unlink(missing_ok=True)
                return ok
            if rest[0] == "chmod":
                return ok
        return ok
