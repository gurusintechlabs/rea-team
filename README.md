# rea-team

A Python framework that runs the four-agent clean-room reverse engineering team (Analyst → Spec Writer → Rebuilder → Verifier) on Claude Code and [REA](https://github.com/morluto). It checks every step, retries the failures that clear on their own, stops on the ones that don't, and saves a checkpoint after each step so you can **resume** a run instead of starting over.

> **Use it only on software you wrote or are authorized to examine.** A license can forbid reverse engineering even where the law allows it. Read the target's license first. This is engineering, not legal advice. Every run asks you to confirm this (`--authorized`) and records when you did.

## What it adds on top of `run-team.sh`

| Problem | What rea-team does |
|---|---|
| A `claude -p` call times out, hangs or exits non-zero | Kills the whole process group, then retries with exponential backoff (`run.retries`) |
| An agent exits 0 but writes nothing, or leaves an old file | Checks that every output file exists, isn't empty and was written by **this** attempt. If not, it retries |
| SPEC.md is missing required sections | Treated as a failed attempt and retried |
| SPEC.md leaks addresses or decompiled names | Asks the spec-writer to rewrite those lines, scans again, and stops hard (`LeakDetected`) before anything crosses the wall |
| The clean container can reach `rea`, the npx cache, the npm registry or lab paths | Checks the wall before **every** rebuild and stops (`WallBreach`) |
| The analyst modifies the target, or the verifier edits `rebuild/` or SPEC.md | Hashes before and after, and stops (`IntegrityError`) |
| report.md has no `RESULT wrong=<n> crash=<n>` line | Retries the verifier, then stops with `ReportFormatError` |
| Docker Desktop is down or the container stopped | Stops with `ContainerError` and tells you how to start it |
| Ctrl-C, SIGTERM, a laptop going to sleep, or a crash | Saves the state to disk after every step (atomic writes). Pick up with `rea-team resume` |
| Two runs started at once in the same workspace | Locks the workspace (`LockHeld`) |
| No parity after N rounds | Exits with code 2 and keeps every round's report. `resume --extra-rounds 2` continues |

Every failure prints its cause, a next step and the resume command. Exit codes are listed in `src/rea_team/errors.py`.

## Install (macOS)

```bash
brew install python@3.12          # Python 3.11+ is required
pipx install git+https://github.com/<you>/<repo>.git
# or, from a clone:  pip install -e '.[dev]'
```

You also need Node 22.19+/24.11+/26+, Claude Code, REA (`npx rea-agents@latest setup`, then `npm i -g rea-agents`), and Docker Desktop.

## Set up

```bash
rea-team init my-team && cd my-team
cp -R /path/to/YourApp.app lab/target/
cd lab && rea doctor --json > analysis/doctor.json && cd ..
rea-team set-tools --from-doctor lab/analysis/doctor.json   # or --tools name1,name2
```

`set-tools` fills in the analyst's `tools:` line with `mcp__rea__*` names. `doctor` won't pass while the placeholder is still there.

### Clean room

The Rebuilder runs in its own container. That container mounts **only** `clean/`, has Claude Code installed and signed in, has no REA, and has the npm registry removed from its firewall allowlist **after** its dependencies are installed. Claude Code's reference devcontainer, which has an outbound allowlist firewall, is a good base. Once you have an image:

```bash
docker run -d --name rea-clean \
  -v "$PWD/clean:/work" -w /work \
  -e ANTHROPIC_API_KEY \
  your-clean-image sleep infinity
```

Never mount `lab/`, `~/.claude.json` or your home folder into it.

```bash
rea-team doctor --target target/YourApp.app
```

`doctor` checks node, claude, rea, docker, all four agent files (the analyst has REA tools, the spec-writer has no Bash or REA, the rebuilder has no REA), the container, Claude inside the container, and the wall itself (`which rea`, `npx --no-install rea-agents`, `npm view`, `/lab` must all **fail**).

## Run

```bash
rea-team run target/YourApp.app "how search ranks results" --authorized
```

Steps and their checkpoint keys:

```
analyst → spec_writer → r1.leak_check → r1.handoff → r1.wall_check → r1.rebuild → r1.verify
        → r1.spec_update → r2.leak_check → … up to run.max_rounds (default 3)
```

```bash
rea-team status                    # list runs
rea-team status latest             # detail: rounds, match rates, error and hint
rea-team resume                    # continue the latest run where it stopped
rea-team resume <id> --from-step r2.rebuild   # redo from a finished step
rea-team resume <id> --extra-rounds 2         # after "no parity"
```

Each run keeps its files in `lab/.rea-team/runs/<id>/`: `state.json`, `logs/<step>.log` (full stdout and stderr of every attempt), `round-N/SPEC.md` and `report.md`, and the Rebuilder's `QUESTIONS.md` for each round.

Read them in this order: the unknowns in `evidence.md`, the Open questions in `SPEC.md`, then `report.md`.

## Configuration

`rea-team.toml` (written by `init`): rounds, retries, backoff, per-step timeouts, extra `claude` flags (headless runs need a permission mode that lets agents write files; the default is `--permission-mode acceptEdits`), the container name, workdir and forbidden paths, and the leak-check regexes.

## Limits

- REA only reads what is on your machine. Server-side code was never shipped to you.
- Decompiled code is a reconstruction, which is why the verifier compares behavior, not source.
- Stripped native binaries are harder than Electron or .NET. Start with those two.
- The wall check catches common breaches. It doesn't replace configuring the container correctly.

## Development

```bash
pip install -e '.[dev]' && pytest
```

The tests simulate claude, docker and the container (`tests/fakes.py`, `tests/fakebin/`). They cover the happy path, convergence over several rounds, no parity, retries, leak auto-fix, leaks that won't go away, wall breaches, integrity violations, a timeout followed by resume, and the authorization gate.
