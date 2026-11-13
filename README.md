# Spec Kit Autopilot

Spec Kit Autopilot takes one feature from "start" to "merged" with no human at the keyboard. It drives a
[GitHub Spec Kit](https://github.com/github/spec-kit) repo through the whole pipeline in its own process, using the
[Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview):

```
feature_start → clarify → plan → tasks → analyze → implement → review → ship → linear
```

- **Workers** (Claude Code sessions) run your repo's own commands: a feature-start command, `speckit.clarify`,
  `speckit.plan`, `speckit.tasks`, `speckit.analyze`, `speckit.implement`, a review command and a ship command.
- **The judge** (a fresh, read-only Claude call per decision) answers every question a command asks, decides
  whether a phase met its Definition of Done, and routes back to an earlier phase when the root cause is upstream.
  Every decision is logged to `decisions.md`, which ships with the feature.
- **Deterministic gates** (plain code over files, git, `gh` and your test / lint / typecheck / build commands)
  decide whether a phase passes. The judge can add a semantic check on top, but it can never overrule a failed gate.
- **A safety policy** checks every tool call and denies dangerous ones (force pushes, `ssh`, bulk `git add`, edits to
  the constitution, CI workflows or lockfiles, pushes outside ship, Linear writes outside the close-out, …).
- **An OS sandbox** (macOS Seatbelt, Linux bubblewrap) confines worker Bash and the supervisor's own test runs:
  writes only inside the repo, no credential files, network only to package registries.
- **Linear (optional).** With an issue key and a Linear MCP server, the run reads the issue at start, comments on
  halts and closes the issue out after the merge. Without one, it runs on a plain description.

It also ships as a **Claude Code plugin** with an `/autopilot` skill that launches, watches and configures runs
from inside Claude Code.

How it works in depth: [docs/DESIGN.md](docs/DESIGN.md). Every setting: [docs/CONFIG.md](docs/CONFIG.md).

Contents: [Requirements](#requirements) · [Install](#install) · [Quick start](#quick-start) ·
[/autopilot in Claude Code](#autopilot-in-claude-code) · [CLI flags](#cli-flags) · [Live progress](#live-progress) ·
[Reading a run](#reading-a-run) · [Human-owned work](#human-owned-work) · [Resuming](#resuming) ·
[Safety](#safety) · [Repo-side commands](#repo-side-commands) · [Limits](#limits) · [Troubleshooting](#troubleshooting)

## Requirements

- **Python ≥ 3.11** (macOS or Linux).
- **Claude Code login.** The Agent SDK spawns its bundled Claude Code CLI, which uses the same login as your
  interactive `claude`. If `ANTHROPIC_API_KEY` is set, the CLI uses it instead and bills the API.
- **A Spec Kit repo**: `.specify/` initialised, the Spec Kit commands installed under `.claude/`, and a filled-in
  `.specify/memory/constitution.md` (the autopilot never edits it).
- **`git`** and **`gh`** (logged in: `gh auth status`): ship pushes, opens and merges a PR.
- **The repo's own toolchain** on `PATH` (node/pnpm, uv, docker, …) and whatever services its tests need.
- Optional: a **Linear MCP server** configured in Claude Code; **`jq`** for the `/autopilot` watcher;
  **bubblewrap + socat** on Linux for the worker sandbox.

## Install

**CLI** (the supervisor):

```bash
uv tool install git+https://github.com/amrramadan/speckit-autopilot
# or
pipx install git+https://github.com/amrramadan/speckit-autopilot

speckit-autopilot version
```

**Claude Code plugin** (the `/autopilot` skill), from inside Claude Code:

```
/plugin marketplace add amrramadan/speckit-autopilot
/plugin install speckit-autopilot@speckit-autopilot
```

The plugin only launches and watches; it needs the CLI on `PATH`.

## Quick start

Run the autopilot in a **dedicated clone** of the repo, not in a checkout you are working in: it refuses to start
on a dirty tree, and it switches branches, commits and merges in the directory it runs in. (A `git worktree` is
awkward: the run must start on the base branch, which your main checkout usually holds.)

```bash
git clone https://github.com/you/my-app ~/autopilot/my-app && cd ~/autopilot/my-app
# install dependencies and git hooks, copy the .env files your tests read, start services

# 1. Prepare the repo: config template, missing pipeline skills, .git/info/exclude, status line
speckit-autopilot init --statusline

# 2. Preflight only: seconds, one tiny judge call (and one cheap Linear read when Linear is on)
speckit-autopilot --issue ENG-142 "Add product filters" --dry-run

# 3. Run it, detached (survives the terminal), stopping before the merge
speckit-autopilot --issue ENG-142 "Add product filters" --until ship --detach

# without Linear: no issue key, the run is labelled by the description
speckit-autopilot --no-linear "Add product filters" --detach

# 4. Watch, inspect, stop
speckit-autopilot --watch          # live view (Ctrl-C quits)
speckit-autopilot --status         # one-shot summary
speckit-autopilot --report         # REPORT.md + decisions.md
speckit-autopilot --stop           # SIGTERM; resumable

# 5. Continue past the --until stop (ship + close-out)
speckit-autopilot --resume --detach
```

`--repo PATH` targets another directory; without it the current directory must be a git repo.

`init` writes `.autopilot.toml` (commit it), adds `.autopilot/` to `.git/info/exclude`, installs the generic
`autopilot-feature-start` / `autopilot-review` / `autopilot-ship` skills into `.claude/skills/` for any of those
steps the repo has no command for, and with `--statusline` installs the status line. It sets `linear = "on"` when it
finds a Linear MCP server, else `"off"`. If Linear is on, also set `linear_workspace_url` (see
[CONFIG.md](docs/CONFIG.md#linear)) or preflight fails.

Every real run (and every `--resume` / `--from`) runs the same preflight first; if any item FAILs, nothing starts
and the result is saved to `.autopilot/PREFLIGHT.md`.

**Exit codes:** `0` completed or paused, `2` halted, `3` stopped (resumable), `1` anything else (including a failed
preflight). `--dry-run` exits `0` when no item FAILs.

## /autopilot in Claude Code

The skill wraps the CLI: it always launches detached, then watches `run.jsonl` while you are there and reports phase
transitions. It never runs pipeline steps itself; close the session and the run continues.

| You type | What happens |
|---|---|
| `/autopilot ENG-142 "Add product filters" [flags]` | New run, detached |
| `/autopilot "Add product filters" [flags]` | New run without an issue key (no Linear) |
| `/autopilot resume [flags]` · `/autopilot … --from PHASE` | Continue / restart at a phase |
| `/autopilot dry-run ENG-142 ["description"]` | Preflight only |
| `/autopilot status` · `watch` · `report` · `stop` | Inspect or stop the run |
| `/autopilot abandon` | Archive a halted/stopped/paused run (asks first) |
| `/autopilot init [--statusline]` | Same as `speckit-autopilot init` |
| `/autopilot model [worker\|judge\|reader\|all] [ID]` · `model auto\|fixed` | Show / set models |
| `/autopilot effort [worker\|judge\|all] [LEVEL]` | Show / set reasoning effort |
| `/autopilot context-reset [PCT]` · `sandbox [auto\|on\|off]` · `linear [auto\|on\|off]` | Other settings |
| `/autopilot statusline [install\|uninstall\|status]` | Status line |
| `/autopilot config show\|get\|set\|unset\|path\|keys` | Raw config access |

Settings commands write `<repo>/.autopilot.toml`; add `--global` for your user config. With no value they show the
current setting and ask you to pick one. Settings apply to the next run or `--resume`, not to a run in progress.
`--repo` defaults to the git top level of Claude Code's working directory.

## CLI flags

| Flag | Meaning |
|---|---|
| `description` (positional) | Feature description. Required for a new run; taken from the saved state with `--resume` / `--from`. |
| `--repo PATH` | Target repository. Default: the current directory if it is a git repo. |
| `--issue KEY` | Tracker issue key, e.g. `ENG-142`. Required when Linear is on. |
| `--linear` / `--no-linear` | Force Linear on / off for this run (overrides `linear` in config). |
| `--spec ID` | Spec id for the feature-start command (`116`, `spec-ABC`, `RATINGS-01`). Overrides auto-numbering and `spec_title_regex`. |
| `--resume` | Continue from the last checkpoint. |
| `--from PHASE` | Restart at PHASE; it and every later phase re-run. Needs an existing state. |
| `--until PHASE` | Stop **before** PHASE (status `paused`, exit 0). `--until none` clears a stop saved by an earlier launch. |
| `--allow-escalations` | Let the ship gate open although the judge escalated decisions to a human. |
| `--auto-resume N` | Resume a halted run by itself up to N times (config `auto_resume`); budget, wall-clock and repeated halts still stop. |
| `--judge-model M`, `--worker-model M` | Override the configured models for this run. |
| `--dry-run` | Preflight only. With `--preflight-run-tests`: also run the test command once. With `--no-judge-ping`: skip the paid judge ping. |
| `--skip-preflight` | Start a real run without preflight (not recommended). |
| `--detach` | Start in the background; prints `{"pid", "console", "events", "status"}` as one JSON line. |
| `--status` | Summarise the current or last run. |
| `--watch` | Live view. `--all`: every active or recent run; `--interval N` (default 5 s); `--once`: one frame. |
| `--stop` | SIGTERM the supervisor and wait (up to 3 min) for the checkpoint. The running test command is killed, in-flight calls are cancelled. |
| `--report` | Print `REPORT.md` and the feature's `decisions.md`. |
| `--abandon` | Archive a halted / stopped / paused run so a new one can start. Never touches git, PRs or Linear. |
| `--cli-path PATH` | Use this Claude Code CLI instead of the SDK's bundled one. |

Phases: `feature_start clarify plan tasks analyze implement review ship linear`. Settings sub-commands
(`init config model effort context-reset sandbox linear statusline version`) are described in
[CONFIG.md](docs/CONFIG.md#settings-commands).

## Live progress

- **Status line.** `speckit-autopilot statusline install [--scope local|project|user]` adds a row to Claude Code's
  status bar: issue, spec, phase (attempt, repairs), a progress bar, status, elapsed time and the latest event, plus
  a compact row for other live runs. Your previous status line stays as row 1; `uninstall` restores it.
- **Terminal.** `speckit-autopilot --watch` (inside a repo: that repo's run; elsewhere or with `--all`: every run the
  machine knows about).
- **No costs shown.** The SDK's USD figures are API-equivalent estimates, not subscription spend; they are recorded
  in `run.jsonl` / `state.json` but not printed.

## Reading a run

Everything lives in `<repo>/.autopilot/` (excluded from git, passed through secret redaction):

| File | Contents |
|---|---|
| `state.json` | The checkpoint: phase records, branch, feature dir, back-edges, resets, escalations, PR / merge / CI facts, Linear facts, halt info. Never edit it. |
| `run.jsonl` | Event log, one JSON object per line (`ts`, `run_id`, `event`, fields). |
| `REPORT.md` | Written on completion, pause, halt and stop: why it ended, what a person must do, the exact resume command, PR, merge commit, phase table, routing, decision statistics. |
| `PREFLIGHT.md`, `preflight.jsonl` | Last preflight result and its events. |
| `console.log` | Output of `--detach` launches. |
| `decisions.jsonl` | Source of truth for the judge's decisions. |
| `transcripts/` | Full worker, judge and reader transcripts. |
| `lock` | The live supervisor's pid; a dead holder's lock is replaced automatically. |
| `archive/` | Earlier runs. |

`decisions.md` lives in the feature directory and is committed by ship: totals, then every decision per phase
(chosen, recommended, followed or not, impact, rationale, evidence), routing decisions and escalations.

Useful `run.jsonl` events: `phase_start` / `phase_end`, `gate` / `verify` / `repair` / `route`, `reset` (context
reset, with reason), `model_switch`, `judge`, `escalation`, `denial` (a tool call the policy refused, with rule),
`ship_gate`, `human_tasks_pause` / `checklists_pause`, `run_end`.

```bash
jq -r '"\(.ts) \(.event) \(.phase // "") \(.summary // .reason // "")"' .autopilot/run.jsonl | tail -40
jq -c 'select(.event=="denial")' .autopilot/run.jsonl
jq -c 'select(.event=="gate" and .passed==false) | {phase, failed}' .autopilot/run.jsonl
```

## Human-owned work

Some work belongs to a person. The run waits for it instead of failing (status `paused`, exit 0; `REPORT.md` says
what to do and prints the resume command).

- **Human-only tasks** in `tasks.md`: tasks labelled as a person's, e.g. `FOUNDER GATE:`, `PERSON:`,
  `*(Operator step — …)*`, `[H]`, `[RUN]`, `[MANUAL]`, a tag the file's own legend reserves for a person, or "not for
  agents". The worker is told never to tick them; ticking, removing or relabelling one fails the implement and review
  gates. Open ones pause the run before ship (`human_tasks_block_ship = true`). Do the work, tick them on the
  feature branch, then `--resume`. With `false` the run ships and lists them in `REPORT.md` and the Linear comment.
- **Checklists** (`<feature dir>/checklists/*.md`) with `checklists_gate = "halt"`: unchecked items pause the run
  before implement. With the default `"judge"`, the judge answers speckit-implement's "proceed anyway?" question.

## Resuming

- **`--resume`** continues at the first phase that has not passed. A phase whose exit gate already passes is
  recorded `already_satisfied` and skipped (analyze and review only when their inputs are unchanged). Ship is
  idempotent: an already-pushed branch or open PR is continued, never duplicated.
- **`--from PHASE`** resets PHASE and everything after it and forces it to re-run (its preconditions are
  re-checked; ship and the close-out keep their never-twice checks).
- **`--until PHASE`** is saved in the state. A plain `--resume` of a run paused at that stop continues past it;
  `--resume --until LATER` sets a new one. Do not put `until` in `.autopilot.toml`: it would re-apply on every resume.
- A halted run blocks new runs in that repo. Resume it, or `--abandon` it and clean up the branch yourself.

A staged run with human review between stages:

```bash
speckit-autopilot --issue ENG-142 "Add product filters" --until implement --detach
# review spec.md / plan.md / tasks.md / decisions.md, then:
speckit-autopilot --resume --until ship --detach
# review the branch and REPORT.md, then ship and close out:
speckit-autopilot --resume --detach
```

## Safety

- **Policy.** Every worker tool call passes a policy check. Denied, among others: force pushes, history rewrites,
  `reset --hard`, `--no-verify`, `gh pr merge --admin`, `ssh` / `scp`, `sudo`, pipe-to-shell, bulk `git add`, reading
  credential files, editing the constitution / CI workflows / lockfiles (`protected_paths`), installing dependencies
  outside implement, pushing outside ship, Linear writes outside the close-out. A denial is logged (`denial` event)
  and the worker is told not to retry. Rules: [DESIGN.md](docs/DESIGN.md).
- **OS sandbox** (`sandbox = "auto"`). Worker Bash in analyze, implement and review (and clarify / plan / tasks
  when they get a session of their own) writes only inside the repo, temp dirs and package caches; never to git
  hooks, Spec Kit scripts, `.claude` or `.autopilot`; reads no credential files (`~/.ssh`, `~/.aws`, `~/.config/gh`,
  `.env.local`, …); reaches only package registries (implement / review) plus `sandbox_network_domains`, and local
  ports. feature_start, ship and the close-out run unsandboxed: they need git/gh credentials.
- **Tooling sandbox** (`tooling_sandbox = "auto"`, macOS). The supervisor's own typecheck / lint / test / build run
  worker-written code, so they run under `sandbox-exec` with the same limits (network: localhost only; build keeps
  the network), always without credential-like environment variables and without `CI`. On Linux they run unsandboxed
  and preflight warns.
- **Escalations.** When the judge flags a decision for a human, the ship gate stays closed unless you resume with
  `--allow-escalations`.

## Repo-side commands

For feature start, review and ship, the autopilot uses the repo's own `cc-feature-start` / `cc-review` / `cc-ship`
skill or command if one exists, else the generic `autopilot-feature-start` / `autopilot-review` / `autopilot-ship`
skills that `init` installs. Spec Kit commands are found in both spellings (`speckit-plan`, `speckit.plan`). Override
any of them under `[commands]` in `.autopilot.toml`.

What every run guarantees to those commands:

- worker sessions get `AUTOPILOT=1`, `AUTOPILOT_UNATTENDED=1` and the Spec Kit pins `GIT_BRANCH_NAME`,
  `SPECIFY_FEATURE_DIRECTORY`, `SPECIFY_FEATURE`; the worker system prompt starts with `AUTOPILOT UNATTENDED MODE`;
- the ship command's argument always starts with the line
  `AUTOPILOT-AUTHORIZED: commit push pr merge switch-base` (never force, `--admin`, `--no-verify` or tracker writes);
- after review / analyze the run asks for `REVIEW_SUMMARY blockers=<n> nits=<n> questions=<n>` /
  `ANALYZE_SUMMARY critical=<n> high=<n> medium=<n> low=<n>`; a skill may end its report with that line.

## Limits

No USD budget, no wall-clock limit and no judge-call cap by default (runs use your Claude Code subscription). You
can set `total_budget_usd`, `phase_budget_usd`, `judge_call_budget_usd` or `max_wallclock_h` to get one. What
remains are guards against hangs and loops: `turn_timeout_min` (20), `judge_timeout_min` (10),
`tooling_timeout_min` (25), `max_turns_per_session` (15), `context_reset_pct` (60), `implement_max_sessions` (8),
`max_repairs_per_phase` (2), `max_back_edges` (3), `max_back_per_target` (1). See
[CONFIG.md](docs/CONFIG.md#limits-and-guards).

## Troubleshooting

Start with `--dry-run`. Each line is `PASS`, `WARN`, `FAIL`, `INFO` or `SKIP`; any `FAIL` means not ready.

| Preflight line | Fix |
|---|---|
| `python / sdk` FAIL | The install is broken: reinstall the CLI ([Install](#install)). |
| `run lock` FAIL | Another supervisor runs in this repo: `--status`, or `--stop` it. |
| `commands (files)` FAIL | A phase command is missing. Run `speckit-autopilot init`, or set it under `[commands]`. |
| `commands (SDK)` FAIL | The commands exist on disk but a session does not see them: `--repo` must be the repo itself. |
| `precondition: clean-tree` / `on-base` FAIL | Commit or remove changes; `git switch main` (or set `base_branch`). Use a dedicated clone. |
| `precondition: fetch` / `base-up-to-date` FAIL | Network or `gh` credentials, then `git pull --ff-only`. |
| `precondition: constitution` FAIL | `.specify/memory/constitution.md` is missing or still the template. A human must write it. |
| `tooling: <kind>` WARN / FAIL | WARN: nothing detected, set `<kind>_cmd`. FAIL: the executable is not on `PATH`. |
| `test baseline` WARN | `test_baseline = "base"` but the test command chains or bails, so failures cannot be pinned per test. |
| `sandbox` / `tooling sandbox` WARN / FAIL | No OS sandbox here. Install bubblewrap + socat (Linux), or set the mode to `"auto"` / `"off"`. |
| `gh auth` FAIL | `gh auth login`. |
| `ci` FAIL | CI is required but recent runs fail instantly. Set `ci_required = false` to merge on local evidence. |
| `spec id` FAIL | `spec_id_style = "key"`: pass `--spec`, or set `spec_title_regex`. |
| `sdk session` / `sdk auth + judge` FAIL | CLI or login problem: run `claude` once interactively, or try `--cli-path`. |
| `linear mcp` FAIL "not loaded" | `linear_server` names a server sessions do not have; use a name from "Linear-like servers seen". |
| `linear mcp` FAIL `needs-auth` | Open `claude` in the repo, `/mcp`, authenticate the Linear server. |
| `linear workspace` FAIL | Set `linear_workspace_url`, or the server is connected to another workspace. |

Run-time:

- **"A previous run for … is halted"**: `--resume`, `--from PHASE`, or `--abandon`.
- **"config error: …"**: a key in a config file is unknown or has the wrong type; the message names it.
- **Halt "ship gate closed"**: earlier phases failed, escalations are open (`--resume --allow-escalations` once
  decided), or the pre-ship test run failed.
- **A tooling check is red on the base branch too**: set `test_baseline = "base"` to tolerate, test by test, failures
  that already fail on the base commit.
- **Denials `sandbox-network`**: a sandboxed command tried to reach a host outside the allowed list. Add it to
  `sandbox_network_domains` if the toolchain really needs it (engine or browser downloads).
- **Paused, not failed**: open human-only tasks or checklist items. Do the work, tick it, `--resume`.
- **`/autopilot` says the CLI is missing**: install the CLI so `speckit-autopilot` is on `PATH`.

## Development

```bash
uv venv && uv pip install -e '.[test]'
uv run python -m pytest -q          # live SDK tests are skipped unless AUTOPILOT_LIVE=1
```

## License

MIT. See [LICENSE](LICENSE).
