# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `queue`: several specs in one repo, one after another (`queue add KEY[=SPEC] …`, `start`, `status`, `pause`, `remove`,
  `retry`, `clear`), with saved progress, `--after` dependencies and `--on-halt stop|skip`.
- With Linear on, a run can be launched with the issue key alone: the issue title becomes the description.
- `founder_decisions` (config, default `"human"`): with `"agent"` the judge takes the human-only tasks that are only a
  decision (`FOUNDER GATE: approve …`) before implement. The decision is recorded as the decision agent's and listed
  in the report; work a person performs is never delegated, and the judge may leave a decision to the person.
- `auto_resume` (config) / `--auto-resume N` (CLI): a halted run resumes itself up to N times, re-running the halted
  phase with fresh repair rounds. Budget, wall-clock and wrong-workspace halts, and a halt that repeats word for
  word, still stop. Default `0` (off).

### Changed

- Review no longer loops on nits. When a review reports no blockers, the judge's approved nit fixes are applied once
  (or accepted when no repair round is left), then tooling and the Definition-of-Done check decide in the same
  session. A run used to halt after several rounds with zero blockers because every fresh review found new nits.
  Set `review_recheck_nonblocking = true` to keep the old loop.
- Analyze no longer halts when its gate passes on the last round: the remaining LOW/MEDIUM findings are accepted
  when no repair round is left to apply the judge's fixes.

### Fixed

- The `notify_command` example in the docs pasted the message into AppleScript source, so a quote in a halt reason could
  run as code. It now passes the message as an argument; change it if you copied the old one.
- A worker turn that fails because the model API refused or dropped the call (403 block page, 429, 5xx, connection
  errors) waits and is re-sent to the same session instead of spending a repair round (`transient_api_retries = 5`,
  `transient_api_backoff_s = 60`, doubling up to 15 minutes).
- Ship no longer mistakes an earlier run's merged PR on the same branch name for this run's merge.
- A judge "forward" after analyze or review used up its repairs always halted ("the deterministic gate still
  fails"), because those phases cannot be re-gated without a fresh run. It now advances when the last round's
  deterministic gate passed and only judge-requested, non-blocking work was left.

## [0.1.0] - unreleased

First public release.

### Added

- `speckit-autopilot` CLI and Python package (`speckit_autopilot`): drives one feature through
  feature_start → clarify → plan → tasks → analyze → implement → review → ship → linear, with a judge,
  deterministic gates, bounded repairs, context resets and a resumable run state.
- Claude Code plugin (`.claude-plugin/`) with the `/autopilot` skill: run, resume, status, watch, stop, report,
  dry-run, abandon, init, model, effort, context-reset, sandbox, statusline and config subcommands.
- Layered configuration: user config (`~/.config/speckit-autopilot/config.toml` or `$SPECKIT_AUTOPILOT_CONFIG`),
  repo `.autopilot.toml`, then CLI flags. `speckit-autopilot config show|get|set|unset|init|path|keys`.
- Optional Linear integration: `linear = "auto" | "on" | "off"`. `auto` turns Linear on only when the issue looks
  like an issue key and a Linear MCP server (or `linear_workspace_url`) is configured. Without Linear, the
  feature description is the run's identity.
- Worker model selection: `model_mode = "fixed"` (per-phase models and efforts) or `"auto"` (start cheap, escalate
  effort and model on failed verification, accept the judge's recommendation).
- Bundled repo-side skills (`autopilot-feature-start`, `autopilot-review`, `autopilot-ship`) installed by
  `speckit-autopilot init` for repos that don't define their own feature-start/review/ship commands.
- Portable Claude Code status line (`speckit-autopilot statusline install|uninstall|status`).
- Run registry in the user state directory, so `watch`/`status` find runs in any repo.
