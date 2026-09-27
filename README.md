# codex-lab

Minimal experiment harness for measuring **interactive coding-agent** development on an isolated Git worktree.

## Commands

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .

codex-lab doctor
codex-lab run --repo ~/projects/lockify --prompt "Add creator subscriptions..."
codex-lab report RUN-001 --repo ~/projects/lockify
codex-lab intervene RUN-001 --repo ~/projects/lockify --reason "I clarified the subscription expiry rule"
```

`run`:
1. refuses a dirty source repository;
2. creates `codex-lab/RUN-XXX` as a separate Git worktree;
3. starts the normal interactive `codex` command inside that worktree;
4. independently runs a detected test command after Codex exits;
5. retries failed verification up to 3 times with a recovery prompt;
6. records results in `~/.codex-lab/runs.sqlite3` (or `$CODEX_LAB_DB`).

The source repository is not modified by the database.

The source branch is never checked out or modified by the harness.

## Measurement rules

- `total_duration`: wall-clock harness time.
- `implementation_duration`: time the interactive Codex process was running. Planning/human time is not guessed.
- failures/retries/recovery: recorded from Codex process failures and independent verifier failures.
- tests/build: `./gradlew test`, `./mvnw test`, `mvn test`, `npm test`, or `python -m pytest`, whichever is detected first.
- files/lines: Git diff in the experiment worktree.
- commits: Git history visible from the worktree; exact experiment-only commit attribution is intentionally not guessed.
- interventions: only explicit `codex-lab intervene` calls are counted.
- model/tokens/cost: unavailable unless a reliable telemetry source is integrated; no estimates.

## Important limitation

The interactive Codex UI is intentionally left intact. `codex-lab` does not try to parse terminal keystrokes, infer when you personally intervened, or inject undocumented Codex flags. The current implementation starts the installed `codex` executable with the task as its positional prompt. Before doing so, `doctor`/runtime discovery checks whether `codex` exists.

If `codex` is not installed in the environment where `codex-lab` is executed, the run is recorded as `blocked` rather than pretending an experiment occurred.


## Agents

`codex-lab` uses an agent adapter boundary so the experiment harness is not tied to one vendor.

Supported interactive agents:

- `codex` — launches the installed Codex CLI interactively. On elevated Windows terminals, it uses `--no-daemon` only when the installed CLI advertises that flag.
- `claude` — launches Claude Code as `claude "<prompt>"`, which Anthropic documents as an interactive REPL with an initial prompt.
- `antigravity` — launches the documented `agy` interactive TUI directly in the current Windows terminal and automatically types/submits the initial prompt using the Windows keyboard input API. `-p` is deliberately not used because that is Antigravity's headless mode. The TUI is not proxied through Python, so normal interactive rendering and later human input remain direct.

On Windows, the automatic prompt injection waits 1.5 seconds by default for the TUI prompt panel to initialize. Override with `CODEX_LAB_ANTIGRAVITY_PROMPT_DELAY` (seconds) if your environment starts Antigravity more slowly or quickly.

Run: `codex-lab run --agent codex|claude|antigravity --repo <repo> --prompt "..."`

The Git worktree, independent verifier, SQLite metrics, diff/commit measurements, and reporting are shared across agents. Agent-specific telemetry is collected only when the agent exposes a reliable machine-readable source; otherwise it is recorded as unavailable.

## Antigravity workspace trust

For Antigravity runs, `codex-lab` uses a **stable worktree path per repository and agent** instead of generating a random temporary directory for every run. Before launching `agy`, it adds that exact path to:

`~/.gemini/antigravity-cli/settings.json` → `trustedWorkspaces`

Existing Antigravity settings are preserved, duplicate workspace entries are avoided, and the file is updated atomically. The automated headless stream path uses `--dangerously-skip-permissions` so Antigravity can execute without interactive permission prompts; the interactive path is unchanged.

The Git branch remains unique per experiment (`codex-lab/RUN-XXX`), so experiments remain isolated even though the filesystem path is reused. Before reuse, `codex-lab` runs `git worktree prune` and removes any stale registration occupying the stable slot. The default stable workspace root is `~/.codex-lab/workspaces`; override it with `CODEX_LAB_WORKTREE_ROOT` if needed.

Because the stable slot is reset before a new run, do not rely on the previous run's uncommitted files remaining in that slot. Preserve anything you need before starting another experiment.


### Antigravity debugging

Interactive Antigravity uses a Windows PTY bridge. Diagnostics are printed with `[antigravity-debug ...]` and appended to `~/.codex-lab/logs/antigravity-input.log`. The log records launch, PTY PID, readiness detection, prompt injection method, character count, and failures without logging the prompt contents.

## Antigravity automated mode (v10)

The default `--agent antigravity` run uses Antigravity's documented headless `--print --output-format stream-json` protocol with `--dangerously-skip-permissions`. The adapter reads the terminal `result` event, records token usage and tool-step counts, and lets `agy` exit normally; no `/exit` is required and this automated path does not use pywinpty.

The previous v7 interactive PTY implementation remains in `codex_lab/agents.py` as `_run_antigravity_interactive` for future/manual interactive experiments.

Environment variable:
- `CODEX_LAB_ANTIGRAVITY_PRINT_TIMEOUT` — Antigravity `--print-timeout`, default `60m`.


## Flutter verification (v10)

For Flutter projects, `flutter analyze` diagnostics are parsed by severity. Analyzer `error` diagnostics fail verification; `warning` and `info` diagnostics are recorded but do not fail the run. The aggregate `N issues found` count is not used as the pass/fail criterion. SQLite stores `analyzer_errors`, `analyzer_warnings`, and `analyzer_infos` for each run.

## v10 run numbering

v10 uses a version-scoped SQLite database (`~/.codex-lab/runs-v10.sqlite3`). A fresh v10 installation therefore starts at `RUN-001`; subsequent v10 runs increment from there without inheriting v9 numbering. The previous database is not deleted. Existing v9 databases are not modified.
