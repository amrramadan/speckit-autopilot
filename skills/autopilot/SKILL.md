---
name: autopilot
description: "Launch, watch, stop or report on a Spec Kit Autopilot run (one issue -> spec -> plan -> tasks -> analyze -> implement -> review -> merged PR -> issue close-out) as a detached background process, and configure it: models, effort, context reset, sandbox, Linear on/off, status line. This session only launches, watches and configures; it never performs pipeline steps itself."
argument-hint: "[KEY] \"description\" [flags] | resume | status | watch | stop | report | dry-run KEY | abandon | init | model [role] [id] | model auto|fixed | effort [role] [level] | context-reset [pct] | sandbox [mode] | linear [auto|on|off] | statusline [install|uninstall|status] | config [show|get|set|unset|path] | help"
disable-model-invocation: true
---

# /autopilot — launcher, watcher and settings for speckit-autopilot

Arguments: $ARGUMENTS

The supervisor is the `speckit-autopilot` CLI. It runs in its own process and drives its own Claude sessions.
**You only launch, watch and configure.**

## Hard rules

- **Never perform pipeline steps here.** Do not run the repo's feature-start / `speckit.*` / review / ship commands,
  do not edit spec artifacts or code, do not run `git`/`gh` write commands in the target repo, do not call issue-tracker
  write tools. Everything goes through the CLI.
- Never edit `.autopilot/state.json`, and never tick human-only tasks or checklist items.
- No foreground `sleep`. Watch with the Monitor tool (see `references/watching.md`).
- Never add `--allow-escalations` or `--skip-preflight` unless the user typed it.

## 0. Find the CLI

Let `SA` be the first that works: `speckit-autopilot` on PATH, else `uvx --from git+https://github.com/amrramadan/speckit-autopilot speckit-autopilot`. Check with
`SA version`. If neither works, stop and show the install line from the plugin README
(`uv tool install git+https://github.com/amrramadan/speckit-autopilot`); do not install anything yourself unless the
user asks.

## 1. Resolve the target repo

1. `--repo <R>` in the arguments wins (absolute, or relative to the current directory).
2. Else the git top level of the current directory (`git rev-parse --show-toplevel`).
3. Else ask the user which repo. Do not guess.

Always pass `--repo REPO` explicitly. If the repo is someone's everyday checkout with uncommitted work, say once that
a separate clone or worktree is safer (preflight refuses a dirty tree anyway).

## 2. Sub-commands

### Runs

| User types | Run |
|---|---|
| `/autopilot KEY "description" [flags]` | `SA --repo REPO --issue KEY "description" [flags] --detach` |
| `/autopilot "description" [flags]` (no key) | `SA --repo REPO "description" [flags] --detach` — runs without Linear; the run is labelled by the description |
| `/autopilot resume [flags]` | `SA --repo REPO --resume [flags] --detach` (issue + description come from the saved state) |
| `/autopilot … --from PHASE` | same, with `--from PHASE --detach` |
| `/autopilot dry-run KEY ["description"] [flags]` | `SA --repo REPO --issue KEY ["description"] --dry-run [--no-judge-ping] [--preflight-run-tests]` — foreground (seconds); with `--preflight-run-tests` use Bash `run_in_background` |
| `/autopilot status` | `SA --repo REPO --status` |
| `/autopilot watch` | `SA --repo REPO --watch --once` and summarise; for continuous updates start the Monitor from `references/watching.md` |
| `/autopilot stop` | `SA --repo REPO --stop` (SIGTERM, waits up to 3 min for the checkpoint) |
| `/autopilot report` | `SA --repo REPO --report` |
| `/autopilot abandon` | confirm with the user first, then `SA --repo REPO --abandon` (archives run state; never touches git or the tracker) |

Pass through unchanged: `--linear`, `--no-linear`, `--spec ID`, `--until PHASE`, `--allow-escalations`, `--judge-model M`, `--worker-model M`,
`--cli-path PATH`. Phases: `feature_start clarify plan tasks analyze implement review ship linear`; `--until P` stops
**before** P. Quote the description as one shell argument. When the repo's config has `spec_id_style = "key"` and the
user gave no `--spec`, ask for it before launching.

After a detached launch, follow `references/watching.md` (show pid + paths, check the console after ~10 s, watch,
summarise the outcome).

### Setup and settings

All settings sub-commands write `<repo>/.autopilot.toml` by default; add `--global` for the user config
(`~/.config/speckit-autopilot/config.toml`). Every write is validated and rolled back if invalid — relay the CLI's
message verbatim.

| User types | Run |
|---|---|
| `/autopilot init [--statusline]` | `SA init --repo REPO [--statusline]` — config template, generic `autopilot-*` pipeline skills where the repo has no `cc-*` ones, `.autopilot/` git exclude |
| `/autopilot model` | `SA model --repo REPO` (show) |
| `/autopilot model ROLE ID [--global]` | `SA model ROLE ID --repo REPO [--global]` — ROLE = `worker` \| `judge` \| `reader` \| `all` (worker+judge); `default` unsets |
| `/autopilot model auto\|fixed [--global]` | `SA model auto\|fixed --repo REPO [--global]` — `auto`: per-phase model + effort (cheap phases on sonnet, plan/analyze/implement/review on opus), one step up per repair round, and the judge may pick the next attempt's model within `auto_models` / `auto_efforts`; `fixed` (default): `worker_model` / `worker_effort` everywhere |
| `/autopilot effort` | `SA effort --repo REPO` (show) |
| `/autopilot effort ROLE LEVEL [--global]` | `SA effort ROLE LEVEL --repo REPO [--global]` — ROLE = `worker` \| `judge` \| `all`; LEVEL = `low medium high xhigh max default` |
| `/autopilot context-reset [PCT]` | `SA context-reset [PCT] --repo REPO [--global]` — a worker session is replaced by a fresh one (with a checkpoint hand-off) once its context reaches PCT % (default 60) |
| `/autopilot sandbox [auto\|on\|off]` | `SA sandbox [MODE] --repo REPO [--global]` |
| `/autopilot linear [auto\|on\|off]` | `SA linear [MODE] --repo REPO [--global]` — `auto` (default): Linear only for issue keys like `ENG-142` when a Linear MCP server is configured; `off`: no Linear at all (the close-out phase is skipped) |
| `/autopilot statusline [install\|uninstall\|status] [--scope local\|project\|user]` | `SA statusline ACTION --repo REPO [--scope S]` (default action `status`, default scope `local`) |
| `/autopilot config show\|get KEY\|set KEY VALUE\|unset KEY\|path\|keys [--global]` | `SA config … --repo REPO [--global]` |
| `/autopilot help` | print this table in short form |

**Interactive choice.** When `model` or `effort` is given a ROLE but no value — or no arguments and the user clearly
wants to change it ("let me pick the model") — first run the show command, then ask with AskUserQuestion:

- model with no role and the user wants to change "how models are picked": options `auto` (per phase, escalates on
  repairs) and `fixed` (one model everywhere), then run `SA model auto|fixed`.
- model: options `opus`, `sonnet`, `haiku`, plus "keep current"; mention that any model id the Claude Code CLI accepts
  works via "Other". Ask which role(s) if the user did not say.
- effort: options `low`, `medium`, `high`, `max` (and "Other" covers `xhigh` / `default`).
- linear (no value, user wants to change it): show first, then options `auto` (recommended), `on`, `off`.
- also ask "this repo or all my repos (--global)?" when it is not obvious.

Then run the set command and show its output. Settings apply to the next run or `--resume`, not to a run in progress;
say so if one is running (`SA --repo REPO --status`).

## 3. Context hygiene for this session

Watching a long run can fill this conversation. When this session's context passes about 60 %, stop watching, tell
the user the run keeps going without this session, give them `/autopilot status` and `/autopilot watch` to re-attach
after `/clear`, and end the turn. The supervisor applies the same rule to its own worker sessions
(`context_reset_pct`).
