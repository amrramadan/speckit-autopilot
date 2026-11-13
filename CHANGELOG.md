# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
