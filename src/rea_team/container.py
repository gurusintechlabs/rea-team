"""The clean room: a Docker container that never sees REA or the lab."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .config import Config
from .errors import ContainerError, StepFailed, TransientError, WallBreach
from .runner import Runner

log = logging.getLogger("rea_team")


class CleanRoom:
    def __init__(self, cfg: Config, runner: Runner):
        self.cfg = cfg
        self.runner = runner
        self.name = cfg.container_name
        self.workdir = cfg.container_workdir.rstrip("/")

    def _docker(self, *args: str, check: bool = True, timeout: float | None = None, log_name="docker"):
        return self.runner.run(["docker", *args], check=check,
                               timeout=timeout or self.cfg.timeout("docker"), log_name=log_name)

    def exec(self, *cmd: str, check: bool = True, timeout: float | None = None, log_name="docker"):
        return self._docker("exec", "-w", self.workdir, self.name, *cmd,
                            check=check, timeout=timeout, log_name=log_name)

    # --- health ------------------------------------------------------------

    def ensure_running(self) -> None:
        try:
            r = self._docker("inspect", "-f", "{{.State.Running}}", self.name, check=False)
        except StepFailed as e:
            raise ContainerError(f"Docker is not available: {e}",
                                 hint="Start Docker Desktop, then retry.") from e
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
            ("npm registry is reachable (could pull rea-agents back in)",
             ["npm", "view", "rea-agents", "version"]),
        ]
        probes += [(f"{p} exists", ["test", "-e", p]) for p in self.cfg.forbidden_paths]

        breaches = []
        for desc, cmd in probes:
            try:
                r = self.exec(*cmd, check=False, timeout=30, log_name="wall-check")
            except TransientError:
                continue  # hung = blocked by firewall = good
            except StepFailed:
                continue
            if r.returncode == 0:
                breaches.append(desc)
        return breaches

    def assert_wall(self) -> None:
        breaches = self.check_wall()
        if breaches:
            raise WallBreach("Clean container is not isolated:\n  - " + "\n  - ".join(breaches))

    def check_claude(self) -> None:
        r = self.exec(self.cfg.container_claude_command, "--version", check=False, log_name="preflight")
        if r.returncode != 0:
            raise ContainerError(f"Claude Code does not run inside {self.name!r}.",
                                 hint="Install Claude Code in the container image and sign it in.")

    # --- transfer ------------------------------------------------------------

    def put_spec(self, spec: Path) -> None:
        dest = f"{self.workdir}/SPEC.md"
        self.exec("rm", "-f", dest)
        self._docker("cp", str(spec), f"{self.name}:{dest}")
        self.exec("chmod", "a-w", dest)

    def get_src(self, dest: Path) -> None:
        r = self.exec("test", "-d", f"{self.workdir}/src", check=False)
        if r.returncode != 0:
            raise StepFailed("Rebuilder finished but wrote no src/ folder in the container")
        tmp = dest.with_name(dest.name + ".incoming")
        shutil.rmtree(tmp, ignore_errors=True)
        self._docker("cp", f"{self.name}:{self.workdir}/src", str(tmp))
        if not any(tmp.rglob("*")):
            shutil.rmtree(tmp, ignore_errors=True)
            raise StepFailed("Rebuilder's src/ is empty")
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)  # swap only once the copy is complete

    def get_optional(self, name: str, dest: Path) -> bool:
        r = self.exec("test", "-f", f"{self.workdir}/{name}", check=False)
        if r.returncode != 0:
            return False
        self._docker("cp", f"{self.name}:{self.workdir}/{name}", str(dest))
        return True
