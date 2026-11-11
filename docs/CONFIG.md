# Configuration reference

Everything here is derived from `src/speckit_autopilot/config.py` (the `Config` dataclass is the source of truth for
keys and defaults), `models.py`, `admin.py` and `__main__.py`.

Contents: [Files and precedence](#files-and-precedence) · [Settings commands](#settings-commands) ·
[Keys](#keys) · [Linear](#linear) · [Model selection](#model-selection-model_mode) ·
[Limits and guards](#limits-and-guards) · [Environment variables](#environment-variables)

## Files and precedence

Settings are layered; later wins:

1. built-in defaults (the tables below);
2. the **user config**: `$SPECKIT_AUTOPILOT_CONFIG` if set, else `$XDG_CONFIG_HOME/speckit-autopilot/config.toml`
   (default `~/.config/speckit-autopilot/config.toml`), top-level keys;
3. that file's per-repo tables: `[repos."<repo dir name>"]`, then `[repos."<absolute repo path>"]`;
4. the **repo config** `<repo>/.autopilot.toml` (meant to be committed);
5. **CLI flags** (`--issue`, `--spec`, `--until`, `--from`, `--resume`, `--allow-escalations`, `--linear` /
   `--no-linear`, `--judge-model`, `--worker-model`, `--dry-run`).

```toml
# ~/.config/speckit-autopilot/config.toml
judge_effort = "high"

[repos."my-app"]          # applies when the repo directory is named my-app
test_cmd = "pnpm test -- --run"
```

Rules:

- **Unknown keys are an error** (`config error: <file>: unknown config key 'x'`), and so are values of the wrong type
  (`must be a number`, `must be a list of strings`, …).
- **Tables merge key by key** across layers (`commands`, `phase_budget_usd`, `phase_models`, `phase_efforts`,
  `worker_env`, `tooling_env`). Every other value, lists included, replaces the earlier one.
- Use TOML literal strings (`'…'`) for regexes.
- Per-run keys (`issue`, `description`, `spec`, `resume`, `dry_run`, `from_phase`, `repo`, `python`) are set by the
  CLI; the settings commands refuse to write them.
- `until` and `allow_escalations` are accepted in a file but belong on the command line: a configured `until` is
  re-applied on every resume.

**State.** Each run keeps its files in `<repo>/.autopilot/` (added to `.git/info/exclude`). A small user-level
registry of repos that have run (used by `--watch` and the status line) lives in `$SPECKIT_AUTOPILOT_STATE`, else
`$XDG_STATE_HOME/speckit-autopilot` (default `~/.local/state/speckit-autopilot`), under `runs/`.

## Settings commands

All write `<repo>/.autopilot.toml` by default, or the user config with `--global`. `--repo PATH` picks the repo
(default: the git top level of the current directory). Writes edit only top-level keys, keep comments and
`[repos."…"]` tables, and are validated by loading the config; an invalid result is rolled back
(`not saved: …`). Settings apply to the next run or `--resume`.

| Command | Effect |
|---|---|
| `speckit-autopilot init [--repo R] [--statusline] [--force] [--all-skills]` | Prepare a repo: install the generic `autopilot-feature-start` / `autopilot-review` / `autopilot-ship` skills into `.claude/skills/` for each step that has no `cc-feature-start` / `cc-review` / `cc-ship` skill or command (`--all-skills`: install them anyway; `--force`: overwrite installed ones); write `.autopilot.toml` from the template with `linear = "on"` if a Linear MCP server is found, else `"off"`; add `.autopilot/` to `.git/info/exclude`; warn when there is no `.specify/`; `--statusline` also installs the status line (scope `local`). |
| `config show` | Every key with its effective value. |
| `config get KEY` | One effective value. |
| `config set KEY VALUE` | `VALUE` is parsed as TOML (`true`, `60`, `["a", "b"]`, `{ test = "x" }`); anything that does not parse is a plain string. String keys always keep the raw text. |
| `config unset KEY` | Remove the key from the target file; prints the value that now applies. |
| `config init [--force]` | Write the commented template (repo or `--global`). |
| `config path` | Print both file paths and whether they exist. |
| `config keys` | Every settable key with its type. |
| `model` | Show `model_mode`, the worker / judge / reader models (in auto mode the per-phase table instead of `worker_model`). |
| `model ROLE ID` | ROLE = `worker`, `judge`, `reader`, or `all` (worker + judge; never the reader). `ID` is any model id or alias the Claude Code CLI accepts (`opus`, `sonnet`, `haiku`, a full id); `default` unsets. |
| `model auto` / `model fixed` | Set `model_mode`. |
| `effort [ROLE] [LEVEL]` | ROLE = `worker`, `judge`, `all`; LEVEL = `low medium high xhigh max`, or `default` to unset. |
| `context-reset [PCT]` | Show / set `context_reset_pct` (1–100; a trailing `%` is accepted). |
| `sandbox [auto\|on\|off]` | Show / set `sandbox`. |
| `linear [auto\|on\|off]` | Show / set `linear`; the show form also says what `auto` would resolve to for an issue key. |
| `statusline install\|uninstall\|status [--scope local\|project\|user] [--interval N]` | Manage the Claude Code status line (see below). |
| `version` | Print the installed version. |

**Status line.** `install` sets `statusLine` in `.claude/settings.local.json` (`--scope local`, default),
`.claude/settings.json` (`project`) or `~/.claude/settings.json` (`user`; `$CLAUDE_CONFIG_DIR/settings.json` when
that is set), refreshing every `--interval` seconds (default 5). A status line you already had is saved in
`$XDG_CONFIG_HOME/speckit-autopilot/statusline-previous.json`, shown as row 1, and restored by `uninstall`. A
checkout's own `.claude/statusline-command.sh`, if present, is used as row 1 instead.

## Keys

Types: `str`, `int`, `float` (integers are accepted), `bool`, `list` (of strings), `table`. "unset" = the key is
absent (or `None` internally).

### Models and effort

| Key | Type | Default | Meaning |
|---|---|---|---|
| `worker_model` | str | unset | Worker model in `fixed` mode. Unset = the Claude Code CLI default (your settings' `model`). |
| `judge_model` | str | `"claude-opus-5-5"` | Judge model. |
| `reader_model` | str | `"claude-haiku-5-5"` | Model for Linear read-back sessions. Unset = CLI default. |
| `worker_effort` | str | unset | Worker reasoning effort in `fixed` mode (`low medium high xhigh max`). Unset = CLI default. |
| `judge_effort` | str | unset | Judge reasoning effort. |
| `model_mode` | str | `"fixed"` | `fixed` or `auto`. See [Model selection](#model-selection-model_mode). |
| `phase_models` | table | `{}` | `auto`: per-phase model overrides, e.g. `{ tasks = "opus" }`. Keys must be phase names. |
| `phase_efforts` | table | `{}` | `auto`: per-phase effort overrides. Values must be effort levels. |
| `auto_models` | list | `["sonnet", "opus"]` | `auto`: model escalation ladder, weakest first; also the models the judge may pick. Non-empty. |
| `auto_efforts` | list | `["low", "medium", "high", "xhigh"]` | `auto`: allowed efforts for escalation and judge picks. Non-empty, valid levels. |
| `auto_max_escalations` | int | `2` | `auto`: escalation steps per phase (≥ 0). |

### Sessions

| Key | Type | Default | Meaning |
|---|---|---|---|
| `context_reset_pct` | float | `60.0` | Replace a worker session by a fresh one (with a checkpoint hand-off) once its context reaches this % (1–100). |
| `max_turns_per_session` | int | `15` | Reset a worker session after this many turns. |
| `turn_timeout_min` | float | `20.0` | Interrupt one worker turn after this long. |
| `implement_max_sessions` | int | `8` | Implement fails after this many sessions without finishing. |

### Repair and routing

| Key | Type | Default | Meaning |
|---|---|---|---|
| `max_repairs_per_phase` | int | `2` | Repair rounds per phase entry after a failed gate / Definition-of-Done check (≥ 0). |
| `max_back_edges` | int | `3` | Total times the judge may route back to an earlier phase in one run (≥ 0). |
| `max_back_per_target` | int | `1` | Times the run may route back to the same phase. |
| `analyze_recheck_nonblocking` | bool | `false` | Analyze: when only LOW/MEDIUM findings remain, `false` applies the judge's fixes once and passes; `true` re-runs analyze after them. |

### Tooling and tests

| Key | Type | Default | Meaning |
|---|---|---|---|
| `test_cmd`, `lint_cmd`, `typecheck_cmd`, `build_cmd` | str | unset | The supervisor's own checks (order: typecheck, lint, test, build). Unset = detected: from `package.json` scripts (`test:ci` / `test:unit` / `test`, `lint`, `typecheck` / `type-check` / `check-types` / `tsc` / `types`, `build`) with the lockfile's package manager; for a `pyproject.toml` repo only `test` (`uv run pytest -q` with `uv.lock`, else `python -m pytest -q`). `""` disables a detected check. |
| `tooling_timeout_min` | float | `25.0` | Kill one tooling command after this long. |
| `tooling_env` | table | `{}` | Extra environment for tooling commands. They run without credential-like variables (`…TOKEN`, `…SECRET`, `…API_KEY`, `SSH_AUTH_SOCK`, …) and without `CI`; pass what a suite needs here. |
| `test_baseline` | str | `"none"` | `none`: any failing test fails implement, review and the ship gate. `base`: feature_start runs `test_cmd` once on the base commit; tests already failing there are tolerated test by test later. Only output pinned to individual tests counts: chained (`a && b`) or bailing commands, coverage thresholds and errors outside any test stay strict (preflight warns). |
| `ci_required` | bool | unset | Whether CI must be green on the merge commit. Unset = `true` when a `.github/workflows` file triggers on `push`. |
| `ci_timeout_min` | float | `30.0` | How long to wait for CI after merging. |
| `ci_poll_s` | float | `30.0` | CI poll interval. |

### Repo profile and spec identity

| Key | Type | Default | Meaning |
|---|---|---|---|
| `base_branch` | str | unset | Base branch. Unset = `origin/HEAD`, else `main`. |
| `commands` | table | `{}` | Phase → slash command overrides, e.g. `{ analyze = "/speckit-analyze", ship = "/my-ship" }`. Keys: `feature_start specify clarify plan tasks analyze implement review ship`. Unset phases are detected (see README). |
| `spec_id_style` | str | `"number"` | `number`: spec id = next free number across `specs/` and branches. `key`: the id must come from `--spec` or `spec_title_regex`, else the run halts. |
| `spec_number_width` | int | `3` | Zero-padding of numeric spec ids (`007`). |
| `spec_title_regex` | str | unset | Regex with one group, applied to the Linear issue title to derive the spec id, e.g. `'^spec-(\d{3})\s+[—–-]\s'`. |
| `slug_words` | int | `4` | Words in the slug made from the description (also the run label without an issue key). |
| `branch_template` | str | `"feature/{spec}-{slug}"` | Feature branch. Placeholders: `{spec} {slug} {num} {issue} {issue_lower}`. |
| `feature_dir_template` | str | `"specs/{spec}-{slug}"` | Feature directory, same placeholders. |
| `feature_start_args` | str | `"{spec}"` | Argument to the feature-start command. Placeholders for all `*_args`: `{issue} {description} {spec} {slug} {num} {base} {branch} {title}`. |
| `specify_args` | str | `"{issue}: {description}"` | Argument to the Spec Kit specify command. |
| `review_args` | str | `"{base}"` | Argument to the review command. |
| `ship_args` | str | the authorisation line + "commit, push the branch, open a PR to {base}, merge it with a merge commit (gh pr merge --merge), then switch to {base} and pull --ff-only. Do not update Linear (the autopilot closes it out)." | Argument to the ship command. The line `AUTOPILOT-AUTHORIZED: commit push pr merge switch-base` is prepended if missing. |
| `worker_env` | table | `{}` | Extra environment for worker sessions. `AUTOPILOT=1` / `AUTOPILOT_UNATTENDED=1` always win. |
| `judge_notes` | str | `""` | Repo-specific guidance appended to every judge prompt. |
| `allowed_reply_commands` | list | `["speckit-specify", "speckit.specify", "speckit.clarify"]` | Slash commands a judge reply may start with (the repo's specify command is added); any other leading `/command` is neutralised. |

### Human-owned work

| Key | Type | Default | Meaning |
|---|---|---|---|
| `human_tasks_block_ship` | bool | `true` | Open human-only tasks in `tasks.md` pause the run before ship. `false`: ship anyway and list them in the report and the Linear comment. |
| `checklists_gate` | str | `"judge"` | `judge`: the judge answers speckit-implement's unchecked-checklist question. `halt`: unchecked items in `<feature dir>/checklists/*.md` pause the run before implement. |

### Linear keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `linear` | str | `"auto"` | `auto`, `on` or `off`. See [Linear](#linear). |
| `linear_server` | str | `"linear"` | MCP server name as Claude Code reports it. Linear tools of any other server are denied. |
| `linear_workspace_url` | str | unset | The workspace the server must be connected to, e.g. `"https://linear.app/your-team"`. **Required when Linear is on**: preflight fails and the run halts without it, because issue keys repeat across workspaces. |
| `linear_project` | str | unset | Fallback project for the status block when the issue has none. Prefer the project UUID. |
| `shared_issues` | list | `[]` | Issue keys that track other work too: the close-out only comments and updates the project status block, never the issue's status. A spec.md front matter `shared: true` counts the same. |
| `status_marker_style` | str | `"html"` | Status block markers: `html` (`<!-- autopilot:status -->`) or `visible` (`[autopilot:status]`). |
| `plugin_dirs` | list | `[]` | Local plugin directories (globs, `~` allowed) loaded into every worker and reader session. Needed when the Linear server comes from a plugin, which SDK sessions do not load by themselves. Preflight fails if a glob matches nothing. |
| `claudeai_connectors` | bool | unset | Whether claude.ai connectors may load in sessions. Unset = only when `linear_server` starts with `claude.ai` (a connector can point at a different workspace). |
| `notify_linear_on_halt` | bool | `true` | Post a comment on the issue when the run halts. |
| `linear_write_phases` | list | `["linear"]` | Phases allowed to write to Linear. |

### Safety

| Key | Type | Default | Meaning |
|---|---|---|---|
| `sandbox` | str | `"auto"` | OS sandbox for worker Bash: `auto` (on where available: macOS `sandbox-exec`, Linux bubblewrap + socat), `on` (preflight fails without it), `off`. Never applied to feature_start, ship, the close-out or the halt notice. |
| `sandbox_network_domains` | list | `[]` | Extra hosts sandboxed Bash may reach, on top of the package registries implement/review always get (`registry.npmjs.org`, `registry.yarnpkg.com`, `pypi.org`, `files.pythonhosted.org`, Google Fonts). |
| `tooling_sandbox` | str | `"auto"` | Sandbox for the supervisor's test/lint/typecheck/build: `auto` (macOS `sandbox-exec` unless `sandbox = "off"`), `on` (halt without it), `off`. |
| `protected_paths` | list | constitution, CI configs, lockfiles (see below) | Globs workers may not edit. |
| `deny_bulk_git_add` | bool | `true` | Deny `git add -A` / `git add .` and similar. |
| `push_phases` | list | `["ship"]` | Phases allowed to push / merge. |
| `install_phases` | list | `["implement"]` | Phases allowed to add dependencies. |

Default `protected_paths`: `.specify/memory/constitution.md`, `.github/workflows/**`, `.gitlab-ci.yml`,
`.circleci/**`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lockb`, `bun.lock`, `poetry.lock`,
`uv.lock`, `Cargo.lock`, `Gemfile.lock`, `composer.lock`, `go.sum`. Setting the key replaces the whole list.

Entries of the `*_phases` lists must be phase names (or `halt_notice`).

### Limits and notifications

| Key | Type | Default | Meaning |
|---|---|---|---|
| `total_budget_usd` | float | unset | USD cap for the run (> 0). Unset = no cap. |
| `phase_budget_usd` | table | `{}` | Per-phase USD caps, e.g. `{ implement = 20.0 }`. |
| `judge_call_budget_usd` | float | unset | USD cap per judge call. |
| `max_wallclock_h` | float | unset | Halt after this many hours of accumulated run time. |
| `ship_min_remaining_usd` | float | `3.0` | Only with `total_budget_usd`: the ship gate keeps this much in reserve. |
| `judge_timeout_min` | float | `10.0` | End a hung judge call. |
| `notify_command` | str | unset | Shell command run on completion, pause and halt. `{message}` is replaced by the shell-quoted message, which is also in `$AUTOPILOT_MESSAGE`. Example: `'osascript -e "display notification \"$AUTOPILOT_MESSAGE\""'`. |

### Run flags (CLI)

| Key | Flag | Meaning |
|---|---|---|
| `until` | `--until PHASE` | Stop before PHASE; `none` clears a saved stop. |
| `allow_escalations` | `--allow-escalations` | Let the ship gate open despite escalations. Default `false`. |
| `from_phase`, `resume`, `dry_run`, `spec`, `issue`, `description` | `--from`, `--resume`, `--dry-run`, `--spec`, `--issue`, positional | Per run only. |

## Linear

`linear` decides whether a run talks to Linear at all:

- **`on`**: `--issue` must be a tracker key (`ENG-142`). The run reads the issue at start (title, project,
  workspace), comments on a halt (`notify_linear_on_halt`), and after the merge closes it out: a close-out comment,
  the issue status, and a status block on the project.
- **`off`**: no Linear reads or writes. `--issue` is optional (without it the run is labelled by a slug of the
  description), the `linear` phase is skipped, and the policy denies every Linear tool.
- **`auto`** (default): on only when both hold:
  1. `--issue` matches `^[A-Z][A-Z0-9]*-\d+$`, and
  2. the Linear server looks configured: `linear_server` names a plugin (`plugin:…`) or claude.ai connector
     (`claude.ai …`), or a server with that name is declared in `<repo>/.mcp.json`, `~/.claude.json` (top level or
     the repo's project entry) or `~/.claude/settings.json` (both inside `$CLAUDE_CONFIG_DIR` when that is set), or
     `linear_workspace_url` is set.

  This is a static check; preflight then confirms the server really connects (`linear mcp`), is in the expected
  workspace (`linear workspace`) and can read the issue (`linear issue`). `speckit-autopilot linear` shows what
  `auto` resolves to.

`--linear` / `--no-linear` force `on` / `off` for one run.

Typical setup with a repo-level `.mcp.json` server named `linear`:

```toml
linear = "on"
linear_server = "linear"
linear_workspace_url = "https://linear.app/your-team"
```

If the server comes from a plugin, name it as Claude Code lists it (`linear_server = "plugin:<plugin>:linear"`) and
point `plugin_dirs` at the plugin's directory. If preflight reports `needs-auth`, open `claude` in the repo, run
`/mcp` and authenticate.

## Model selection (`model_mode`)

**`fixed`** (default): every worker session uses `worker_model` / `worker_effort`; the judge uses `judge_model` /
`judge_effort`.

**`auto`**: each phase starts from a per-phase table, overridable key by key with `phase_models` /
`phase_efforts` (`worker_model` / `worker_effort` are not used for workers; the judge is unaffected):

| Phase | Model | Effort |
|---|---|---|
| feature_start | sonnet | medium |
| clarify | sonnet | medium |
| plan | opus | high |
| tasks | sonnet | medium |
| analyze | opus | high |
| implement | opus | high |
| review | opus | high |
| ship | sonnet | medium |
| linear | sonnet | low |

**Escalation on repairs.** Repair round *n* of a phase climbs `min(n, auto_max_escalations)` steps, alternating:
odd steps raise the effort to the next level in `auto_efforts`, even steps move the model one place up
`auto_models`; when one ladder is already at its top the step goes to the other, and at the top of both it stops.
With the defaults, tasks (sonnet/medium) goes to sonnet/high in round 1 and opus/high in round 2; implement
(opus/high) goes to opus/xhigh and stays there. A phase model that is not in `auto_models` never changes model.

**Judge recommendations.** After a failed Definition-of-Done check the judge may recommend the model / effort for
the next attempt. A recommendation inside `auto_models` and `auto_efforts` replaces the ladder for the rest of that
phase; anything else is ignored and logged (`model_recommendation` event).

Whenever the choice for a phase differs from the live session's, the runner opens a fresh worker session with the
usual hand-off brief and logs a `model_switch` event.

```toml
model_mode = "auto"
phase_models = { tasks = "opus" }
phase_efforts = { implement = "xhigh" }
auto_models = ["sonnet", "opus"]
auto_efforts = ["low", "medium", "high", "xhigh", "max"]
auto_max_escalations = 3
```

## Limits and guards

There is no USD budget, judge-call cap or wall-clock limit unless you set `total_budget_usd`, `phase_budget_usd`,
`judge_call_budget_usd` or `max_wallclock_h`. The always-on guards against hangs and loops:

| Key | Default |
|---|---|
| `turn_timeout_min` | 20 |
| `judge_timeout_min` | 10 |
| `tooling_timeout_min` | 25 |
| `max_turns_per_session` | 15 |
| `context_reset_pct` | 60 |
| `implement_max_sessions` | 8 |
| `max_repairs_per_phase` | 2 |
| `max_back_edges` | 3 |
| `max_back_per_target` | 1 |
| `ci_timeout_min` / `ci_poll_s` | 30 / 30 (only when `ci_required`) |

The hidden CLI flags `--budget` and `--wallclock-h` still parse for old command lines but are ignored.

## Environment variables

Read by the CLI:

| Variable | Effect |
|---|---|
| `SPECKIT_AUTOPILOT_CONFIG` | Path of the user config file. |
| `XDG_CONFIG_HOME` | Base of the default user config dir (`~/.config`). |
| `SPECKIT_AUTOPILOT_STATE` | Directory of the cross-repo run registry. |
| `XDG_STATE_HOME` | Base of the default state dir (`~/.local/state`). |
| `SPECKIT_AUTOPILOT_ROOTS` | Extra directories (path-separator list) whose children `--watch` and the status line scan for runs. |
| `SPECKIT_AUTOPILOT_PYTHON` | Interpreter the status line script uses (set by `statusline install`). |
| `AUTOPILOT_SHIM_DIR` | Where the macOS tooling sandbox keeps its non-setuid `ps` copy (default `~/Library/Caches/speckit-autopilot/shims`). |
| `ANTHROPIC_API_KEY` | If set, Claude sessions use the API key instead of your Claude Code login. |
| `CLAUDE_CONFIG_DIR` | Where Claude Code's config lives; used for Linear detection and the `user` status line scope. |
| `AUTOPILOT_LIVE` | `1` enables the live SDK tests (development only). |

Set by the autopilot:

| Variable | Where |
|---|---|
| `AUTOPILOT=1`, `AUTOPILOT_UNATTENDED=1` | Every worker session. |
| `GIT_BRANCH_NAME`, `SPECIFY_FEATURE_DIRECTORY`, `SPECIFY_FEATURE` | Worker sessions: the pinned branch and feature directory. |
| `AUTOPILOT_MESSAGE` | `notify_command`. |
| `ENABLE_CLAUDEAI_MCP_SERVERS=false` | Sessions, unless `claudeai_connectors` allows connectors. |

The supervisor removes a parent Claude Code session's variables (`CLAUDECODE`, `CLAUDE_CODE_*`, …) before
starting workers, so launching from inside Claude Code is fine.
