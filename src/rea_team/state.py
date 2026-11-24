"""Checkpointed run state, saved after every step so a run can resume."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import LockHeld, ReaTeamError

STATUS_RUNNING = "running"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_PASSED = "passed"
STATUS_NO_PARITY = "no_parity"


@dataclass
class RunState:
    run_id: str
    target: str
    feature: str
    authorized_at: str
    status: str = STATUS_RUNNING
    round: int = 1
    done: list[str] = field(default_factory=list)      # completed step keys
    hashes: dict[str, str] = field(default_factory=dict)
    results: list[dict] = field(default_factory=list)  # one entry per verify round
    error: dict | None = None
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # --- persistence -------------------------------------------------------

    @staticmethod
    def new_id() -> str:
        return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)

    def path(self, state_dir: Path) -> Path:
        return state_dir / "runs" / self.run_id / "state.json"

    def save(self, state_dir: Path) -> None:
        self.updated_at = time.time()
        p = self.path(state_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, p)  # atomic: a crash never leaves half a state file

    @classmethod
    def load(cls, state_dir: Path, run_id: str) -> "RunState":
        p = state_dir / "runs" / run_id / "state.json"
        if not p.is_file():
            known = ", ".join(list_runs(state_dir)) or "none"
            raise ReaTeamError(f"No run {run_id!r}. Known runs: {known}")
        try:
            return cls(**json.loads(p.read_text()))
        except (json.JSONDecodeError, TypeError) as e:
            raise ReaTeamError(f"State file {p} is corrupt: {e}") from e

    def is_done(self, key: str) -> bool:
        return key in self.done

    def mark_done(self, key: str, state_dir: Path) -> None:
        if key not in self.done:
            self.done.append(key)
        self.save(state_dir)


def list_runs(state_dir: Path) -> list[str]:
    d = state_dir / "runs"
    return sorted(p.name for p in d.iterdir() if (p / "state.json").is_file()) if d.is_dir() else []


class WorkspaceLock:
    """Exclusive lock so two runs never share lab/ and the container."""

    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "lock"
        self._fh = None

    def __enter__(self):
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fh.seek(0)
            holder = self._fh.read().strip() or "unknown"
            self._fh.close()
            raise LockHeld(f"Workspace is locked by {holder}")
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"pid {os.getpid()}")
        self._fh.flush()
        return self

    def __exit__(self, *exc):
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
        return False
