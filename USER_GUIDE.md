# rea-team User Guide

rea-team rebuilds legacy software **one feature at a time** with Claude Code. A team of agents reads the old application, writes a behavior-only spec for each feature, and builds the new version from those specs in a separate clean room. Every feature has to pass its own tests, every earlier feature's tests and a side-by-side comparison with the original before it's committed.

The guide is written for rebuilding **your own** legacy products: desktop apps, web apps and Jira plugins.

**New here?** Read section 1, then follow the [end-to-end walkthrough](#21-end-to-end-walkthrough-a-sample-web-app) in section 21, which rebuilds a sample web app step by step.

---

## Contents

1. [How it works in one page](#1-how-it-works-in-one-page)
2. [Core concepts](#2-core-concepts)
3. [Install](#3-install)
4. [Workspace layout](#4-workspace-layout)
5. [Set up the clean room container](#5-set-up-the-clean-room-container)
6. [Configure rea-team.toml](#6-configure-rea-teamtoml)
7. [Choose an analysis mode per target](#7-choose-an-analysis-mode-per-target)
8. [Plan: survey, features and contracts](#8-plan-survey-features-and-contracts)
9. [features.toml and how to slice features](#9-featurestoml-and-how-to-slice-features)
10. [Run the build](#10-run-the-build)
11. [Testing strategy](#11-testing-strategy)
12. [Recipes: web app, desktop app, Jira plugin](#12-recipes)
13. [When something fails](#13-when-something-fails)
14. [Resume, reset and extra rounds](#14-resume-reset-and-extra-rounds)
15. [Files and logs reference](#15-files-and-logs-reference)
16. [Keeping context small and costs down](#16-keeping-context-small-and-costs-down)
17. [rea-team.toml reference](#17-rea-teamtoml-reference)
18. [Command reference](#18-command-reference)
19. [FAQ](#19-faq)
20. [Limits](#20-limits)
21. [End-to-end walkthrough: a sample web app](#21-end-to-end-walkthrough-a-sample-web-app)

---

## 1. How it works in one page

```
                 LAB  (your machine: original app, REA, analysis)          │  CLEAN ROOM (container: no REA, no lab)
                                                                           │
 plan:   analyst (survey) ──► features.proposed.toml                       │
         spec-writer      ──► handoff/CONTRACTS.md                         │
                                                                           │
 for each feature, in dependency order:                                    │
   1 analyst      reads ONE feature  ──► analysis/features/<id>/evidence.md│
   2 spec-writer  behavior only      ──► handoff/features/<id>.md          │
   3 leak check   no addresses / internal names may cross                 │
   4 handoff      CONTRACTS.md + feature spec  ════════════════════════════╪══► specs/ (read-only)
   5 wall check   container can't reach rea, npm-rea, /lab                 │
   6                                                                       │  test-writer ──► tests/acceptance/<id>/ (then read-only)
   7                                                                       │  rebuilder   ──► src/ + BUILD_STATE.md
   8                                                                       │  TEST GATE: unit + ALL features' acceptance tests
   9                                                                       │    fail → fix session (up to 3) → still failing = rollback
  10 verifier     original vs rebuild, same inputs  ◄═══════════════════════╪═══ src/ + tests/ copied back
  11 not equal?   divergences become new Test Cases → next round (step 3)  │
  12 equal        ═════════════════════════════════════════════════════════╪══► git commit, tag rea/<id>
```

Every agent run is a **fresh Claude Code session** (`claude -p`) with one job. Nothing carries over between sessions except files:

| Carried by | From → to | Contents |
|---|---|---|
| `CONTRACTS.md` | spec-writer → everyone | shared conventions, data model, API (append-only) |
| `features/<id>.md` | spec-writer → clean room | one feature's behavior and test cases |
| `tests/acceptance/<id>/` | test-writer → rebuilder, gate, verifier | executable definition of "done" |
| `BUILD_STATE.md` | rebuilder → next rebuilder | what's built, code map, decisions (under 200 lines) |
| `report.md` | verifier → spec-writer | where the rebuild differs from the original |

This is how a large application fits: no session ever needs more than the contracts, one feature spec, the build state and the code it touches.

---

## 2. Core concepts

**Lab and clean room.** The lab has the original application and the analysis tools. The clean room is a Docker container that only ever receives specs. It can't see the original, so the rebuild is designed from behavior rather than copied from legacy internals, which means it doesn't inherit legacy bugs or structure. For your own software this is about **quality**, not legal separation. It's still worth enforcing, because an agent that can peek at old code will copy it.

**Feature.** Something a user can do that you can describe in one sentence, e.g. "A user adds, renames and deletes pages of a website". Each feature has an id, a target, an analysis mode and dependencies.

**Contracts.** `CONTRACTS.md` defines what all features share: naming and error conventions, the data model, the public API. Without it, feature 7 would invent a different `User` from feature 2. It's **append-only**: features may add to it, never change it. rea-team enforces this.

**Spec.** A feature's behavior-only description, with numbered Test Cases (TC-1, TC-2…) and Open questions for anything not proven.

**Acceptance tests.** These are written by a separate test-writer agent from the spec, **before** the code exists. They test from the outside (UI, HTTP, CLI, files) and read the app's location from `APP_URL` or `APP_CMD`, so the same tests also run against the original. Once written they're read-only, and the rebuilder can't weaken them.

**Test gate.** After each build session rea-team itself runs `project.test_command`: your unit tests plus **every** feature's acceptance tests. Exit 0 or it doesn't pass. That's the regression test. A feature that breaks an older feature fails.

**Round.** One pass of: spec → tests → build → gate → verify. If the verifier finds differences, they become new Test Cases and the next round starts. The default is at most 3 rounds.

**Checkpoint.** Every completed step is recorded in `lab/.rea-team/project.json`. `rea-team run` always continues from where things stopped.

**Rollback.** The clean room is a git repo with one commit per passed feature, tagged `rea/<id>`. If a feature fails, the clean room is reset to the last passed feature, so a half-built feature never pollutes the next one.

---

## 3. Install

On macOS you need:

| Tool | Why | Check |
|---|---|---|
| Python 3.11+ | runs rea-team | `python3 --version` |
| Claude Code, signed in | runs the agents | `claude --version` |
| Docker Desktop | the clean room | `docker info` |
| Node 22.19+ / 24.11+ / 26+ and REA | only if a feature uses `mode = "rea"` | `node -v`, `rea --version` |
| A decompiler | only for `mode = "decompiled"` (e.g. Jira plugin JARs without source) | — |

```bash
brew install python@3.12
pipx install git+https://github.com/<you>/<repo>.git     # or: pip install -e '.[dev]' from a clone
rea-team --version
```

REA (only if you need it):

```bash
npx rea-agents@latest setup          # pick Claude Code, review the plan, approve
npm install --global rea-agents
```

---

## 4. Workspace layout

```bash
rea-team init my-rebuild && cd my-rebuild
```

```
my-rebuild/
  rea-team.toml                 settings (section 17)
  features.toml                 the feature list (section 9)
  features.proposed.toml        written by `plan`
  lab/
    .claude/agents/analyst.md, spec-writer.md, verifier.md
    target/                     the original app(s): install folders, binaries, source trees, JARs
    analysis/                   survey.md, features/<id>/evidence.md + report.md, decompiled/
    handoff/CONTRACTS.md        shared contracts
    handoff/features/<id>.md    one spec per feature
    rebuild/                    latest clean-room code + tests, copied back for the verifier
    .rea-team/                  state, logs, archives (section 15)
  clean/                        mounted into the container as /work
    .claude/agents/test-writer.md, rebuilder.md
    specs/  tests/acceptance/  src/  BUILD_STATE.md  .git
```

You can put several targets in `lab/target/`, for example `target/desktop-app`, `target/web-admin` and `target/jira-plugin`, and point each feature at one of them.

---

## 5. Set up the clean room container

The container needs: Claude Code (signed in), git, everything your stack needs to **build and test** the rebuild, and **no** REA. Start from `docs/clean-room/Dockerfile.example`.

```bash
docker build -t rea-clean-img -f docs/clean-room/Dockerfile.example .
docker run -d --name rea-clean \
  --cap-add=NET_ADMIN --cap-add=NET_RAW \
  -v "$PWD/clean:/work" -w /work \
  -e ANTHROPIC_API_KEY \
  rea-clean-img sleep infinity
```

Never mount `lab/`, your home folder or `~/.claude.json`. Mounting only `clean/` means everything the clean room produces is also visible on your Mac in `clean/`.

**Authentication.** Pass `ANTHROPIC_API_KEY`, or `docker exec -it rea-clean claude` once and sign in.

**Network.** The container needs the Anthropic API. It must not be able to fetch REA. There are two workable setups:

1. **Preinstall, then lock down (simplest).** Install every dependency at image build time, then apply an outbound allowlist (Claude Code's reference devcontainer firewall script is a good base) without the npm registry. This works when the stack is known up front.
2. **Filtered registry (most flexible).** Run a local npm proxy such as Verdaccio that serves normal packages but blocks `rea-agents`, and point the container's npm at it. The rebuilder can add libraries as it goes, and the wall check still passes because `npm view rea-agents` fails.

**Where things run.** The rebuilder, the test-writer and the **test gate** run inside this container, so gate tests must be self-contained: a database in the container (or in memory), and fakes for email, payment and other external services. The **verifier** runs in the lab on your Mac. It starts the original wherever it lives (locally, or a staging server over your VPN) and starts the rebuild either on your Mac or, better, in a throwaway container from this same image. Describe that in `[verify].rebuild` (section 21.3 has an example). Only the verifier needs your internal network. The clean room never does.

**Verify:**

```bash
rea-team doctor
```

It checks inside the container that `which rea`, `npx --no-install rea-agents`, `npm view rea-agents` and `test -e /lab` all **fail**, and that Claude and git work. rea-team repeats the wall check before every build.

---

## 6. Configure rea-team.toml

The settings that matter most (all keys are in section 17):

```toml
[project]
name = "acme-sitebuilder"
stack = "TypeScript on Node 22, Express + Postgres. Unit tests with Vitest. Functional tests with Playwright."
test_command = "npm test --prefix src && npx playwright test tests/acceptance"

[verify]
original = "The legacy app runs at https://legacy.internal.acme (test tenant, user qa@acme / see 1Password 'legacy-qa')."
rebuild  = "In lab/rebuild/src: `npm ci && npm start`; it listens on http://localhost:3000."
```

- **stack** is read by both the test-writer and the rebuilder, so the tests and the code agree on tools. Be specific.
- **test_command** is the regression gate. It runs from `/work` and must run unit tests and **all** of `tests/acceptance/`. It must exit non-zero when anything fails.
- **verify.original / verify.rebuild** are plain-language run instructions for the verifier. Include URLs, ports and test accounts. Never use production data.

---

## 7. Choose an analysis mode per target

Each feature has a `mode`. Choose by what you actually have:

| You have | Mode | The analyst reads |
|---|---|---|
| The legacy **source code** | `source` | the source tree in `lab/target/...` with Read/Grep. Fastest and most exact. REA isn't needed. |
| A **native** desktop binary (C/C++/Swift/Delphi…) | `rea` | REA `open_binary` + a decompiler engine (Ghidra/Hopper/IDA) |
| An **Electron** app or extracted JS bundle | `rea` | REA `analyze_javascript_application` (no native engine needed) |
| A **.NET** assembly | `rea` | REA `inspect_managed_artifact` |
| A **web app**, browser side only | `rea` | REA on the JS bundle, plus `capture_browser_scenario` or saved HAR captures via `inspect_web_network_capture` |
| A **Jira plugin JAR** with no source | `decompiled` | output of your Java decompiler (REA's docs don't name JAR support) |

**Web apps you own:** you have the server code too, so use `source` for the backend. For behavior that's easier to observe than to read, add notes to the feature (`notes = "Record a HAR of creating a site in the legacy UI: lab/target/captures/create-site.har"`), and the analyst will use it.

**Decompiled mode** needs `analysis.decompile_command`, which runs once per target before analysis:

```toml
[analysis]
decompile_command = "java -jar /opt/vineflower.jar {input} {output}"
```

**REA mode** needs the analyst's tool list filled in:

```bash
rea-team set-tools     # starts `rea --mcp`, asks for its tool list, writes every tool into the analyst
```

Other sources: `--from-mcp "path/to/rea --mcp"` for a different REA install, or `--tools open_binary,analyze_javascript_application,...` for a hand-picked list. `--from-doctor FILE` still reads a `rea doctor --json` file, but REA 6.x only reports a tool count there, so it usually finds nothing. The analyst also needs an MCP server named `rea` registered in Claude Code (`claude mcp list` from `lab/`; `rea setup --client claude_code` if it is missing).

If no feature uses REA, rea-team skips the REA and Node checks and removes the unused placeholder from the analyst's tools line by itself.

---

## 8. Plan: survey, features and contracts

```bash
rea-team plan target/legacy-web --mode source --authorized
```

1. **Survey.** The analyst maps the whole application (stack, modules, user-facing features, stored data, interfaces) into `analysis/survey.md`, and proposes features in `features.proposed.toml`. The proposal is validated and the step retried if it's malformed.
2. **Contracts.** The spec-writer writes `handoff/CONTRACTS.md`, with sections Conventions, Data Model and Public API. It's leak-checked, then snapshotted as the approved baseline.

`--authorized` records once, in the project state, that you own or are authorized to examine the targets. Plan several targets at once with `rea-team plan target/desktop target/web-admin`.

**Review the proposal before accepting it.** This is the single most valuable hour you'll spend:

- [ ] Each feature fits in one sentence a tester could check.
- [ ] Foundations come first: auth, core data and "create X" before "edit X" and "publish X".
- [ ] `depends_on` is complete. If feature B's tests need A's data, B depends on A.
- [ ] Nothing important is missing, such as admin screens, import/export or emails.
- [ ] Nothing is too big. "Editor" is a product, not a feature; "Insert an image block" is a feature.

**Review CONTRACTS.md:**

- [ ] The data model uses the names you want in the new system. This is your chance to fix legacy naming.
- [ ] Error format, id format, dates and auth are decided.
- [ ] Nothing describes internals. It's behavior and interfaces only.

Then accept it: copy or edit it into `features.toml`, or run `rea-team plan ... --accept`.

---

## 9. features.toml and how to slice features

```toml
[[feature]]
id = "create-site"                 # lowercase, digits, dashes
title = "Create a website"
scope = "A signed-in user creates a new website from a template and lands in the editor."
target = "target/legacy-web"       # inside lab/
mode = "source"                    # rea | source | decompiled (default: analysis.default_mode)
depends_on = ["sign-in"]
notes = "Templates live in the 'site_templates' table."   # optional hints for the analyst
```

rea-team validates on every run: unique ids, required fields, known modes, every dependency exists, no cycles. Errors name the exact feature and field.

**Sizing rules of thumb:**

| Too big | About right |
|---|---|
| "Site editor" | "Add, move and delete text blocks", "Insert an image block", "Undo/redo" |
| "Billing" | "Start a subscription", "Change plan", "Download invoices" |
| "Jira integration" | "Create issue from a form", "Show linked issues panel", "Admin settings page" |

A feature spec over `project.max_spec_chars` (60,000 characters by default) stops the run with `SpecTooLarge` and asks you to split it. That's deliberate: a spec that big won't fit comfortably in one session alongside the code.

---

## 10. Run the build

```bash
rea-team doctor          # all ✓ ?
rea-team run             # builds every unfinished feature in dependency order
rea-team status          # overview, any time (also from another terminal)
rea-team status add-page # detail for one feature
```

What happens for each feature (step keys in brackets):

1. **[analyst]** The target is fingerprinted, the analyst writes `analysis/features/<id>/evidence.md`, and the fingerprint is checked again. A modified target stops the run.
2. **[spec]** The spec-writer writes `handoff/features/<id>.md` and may append to `CONTRACTS.md`. Checks: the file is fresh, has all 7 sections, is under the size limit, and contracts were only appended to.
3. **[rN.leak]** The spec and contracts are scanned for addresses and internal names. The spec-writer gets to fix any it finds. Anything still leaking stops the run before it crosses.
4. **[rN.handoff]** The contracts and the spec are copied into `specs/` as read-only. The clean room's git repo is created on first use.
5. **[rN.wall]** The isolation probes run again.
6. **[rN.tests]** The test-writer writes `tests/acceptance/<id>/` from the spec. It must not touch other features' tests or `src/`. Afterwards the tests are made read-only and fingerprinted.
7. **[rN.build]** The rebuilder implements the feature and updates `BUILD_STATE.md`. Then the **test gate** runs `test_command`. If it fails, a fix session gets the last 120 lines of test output, up to `fix_attempts` times. After every session rea-team checks that tests and specs weren't modified.
8. **[rN.verify]** `src/` and `tests/` are copied back to `lab/rebuild/`. The verifier runs both apps, runs the acceptance tests against the **original** too, and compares results. It writes `report.md`, ending in `RESULT wrong=N crash=N spec_wrong=N`. rea-team checks that the verifier changed nothing.
9. All zeros means **[commit]**: a git commit plus a `rea/<id>` tag in the clean room, and the feature is marked passed. Otherwise comes **[rN.spec_update]**, where divergences become new Test Cases and the next round starts at step 3.

Run a subset with `rea-team run --feature add-page`. Unfinished dependencies are built first. `--keep-going` (or `project.keep_going = true`) skips the dependents of a failed feature and carries on with the independent ones.

---

## 11. Testing strategy

Four layers, each catching what the others can't:

| Layer | Written by | Runs | Catches |
|---|---|---|---|
| Unit tests | rebuilder | test gate | logic bugs inside the new code |
| Acceptance tests (functional) | test-writer, from the spec, before the code | test gate, every build | the feature doesn't do what the spec says |
| Regression | the same acceptance tests of **all** passed features | test gate, every build | the new feature broke an old one |
| Differential verification | verifier | once per round | the rebuild differs from the original, or the spec is wrong |

**Why a separate test-writer?** An agent that writes the code and the tests can make both wrong in the same way. The test-writer only sees the spec, and its tests are locked before the rebuilder starts.

**Why run acceptance tests against the original?** It checks the spec itself. A test that fails on the original means the spec or the test misread the legacy behavior. The verifier labels it `spec_wrong`, and the next round corrects the spec.

**APP_URL / APP_CMD.** Acceptance tests find the application through environment variables. That's what lets one suite run against the rebuild in the gate and against the original during verification. Make sure your `test_command` sets them for the rebuild. A web example:

```toml
test_command = "npm ci --prefix src && npm test --prefix src && (npm start --prefix src & p=$!; sleep 5; APP_URL=http://localhost:3000 npx playwright test tests/acceptance; s=$?; kill $p; exit $s)"
```

For anything longer than this, put a `run-tests.sh` in `clean/` (it's committed with the rebuild) and set `test_command = "sh run-tests.sh"`.

**Flaky tests** look like random regressions. Have the test-writer use stable selectors and explicit waits (add that to `stack`), and keep test data per test.

---

## 12. Recipes

### Web app (you have the source)

```toml
# rea-team.toml
[project]
stack = "TypeScript, Node 22, Express, Postgres (via docker-free pg-mem in tests). Vitest unit tests. Playwright functional tests."
test_command = "sh run-tests.sh"
[analysis]
default_mode = "source"
[verify]
original = "Legacy app: https://legacy-staging.acme.internal, test user qa@acme.test"
rebuild = "lab/rebuild/src: npm ci && npm start, http://localhost:3000, seeds user qa@acme.test"
```

```bash
cp -R ~/code/legacy-sitebuilder lab/target/legacy-web
rea-team plan target/legacy-web --mode source --authorized
# review, accept
rea-team run
```

### Desktop app (binary only, or Electron)

```toml
[project]
stack = "Python 3.12 CLI + Qt via PySide6. pytest unit tests. Functional tests drive the CLI and compare output files."
test_command = "cd src && pip install -e . -q && pytest -q && cd .. && APP_CMD='python -m newapp' pytest -q tests/acceptance"
[analysis]
default_mode = "rea"
[verify]
original = "Run lab/target/OldApp.app/Contents/MacOS/OldApp with the same CLI flags; GUI features: use the export files it writes."
rebuild = "cd lab/rebuild/src && pip install -e . && python -m newapp"
```

Desktop functional tests work best through whatever the app exposes outside its UI: the command line, file import/export, saved project files and settings files. For UI-only behavior, describe screens and states in the spec and test the resulting files and data.

### Jira plugin (JAR, with or without source)

With source, use `mode = "source"`. Without it:

```toml
[analysis]
default_mode = "decompiled"
decompile_command = "java -jar /opt/vineflower.jar {input} {output}"
[project]
stack = "Java 17, Atlassian Plugin SDK (atlas-*), JUnit 5. Functional tests call the plugin's REST endpoints on a local Jira (atlas-run)."
test_command = "sh run-tests.sh"   # build with atlas-package, start atlas-run, run REST tests with APP_URL, stop
```

Plugins are tested through what they expose: REST endpoints, web panels (Playwright against a local Jira) and stored configuration. A local Jira is heavy, so preinstall the SDK and the Jira dependencies in the clean-room image (section 5) and allow plenty of time in `timeouts.gate`.

---

## 13. When something fails

rea-team prints what failed, why and what to do next, then exits with a code you can script against. In `--keep-going` mode, **fatal** errors still stop everything.

| Exit | Error | What happened | What rea-team already did | What you do |
|---|---|---|---|---|
| 2 | `ParityNotReached` | still differs from the original after `max_rounds` | added the last divergences to the spec, rolled the clean room back | read `report.md` and the evidence unknowns. `rea-team run --extra-rounds 2`, or split the feature |
| 10 | `ConfigError` / `FeatureGraphError` | bad `rea-team.toml` or `features.toml` | nothing ran | fix the named key or feature |
| 11 | `PreflightError` | a doctor check failed | printed every check | fix the ✗ items |
| 12 | `AuthorizationError` | authorization not confirmed | nothing ran | pass `--authorized` (once per project) |
| 13 | `LockHeld` | another run is active here | nothing | wait. The lock frees itself if that process dies |
| 21 | `StepFailed` | a step still failed after retries (timeout, crash, no output) | rolled back, kept the lab work | read `logs/<id>/<step>.log`, fix, `rea-team run` |
| 21 | `GateFailed` | tests still failing after all fix sessions | archived every gate output, rolled back | read `features/<id>/round-N/gate-*.txt`. Often the spec is ambiguous, or an older feature's test is disputed (see `BUILD_STATE.md`) |
| 21 | `SpecTooLarge` | the feature spec is over the limit | nothing crossed the wall | split the feature, then `rea-team reset <id>` |
| 21 | `NoAcceptanceTests` | the test-writer produced nothing | rolled back | check its log, make `stack` clearer |
| 21 | `ReportFormatError` | the verifier's report has no RESULT line | retried | check the verifier log |
| 22 | `ContainerError` | Docker is down, or the container is missing or stopped | nothing in the container | `docker start rea-clean`, then `rea-team run` |
| 30 | `LeakDetected` | the spec still contains addresses or internal names after auto-fix | **nothing crossed** | edit the listed lines in `handoff/`, then `rea-team run` |
| 31 | `WallBreach` (fatal) | the container can reach REA, rea-agents or lab paths | stopped before any clean agent | fix the container (section 5) |
| 32 | `IntegrityError` (fatal) | an agent changed what it mustn't: the target, tests, specs or the rebuild | stopped, rolled back | inspect the log. Restore the target from a clean copy if needed |
| 32 | `ContractViolation` (fatal) | CONTRACTS.md was edited instead of appended to | stopped | restore from `lab/.rea-team/contracts/approved.md`. A real contract change needs a deliberate re-plan |
| 130 | interrupted | Ctrl-C, SIGTERM or a closed terminal | saved state, marked the feature interrupted | `rea-team run` |

**Retryable vs not.** Timeouts, non-zero exits, missing or stale outputs, malformed reports and missing spec sections are retried with backoff (`run.retries`, `run.retry_backoff_seconds`). Safety problems (leaks, the wall, integrity, contracts) are **never** retried, because retrying would mean trusting a broken guarantee.

**Hung agents.** Every step has a timeout (`[timeouts]`). On timeout the whole process group is killed, so no orphaned Claude processes are left, and the attempt is retried.

---

## 14. Resume, reset and extra rounds

- **Continue after any stop:** `rea-team run`. Passed features are skipped. A failed feature keeps its analysis and spec and redoes its clean-room work. An interrupted one continues at the step it was on.
- **Redo a feature from a step:** `rea-team reset add-page --from-step r2.build`. The keys are shown by `rea-team status add-page`.
- **Redo a feature completely:** `rea-team reset add-page`. If later features depend on it, you're told; their commits stay as they are.
- **More rounds:** `rea-team run --feature add-page --extra-rounds 2`.
- **Re-plan:** `rea-team plan ... --redo`. Use it sparingly once features have passed, because contracts are append-only on purpose.
- **Undo a bad state file:** `lab/.rea-team/project.json.bak` is written before every `run` and `reset`.

---

## 15. Files and logs reference

```
lab/.rea-team/
  project.json                 checkpoint state (authorization, plan steps, every feature)
  project.json.bak             copy taken before each run/reset
  lock                         workspace lock
  contracts/approved.md        last approved CONTRACTS.md; CONTRACTS-NNN.md = every version
  logs/plan/                   survey.log, contracts.log
  logs/<id>/                   analyst.log, spec.log, r1-test-writer.log, r1-rebuilder.log,
                               r1-fix-1.log, r1-gate.log, r1-verifier.log, docker.log, hash.log ...
  features/<id>/round-N/       CONTRACTS.md, <id>.md (spec as sent), report.md,
                               gate-0.txt, gate-1.txt ..., BUILD_STATE.md, QUESTIONS.md
lab/analysis/features/<id>/    evidence.md, report.md (latest)
clean/                         the rebuild itself: git log shows one commit per feature
```

Every log contains each attempt's command, exit status, stdout and stderr.

Suggested reading order when a feature fails: `status <id>`, then the newest `gate-*.txt` or `report.md`, then the evidence unknowns, then the step log.

---

## 16. Keeping context small and costs down

- **Small features.** This is the biggest lever on quality and cost.
- **Source mode whenever you have the source.** It's cheaper and more exact than decompiling.
- **Lean contracts.** Shared things only; feature details belong in feature specs.
- **BUILD_STATE.md under 200 lines.** The rebuilder is told to keep it that way. If it grows, ask for a cleanup or edit it yourself in `clean/`.
- **max_rounds = 3.** More rounds rarely help, because a persistent gap is usually missing evidence. Read the unknowns.
- **Start with one feature:** `rea-team run --feature <first-id>`. Check the quality of the spec, tests and code before letting it run through everything.

---

## 17. rea-team.toml reference

| Key | Default | Meaning |
|---|---|---|
| `lab_dir`, `clean_dir` | `lab`, `clean` | the two rooms. They must be separate folders, neither inside the other |
| `project.name` | — | shown in summaries |
| `project.stack` | — | tech stack for the test-writer and rebuilder |
| `project.test_command` | — (required) | the regression gate, run in the container from the workdir |
| `project.max_spec_chars` | 60000 | above this a feature spec must be split |
| `project.fix_attempts` | 3 | fix sessions after a failing gate |
| `project.keep_going` | false | continue with independent features after a failure |
| `analysis.default_mode` | `rea` | `rea` \| `source` \| `decompiled` |
| `analysis.decompile_command` | "" | `{input}` / `{output}` substituted; runs once per target |
| `verify.original` / `verify.rebuild` | — | run instructions for the verifier |
| `run.max_rounds` | 3 | rounds per feature |
| `run.retries` | 2 | extra attempts for retryable failures |
| `run.retry_backoff_seconds` | 15 | doubles each retry |
| `run.check_target_unchanged` | true | fingerprint the target around analysis (turn off for huge targets) |
| `timeouts.*` | see file | seconds per attempt: analyst, spec_writer, test_writer, rebuilder, gate, verifier, docker |
| `claude.command` / `claude.args` | `claude`, `--permission-mode acceptEdits` | lab-side Claude Code |
| `container.name` / `workdir` | `rea-clean`, `/work` | the clean room |
| `container.claude_command` / `args` | as above | Claude Code inside the container |
| `container.forbidden_paths` | `/lab`, `/target`, `/analysis` | must not exist in the container |
| `leak_check.patterns` | addresses, `sub_`, `FUN_`, `DAT_`… | regexes that must never cross. Add your own internal prefixes (e.g. legacy class-name prefixes) |

---

## 18. Command reference

| Command | Does |
|---|---|
| `rea-team init [dir] [--force]` | creates the workspace, agent files, config and example features.toml |
| `rea-team doctor [--target T]` | checks every prerequisite: tools, agents, features, contracts, container, wall |
| `rea-team set-tools [--from-mcp CMD \| --from-doctor F \| --tools a,b]` | fills in the analyst's REA tools |
| `rea-team plan T... [--mode M] [--accept [--force]] [--redo] [--authorized]` | survey, proposed features and contracts |
| `rea-team run [--feature ID]... [--keep-going] [--extra-rounds N] [--authorized] [--skip-preflight]` | builds features, continuing from checkpoints (`resume` is an alias) |
| `rea-team reset ID [--from-step KEY]` | forgets progress so a feature runs again |
| `rea-team status [ID]` | overview, or one feature's steps, rounds, gate runs, commit and error |
| `-v / --verbose` | debug logging and full tracebacks |

---

## 19. FAQ

**Do I need REA at all?** Only for targets where you don't have source: native binaries, Electron bundles, .NET, or behavior that's easier to capture than to read. For your own source trees, use `mode = "source"`.

**Why a container for my own code?** So the rebuild can't copy the legacy implementation, quirks and bugs included. The specs force a fresh design that's checked against behavior.

**Can I edit specs or tests myself?** Yes, in `lab/handoff/` before the handoff. Then `rea-team reset <id> --from-step r1.leak` (or the current round) so they go through the leak check and get re-sent. Don't edit files in `clean/specs` or `clean/tests` directly, because fingerprint checks will stop the run.

**A test is genuinely wrong.** The rebuilder notes it under "Disputed tests" in `BUILD_STATE.md` and the verifier may label it `spec_wrong`. Fix the Test Case in the spec, then reset the feature from its `rN.leak` step.

**Can two features run in parallel?** Not in one workspace. The lock prevents it, because the clean room and the git history are shared. To parallelize, split the work into separate workspaces with separate containers and merge the results yourself.

**How do I see the new code?** It's in `clean/` on your Mac, with git history: `cd clean && git log --oneline`.

---

## 20. Limits

- Agents can misread behavior. The verifier and the acceptance tests catch most of it, but review what comes out, especially the specs of core features.
- REA's analysis of shipped artifacts is a reconstruction, not the original source. Prefer `source` mode wherever you can.
- UI-heavy desktop behavior is harder to test automatically than web or CLI behavior. Design specs around observable outputs.
- The wall check catches common breaches. It doesn't replace building the container correctly.
- Use test environments and test accounts for the original's runtime. Never production.

---

## 21. End-to-end walkthrough: a sample web app

This walkthrough rebuilds a small legacy web app from start to finish. Every command, file and output is shown in the shape you'll actually see. Swap in your own app as you go.

### 21.1 The sample app

**Notely** is a legacy notes app you own: Node 14, Express, EJS server-rendered pages, jQuery, SQLite. Its source is in `~/code/notely`. Users sign in, create notes, edit them, search them and share a read-only link. The target is a modern TypeScript rebuild with the same behavior.

Where things run (see "Where things run" in section 5):

| What | Where | Address |
|---|---|---|
| Original Notely | your Mac | `http://localhost:4000` |
| Rebuild during the **test gate** | clean container | `http://localhost:3000` inside the container |
| Rebuild during **verification** | a throwaway container started by the verifier | `http://localhost:3001` on your Mac |

Nothing needs your company network. Everything is local.

### 21.2 Create the workspace

```bash
rea-team init notely-rebuild && cd notely-rebuild
cp -R ~/code/notely lab/target/notely
```

Make sure the original runs, because the verifier will need it:

```bash
cd lab/target/notely && npm ci && PORT=4000 DATABASE_FILE=/tmp/notely-legacy.db npm start
# open http://localhost:4000, sign in with the seeded user demo@notely.test / demo1234, then Ctrl-C
cd ../../..
```

### 21.3 Configure

Edit `rea-team.toml`:

```toml
[project]
name = "notely"
stack = """TypeScript on Node 22. Express 5 + server-rendered HTML (no SPA framework).
SQLite via better-sqlite3; the database file path comes from env DATABASE_FILE.
Port from env PORT. GET /health returns 200 when ready.
Unit tests: Vitest, in src/. Functional tests: Playwright (@playwright/test), TypeScript,
in tests/acceptance/<feature>/, base URL from env APP_URL, stable selectors
(getByRole / getByLabel), explicit waits, each test creates its own user and data."""
test_command = "sh run-tests.sh"
max_spec_chars = 60000
fix_attempts = 3
keep_going = false

[analysis]
default_mode = "source"        # we have Notely's source code; no REA needed

[verify]
original = """Original: in lab/target/notely run `npm ci` once, then
`PORT=4000 DATABASE_FILE=$(mktemp) npm start` in the background; it is ready when
http://localhost:4000/login returns 200. Seeded user: demo@notely.test / demo1234.
Stop it when you are done."""
rebuild = """Rebuild: start it in a throwaway container so nothing runs on the Mac directly:
docker run --rm -d --name notely-verify -p 3001:3000 -e PORT=3000 -e DATABASE_FILE=/tmp/v.db
  -v "$PWD/rebuild/src:/app" -w /app rea-clean-img sh -c "npm ci && npm start"
Ready when http://localhost:3001/health returns 200. Stop with `docker rm -f notely-verify`.
Run acceptance tests with APP_URL=http://localhost:3001 (rebuild) or APP_URL=http://localhost:4000 (original),
from lab/rebuild with `npx playwright test tests/acceptance/<id>`."""
```

The verifier runs Playwright from your Mac against both apps, so install it once next to where the tests get copied (`lab/rebuild/` survives between rounds; only its `src/` and `tests/` are replaced):

```bash
mkdir -p lab/rebuild && cd lab/rebuild && npm init -y >/dev/null && npm i -D @playwright/test && npx playwright install chromium && cd ../..
```

Folders that running things creates, like `node_modules/` and `test-results/`, are ignored by the check that the verifier didn't modify the rebuild. Edits to the rebuild's source or tests still stop the run.

### 21.4 The clean room

**Image.** `docs/clean-room/Dockerfile.example` already installs Node 22, git, curl, Claude Code and Playwright with Chromium. Add the SQLite build tools:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ && rm -rf /var/lib/apt/lists/*
```

```bash
docker build -t rea-clean-img -f docs/clean-room/Dockerfile.example .
```

**Shared test tooling.** Create `clean/package.json`, so the acceptance tests have Playwright no matter what the rebuilder does in `src/`:

```json
{ "private": true, "devDependencies": { "@playwright/test": "^1.50.0" } }
```

**The regression gate script.** Create `clean/run-tests.sh`. The gate runs it from `/work` on every build:

```sh
#!/bin/sh
# Regression gate: unit tests + EVERY feature's acceptance tests. Exit 0 only if all pass.
set -e
cd /work/src
npm ci --silent
npm test                                   # Vitest unit tests

export DATABASE_FILE="$(mktemp)"           # fresh database every gate run
PORT=3000 npm start > /tmp/notely.log 2>&1 &
APP=$!
trap 'kill $APP 2>/dev/null; true' EXIT
for i in $(seq 1 30); do curl -sf http://localhost:3000/health >/dev/null && break; sleep 1; done
curl -sf http://localhost:3000/health >/dev/null || { echo "app did not start:"; cat /tmp/notely.log; exit 1; }

cd /work
APP_URL=http://localhost:3000 npx playwright test tests/acceptance --reporter=line
```

**Start the container**, install the shared tooling while npm is still reachable, then turn on the firewall (or point npm at your filtered proxy, section 5):

```bash
docker run -d --name rea-clean --cap-add=NET_ADMIN --cap-add=NET_RAW \
  -v "$PWD/clean:/work" -w /work -e ANTHROPIC_API_KEY rea-clean-img sleep infinity
docker exec rea-clean npm install          # @playwright/test into /work/node_modules
docker exec rea-clean sudo /usr/local/bin/init-firewall.sh   # your allowlist, without registry.npmjs.org
```

With a filtered npm proxy instead of a hard firewall, the rebuilder can `npm install express` as it goes. With the hard firewall, preinstall the libraries you expect it to use in the image.

**Check it:**

```bash
rea-team doctor
```

```
  ✓ claude               2.x (Claude Code)
  ✓ docker               Docker version 27.x
  ✓ analyst agent        no REA needed; the unused placeholder is removed at run time
  ✓ spec-writer agent
  ✓ verifier agent
  ✓ test-writer agent
  ✓ rebuilder agent
  ✓ clean container      rea-clean
  ✓ claude in container
  ✓ git in container
  ✓ clean-room wall      rea, npm and lab unreachable

All checks passed.
```

### 21.5 Plan

```bash
rea-team plan target/notely --mode source --authorized
```

```
09:00:53 no feature uses REA: removed the unused REA placeholder from the analyst's tools
09:00:53 → plan.survey
09:14:20 → plan.contracts
09:21:02 ✓ plan done. Review features.proposed.toml and handoff/CONTRACTS.md, then save the list as features.toml

Proposed 6 features (build order):
  sign-in                      Sign in and out
  create-note                  Create a note                 ← sign-in
  edit-note                    Edit and delete a note        ← create-note
  list-notes                   List notes, newest first      ← create-note
  search-notes                 Search notes                  ← list-notes
  share-note                   Share a read-only link        ← create-note
```

**Review `features.proposed.toml`.** Here the survey lumped sign-up into `sign-in`. Split it, because sign-up has its own validation rules and emails:

```toml
[[feature]]
id = "sign-up"
title = "Create an account"
scope = "A visitor creates an account with email and password and is signed in."
target = "target/notely"
mode = "source"

[[feature]]
id = "sign-in"
title = "Sign in and out"
scope = "A user with an account signs in with email and password, and signs out."
target = "target/notely"
mode = "source"
depends_on = ["sign-up"]

[[feature]]
id = "create-note"
title = "Create a note"
scope = "A signed-in user creates a note with a title and a body and sees it saved."
target = "target/notely"
mode = "source"
depends_on = ["sign-in"]
notes = "Legacy trims titles and limits them to 120 characters; check routes/notes.js."
# ... edit-note, list-notes, search-notes, share-note as proposed
```

Save it as `features.toml`.

**Review `lab/handoff/CONTRACTS.md`.** An excerpt of what you should see, and what to fix:

```markdown
# Notely contracts

## Conventions
- All pages are server-rendered HTML. Forms POST and redirect (303) on success.
- Validation errors re-render the form with HTTP 422 and a message in an element with role="alert".
- IDs are opaque strings. Timestamps are ISO-8601 UTC.
- GET /health returns 200 "ok" when the app is ready.
- Sessions: cookie "sid", HttpOnly, expires after 14 days of inactivity.

## Data Model
- User: id, email (unique, case-insensitive), password (hashed; never returned), created_at
- Note: id, owner (User.id), title (1–120 chars, trimmed), body (0–20000 chars), created_at, updated_at

## Public API
- GET  /login, POST /login, POST /logout
- GET  /notes, GET /notes/new, POST /notes, GET /notes/:id
```

Check that the names are the ones you want going forward. If the legacy app called notes `memos` internally, the contracts are where you say `Note` for good. They're append-only once features build on them.

### 21.6 Build the first feature only

Start small. Check the quality of the spec, tests and code before letting it run through everything.

```bash
rea-team run --feature sign-up
```

```
09:30:01 ━━ feature sign-up: Create an account
09:30:01   → analyst
09:41:37   ✓ analyst (696s)
09:41:37   → spec
09:47:12   ✓ spec (335s)
09:47:12   → r1.leak
09:47:12   ✓ r1.leak (0s)
09:47:12   → r1.handoff
09:47:14   ✓ r1.handoff (2s)
09:47:14   → r1.wall
09:47:20   ✓ r1.wall (6s)
09:47:20   → r1.tests
09:55:03   ✓ r1.tests (463s)
09:55:03   → r1.build
10:19:48     gate passed (unit + all acceptance tests)
10:19:48   ✓ r1.build (1485s)
10:19:48   → r1.verify
10:34:10     verify: wrong=0 crash=0 spec_wrong=0
10:34:10   ✓ r1.verify (862s)
10:34:10   → commit
10:34:11   ✓ commit (1s)
10:34:11 ━━ sign-up PASSED (commit 3e1a9c2)

notely: features in build order
  ✓ sign-up                      passed      round 1: wrong=0 crash=0  commit 3e1a9c2
  · sign-in                      pending
  ...
```

(Timings are illustrative. Real ones depend on the app and the model.)

**Inspect what was produced:**

`lab/handoff/features/sign-up.md` (excerpt):

```markdown
# Feature: sign-up

## Architecture
Adds GET /signup and POST /signup. Uses User from CONTRACTS.md. On success the user is signed in
(session cookie per Conventions) and redirected to /notes.

## Errors
- Email already registered (any letter case) → 422, alert "An account with this email already exists."
- Password shorter than 8 characters → 422, alert "Password must be at least 8 characters."
- Invalid email → 422, alert "Enter a valid email address."

## Test Cases
- TC-1 New email + valid password → 303 to /notes; the page shows the email in the header.
- TC-2 Same email in different case (Ana@x.test after ana@x.test) → 422 with the duplicate message.
- TC-3 Password "short" → 422 with the length message; the email field keeps its value.
- TC-4 Email "not-an-email" → 422 with the email message.

## Open questions
- The legacy code sends a welcome email through a provider that isn't in the shipped source. Not tested.
```

`clean/tests/acceptance/sign-up/sign-up.spec.ts`, written by the test-writer from the spec only:

```ts
// Open questions not tested: welcome email.
import { test, expect } from "@playwright/test";

const base = process.env.APP_URL!;
const unique = () => `u${Date.now()}${Math.floor(Math.random() * 1e6)}@notely.test`;

test("TC-1 new account lands on notes, signed in", async ({ page }) => {
  const email = unique();
  await page.goto(`${base}/signup`);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("correct-horse");
  await page.getByRole("button", { name: /sign up/i }).click();
  await expect(page).toHaveURL(/\/notes$/);
  await expect(page.getByText(email)).toBeVisible();
});

test("TC-2 duplicate email is rejected regardless of case", async ({ page }) => {
  const email = unique();
  for (const e of [email, email.toUpperCase()]) {
    await page.goto(`${base}/signup`);
    await page.getByLabel("Email").fill(e);
    await page.getByLabel("Password").fill("correct-horse");
    await page.getByRole("button", { name: /sign up/i }).click();
    if (e !== email) {
      await expect(page.getByRole("alert")).toHaveText("An account with this email already exists.");
    } else {
      await page.getByRole("button", { name: /sign out/i }).click();
    }
  }
});
// TC-3, TC-4 ...
```

Because these tests only use `APP_URL` and what a user sees, the verifier runs the very same file against the original at `localhost:4000`. If TC-2 failed there, the legacy app doesn't actually ignore case: that's a `spec_wrong`, and the next round fixes the spec.

`clean/BUILD_STATE.md`, the next session's memory:

```markdown
# Build state
## Features done
- sign-up: GET/POST /signup, signs the user in, redirects to /notes
## How to run
cd src && npm ci && PORT=3000 DATABASE_FILE=/tmp/dev.db npm start · tests: sh /work/run-tests.sh
## Code map
src/server.ts (app + routes mount) · src/db.ts (better-sqlite3, migrations in src/migrations/)
src/auth/ (signup, session middleware) · src/views/ (HTML templates)
## Interfaces
requireUser(req) → User | redirect to /login · db.users.findByEmail(email) (case-insensitive)
## Decisions
Passwords: argon2id. Sessions stored in SQLite table sessions. Email normalized to lowercase on write.
## Disputed tests and known gaps
none
```

`lab/analysis/features/sign-up/report.md` (excerpt):

```
ordinary    24 / 24   100%
edge        12 / 12   100%
malformed    9 /  9   100%
acceptance on original   4 / 4
RESULT wrong=0 crash=0 spec_wrong=0
```

The code is in `clean/src` on your Mac:

```bash
cd clean && git log --oneline
3e1a9c2 feature sign-up: Create an account
a01b2c3 rea-team: clean room baseline
```

### 21.7 Run the rest

Happy with the first feature? Let it run through everything:

```bash
rea-team run
```

It continues with `sign-in`, `create-note` and the rest, in dependency order, each in fresh sessions. You can watch from another terminal with `rea-team status`.

### 21.8 What a regression looks like

Say that while building `search-notes`, the rebuilder changes how notes are stored and breaks `list-notes`' ordering. The gate catches it:

```
13:02:44   → r1.build
13:31:10     gate failed (exit 1), fix session 1/3
13:44:52     gate passed (unit + all acceptance tests)
13:44:52   ✓ r1.build (2528s)
```

The first gate output is saved in `lab/.rea-team/features/search-notes/round-1/gate-0.txt`:

```
  ✘ [chromium] › list-notes/list-notes.spec.ts › TC-2 notes are listed newest first
    Expected: "Third", "Second", "First"   Received: "First", "Second", "Third"
  1 failed, 31 passed
```

The fix session got those lines, fixed the ordering without touching any test, and the whole suite passed. If it hadn't passed after 3 fix sessions, rea-team would have stopped with `GateFailed` and rolled `clean/` back to the `list-notes` commit, and `search-notes` would start fresh on the next `rea-team run`.

### 21.9 What a divergence looks like

The verifier finds that searching for `café` matches `cafe` in the original but not in the rebuild:

```
13:59:31     verify: wrong=1 crash=0 spec_wrong=0
13:59:31   ✓ r1.verify (881s)
13:59:31   → r1.spec_update
14:03:02   ✓ r1.spec_update (211s)
14:03:02   → r2.leak
...
14:21:40   → r2.tests                       ← the test-writer adds a test for the new case
14:33:15   → r2.build                       ← the rebuilder makes it pass
14:58:02     verify: wrong=0 crash=0 spec_wrong=0
14:58:03 ━━ search-notes PASSED (commit 7d0e4f1)
```

The spec gained a case, written as behavior:

```markdown
- TC-7 Search is accent-insensitive: a note titled "Café plans" is found by "cafe" and by "café".
```

### 21.10 Finish

```bash
rea-team status
```

```
plan: survey, contracts  (authorized 2026-11-24T09:00:41)

notely: features in build order
  ✓ sign-up                      passed      round 1: wrong=0 crash=0  commit 3e1a9c2
  ✓ sign-in                      passed      round 1: wrong=0 crash=0  commit 51aa0b9
  ✓ create-note                  passed      round 1: wrong=0 crash=0  commit 8c3d2e1
  ✓ edit-note                    passed      round 1: wrong=0 crash=0  commit 0f9b7a4
  ✓ list-notes                   passed      round 1: wrong=0 crash=0  commit c4e81d0
  ✓ search-notes                 passed      round 2: wrong=0 crash=0  commit 7d0e4f1
  ✓ share-note                   passed      round 1: wrong=0 crash=0  commit e2b6c93
  7 passed
```

The rebuild is a normal git repo in `clean/`, with one commit per feature, a full acceptance suite in `clean/tests/acceptance/` and `BUILD_STATE.md` describing the code. Copy it into your own repository, add CI that runs `sh run-tests.sh`, and keep the acceptance suite as the regression net for future changes.

**Before you ship, read** each feature's `QUESTIONS.md` (in `lab/.rea-team/features/<id>/round-N/`) and every spec's **Open questions**. They list the behavior nobody could prove from the legacy app, like the welcome email in `sign-up`. Those are decisions for you, not for the agents.
