"""Settings sub-commands: ``speckit-autopilot config|model|effort|context-reset|sandbox|linear|statusline ...``.

They read and write the two config layers a person owns:

* the repo file ``<repo>/.autopilot.toml`` (default target; meant to be committed), and
* the user file (``--global``; see :func:`config.user_config_path`).

Writes are textual edits of top-level keys, so comments and ``[repos."..."]`` tables survive. Every write is validated
with :func:`config.load_config` and rolled back when the result does not load.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from .config import LINEAR_MODES, REPO_CONFIG_NAME, Config, ConfigError, linear_server_configured, load_config, resolve_linear, user_config_dir, user_config_path

ADMIN_COMMANDS = ("init", "config", "model", "effort", "context-reset", "sandbox", "linear", "statusline", "version")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MODEL_ROLES = {"worker": "worker_model", "judge": "judge_model", "reader": "reader_model"}
EFFORT_ROLES = {"worker": "worker_effort", "judge": "judge_effort"}
# Suggestions only: any model id or alias the Claude Code CLI accepts is allowed.
SUGGESTED_MODELS = ("opus", "sonnet", "haiku", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2")
FIELDS = {f.name: f for f in dataclasses.fields(Config)}
RUN_ONLY_KEYS = {"repo", "issue", "description", "resume", "dry_run", "from_phase", "python", "spec"}
STATUSLINE_MARK = "speckit-autopilot statusline"


# --------------------------------------------------------------------------------------
# TOML text editing
# --------------------------------------------------------------------------------------


def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # a JSON string is a valid TOML basic string
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{json.dumps(str(k))} = {toml_value(x)}" for k, x in v.items()) + " }"
    raise ConfigError(f"cannot write {v!r} as TOML")


def parse_cli_value(key: str, raw: str) -> Any:
    """``true`` / ``60`` / ``["a"]`` / ``{a = 1}`` parse as TOML; anything else is a plain string."""
    try:
        val = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        val = raw
    t = str(FIELDS[key].type)
    if t.startswith("str") and not isinstance(val, str):
        val = raw  # e.g. a model id that happens to look like a number
    if t.startswith("float") and isinstance(val, int) and not isinstance(val, bool):
        val = float(val)
    return val


_TABLE = re.compile(r"^\s*\[")


def _key_re(key: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{re.escape(key)}\s*=")


def _value_span(lines: list[str], i: int) -> int:
    """Index one past the last line of the value that starts on line ``i`` (multi-line arrays / triple quotes)."""
    first = lines[i].split("=", 1)[1]
    j = i + 1
    for opener, closer in (("[", "]"), ('"""', '"""'), ("'''", "'''")):
        if first.strip().startswith(opener):
            text = first
            while j <= len(lines):
                try:
                    tomllib.loads("v =" + text)
                    return j
                except tomllib.TOMLDecodeError:
                    if j >= len(lines):
                        return j
                    text += "\n" + lines[j]
                    j += 1
    return i + 1


def set_key_text(text: str, key: str, value: Any | None) -> str:
    """Set (or with ``value=None`` remove) a top-level key, keeping everything else byte-for-byte."""
    lines = text.splitlines()
    end_top = next((n for n, line in enumerate(lines) if _TABLE.match(line)), len(lines))
    pat = _key_re(key)
    for n in range(end_top):
        if pat.match(lines[n]):
            stop = _value_span(lines, n)
            new = [] if value is None else [f"{key} = {toml_value(value)}"]
            lines[n:stop] = new
            break
    else:
        if value is None:
            return text
        insert = [f"{key} = {toml_value(value)}"]
        # put it after the last top-level line that is not blank, before the first table
        at = end_top
        while at > 0 and not lines[at - 1].strip():
            at -= 1
        lines[at:at] = insert
    out = "\n".join(lines)
    return out + "\n" if out else ""


# --------------------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------------------


def find_repo(arg: str | None) -> Path | None:
    if arg:
        return Path(arg).expanduser().resolve()
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None


def target_file(args: argparse.Namespace) -> Path:
    if getattr(args, "global_", False):
        return user_config_path()
    repo = find_repo(getattr(args, "repo", None))
    if repo is None:
        raise SystemExit("not inside a git repository: pass --repo PATH, or --global for the user config")
    return repo / REPO_CONFIG_NAME


def write_key(args: argparse.Namespace, key: str, value: Any | None) -> Path:
    if key not in FIELDS or key in RUN_ONLY_KEYS:
        raise SystemExit(f"unknown or per-run key {key!r} (see: speckit-autopilot config keys)")
    path = target_file(args)
    old = path.read_text() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(set_key_text(old or "", key, value))
    try:
        repo = find_repo(getattr(args, "repo", None)) or Path.cwd()
        if getattr(args, "global_", False):
            tomllib.loads(path.read_text())
            load_config(repo, user_config=path)
        else:
            load_config(repo)
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        if old is None:
            path.unlink()
        else:
            path.write_text(old)
        raise SystemExit(f"not saved: {exc}")
    return path


def effective(args: argparse.Namespace) -> Config:
    repo = find_repo(getattr(args, "repo", None)) or Path.cwd()
    try:
        return load_config(repo)
    except ConfigError as exc:
        raise SystemExit(f"config error: {exc}")


def show(v: Any) -> str:
    return "(unset)" if v is None or v == {} or v == [] else (toml_value(v) if not isinstance(v, str) else v)


# --------------------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------------------

INIT_TEMPLATE = """\
# speckit-autopilot configuration ({where}).
# Precedence (later wins): built-in defaults < user config < [repos."<name>"] tables of the user config
#   < <repo>/.autopilot.toml < CLI flags.  Unknown keys are an error.  Full reference: docs/CONFIG.md
# Edit with:  speckit-autopilot config set KEY VALUE {flag}

# ---- models / effort ----------------------------------------------------------------
# worker_model = "opus"              # unset = the Claude Code CLI default
# judge_model = "claude-opus-5-5"
# reader_model = "claude-haiku-5-5"  # cheap model for Linear read-backs
# worker_effort = "high"             # low | medium | high | xhigh | max
# judge_effort = "high"
# model_mode = "fixed"               # fixed | auto (per-phase model + effort, escalated on repairs; see docs/CONFIG.md)
# phase_models = {{ plan = "opus", tasks = "sonnet" }}    # auto: override the built-in per-phase table
# phase_efforts = {{ implement = "xhigh" }}
# auto_models = ["sonnet", "opus"]   # auto: escalation ladder, weakest first (also what the judge may pick from)
# auto_efforts = ["low", "medium", "high", "xhigh"]
# auto_max_escalations = 2

# ---- sessions -----------------------------------------------------------------------
# context_reset_pct = 60.0           # start a fresh worker session at this % of the context window
# max_turns_per_session = 15
# turn_timeout_min = 20.0

# ---- tooling (auto-detected when unset: package.json scripts; pyproject -> test_cmd only)
# test_cmd = "pnpm test"
# lint_cmd = "pnpm lint"
# typecheck_cmd = "pnpm typecheck"
# build_cmd = "pnpm build"

# ---- Linear -------------------------------------------------------------------------
# linear = "auto"                    # auto | on | off (auto: on for --issue keys like ENG-142 when a Linear MCP server is configured)
# linear_server = "linear"           # MCP server name as Claude Code lists it
# linear_workspace_url = "https://linear.app/your-team"

# ---- safety -------------------------------------------------------------------------
# sandbox = "auto"                   # auto | on | off
# tooling_sandbox = "auto"
"""


def cmd_config(args: argparse.Namespace) -> int:
    act = args.action
    if act == "path":
        repo = find_repo(args.repo)
        print(f"user: {user_config_path()}{'' if user_config_path().exists() else '  (missing)'}")
        if repo:
            rf = repo / REPO_CONFIG_NAME
            print(f"repo: {rf}{'' if rf.exists() else '  (missing)'}")
        return 0
    if act == "keys":
        for name, f in FIELDS.items():
            if name not in RUN_ONLY_KEYS:
                print(f"{name}: {f.type}")
        return 0
    if act == "show":
        cfg = effective(args)
        for name in FIELDS:
            if name not in RUN_ONLY_KEYS:
                print(f"{name} = {show(getattr(cfg, name))}")
        return 0
    if act == "get":
        if not args.key or args.key not in FIELDS:
            raise SystemExit(f"unknown key {args.key!r}")
        print(show(getattr(effective(args), args.key)))
        return 0
    if act == "set":
        if not args.key or args.value is None:
            raise SystemExit("usage: speckit-autopilot config set KEY VALUE [--global]")
        if args.key not in FIELDS:
            raise SystemExit(f"unknown key {args.key!r} (see: speckit-autopilot config keys)")
        path = write_key(args, args.key, parse_cli_value(args.key, args.value))
        print(f"{args.key} = {show(getattr(effective(args), args.key))}   ({path})")
        return 0
    if act == "unset":
        if not args.key:
            raise SystemExit("usage: speckit-autopilot config unset KEY [--global]")
        path = write_key(args, args.key, None)
        print(f"removed {args.key} from {path}; effective value: {show(getattr(effective(args), args.key))}")
        return 0
    if act == "init":
        path = target_file(args)
        if path.exists() and not args.force:
            raise SystemExit(f"{path} exists (use --force to overwrite)")
        path.parent.mkdir(parents=True, exist_ok=True)
        where = "user config" if args.global_ else "repo config, commit it"
        path.write_text(INIT_TEMPLATE.format(where=where, flag="--global" if args.global_ else ""))
        print(f"wrote {path}")
        return 0
    raise SystemExit(f"unknown config action {act!r}")


# --------------------------------------------------------------------------------------
# model / effort / context-reset / sandbox
# --------------------------------------------------------------------------------------


def cmd_model(args: argparse.Namespace) -> int:
    if args.role in ("auto", "fixed"):
        if args.value is not None:
            raise SystemExit(f"usage: speckit-autopilot model {args.role} [--repo R] [--global]")
        path = write_key(args, "model_mode", args.role)
        print(f"model_mode = {args.role}   ({path})")
        args.role = "all"
    roles = list(MODEL_ROLES) if args.role == "all" else [args.role]
    if args.value is None:
        cfg = effective(args)
        print(f"mode    {cfg.model_mode}")
        if cfg.model_mode == "auto":
            from .models import PHASES as _P, phase_base

            for ph in _P:
                m, e = phase_base(cfg, ph)
                print(f"  {ph:<14} {m or 'default'} / {e or 'default'}")
            print(f"  escalation: up to {cfg.auto_max_escalations} step(s) per phase within models {list(cfg.auto_models)} · efforts {list(cfg.auto_efforts)}")
            roles = [r for r in roles if r != "worker"]  # the table above replaces worker_model
        for r in roles:
            print(f"{r:<7} {show(getattr(cfg, MODEL_ROLES[r]))}")
        print("suggested: " + ", ".join(SUGGESTED_MODELS) + "  (any id the Claude Code CLI accepts; 'default' unsets)")
        return 0
    for r in roles if args.role != "all" else ["worker", "judge"]:  # "all" never moves the cheap reader model
        val = None if args.value in ("default", "unset", "none") else args.value
        path = write_key(args, MODEL_ROLES[r], val)
        print(f"{r} model -> {val or '(default)'}   ({path})")
    return 0


def cmd_effort(args: argparse.Namespace) -> int:
    roles = list(EFFORT_ROLES) if args.role == "all" else [args.role]
    if args.value is None:
        cfg = effective(args)
        for r in roles:
            print(f"{r:<7} {show(getattr(cfg, EFFORT_ROLES[r]))}")
        print("levels: " + " | ".join(EFFORT_LEVELS) + "  ('default' unsets)")
        return 0
    val = None if args.value in ("default", "unset", "none") else args.value
    if val is not None and val not in EFFORT_LEVELS:
        raise SystemExit(f"effort must be one of {', '.join(EFFORT_LEVELS)} or 'default'")
    for r in roles:
        path = write_key(args, EFFORT_ROLES[r], val)
        print(f"{r} effort -> {val or '(default)'}   ({path})")
    return 0


def cmd_simple(args: argparse.Namespace, key: str, conv: Any) -> int:
    if args.value is None:
        print(f"{key} = {show(getattr(effective(args), key))}")
        return 0
    try:
        val = conv(args.value)
    except ValueError as exc:
        raise SystemExit(str(exc))
    path = write_key(args, key, val)
    print(f"{key} = {show(val)}   ({path})")
    return 0


def _pct(v: str) -> float:
    f = float(v.rstrip("%"))
    if not 1 <= f <= 100:
        raise ValueError("context-reset must be a percentage within 1..100")
    return f


def _linear(v: str) -> str:
    if v not in LINEAR_MODES:
        raise ValueError("linear must be auto, on or off")
    return v


def cmd_linear(args: argparse.Namespace) -> int:
    if args.value is not None:
        return cmd_simple(args, "linear", _linear)
    cfg = effective(args)
    print(f"linear = {cfg.linear}")
    if cfg.linear == "auto":
        probe = dataclasses.replace(cfg, issue="ENG-1")
        on, why = resolve_linear(probe)
        print(f"  with an issue key (e.g. --issue ENG-142): {'on' if on else 'off'} — {why.removeprefix('auto: ')}")
        print("  with no --issue, or a label that is not a key: off")
    return 0


def _sandbox(v: str) -> str:
    if v not in ("auto", "on", "off"):
        raise ValueError("sandbox must be auto, on or off")
    return v


# --------------------------------------------------------------------------------------
# statusline
# --------------------------------------------------------------------------------------


def statusline_script() -> Path:
    return Path(__file__).with_name("statusline.sh")


def statusline_command() -> str:
    return f"SPECKIT_AUTOPILOT_PYTHON={shlex.quote(sys.executable)} bash {shlex.quote(str(statusline_script()))}  # {STATUSLINE_MARK}"


def settings_file(scope: str, repo: Path | None) -> Path:
    if scope == "user":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "settings.json"
    if repo is None:
        raise SystemExit("not inside a git repository: pass --repo PATH or --scope user")
    return repo / ".claude" / ("settings.local.json" if scope == "local" else "settings.json")


def previous_file() -> Path:
    return user_config_dir() / "statusline-previous.json"


def cmd_statusline(args: argparse.Namespace) -> int:
    path = settings_file(args.scope, find_repo(args.repo))
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path} is not valid JSON ({exc}); fix it first")
    cur = data.get("statusLine") or {}
    ours = STATUSLINE_MARK in str(cur.get("command", ""))
    if args.action == "status":
        print(f"{path}: {'installed' if ours else ('another status line' if cur else 'no status line')}")
        if cur:
            print(f"  command: {cur.get('command')}")
        return 0
    if args.action == "install":
        if cur and not ours:  # keep the old one: statusline.sh shows it as row 1, uninstall restores it
            prev = previous_file()
            prev.parent.mkdir(parents=True, exist_ok=True)
            saved = json.loads(prev.read_text()) if prev.exists() else {}
            saved[str(path)] = cur
            prev.write_text(json.dumps(saved, indent=2))
        data["statusLine"] = {"type": "command", "command": statusline_command(), "refreshInterval": args.interval}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        print(f"installed in {path} (refresh every {args.interval}s); restart Claude Code or open /statusline to see it")
        return 0
    if args.action == "uninstall":
        if not ours:
            print(f"{path}: the autopilot status line is not installed there")
            return 0
        prev = previous_file()
        saved = json.loads(prev.read_text()) if prev.exists() else {}
        old = saved.pop(str(path), None)
        if old:
            data["statusLine"] = old
            prev.write_text(json.dumps(saved, indent=2))
        else:
            data.pop("statusLine", None)
        path.write_text(json.dumps(data, indent=2) + "\n")
        print(f"removed from {path}" + ("; restored your previous status line" if old else ""))
        return 0
    raise SystemExit(f"unknown statusline action {args.action!r}")


# --------------------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------------------

REPO_SKILLS = {"feature_start": ("cc-feature-start", "autopilot-feature-start"), "review": ("cc-review", "autopilot-review"), "ship": ("cc-ship", "autopilot-ship")}


def cmd_init(args: argparse.Namespace) -> int:
    """Prepare a repo: config template, the generic pipeline skills it lacks, git exclude, optional status line."""
    import shutil

    from .state import ensure_git_exclude

    repo = find_repo(args.repo)
    if repo is None:
        raise SystemExit("not inside a git repository: pass --repo PATH")
    src_root = Path(__file__).with_name("repo_skills")
    skills = repo / ".claude" / "skills"
    for phase, (own, generic) in REPO_SKILLS.items():
        mine = [p for p in (skills / own / "SKILL.md", repo / ".claude" / "commands" / f"{own}.md") if p.exists()]
        dest = skills / generic / "SKILL.md"
        if mine and not args.all_skills:
            print(f"{phase:<14} keeping your /{own}")
            continue
        if dest.exists() and not args.force:
            print(f"{phase:<14} /{generic} already installed")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_root / generic / "SKILL.md", dest)
        print(f"{phase:<14} installed /{generic} -> {dest.relative_to(repo)}")
    cfg = repo / REPO_CONFIG_NAME
    if not cfg.exists():
        has_linear, why = linear_server_configured(load_config(repo, user_config=None))
        mode = "on" if has_linear else "off"
        cfg.write_text(set_key_text(INIT_TEMPLATE.format(where="repo config, commit it", flag=""), "linear", mode))
        print(f"config         wrote {cfg.relative_to(repo)}")
        print(f"linear         {mode} ({why}); change with: speckit-autopilot linear auto|on|off")
        if has_linear:
            print("               next: speckit-autopilot config set linear_workspace_url https://linear.app/<your-team>"
                  " (required before a run touches Linear)")
    else:
        print(f"config         {cfg.relative_to(repo)} exists")
    if ensure_git_exclude(repo):
        print("git            added .autopilot/ to .git/info/exclude")
    if not (repo / ".specify").is_dir():
        print("spec kit       WARNING: no .specify/ here; install Spec Kit first (https://github.com/github/spec-kit)")
    if args.statusline:
        ns = argparse.Namespace(action="install", scope="local", interval=5, repo=str(repo))
        cmd_statusline(ns)
    print("next           speckit-autopilot --repo . --issue KEY \"description\" --dry-run")
    return 0


# --------------------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="speckit-autopilot", description="speckit-autopilot settings")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser, glob: bool = True) -> None:
        sp.add_argument("--repo", default=None, help="target repository (default: the git repo of the current directory)")
        if glob:
            sp.add_argument("--global", dest="global_", action="store_true", help="write the user config instead of <repo>/.autopilot.toml")

    i = sub.add_parser("init", help="prepare a repo: config template, generic pipeline skills, git exclude")
    i.add_argument("--repo", default=None)
    i.add_argument("--force", action="store_true", help="overwrite installed autopilot-* skills")
    i.add_argument("--all-skills", action="store_true", help="install the generic skills even where a cc-* skill exists")
    i.add_argument("--statusline", action="store_true", help="also install the status line in .claude/settings.local.json")

    c = sub.add_parser("config", help="show, get, set, unset or initialise configuration")
    c.add_argument("action", choices=("show", "get", "set", "unset", "init", "path", "keys"))
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")
    c.add_argument("--force", action="store_true", help="with init: overwrite an existing file")
    common(c)

    m = sub.add_parser("model", help="show or set the worker / judge / reader model")
    m.add_argument("role", nargs="?", default="all", choices=(*MODEL_ROLES, "all", "auto", "fixed"), help="a role, or auto|fixed to switch model_mode")
    m.add_argument("value", nargs="?", help="model id or alias; 'default' unsets")
    common(m)

    e = sub.add_parser("effort", help="show or set the worker / judge reasoning effort")
    e.add_argument("role", nargs="?", default="all", choices=(*EFFORT_ROLES, "all"))
    e.add_argument("value", nargs="?", help=" | ".join(EFFORT_LEVELS) + " | default")
    common(e)

    r = sub.add_parser("context-reset", help="show or set the context %% at which a worker session is replaced")
    r.add_argument("value", nargs="?")
    common(r)

    li = sub.add_parser("linear", help="show or set the Linear integration (auto|on|off)")
    li.add_argument("value", nargs="?")
    common(li)

    s = sub.add_parser("sandbox", help="show or set the OS sandbox mode for worker Bash (auto|on|off)")
    s.add_argument("value", nargs="?")
    common(s)

    sl = sub.add_parser("statusline", help="install / uninstall the live progress status line for Claude Code")
    sl.add_argument("action", choices=("install", "uninstall", "status"))
    sl.add_argument("--scope", choices=("local", "project", "user"), default="local", help="local = .claude/settings.local.json (default), project = .claude/settings.json, user = ~/.claude/settings.json")
    sl.add_argument("--interval", type=int, default=5, help="refresh interval in seconds (default 5)")
    common(sl, glob=False)

    sub.add_parser("version", help="print the version")
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        return cmd_init(args)
    if args.cmd == "config":
        return cmd_config(args)
    if args.cmd == "model":
        return cmd_model(args)
    if args.cmd == "effort":
        return cmd_effort(args)
    if args.cmd == "context-reset":
        return cmd_simple(args, "context_reset_pct", _pct)
    if args.cmd == "sandbox":
        return cmd_simple(args, "sandbox", _sandbox)
    if args.cmd == "linear":
        return cmd_linear(args)
    if args.cmd == "statusline":
        return cmd_statusline(args)
    if args.cmd == "version":
        from importlib.metadata import PackageNotFoundError, version

        try:
            print(version("speckit-autopilot"))
        except PackageNotFoundError:
            print("unknown (not installed)")
        return 0
    return 2
