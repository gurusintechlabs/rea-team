"""Pure checks: leak scan, spec shape, report parsing, directory hashing."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import LeakDetected, MissingArtifact, ReportFormatError

REQUIRED_SPEC_SECTIONS = [
    "Architecture", "Data Model", "Public API", "File Formats",
    "Errors", "Test Cases", "Open questions",
]

RESULT_RE = re.compile(r"^\s*RESULT\s+wrong=(\d+)\s+crash=(\d+)(?:\s+spec_wrong=(\d+))?\s*$")
GROUP_RE = re.compile(r"^\s*(\w[\w-]*)\s+(\d+)\s*/\s*(\d+)\s+([\d.]+)%\s*$")


def require_file(path: Path, what: str, *, newer_than: float | None = None) -> None:
    """Fail if a step exited 0 but did not (re)write its output."""
    if not path.is_file() or path.stat().st_size == 0:
        raise MissingArtifact(f"{what} did not write {path}")
    if newer_than is not None and path.stat().st_mtime < newer_than - 1:
        raise MissingArtifact(f"{what} exited without updating {path} (file is stale)")


def leak_scan(text: str, patterns: list[str]) -> list[tuple[int, str]]:
    rx = re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)
    return [(n, line) for n, line in enumerate(text.splitlines(), 1) if rx.search(line)]


def assert_no_leaks(spec: Path, patterns: list[str]) -> None:
    hits = leak_scan(spec.read_text(errors="replace"), patterns)
    if hits:
        shown = "\n".join(f"  {spec.name}:{n}: {line.strip()[:120]}" for n, line in hits[:20])
        more = f"\n  ... and {len(hits) - 20} more" if len(hits) > 20 else ""
        raise LeakDetected(f"SPEC.md contains {len(hits)} leaking line(s):\n{shown}{more}")


def missing_spec_sections(text: str) -> list[str]:
    return missing_sections(text, REQUIRED_SPEC_SECTIONS)


@dataclass
class VerifyResult:
    wrong: int
    crash: int
    groups: dict[str, dict]
    spec_wrong: int = 0   # acceptance tests that fail on the ORIGINAL: the spec is wrong

    @property
    def passed(self) -> bool:
        return self.wrong == 0 and self.crash == 0 and self.spec_wrong == 0


def parse_report(text: str) -> VerifyResult:
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        raise ReportFormatError("report.md is empty")
    m = RESULT_RE.match(lines[-1])
    if not m:
        raise ReportFormatError(f"Last line of report.md is not a RESULT line: {lines[-1][:120]!r}")
    groups = {}
    for l in lines:
        g = GROUP_RE.match(l)
        if g:
            groups[g.group(1)] = {"pass": int(g.group(2)), "total": int(g.group(3)), "pct": float(g.group(4))}
    return VerifyResult(int(m.group(1)), int(m.group(2)), groups, int(m.group(3) or 0))


# Folders that installing, building or testing creates. Ignored when checking that
# an agent did not edit the rebuild, because running it legitimately creates them.
GENERATED_DIRS = frozenset({
    "node_modules", "test-results", "playwright-report", "blob-report", "coverage", "dist", "build",
    ".venv", "venv", "__pycache__", ".pytest_cache", ".gradle", "target", ".next", ".cache", ".turbo",
})


def hash_tree(root: Path, exclude: frozenset[str] = frozenset()) -> str:
    """Stable hash of a file or directory (paths + contents), skipping `exclude` folder names."""
    h = hashlib.sha256()
    if not root.exists():
        return ""
    if root.is_file():
        files = [root]
        base = root.parent
    else:
        files = sorted(p for p in root.rglob("*")
                       if p.is_file() and not p.is_symlink()
                       and not (exclude and exclude.intersection(p.relative_to(root).parts[:-1])))
        base = root
    for p in files:
        h.update(str(p.relative_to(base)).encode())
        h.update(b"\0")
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


# --- feature pipeline checks -------------------------------------------------------

REQUIRED_CONTRACT_SECTIONS = ["Conventions", "Data Model", "Public API"]


def missing_sections(text: str, required: list[str]) -> list[str]:
    headings = {m.group(1).strip().lower()
                for m in re.finditer(r"^#{1,4}\s+(.+?)\s*#*\s*$", text, re.MULTILINE)}
    return [s for s in required
            if not any(h == s.lower() or h.endswith(s.lower()) or h.startswith(s.lower()) for h in headings)]


def assert_append_only(old: str, new: str) -> None:
    """CONTRACTS.md may grow, never change: earlier text must be a prefix of the new text."""
    from .errors import ContractViolation
    o, n = old.rstrip(), new.rstrip()
    if not n.startswith(o):
        # find the first differing line to show the user
        ol, nl = o.splitlines(), n.splitlines()
        for i, (a, b) in enumerate(zip(ol, nl), 1):
            if a != b:
                raise ContractViolation(f"CONTRACTS.md line {i} changed:\n  was: {a[:120]}\n  now: {b[:120]}")
        raise ContractViolation("CONTRACTS.md lost text at its end (lines were removed)")


def assert_spec_size(path: Path, limit: int) -> None:
    from .errors import SpecTooLarge
    n = len(path.read_text(errors="replace"))
    if n > limit:
        raise SpecTooLarge(f"{path.name} is {n:,} characters (limit {limit:,}); one session can't hold it well")
