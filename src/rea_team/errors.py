"""Exception hierarchy.

Every failure the pipeline can hit maps to one of these classes. Each class
carries an exit code and a ``hint`` telling the user what to do next, so the
CLI can stop with a clear reason instead of a traceback.

Retryable failures (``TransientError``) are retried with backoff by the
runner. Everything else stops the run and leaves a checkpoint behind.
"""

from __future__ import annotations


class ReaTeamError(Exception):
    exit_code = 1
    hint = ""

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        if hint is not None:
            self.hint = hint


# --- setup -----------------------------------------------------------------

class ConfigError(ReaTeamError):
    exit_code = 10
    hint = "Fix rea-team.toml (see README, 'Configuration')."


class PreflightError(ReaTeamError):
    exit_code = 11
    hint = "Run `rea-team doctor` to see every failing check."


class AuthorizationError(ReaTeamError):
    exit_code = 12
    hint = (
        "Only run this on software you wrote or are authorized to examine, "
        "and check its license. Then pass --authorized."
    )


class LockHeld(ReaTeamError):
    exit_code = 13
    hint = "Another rea-team run is using this workspace. Wait for it to finish (the lock frees itself if that process dies)."


# --- execution -------------------------------------------------------------

class TransientError(ReaTeamError):
    """A failure worth retrying: timeout, non-zero exit, docker hiccup."""
    exit_code = 20


class StepFailed(ReaTeamError):
    """A step still failed after all retries, or produced no usable output."""
    exit_code = 21
    hint = "Read the step log, fix the cause, then `rea-team resume <run-id>`."


class MissingArtifact(StepFailed):
    """A step exited 0 but did not write the file the next step needs."""


class ContainerError(ReaTeamError):
    exit_code = 22
    hint = "Check the clean container is running: `docker ps` and the container name in rea-team.toml."


# --- safety walls ----------------------------------------------------------

class LeakDetected(ReaTeamError):
    """SPEC.md contains addresses or decompiled names. Never crosses the wall."""
    exit_code = 30
    hint = "Remove the listed lines from handoff/SPEC.md (or re-run the spec-writer), then resume."


class WallBreach(ReaTeamError):
    """The clean container can reach REA or the lab. Rebuild is not clean-room."""
    exit_code = 31
    hint = "Rebuild the clean container without REA, without lab mounts, and with npm off its allowlist."


class IntegrityError(ReaTeamError):
    """An agent changed something it must never change (target, rebuild, spec)."""
    exit_code = 32
    hint = "Inspect the diff in the step log. Restore the original before resuming."


# --- outcome ---------------------------------------------------------------

class ReportFormatError(StepFailed):
    hint = "report.md must end with `RESULT wrong=<n> crash=<n>`. Check the verifier prompt, then resume."


class ParityNotReached(ReaTeamError):
    exit_code = 2
    hint = "Read report.md and evidence.md unknowns: a gap that survives every round is usually missing evidence."


# --- feature pipeline ----------------------------------------------------------

class FeatureGraphError(ConfigError):
    hint = "Fix features.toml: unique ids, known depends_on, no cycles (see USER_GUIDE, 'features.toml')."


class SpecTooLarge(StepFailed):
    hint = ("Split this feature into smaller features in features.toml (or raise "
            "project.max_spec_chars), then `rea-team reset <id>` and run again.")


class ContractViolation(IntegrityError):
    """CONTRACTS.md may only grow: earlier text must stay exactly as it was."""
    hint = ("CONTRACTS.md was rewritten instead of extended. Restore it from the archive in "
            "lab/.rea-team/contracts/, then resume. Breaking contract changes need a deliberate re-plan.")


class GateFailed(StepFailed):
    """The test suite (unit + every feature's acceptance tests) still fails after fix attempts."""
    hint = ("Read the gate log. A regression in an older feature means this feature broke it. "
            "The clean room was rolled back to the last passing feature.")


class NoAcceptanceTests(StepFailed):
    hint = "The test-writer produced no acceptance tests. Check its log and the [project].stack setting."
