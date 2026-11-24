"""features.toml: the list of features to rebuild, and the order to build them in."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import FeatureGraphError

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MODES = ("rea", "source", "decompiled")

EXAMPLE = """\
# One [[feature]] per thing you can describe in one sentence.
# Features are built in dependency order; each one gets fresh agent sessions.
#
# mode: how the analyst reads the original for this feature
#   rea         use REA tools on lab/target/<target>   (binaries, Electron, .NET, web capture)
#   source      read the original source code at lab/target/<target>  (you still have the code)
#   decompiled  read decompiler output (e.g. a Jira plugin JAR, see [analysis] in rea-team.toml)

# [[feature]]
# id = "create-site"
# title = "Create a website"
# scope = "A signed-in user creates a new website from a template and lands in the editor."
# target = "target/legacy-web"
# mode = "source"
# depends_on = []
#
# [[feature]]
# id = "add-page"
# title = "Add a page"
# scope = "A user adds, renames and deletes pages of a website."
# target = "target/legacy-web"
# mode = "source"
# depends_on = ["create-site"]
"""


@dataclass
class Feature:
    id: str
    title: str
    scope: str
    target: str
    mode: str = "rea"
    depends_on: list[str] = field(default_factory=list)
    notes: str = ""


def parse(text: str, *, default_mode: str = "rea", source: str = "features.toml") -> list[Feature]:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise FeatureGraphError(f"{source}: {e}") from e
    items = raw.get("feature", [])
    if not isinstance(items, list):
        raise FeatureGraphError(f"{source}: use [[feature]] tables")
    feats: list[Feature] = []
    for i, it in enumerate(items, 1):
        where = f"{source} feature #{i}"
        for key in ("id", "title", "scope", "target"):
            if not isinstance(it.get(key), str) or not it[key].strip():
                raise FeatureGraphError(f"{where}: `{key}` is required")
        if not ID_RE.match(it["id"]):
            raise FeatureGraphError(f"{where}: id {it['id']!r} must be lowercase letters, digits and dashes")
        mode = it.get("mode", default_mode)
        if mode not in MODES:
            raise FeatureGraphError(f"{where}: mode must be one of {', '.join(MODES)}")
        deps = it.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise FeatureGraphError(f"{where}: depends_on must be a list of feature ids")
        unknown = set(it) - {"id", "title", "scope", "target", "mode", "depends_on", "notes"}
        if unknown:
            raise FeatureGraphError(f"{where}: unknown keys {sorted(unknown)}")
        feats.append(Feature(it["id"], it["title"].strip(), it["scope"].strip(),
                             it["target"].removeprefix("lab/"), mode, deps, it.get("notes", "")))
    return feats


def order(feats: list[Feature]) -> list[Feature]:
    """Validate ids and dependencies; return features in build order (stable)."""
    by_id: dict[str, Feature] = {}
    for f in feats:
        if f.id in by_id:
            raise FeatureGraphError(f"Duplicate feature id {f.id!r}")
        by_id[f.id] = f
    for f in feats:
        for d in f.depends_on:
            if d not in by_id:
                raise FeatureGraphError(f"{f.id!r} depends on unknown feature {d!r}")
            if d == f.id:
                raise FeatureGraphError(f"{f.id!r} depends on itself")

    out: list[Feature] = []
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    def visit(f: Feature, path: list[str]):
        s = state.get(f.id)
        if s == 2:
            return
        if s == 1:
            cyc = path[path.index(f.id):] + [f.id]
            raise FeatureGraphError("Dependency cycle: " + " → ".join(cyc))
        state[f.id] = 1
        for d in f.depends_on:
            visit(by_id[d], path + [f.id])
        state[f.id] = 2
        out.append(f)

    for f in feats:
        visit(f, [])
    return out


def load(path: Path, *, default_mode: str = "rea") -> list[Feature]:
    if not path.is_file():
        raise FeatureGraphError(f"No {path}", hint="Run `rea-team plan`, review features.proposed.toml, "
                                                    "then save it as features.toml (or write it by hand).")
    feats = order(parse(path.read_text(), default_mode=default_mode, source=path.name))
    if not feats:
        raise FeatureGraphError(f"{path} has no [[feature]] entries")
    return feats


def dependents(feats: list[Feature], fid: str) -> set[str]:
    """Every feature that (transitively) depends on fid."""
    out: set[str] = set()
    changed = True
    while changed:
        changed = False
        for f in feats:
            if f.id not in out and (fid in f.depends_on or out & set(f.depends_on)):
                out.add(f.id)
                changed = True
    return out
