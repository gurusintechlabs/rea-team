# rea-team

Rebuild legacy software **feature by feature** with Claude Code, in a clean room, with automatic regression testing. Native binaries, Electron and .NET apps, web apps and Jira plugins can be analyzed from source, through [REA](https://github.com/morluto/rea), or from decompiler output.

```
plan:     survey → proposed features → CONTRACTS.md (shared, append-only)
feature:  analyst → spec → leak check → handoff ═╗
                                                 ║  clean room (container, no REA, no lab)
                      test-writer → acceptance tests (locked)
                      rebuilder → code + BUILD_STATE.md
                      TEST GATE: unit + every feature's acceptance tests (regression)
          verifier ◄═╝ original vs rebuild, same inputs → new test cases → next round
          pass → git commit + tag rea/<id>        fail → roll back to last passed feature
```

**Why it scales:**
- Each agent session is fresh and gets only CONTRACTS.md, one feature spec, the short BUILD_STATE.md handoff note and the code it touches.
- A feature only passes when its own tests and **every earlier feature's** tests pass, and it matches the original.
- Every step is checkpointed. `rea-team run` always continues where it stopped.

```bash
pipx install git+https://github.com/<you>/<repo>.git
rea-team init my-rebuild && cd my-rebuild
cp -R ~/code/legacy-app lab/target/legacy-app      # source tree, install folder, binary or JAR
# edit rea-team.toml: [project] stack + test_command, [verify]
# start the clean container (docs/clean-room/Dockerfile.example)
rea-team doctor
rea-team plan target/legacy-app --mode source --authorized    # review, then --accept
rea-team run
rea-team status
```

**Read the full [User Guide](docs/USER_GUIDE.md).** It covers setup, the clean-room container, analysis modes for each kind of app, slicing features, the testing strategy, recipes for web, desktop and Jira plugins, every error with its fix, and the configuration reference.

> Use it only on software you own or are authorized to examine. The first `plan` or `run` records that you confirmed this.

## Development

```bash
pip install -e '.[dev]' && pytest
```

The 48 tests include a fake clean room that runs real shell, git and a real regression gate. They cover multi-feature builds, regressions caught and fixed, rollback, `keep_going`, rounds to parity, retries, leaks, contract rewrites, tampered tests, wall breaches, oversized specs, decompiled mode, and CLI subprocess runs with SIGTERM and resume.
