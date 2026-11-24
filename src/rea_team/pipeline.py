"""Plan once, then build feature after feature, each in fresh agent sessions.

    plan:     survey -> contracts
    feature:  analyst -> spec -> [ leak -> handoff -> wall -> tests -> build+gate -> verify
              -> (spec_update -> next round) ] -> commit

Every step is checkpointed in lab/.rea-team/project.json. A failed feature is
rolled back in the clean room's git repo to the last passed feature.
"""

from __future__ import annotations

import logging
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Callable

from . import checks, features as featmod
from .config import Config
from .container import CleanRoom
from .errors import (ContainerError, GateFailed, IntegrityError, LeakDetected,
                     NoAcceptanceTests, ParityNotReached, ReaTeamError, StepFailed,
                     TransientError, WallBreach)
from .features import Feature
from .runner import Runner, with_retries
from .state import (BLOCKED, FAILED, INTERRUPTED, PASSED, PENDING, RUNNING,
                    FeatureState, ProjectState)  # noqa: F401

log = logging.getLogger("rea_team")

# errors that stop the whole project even with keep_going
FATAL = (WallBreach, IntegrityError, ContainerError)

PROPOSED_FORMAT = """[[feature]]
id = "kebab-case-id"
title = "Short title"
scope = "One sentence a tester could check."
target = "{target}"
mode = "{mode}"
depends_on = []   # ids of features this one needs"""


class Project:
    def __init__(self, cfg: Config, state: ProjectState, runner: Runner | None = None,
                 room: CleanRoom | None = None, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.state = state
        self.runner = runner or Runner(cfg.state_dir / "logs")
        self.room = room or CleanRoom(cfg, self.runner)
        self.sleep = sleep
        lab = cfg.lab_dir
        self.lab = lab
        self.contracts = lab / "handoff" / "CONTRACTS.md"
        self.contracts_snapshot = cfg.state_dir / "contracts" / "approved.md"

    # ===== helpers ====================================================================

    def _save(self):
        self.state.save(self.cfg.state_dir)

    def _retry(self, what: str, fn):
        return with_retries(fn, what=what, retries=self.cfg.retries,
                            backoff=self.cfg.retry_backoff_seconds, sleep=self.sleep)

    def _claude_lab(self, prompt: str, log_name: str, timeout_key: str):
        cmd = [self.cfg.claude_command, "-p", prompt, *self.cfg.claude_args]
        return self.runner.run(cmd, cwd=self.lab, timeout=self.cfg.timeout(timeout_key), log_name=log_name)

    def _logs(self, sub: str) -> None:
        self.runner.log_dir = self.cfg.state_dir / "logs" / sub

    @staticmethod
    def _fresh(path: Path, what: str, t0: float) -> None:
        """Output must exist and be (re)written by this attempt, else retry the agent."""
        try:
            checks.require_file(path, what, newer_than=t0)
        except ReaTeamError as e:
            raise TransientError(str(e)) from e

    def _archive_dir(self, fid: str, rnd: int | None = None) -> Path:
        d = self.cfg.state_dir / "features" / fid
        return d / f"round-{rnd}" if rnd else d

    def _archive(self, fid: str, rnd: int, *paths: Path) -> None:
        d = self._archive_dir(fid, rnd)
        d.mkdir(parents=True, exist_ok=True)
        for p in paths:
            if p.is_file():
                shutil.copy2(p, d / p.name)

    def _check_contracts(self) -> None:
        """CONTRACTS.md must keep its sections and only grow."""
        if not self.contracts.is_file():
            raise StepFailed("handoff/CONTRACTS.md is missing", hint="Run `rea-team plan` first.")
        text = self.contracts.read_text(errors="replace")
        if self.contracts_snapshot.is_file():
            checks.assert_append_only(self.contracts_snapshot.read_text(), text)
        missing = checks.missing_sections(text, checks.REQUIRED_CONTRACT_SECTIONS)
        if missing:
            raise TransientError(f"CONTRACTS.md is missing sections: {', '.join(missing)}")

    def _approve_contracts(self) -> None:
        self.contracts_snapshot.parent.mkdir(parents=True, exist_ok=True)
        text = self.contracts.read_text()
        if self.contracts_snapshot.is_file() and self.contracts_snapshot.read_text() == text:
            return
        n = len(list(self.contracts_snapshot.parent.glob("CONTRACTS-*.md"))) + 1
        (self.contracts_snapshot.parent / f"CONTRACTS-{n:03d}.md").write_text(text)
        self.contracts_snapshot.write_text(text)

    def _leak_free(self, paths: list[Path], log_name: str) -> None:
        """Scan everything that will cross the wall; let the spec-writer fix leaks, then fail hard."""
        for attempt in range(self.cfg.retries + 1):
            problems = []
            for p in paths:
                try:
                    checks.assert_no_leaks(p, self.cfg.leak_patterns)
                except LeakDetected as e:
                    problems.append(f"{p.relative_to(self.lab)}:\n{e}")
            if not problems:
                return
            if attempt == self.cfg.retries:
                raise LeakDetected("\n".join(problems))
            log.warning("leak found, asking spec-writer to fix (%d/%d)", attempt + 1, self.cfg.retries)
            prompt = ("Use the spec-writer agent. These files leak implementation details from the original "
                      "on the lines below. Rewrite those lines as behavior only, with no addresses, offsets "
                      "or internal names, and change nothing else. In CONTRACTS.md do not delete text: "
                      "rewrite only the leaking words.\n\n" + "\n".join(problems))
            self._retry("spec-writer (leak fix)", lambda: self._claude_lab(prompt, log_name, "spec_writer"))

    # ===== plan ===========================================================================

    def plan(self, targets: list[str], mode: str) -> None:
        self._logs("plan")
        st = self.state
        survey = self.lab / "analysis" / "survey.md"
        proposed = self.cfg.root / "features.proposed.toml"
        survey.parent.mkdir(parents=True, exist_ok=True)
        tlist = ", ".join(targets)

        if "survey" not in st.planned:
            log.info("→ plan.survey")
            for t in targets:
                if not (self.lab / t).exists():
                    raise StepFailed(f"Target not found: lab/{t}")
                self._decompile_if_needed(t, mode)
            fmt = PROPOSED_FORMAT.format(target=targets[0], mode=mode)

            def attempt():
                t0 = time.time()
                self._claude_lab(
                    f"Use the analyst agent for a SURVEY, not a single feature. Targets: {tlist}. "
                    f"Mode: {mode}.{self._mode_note(targets[0], mode)} Map the whole application: tech stack, "
                    "modules, user-facing features, data it stores, external interfaces. Write the survey to "
                    "analysis/survey.md. Then write ../features.proposed.toml: one [[feature]] per user-visible "
                    "feature small enough to describe in one sentence, ordered so foundations come first, "
                    f"using exactly this format:\n{fmt}", "survey", "analyst")
                self._fresh(survey, "analyst (survey)", t0)
                self._fresh(proposed, "analyst (feature proposal)", t0)
                try:
                    featmod.order(featmod.parse(proposed.read_text(), source=proposed.name))
                except ReaTeamError as e:
                    raise TransientError(f"features.proposed.toml is invalid: {e}") from e

            self._retry("survey", attempt)
            st.planned.append("survey")
            self._save()

        if "contracts" not in st.planned:
            log.info("→ plan.contracts")
            self.contracts.parent.mkdir(parents=True, exist_ok=True)

            def attempt():
                t0 = time.time()
                self._claude_lab(
                    "Use the spec-writer agent to write handoff/CONTRACTS.md from analysis/survey.md: the "
                    "conventions, data model and public API that every feature shares. Behavior only.",
                    "contracts", "spec_writer")
                self._fresh(self.contracts, "spec-writer (contracts)", t0)
                self._check_contracts()

            self._retry("contracts", attempt)
            self._leak_free([self.contracts], "contracts-leak-fix")
            self._approve_contracts()
            st.planned.append("contracts")
            self._save()
        log.info("✓ plan done. Review %s and handoff/CONTRACTS.md, then save the list as features.toml",
                 proposed.name)

    # ===== analysis helpers ==================================================================

    def _decompiled_dir(self, target: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", target.strip("/"))
        return self.lab / "analysis" / "decompiled" / slug

    def _decompile_if_needed(self, target: str, mode: str) -> None:
        if mode != "decompiled":
            return
        out = self._decompiled_dir(target)
        if out.is_dir() and any(out.iterdir()):
            return
        if not self.cfg.decompile_command:
            raise StepFailed(f"Mode 'decompiled' needs analysis.decompile_command in rea-team.toml (target {target})")
        out.mkdir(parents=True, exist_ok=True)
        cmd = [part.replace("{input}", str(self.lab / target)).replace("{output}", str(out))
               for part in shlex.split(self.cfg.decompile_command)]
        log.info("  decompiling %s", target)
        self._retry("decompile", lambda: self.runner.run(cmd, cwd=self.lab, timeout=self.cfg.timeout("analyst"),
                                                         log_name="decompile"))
        if not any(out.iterdir()):
            raise StepFailed(f"Decompiler wrote nothing to {out}")

    def _mode_note(self, target: str, mode: str) -> str:
        if mode == "decompiled":
            return f" Decompiler output is in {self._decompiled_dir(target).relative_to(self.lab)}/."
        if mode == "source":
            return f" The original source code is in {target}/."
        return ""

    # ===== feature steps ======================================================================

    def _step(self, fs: FeatureState, key: str, fn: Callable[[], None]) -> None:
        if fs.is_done(key):
            log.info("  ✓ %s (done earlier)", key)
            return
        log.info("  → %s", key)
        t0 = time.time()
        fs.current = key  # type: ignore[attr-defined]
        fn()
        fs.done.append(key)
        self._save()
        log.info("  ✓ %s (%.0fs)", key, time.time() - t0)

    def step_analyst(self, f: Feature, fs: FeatureState) -> None:
        target = self.lab / f.target
        if not target.exists():
            raise StepFailed(f"Target not found: lab/{f.target}")
        self._decompile_if_needed(f.target, f.mode)
        evidence = self.lab / "analysis" / "features" / f.id / "evidence.md"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        before = checks.hash_tree(target) if self.cfg.check_target_unchanged else None

        def attempt():
            t0 = time.time()
            self._claude_lab(
                f"Use the analyst agent. Feature id: {f.id}. Title: {f.title}. Scope: {f.scope} "
                f"Target: {f.target}. Mode: {f.mode}.{self._mode_note(f.target, f.mode)} "
                f"{'Notes: ' + f.notes + ' ' if f.notes else ''}"
                "Reuse the names in handoff/CONTRACTS.md where they fit. "
                f"Write the evidence to analysis/features/{f.id}/evidence.md.", "analyst", "analyst")
            self._fresh(evidence, "analyst", t0)

        self._retry("analyst", attempt)
        if before and checks.hash_tree(target) != before:
            raise IntegrityError(f"The target changed during analysis: lab/{f.target}",
                                 hint="Restore the target from a clean copy. The analyst must only read it.")
        text = evidence.read_text(errors="replace").lower()
        if "unknown" not in text and "observation" not in text:
            log.warning("  evidence for %s has no observation/unknown tags; the spec may rest on guesses", f.id)

    def _spec_path(self, f: Feature) -> Path:
        return self.lab / "handoff" / "features" / f"{f.id}.md"

    def _check_spec(self, f: Feature) -> None:
        spec = self._spec_path(f)
        missing = checks.missing_spec_sections(spec.read_text(errors="replace"))
        if missing:
            raise TransientError(f"{spec.name} is missing sections: {', '.join(missing)}")
        self._check_contracts()
        checks.assert_spec_size(spec, self.cfg.max_spec_chars)

    def step_spec(self, f: Feature, fs: FeatureState) -> None:
        spec = self._spec_path(f)
        spec.parent.mkdir(parents=True, exist_ok=True)

        def attempt():
            t0 = time.time()
            self._claude_lab(
                f"Use the spec-writer agent to write handoff/features/{f.id}.md from "
                f"analysis/features/{f.id}/evidence.md for the feature '{f.title}' ({f.scope}). Reuse "
                "handoff/CONTRACTS.md and append to it only what this feature adds to the shared contracts.",
                "spec", "spec_writer")
            self._fresh(spec, "spec-writer", t0)
            self._check_spec(f)

        self._retry("spec-writer", attempt)
        self._approve_contracts()

    def step_leak(self, f: Feature, fs: FeatureState, rnd: int) -> None:
        self._leak_free([self.contracts, self._spec_path(f)], f"r{rnd}-leak-fix")
        self._check_contracts()  # a leak fix must not rewrite contracts either
        self._approve_contracts()

    def step_handoff(self, f: Feature, fs: FeatureState, rnd: int) -> None:
        def go():
            self.room.ensure_running()
            self.room.git_init()
            self.room.put_file(self.contracts, "specs/CONTRACTS.md")
            self.room.put_file(self._spec_path(f), f"specs/features/{f.id}.md")
        self._retry("handoff", go)
        fs.hashes["specs"] = self.room.tree_hash("specs")
        self._archive(f.id, rnd, self.contracts, self._spec_path(f))

    def _clean_context(self, f: Feature) -> str:
        return (f"Feature id: {f.id}. Title: {f.title}. Scope: {f.scope} "
                f"Specs: specs/CONTRACTS.md and specs/features/{f.id}.md. Stack: {self.cfg.stack} "
                f"The full test command, run from {self.room.workdir}, is: {self.cfg.test_command}")

    def step_tests(self, f: Feature, fs: FeatureState, rnd: int) -> None:
        mine = f"tests/acceptance/{f.id}"
        others_before = {d: self.room.tree_hash(f"tests/acceptance/{d}")
                         for d in self.room.list_dirs("tests/acceptance") if d != f.id}
        src_before = self.room.tree_hash("src")
        self.room.unlock_tree(mine)
        what = ("Write the acceptance tests" if rnd == 1 else
                "The spec gained new Test Cases. Update the acceptance tests so every Test Case has one")

        def attempt():
            self.room.claude(f"Use the test-writer agent. {what} for this feature in {mine}/. "
                             + self._clean_context(f), timeout=self.cfg.timeout("test_writer"),
                             log_name=f"r{rnd}-test-writer")
            if not self.room.tree_hash(mine):
                raise TransientError(f"test-writer wrote nothing in {mine}/")

        try:
            self._retry("test-writer", attempt)
        except StepFailed as e:
            raise NoAcceptanceTests(str(e)) from e

        for d, h in others_before.items():
            if self.room.tree_hash(f"tests/acceptance/{d}") != h:
                raise IntegrityError(f"The test-writer changed another feature's tests: tests/acceptance/{d}/")
        if self.room.tree_hash("src") != src_before:
            raise IntegrityError("The test-writer changed application code under src/")
        self.room.lock_tree("tests/acceptance")
        fs.hashes["acceptance"] = self.room.tree_hash("tests/acceptance")

    def _assert_locked_files_intact(self, fs: FeatureState, who: str) -> None:
        if self.room.tree_hash("tests/acceptance") != fs.hashes.get("acceptance"):
            raise IntegrityError(f"The {who} changed tests under tests/acceptance/. Tests are read-only for it.")
        if self.room.tree_hash("specs") != fs.hashes.get("specs"):
            raise IntegrityError(f"The {who} changed files under specs/.")

    def step_build(self, f: Feature, fs: FeatureState, rnd: int) -> None:
        def session(prompt: str, name: str):
            self._retry(name, lambda: self.room.claude(prompt, timeout=self.cfg.timeout("rebuilder"),
                                                       log_name=f"r{rnd}-{name}"))
            self._assert_locked_files_intact(fs, "rebuilder")

        intro = ("Implement this feature" if rnd == 1 else
                 "The spec gained new Test Cases after verification. Update the implementation")
        session(f"Use the rebuilder agent. {intro}. " + self._clean_context(f), "rebuilder")

        for attempt in range(self.cfg.fix_attempts + 1):
            res = self._retry("test gate", lambda: self.room.run_tests(log_name=f"r{rnd}-gate"))
            output = (res.stdout + "\n" + res.stderr).strip()
            fs.gate.append({"round": rnd, "attempt": attempt, "exit": res.returncode, "at": time.time()})
            self._save()
            d = self._archive_dir(f.id, rnd)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"gate-{attempt}.txt").write_text(output)
            if res.returncode == 0:
                log.info("    gate passed (unit + all acceptance tests)")
                break
            if attempt == self.cfg.fix_attempts:
                raise GateFailed(f"Test gate still failing after {self.cfg.fix_attempts} fix session(s) "
                                 f"for {f.id}. Last output: {d / f'gate-{attempt}.txt'}")
            tail = "\n".join(output.splitlines()[-120:])
            log.warning("    gate failed (exit %d), fix session %d/%d", res.returncode,
                        attempt + 1, self.cfg.fix_attempts)
            session("Use the rebuilder agent. The full test command fails. Fix the code under src/ so every "
                    "test passes, including earlier features' acceptance tests (regressions). Do not edit "
                    f"tests or specs. {self._clean_context(f)}\n\nLast lines of the test output:\n{tail}",
                    f"fix-{attempt + 1}")

        if not self.room.exists("BUILD_STATE.md", "f"):
            log.warning("    rebuilder did not write BUILD_STATE.md; the next session starts with less context")

    def step_verify(self, f: Feature, fs: FeatureState, rnd: int) -> None:
        rebuild = self.lab / "rebuild"
        rebuild.mkdir(exist_ok=True)
        self._retry("fetch rebuild", lambda: (self.room.get_dir("src", rebuild / "src"),
                                              self.room.get_dir("tests", rebuild / "tests")))
        arch = self._archive_dir(f.id, rnd)
        for name in ("BUILD_STATE.md", "QUESTIONS.md"):
            self.room.get_file(name, arch / name)

        report = self.lab / "analysis" / "features" / f.id / "report.md"
        report.unlink(missing_ok=True)
        spec = self._spec_path(f)
        def rebuild_hash() -> str:  # only what came from the clean room; ignore generated folders
            return "".join(checks.hash_tree(rebuild / d, checks.GENERATED_DIRS) for d in ("src", "tests"))

        rebuild_before = rebuild_hash()
        spec_before = checks.hash_tree(spec)

        def attempt():
            t0 = time.time()
            self._claude_lab(
                f"Use the verifier agent. Feature id: {f.id} ({f.title}). Spec: handoff/features/{f.id}.md "
                f"and handoff/CONTRACTS.md. Original: {self.cfg.verify_original} Target: {f.target}. "
                f"Rebuild: {self.cfg.verify_rebuild} Acceptance tests: rebuild/tests/acceptance/{f.id}/. "
                f"Write the report to analysis/features/{f.id}/report.md.", f"r{rnd}-verifier", "verifier")
            self._fresh(report, "verifier", t0)
            try:
                return checks.parse_report(report.read_text(errors="replace"))
            except ReaTeamError as e:
                raise TransientError(str(e)) from e

        result = self._retry("verifier", attempt)
        if rebuild_hash() != rebuild_before:
            raise IntegrityError("The verifier modified lab/rebuild/. It must only report.")
        if checks.hash_tree(spec) != spec_before:
            raise IntegrityError("The verifier modified the feature spec. It must only report.")
        self._archive(f.id, rnd, report)
        fs.results = [r for r in fs.results if r["round"] != rnd]
        fs.results.append({"round": rnd, "wrong": result.wrong, "crash": result.crash,
                           "spec_wrong": result.spec_wrong, "groups": result.groups})
        log.info("    verify: wrong=%d crash=%d spec_wrong=%d", result.wrong, result.crash, result.spec_wrong)

    def step_spec_update(self, f: Feature, fs: FeatureState, rnd: int) -> None:
        spec = self._spec_path(f)

        def attempt():
            t0 = time.time()
            self._claude_lab(
                f"Use the spec-writer agent to update handoff/features/{f.id}.md from "
                f"analysis/features/{f.id}/report.md: add every wrong or crash divergence as a new Test Case "
                "and correct whatever spec_wrong items prove wrong.", f"r{rnd}-spec-update", "spec_writer")
            self._fresh(spec, "spec-writer (update)", t0)
            self._check_spec(f)

        self._retry("spec-writer (update)", attempt)
        self._approve_contracts()

    def step_commit(self, f: Feature, fs: FeatureState) -> None:
        self.room.unlock_tree("tests/acceptance")  # so later rollbacks/cleanup can work
        fs.commit = self.room.git_commit(f.id, f.title)
        self.room.lock_tree("tests/acceptance")
        self.room.lock_tree("specs")

    # ===== one feature ===========================================================================

    def _discard_other_unfinished(self, current: str) -> None:
        """Another feature stopped mid-way in the clean room: don't commit its leftovers as ours."""
        for other in self.state.features.values():
            if other.id == current or other.status not in (RUNNING, INTERRUPTED):
                continue
            if any(k.startswith("r") and "." in k for k in other.done):
                head = self.room.git_rollback()
                log.warning("  %s stopped mid-build earlier; rolled the clean room back to %s first", other.id, head)
                other.done = [k for k in other.done if k in ("analyst", "spec")]
            other.status = PENDING
            self._save()

    def build_feature(self, f: Feature) -> None:
        fs = self.state.feature(f.id)
        self._discard_other_unfinished(f.id)
        self._logs(f.id)
        fs.status = RUNNING
        fs.error = None
        fs.started_at = fs.started_at or time.time()
        self._save()
        log.info("━━ feature %s: %s", f.id, f.title)

        self._step(fs, "analyst", lambda: self.step_analyst(f, fs))
        self._step(fs, "spec", lambda: self.step_spec(f, fs))

        rnd = fs.round
        while True:
            fs.round = rnd
            self._save()
            for name, fn in [("leak", self.step_leak), ("handoff", self.step_handoff),
                             ("wall", lambda *_: self.room.assert_wall()), ("tests", self.step_tests),
                             ("build", self.step_build), ("verify", self.step_verify)]:
                self._step(fs, f"r{rnd}.{name}", lambda fn=fn: fn(f, fs, rnd))

            res = next((r for r in fs.results if r["round"] == rnd), None)
            if res and res["wrong"] == 0 and res["crash"] == 0 and res.get("spec_wrong", 0) == 0:
                break
            if rnd >= self.cfg.max_rounds:
                # Teach the spec what this round found, so the next attempt starts smarter.
                self._step(fs, f"r{rnd}.spec_update", lambda: self.step_spec_update(f, fs, rnd))
                fs.round = rnd + 1
                self._save()
                raise ParityNotReached(
                    f"{f.id}: no parity after {rnd} round(s): wrong={res and res['wrong']} "
                    f"crash={res and res['crash']} spec_wrong={res and res.get('spec_wrong', 0)}",
                    hint=f"Read the report and the evidence unknowns, then `rea-team run --feature {f.id} "
                         "--extra-rounds 2`, or split the feature.")
            self._step(fs, f"r{rnd}.spec_update", lambda: self.step_spec_update(f, fs, rnd))
            rnd += 1

        self._step(fs, "commit", lambda: self.step_commit(f, fs))
        fs.status = PASSED
        fs.finished_at = time.time()
        self._save()
        log.info("━━ %s PASSED (commit %s)", f.id, fs.commit)

    # ===== the project =============================================================================

    def _fail_feature(self, f: Feature, fs: FeatureState, e: BaseException) -> None:
        fs.status = FAILED
        fs.error = {"step": getattr(fs, "current", "?"), "type": type(e).__name__, "message": str(e),
                    "hint": getattr(e, "hint", "") or "Unexpected error; re-run with --verbose."}
        self._save()
        if isinstance(e, (WallBreach, ContainerError)):
            return  # can't (or mustn't) touch the container
        try:
            head = self.room.git_rollback()
            log.warning("  clean room rolled back to %s (last passed feature)", head)
            # clean-room steps must be redone; lab-side analysis and spec are kept
            fs.done = [k for k in fs.done if k in ("analyst", "spec")]
            fs.error["rolled_back_to"] = head
            self._save()
        except ReaTeamError as re_err:
            log.error("  rollback failed: %s", re_err)
            fs.error["rollback_error"] = str(re_err)
            self._save()

    def run(self, feats: list[Feature], only: list[str] | None = None) -> dict[str, str]:
        by_id = {f.id: f for f in feats}
        if only:
            unknown = [i for i in only if i not in by_id]
            if unknown:
                raise ReaTeamError(f"Unknown feature id(s): {', '.join(unknown)}")
            wanted = set(only)
            for i in only:  # pull in unfinished dependencies
                stack = list(by_id[i].depends_on)
                while stack:
                    d = stack.pop()
                    if d not in wanted:
                        wanted.add(d)
                        stack.extend(by_id[d].depends_on)
            feats = [f for f in feats if f.id in wanted]

        if not self.contracts.is_file():
            raise StepFailed("handoff/CONTRACTS.md is missing", hint="Run `rea-team plan` first.")

        outcome: dict[str, str] = {}
        first_error: ReaTeamError | None = None
        for f in feats:
            fs = self.state.feature(f.id)
            if fs.status == PASSED:
                outcome[f.id] = PASSED
                continue
            bad_deps = [d for d in f.depends_on if self.state.feature(d).status != PASSED]
            if bad_deps:
                fs.status = BLOCKED
                fs.error = {"step": "-", "type": "Blocked", "message": f"waiting on {', '.join(bad_deps)}",
                            "hint": "Fix the dependency first."}
                self._save()
                outcome[f.id] = BLOCKED
                continue
            try:
                self.build_feature(f)
                outcome[f.id] = PASSED
            except KeyboardInterrupt:
                fs.status = INTERRUPTED
                self._save()
                raise
            except Exception as e:
                self._fail_feature(f, fs, e)
                outcome[f.id] = FAILED
                err = e if isinstance(e, ReaTeamError) else StepFailed(f"{type(e).__name__}: {e}")
                if first_error is None:
                    first_error = err
                if isinstance(e, FATAL) or not self.cfg.keep_going:
                    log.error("stopping: %s", e)
                    raise err from e
                log.error("feature %s failed (%s); keep_going: skipping its dependents", f.id, type(e).__name__)
                for dep in featmod.dependents(feats, f.id):
                    if self.state.feature(dep).status != PASSED:
                        self.state.feature(dep).status = BLOCKED
                self._save()
        if first_error:
            raise first_error
        return outcome
