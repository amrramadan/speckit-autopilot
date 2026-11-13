"""Configuration: dataclass defaults < user config < repo .autopilot.toml < CLI flags.

The user config (``~/.config/speckit-autopilot/config.toml``; see :func:`user_config_path`) holds personal defaults
and may contain per-repo tables keyed by the repo directory's basename or its absolute path:

    [repos."my-app"]
    test_cmd = "pnpm test"

The repo file (``<repo>/.autopilot.toml``) is meant to be committed in that repo.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PHASES: tuple[str, ...] = (
    "feature_start",
    "clarify",
    "plan",
    "tasks",
    "analyze",
    "implement",
    "review",
    "ship",
    "linear",
)

TEST_BASELINE_MODES: tuple[str, ...] = ("none", "base")
CHECKLISTS_GATE_MODES: tuple[str, ...] = ("judge", "halt")
SANDBOX_CONFIG_MODES: tuple[str, ...] = ("auto", "on", "off")  # = sessions.SANDBOX_MODES (config stays import-light)
LINEAR_MODES: tuple[str, ...] = ("auto", "on", "off")
# what a tracker issue key looks like (ENG-142, APP2-7): "auto" only turns Linear on for such an --issue
ISSUE_KEY_RE = r"^[A-Z][A-Z0-9]*-\d+$"

# ---- contract with repo-side skills (DESIGN.md §10) -----------------------------------
# Every worker session gets these variables, so a repo-side skill can tell an autopilot run from a person at the keyboard.
CONTRACT_ENV: dict[str, str] = {"AUTOPILOT": "1", "AUTOPILOT_UNATTENDED": "1"}
# The first line of every ship-command argument: a skill that honours it commits, pushes, opens and merges the PR and
# switches back to the base branch without asking.
SHIP_AUTHORIZATION = "AUTOPILOT-AUTHORIZED: commit push pr merge switch-base"

REPO_CONFIG_NAME = ".autopilot.toml"
CONFIG_ENV = "SPECKIT_AUTOPILOT_CONFIG"  # path of the user config file (overrides the XDG location)
STATE_ENV = "SPECKIT_AUTOPILOT_STATE"  # directory for cross-repo state (the run registry used by the live view)


def user_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "speckit-autopilot"


def user_config_path() -> Path:
    """The user-level config file: $SPECKIT_AUTOPILOT_CONFIG, else $XDG_CONFIG_HOME/speckit-autopilot/config.toml."""
    env = os.environ.get(CONFIG_ENV)
    return Path(env).expanduser() if env else user_config_dir() / "config.toml"


def user_state_dir() -> Path:
    """Cross-repo state: $SPECKIT_AUTOPILOT_STATE, else $XDG_STATE_HOME/speckit-autopilot."""
    env = os.environ.get(STATE_ENV)
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base).expanduser() / "speckit-autopilot"


@dataclass
class Config:
    repo: Path = field(default_factory=Path.cwd)
    issue: str = ""
    description: str = ""

    # ---- models ---------------------------------------------------------------------
    worker_model: str | None = None  # None -> CLI default (user settings.json "model")
    judge_model: str = "claude-opus-5-5"

    # ---- sessions -------------------------------------------------------------------
    context_reset_pct: float = 60.0
    max_turns_per_session: int = 15
    turn_timeout_min: float = 20.0
    implement_max_sessions: int = 8  # hard stop on implement resets without progress
    worker_effort: str | None = None  # passed as ClaudeAgentOptions.effort when set
    judge_effort: str | None = None
    # "fixed": every worker session uses worker_model / worker_effort. "auto": per-phase model + effort, escalated on
    # repair rounds, and the judge may recommend the next attempt's model / effort (see models.py)
    model_mode: str = "fixed"
    phase_models: dict[str, str] = field(default_factory=dict)  # auto: overrides models.DEFAULT_PHASE_MODELS per phase
    phase_efforts: dict[str, str] = field(default_factory=dict)  # auto: overrides models.DEFAULT_PHASE_EFFORTS per phase
    auto_models: list[str] = field(default_factory=lambda: ["sonnet", "opus"])  # auto: escalation ladder, weakest first
    auto_efforts: list[str] = field(default_factory=lambda: ["low", "medium", "high", "xhigh"])  # auto: allowed efforts
    auto_max_escalations: int = 2  # auto: repair-round escalation steps per phase

    # ---- repair / routing -----------------------------------------------------------
    max_repairs_per_phase: int = 2
    max_back_edges: int = 3
    max_back_per_target: int = 1
    # analyze: with the gate passing (no CRITICAL/HIGH), the judge's fixes for the remaining LOW/MEDIUM findings are
    # applied once and the phase passes; true re-runs a fresh analyze after them (the old loop, which can run out of
    # repairs chasing new LOW items)
    analyze_recheck_nonblocking: bool = False
    # review: with no blockers left, the judge's approved nit fixes are applied once, then tooling + the DoD check
    # decide in the same session; true re-runs a fresh review after them (the old loop: every fresh review finds new
    # nits, so it could halt with zero blockers once the repairs ran out)
    review_recheck_nonblocking: bool = False

    # ---- limits: none by default ------------------------------------------------------
    # Runs use the Claude subscription, so there is no USD budget and no wall-clock limit unless a config sets one
    # (unset = no limit, no budget cap on any session, nothing about limits in the output).
    total_budget_usd: float | None = None
    max_wallclock_h: float | None = None
    phase_budget_usd: dict[str, float] = field(default_factory=dict)
    judge_call_budget_usd: float | None = None
    judge_timeout_min: float = 10.0  # hang guard for one judge call (not a limit on the run)
    ship_min_remaining_usd: float = 3.0  # only with total_budget_usd set: the ship gate keeps this much in reserve

    # ---- ship / CI ------------------------------------------------------------------
    ci_timeout_min: float = 30.0
    ci_required: bool | None = None  # None = auto (True when a .github/workflows file triggers on push: CI is checked on the merge commit)
    ci_poll_s: float = 30.0

    # ---- notifications --------------------------------------------------------------
    notify_linear_on_halt: bool = True
    notify_command: str | None = None  # message in $AUTOPILOT_MESSAGE; {message} is replaced shell-quoted

    # ---- run flags ------------------------------------------------------------------
    allow_escalations: bool = False
    # a halted run resumes itself up to this many times (each resume re-runs the halted phase with fresh repair
    # rounds); budget / wall-clock / wrong-workspace halts and a halt that repeats word for word still stop. 0 = off
    auto_resume: int = 0
    until: str | None = None  # a phase to stop before; "none" clears the stop a resumed run was started with
    from_phase: str | None = None
    resume: bool = False
    dry_run: bool = False

    # ---- repo profile (auto-detected when empty / None) -----------------------------
    base_branch: str | None = None
    commands: dict[str, str] = field(default_factory=dict)  # phase -> "/slash-name"
    # Spec identity. the feature-start command takes a *spec id*, not a Linear key:
    #   "number" -> NNN (auto: next free number across specs/ and branches)
    #   "key"    -> e.g. spec-ABC or RATINGS-01 (must come from --spec or the issue title)
    spec_id_style: str = "number"
    spec_number_width: int = 3
    spec_title_regex: str | None = None  # regex with one group, applied to the Linear issue title
    spec: str | None = None  # --spec override
    slug_words: int = 4
    branch_template: str = "feature/{spec}-{slug}"
    feature_dir_template: str = "specs/{spec}-{slug}"
    feature_start_args: str = "{spec}"
    specify_args: str = "{issue}: {description}"
    review_args: str = "{base}"
    ship_args: str = SHIP_AUTHORIZATION + "\ncommit, push the branch, open a PR to {base}, merge it with a merge commit (gh pr merge --merge), then switch to {base} and pull --ff-only. Do not update Linear (the autopilot closes it out)."
    worker_env: dict[str, str] = field(default_factory=dict)  # extra env for worker sessions
    tooling_env: dict[str, str] = field(default_factory=dict)  # extra env for test/lint/typecheck/build
    judge_notes: str = ""  # repo-specific guidance appended to every judge prompt
    allowed_reply_commands: list[str] = field(default_factory=lambda: ["speckit-specify", "speckit.specify", "speckit.clarify"])
    test_cmd: str | None = None
    lint_cmd: str | None = None
    typecheck_cmd: str | None = None
    build_cmd: str | None = None
    tooling_timeout_min: float = 25.0
    # "none": any failing test fails the gates. "base": feature_start runs test_cmd once on the base commit (before the
    # branch exists); tests already failing there are tolerated, test by test, at implement / review / the ship gate.
    # Only pinnable output counts (gates.parse_test_failures): a bailing "pnpm -r test" or an "a && b" chain stays strict.
    test_baseline: str = "none"

    # ---- founder / human-owned work -------------------------------------------------
    # tasks.md tasks reserved for a person (FOUNDER GATE, Operator steps, [H], [RUN], ...): when any is
    # still open right before ship, the run pauses (exit 0) until a person ticks them and resumes.
    human_tasks_block_ship: bool = True
    # feature_dir/checklists/*.md: "judge" lets the judge answer speckit-implement's "proceed anyway?" question;
    # "halt" pauses the run before implement while any "- [ ]" item is open (the founder ticks them, then --resume).
    checklists_gate: str = "judge"
    # Linear integration: "on" (the issue is read at start, closed out after merge, halts are commented), "off" (no
    # Linear at all: --issue is optional, the close-out phase is skipped, every Linear tool is denied), "auto" (on only
    # when --issue looks like a tracker key AND a Linear MCP server is configured; see resolve_linear)
    linear: str = "auto"
    linear_server: str = "linear"
    # Extra local plugin directories for every worker/reader session (globs + ~ allowed). Needed when the Linear
    # server comes from a claude.ai-synced plugin, which SDK sessions do not load by themselves (verified live):
    #   plugin_dirs = ["~/.claude/plugins/synced/*/design~*"]
    plugin_dirs: list[str] = field(default_factory=list)
    # claude.ai connectors (e.g. "claude.ai Linear") load in SDK sessions and can shadow a plugin server with the same
    # URL, pointing at a different workspace (verified live). None = auto: enabled only when linear_server is one.
    claudeai_connectors: bool | None = None
    linear_workspace_url: str | None = None  # e.g. https://linear.app/your-team (verified in preflight)
    linear_project: str | None = None  # Linear project name/id for the overview block (auto from issue)
    # Issues that also track other work (e.g. a spec's "linear: { shared: true }"): the close-out only comments and updates
    # the project status block, never the issue's status. A spec.md front matter "shared: true" counts the same.
    shared_issues: list[str] = field(default_factory=list)
    status_marker_style: str = "html"  # html: <!-- autopilot:status -->, visible: [autopilot:status]
    reader_model: str | None = "claude-haiku-5-5"  # cheap model for Linear read-back sessions
    deny_bulk_git_add: bool = True
    linear_write_phases: list[str] = field(default_factory=lambda: ["linear"])
    push_phases: list[str] = field(default_factory=lambda: ["ship"])
    install_phases: list[str] = field(default_factory=lambda: ["implement"])
    # OS-level Bash sandbox for worker sessions (sessions.sandbox_settings): "auto" = on where the CLI can sandbox
    # (macOS sandbox-exec; Linux bubblewrap + socat), "on" = required (preflight fails without it), "off". Never applied
    # to feature_start, ship, linear or the halt notice (they need git/gh credentials or the network).
    sandbox: str = "auto"
    # Extra hosts sandboxed Bash may reach, on top of the package registries implement/review always get.
    sandbox_network_domains: list[str] = field(default_factory=list)
    # The supervisor's own test/lint/typecheck/build run the worker's code too (sessions.tooling_sandbox_profile):
    # "auto" = under macOS sandbox-exec unless sandbox = "off"; "on" = required (a run halts without it); "off".
    # Sandboxed tooling writes only in the repo, temp dirs and package caches, reads no credential files, reaches only
    # localhost (build commands keep the network) and never sees credential-like environment variables.
    tooling_sandbox: str = "auto"
    protected_paths: list[str] = field(
        default_factory=lambda: [
            ".specify/memory/constitution.md",
            ".github/workflows/**",
            ".gitlab-ci.yml",
            ".circleci/**",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "bun.lockb",
            "bun.lock",
            "poetry.lock",
            "uv.lock",
            "Cargo.lock",
            "Gemfile.lock",
            "composer.lock",
            "go.sum",
        ]
    )

    # ---- paths ----------------------------------------------------------------------
    python: str | None = None  # interpreter used by the detached launcher

    @property
    def linear_tool_prefix(self) -> str:
        """``linear`` -> ``mcp__linear__``; ``plugin:x:linear`` -> ``mcp__plugin_x_linear__``."""
        import re

        return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", self.linear_server) + "__"

    def session_env(self) -> dict[str, str]:
        """Environment every Claude session (worker, reader, judge) gets."""
        on = self.claudeai_connectors if self.claudeai_connectors is not None else self.linear_server.startswith("claude.ai")
        return {} if on else {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}

    def resolved_plugin_dirs(self) -> list[str]:
        import glob
        import os

        out: list[str] = []
        for pat in self.plugin_dirs:
            for p in sorted(glob.glob(os.path.expanduser(pat))):
                if os.path.isdir(p) and p not in out:
                    out.append(p)
        return out

    def phase_budget(self, phase: str) -> float | None:
        """The USD limit of one phase, or None (no limit) unless phase_budget_usd sets one."""
        v = self.phase_budget_usd.get(phase)
        return None if v is None else float(v)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["repo"] = str(self.repo)
        return d


_FIELD_NAMES = {f.name for f in dataclasses.fields(Config)}


class ConfigError(ValueError):
    pass


def _apply(cfg: Config, data: dict[str, Any], source: str) -> None:
    for key, value in data.items():
        if key == "repos":
            continue
        if key not in _FIELD_NAMES:
            raise ConfigError(f"{source}: unknown config key {key!r}")
        if key == "repo":
            continue
        current = getattr(cfg, key)
        value = _coerce(key, value, current, source)
        if isinstance(current, dict) and isinstance(value, dict):
            merged = dict(current)
            merged.update(value)
            setattr(cfg, key, merged)
        else:
            setattr(cfg, key, value)


_FIELD_TYPES = {f.name: f.type for f in dataclasses.fields(Config)}


def _coerce(key: str, value: Any, current: Any, source: str) -> Any:
    """Type-check a config value against the field's annotation; raise ConfigError instead of TypeError later."""
    t = str(_FIELD_TYPES.get(key, ""))
    optional = "None" in t
    if value is None:
        if optional:
            return None
        raise ConfigError(f"{source}: {key} may not be empty")
    def bad(expected: str) -> ConfigError:
        return ConfigError(f"{source}: {key} must be {expected} (got {value!r})")
    if t.startswith("bool"):
        if not isinstance(value, bool):
            raise bad("true/false")
    elif t.startswith("float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise bad("a number")
        value = float(value)
    elif t.startswith("int"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad("an integer")
    elif t.startswith("str") or t.startswith("Path"):
        if not isinstance(value, str):
            raise bad("a string")
    elif t.startswith("list"):
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise bad("a list of strings")
    elif t.startswith("dict"):
        if not isinstance(value, dict):
            raise bad("a table")
        if key in ("phase_models", "phase_efforts") and not all(isinstance(v, str) for v in value.values()):
            raise bad("a table of strings")
        if key == "phase_budget_usd" and not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value.values()):
            raise bad("a table of numbers")
    return value


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_config(repo: Path, overrides: dict[str, Any] | None = None, user_config: Path | None = None) -> Config:
    repo = repo.resolve()
    cfg = Config(repo=repo)
    ws_path = user_config if user_config is not None else user_config_path()
    ws = load_toml(ws_path)
    _apply(cfg, ws, str(ws_path))
    repo_tables = ws.get("repos", {}) or {}
    for key in (repo.name, str(repo)):
        if key in repo_tables:
            _apply(cfg, repo_tables[key], f"{ws_path} [repos.{key}]")
    repo_file = repo / REPO_CONFIG_NAME
    if repo_file.resolve() != ws_path.resolve():
        _apply(cfg, load_toml(repo_file), str(repo_file))
    if overrides:
        _apply(cfg, {k: v for k, v in overrides.items() if v is not None}, "CLI")
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    for name in ("until", "from_phase"):
        val = getattr(cfg, name)
        if val is not None and val not in PHASES and not (name == "until" and val == "none"):
            raise ConfigError(f"--{name.replace('_phase', '')} must be one of {', '.join(PHASES)} (got {val!r})")
    if not (1 <= cfg.context_reset_pct <= 100):
        raise ConfigError("context_reset_pct must be within 1..100")
    if cfg.auto_resume < 0:
        raise ConfigError("auto_resume must be >= 0")
    if cfg.max_repairs_per_phase < 0 or cfg.max_back_edges < 0:
        raise ConfigError("repair / back-edge limits must be >= 0")
    if cfg.spec_id_style not in ("number", "key"):
        raise ConfigError("spec_id_style must be 'number' or 'key'")
    if cfg.status_marker_style not in ("html", "visible"):
        raise ConfigError("status_marker_style must be 'html' or 'visible'")
    if cfg.test_baseline not in TEST_BASELINE_MODES:
        raise ConfigError(f"test_baseline must be one of {', '.join(TEST_BASELINE_MODES)} (got {cfg.test_baseline!r})")
    if cfg.checklists_gate not in CHECKLISTS_GATE_MODES:
        raise ConfigError(f"checklists_gate must be one of {', '.join(CHECKLISTS_GATE_MODES)} (got {cfg.checklists_gate!r})")
    if cfg.linear not in LINEAR_MODES:
        raise ConfigError(f"linear must be one of {', '.join(LINEAR_MODES)} (got {cfg.linear!r})")
    if cfg.sandbox not in SANDBOX_CONFIG_MODES:
        raise ConfigError(f"sandbox must be one of {', '.join(SANDBOX_CONFIG_MODES)} (got {cfg.sandbox!r})")
    if cfg.tooling_sandbox not in SANDBOX_CONFIG_MODES:
        raise ConfigError(f"tooling_sandbox must be one of {', '.join(SANDBOX_CONFIG_MODES)} (got {cfg.tooling_sandbox!r})")
    if cfg.total_budget_usd is not None and cfg.total_budget_usd <= 0:
        raise ConfigError("total_budget_usd must be > 0 when set (leave it out for no limit)")
    from .models import validate_model_config

    errs = validate_model_config(cfg, PHASES)
    if errs:
        raise ConfigError("; ".join(errs))
    for p in cfg.linear_write_phases + cfg.push_phases + cfg.install_phases:
        if p not in PHASES and p != "halt_notice":
            raise ConfigError(f"unknown phase {p!r} in a *_phases list")


# ---- Linear on/off ---------------------------------------------------------------------


def looks_like_issue_key(issue: str | None) -> bool:
    import re

    return bool(issue) and bool(re.match(ISSUE_KEY_RE, issue.strip()))


def _mcp_names(path: Path, repo: Path | None = None) -> set[str]:
    """MCP server names a Claude Code config file declares (.mcp.json, ~/.claude.json incl. its per-project tables)."""
    import json

    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    names = set((data.get("mcpServers") or {}).keys()) if isinstance(data.get("mcpServers"), dict) else set()
    projects = data.get("projects")
    if repo is not None and isinstance(projects, dict):
        proj = projects.get(str(repo)) or {}
        if isinstance(proj, dict) and isinstance(proj.get("mcpServers"), dict):
            names |= set(proj["mcpServers"].keys())
    return names


def linear_server_configured(cfg: Config, home: Path | None = None) -> tuple[bool, str]:
    """Whether the configured Linear MCP server is declared anywhere Claude Code reads MCP servers from (a cheap, static
    check: preflight confirms the server really connects). Plugin and claude.ai connector names cannot be resolved
    statically, so naming one in linear_server counts as configured."""
    name = cfg.linear_server
    if name.startswith("plugin:") or name.startswith("claude.ai"):
        return True, f"linear_server {name!r} names a plugin/connector server"
    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if home is None and claude_dir:  # Claude Code keeps .claude.json inside CLAUDE_CONFIG_DIR when that is set
        base = Path(claude_dir).expanduser()
        files = ((base / ".claude.json", cfg.repo), (base / "settings.json", None))
    else:
        home = home or Path.home()
        files = ((home / ".claude.json", cfg.repo), (home / ".claude" / "settings.json", None))
    for path, repo in ((cfg.repo / ".mcp.json", None), *files):
        if name in _mcp_names(path, repo):
            return True, f"MCP server {name!r} declared in {path}"
    return False, f"no MCP server named {name!r} in {cfg.repo / '.mcp.json'} or ~/.claude.json"


def resolve_linear(cfg: Config, home: Path | None = None) -> tuple[bool, str]:
    """(enabled, why) for this run's Linear integration."""
    if cfg.linear == "on":
        return True, "linear = on"
    if cfg.linear == "off":
        return False, "linear = off"
    if not looks_like_issue_key(cfg.issue):
        return False, f"auto: --issue {cfg.issue!r} is not a tracker key like ENG-142" if cfg.issue else "auto: no --issue given"
    ok, why = linear_server_configured(cfg, home)
    if not ok and cfg.linear_workspace_url:
        ok, why = True, f"linear_workspace_url is pinned ({cfg.linear_workspace_url})"
    return ok, f"auto: {why}"
