"""Subprocess execution with timeouts, logs and retries."""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence, TypeVar

from .errors import StepFailed, TransientError

log = logging.getLogger("rea_team")
T = TypeVar("T")


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


class Runner:
    """Runs commands. Tests swap this for a fake."""

    def __init__(self, log_dir: Path | None = None):
        self.log_dir = log_dir

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = None,
        log_name: str | None = None,
        check: bool = True,
        stdin: str | None = None,
    ) -> Result:
        printable = shlex.join(cmd)
        log.debug("run: %s (cwd=%s)", printable, cwd)
        try:
            proc = subprocess.Popen(
                list(cmd),
                cwd=cwd,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,  # own process group, so timeouts kill children too
            )
        except FileNotFoundError as e:
            raise StepFailed(f"Command not found: {cmd[0]}", hint="Install it or fix its path in rea-team.toml.") from e
        except PermissionError as e:
            raise StepFailed(f"Not executable: {cmd[0]}") from e

        try:
            out, err = proc.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            out, err = proc.communicate()
            self._write_log(log_name, printable, out, err, f"TIMEOUT after {timeout}s")
            raise TransientError(f"Timed out after {timeout}s: {printable}")
        except KeyboardInterrupt:
            self._kill(proc)
            raise

        res = Result(proc.returncode, out or "", err or "")
        self._write_log(log_name, printable, res.stdout, res.stderr, f"exit {res.returncode}")
        if check and res.returncode != 0:
            tail = (res.stderr or res.stdout).strip().splitlines()[-5:]
            raise TransientError(f"Exit {res.returncode}: {printable}\n  " + "\n  ".join(tail))
        return res

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _write_log(self, name, cmd, out, err, status) -> None:
        if not (self.log_dir and name):
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with open(self.log_dir / f"{name}.log", "a") as f:
            f.write(f"\n===== {time.strftime('%H:%M:%S')} $ {cmd}\n----- {status}\n")
            f.write(f"--- stdout\n{out or ''}\n--- stderr\n{err or ''}\n")


def with_retries(
    fn: Callable[[], T],
    *,
    what: str,
    retries: int,
    backoff: float,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call fn, retrying TransientError with exponential backoff.

    Any other exception (leak, wall breach, integrity) is never retried.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except TransientError as e:
            if attempt >= retries:
                raise StepFailed(f"{what} failed after {attempt + 1} attempt(s): {e}") from e
            delay = backoff * (2 ** attempt)
            log.warning("%s failed (attempt %d/%d): %s; retrying in %.0fs",
                        what, attempt + 1, retries + 1, str(e).splitlines()[0], delay)
            sleep(delay)
            attempt += 1
