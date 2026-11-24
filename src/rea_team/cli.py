"""rea-team command line."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import signal
import sys
import traceback
from importlib import resources
from pathlib import Path

from . import __version__, config, features as featmod, preflight
from .errors import AuthorizationError, PreflightError, ReaTeamError
from .pipeline import Project
from .runner import Runner
from .state import PASSED, ProjectState, WorkspaceLock

log = logging.getLogger("rea_team")

LAB_AGENTS = ["analyst", "spec-writer", "verifier"]
CLEAN_AGENTS = ["test-writer", "rebuilder"]
ICON = {"passed": "✓", "failed": "✗", "blocked": "⊘", "running": "…", "interrupted": "‖", "pending": "·"}


# --- init ----------------------------------------------------------------------------------

def cmd_init(args) -> int:
    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    tpl = resources.files("rea_team") / "templates"

    def put(rel: str, content: str):
        p = root / rel
        if p.exists() and not args.force:
            log.info("  keep   %s", rel)
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        log.info("  write  %s", rel)

    put(config.CONFIG_NAME, config.DEFAULT_CONFIG)
    put("features.toml", featmod.EXAMPLE)
    for side, names in [("lab", LAB_AGENTS), ("clean", CLEAN_AGENTS)]:
        for n in names:
            put(f"{side}/.claude/agents/{n}.md", (tpl / side / ".claude" / "agents" / f"{n}.md").read_text())
    for d in ["lab/target", "lab/analysis", "lab/handoff/features", "clean/src"]:
        (root / d).mkdir(parents=True, exist_ok=True)
    put("lab/.gitignore", "target/\nanalysis/\nrebuild/\n.rea-team/\n")

    print(f"\nWorkspace ready at {root}\n\nNext (details in docs/USER_GUIDE.md):\n"
          f"  1. Copy the app into lab/target/  (binary, install folder, source tree, JAR...)\n"
          f"  2. Edit rea-team.toml: [project] stack + test_command, [verify]\n"
          f"  3. If you use REA: rea-team set-tools   (asks `rea --mcp` for its tool names)\n"
          f"  4. Start the clean container, then: rea-team doctor\n"
          f"  5. rea-team plan target/<app> --authorized")
    return 0


# --- doctor / set-tools -------------------------------------------------------------------------

def _print_checks(checks) -> bool:
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"  {'✓' if c.ok else '✗'} {c.name:<{width}}  {c.detail}")
    return all(c.ok for c in checks)


def cmd_doctor(args) -> int:
    cfg = config.load()
    has_features = featmod.has_entries(cfg.features_file)
    checks = preflight.run_checks(cfg, Runner(cfg.state_dir / "logs" / "doctor"), targets=args.target or None,
                                  check_features=has_features,
                                  needs_rea=None if has_features else cfg.default_mode == "rea")
    ok = _print_checks(checks)
    print("\nAll checks passed." if ok else "\nFix the ✗ items above before running.")
    return 0 if ok else PreflightError.exit_code


def cmd_set_tools(args) -> int:
    cfg = config.load()
    if args.tools:
        tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    elif args.from_doctor:
        tools = preflight.extract_rea_tools(Path(args.from_doctor))
    else:
        tools = preflight.list_mcp_tools(args.from_mcp)
    if not tools:
        raise ReaTeamError("No REA tool names found.",
                           hint="Run `rea-team set-tools` with no options to ask `rea --mcp`, or pass --tools.")
    path = preflight.set_analyst_tools(cfg, tools)
    print(f"Set {len(tools)} REA tools in {path}:\n  " + "\n  ".join(tools))
    return 0


# --- authorization / preflight ---------------------------------------------------------------------

def _authorize(state: ProjectState, args) -> None:
    if state.authorized_at:
        return
    if getattr(args, "authorized", False):
        state.authorized_at = dt.datetime.now().isoformat(timespec="seconds")
        return
    if sys.stdin.isatty():
        print("This pipeline reverse engineers software. Use it only on software you own or are\n"
              "authorized to examine, and check its license. This is not legal advice.")
        if input("Do you own or have authorization to examine these targets? [y/N] ").strip().lower() == "y":
            state.authorized_at = dt.datetime.now().isoformat(timespec="seconds")
            return
    raise AuthorizationError("Authorization not confirmed.")


def _preflight_or_fail(cfg, *, targets=None, check_features=False, container=True, needs_rea=None) -> None:
    checks = preflight.run_checks(cfg, Runner(cfg.state_dir / "logs" / "doctor"), targets=targets,
                                  check_features=check_features, include_container=container,
                                  needs_rea=needs_rea)
    failed = [c for c in checks if not c.ok]
    if failed:
        _print_checks(checks)
        raise PreflightError(f"{len(failed)} preflight check(s) failed: " + ", ".join(c.name for c in failed))
    if needs_rea is False or (needs_rea is None and not preflight.project_needs_rea(cfg)):
        if preflight.strip_rea_placeholder(cfg):
            log.info("no feature uses REA: removed the unused REA placeholder from the analyst's tools")


# --- plan --------------------------------------------------------------------------------------------

def cmd_plan(args) -> int:
    cfg = config.load()
    targets = [t.removeprefix("lab/") for t in args.targets]
    mode = args.mode or cfg.default_mode
    with WorkspaceLock(cfg.state_dir):
        state = ProjectState.load(cfg.state_dir)
        _authorize(state, args)
        if args.redo:
            state.planned = []
        state.save(cfg.state_dir)
        if not args.skip_preflight:
            _preflight_or_fail(cfg, targets=targets, container=False, needs_rea=(mode == "rea"))
        try:
            Project(cfg, state).plan(targets, mode)
        except KeyboardInterrupt:
            print("\nInterrupted. Run `rea-team plan` again to continue.", file=sys.stderr)
            return 130

    proposed = cfg.root / "features.proposed.toml"
    feats = featmod.order(featmod.parse(proposed.read_text(), default_mode=mode))
    print(f"\nProposed {len(feats)} features (build order):")
    for f in feats:
        deps = f"  ← {', '.join(f.depends_on)}" if f.depends_on else ""
        print(f"  {f.id:<28} {f.title}{deps}")
    if args.accept:
        if featmod.has_entries(cfg.features_file) and not args.force:
            raise ReaTeamError("features.toml already has features; pass --force to overwrite it")
        cfg.features_file.write_text(proposed.read_text())
        print(f"\nSaved as {cfg.features_file.name}.")
    else:
        print(f"\nReview {proposed.name} and lab/handoff/CONTRACTS.md, edit as needed, then save the list as "
              f"features.toml (or re-run with --accept).")
    return 0


# --- run ---------------------------------------------------------------------------------------------

def cmd_run(args) -> int:
    cfg = config.load()
    if args.keep_going:
        cfg.keep_going = True
    feats = featmod.load(cfg.features_file, default_mode=cfg.default_mode)
    with WorkspaceLock(cfg.state_dir):
        state = ProjectState.load(cfg.state_dir)
        state.backup(cfg.state_dir)
        _authorize(state, args)
        state.save(cfg.state_dir)
        if args.extra_rounds:
            highest = max((state.feature(f.id).round for f in feats), default=1)
            cfg.max_rounds = max(cfg.max_rounds, highest - 1) + args.extra_rounds
        if not args.skip_preflight:
            _preflight_or_fail(cfg, check_features=True)
        project = Project(cfg, state)
        try:
            project.run(feats, only=args.feature or None)
        except KeyboardInterrupt:
            state.save(cfg.state_dir)
            print("\nInterrupted. Run `rea-team run` again to continue where it stopped.", file=sys.stderr)
            return 130
        except ReaTeamError:
            _summary(cfg, state, feats)
            print("\nFix the cause above, then `rea-team run` continues from the last checkpoint.",
                  file=sys.stderr)
            raise
    _summary(cfg, state, feats)
    return 0


# --- reset / status -------------------------------------------------------------------------------------

def cmd_reset(args) -> int:
    cfg = config.load()
    feats = {f.id: f for f in featmod.load(cfg.features_file, default_mode=cfg.default_mode)}
    if args.feature not in feats:
        raise ReaTeamError(f"Unknown feature {args.feature!r}")
    with WorkspaceLock(cfg.state_dir):
        state = ProjectState.load(cfg.state_dir)
        state.backup(cfg.state_dir)
        fs = state.feature(args.feature)
        if fs.status == PASSED and not args.from_step:
            dependents = featmod.dependents(list(feats.values()), args.feature)
            passed_deps = [d for d in dependents if state.feature(d).status == PASSED]
            if passed_deps:
                print(f"Note: {', '.join(sorted(passed_deps))} depend on it and stay passed. Their commits\n"
                      "already include this feature's code; re-running it builds on top of them.")
        fs.reset(args.from_step)
        state.save(cfg.state_dir)
    what = f"from {args.from_step}" if args.from_step else "completely"
    print(f"Reset {args.feature} {what}. `rea-team run --feature {args.feature}` runs it again.")
    return 0


def _summary(cfg, state: ProjectState, feats) -> None:
    print(f"\n{cfg.project_name}: features in build order")
    for f in feats:
        fs = state.features.get(f.id)
        status = fs.status if fs else "pending"
        line = f"  {ICON.get(status, '?')} {f.id:<28} {status:<11}"
        if fs and fs.results:
            r = fs.results[-1]
            line += f" round {r['round']}: wrong={r['wrong']} crash={r['crash']}"
        if fs and fs.commit:
            line += f"  commit {fs.commit}"
        print(line)
        if fs and fs.error and status in ("failed", "blocked"):
            print(f"      {fs.error['type']} at {fs.error['step']}: {fs.error['message'].splitlines()[0][:150]}")
            if fs.error.get("hint"):
                print(f"      next: {fs.error['hint']}")
    counts = {}
    for f in feats:
        s = state.features[f.id].status if f.id in state.features else "pending"
        counts[s] = counts.get(s, 0) + 1
    print("  " + ", ".join(f"{n} {s}" for s, n in counts.items()))


def cmd_status(args) -> int:
    cfg = config.load()
    state = ProjectState.load(cfg.state_dir)
    feats = featmod.load(cfg.features_file, default_mode=cfg.default_mode)
    if not args.feature:
        print(f"plan: {', '.join(state.planned) or 'not started'}"
              f"{'  (authorized ' + state.authorized_at + ')' if state.authorized_at else ''}")
        _summary(cfg, state, feats)
        return 0
    fs = state.features.get(args.feature)
    if not fs:
        print(f"{args.feature}: not started")
        return 0
    print(f"{fs.id}: {fs.status}, round {fs.round}")
    print(f"  steps done: {', '.join(fs.done) or 'none'}")
    for r in fs.results:
        groups = "  ".join(f"{k} {v['pass']}/{v['total']}" for k, v in r["groups"].items())
        print(f"  round {r['round']}: wrong={r['wrong']} crash={r['crash']} "
              f"spec_wrong={r.get('spec_wrong', 0)}  {groups}")
    fails = [g for g in fs.gate if g["exit"] != 0]
    print(f"  test gate runs: {len(fs.gate)} ({len(fails)} failing)")
    if fs.commit:
        print(f"  commit: {fs.commit} (tag rea/{fs.id})")
    if fs.error:
        print(f"  error: {fs.error['type']} at {fs.error['step']}: {fs.error['message']}")
        if fs.error.get("hint"):
            print(f"  next:  {fs.error['hint']}")
    print(f"  files: {cfg.state_dir / 'features' / fs.id}  logs: {cfg.state_dir / 'logs' / fs.id}")
    return 0


# --- entry ------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rea-team",
                                description="Feature-by-feature clean-room rebuilds with Claude Code (+ REA).")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create the workspace")
    s.add_argument("dir", nargs="?", default="rea-team")
    s.add_argument("--force", action="store_true", help="overwrite existing files")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("doctor", help="check tools, agents, features, container and wall")
    s.add_argument("--target", action="append", help="also check this target exists (repeatable)")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("set-tools", help="fill the analyst's REA tools line")
    src = s.add_mutually_exclusive_group()
    src.add_argument("--from-mcp", default="rea --mcp", metavar="CMD",
                     help="ask this stdio MCP server for its tools (default: %(default)s)")
    src.add_argument("--from-doctor", metavar="FILE", help="read tool names from `rea doctor --json` output")
    src.add_argument("--tools", help="comma-separated REA tool names")
    s.set_defaults(fn=cmd_set_tools)

    s = sub.add_parser("plan", help="survey the app, propose features, write CONTRACTS.md")
    s.add_argument("targets", nargs="+", help="paths inside lab/, e.g. target/legacy-web")
    s.add_argument("--mode", choices=["rea", "source", "decompiled"])
    s.add_argument("--accept", action="store_true", help="save the proposal as features.toml")
    s.add_argument("--force", action="store_true", help="with --accept: overwrite features.toml")
    s.add_argument("--redo", action="store_true", help="run the survey and contracts again")
    s.add_argument("--authorized", action="store_true")
    s.add_argument("--skip-preflight", action="store_true")
    s.set_defaults(fn=cmd_plan)

    for name, help_ in [("run", "build features in dependency order (continues where it stopped)"),
                        ("resume", "same as run")]:
        s = sub.add_parser(name, help=help_)
        s.add_argument("--feature", action="append", help="only this feature (+ its unfinished dependencies)")
        s.add_argument("--keep-going", action="store_true", help="skip a failed feature's dependents and continue")
        s.add_argument("--extra-rounds", type=int, default=0, help="allow N more rounds for features out of rounds")
        s.add_argument("--authorized", action="store_true")
        s.add_argument("--skip-preflight", action="store_true")
        s.set_defaults(fn=cmd_run)

    s = sub.add_parser("reset", help="forget a feature's progress so it runs again")
    s.add_argument("feature")
    s.add_argument("--from-step", help="keep steps before this one, e.g. spec or r2.build")
    s.set_defaults(fn=cmd_reset)

    s = sub.add_parser("status", help="show all features, or one in detail")
    s.add_argument("feature", nargs="?")
    s.set_defaults(fn=cmd_status)
    return p


def _sigterm(*_):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S", force=True)
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        return args.fn(args)
    except ReaTeamError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        if e.hint:
            print(f"hint:  {e.hint}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception:
        if args.verbose:
            traceback.print_exc()
        else:
            print(f"\nunexpected error: {sys.exc_info()[1]!r}\n(re-run with --verbose for the traceback)",
                  file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
