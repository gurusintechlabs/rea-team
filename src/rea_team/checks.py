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

RESULT_RE = re.compile(r"^\s*RESULT\s+wrong=(\d+)\s+crash=(\d+)\s*$")
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
    headings = {m.group(1).strip().lower()
                for m in re.finditer(r"^#{1,4}\s+(.+?)\s*#*\s*$", text, re.MULTILINE)}
    return [s for s in REQUIRED_SPEC_SECTIONS
            if not any(h == s.lower() or h.endswith(s.lower()) or h.startswith(s.lower()) for h in headings)]


@dataclass
class VerifyResult:
    wrong: int
    crash: int
    groups: dict[str, dict]

    @property
    def passed(self) -> bool:
        return self.wrong == 0 and self.crash == 0


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
    return VerifyResult(int(m.group(1)), int(m.group(2)), groups)


def hash_tree(root: Path) -> str:
    """Stable hash of a file or directory (paths + contents)."""
    h = hashlib.sha256()
    if root.is_file():
        files = [root]
        base = root.parent
    else:
        files = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
        base = root
    for p in files:
        h.update(str(p.relative_to(base)).encode())
        h.update(b"\0")
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()
