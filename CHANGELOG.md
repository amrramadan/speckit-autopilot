# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

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
