"""Checkpointed project state, saved after every step so any run can resume."""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import LockHeld, ReaTeamError

# feature statuses
PENDING = "pending"
RUNNING = "running"
PASSED = "passed"
FAILED = "failed"
BLOCKED = "blocked"          # a dependency failed
INTERRUPTED = "interrupted"


@dataclass
class FeatureState:
    id: str
    status: str = PENDING
    round: int = 1
    done: list[str] = field(default_factory=list)   # completed step keys
    hashes: dict[str, str] = field(default_factory=dict)
    results: list[dict] = field(default_factory=list)
    gate: list[dict] = field(default_factory=list)  # one entry per gate run
    commit: str = ""
    error: dict | None = None
    started_at: float = 0
    finished_at: float = 0

    def is_done(self, key: str) -> bool:
        return key in self.done

    def reset(self, from_step: str | None = None) -> None:
        if from_step is None:
            self.__init__(self.id)  # type: ignore[misc]
            return
        if from_step not in self.done:
            raise ReaTeamError(f"{from_step!r} is not a completed step of {self.id}. "
                               f"Done: {', '.join(self.done) or 'nothing'}")
        self.done = self.done[: self.done.index(from_step)]
        if from_step.startswith("r") and "." in from_step:
            self.round = int(from_step[1:].split(".")[0])
            self.results = [r for r in self.results if r["round"] < self.round]
        self.status = PENDING
        self.error = None
        self.commit = ""


@dataclass
class ProjectState:
    authorized_at: str = ""
    planned: list[str] = field(default_factory=list)   # completed plan steps
    features: dict[str, FeatureState] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    @staticmethod
    def path(state_dir: Path) -> Path:
        return state_dir / "project.json"

    def feature(self, fid: str) -> FeatureState:
        if fid not in self.features:
            self.features[fid] = FeatureState(fid)
        return self.features[fid]

    def save(self, state_dir: Path) -> None:
        self.updated_at = time.time()
        p = self.path(state_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, p)  # atomic: a crash never leaves half a state file

    @classmethod
    def load(cls, state_dir: Path) -> "ProjectState":
        p = cls.path(state_dir)
        if not p.is_file():
            return cls()
        try:
            raw = json.loads(p.read_text())
            feats = {k: FeatureState(**v) for k, v in raw.pop("features", {}).items()}
            return cls(**raw, features=feats)
        except (json.JSONDecodeError, TypeError) as e:
            raise ReaTeamError(f"State file {p} is corrupt: {e}",
                               hint="Restore it from lab/.rea-team/project.json.bak, or delete it to start over.") from e

    def backup(self, state_dir: Path) -> None:
        p = self.path(state_dir)
        if p.is_file():
            (state_dir / "project.json.bak").write_text(p.read_text())


class WorkspaceLock:
    """Exclusive lock so two runs never share lab/ and the container.

    The OS releases it automatically if the process dies, so it never goes stale.
    """

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
