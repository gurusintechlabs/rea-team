"""The clean room: a Docker container that never sees REA or the lab.

Layout inside the container (workdir, default /work):

    specs/CONTRACTS.md          read-only, shared by every feature
    specs/features/<id>.md      read-only, one per feature
    tests/acceptance/<id>/      read-only after the test-writer, one per feature
    src/                        the rebuild (code + its unit tests)
    BUILD_STATE.md              the handoff note between sessions
    .git                        one commit per passed feature, tag rea/<id>
"""

from __future__ import annotations

import logging
import shlex
import shutil
from pathlib import Path

from .config import Config
from .errors import ContainerError, StepFailed, TransientError, WallBreach
from .runner import Result, Runner

log = logging.getLogger("rea_team")

GIT = ["git", "-c", "user.name=rea-team", "-c", "user.email=rea-team@localhost"]
DEFAULT_GITIGNORE = "node_modules/\n__pycache__/\n.venv/\ndist/\nbuild/\ncoverage/\ntest-results/\nplaywright-report/\n*.log\n"


class CleanRoom:
    def __init__(self, cfg: Config, runner: Runner):
        self.cfg = cfg
        self.runner = runner
        self.name = cfg.container_name
        self.workdir = cfg.container_workdir.rstrip("/")

    # --- low level ---------------------------------------------------------------

    def _docker(self, *args: str, check: bool = True, timeout: float | None = None, log_name="docker"):
        return self.runner.run(["docker", *args], check=check,
                               timeout=timeout or self.cfg.timeout("docker"), log_name=log_name)

    def exec(self, *cmd: str, check: bool = True, timeout: float | None = None, log_name="docker") -> Result:
        return self._docker("exec", "-w", self.workdir, self.name, *cmd,
                            check=check, timeout=timeout, log_name=log_name)

    def sh(self, script: str, *, check: bool = True, timeout: float | None = None, log_name="docker") -> Result:
        return self.exec("sh", "-c", script, check=check, timeout=timeout, log_name=log_name)

    def path(self, rel: str) -> str:
        return f"{self.workdir}/{rel.lstrip('/')}"

    # --- health ------------------------------------------------------------------

    def ensure_running(self) -> None:
        try:
            r = self._docker("inspect", "-f", "{{.State.Running}}", self.name, check=False)
        except StepFailed as e:
            raise ContainerError(f"Docker is not available: {e}", hint="Start Docker Desktop, then retry.") from e
        except TransientError as e:
            raise ContainerError(f"Docker did not answer: {e}", hint="Restart Docker Desktop.") from e
        if r.returncode != 0:
            raise ContainerError(f"Container {self.name!r} does not exist.")
        if r.stdout.strip() != "true":
            raise ContainerError(f"Container {self.name!r} exists but is not running.",
                                 hint=f"Start it: docker start {self.name}")

    def check_wall(self) -> list[str]:
        """Return a list of breaches. Every probe here is expected to FAIL."""
        probes: list[tuple[str, list[str]]] = [
            ("`rea` is on PATH", ["sh", "-c", "command -v rea"]),
            ("rea-agents runs from npx cache", ["npx", "--no-install", "rea-agents", "--version"]),
            ("npm registry is reachable (could pull rea-agents back in)", ["npm", "view", "rea-agents", "version"]),
        ]
        probes += [(f"{p} exists", ["test", "-e", p]) for p in self.cfg.forbidden_paths]
        breaches = []
        for desc, cmd in probes:
            try:
                r = self.exec(*cmd, check=False, timeout=30, log_name="wall-check")
            except (TransientError, StepFailed):
                continue  # hung or missing = blocked = good
            if r.returncode == 0:
                breaches.append(desc)
        return breaches

    def assert_wall(self) -> None:
        breaches = self.check_wall()
        if breaches:
            raise WallBreach("Clean container is not isolated:\n  - " + "\n  - ".join(breaches))

    def check_tool(self, *cmd: str) -> bool:
        try:
            return self.exec(*cmd, check=False, log_name="preflight").returncode == 0
        except (TransientError, StepFailed):
            return False

    # --- files ---------------------------------------------------------------------

    def put_file(self, src: Path, rel: str, *, readonly: bool = True) -> None:
        dest = self.path(rel)
        parent = dest.rsplit("/", 1)[0]
        self.exec("mkdir", "-p", parent)
        self.exec("rm", "-f", dest)
        self._docker("cp", str(src), f"{self.name}:{dest}")
        if readonly:
            self.exec("chmod", "a-w", dest)

    def exists(self, rel: str, kind: str = "e") -> bool:
        return self.exec("test", f"-{kind}", self.path(rel), check=False).returncode == 0

    def read_text(self, rel: str) -> str:
        r = self.exec("cat", self.path(rel), check=False)
        return r.stdout if r.returncode == 0 else ""

    def lock_tree(self, rel: str) -> None:
        """Make every file under rel read-only (dirs stay writable so git can roll back)."""
        self.sh(f"[ -e {shlex.quote(rel)} ] && find {shlex.quote(rel)} -type f -exec chmod a-w {{}} + || true")

    def unlock_tree(self, rel: str) -> None:
        self.sh(f"[ -e {shlex.quote(rel)} ] && chmod -R u+w {shlex.quote(rel)} || true")

    def tree_hash(self, rel: str) -> str:
        """Hash of every file under rel (path + content). Empty string if rel is missing."""
        q = shlex.quote(rel)
        r = self.sh(f"[ -e {q} ] || exit 0; find {q} -type f -print0 | sort -z | xargs -0 -r sha256sum | sha256sum",
                    log_name="hash")
        return r.stdout.split()[0] if r.stdout.strip() else ""

    def list_dirs(self, rel: str) -> list[str]:
        r = self.sh(f"[ -d {shlex.quote(rel)} ] && ls -1 {shlex.quote(rel)} || true", log_name="hash")
        return sorted(x for x in r.stdout.split() if x)

    def get_dir(self, rel: str, dest: Path, *, required: bool = True) -> bool:
        if not self.exists(rel, "d"):
            if required:
                raise StepFailed(f"Expected folder {rel}/ in the clean room, found none")
            return False
        tmp = dest.with_name(dest.name + ".incoming")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        self._docker("cp", f"{self.name}:{self.path(rel)}", str(tmp))
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)  # swap only once the copy is complete
        return True

    def get_file(self, rel: str, dest: Path) -> bool:
        if not self.exists(rel, "f"):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._docker("cp", f"{self.name}:{self.path(rel)}", str(dest))
        return True

    # --- agents and tests ---------------------------------------------------------------

    def claude(self, prompt: str, *, timeout: float, log_name: str) -> Result:
        return self.exec(self.cfg.container_claude_command, "-p", prompt, *self.cfg.container_claude_args,
                         timeout=timeout, log_name=log_name)

    def run_tests(self, *, log_name: str) -> Result:
        """Run project.test_command. Never raises on test failure; a timeout raises TransientError."""
        return self.sh(self.cfg.test_command, check=False, timeout=self.cfg.timeout("gate"), log_name=log_name)

    # --- git: one commit per passed feature ---------------------------------------------

    def git_init(self) -> None:
        if self.exists(".git", "d"):
            return
        if not self.exists(".gitignore", "f"):
            self.sh(f"printf %s {shlex.quote(DEFAULT_GITIGNORE)} > .gitignore")
        self.exec(*GIT, "init", "-q")
        self.exec(*GIT, "add", "-A")
        self.exec(*GIT, "commit", "-q", "--allow-empty", "-m", "rea-team: clean room baseline")
        self.exec(*GIT, "tag", "-f", "rea/baseline")

    def git_commit(self, fid: str, title: str) -> str:
        self.exec(*GIT, "add", "-A")
        self.exec(*GIT, "commit", "-q", "--allow-empty", "-m", f"feature {fid}: {title}")
        self.exec(*GIT, "tag", "-f", f"rea/{fid}")
        return self.exec("git", "rev-parse", "--short", "HEAD").stdout.strip()

    def git_rollback(self) -> str:
        """Throw away everything since the last commit (the last passed feature)."""
        self.sh("chmod -R u+w . 2>/dev/null || true")
        self.exec(*GIT, "reset", "-q", "--hard", "HEAD")
        self.exec(*GIT, "clean", "-q", "-fd")
        return self.exec("git", "rev-parse", "--short", "HEAD").stdout.strip()
