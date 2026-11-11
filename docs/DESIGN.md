# Spec Kit Autopilot — Design (as implemented)

This document is for contributors. It describes what the code in `src/speckit_autopilot/` does and why: the
architecture, the session model, every phase's exit gate, the permission policy, the SDK behaviour the code relies
on, the contract with the repo-side commands, and the gaps that are known and still open.

For installing, running and reading reports, see the [README](../README.md). For every configuration key, see
[CONFIG.md](CONFIG.md). The Claude Code launcher skill (`/autopilot`) is in
[`skills/autopilot/SKILL.md`](../skills/autopilot/SKILL.md).

Contents: [1 Principles](#1-principles) · [2 Architecture](#2-architecture) ·
[3 Sessions and resets](#3-sessions-and-resets) · [4 The run loop](#4-the-run-loop) ·
[5 Spec Kit dialects](#5-spec-kit-dialects-markers-the-gates-rely-on) ·
[6 Phase contracts](#6-phase-contracts-as-implemented) · [7 Policy](#7-policy-rules-as-implemented) ·
[8 SDK notes](#8-sdk-behaviour-notes) · [9 Design decisions](#9-design-decisions-and-why) ·
[10 Repo-side commands](#10-repo-side-commands-contract-and-requirements) · [11 Testing](#11-testing) ·
[12 Verification and known gaps](#12-verification-status-and-known-gaps)

Some code comments cite older section labels: `DESIGN.md §(i)` is [§10](#10-repo-side-commands-contract-and-requirements)
and `§(h.1)` is [§12.1](#121-verified-live).

---

## 1. Principles

The pipeline takes one feature (optionally one Linear issue) from "start" to "merged" (and the issue closed) with no
human input:

```
feature_start → clarify → plan → tasks → analyze → implement → review → ship → linear (close-out)
```

Five facts drive the design:

1. **The orchestrator runs outside any Claude Code chat session.** A model cannot run `/clear` or `/compact` (they
   are CLI built-ins the Skill tool cannot invoke), and a session cannot reset its own context. So the pipeline is a
   **Python supervisor** on the **Claude Agent SDK** (`claude-agent-sdk`). Closing a `ClaudeSDKClient` and opening a
   new one is a programmatic `/clear`; files on disk carry the state between sessions.
2. **Structured channels, never terminal scraping.** Questions arrive through the `can_use_tool` callback
   (`AskUserQuestion`) or as text at the end of a turn; results arrive as `ResultMessage` objects.
3. **Two roles that never share a session.** A **worker** runs one command or task and may edit files. A **judge**
   is a fresh, read-only session for every decision: it stands in for the product owner and tech lead, answers
   questions, approves fixes, verifies phases and chooses routing. The model that wrote an artifact is biased toward
   its own recommendations, so it is never the only reviewer.
4. **Plain code checks come before AI checks.** Anything checkable in code (files, markers, checkboxes, exit codes,
   git state, a Linear read-back) is checked in Python. The judge adds semantic checks on top and can never overrule
   a failed code check.
5. **The `/autopilot` launcher only launches, watches and configures.** It never performs a pipeline step itself.

---

## 2. Architecture

### 2.1 Processes

```
speckit-autopilot  (supervisor; plain Python, asyncio; one per repo, guarded by .autopilot/lock)
 │
 ├── worker sessions ── claude_agent_sdk.ClaudeSDKClient ── bundled Claude Code CLI (one process per session)
 │      cwd = repo; setting_sources user+project+local; preset "claude_code" system prompt + WORKER_SYSTEM_APPEND
 │      every tool call ─► PreToolUse hook ─► PolicyGate (policy.py) ─► allow / deny
 │      "ask" tool calls (Bash, MCP, AskUserQuestion) ─► can_use_tool ─► PolicyGate (+ judge for AskUserQuestion)
 │
 ├── judge calls ─────── claude_agent_sdk.query()  (one fresh process per decision; read-only; JSON schema output)
 │
 ├── Linear readers ──── short read-only worker sessions (reader_model) whose RAW MCP tool results are parsed
 │
 └── plain code ──────── git / gh / the repo's test-lint-typecheck-build commands, gates, checkpoint, reports
```

The supervisor holds all authority. It decides which phase runs, what counts as done, which tool calls are allowed,
when a session is replaced, when to stop and what gets written to Linear. It never asks a model whether a gate passed.

### 2.2 Modules

| Module | Responsibility |
|---|---|
| `__main__.py` | The CLI (`speckit-autopilot` / `python -m speckit_autopilot`): run, `--resume`, `--from`, `--until`, `--dry-run`, `--detach`, `--status`, `--watch`, `--stop`, `--report`, `--abandon` |
| `admin.py` | Settings sub-commands: `init`, `config`, `model`, `effort`, `context-reset`, `sandbox`, `linear`, `statusline`, `version` |
| `config.py` | `Config` dataclass and defaults, layered loading, validation, `resolve_linear`, the contract constants |
| `models.py` | Worker model/effort selection (`model_mode` fixed or auto) |
| `state.py` | Checkpoint (`state.json`), run lock, event log, transcripts, redaction, run registry, `.git/info/exclude` |
| `sessions.py` | `WorkerSession` (wraps `ClaudeSDKClient`: turns, context usage, cost, interrupt, timeouts), `FakeSession`, sandbox settings |
| `judge.py` | `SDKJudge` (respond / answer / verify / route) with JSON-schema output and validation; `FakeJudge` |
| `gates.py` | Deterministic exit gates and parsers, as pure functions over the repo |
| `policy.py` | The permission gate: Bash/git/gh rules, script scanning, phase-scoped tools, MCP rules, AskUserQuestion routing |
| `phases.py` | Phase definitions, session groups and the routing graph |
| `runner.py` | The state machine: run → gate → repair → route → advance or halt; resets; pauses; ship gate; close-out |
| `prompts.py` | Worker and judge system prompts, handoff brief, repair messages, trailer requests, reply sanitising |
| `repo.py` | git/gh/shell helpers, tooling runner, repo-profile detection (commands, tooling, CI), setuid shims |
| `linear.py` | Parsing Linear read-backs from raw MCP tool results |
| `preflight.py` | Checks run in seconds before any real spend |
| `report.py` | `decisions.md`, `REPORT.md`, status summary, notifications |
| `live.py` | Status-line row and the `--watch` view (standard library only, so it stays fast) |
| `repo_skills/` | Generic `autopilot-feature-start` / `autopilot-review` / `autopilot-ship` skills installed by `init` |

The SDK is wrapped behind `WorkerSession` and `Judge`, so the whole state machine runs in tests against scripted
fakes with no API calls (§11).

### 2.3 Configuration, run files and the run registry

Configuration is layered: dataclass defaults < user config (`~/.config/speckit-autopilot/config.toml`, or
`$XDG_CONFIG_HOME/…`, or the file named by `$SPECKIT_AUTOPILOT_CONFIG`; it may hold `[repos."<name or path>"]`
tables) < the repo's committed `.autopilot.toml` < CLI flags. Unknown keys and wrongly typed values are a
`ConfigError`. Keys and defaults are documented in [CONFIG.md](CONFIG.md).

Per-run files live in `<repo>/.autopilot/` (added to `.git/info/exclude` before anything is written):
`state.json` (checkpoint), `lock`, `run.jsonl` (event log), `decisions.jsonl`, `REPORT.md`, `PREFLIGHT.md`,
`console.log` (detached runs), `transcripts/`, `decisions-post-ship.md` and `archive/` (`--abandon`).

Whenever a run starts in a repo, `state.register_repo` writes the repo path into the user-level registry
(`$SPECKIT_AUTOPILOT_STATE`, else `$XDG_STATE_HOME/speckit-autopilot`, else `~/.local/state/speckit-autopilot`,
under `runs/`). The status line and `--watch --all` find runs through that registry, plus the children of every
directory in `$SPECKIT_AUTOPILOT_ROOTS`. The registry is a convenience: a failure to write it never fails a run.

### 2.4 Workers

Workers do the repo work through the repo's own commands (feature-start, `/speckit-*`, review, ship; §10.1 says which
ones). `sessions.SDKWorkerSession._options()` builds:

- `system_prompt={"type":"preset","preset":"claude_code","append": WORKER_SYSTEM_APPEND}`;
- `setting_sources=["user","project","local"]` (CLAUDE.md, project skills, hooks and MCP servers load as in the
  interactive CLI), `permission_mode="acceptEdits"`,
  `allowed_tools=["Read","Glob","Grep","Edit","Write","TodoWrite","Skill"]`, `can_use_tool=PolicyGate.can_use_tool`;
- hooks `PreToolUse → PolicyGate.pre_tool_use_hook` and `PreCompact → mark compaction`;
- `max_budget_usd` only when a budget is configured (§4.6), a `stderr` callback into the transcript,
  `max_buffer_size=64 MB`, optional `model` / `effort` (§2.6) / `cli_path`;
- `env`: the feature pins (`GIT_BRANCH_NAME`, `SPECIFY_FEATURE_DIRECTORY`, `SPECIFY_FEATURE`), `worker_env`, and the
  contract variables `AUTOPILOT=1` / `AUTOPILOT_UNATTENDED=1` (§10.2), set last so a config cannot remove them;
- `sandbox` = `sessions.sandbox_settings(phase, cfg.sandbox, cfg.sandbox_network_domains)` (§4.5; `None` for
  feature_start, ship, linear and the halt notice);
- `plugins` from `plugin_dirs` (type `local`) and `Config.session_env()` (`ENABLE_CLAUDEAI_MCP_SERVERS=false` unless
  `claudeai_connectors` is on); sessions that need Linear wait up to 45 s after connecting for the server to leave
  `pending`.

Reader sessions (Linear read-backs, preflight) additionally set
`disallowed_tools=["Edit","Write","MultiEdit","NotebookEdit","Bash"]`.

`WORKER_SYSTEM_APPEND` (`prompts.py`) starts with the literal line `AUTOPILOT UNATTENDED MODE`. It tells the worker
that it is unattended; that "the operator / the user / the founder" is the decision agent; to ask questions in the
command's own format and stop; not to invoke brainstorming or plan mode; to print (not Skill-invoke) any `/command` a
skill tells it to run; to skip Linear steps; to stage files by path and never use bulk add, stash, `--no-verify`,
force pushes, `reset --hard`, `clean -f` or `branch -D`; never to edit the constitution; never to do a task reserved
for a person; to complete one tasks.md phase per implement turn; and not to retry a call denied with
"AUTOPILOT POLICY".

### 2.5 The judge

`judge.SDKJudge` makes every decision a human would. Each call is a fresh one-shot `query()` with
`system_prompt=JUDGE_SYSTEM_PROMPT` (a custom prompt that replaces Claude Code's), `setting_sources=["project"]`,
`tools=allowed_tools=["Read","Grep","Glob"]`, `permission_mode="dontAsk"`, `max_turns=40`,
`strict_mcp_config=True` with no MCP servers (MCP tool definitions would otherwise ride along and cost ~10× the
tokens, §12.1), `max_budget_usd=judge_call_budget_usd` (unset by default), a wall-clock bound
(`judge_timeout_min`, 10) and `output_format={"type":"json_schema","schema": <mode schema>}`. Four modes:

| Mode | Used for | Schema (`judge.py`) |
|---|---|---|
| `respond` | every worker turn in a conversation | `status` (needs_reply/done/failed), `reply` (sent verbatim after sanitising), `decisions[]`, `blocking_issues_remaining`, `escalate`, `escalate_reason` |
| `answer` | an `AskUserQuestion` call | `answers[] {question, labels[], free_text?}`, `decisions[]`, `escalate`, `escalate_reason` |
| `verify` | the phase's Definition of Done after its gate passed | `pass`, `failed_criteria[]`, `notes`, `escalate?`, `escalate_reason?`, `next_worker?` (§2.6) |
| `route` | a phase that used all its repairs, or the early route check | `action` (forward/back/halt, or `fix_here` when early), `target_phase`, `reason` |

Every decision record carries `item, recommended, chosen, followed_recommendation, rationale, evidence, impact
(low/medium/high)`. Output is validated in code (`judge.validate`, a small JSON-schema subset) and retried once with
the error appended; a second failure fails the phase or halts.

The judge prompt (`prompts.judge_prompt`) carries the issue, feature dir and branch, the repo's `judge_notes`, the
last 25 decisions (for consistency across phases), the reply format the phase expects (`PHASE_REPLY_FORMATS`),
deterministic facts computed by code ("they override anything the worker claims") and the worker's latest message.
`JUDGE_SYSTEM_PROMPT` tells it to ground every decision in the constitution, CLAUDE.md, the artifacts and existing
code (citing file and section); to treat "Recommended" as a hint from the model that wrote the spec and report
`followed_recommendation` honestly; to always fix CRITICAL findings and constitution violations; never to reply just
"yes" to a remediation offer; never to approve force pushes, history rewrites, constitution amendments or disabling
tests; and to escalate high-impact, hard-to-reverse decisions (security/auth, PII, payments, legal/compliance,
breaking public APIs, destructive migrations, constitution conflicts). An escalation does not stop the pipeline; it
keeps the ship gate closed unless `--allow-escalations`.

### 2.6 Worker models (`model_mode`)

`models.choose(cfg, phase, repairs, recommendation)` picks each worker session's model and effort.

- `model_mode = "fixed"` (default): every session uses `worker_model` / `worker_effort` (unset = the CLI's defaults
  from the user's settings).
- `model_mode = "auto"`: each phase starts from `DEFAULT_PHASE_MODELS` / `DEFAULT_PHASE_EFFORTS` (sonnet for the
  mechanical phases, opus with high effort for plan, analyze, implement and review), overridden per phase by
  `phase_models` / `phase_efforts`. Each repair round climbs one rung (at most `auto_max_escalations`), alternating
  effort up `auto_efforts` and model up `auto_models`; a ladder already at its top hands the step to the other.
  After a failed Definition of Done, the judge may set `next_worker` in its verify reply; a recommendation inside
  `auto_models` / `auto_efforts` replaces the ladder for the rest of that phase, anything else is ignored and logged.

Whenever the choice for a phase differs from the live session's, the runner opens a fresh session with the handoff
brief and logs a `model_switch` event.

### 2.7 Linear on or off

`config.resolve_linear(cfg)` decides once per run whether Linear is used (`linear = auto | on | off`, or
`--linear` / `--no-linear`):

- `on`: the issue is read at start, closed out after the merge, and halts are commented. `--issue` must look like a
  tracker key (`^[A-Z][A-Z0-9]*-\d+$`, e.g. `ENG-123`).
- `off`: no Linear at all. `--issue` is optional (the run is labelled by a slug of the description), the close-out
  phase is skipped, and the policy denies every Linear tool, reads included (`linear-off`).
- `auto` (default): on only when `--issue` looks like a tracker key **and** the configured server (`linear_server`,
  default `"linear"`) is declared in the repo's `.mcp.json`, `~/.claude.json` (including its per-project tables) or
  `~/.claude/settings.json`, or names a plugin/connector server, or `linear_workspace_url` is pinned.

The decision is recorded in `state.flags["linear"]`; under `auto` a resumed run keeps it, so a run never switches
Linear on halfway. With Linear on, `linear_workspace_url` is mandatory: issue keys can exist in more than one
workspace, so the run refuses to read or write Linear until the expected workspace is pinned (preflight FAILs, the
runner halts).

---

## 3. Sessions and resets

Phases are grouped; a group shares one worker session while its context allows, and a new group always starts a new
session (`phases.SESSION_GROUPS`, `runner._end_of_phase_boundary`):

| Group | Phases | Session behaviour |
|---|---|---|
| S1 | feature_start, clarify, plan, tasks | one session for the whole group; repairs stay in it |
| S2 | analyze | a **fresh** session for every analyze round (fresh verification); fixes are applied in that round's session, which is then closed |
| S3 | implement | fresh sessions; the phase loops over sessions (up to `implement_max_sessions`, 8) |
| S4 | review | a **fresh** session for every review round, like S2 |
| S5 | ship | fresh session |
| S6 | linear | fresh close-out session; Linear read-backs and the halt notice use separate read-only reader sessions |

A reset (close the session, open a new one) is scheduled when, after any turn,
`get_context_usage()["percentage"] ≥ context_reset_pct` (60); when the `PreCompact` hook fires or a
`compact_boundary` system message arrives (a compacted context is lossy); when a session reaches
`max_turns_per_session` (15 messages sent, including the brief); or when `model_mode = "auto"` picks a different
model/effort. Pending resets are applied at phase boundaries, before repairs, and immediately inside implement. A
turn that exceeds `turn_timeout_min` (20) is interrupted and its session closed.

Every new session first receives a **handoff brief** (`prompts.handoff_brief`): issue, description, spec id, branch,
base, feature dir, phase and attempt, the artifact precedence (spec.md > plan.md > tasks.md; the constitution
overrides all), the path of `decisions.md` ("do not re-open them") and, for repairs, the failed gate checks verbatim.
The worker answers `READY`. No conversation text crosses a reset; judge history is cleared on every session close.

**Why a reset is a new client and not `/clear`.** The SDK has no clear method; `/clear` would be a prompt dispatched
as a built-in inside the same CLI process. A mid-session `/clear` resets the cumulative `total_cost_usd` and the
`max_budget_usd` window (breaking delta-based cost accounting), and the process keeps its background tasks, hook state
and pending compaction flags. A new `ClaudeSDKClient` gives a clean CLI process, a new session id, a known cost
baseline, a fresh per-session budget cap and re-registered hooks. For the same reason `prompts.sanitize_reply` never
lets the judge send `/clear`, `/compact` or any other built-in: a reply starting with `/` passes through only when it
is one of `allowed_reply_commands` (default `speckit-specify`, `speckit.specify`, `speckit.clarify`, plus the detected
specify command); otherwise it is prefixed with `Decision: ` and sent as text.

---

## 4. The run loop

### 4.1 Turns and conversations

- **A turn** (`SDKWorkerSession.send` / `_TurnAccumulator`): the supervisor sends one user message and reads
  `receive_messages()` until its own `ResultMessage` (origin `None` or `human`) has arrived **and** no background
  task is still running. Background work (Agent/Task subagents, `run_in_background` Bash — e.g. a review command whose
  reviewers run in the background) is tracked from `TaskStartedMessage` / `TaskNotificationMessage` /
  `TaskUpdatedMessage` until a terminal status. Once the tasks are done, the supervisor waits up to 180 s for the
  CLI-injected follow-up turn carrying their results and folds its text and cost into the same turn; a leftover
  injected turn from earlier work is skipped. The whole read is bounded by `turn_timeout_min`.
- **A conversation** (`_converse(phase, session, first_message)`): send → if the turn errored (`is_error`, whatever
  the subtype) or timed out, fail → judge `respond` → `done` / `failed` end the conversation, `needs_reply` sends the
  sanitised reply and loops, until the turn cap or a reset.

### 4.2 Gates, repairs, routing

- **`_gate_loop`**: run the phase's deterministic gate; if it passes, run the judge `verify` (Definition of Done); if
  either fails, send a repair message listing the failed checks verbatim (`prompts.repair_message`), up to
  `max_repairs_per_phase` (2) rounds per phase entry. Repairs reuse the live session when its context allows,
  otherwise a fresh one with the brief. The **full** gate runs again after every repair.
- **`_route`** after repairs are exhausted: judge `route`. `back` is allowed only to the phase's declared targets
  (analyze → clarify/plan/tasks; review → implement/tasks), at most `max_back_edges` (3) per run and
  `max_back_per_target` (1) per target; the target and every phase after it are reset to pending and re-run
  (`_apply_back_edge`: reason and failed checks go into `route_context` and the re-run phase's brief; the re-run set
  is checkpointed). `forward` re-runs the deterministic gate and continues only if it now passes (never possible for
  analyze, review, ship or linear, which need a fresh run). `halt`, `fix_here` with no rounds left, or invalid output
  halts. Every routing decision is a `routing` record in `decisions.jsonl`.
- **`_early_route`** ("the root cause is upstream"): in analyze and review, on the **first** round of a phase entry
  that has blocking findings (analyze: effective CRITICAL + HIGH > 0; review: blocker estimate > 0) and **before any
  fix**, the judge is asked once in route mode with `payload.early = true` (offering `back` to the declared targets
  or `fix_here`). An allowed `back` routes at once (same limits, `back_edges` entry marked `early`); anything else
  continues with the normal repair loop. "Asked" (recorded only once the judge has answered) or "a fix was applied" is
  persisted per phase entry in `flags.early_checked`, so a resumed analyze/review does not ask again; it is cleared
  when the phase passes, when a back-edge re-enters it and on `--from`.
- **Never skip a phase, never advance on a failed gate.** A judge can never overrule a failed deterministic gate.

### 4.3 Pauses for human-owned work

`_founder_pause` runs right before a phase starts:

- with `checklists_gate = "halt"`, any unchecked `- [ ]` item in `<feature dir>/checklists/*.md` (outside code
  fences) pauses the run before implement;
- with `human_tasks_block_ship` (default on), any open human-only task in tasks.md (§10.2) pauses it before the ship
  gate (not once ship has started).

A pause is status `paused`, exit code 0, with REPORT.md listing what a person must do and the `--resume` command;
`--resume` checks again. A pause records `flags.paused_by` (`until`, `checklists`, `human_tasks`); a plain `--resume`
drops a `--until` only after the `--until` stop itself happened, so `--until ship` still stops before ship after a
founder pause. A human-only task that is the pipeline's own review/ship step (`gates.pipeline_step`) does not hold the
ship pause; REPORT.md asks a person to tick it.

Human-only tasks are snapshotted before the first implement session (`flags.human_before`); a back-edge that
regenerates tasks.md carries the open ones into the new snapshot (`flags.human_carry`), so an agent tick a gate caught
is never laundered. A human-only task open at a halt/stop/pause (`flags.human_at_stop`) and ticked on `--resume` was
ticked by a person and is accepted.

### 4.4 Idempotency, checkpoints, stopping

- **Idempotency** (`_already_satisfied`): a phase whose gate already passes **and** whose Definition of Done passes
  (asked again: a stop or crash before the phase's own DoD check must not let the gate stand in for it) is recorded
  `already_satisfied`. Ship has no DoD. Analyze and review are skipped only if their inputs are unchanged since they
  passed (fingerprint of spec/plan/tasks; HEAD and file contents). This is what makes `--resume` safe; ship and
  close-out additionally never push, merge, comment or close twice.
- **Checkpoint**: `state.json` is written atomically after every phase, gate result and judge decision. The lock is an
  `O_EXCL` file with pid and host; a lock whose pid is dead is replaced atomically.
- **Stopping** (`Runner.request_stop`, wired to SIGTERM/SIGINT through `loop.add_signal_handler`; `--stop` sends
  SIGTERM): every running tooling command's process group gets SIGTERM and, after `repo.KILL_GRACE_S` (10 s),
  SIGKILL; the in-flight judge call and Linear-reader call are cancelled (`_cancellable`) and the live worker turn is
  interrupted. The run ends `stopped` (exit 3) with a report and the resume command, written (and the lock released)
  only once no tooling command the run started is still running (`_wait_tooling_gone`). This works because the loop
  is free: tooling, the base-commit test baseline and the CI wait run in worker threads (`_in_thread`), and the
  thread's tool runner (`_stoppable_tool_runner`) starts nothing after a stop and does not retry a killed test.
- **Unexpected exceptions** end in a high-priority halt report, never a bare crash.

### 4.5 Sandboxes

**Worker OS sandbox** (`sandbox`, default `"auto"`; `sessions.sandbox_settings`). Worker Bash commands run under the
CLI's Seatbelt (macOS) / bubblewrap (Linux) sandbox in every session opened for analyze, implement or review, and for
clarify, plan or tasks when one of them opens a session of its own (normally they continue the unsandboxed S1 session
feature_start opened; §12.2):

- writes only inside the repo, temp dirs and (implement/review) the package caches; never to the always-protected
  paths (git hooks and config, `.husky`, `.githooks`, lefthook, Spec Kit scripts/templates/extensions/constitution,
  Claude settings/hooks/skills/commands/agents, `.mcp.json`, `.autopilot*`) via `filesystem.denyWrite`, so a
  `python -c` / `node -e` write the text policy cannot see is stopped too;
- no reads of `~/.ssh`, `~/.aws`, gh/gcloud/kube/docker credentials, `.netrc`, `~/.npmrc`, `~/.claude.json`, nor of
  the repo's local secret env files (`.env.local`, `.env.*.local`, `.env.production*`; `.env`, `.env.test` and
  `.env.example` stay readable because test suites load them); credential-like variables (`…TOKEN`, `…SECRET`,
  `…API_KEY`, `…PASSWORD`, `SSH_AUTH_SOCK`, …) are unset for sandboxed commands (`credentials.envVars`, mode deny);
- network only to the package registries (implement/review: `DEFAULT_SANDBOX_DOMAINS`) plus
  `sandbox_network_domains`; local port binding in implement/review.

`allowUnsandboxedCommands=false`, and the policy denies `dangerouslyDisableSandbox` and every `SandboxNetworkAccess`
request. feature_start, ship, linear and the halt notice are exempt (they need git/gh credentials or the network).
`"auto"` falls back to no sandbox where the CLI cannot sandbox; `"on"` makes preflight FAIL there.

**Tooling sandbox** (`tooling_sandbox`, default `"auto"`; `sessions.tooling_sandbox_profile`). The supervisor's own
typecheck/lint/test/build (and the base-commit baseline) execute worker-written code with the user's rights, so where
the OS sandbox is on they run under a Seatbelt profile via `sandbox-exec` (macOS only): writes only in the repo (never
its protected paths), temp dirs and package caches; no reads of the credential files above; outbound network only to
localhost and unix sockets other than container sockets — except build commands, which keep the network (e.g.
`next/font` fetches fonts at build time, and plain `sandbox-exec` has no domain proxy). Every tooling command runs
without the user's credential-like environment variables (`tooling_env` can pass one back), and the supervisor never
sets `CI` (some test configs change their database target when it is set; pin it in `tooling_env` if needed). `"on"`
halts rather than run tooling unsandboxed; where `sandbox-exec` is missing, `"auto"` logs
`tooling_sandbox_unavailable`.

Seatbelt refuses to exec setuid programs (EPERM even under `(allow default)`), which breaks tests that run `ps`, so
`repo.sandbox_shim_dir()` puts verified non-setuid copies of `repo.SETUID_SHIMS` (in
`~/Library/Caches/spec-kit-autopilot/shims/`, or `$AUTOPILOT_SHIM_DIR`) first on `PATH` for sandboxed commands.

### 4.6 Limits, redaction, decisions, halts

- **Limits**: none by default (runs are expected to use a Claude subscription). `total_budget_usd`,
  `phase_budget_usd` and `max_wallclock_h` are opt-in; when set, `_check_limits` halts before a turn that would
  exceed them, each session gets `max_budget_usd = max(0.25, remaining)`, a turn ending in `error_max_budget_usd`
  halts, and the ship gate keeps `ship_min_remaining_usd` in reserve. The old `--budget` / `--wallclock-h` flags
  still parse but are ignored. Cost per turn is the increase of the session's cumulative `total_cost_usd` (§8).
- **Redaction**: every log, transcript, state and report write goes through `state.redact` / `redact_obj` (PEM keys,
  auth headers, vendor tokens, JWTs, URL credentials, token/key/secret query parameters, `*SECRET*=` / `*TOKEN*=` /
  `*PASSWORD*=` / `DATABASE_URL=` assignments, JSON secret fields, and any string under a secret-named key such as
  `password`, `api_key`, `dsn` or `cookie`, but not token *counts*). `redact` is idempotent.
- **Decisions**: `decisions.jsonl` (in `.autopilot/`) is the source of truth; it is rendered to
  `<feature dir>/decisions.md`, which is frozen at the ship gate so the ship command commits it; later decisions
  render to `.autopilot/decisions-post-ship.md` so the base branch is never left dirty.
- **Halt**: `REPORT.md` (why, phase and attempt, failed checks, last judge reasoning, escalations, the exact resume
  command), optionally one "Autopilot halted" Linear comment (`notify_linear_on_halt`, through a reader session whose
  policy phase `halt_notice` permits exactly one new comment), and `notify_command` (the message is exported as
  `$AUTOPILOT_MESSAGE`; `{message}` is replaced shell-quoted, never interpolated raw).
- **Exit codes**: completed or paused 0, halted 2, stopped 3, other 1.

---

## 5. Spec Kit dialects (markers the gates rely on)

The gates were built against real repos on two Spec Kit generations. Repos differ mainly in how commands are named and
invoked, how the feature directory is resolved, and the exact markers each phase writes.

**Spec Kit 0.7.x / 1.0 (skill-text generation)**

- Commands: `/speckit-{specify,clarify,plan,tasks,analyze,implement,checklist,…}` (hyphen; the skill bodies mention
  `/speckit.plan` in prose, which does not exist). All `disable-model-invocation: true`, so a feature-start skill
  cannot chain them with the Skill tool (§9.2).
- Extension hooks (`.specify/extensions.yml`): `before_specify` → `speckit.git.feature` (mandatory, prints
  `EXECUTE_COMMAND: speckit.git.feature`); every other phase has optional `speckit.git.commit` hooks printing
  `## Extension Hooks` … `Prompt: Commit … ?` / ``To execute: `/speckit.git.commit` `` (a dot name that is not a real
  command). `git-config.yml` usually disables auto-commit. The branch script runs `git fetch --all --prune` and
  `git checkout -q -b`, naming `NNN-slug` unless `GIT_BRANCH_NAME` is set.
- Feature dir resolution (`common.sh get_feature_paths`): `SPECIFY_FEATURE_DIRECTORY` → `.specify/feature.json` (often
  git-tracked and pointing at a previous spec) → branch-prefix lookup. `check-prerequisites.sh` validates the branch
  even with `--paths-only`, using `$SPECIFY_FEATURE` when set: `^[0-9]{3,}-` unless the repo patched the pattern, so
  lettered specs such as `023b-` fail on an unpatched install.
- Specify: ≤ 3 `[NEEDS CLARIFICATION]` markers; questions all at once (`## Question [N]: …`, `**Your choice**`),
  reply `Q1: A, Q2: Custom - …, Q3: B`.
- Clarify: max 5 questions, one per turn, `**Recommended:** Option X` + option table or `**Suggested:** …` (answer
  ≤ 5 words); writes `## Clarifications` / `### Session YYYY-MM-DD` / `- Q: … → A: …`; "No critical ambiguities
  detected worth formal clarification." when none.
- Plan: `setup-plan.sh` copies the template **over** plan.md every run; `## Constitution Check`,
  `## Complexity Tracking`; the agent-context update rewrites a tracked file (root `CLAUDE.md`, `AGENTS.md` or a doc,
  depending on the agent target).
- Tasks: `- [ ] T001 [P] [US1] …`; done is `[X]` or `[x]`; real repos also use `[~]` and `[B]`, `T012a` and bold
  IDs; tests are optional per Spec Kit.
- Analyze: STRICTLY READ-ONLY, report only in chat: `## Specification Analysis Report`, table
  `| ID | Category | Severity | …`, a "Critical Issues Count" metric (no HIGH metric), ending with "Would you like me
  to suggest concrete remediation edits for the top N issues?".
- Implement: the checklist gate asks "Some checklists are incomplete. Do you want to proceed with implementation
  anyway? (yes/no)" when any checklist box is open; marks tasks `[X]`.

**Spec Kit 1.2.x**

- `/speckit-*` skills are `disable-model-invocation: false`, so chaining from a feature-start skill works; some repos
  also have dotted wrappers `.claude/commands/speckit.*.md`. Hooks are printed with hyphen names.
- Feature dir: `SPECIFY_FEATURE_DIRECTORY` → `.specify/feature.json` (gitignored, can be stale) → error. The git
  branch is neither used nor validated.
- Branch: the git extension's `branch_prefix` (e.g. `"feature"` → `feature/NNN-slug`; number = max over branches after
  `git fetch --all --prune` and `specs/`, + 1); older extension versions build bare `NNN-slug`. `GIT_BRANCH_NAME`
  overrides both.
- Clarify uses `**Question:** <interrogative>?` lines. Implement's question is "Some checklists have unchecked items.
  Do you want to proceed with implementation anyway? (yes/no)" — in repos whose checklists are owned by a person it
  fires on every spec (hence `checklists_gate`).
- Plan: `setup-plan.sh` skips the copy when plan.md exists (a retry resumes).

**Both generations**: real tasks.md files contain tasks reserved for a person ("FOUNDER GATE", "Operator steps (not
for agents)", "PERSON:", repo-specific tags); see §10.2.

---

## 6. Phase contracts as implemented

Each phase is **preconditions → run → exit gate (deterministic + judge) → repair loop → route**. "Repairs" = up to
`max_repairs_per_phase` (2) rounds per phase entry, then the judge routes. `{feature_start}`, `{review}` and `{ship}`
below are the detected repo commands (§10.1).

| Phase | Group | Sends | Preconditions (code) | Deterministic gate | Judge DoD | Route targets |
|---|---|---|---|---|---|---|
| feature_start | S1 | `{feature_start} {feature_start_args}`, then `/speckit-specify {specify_args}` if spec.md is missing | clean tree, on base, fetch, base == origin/base, constitution filled (skipped when already on the feature branch) | branch, dir, spec.md present, not template, no placeholders | spec covers the issue, no invented scope | none |
| clarify | S1 | `/speckit-clarify` | feature_start passed, spec.md exists, on feature branch | session dated in the run window (or "no ambiguities"), 0 `[NEEDS CLARIFICATION]` | answers integrated, no contradictions | none |
| plan | S1 | `/speckit-plan` | clarify passed, on feature branch | plan + research, no NC, Constitution Check, no unjustified violation, no leftovers (+ warn-only artifacts) | covers every FR, fits the stack | none |
| tasks | S1 | `/speckit-tasks` | plan passed, on feature branch | ≥ 1 task, unique IDs, format, no leftovers, test tasks if required | covers FRs/stories, sane order | none |
| analyze | S2 (fresh each round) | `/speckit-analyze` + trailer request, then the approved edits | tasks passed, on feature branch | report present, 0 CRITICAL, 0 HIGH, metrics agree, read-only | remaining findings fixed or accepted | clarify, plan, tasks |
| implement | S3 (session loop) | `/speckit-implement <IMPLEMENT_ARGS>`, `continue`, … | analyze passed, on feature branch | all agent tasks checked, human-only tasks untouched, typecheck/lint/test/build pass, code diff not empty | behaviour, no stubs | none |
| review | S4 (fresh each round) | `{review} {review_args}` + trailer request, then approved fixes | implement passed, on feature branch | trailer or explicit none, 0 blockers, trailer agrees, tooling passes, human-only tasks untouched | every finding fixed or accepted | implement, tasks |
| *ship gate* | — | — | all earlier phases passed, no open escalations, budget reserve (if set), tooling green (unless merged) | — | — | — |
| ship | S5 | `{ship} {ship_args}` (starts with the authorisation line) + `Include <dir>/decisions.md in the commit.`, or a resume message | ship gate open | on base, clean, base synced, merged, CI green if required | (none) | none |
| linear | S6 | close-out prompt (only the missing steps); skipped when Linear is off | ship passed | issue completed (shared issue: left alone), exactly one close-out comment of this run, project status block dated today | comment/block match what shipped | none |

### Identity (before feature_start; `runner._resolve_identity`)

1. With Linear on: read the issue through a reader session (`get_workspace`, `get_issue`, `list_comments`,
   `get_project`); halt if the workspace differs from `linear_workspace_url` or the issue cannot be read.
2. Spec id = `--spec` → `spec_title_regex` group 1 on the issue title → (style `number`) next number =
   max(numbers at the start of `specs/*` dirs, numbers in `git branch -a` names matching `(?:^|/)(\d{3,})[a-z]?-`)
   + 1, zero-padded to `spec_number_width`; style `key` without an id halts.
3. `slug` = the first `slug_words` (4) lower-case alphanumeric tokens of the description minus stop words
   (`a an the and or of for to in on with add new implement support create make by via into`).
4. Branch = `branch_template`, feature dir = `feature_dir_template` (placeholders `{spec} {slug} {num} {issue}
   {issue_lower}`).
5. Worker env pins: `GIT_BRANCH_NAME=<branch>`, `SPECIFY_FEATURE_DIRECTORY=<dir relative to repo>`,
   `SPECIFY_FEATURE=<dir basename>`.

### Markers and regexes (verbatim from `gates.py`)

```python
FRONT_MATTER      = r"\A---\n.*?\n---\n"                       # re.S; stripped before section parsing
INLINE_CODE       = r"`[^`\n]*`"                                # removed (with ``` fences) before marker counting
CLARIFICATIONS_H2 = r"^##\s+(?:Resolved\s+)?Clarifications\s*$"
SESSION_H3        = r"^###\s+Session\s+(\d{4}-\d{2}-\d{2})\b[^\n]*$"          # re.M
NC_MARKER         = r"\[NEEDS\s+CLARIFICATION\s*:"              # the unresolved marker specify writes
NC_BARE           = r"\[NEEDS\s+CLARIFICATION\s*\]"             # counted in spec.md only (plans MENTION it in prose)
NC_TECH_FIELD     = r"^\*\*[^*\n]+\*\*:\s*[^\n]*\bNEEDS CLARIFICATION\b"       # re.M, inside ## Technical Context only
NO_AMBIGUITY      = r"No critical ambiguities detected worth formal clarification"   # re.I

CONST_CHECK_H     = r"^#{2,3}\s+(?:Post-?\s*design\s+)?Constitution\s+(?:Check|Re-?\s*check)\b[^\n]*$"
COMPLEXITY_H      = r"^#{2,3}\s+Complexity Tracking\b[^\n]*$"
CONST_FAIL_CELL   = (r"^\|(?:[^|\n]*\|)*?[ \t]*(?:\*\*|__)?[ \t]*(?:"         # re.M: a cell that STARTS with a fail status
                     r"[❌✗✘]"
                     r"|(?:FAIL(?:ED|S|ING)?|VIOLATION|VIOLATED|NON-?COMPLIANT|ERROR)(?![\w-])"
                     r"|(?:[Ff]ail(?:ed|s)?|[Vv]iolat(?:ion|ed)|[Nn]on-?compliant)(?=[ \t]*(?:\*\*|__)?[ \t]*(?:\||$|[—–:;,.(/]|-\s))"
                     r")")
COMPLEXITY_PLACEHOLDER_ROW = r"\[e\.g\.[^\]]*\]|\[[A-Z][A-Z _]*\]|^\|\s*\[[^\]\n]*\]\s*\||N/A|^\|\s*\|"
PLAN_LEFTOVERS    = r"\[###-feature-name\]|\[FEATURE\]|\[DATE\]|\[REMOVE IF UNUSED\]|ACTION REQUIRED|\[Gates determined based on constitution file\]|\[e\.g\.,"
SPEC_LEFTOVERS    = r"\[FEATURE NAME\]|\[###-feature-name\]|\[DATE\]|\$ARGUMENTS"

TASK_LINE         = r"^\s*-\s+\[(?P<mark>[ xX~B])\]\s+(?:\*\*)?(?P<id>T\d{3,}[a-z]?)(?:\*\*)?(?=[\s:.,)\]]|$)(?P<rest>.*)$"   # re.M
CHECKBOX_LINE     = r"^\s*-\s+\[[ xX~B]\]\s+(?P<rest>.*)$"
TASK_LEFTOVERS    = r"\bTXXX\b|SAMPLE TASKS|\[Title\]"
PHASE_H2          = r"^##\s+Phase\s+(?P<n>\d+[a-z]?|N)\b\s*[:—–-]?\s*(?P<title>.*)$"
TEST_HINT         = r"(?i)\btests?\b|\.test\.|\.spec\.|_test\.|\btest_|\bpytest\b|\bvitest\b|\bjest\b|\bplaywright\b|\be2e\b"

ANALYZE_H2        = r"^#{1,3}\s+Specification Analysis Report\b"                # re.M | re.I
ANALYZE_TABLE_HDR = r"^\|\s*ID\s*\|\s*Category\s*\|\s*Severity\s*\|"            # re.M | re.I
ANALYZE_ROW       = r"^\|\s*\**(?P<id>[A-Za-z]{1,4}-?\d+)\**\s*\|\s*(?P<cat>[^|\n]*)\|\s*\**\s*(?P<sev>CRITICAL|HIGH|MEDIUM|LOW)\b[^|\n]*\|(?P<rest>[^\n]*)$"   # re.M | re.I (legacy fallback)
ANALYZE_ID_CELL   = r"^\**\s*([A-Za-z]{1,4}-?\d+[a-z]?)\s*\**$"
ANALYZE_SEV_CELL  = r"^[\W_]*?(CRITICAL|HIGH|MEDIUM|LOW)\b"                      # re.I
ANALYZE_SEV_HEADING = r"^#{2,5}\s+[*_]*\s*(CRITICAL|HIGH|MEDIUM|LOW)(?![\w-])"    # re.I ("### High" groups without a Severity column)
ANALYZE_CRIT_METRIC = r"(?im)(?:^|[\-*|·;,.(]|\*\*)[ \t]*[*_]*[ \t]*Critical[ \t]+(?:Issues?|Findings?)(?:[ \t]+Count)?[ \t]*[*_]*[ \t]*[:=|\-–]?[ \t]*[*_]*[ \t]*(\d+)"
ANALYZE_CRIT_METRIC_PRE = r"(?im)(?:^|[\-*|·;,(]|\*\*)[ \t]*[*_]*(\d+)[*_]*[ \t]+critical[ \t]+(?:issues?|findings?)\b"
ANALYZE_HIGH_METRIC = r"(?im)(?:^|[\-*|·;,.(]|\*\*)[ \t]*[*_]*[ \t]*High(?:[ \t]+(?:Issues?|Findings?|severity))?(?:[ \t]+Count)?[ \t]*[*_]*[ \t]*[:=|][ \t]*[*_]*[ \t]*(\d+)"
ANALYZE_OFFER     = r"(?i)would you like me to suggest (?:concrete )?(?:remediation )?edits"
ANALYZE_TRAILER   = r"(?im)^[ \t>]*[`*_]*ANALYZE[_-]SUMMARY[`*_]*:?[`*_]*[ \t]+(?P<body>[^\n]*)$"
ANALYZE_TRAILER_PAIR = r"(?i)\b(critical|high|medium|low)[`*_]*[ \t]*[=:][ \t]*[`*_]*(\d+)\b"

REVIEW_TRAILER       = r"(?m)^\s*[`*]*REVIEW[_-]SUMMARY[`*]*:?[`*]*\s+blockers\s*=\s*(\d+)\b[^\n]*$"    # re.I
REVIEW_BLOCKERS_NONE = r"(?im)^\s*(?:#{1,4}[ \t]+[*_]*Blockers?[*_]*[ \t]*(?:\(\s*0\s*\))?[ \t]*:?[ \t]*\n\s*|[*_]*Blockers?(?:[ \t]*/[ \t]*[a-z][a-z ]{0,20}?)?[*_]*\s*:?[*_]*\s*)[*_]*(?:none|0|no blockers?)\b"
REVIEW_BLOCKER_ITEM  = r"(?im)^\s*(?:[-*]\s+)?\*\*Severity\*\*\s*:\s*[*_]*blocker\b|^\s*[-*]\s*\*\*(?:Blocker|B\d+)\b"
REVIEW_BLOCKERS_LIST = r"(?im)^#{1,4}\s+Blockers?\s*:?\s*\n+\s*(?:\d+\.|[-*]\s)"
REVIEW_BLOCKERS_H    = r"(?im)^(#{1,4})[ \t]+[*_]*Blockers?[*_]*[ \t]*(?:\((\d+)\))?[ \t]*:?[ \t]*$"
REVIEW_BLOCKERS_LABEL = r"(?im)^[ \t]*\*\*Blockers?:?\*\*[ \t]*:?[ \t]*$"
REVIEW_NONE_START    = r"(?i)^[*_]*\s*(?:none|no blockers?|0)\b"
REVIEW_GROUP_LABEL   = r"^(?:#{1,6}\s+.*|\*\*[^*\n]+\*\*:?)\s*$"

CONSTITUTION_PLACEHOLDER = r"\[(?:PROJECT_NAME|PRINCIPLE_\d+_NAME|PRINCIPLE_\d+_DESCRIPTION|SECTION_\d+_NAME|GOVERNANCE_RULES|CONSTITUTION_VERSION|RATIFICATION_DATE|LAST_AMENDED_DATE)\]"
TESTS_REQUIRED    = r"(?i)test[- ]first|\bTDD\b|tests? (?:MUST|are required|is required)|(?:MUST|must) (?:include|have|add) (?:test|tests)|require[sd]? (?:explicit |named )?tests|No PR without tests|explicit named test|NON-NEGOTIABLE[^\n]*test"

STATUS_START, STATUS_END   = "<!-- autopilot:status -->", "<!-- /autopilot:status -->"
VISIBLE_START, VISIBLE_END = "[autopilot:status]", "[/autopilot:status]"
COMMENT_MARKER             = "Autopilot close-out"
```

`ANALYZE_ZERO` and `ANALYZE_ANY_TABLE` feed `parse_analyze`'s explicit-zero and has-table flags. Defined but used by
no gate today: `NC_BRACKETED`, `IMPLEMENT_CHECKLIST_Q`, `SLASH_IN_TEXT`. The comments in `gates.py` record the
real-corpus variants each regex was widened for (plans that mention `[NEEDS CLARIFICATION]` in prose; analyze reports
grouped under `### High` headings with no Severity column; "Critical issues: 0" metric spellings; `**Blockers**`
labels with sub-groups). If you change a regex, update this block; a test compares nothing here automatically.

### feature_start (S1)

- **Preconditions** (`gates.preconditions_feature_start`; skipped when already on the feature branch, i.e. on resume):
  `clean-tree` (`git status --porcelain --untracked-files=all`, ignoring `.autopilot/`), `on-base`, `fetch`
  (`git fetch origin <base>`), `base-up-to-date` (`git rev-list --left-right --count base...origin/base` == 0 0),
  `constitution` (exists, no `CONSTITUTION_PLACEHOLDER`, ≥ 400 characters after front matter). The run lock is taken
  before any phase.
- **Body.** With `test_baseline = "base"`, first measure the base: while the checkout is still the base commit, run
  the test command once and store `{sha, cmd, rc, failures, note}` in `state.flags["test_baseline"]` (kept on
  `--resume`; re-measured only by `--from feature_start` on base). Output that cannot be pinned to individual tests
  stores `failures: null`, logs `test_baseline_unparseable` and leaves every gate strict. Then open the S1 session,
  send the feature-start command and converse. If `<feature dir>/spec.md` does not exist afterwards (0.7.x repos,
  where the Skill chain is refused), log `feature_start_specify_fallback` and send `/speckit-specify {specify_args}`
  in the same session.
- **Gate** (`gate_feature_start`): `feature-branch` (current branch not base/`HEAD`/empty), `branch-name` (==
  expected), `feature-dir`, `spec-exists` (non-empty), `spec-not-template` (whitespace-normalised spec.md ≠
  `.specify/templates/spec-template.md`), `spec-no-placeholders` (`SPEC_LEFTOVERS`).
- **DoD:** the spec covers the issue's intent (title + description) and invents no scope beyond it.

### clarify (S1)

- **Body:** `/speckit-clarify`; the judge answers one question per turn (option letter, `yes`/`recommended`/
  `suggested`, or ≤ 5 words). A worker text matching `NO_AMBIGUITY` records `clarify_nothing`.
- **Gate** (`gate_clarify(..., since=<run start date>)`): `spec-exists`; `clarifications-session` (a
  `### Session <date>` heading under `CLARIFICATIONS_H2` with run start date ≤ date ≤ today, both the supervisor's
  local date, so a run that crosses midnight or resumes later still counts its session; waived when clarify reported
  no ambiguities); `no-needs-clarification` (no `NC_MARKER` or `NC_BARE` outside fenced and inline code).
- **DoD:** every answer is written into the right spec section (not only the Clarifications log), and no two sections
  contradict each other.

### plan (S1)

- **Gate** (`gate_plan`): `plan-exists`; `research-exists`; `no-needs-clarification` (`NC_MARKER` in plan.md or
  research.md outside code, or `NC_TECH_FIELD` inside `## Technical Context`; bare markers and prose mentions are
  ignored); `constitution-check` (a `CONST_CHECK_H` section); `constitution-no-unjustified-violation`
  (`CONST_FAIL_CELL` rows in **every** Constitution Check / post-design re-check section are allowed only if
  `## Complexity Tracking` has a data row that does not match `COMPLEXITY_PLACEHOLDER_ROW`);
  `plan-no-template-leftovers` (`PLAN_LEFTOVERS`). Warn-only: `data-model.md`, `quickstart.md`, and `contracts/` when
  plan.md mentions `contracts/` and external interfaces.
- **DoD:** plan.md satisfies every functional requirement, and its technical choices fit the existing stack (no new
  frameworks or languages without a recorded justification).

### tasks (S1)

- **Gate** (`gate_tasks`, over tasks.md without front matter and code fences): `tasks-exists`; `at-least-one-task`
  (`TASK_LINE`); `unique-ids`; `task-format` (every checkbox line under a `## Phase …` heading is a `TASK_LINE`);
  `no-template-leftovers`; `test-tasks` only when `TESTS_REQUIRED` matches the constitution or plan.md: at least one
  task's text or indented continuation matches `TEST_HINT`.
- **DoD:** tasks.md covers every functional requirement and user story, and the dependency order makes sense.

### analyze (S2)

- **Body, per round:** open a **fresh** session; fingerprint the feature directory (sha256 over paths and bytes,
  `decisions.md` excluded); send `/speckit-analyze`; then, in the same session, `ANALYZE_TRAILER_REQUEST` (one line
  `ANALYZE_SUMMARY critical=<n> high=<n> medium=<n> low=<n>`); parse with `parse_analyze(text, trailer_text)`;
  re-fingerprint (the read-only check covers both turns); on the first blocking round, the early route check (§4.2);
  judge `respond` with the table counts, effective counts, trailer and failed checks. If the judge's
  `blocking_issues_remaining` is lower than the effective CRITICAL + HIGH count, the parsed count wins and a note goes
  into decisions.md.
- **Parsing** (`parse_analyze`, table-aware): a table row whose first cell matches `ANALYZE_ID_CELL` is a finding;
  its severity comes from the column whose header starts with "sev" (`ANALYZE_SEV_CELL`, so "CRITICAL (tracked)"
  still counts), else from the enclosing `### High`-style heading when the table has no Severity column, else from the
  legacy `ANALYZE_ROW`. Findings are de-duplicated by ID. Metrics are the **largest** number stated outside finding
  rows. `parse_analyze_trailer` takes, per severity, the largest count over every complete ANALYZE_SUMMARY line of
  the report and the trailer reply (so a later all-zero reply never erases a stated count); `effective_counts` is the
  per-severity maximum of table and trailer.
- **Gate** (`gate_analyze`): `report-present` (fail closed), `findings-parsed` (for a report with no table and no
  explicit zero: passes on a trailer), `zero-critical`, `zero-high` (effective counts), `critical-metric-agrees` /
  `high-metric-agrees` (only when stated: fail when the metric exceeds the table and the table has none of that
  severity), `analyze-read-only`; warn-only `trailer-matches-table`.
- **Repair:** the approval is the judge's reply when it asked to reply, else `_forced_approval` ("Approved, apply these
  edits now:" + one numbered item per CRITICAL/HIGH finding + other failed checks). It is applied in the round's
  session (`analyze:apply`), the session is closed, and the next round re-analyzes in a fresh session. Exception:
  when the gate already passed (only LOW/MEDIUM findings remain) and `analyze_recheck_nonblocking` is false (the
  default), the judge's fixes are applied once and the phase passes without a re-analyze — a fresh analyze of edited
  docs tends to find new LOW items every pass and would exhaust repairs; implement and review still check the work.
- **DoD:** every remaining (non-CRITICAL, non-HIGH) finding was fixed or consciously accepted with a reason.

### implement (S3)

- **Preconditions:** analyze passed; on the feature branch; with `checklists_gate = "halt"`, no open checklist item
  (a pause, §4.3). With `"judge"` (default), the judge answers speckit-implement's "proceed anyway?" question.
- **Human-only tasks** (`gates.is_human_task`, §10.2): snapshotted before the first session (§4.3). Every implement
  message appends `human_tasks_note` ("HUMAN-ONLY TASKS (reserved for a person): T001, T023. Never tick these; skip
  them …"). The judge's facts carry `human_only_open`; `tasks_done` / `tasks_total` / `unchecked_remaining` count
  agent tasks only.
- **Body:** a loop of fresh sessions. The first sends `/speckit-implement` + `IMPLEMENT_ARGS` ("complete exactly ONE
  tasks.md phase (or one user story) per turn, mark those tasks [X] … then STOP and report … Wait for 'continue'");
  later ones send `IMPLEMENT_CONTINUE_ARGS` (only the unchecked tasks, in order, same rules). tasks.md is the
  checkpoint. The judge replies `continue` while tasks remain. A pending reset ends the session immediately; two
  consecutive sessions without new checked tasks, or more than `implement_max_sessions`, fail the conversation.
- **Gate** (`_implement_gate` → `gate_implement(..., human_before=…)`): `all-tasks-checked` (agent tasks only: done
  == total > 0; `[X]`/`[x]` done, `[ ]`/`[~]`/`[B]` not); warn-only `human-only-tasks`; `human-tasks-untouched`
  (fails when a task open in the snapshot is now ticked or was removed); `human-task-relabelled` (runner-side: a
  snapshot task whose human-only label was rewritten away); tooling — only when every task is checked, otherwise a
  failing `tooling` check — in the order typecheck, lint, test, build, each bounded by `tooling_timeout_min`; a failing
  (not timed-out) test run is retried once unless the baseline already excuses it; unset commands are skipped
  (warn-only). With `test_baseline = "base"`, a still-failing test run passes when every failing test id is in the
  base's set (warn-only `test-preexisting-failures`; the same at review and the ship gate). `diff-not-empty` (files
  changed vs `origin/<base>` or uncommitted, outside `specs/` and `.autopilot/`).
- **DoD:** the code implements the behaviour in spec.md (not just the task titles), with no TODO stubs or placeholder
  implementations for required behaviour.

### review (S4)

- **Body, per round:** fresh session; send the review command with `review_args` (default `{base}`); then, in the same
  session, `REVIEW_TRAILER_REQUEST` (one line `REVIEW_SUMMARY blockers=<n> nits=<n> questions=<n>`). Parse
  (`parse_review`) report + trailer reply; on the first blocking round, the early route check; judge `respond`.
  Tooling runs only when the parsed blocker count is 0 and the judge says `done`.
- **Parsing:** the trailer is the last `REVIEW_TRAILER` match. Every `## Blockers` heading section
  (`REVIEW_BLOCKERS_H`, optionally with a count "(N)") and every stand-alone `**Blockers**` label block is read: a
  section counts as "none" only when each sub-group (`### API`, `**Web**`, matched by `REVIEW_GROUP_LABEL`) starts with
  `REVIEW_NONE_START`; any other content counts as "listed"; an empty section is unknown. `explicit_none` =
  `REVIEW_BLOCKERS_NONE` or a "none" section; blocker items = max(`REVIEW_BLOCKER_ITEM` lines, largest heading count);
  `blockers_listed` = `REVIEW_BLOCKERS_LIST` or a "listed" section.
- **Blocker estimate:** with a trailer, max(trailer, items, 1 if listed); without, the item count (at least 1) if
  anything is listed, else 0 on an explicit none, else unknown (fail closed).
- **Gate** (`gate_review`): `review-summary` (trailer or explicit none), `zero-must-fix` (estimate == 0),
  `trailer-agrees` (fails when the trailer is lower than the listed items, or says 0 while blockers are listed), the
  tooling checks (or a failing `tooling` check "tooling not run (fixes pending)"), and, when the feature has
  human-only tasks, `human-tasks-untouched` (`_add_human_untouched_check`).
- **Repair:** the judge's "Approved, apply these fixes now:" reply (or the generic repair message) is applied in the
  round's session (`review:apply`); the next round reviews again in a fresh session.
- **DoD:** every review finding was fixed or consciously accepted; no must-fix finding remains.

### Ship gate (`_ship_gate`, before ship)

Open human-only tasks pause the run first (§4.3). The gate is closed (halt "ship gate closed") when any earlier phase
has not passed; when there are escalations and `allow_escalations` is off; when a total budget is set and less than
`ship_min_remaining_usd` (+ the linear phase budget) remains; or, unless the branch is already merged, when a fresh
tooling run fails (pre-existing failures tolerated as at implement). Once shipping has started, the escalation,
budget and tooling checks are not re-applied. When it opens, `decisions.md` is frozen into the feature directory.

### ship (S5)

- **Body:** compute `ship_facts` (fetch; current branch; clean; local base == origin/base; remote feature branch
  exists; `gh pr list --head <branch> --state all` → PR number/state/URL/merge commit; merged = the feature tip is an
  ancestor of `origin/<base>` or the PR is MERGED). If not merged-and-on-base-and-synced, open a fresh session. When
  nothing has been published yet (no remote branch, no PR), send the ship command with `ship_args`, which always
  starts with the authorisation line (`_ship_args` adds it if missing; §10.2), then `Include <dir>/decisions.md in the
  commit.`; a retry after a crash before the push is logged `ship_restart`. Otherwise send `ship_resume_message` (PR
  open → "Do NOT commit again and do NOT open another PR. Merge it with `gh pr merge N --merge`, then
  `git switch <base> && git pull --ff-only origin <base>`"; merged → only switch and pull; pushed without PR → open
  one, merge, switch).
- **Gate** (`gate_ship`): `on-base`, `clean-tree`, `base-synced`, `merged`, and `ci-green` when CI is required
  (`ci_required`, auto = a workflow triggers on push): `wait_for_ci` polls `gh run list --commit <sha>` in a worker
  thread every `ci_poll_s` until no run is pending, up to `ci_timeout_min`, at most 3 polls of "no runs yet"; success
  when every run concluded success/skipped/neutral. Without required CI, `ci-green` is a passing warn-only check.
- **CI red after the merge** → halt with priority HIGH; nothing is reverted automatically. No judge DoD.

### linear (S6; skipped when Linear is off)

- **Read-back:** a read-only reader session calls `get_workspace`, `get_issue`, `list_comments` (limit 100) and
  `get_project`; the supervisor parses the **raw tool results** from the message stream (`linear.parse_snapshot`,
  `gates.parse_mcp_json`), never the model's paraphrase. A workspace other than `linear_workspace_url` halts; the
  close-out never writes blind (three read-back attempts must verify the workspace and return issue and comments).
- **Gate** (`_gate_linear` → `gate_linear`): `issue-read`; `issue-completed` (state **type** == `completed`);
  `one-autopilot-comment` (exactly one comment containing "Autopilot close-out" and the issue key); `project-read`;
  `status-block` (text between the HTML markers or the visible fallback markers); `status-block-dated-today`. Before
  gating, `unescape_status_markers` turns `\[autopilot:status\]` back into the visible markers (Linear escapes
  literal brackets on save).
- **Shared issues** (`_shared_issue`): the key is in `shared_issues`, or the spec.md front matter says
  `linear: … shared: true` (block or `{ … }` form, `linear_front_matter`). Then the close-out prompt has no status step,
  the session runs under `SharedIssueGate` (denies `save_issue`, rule `shared-issue`), only this run's comments count
  (those carrying this run's id, plus any close-out comment absent at the close-out's first read,
  `state.linear.closeouts_before`), and `issue-completed` becomes a warn-only `shared-issue-status-unchanged`.
- **Body when the gate fails:** more than one close-out comment → halt (never deletes). Otherwise one session with only
  the missing steps (`linear_closeout_prompt`): move the issue to the team state whose **type** is `completed` (via
  `list_issue_statuses`; never assume "Done"); post exactly one comment starting
  `**Autopilot close-out** · <KEY> · run <id>` with the PR, merge commit, a 3-bullet summary, high-impact/escalated
  decisions and still-open human-only tasks (checking `list_comments` first); patch only the project status block
  (`save_project` with `patch` between the markers, or append a `## Current status` section) with Date / Just shipped
  / Open issues / Next up / Health; read the project back and re-save with visible markers if the HTML comment was
  stripped. Every Linear prompt adds `ONLY_LISTED_PARAMS` (§12.1). Then read back and re-gate; a failed DoD is
  repaired by editing the existing comment/block.
- **DoD:** the comment and status block accurately describe what shipped and mention every high-impact or escalated
  decision.

---

## 7. Policy rules as implemented

`policy.evaluate(tool_name, tool_input, ctx)` is a pure function; `PolicyGate` applies it in two places: the
**PreToolUse hook** (fires for every tool call, including calls auto-approved by `allowed_tools`, `acceptEdits` or the
repo's settings allow rules) and **`can_use_tool`** (fires only for calls that would otherwise ask). A denial returns
"AUTOPILOT POLICY (<rule>): <reason>" to the worker and a `denial` event (each distinct call logged once per phase).
Sensitive allowed calls (pushes, merges, PR actions, installs, Linear writes, pulls/checkouts during feature_start)
are logged as `policy_allow_logged`. Unknown tools are denied by default; a policy exception denies (`policy-error`,
fail closed).

`PolicyContext` carries the phase, repo, base branch, `push_phases` (`ship`), `linear_write_phases` (`linear`),
`install_phases` (`implement`), `protected_paths`, the Linear tool prefix, `linear_enabled` and `deny_bulk_add`.

### 7.1 Bash

The shell check is layered: (1) **normalisation** — continuations joined, heredoc bodies removed (bodies fed to a
shell are checked as commands) by a quote-aware lexer, `$(…)`, backticks and `<(…)`/`>(…)` bodies checked as
segments; (2) a **structural pass** that splits simple commands, strips leading `VAR=` assignments, peels shell
keywords and wrappers with their value-taking options (`env -u/-S`, `time`, `nice`, `timeout -s`, `nohup`, `xargs`,
`find -exec`, `watch`, `script -c`, `arch`, `xcrun`, `taskpolicy`, `setsid`, `flock`, `cross-env`, `dotenv`,
`env-cmd`, …) and recurses into `bash -c` / `eval`; (3) a **raw backstop** over the de-quoted command for the
always-deny rules (force push, `reset --hard`, `clean -f`, `branch -D`, `--no-verify` / `core.hooksPath` / `HUSKY=0`,
`--admin`, `sudo`, git/gh aliases, history rewrites), which also catches commands embedded in one-liners.

| Rule | Denied |
|---|---|
| `pipe-to-shell` | `curl/wget/fetch … \| sh/bash/zsh…`, `sh <(curl …)`, `sh -c "$(curl …)"` |
| `no-verify` | `--no-verify` anywhere; `git commit -n` (also in clusters like `-nm`) |
| `system-destructive` | shutdown/reboot/halt, `mkfs`, `diskutil erase/partition`, `launchctl unload/remove`, `dd … of=/dev/`, fork bomb, `chmod -R`/`chown -R` on `/` or `~` |
| `credentials` | keychain/secret-manager reads (`security find-*-password`, `gh auth token`, `op read`, `vault read`, `aws configure get`, `gcloud auth print-*-token`, …); any mention of `~/.ssh`, `~/.aws`, `~/.config/gh`, `.netrc`, `.git-credentials`, `~/.npmrc`, `.pypirc`, `.docker/config.json`, `.kube/config`, gcloud config, `~/.claude/.credentials.json`, `~/.claude.json`, `id_rsa`-style keys, or `.env*` files (except `.example/.sample/.template/.dist/.defaults`); environment dumps (`env`, `printenv`, `set`, `export -p`, `declare -x/-p`) |
| `sudo` | `sudo`, `doas`, `su`, `pkexec` |
| `remote-shell` | `ssh`, `scp`, `sftp`, `mosh`, `telnet`, `rsync host:…`, `docker -H/--host/--context` |
| `cluster-write` | `kubectl`/`helm` apply/delete/exec/install/upgrade/uninstall/rollout/scale/patch/replace/edit |
| `rm-recursive-root` | recursive `rm`/`rmdir`/`trash` (also `find -delete`, `rsync --delete`) of `/`, `~`, `$HOME`, home roots, `.`, `*`, `..`, `.git`, a repo ancestor or the repo root |
| `install-global` | brew/apt/yum/dnf/port/snap/choco/winget installs, `-g`/`--global`, `yarn global`, `cargo install`, `go install` |
| `install-phase` | adding dependencies (`npm i <pkg>`, `pnpm add`, `yarn add`, `pip install <pkg>`, `uv add`, …) outside `install_phases`; lockfile installs (`pnpm install`, `npm ci`, `uv sync`, …) are allowed and logged |
| `protected-file` | shell writes (`>`, `>>`, `tee`, `sed -i`, `perl -i`, `mv`, `cp`, `rm`, `truncate`, `git rm`, `git checkout --`, `git restore`, `ln`, `dd`) to the constitution, `.autopilot/**` or other always-protected paths (case-insensitive, including deleting an ancestor); to `protected_paths` unless in implement and a tasks.md line names the file |
| `outside-repo` | shell writes outside the repo (temp dirs excepted) |
| `package-publish` | `npm`/`pnpm`/`yarn publish` and similar |
| `sandbox-escape` | any Bash/Monitor/PowerShell input with `dangerouslyDisableSandbox` truthy |
| `sandbox-network` | the CLI's `SandboxNetworkAccess` pseudo-tool (a sandboxed command asking for a host outside `allowedDomains`) |

### 7.2 Scripts the command runs

When a command runs a script, the policy reads it and checks its content: shell scripts (`bash FILE`, `source`,
`./FILE`; a file without a shebang run directly counts as shell), interpreters (`python`, `node`/`tsx`/`bun`/`deno`,
`perl`, `ruby`, `awk -f`, …), runners (`npx`, `pnpm exec`, `uv run`, …), package scripts with pre/post hooks across
workspaces (`npm|pnpm|yarn|bun run`, `--filter`, `turbo run`, `nx`, `run-s`, `concurrently`, …), install lifecycle
scripts that differ from the base branch, Makefile recipes (best effort) and script paths named in inline code. Shell
content gets the same phase rules as a typed command; other languages get the raw backstop and credential checks.
Nesting is followed to depth 4 with cycle protection; a denial reads `script <relpath>: <inner reason>`. Only files the
worker could have written are read (the repo, temp dirs, the writable package caches, and `node_modules/.bin` /
`.venv/bin` entries, never installed packages). Relative paths resolve against the PreToolUse hook's `cwd`.

| Rule | Denied |
|---|---|
| `write-then-run` | writing a file and running it in the same command (redirect, heredoc, `tee`, `cp`/`mv`/`ln`/`install`, `sed -i`, `curl -o`, `wget -O`, `tar`/`unzip`, `git checkout/restore`, `git pull/merge/stash pop`, `patch`, inline interpreter writes) |
| `script-missing` | running or sourcing a script that does not exist yet, named in the typed command |
| `deferred-script` | in phases without the OS sandbox (feature_start, ship, linear): a script run in the background (`run_in_background`, `&`, `nohup`, `setsid`, `disown`, `at`) or after a delay/wait loop |
| `ran-script` | in those phases: rewriting a script that already ran in the same phase |
| `installed-package` | Write/Edit or a shell write inside `node_modules`, `.venv`, `venv`, `site-packages` |
| `git-variable-arg` | `git push` / `reset` / `clean` with an argument whose value cannot be known; literal assignments earlier in the command or script are substituted for every git rule |
| `stdin-script` | `… \| bash`, `python3 -` fed by a pipe (`cat FILE \| bash` is checked as FILE) |
| `generated-code` | `eval "$(…)"`, `bash -c "$(…)"`, `source <(…)` (known env initialisers allowed) |
| `exec-env` | `NODE_OPTIONS` / `BASH_ENV` / `--rcfile` / `-r` files (checked), `LD_PRELOAD` / `GIT_SSH_COMMAND`-style variables |
| `variable-command` | a program name held in a variable (typed commands) |
| `script-path`, `script-unreadable`, `script-depth` | an unresolvable path; an unreadable, non-regular or > 512 KB file; nesting deeper than 4 |

### 7.3 git and gh

| Rule | Behaviour |
|---|---|
| `force-push` | `--force`, `-f` (any cluster), `--force-with-lease`, `--force-if-includes`, `--mirror`, `+refspec`: never |
| `push-phase` | any push outside `push_phases` (ship) |
| `delete-base` | deleting the base branch on the remote: never, even in ship |
| `base-mutation` | moving or force-setting the base ref (`branch -f/-M <base>`, `update-ref`) outside ship; fetching into the local base (`fetch …:<base>`) outside feature_start and ship |
| `reset-hard`, `clean-force`, `branch-force-delete`, `discard-work` | `reset --hard`, `clean -f`, `branch -D` / `-d --force`, `git checkout -- .` / `git restore .`: never |
| `stash-destroy`, `stash` | `stash drop/clear` never; any stash action except `list`/`show` denied (the stash is shared across worktrees) |
| `bulk-add` | `git add -A/--all/./:/ /*/-u` and `git commit -a` (when `deny_bulk_git_add`) |
| `history-rewrite`, `interactive` | `filter-branch`, `filter-repo`, `replace`; `rebase -i` |
| `merge-phase`, `pull-phase` | `merge` / `pull` only in ship (`merge --abort/--quit` always; `pull` allowed and logged in feature_start) |
| `checkout-base` | `checkout` / `switch` to the base branch (including `-`, `@{-1}`, `refs/heads/<base>`, `-B/-C <base>`) only in ship; allowed and logged in feature_start; creating branches and `--` path checkouts are always fine |
| `tag-phase` | creating tags only in ship |
| `git-alias`, `git-config-override` | defining git aliases or rewriting hook/credential/clean settings; `git -c key=…` overrides (they can bypass hooks or this policy): never |

`gh`: `auth token/refresh/login/logout/setup-git` and `secret` / `ssh-key` / `gpg-key` / `variable` other than `list`
are denied (`credentials`); `repo delete/archive/rename/edit` never (`gh-repo-admin`); issue writes, `pr
merge/create/ready/close/reopen/…`, `release` writes, `workflow run/enable/disable`, `run cancel/delete/rerun` and
non-GET `gh api` (including GraphQL mutations; in ship only pull-request endpoints) only in ship; `pr merge --admin`
and `gh api -X DELETE` never. Group/action parsing skips flag values (`gh -R o/r pr merge --admin`).

### 7.4 File tools, MCP, AskUserQuestion

**Edit / Write / MultiEdit / NotebookEdit** (`check_edit_path`): credential files never; paths outside the repo denied
except temp dirs; the constitution, `.autopilot/**`, `specs/**/decisions.md` and the always-protected paths
(`.git/**`, `.claude/settings*.json`, `.claude/{hooks,skills,commands,agents}/**`, `.mcp.json`, `.husky/**`,
`.githooks/**`, lefthook, `.specify/{scripts,templates,extensions}/**`, `.autopilot.toml`) never; `protected_paths`
(CI config and lockfiles by default) denied unless the phase is implement and a tasks.md line names the file. Edits
are auto-approved by `acceptEdits` and never reach `can_use_tool`, which is why this runs in the hook.

**Read / Grep / Glob / LS / NotebookRead:** credential and `.env` paths denied.

**MCP:** a tool is a Linear tool when it starts with the configured prefix (`linear_server` `"linear"` →
`mcp__linear__`) or matches `mcp__*linear*__`. With Linear off, every Linear tool is denied (`linear-off`). A Linear
tool from any **other** server (e.g. a claude.ai connector, which may be another workspace) is always denied
(`linear-wrong-server`). On the configured server, reads (`get_`, `list_`, `search_`, `extract_`, `whoami`, `query_`)
are always allowed; writes follow a per-phase allowlist (close-out: `save_issue`, `save_comment`, `save_project`,
`save_status_update`; halt notice: one new `save_comment` only; `linear-write-phase`), and `delete_*`, `merge_*`,
`*_diff*` are never allowed (`linear-destructive`). Other MCP tools are allowed only when the name starts with a read
verb (`get/list/search/read/query/fetch/find/describe/show/lookup/retrieve/resolve_library/view/count/inspect/check`)
and contains no write verb; everything else is denied (`mcp-write`), so e.g. GitHub-MCP issue creation cannot run.

**Everything else** known (Skill, TodoWrite, Agent/Task, WebFetch, …) is allowed. `Monitor` and `PowerShell` inputs are
checked as shell commands; `RemoteTrigger`, `Cron*`, `EnterWorktree`, … are in `disallowed_tools`.

**AskUserQuestion:** the hook returns `{}` (never "allow", which would skip `can_use_tool`); `can_use_tool` asks the
judge (`answer` mode) and returns `PermissionResultAllow(updated_input={**input, "answers": {...}})`. `map_answers`
maps the judge's labels onto the exact option labels (exact, case/space-insensitive, "(Recommended)" stripped, or an
option letter/number); multi-select labels are joined with `", "`; free text is used only when no label was chosen; if
nothing matches it falls back to the option marked Recommended, else the first option, and logs `ask_fallback`.

**Shared-issue close-out:** `runner.SharedIssueGate` additionally denies `save_issue` on the configured server
(`shared-issue`).

---

## 8. SDK behaviour notes

Verified against `claude-agent-sdk` 0.2.180 (bundled CLI 2.1.321) from source and, where noted, live. `pyproject.toml`
pins `claude-agent-sdk>=0.2.170,<0.3`. Re-check these when bumping the SDK.

| Topic | Finding | What the code does |
|---|---|---|
| `system_prompt=None` | Passes `--system-prompt ""`: an **empty** system prompt, not Claude Code's. | Workers pass the `claude_code` preset with an append; the judge passes its own string on purpose. |
| `setting_sources=None` | Passes **no** flag, so the CLI loads all sources; `[]` passes `--setting-sources=`. | Workers pass `["user","project","local"]` explicitly; the judge `["project"]`. |
| `can_use_tool` with `query()` | Works with a plain string prompt (no AsyncIterable needed). | The judge uses `query()` with a string prompt and no callback; workers use `ClaudeSDKClient`. |
| What reaches `can_use_tool` | Only calls whose rules evaluate to "ask". `allowed_tools`, `acceptEdits`, settings allow rules and a PreToolUse "allow" all skip it. | The policy is enforced in the PreToolUse hook (fires for every call) and re-checked in `can_use_tool`. |
| `acceptEdits` and Bash | Auto-approves Bash commands whose base command is `mkdir`, `touch`, `rm`, `rmdir`, `mv`, `cp`, `sed`, and file edits in the working dirs. | The hook still applies `rm-recursive-root`, `protected-file`, etc. |
| AskUserQuestion | Always asks; never return allow from a hook; answer with `{**input, "answers": {question: label}}`; multi-select joined with `", "`. Not available inside subagents. | Hook returns `{}`; `can_use_tool` returns the merged input via `map_answers`. |
| `receive_response()` | No timeout; stops at the first `ResultMessage`, before background work finishes. | `send` reads `receive_messages()` under `asyncio.timeout`; on timeout `interrupt()` (bounded) and drain; a failure marks the session broken. |
| Background tasks | Background work produces extra turns and `ResultMessage`s; `origin` None or `{"kind":"human"}` marks your own; tasks are tracked via `TaskStarted/TaskNotification/TaskUpdatedMessage`. Verified live: the turn is done when `session_state_changed` reports `idle` (`CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1`). | See §4.1. |
| `get_server_info()` | A coroutine (not a plain method); `commands` entries are dicts `{name, …}` without the leading slash. | Awaited when it returns a coroutine; preflight matches command names, accepting hyphen/dot variants. |
| `get_mcp_status()` | `{"mcpServers":[{name, status, tools, error, …}]}`. HTTP servers connect asynchronously and can still be `pending` right after `connect()`. | Preflight checks status and the required Linear tools; reader/preflight sessions poll up to 45 s; the reader prompt says to load deferred Linear tools with `ToolSearch` first. |
| `get_context_usage()` | `percentage` 0–100. | Drives `context_reset_pct`. |
| Compaction | `PreCompact` hook and `compact_boundary` system messages. | Both schedule a reset. |
| `total_cost_usd` | Cumulative per CLI session (verified live: per-turn deltas sum exactly to the total); a `/clear` resets it. | Cost per turn = increase over the session's last total (a drop is treated as a reset). |
| Parent-session env leakage | Only `CLAUDECODE` is dropped by the SDK; `CLAUDE_CODE_SSE_PORT` (IDE auto-connect), `CLAUDE_CODE_MESSAGING_*`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_SESSION_ATTENDED`, `CLAUDE_PID`, `CLAUDE_EFFORT`, … leak into workers; `options.env` cannot delete. | `scrub_parent_session_env()` removes them from `os.environ` at startup; `run_shell` drops `CLAUDECODE` for tooling. |
| Authentication | No auth variables added; the CLI uses the existing Claude Code login (`apiKeySource: "none"`) or `ANTHROPIC_API_KEY`. `CLAUDE_CONFIG_DIR` changes where the login is looked up. | `session_init` events log `api_key_source`; preflight's judge ping reports it. |
| `output_format` | Becomes `--json-schema` for the whole CLI process. | Used only on one-shot judge calls (and the preflight ping), never on workers. |
| API-error trap | A run that ends on an API error can arrive as `subtype: "success"` with `is_error: true`. | `_converse` fails on `is_error`, whatever the subtype. |
| `max_buffer_size` | 1 MB per NDJSON line by default; overflow kills the read loop. | Raised to 64 MB. |
| User settings | With `"user"` loaded, workers inherit the user's `model`, effort and enabled plugins. | Set `worker_model` / `worker_effort` or `model_mode = "auto"` for deterministic behaviour. |
| claude.ai-synced plugins | Verified live: SDK sessions do **not** load them (unlike the interactive CLI). | `plugin_dirs` (globs) are passed as local plugins. |
| claude.ai connectors | Verified live: they **do** load in SDK sessions and can shadow a plugin server with the same URL while pointing at a different workspace. | `ENABLE_CLAUDEAI_MCP_SERVERS=false` unless `claudeai_connectors` is on (auto: only when `linear_server` starts with `claude.ai`); the policy denies Linear tools of any other server. |
| Slash dispatch | Verified live: a prompt `/name args` runs a `disable-model-invocation: true` skill, and the skill appears in `get_server_info()["commands"]`. | Commands are sent as the first text of a user message. |
| `ClaudeAgentOptions.sandbox` | Verified live (macOS): passed through `--settings`. With `enabled`, `autoAllowBashIfSandboxed=false`, `allowUnsandboxedCommands=false`: writes under `$HOME` fail, repo writes and `git commit` work (`.git/hooks/*`, `.git/config` stay refused); `$TMPDIR` is rewritten and writable, `/tmp` only when listed; `denyRead` holds even for `os.listdir`; `dangerouslyDisableSandbox=true` still runs sandboxed. The sandbox creates an empty `.claude/.cc-writes/` in the repo. | `sessions.sandbox_settings` builds per-phase settings (§4.5). |
| Sandbox network | A host outside `network.allowedDomains` triggers `can_use_tool` with a pseudo-tool `SandboxNetworkAccess {"host": …}` (the PreToolUse hook does **not** fire). Denied → the request fails fast (403 from the proxy). | The policy denies every `SandboxNetworkAccess`. |
| Sandbox and toolchains | Binding 127.0.0.1 is EPERM unless `allowLocalBinding`; npm needs `~/.npm`, uv `~/.cache/uv`; pnpm cannot link its home store from inside the sandbox and silently creates `<repo>/.pnpm-store`. | implement/review get `allowLocalBinding`, the caches and the pnpm store writable, and `npm_config_store_dir` pinned (`sandbox_env`). Corepack's cache stays read-only on purpose. |
| Sandbox `denyWrite` / `credentials` | In the bundled CLI's settings schema (`filesystem.denyWrite` takes precedence over `allowWrite`; `credentials.envVars` `deny` unsets variables). Not yet verified live. | Passed by every worker session; preflight's real session uses the same settings, so a CLI that rejects them fails preflight, not a run. |

---

## 9. Design decisions and why

1. **The spec id is not the issue key.** Feature-start commands take a spec id (`NNN`, `NNNa`, `spec-KEY`,
   `DOMAIN-NN`); passing `ENG-123` would start a nonsense spec. The runner resolves the id from `--spec`, then
   `spec_title_regex` over the issue title, then (only for `spec_id_style = "number"`) the next free number; style
   `key` halts instead of guessing. Repos whose numbers come from a roadmap should use style `key`.
2. **The supervisor runs specify itself when needed.** In 0.7.x repos `speckit-specify` is
   `disable-model-invocation: true`, so a feature-start skill's Skill chain is refused and the worker hands back a
   `/speckit-specify …` line. The runner sends `/speckit-specify {specify_args}` when spec.md is missing afterwards; the
   judge may also reply with the handed-back command (`allowed_reply_commands`).
3. **Branch and directory are pinned.** Branch scripts default to `NNN-slug` (often the wrong prefix) and auto-number,
   and `.specify/feature.json` is often stale or tracked. The runner exports `GIT_BRANCH_NAME`,
   `SPECIFY_FEATURE_DIRECTORY` and `SPECIFY_FEATURE` to every worker; a repo can also spell the pins out in
   `specify_args`, because the specify skill only honours a directory "the user explicitly provides".
4. **Report-only review and read-only analyze become loops.** Neither changes anything, and both end with an *offer*.
   A plain "yes" would only produce more suggestions, so the judge approves explicitly ("Approved, apply these edits /
   fixes now:" + a numbered list), the worker applies them in the same session, and a **fresh** session re-checks.
   A machine-readable trailer (`ANALYZE_SUMMARY` / `REVIEW_SUMMARY`) is requested in a second turn to back up the
   parsed report; the larger count wins.
5. **The ship command needs authorisation in its arguments.** Typical ship commands wait for confirmation and push
   only "on explicit user instruction". Every ship argument starts with a fixed authorisation line (§10.2); the
   supervisor then checks the result from git and `gh` (merged = ancestry or PR state) and resumes idempotently
   without a second commit or PR.
6. **Linear is written only in the close-out.** Repo-side tracker-sync skills tend to use states, tool names and
   lookups that do not match the real workspace, and can create duplicates. Linear writes are denied in every phase but
   `linear` (and the halt notice); the worker is told to skip Linear steps; the close-out verifies first and fills only
   the gaps: state of type `completed`, one comment, the project status block only.
7. **CI is optional per repo.** `ci_required` is auto-detected (a workflow that triggers on push); without CI, merges
   rest on the supervisor's own tooling runs (implement, review, the ship gate). Preflight's `ci_health` flags a
   required-but-dead CI.
8. **Linear server and workspace are pinned.** The same issue key can exist in more than one workspace, and a
   claude.ai connector can point at a different workspace than a plugin server with the same URL. `linear_server` and
   `linear_workspace_url` are per repo, every read-back checks `get_workspace` before any write, claude.ai connectors
   are off by default, and Linear tools of any other server are denied.
9. **Status markers may be stripped or escaped.** Linear's editor may drop HTML comments (unverified), so the
   close-out falls back to visible `[autopilot:status]` markers and the gate accepts either; Linear does escape literal
   brackets on save, so the runner un-escapes exactly those markers before gating. `status_marker_style` stays
   `"html"` by default because the evidence says "unknown", not "stripped".
10. **`--until P` means stop before P**, ending `paused` (exit 0). A plain `--resume` continues past that stop;
    `--resume --until Q` sets a new one. Runs paused for a person's work, and halted runs, keep their stop point.
11. **Bulk staging, stash and remote shells are denied.** Real checkouts carry many untracked files and stashes shared
    across worktrees, and repo settings sometimes pre-approve production SSH.
12. **The supervisor runs the tooling itself** and never trusts a transcript's "tests pass"; a failing test run is
    retried once to rule out flakiness, unless the test baseline already excuses the first run.
13. **A judge never overrules a failed deterministic gate**, and parsed counts beat the judge's: if it reports fewer
    blocking findings than the analyze table or review markers show, the parsed number is used and the disagreement
    logged.
14. **A turn does not end at the first `ResultMessage`.** `receive_response()` has no timeout and stops before
    background work finishes, so the supervisor reads `receive_messages()` under a timeout, waits for tracked
    background tasks and folds the injected follow-up turn into the same turn.
15. **Extension hooks.** The mandatory `before_specify` hook is left to the worker (the pinned `GIT_BRANCH_NAME` makes
    the branch right either way). For optional hooks the judge declines anything that commits, pushes or touches remote
    state outside ship.
16. **feature_start may `git pull` and check out the base branch** (logged): feature-start commands and branch scripts
    fetch and may need to return to an up-to-date base before branching. Pushing, merging and PR actions stay
    ship-only.
17. **Plan's design artifacts are warn-only.** `data-model.md`, `quickstart.md` and `contracts/` are conditional in
    the templates and real specs omit them; only `plan.md` and `research.md` are hard requirements.
18. **Pauses for human-owned work** end `paused` with exit 0, like `--until`, instead of halting, because nothing
    failed.
19. **Shared issues are never completed.** Some teams track several specs on one issue and leave its status to its
    owner; the close-out only comments and patches the status block.
20. **An OS sandbox, on by default where available.** The shell policy only sees command text; writes outside the
    repo, credential reads through a one-liner, and network to arbitrary hosts are stopped by the CLI's sandbox
    instead. Phases that need git/gh credentials or the network run without it.
21. **No spend limits by default.** Runs are expected to use a Claude subscription, where the SDK's USD figures are
    API-equivalent estimates, not spend; budgets and a wall-clock limit are opt-in. The live view never shows costs.
22. **A written contract for repo-side commands** (§10.2) instead of free-text arguments only, so a repo's skills can
    switch to unattended behaviour deterministically.

---

## 10. Repo-side commands: contract and requirements

### 10.1 Which command runs

`repo.detect_profile` resolves one command per phase. For feature_start, review and ship it looks for, in order, the
repo's own `cc-feature-start` / `cc-review` / `cc-ship`, then the generic `autopilot-feature-start` /
`autopilot-review` / `autopilot-ship`; each name is searched as `.claude/skills/<name>/SKILL.md` and
`.claude/commands/<name>.md` in the repo first, then under `~/.claude/`. The first hit wins; the config's `commands`
table overrides. The Spec Kit phases look for `speckit-<phase>`, then `speckit.<phase>`.

`speckit-autopilot init` installs the bundled generic skills (`src/speckit_autopilot/repo_skills/`) into
`.claude/skills/` for each phase where the repo has no own `cc-*` command (`--all-skills` installs all three,
`--force` overwrites), writes a commented `.autopilot.toml` (with `linear` set from whether a Linear server is
configured), and adds `.autopilot/` to `.git/info/exclude`. The bundled skills are minimal, correct implementations of
the requirements below; a repo with richer conventions should replace them with its own.

### 10.2 What a run guarantees

The constants live in `config.py` (`CONTRACT_ENV`, `SHIP_AUTHORIZATION`) and `prompts.py` (`UNATTENDED_LINE`, the
trailer requests); `tests/test_e2e_fixall.py` and `tests/test_config.py` pin them.

**1. Environment.** Every worker session (every phase, including the halt notice) gets:

| Variable | Value | Meaning |
|---|---|---|
| `AUTOPILOT` | `1` | This Claude session is driven by Spec Kit Autopilot. |
| `AUTOPILOT_UNATTENDED` | `1` | Nobody is at the keyboard: do not wait for confirmation; ask questions in the command's own format (the decision agent answers them) instead of "ask the operator" prose. |
| `GIT_BRANCH_NAME`, `SPECIFY_FEATURE_DIRECTORY`, `SPECIFY_FEATURE` | the pinned branch / feature dir | Spec Kit's own overrides; use them instead of auto-numbering. |

They are set after `worker_env`, so a config cannot remove them. The supervisor's own tooling runs and the judge do
not get them.

**2. System prompt.** The text appended to Claude Code's system prompt starts with the literal line
`AUTOPILOT UNATTENDED MODE`.

**3. Ship authorisation.** Every ship-command argument starts with the line
`AUTOPILOT-AUTHORIZED: commit push pr merge switch-base`, alone on its line, then the repo's `ship_args` and, on a
line of its own, `Include <feature dir>/decisions.md in the commit.` (The runner adds the line when `ship_args` has
no line that is exactly it; the phrase inside a sentence does not count.) It authorises: commit (files staged by
path), push the feature branch, open **one** PR to the base branch, merge it with a merge commit
(`gh pr merge <N> --merge`), then `git switch <base> && git pull --ff-only`. It does not authorise force pushes,
`--admin`, `--no-verify`, deleting the base branch or tracker updates. Once anything is published (a remote branch or
a PR), a resumed ship gets a plain "AUTOPILOT SHIP RESUME" message instead. If the earlier attempt published nothing
(e.g. a crash before the push), the ship command is sent again (`ship_restart`): a skill may see a second authorised
ship for the same feature, but only when the first left nothing on the remote, so it cannot double-ship.

**4. Machine-readable summaries.** After the review command and after `/speckit-analyze`, the run asks in the same
session for one line; a command that already ends its report with it makes the second turn trivial:

```
REVIEW_SUMMARY blockers=<n> nits=<n> questions=<n>
ANALYZE_SUMMARY critical=<n> high=<n> medium=<n> low=<n>
```

`REVIEW_SUMMARY` is matched by `REVIEW_TRAILER` (backticks/bold around it and `REVIEW-SUMMARY` are fine); the last
one wins and the blocker estimate is the larger of the trailer and the listed blockers. `ANALYZE_SUMMARY` needs all
four keys (any order, any case, `=` or `:`); every complete line counts, and each severity counts as the largest of
the findings table and those lines.

**5. Human-only tasks in tasks.md** (`gates.is_human_task`). A task is a person's when it names itself so; then no
agent ticks it, the implement gate excludes it from "all tasks checked" and fails if an agent ticks, removes or
relabels it, and the run pauses before ship while it is open. Recognised:

- a label at the start of the description (after `[P] [US1]`-style tags): `FOUNDER GATE` / `FOUNDER ACTION` /
  `FOUNDER DECISION` / `FOUNDER REVIEW`, `FOUNDER:`, `PERSON:`, `Operator steps (not for agents)`, `Operator —`,
  `Operator act`, `**Operator-run**:`, `HUMAN GATE`, `Owner-only:`. A bare `Founder` / `Operator` / `Person` label
  before `:` counts only in capitals or bold, so "Operator: add the /ops page" stays agent work; a `-only` label only
  before `:` or a dash, never before `(` (an authorisation rule such as "Owner-only (`requireOwner`) guard"); a label
  followed by an HTTP endpoint ("Owner-only: DELETE /api/x → 403") is not one;
- a parenthetical that starts with the label: `*(Operator step — …)*`, `(operator step: …)`,
  `*(Optional, human-gated, …)*`; elsewhere in the first paragraph only when it reads as a label;
- a gate whose subject is the founder: `**Gate: the founder approves …**`;
- a leading tag reserved for a person: `[H]`, `[RUN]`, `[MANUAL]`, `[NOT CLAIMABLE BY THE IMPLEMENTER …]`; any tag the
  file's own legend defines that way (a non-task line that spells the tag in backticks or bold and says it is
  "executed / performed by a person / the operator", "never run by an agent", "never ticked by the implementer",
  "operator act (never autonomous)", "human-gated", "deliberately left unticked"; e.g. `[OPERATOR]`, `[op]`,
  `[REVIEW]`); and a tag that says so itself (`[… performed by the operator, not by the implementer]`);
- an explicit prohibition: "not for agents", "MUST NOT be ticked by any agent", "the implementer must NOT tick this",
  "Do **not** tick this".

Prose that only mentions a founder, operator or owner stays agent work. A human-only task that is the pipeline's own
review or ship step (the command is its first action, e.g. "Operator steps (not for agents): `/cc-review`, then
`/cc-ship`") is performed by the run itself: it does not hold the ship pause, stays unticked, and the report asks a
person to tick it.

**6. Checklists** (`<feature dir>/checklists/*.md`). With `checklists_gate = "judge"` (default) the decision agent
answers speckit-implement's "proceed anyway?" question. With `"halt"` (for checklists owned by a person) the run
pauses before implement while any `- [ ]` item outside a code fence is open, naming each file and its count. Agents
never tick checklist items.

**7. Shared Linear issues.** A spec whose front matter says `linear:` / `issueId: ENG-123` / `shared: true` (block or
`{ … }` form), or an issue in `shared_issues`, is closed out with a comment and the project status block only; the
issue's status, title, description and labels are never changed.

### 10.3 Requirements for feature-start, review and ship commands

A repo-side command works unattended when, with `AUTOPILOT_UNATTENDED=1`:

- **feature-start** creates (or switches to) `$GIT_BRANCH_NAME` from an up-to-date base, creates
  `$SPECIFY_FEATURE_DIRECTORY`, and produces spec.md through the repo's specify command. If it cannot invoke a
  `disable-model-invocation` skill, it prints exactly one `/speckit-specify …` (or `/speckit.specify …`) line and stops;
  the pipeline runs it. It never opens a PR or touches the tracker.
- **review** is report-only (no edits, commits or pushes), classifies every finding as blocker / nit / question under
  `## Blockers` / `## Nits` / `## Questions` headings (a section that is empty says `None`), and ends with
  `REVIEW_SUMMARY blockers=<n> nits=<n> questions=<n>`. It should finish its reviewers in the foreground; the
  supervisor tolerates background reviewers (§4.1) but every extra injected turn is a chance to time out.
- **ship** proceeds without asking only when the first argument line is the authorisation line, stages by path (never
  `git add -A` / `.`; never `.autopilot/`, secrets or build output), includes the named `decisions.md`, pushes, opens
  one PR if none exists, merges with `gh pr merge --merge`, and returns to the base branch with `pull --ff-only`. It
  must not require invoking the review command itself (the pipeline already ran it).
- **all of them** skip every "wait for confirmation" step, skip tracker-sync steps (the close-out owns Linear), do not
  invoke brainstorming or plan mode, and never tick human-only tasks or checklists owned by a person.

### 10.4 Common pitfalls in existing repo setups

Seen in real repos when adopting the autopilot; each is a change to make in the repo, not in the autopilot:

| Pitfall | Fix |
|---|---|
| A feature-start skill chains `Skill: speckit-specify` on a 0.7.x install, where that is refused | Make specify model-invocable (as 1.2.x does) or end with one `/speckit-specify …` line. |
| Skills, scripts and constitution disagree on the branch pattern (`feature/NNN-slug` vs `NNN-slug`) | Honour `GIT_BRANCH_NAME` / `SPECIFY_FEATURE_DIRECTORY`; set the git extension's `branch_prefix`; align the constitution text. |
| `.specify/feature.json` is git-tracked and points at an old spec | Untrack it (1.2.x gitignores it). |
| Lettered specs (`023b-`) fail `check-prerequisites.sh` on 0.7.x | Widen the branch pattern in `common.sh` (`^[0-9]{3,}[a-z]?-`). |
| The ship command waits for confirmation, asks "split this commit?", or never opens/merges a PR | Honour the authorisation line and add the PR create/merge/switch steps. |
| Ship or review check stale paths (`src/**`, removed workspaces, old scripts) | Point them at the real workspaces and scripts. |
| Review has no machine-readable result, or its reviewers run in the background | End with `REVIEW_SUMMARY`; wait for reviewers in the foreground. |
| Reviewer agents pin dated model IDs; `isolation: worktree` leaves worktrees behind | Use model aliases or inherit; clean up and gitignore `.claude/worktrees/`. |
| A tracker-sync skill uses states or tool names that do not exist, overwrites descriptions, or creates duplicates | Make it idle under `AUTOPILOT_UNATTENDED=1`; the close-out does Linear. |
| Repo protocols say "do not pick the default on product decisions, ask the operator" or pause per phase | Add an unattended clause keyed on `AUTOPILOT_UNATTENDED=1` / `AUTOPILOT UNATTENDED MODE`: the decision agent decides, decisions are logged, listed categories escalate. |
| Checklists are owned by a person, so implement's checklist question fires on every spec | `checklists_gate = "halt"` (or run `--until implement`), and mark human-only tasks distinctly. |
| Optional hooks print a non-existent dotted command (`/speckit.git.commit`) with a "Commit …?" prompt | Upgrade to hyphenated hook names; keep auto-commit disabled (it runs `git add .`). |
| Project settings pre-approve `ssh`/`scp`, `git push *` or reading `~/.ssh` | Remove production access from project-level allow lists (the hook denies them anyway). |
| A SessionStart injection or a brainstorming "hard gate" pushes the worker to wait for approval | Skip it when non-interactive. |
| Pre-commit hook messages suggest `--no-verify` | Drop the suggestion (the policy denies it). |
| The test command's exit code lies when piped or backgrounded; the base branch is red | Fix the exit code; fix main, or use `test_baseline = "base"` with a pinnable, non-bailing test command. |
| Plan rewrites tracked agent-context files (`CLAUDE.md`, `AGENTS.md`) and 0.7.x `setup-plan.sh` overwrites plan.md on re-run | Expected diffs; consider moving agent context out of always-loaded files. |

---

## 11. Testing

`pytest` from the repo root runs everything under `tests/` with no API calls:

- **Unit tests** for every gate and parser (`test_gates*.py`), with passing and failing fixtures in `tests/fixtures/`
  (spec, plan, tasks, analyze and review reports, constitutions, captured test-runner output) in the real template
  formats of both Spec Kit generations; the policy (`test_policy*.py`: each rule, phase scoping, bypass attempts, false
  positives, script scanning); sandbox settings and shims; config, Linear on/off and `model_mode`; state, lock and
  redaction; routing limits; AskUserQuestion mapping including the fallback; the CLI, admin commands and live view.
- **Scripted end-to-end tests** (`test_e2e*.py`) drive the real `Runner` against `tests/fakeworld.py`: a temp git repo,
  `FakeSession` workers (`sessions.py`), a scripted judge (`JudgeScript` / `FakeJudge`), and fake `gh` and Linear.
  Scenarios include the happy path through all phases, a rejected recommendation, analyze CRITICAL → fix → fresh
  re-verify, back-edges with downstream re-runs, an implement reset mid-way, red tests → repair, repairs exhausted →
  halt, an escalation closing the ship gate, a crash during ship then `--resume` with no double push or close, a
  denied push outside ship, pauses, stops and shared issues.
- **Live tests** (`tests/test_live_sdk.py`, marker `live`) are opt-in with `AUTOPILOT_LIVE=1`; they use the cheapest
  model and a temp repo (a few cents per run) and exercise the real `SDKWorkerSession` (§12.1).

Every bug fix and review finding gets a regression test first. When you widen a gate regex for a new real-world
variant, add the variant as a fixture.

---

## 12. Verification status and known gaps

### 12.1 Verified live

Against the real SDK/CLI (`tests/test_live_sdk.py` and one-off probes):

- `total_cost_usd` is cumulative per session; `get_context_usage()["percentage"]` is present.
- AskUserQuestion answered through `can_use_tool` with `{**input, "answers": {...}}` reaches the model.
- A PreToolUse deny (e.g. `git push` during implement) blocks the call and its reason reaches the model.
- A `run_in_background` Agent's report arrives in a later CLI-injected turn; the turn completes when the session goes
  `idle`. A background shell that never finishes does not hold the turn open.
- A prompt `/name args` dispatches a `disable-model-invocation: true` skill.
- OS sandbox: a python one-liner writing under `$HOME` is refused; repo writes and commits land; the network,
  local-binding and package-cache behaviour in §8.
- `get_server_info()` is async; claude.ai-synced plugins do not load in SDK sessions, claude.ai connectors do, and a
  connector can be authenticated to a different workspace than a plugin server with the same URL — and connector
  workspaces can drift over time. A reader model that could not find the configured tools silently used the
  connector's tools, which is why `linear-wrong-server`, `linear_workspace_url` and the workspace check exist.
- Judge cost: with `setting_sources=["project"]`, MCP tool definitions still rode along (~112K tokens per cold call);
  `strict_mcp_config=True, mcp_servers={}` brings it to ~12.5K.
- Linear escapes literal brackets on save (`[X]` reads back as `\[X\]`). A cheap reader model invented an extra
  `user_intent` argument on every call and the server rejected it, so every Linear prompt says to pass only the
  documented parameters (`ONLY_LISTED_PARAMS`).
- Preflight on a clean sandbox clone reports READY; on an everyday checkout with untracked files or another session's
  branch it reports NOT READY (the intended refusal).

### 12.2 Known gaps

1. **Text checks can be beaten; the remote enforces nothing.** Not followed: obfuscated code, imports of a scanned
   file, test files and configs a runner loads, turbo root tasks, nx targets defined only in `project.json`,
   lint-staged configs run by `git commit`, a shell write to `$VAR`, and variables in `git checkout` / `branch`
   arguments (Spec Kit's own scripts pass `"$BRANCH_NAME"`). A script changed while it runs is not re-checked; without
   the OS sandbox the policy narrows that window (`deferred-script`, `ran-script`, `script-missing`). Backstops: the OS
   sandbox, branch protection, the repo's own guard hooks, and running in a sandbox clone.
2. **Not every phase is sandboxed.** feature_start, ship, linear and the halt notice need credentials or the network;
   clarify, plan and tasks share the unsandboxed S1 session (keeping the clarify Q&A in context) unless a resume opens
   a fresh one. Linux (bubblewrap + socat) and `denyWrite` / `credentials.envVars` are not verified live; hosts beyond
   the package registries need `sandbox_network_domains`. The package caches stay writable in implement/review and
   outlive the run; `.env` / `.env.test` stay readable while the npm registry is reachable. The tooling sandbox is
   macOS-only (Linux tooling runs unsandboxed minus credential-like variables, with a warning), and build commands keep
   the network.
3. **Red tests on base.** `test_baseline = "base"` tolerates only failures that `gates.parse_test_failures` pins test
   by test and that already fail on the base commit. It never applies to a chained command (`… && pytest
   --cov-fail-under=85`), a bailing one (`pnpm -r test` needs `--no-bail`; turbo needs `--continue`), coverage
   thresholds, collection/import errors, timeouts, or failures outside tests (global setup/teardown errors, obsolete
   snapshots, pytest hook errors). It is measured once, so a base that moves during the run is not re-measured.
4. **Do HTML comments survive in a Linear project description?** Unknown until a real write is observed; the default
   stays `"html"` with the visible fallback, which the gate accepts escaped or not.
5. **Shared issues are recognised only when declared** (`shared_issues` or spec front matter). An undeclared shared
   issue is completed like any other. If a worker changed a shared issue's status despite the prompt and the denial,
   `shared-issue-status-unchanged` only warns: the pipeline never reverts Linear on its own.
6. **Human-only tasks are recognised only when a task names itself a person's** (or its tag is reserved by the
   file's legend). A miss counts as agent work: the worker may do it and tick it, and `all-tasks-checked` then passes.
   A person who ticks a human-only task while the run is *running* (not stopped) is indistinguishable from an agent and
   fails the gate.
7. **Clarify session date** is the worker's local date; the gate accepts any date from the run's start to the
   supervisor's today, so only a model that invents a date (another time zone) fails, once, and the repair fixes it.
8. **A stop during short blocking calls** (a `git fetch` in `ship_facts`, a session connect) is handled when the call
   returns (seconds). A tooling command that ignores SIGTERM is SIGKILLed after 10 s; a child that left the process
   group (`setsid`) is not reached.
