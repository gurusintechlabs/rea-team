"""The four-agent clean-room pipeline, with checkpoints after every step."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Callable

from . import checks
from .config import Config
from .container import CleanRoom
from .errors import (IntegrityError, LeakDetected, ParityNotReached, ReaTeamError,
                     TransientError)
from .runner import Runner, with_retries
from .state import (STATUS_FAILED, STATUS_NO_PARITY, STATUS_PASSED, STATUS_RUNNING,
                    RunState)

log = logging.getLogger("rea_team")

SPEC_REL = "handoff/SPEC.md"
EVIDENCE_REL = "analysis/evidence.md"
REPORT_REL = "report.md"
REBUILD_REL = "rebuild"


class Pipeline:
    def __init__(self, cfg: Config, state: RunState, runner: Runner | None = None,
                 room: CleanRoom | None = None, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.state = state
        self.run_dir = state.path(cfg.state_dir).parent
        self.runner = runner or Runner(self.run_dir / "logs")
        if self.runner.log_dir is None:
            self.runner.log_dir = self.run_dir / "logs"
        self.room = room or CleanRoom(cfg, self.runner)
        self.sleep = sleep
        lab = cfg.lab_dir
        self.target = lab / state.target
        self.evidence = lab / EVIDENCE_REL
        self.spec = lab / SPEC_REL
        self.report = lab / REPORT_REL
        self.rebuild = lab / REBUILD_REL

    # --- helpers ---------------------------------------------------------------

    def _save(self):
        self.state.save(self.cfg.state_dir)

    def _retry(self, what: str, fn):
        return with_retries(fn, what=what, retries=self.cfg.retries,
                            backoff=self.cfg.retry_backoff_seconds, sleep=self.sleep)

    def _claude_lab(self, prompt: str, step: str, timeout_key: str):
        cmd = [self.cfg.claude_command, "-p", prompt, *self.cfg.claude_args]
        return self.runner.run(cmd, cwd=self.cfg.lab_dir, timeout=self.cfg.timeout(timeout_key), log_name=step)

    def _step(self, key: str, fn: Callable[[], None]) -> None:
        if self.state.is_done(key):
            log.info("✓ %s (done earlier, skipping)", key)
            return
        log.info("→ %s", key)
        t0 = time.time()
        fn()
        self.state.mark_done(key, self.cfg.state_dir)
        log.info("✓ %s (%.0fs)", key, time.time() - t0)

    def _archive(self, rnd: int, *paths: Path) -> None:
        d = self.run_dir / f"round-{rnd}"
        d.mkdir(parents=True, exist_ok=True)
        for p in paths:
            if p.is_file():
                shutil.copy2(p, d / p.name)

    # --- steps -----------------------------------------------------------------

    def analyst(self) -> None:
        self.evidence.parent.mkdir(parents=True, exist_ok=True)
        before = checks.hash_tree(self.target) if self.cfg.check_target_unchanged else None

        def attempt():
            t0 = time.time()
            self._claude_lab(f"Use the analyst agent on {self.state.target}. Feature: {self.state.feature}",
                             "analyst", "analyst")
            try:
                checks.require_file(self.evidence, "analyst", newer_than=t0)
            except ReaTeamError as e:
                raise TransientError(str(e)) from e

        self._retry("analyst", attempt)
        if before and checks.hash_tree(self.target) != before:
            raise IntegrityError(f"The target changed during analysis: {self.target}",
                                 hint="Restore the target from a clean copy. The analyst must only read it.")
        text = self.evidence.read_text(errors="replace").lower()
        if "unknown" not in text and "observation" not in text:
            log.warning("evidence.md has no observation/unknown tags; the spec may be built on guesses")

    def spec_writer(self) -> None:
        self.spec.parent.mkdir(parents=True, exist_ok=True)

        def attempt():
            t0 = time.time()
            self._claude_lab(f"Use the spec-writer agent to write {SPEC_REL} from {EVIDENCE_REL}",
                             "spec-writer", "spec_writer")
            try:
                checks.require_file(self.spec, "spec-writer", newer_than=t0)
            except ReaTeamError as e:
                raise TransientError(str(e)) from e
            missing = checks.missing_spec_sections(self.spec.read_text(errors="replace"))
            if missing:
                raise TransientError(f"SPEC.md is missing sections: {', '.join(missing)}")

        self._retry("spec-writer", attempt)

    def leak_check(self, rnd: int) -> None:
        """Scan SPEC.md; let the spec-writer clean it up, then fail hard if it still leaks."""
        for fix in range(self.cfg.retries + 1):
            try:
                checks.assert_no_leaks(self.spec, self.cfg.leak_patterns)
                return
            except LeakDetected as e:
                if fix == self.cfg.retries:
                    raise
                log.warning("leak found, asking spec-writer to fix (%d/%d)", fix + 1, self.cfg.retries)
                prompt = ("Use the spec-writer agent. handoff/SPEC.md leaks implementation details "
                          "from the binary on these lines. Rewrite them as behavior only, with no "
                          "addresses, offsets or decompiled names, and change nothing else.\n" + str(e))
                self._retry("spec-writer (leak fix)",
                            lambda: self._claude_lab(prompt, f"r{rnd}-leak-fix", "spec_writer"))

    def handoff(self, rnd: int) -> None:
        self._retry("handoff", lambda: (self.room.ensure_running(), self.room.put_spec(self.spec)))
        self.state.hashes[f"r{rnd}.spec"] = checks.hash_tree(self.spec)
        self._archive(rnd, self.spec)

    def rebuild_step(self, rnd: int) -> None:
        def attempt():
            self.room.ensure_running()
            self.room.exec(self.cfg.container_claude_command, "-p",
                           f"Use the rebuilder agent on {self.room.workdir}/SPEC.md",
                           *self.cfg.container_claude_args,
                           timeout=self.cfg.timeout("rebuilder"), log_name=f"r{rnd}-rebuilder")
            self.room.get_src(self.rebuild)

        self._retry("rebuilder", attempt)
        self.room.get_optional("QUESTIONS.md", self.run_dir / f"round-{rnd}-QUESTIONS.md")

    def verify(self, rnd: int) -> None:
        spec_before = checks.hash_tree(self.spec)
        rebuild_before = checks.hash_tree(self.rebuild)
        self.report.unlink(missing_ok=True)

        def attempt():
            t0 = time.time()
            self._claude_lab("Use the verifier agent.", f"r{rnd}-verifier", "verifier")
            try:
                checks.require_file(self.report, "verifier", newer_than=t0)
                return checks.parse_report(self.report.read_text(errors="replace"))
            except ReaTeamError as e:
                raise TransientError(str(e)) from e

        result = self._retry("verifier", attempt)
        if checks.hash_tree(self.rebuild) != rebuild_before:
            raise IntegrityError("The verifier modified rebuild/. It must only report.")
        if checks.hash_tree(self.spec) != spec_before:
            raise IntegrityError("The verifier modified SPEC.md. It must only report.")
        self._archive(rnd, self.report)
        self.state.results = [r for r in self.state.results if r["round"] != rnd]
        self.state.results.append({"round": rnd, "wrong": result.wrong, "crash": result.crash,
                                   "groups": result.groups})
        log.info("round %d: wrong=%d crash=%d", rnd, result.wrong, result.crash)

    def spec_update(self, rnd: int) -> None:
        def attempt():
            t0 = time.time()
            self._claude_lab("Use the spec-writer agent to add every wrong or crash divergence in "
                             f"{REPORT_REL} as a case under Test Cases in {SPEC_REL}",
                             f"r{rnd}-spec-update", "spec_writer")
            try:
                checks.require_file(self.spec, "spec-writer (update)", newer_than=t0)
            except ReaTeamError as e:
                raise TransientError(str(e)) from e
            missing = checks.missing_spec_sections(self.spec.read_text(errors="replace"))
            if missing:
                raise TransientError(f"Updated SPEC.md lost sections: {', '.join(missing)}")

        self._retry("spec-writer (update)", attempt)

    # --- driver ----------------------------------------------------------------

    def _round_result(self, rnd: int) -> dict | None:
        return next((r for r in self.state.results if r["round"] == rnd), None)

    def run(self) -> RunState:
        st = self.state
        st.status = STATUS_RUNNING
        st.error = None
        self._save()
        current = "start"
        try:
            current = "analyst"
            self._step("analyst", self.analyst)
            current = "spec_writer"
            self._step("spec_writer", self.spec_writer)

            for rnd in range(st.round, self.cfg.max_rounds + 1):
                st.round = rnd
                self._save()
                for name, fn in [("leak_check", self.leak_check), ("handoff", self.handoff),
                                 ("wall_check", lambda r: self.room.assert_wall()),
                                 ("rebuild", self.rebuild_step), ("verify", self.verify)]:
                    current = f"r{rnd}.{name}"
                    self._step(current, lambda fn=fn: fn(rnd))

                res = self._round_result(rnd)
                if res and res["wrong"] == 0 and res["crash"] == 0:
                    st.status = STATUS_PASSED
                    self._save()
                    return st
                if rnd == self.cfg.max_rounds:
                    break
                current = f"r{rnd}.spec_update"
                self._step(current, lambda: self.spec_update(rnd))

            st.status = STATUS_NO_PARITY
            self._save()
            last = self.state.results[-1] if self.state.results else {}
            raise ParityNotReached(
                f"No parity after {self.cfg.max_rounds} round(s): "
                f"wrong={last.get('wrong', '?')} crash={last.get('crash', '?')}")
        except ParityNotReached:
            raise
        except ReaTeamError as e:
            st.status = STATUS_FAILED
            st.error = {"step": current, "type": type(e).__name__, "message": str(e), "hint": e.hint}
            self._save()
            raise
        except Exception as e:  # bug or OS error: still leave a resumable checkpoint
            st.status = STATUS_FAILED
            st.error = {"step": current, "type": type(e).__name__, "message": str(e),
                        "hint": "Unexpected error; see the traceback with --verbose."}
            self._save()
            raise
