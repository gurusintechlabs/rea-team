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

from . import __version__, config, preflight
from .errors import AuthorizationError, PreflightError, ReaTeamError
from .pipeline import Pipeline
from .runner import Runner
from .state import STATUS_PASSED, RunState, WorkspaceLock, list_runs

log = logging.getLogger("rea_team")


# --- init --------------------------------------------------------------------

def cmd_init(args) -> int:
    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    tpl = resources.files("rea_team") / "templates"
    written = []

    def put(rel: str, content: str):
        p = root / rel
        if p.exists() and not args.force:
            log.info("  keep   %s", rel)
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        written.append(rel)
        log.info("  write  %s", rel)

    put(config.CONFIG_NAME, config.DEFAULT_CONFIG)
    for side, names in [("lab", ["analyst", "spec-writer", "verifier"]), ("clean", ["rebuilder"])]:
        for n in names:
            put(f"{side}/.claude/agents/{n}.md", (tpl / side / ".claude" / "agents" / f"{n}.md").read_text())
    for d in ["lab/target", "lab/analysis", "lab/handoff", "clean/src"]:
        (root / d).mkdir(parents=True, exist_ok=True)
    put("lab/.gitignore", "target/\nanalysis/\nrebuild/\nreport.md\n.rea-team/\n")

    print(f"\nWorkspace ready at {root}\nNext:\n"
          f"  1. Put the app you own in {root / 'lab/target'}\n"
          f"  2. cd {root / 'lab'} && rea doctor --json > analysis/doctor.json\n"
          f"  3. rea-team set-tools --from-doctor lab/analysis/doctor.json\n"
          f"  4. Start the clean container (README, 'Clean room'), then: rea-team doctor")
    return 0


# --- doctor / set-tools --------------------------------------------------------

def _print_checks(checks) -> bool:
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"  {'✓' if c.ok else '✗'} {c.name:<{width}}  {c.detail}")
    return all(c.ok for c in checks)


def cmd_doctor(args) -> int:
    cfg = config.load()
    ok = _print_checks(preflight.run_checks(cfg, Runner(cfg.state_dir / "logs"), target=args.target))
    print("\nAll checks passed." if ok else "\nFix the ✗ items above before running.")
    return 0 if ok else PreflightError.exit_code


def cmd_set_tools(args) -> int:
    cfg = config.load()
    if args.tools:
        tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    elif args.from_doctor:
        tools = preflight.extract_rea_tools(Path(args.from_doctor))
    else:
        raise ReaTeamError("Pass --tools a,b,c or --from-doctor path/to/doctor.json")
    if not tools:
        raise ReaTeamError("No REA tool names found.",
                           hint="Open doctor.json, find the tool catalog and pass the names with --tools.")
    path = preflight.set_analyst_tools(cfg, tools)
    print(f"Set {len(tools)} REA tools in {path}:\n  " + "\n  ".join(tools))
    return 0


# --- run / resume ----------------------------------------------------------------

def _confirm_authorized(args) -> str:
    if args.authorized:
        return dt.datetime.now().isoformat(timespec="seconds")
    if sys.stdin.isatty():
        print("This pipeline reverse engineers software. Use it only on software you wrote or are\n"
              "authorized to examine, and check the target's license: it can forbid reverse\n"
              "engineering even where the law allows it. This is not legal advice.")
        if input("Do you own or have authorization to examine this target? [y/N] ").strip().lower() == "y":
            return dt.datetime.now().isoformat(timespec="seconds")
    raise AuthorizationError("Authorization not confirmed.")


def _preflight_or_fail(cfg, target: str) -> None:
    checks = preflight.run_checks(cfg, Runner(cfg.state_dir / "logs"), target=target)
    failed = [c for c in checks if not c.ok]
    if failed:
        _print_checks(checks)
        raise PreflightError(f"{len(failed)} preflight check(s) failed: " + ", ".join(c.name for c in failed))


def _execute(cfg, state: RunState, skip_preflight: bool) -> int:
    with WorkspaceLock(cfg.state_dir):
        if not skip_preflight:
            _preflight_or_fail(cfg, state.target)
        state.save(cfg.state_dir)
        log.info("run %s: %s / %s", state.run_id, state.target, state.feature)
        try:
            Pipeline(cfg, state).run()
        except KeyboardInterrupt:
            state.status = "interrupted"
            state.save(cfg.state_dir)
            print(f"\nInterrupted. Resume with: rea-team resume {state.run_id}", file=sys.stderr)
            return 130
        except ReaTeamError:
            print(f"\nRun {state.run_id} stopped. Resume with: rea-team resume {state.run_id}", file=sys.stderr)
            raise
    _summary(cfg, state)
    return 0


def cmd_run(args) -> int:
    cfg = config.load()
    authorized_at = _confirm_authorized(args)
    target = args.target.removeprefix("lab/")
    state = RunState(run_id=RunState.new_id(), target=target, feature=args.feature,
                     authorized_at=authorized_at)
    return _execute(cfg, state, args.skip_preflight)


def cmd_resume(args) -> int:
    cfg = config.load()
    run_id = args.run_id
    if run_id == "latest":
        runs = list_runs(cfg.state_dir)
        if not runs:
            raise ReaTeamError("No runs to resume.")
        run_id = runs[-1]
    state = RunState.load(cfg.state_dir, run_id)
    if state.status == STATUS_PASSED:
        print(f"Run {run_id} already reached parity.")
        return 0
    if args.from_step:
        if args.from_step not in state.done:
            raise ReaTeamError(f"{args.from_step!r} is not a completed step. Done: {', '.join(state.done)}")
        state.done = state.done[: state.done.index(args.from_step)]
        if args.from_step.startswith("r") and "." in args.from_step:
            state.round = int(args.from_step[1:].split(".")[0])
    if args.extra_rounds:
        cfg.max_rounds = max(cfg.max_rounds, state.round) + args.extra_rounds
    return _execute(cfg, state, args.skip_preflight)


# --- status ------------------------------------------------------------------------

def _summary(cfg, st: RunState) -> None:
    print(f"\nRun {st.run_id}: {st.status.upper()}")
    print(f"  target   {st.target}\n  feature  {st.feature}\n  rounds   {st.round}")
    for r in st.results:
        groups = "  ".join(f"{k} {v['pass']}/{v['total']}" for k, v in r["groups"].items())
        print(f"  round {r['round']}: wrong={r['wrong']} crash={r['crash']}  {groups}")
    if st.error:
        print(f"  failed at {st.error['step']}: {st.error['type']}: {st.error['message']}")
        if st.error.get("hint"):
            print(f"  next: {st.error['hint']}")
    print(f"  files    {st.path(cfg.state_dir).parent}")


def cmd_status(args) -> int:
    cfg = config.load()
    runs = list_runs(cfg.state_dir)
    if not args.run_id:
        if not runs:
            print("No runs yet.")
        for r in runs:
            s = RunState.load(cfg.state_dir, r)
            print(f"  {r}  {s.status:<12} round {s.round}  {s.target}: {s.feature}")
        return 0
    _summary(cfg, RunState.load(cfg.state_dir, runs[-1] if args.run_id == "latest" else args.run_id))
    return 0


# --- entry ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rea-team", description="Clean-room reverse engineering team for Claude Code + REA.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create the lab/ + clean/ workspace")
    s.add_argument("dir", nargs="?", default="rea-team")
    s.add_argument("--force", action="store_true", help="overwrite existing files")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("doctor", help="check tools, agent files, container and wall")
    s.add_argument("--target")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("set-tools", help="fill the analyst's REA tools line")
    s.add_argument("--from-doctor")
    s.add_argument("--tools", help="comma-separated REA tool names")
    s.set_defaults(fn=cmd_set_tools)

    s = sub.add_parser("run", help="start a new run")
    s.add_argument("target", help="path inside lab/, e.g. target/Notes.app")
    s.add_argument("feature", help='one sentence, e.g. "how search ranks results"')
    s.add_argument("--authorized", action="store_true",
                   help="confirm you own or are authorized to examine the target")
    s.add_argument("--skip-preflight", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("resume", help="continue a stopped run from its last checkpoint")
    s.add_argument("run_id", nargs="?", default="latest")
    s.add_argument("--from-step", help="redo from this completed step, e.g. spec_writer or r2.rebuild")
    s.add_argument("--extra-rounds", type=int, default=0, help="allow N more rounds after no parity")
    s.add_argument("--skip-preflight", action="store_true")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("status", help="list runs, or show one")
    s.add_argument("run_id", nargs="?")
    s.set_defaults(fn=cmd_status)
    return p


def _sigterm(*_):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
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
        traceback.print_exc() if args.verbose else print(
            f"\nunexpected error: {sys.exc_info()[1]!r}\n(re-run with --verbose for the traceback)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
