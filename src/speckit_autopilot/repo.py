"""Git and shell helpers plus repo-profile auto-detection (all plain code, no AI)."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

# --------------------------------------------------------------------------------------
# Command execution
# --------------------------------------------------------------------------------------


@dataclass
class CmdResult:
    cmd: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def tail(self, n: int = 40) -> str:
        text = (self.stdout or "") + ("\n" + self.stderr if self.stderr else "")
        lines = text.strip().splitlines()
        return "\n".join(lines[-n:])


Runner = Callable[[str, Path, float], CmdResult]


_RUNNING: set[subprocess.Popen] = set()
_RUNNING_LOCK = threading.Lock()  # tooling runs in worker threads; a stop kills from the event-loop thread
# How long a stopped tooling command gets to exit on SIGTERM before its process group is SIGKILLed (a test runner
# that traps TERM, or a slow docker compose / web-server teardown, must not outlive the run that started it).
KILL_GRACE_S = 10.0
# Environment variable names that hold credentials: never handed to the supervisor's tooling (it runs worker-written
# code). SSH_AUTH_SOCK lets a process use the user's ssh agent (git push with their key).
SECRET_ENV_NAME = re.compile(r"(?i)TOKEN|SECRET|PASSW(?:OR)?D|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY|CREDENTIAL|AUTH|COOKIE|SESSION_?KEY")


def secret_env_names(env: dict[str, str] | None = None) -> list[str]:
    """Names in ``env`` (default: os.environ) that look like credentials."""
    return sorted(k for k in (os.environ if env is None else env) if SECRET_ENV_NAME.search(k))


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _escalate(pgids: list[int], grace_s: float) -> None:
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and any(_group_alive(g) for g in pgids):
        time.sleep(0.05)
    import signal as _signal

    for g in pgids:
        try:
            os.killpg(g, _signal.SIGKILL)
        except OSError:
            pass


def kill_running_commands(grace_s: float | None = None) -> list[int]:
    """Stop every tooling command started by run_shell (whole process group): SIGTERM now, SIGKILL after
    ``grace_s`` (default KILL_GRACE_S) for any group still alive. Used by the stop signal handler."""
    import signal as _signal

    killed = []
    with _RUNNING_LOCK:
        running = list(_RUNNING)
    for p in running:
        try:
            os.killpg(p.pid, _signal.SIGTERM)
            killed.append(p.pid)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if killed:
        threading.Thread(target=_escalate, args=(killed, KILL_GRACE_S if grace_s is None else grace_s), daemon=True, name="autopilot-kill").start()
    return killed


def wait_for_commands(timeout_s: float) -> list[int]:
    """Wait until no run_shell command is running (each is reaped by its own thread); the pids still running then."""
    deadline = time.monotonic() + timeout_s
    while True:
        with _RUNNING_LOCK:
            left = [p.pid for p in _RUNNING]
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.05)


# Sandboxed processes may not run setuid programs: under macOS Seatbelt even "(version 1)(allow default)" gets EPERM
# from execve, and test suites call `ps` to check that child processes are gone (seen with a
# DOM evaluator suite). A non-setuid, ad-hoc-signed copy of the program sees the user's own processes the same way, so it goes
# first on PATH for sandboxed commands (tooling and worker Bash). A test that runs /bin/ps by absolute path still fails.
SETUID_SHIMS: dict[str, str] = {"ps": "/bin/ps"}
_SHIM_CACHE: dict[str, str | None] = {}
_SHIM_LOCK = threading.Lock()


def _shim_base() -> Path:
    if os.environ.get("AUTOPILOT_SHIM_DIR"):
        return Path(os.environ["AUTOPILOT_SHIM_DIR"]).expanduser()
    return Path.home() / "Library" / "Caches" / "speckit-autopilot" / "shims"


def _shim_works(path: Path) -> bool:
    try:
        r = subprocess.run(["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", str(path), "-o", "pid=", "-p", str(os.getpid())], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and r.stdout.strip() == str(os.getpid())


def sandbox_shim_dir(base: Path | None = None) -> str | None:
    """A directory holding non-setuid copies of SETUID_SHIMS that run inside the sandbox, or None (not macOS, or a copy
    could not be built). Built once per source binary (rebuilt after an OS update changes it) and cached per process."""
    import shutil
    import stat
    import sys

    if sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"):
        return None
    base = Path(base) if base is not None else _shim_base()
    key = str(base)
    with _SHIM_LOCK:
        if key in _SHIM_CACHE:
            return _SHIM_CACHE[key]
        result: str | None = str(base)
        try:
            base.mkdir(parents=True, exist_ok=True)
            for name, src in SETUID_SHIMS.items():
                st = os.stat(src)
                stamp = f"{st.st_size}:{st.st_mtime_ns}"
                dst, marker = base / name, base / f".{name}.source"
                if dst.exists() and marker.exists() and marker.read_text() == stamp and not (dst.stat().st_mode & stat.S_ISUID) and _shim_works(dst):
                    continue
                tmp = base / f".{name}.{os.getpid()}.tmp"
                shutil.copyfile(src, tmp)  # copies the bytes only: no setuid bit, no extended attributes
                os.chmod(tmp, 0o755)
                subprocess.run(["xattr", "-c", str(tmp)], capture_output=True, timeout=20)
                subprocess.run(["codesign", "--force", "-s", "-", str(tmp)], capture_output=True, timeout=60, check=True)
                if not _shim_works(tmp):
                    tmp.unlink(missing_ok=True)
                    result = None
                    break
                os.replace(tmp, dst)
                marker.write_text(stamp)
        except (OSError, subprocess.SubprocessError):
            result = None
        _SHIM_CACHE[key] = result
        return result


def with_shims_on_path(env: dict[str, str]) -> dict[str, str]:
    """``env`` with sandbox_shim_dir() first on PATH (unchanged when there is no shim directory)."""
    shim = sandbox_shim_dir()
    if not shim:
        return env
    parts = [p for p in (env.get("PATH") or os.environ.get("PATH") or "").split(os.pathsep) if p and p != shim]
    return {**env, "PATH": os.pathsep.join([shim, *parts])}


def run_shell(
    cmd: str,
    cwd: Path,
    timeout_s: float = 600.0,
    env: dict[str, str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    *,
    sandbox_profile: str | None = None,
    scrub_secrets: bool = False,
) -> CmdResult:
    """Run ``cmd`` through the user's shell so PATH (node, pnpm, uv...) matches theirs.

    The command gets its own process group so a timeout or a stop request kills the whole tree. ``should_stop`` is
    checked once the process is registered: a stop that took its kill snapshot just before then is not missed.
    ``sandbox_profile``: a Seatbelt profile (sessions.tooling_sandbox_profile) the command runs under (sandbox-exec),
    with the non-setuid shims (sandbox_shim_dir) first on PATH.
    ``scrub_secrets``: drop credential-like variables (SECRET_ENV_NAME) from the inherited environment; ``env`` is
    applied after, so a repo can still pass one explicitly. CI is never set here: it changes what test suites do
    (e.g. a vitest config that only redirects DB suites to the `_test` database when CI is unset)."""
    import signal as _signal

    shell = os.environ.get("SHELL") or "/bin/bash"
    if not Path(shell).exists():
        shell = "/bin/sh"
    start = time.monotonic()
    full_env = dict(os.environ)
    full_env.pop("CLAUDECODE", None)
    if scrub_secrets:
        for k in secret_env_names(full_env):
            full_env.pop(k, None)
    if env:
        full_env.update(env)
    argv = [shell, "-c", cmd]
    if sandbox_profile:
        argv = ["/usr/bin/sandbox-exec", "-p", sandbox_profile, *argv]
        full_env = with_shims_on_path(full_env)
    p = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=full_env, stdin=subprocess.DEVNULL, start_new_session=True)
    with _RUNNING_LOCK:
        _RUNNING.add(p)
    if should_stop is not None and should_stop():
        try:
            os.killpg(p.pid, _signal.SIGTERM)
        except OSError:
            pass
    try:
        out, err = p.communicate(timeout=timeout_s)
        return CmdResult(cmd, p.returncode, out, err, time.monotonic() - start)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, _signal.SIGKILL)
        except OSError:
            pass
        out, err = p.communicate()
        return CmdResult(cmd, 124, out or "", err or "", time.monotonic() - start, timed_out=True)
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(p)


def git(repo: Path, *args: str, timeout_s: float = 120.0) -> CmdResult:
    start = time.monotonic()
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return CmdResult("git " + " ".join(shlex.quote(a) for a in args), p.returncode, p.stdout, p.stderr, time.monotonic() - start)
    except subprocess.TimeoutExpired:
        return CmdResult("git " + " ".join(args), 124, "", "timeout", time.monotonic() - start, timed_out=True)


def gh(repo: Path, *args: str, timeout_s: float = 60.0) -> CmdResult:
    start = time.monotonic()
    try:
        p = subprocess.run(
            ["gh", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"},
        )
        return CmdResult("gh " + " ".join(args), p.returncode, p.stdout, p.stderr, time.monotonic() - start)
    except FileNotFoundError:
        return CmdResult("gh " + " ".join(args), 127, "", "gh not installed")
    except subprocess.TimeoutExpired:
        return CmdResult("gh " + " ".join(args), 124, "", "timeout", time.monotonic() - start, timed_out=True)


# --------------------------------------------------------------------------------------
# Git queries
# --------------------------------------------------------------------------------------


class Git:
    """Thin, testable wrapper. Tests point it at a temp repo with a bare 'origin'."""

    def __init__(self, repo: Path):
        self.repo = repo

    def out(self, *args: str) -> str:
        r = git(self.repo, *args)
        return r.stdout.strip() if r.ok else ""

    def ok(self, *args: str) -> bool:
        return git(self.repo, *args).ok

    def current_branch(self) -> str:
        return self.out("rev-parse", "--abbrev-ref", "HEAD")

    def head(self, ref: str = "HEAD") -> str:
        return self.out("rev-parse", ref)

    def status_entries(self) -> list[tuple[str, str]]:
        """(XY status, path) pairs from ``git status --porcelain -z`` (unquoted paths; renames give the new path)."""
        r = git(self.repo, "status", "--porcelain", "-z", "--untracked-files=all")
        parts = r.stdout.split("\0")
        out: list[tuple[str, str]] = []
        i = 0
        while i < len(parts):
            entry = parts[i]
            i += 1
            if len(entry) < 4:
                continue
            xy, path = entry[:2], entry[3:]
            if xy[0] in "RC":
                i += 1  # the original path follows as its own NUL-separated field
            if path.startswith(".autopilot/"):
                continue
            out.append((xy, path))
        return out

    def status_porcelain(self) -> list[str]:
        return [f"{xy} {p}" for xy, p in self.status_entries()]

    def is_clean(self) -> bool:
        return not self.status_porcelain()

    def fetch(self, remote: str = "origin", *refs: str) -> CmdResult:
        return git(self.repo, "fetch", "--quiet", remote, *refs, timeout_s=180)

    def ref_exists(self, ref: str) -> bool:
        return self.ok("rev-parse", "--verify", "--quiet", ref)

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", a, b)

    def ahead_behind(self, left: str, right: str) -> tuple[int, int] | None:
        """Commits only in ``left`` and only in ``right``."""
        o = self.out("rev-list", "--left-right", "--count", f"{left}...{right}")
        m = re.match(r"(\d+)\s+(\d+)", o)
        return (int(m.group(1)), int(m.group(2))) if m else None

    def diff_names(self, base: str, head: str = "HEAD") -> list[str]:
        o = self.out("diff", "--name-only", f"{base}...{head}")
        return [l for l in o.splitlines() if l.strip()]

    def uncommitted_names(self) -> list[str]:
        return [p for _, p in self.status_entries()]

    def origin_head_branch(self) -> str | None:
        o = self.out("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if o.startswith("origin/"):
            return o.split("/", 1)[1]
        for cand in ("main", "master"):
            if self.ref_exists(f"refs/remotes/origin/{cand}"):
                return cand
        return None

    def remote_slug(self) -> str | None:
        url = self.out("remote", "get-url", "origin")
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
        return m.group(1) if m else None

    def branch_exists_remote(self, branch: str) -> bool:
        return self.ref_exists(f"refs/remotes/origin/{branch}")


# --------------------------------------------------------------------------------------
# Repo profile
# --------------------------------------------------------------------------------------

SPECKIT_PHASES = ("specify", "clarify", "plan", "tasks", "analyze", "implement")


@dataclass
class RepoProfile:
    repo: Path
    base_branch: str = "main"
    speckit_version: str | None = None
    script_type: str = "sh"
    commands: dict[str, str] = field(default_factory=dict)  # phase -> "/name"
    command_files: dict[str, str] = field(default_factory=dict)  # phase -> path of definition
    command_candidates: dict[str, list[str]] = field(default_factory=dict)
    tooling: dict[str, str | None] = field(default_factory=dict)  # test/lint/typecheck/build
    package_manager: str | None = None
    has_ci: bool = False
    ci_workflows: list[str] = field(default_factory=list)
    remote_slug: str | None = None
    constitution_path: Path | None = None
    extensions_file: Path | None = None
    notes: list[str] = field(default_factory=list)


def _skill_or_command(repo: Path, names: Sequence[str]) -> tuple[str | None, str | None, list[str]]:
    """Return (slash name, defining file, all candidate names found)."""
    found: list[tuple[str, str]] = []
    for name in names:
        skill = repo / ".claude" / "skills" / name / "SKILL.md"
        if skill.exists():
            found.append((name, str(skill)))
        cmd = repo / ".claude" / "commands" / f"{name}.md"
        if cmd.exists():
            found.append((name, str(cmd)))
    home = Path.home() / ".claude"
    for name in names:
        for p in (home / "skills" / name / "SKILL.md", home / "commands" / f"{name}.md"):
            if p.exists():
                found.append((name, str(p)))
    if not found:
        return None, None, []
    name, path = found[0]
    return "/" + name, path, sorted({"/" + n for n, _ in found})


def detect_package_manager(repo: Path) -> str | None:
    for lock, pm in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun"), ("package-lock.json", "npm")):
        if (repo / lock).exists():
            return pm
    if (repo / "package.json").exists():
        try:
            pm = json.loads((repo / "package.json").read_text()).get("packageManager", "")
            if pm:
                return pm.split("@", 1)[0]
        except (json.JSONDecodeError, OSError):
            pass
        return "npm"
    if (repo / "uv.lock").exists():
        return "uv"
    if (repo / "poetry.lock").exists():
        return "poetry"
    return None


def detect_tooling(repo: Path, pm: str | None) -> dict[str, str | None]:
    tooling: dict[str, str | None] = {"test": None, "lint": None, "typecheck": None, "build": None}
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {}) or {}
        except (json.JSONDecodeError, OSError):
            scripts = {}
        run = {"pnpm": "pnpm", "yarn": "yarn", "bun": "bun run", "npm": "npm run"}.get(pm or "npm", "npm run")
        prefs = {
            "test": ["test:ci", "test:unit", "test"],
            "lint": ["lint"],
            "typecheck": ["typecheck", "type-check", "check-types", "tsc", "types"],
            "build": ["build"],
        }
        for kind, names in prefs.items():
            for n in names:
                if n in scripts:
                    tooling[kind] = f"{run} {n}" if not (pm == "npm" and n == "test") else "npm test"
                    break
    elif (repo / "pyproject.toml").exists():
        tooling["test"] = "uv run pytest -q" if (repo / "uv.lock").exists() else "python -m pytest -q"
    return tooling


def detect_profile(repo: Path, cfg_commands: dict[str, str] | None = None, cfg_tooling: dict[str, str | None] | None = None, base_branch: str | None = None) -> RepoProfile:
    prof = RepoProfile(repo=repo)
    g = Git(repo)
    prof.base_branch = base_branch or g.origin_head_branch() or "main"
    prof.remote_slug = g.remote_slug()
    init = repo / ".specify" / "init-options.json"
    if init.exists():
        try:
            data = json.loads(init.read_text())
            prof.speckit_version = data.get("speckit_version")
            prof.script_type = data.get("script", "sh")
        except (json.JSONDecodeError, OSError):
            pass
    # commands: a repo's own cc-* skills win, then the generic autopilot-* skills that `speckit-autopilot init`
    # installs; speckit commands in both hyphen and dot spellings
    wanted = {
        "feature_start": ["cc-feature-start", "autopilot-feature-start"],
        "review": ["cc-review", "autopilot-review"],
        "ship": ["cc-ship", "autopilot-ship"],
    }
    for ph in SPECKIT_PHASES:
        wanted[ph] = [f"speckit-{ph}", f"speckit.{ph}"]
    for phase, names in wanted.items():
        slash, path, cands = _skill_or_command(repo, names)
        if slash:
            prof.commands[phase] = slash
            prof.command_files[phase] = path or ""
            prof.command_candidates[phase] = cands
    if cfg_commands:
        prof.commands.update({k: v for k, v in cfg_commands.items() if v})
    prof.package_manager = detect_package_manager(repo)
    prof.tooling = detect_tooling(repo, prof.package_manager)
    if cfg_tooling:
        # None = keep the detected command; "" = explicitly disabled (skip that check)
        prof.tooling.update({k: (v or None) for k, v in cfg_tooling.items() if v is not None})
    wf = repo / ".github" / "workflows"
    if wf.is_dir():
        prof.ci_workflows = sorted(p.name for p in wf.glob("*.y*ml"))
        prof.has_ci = any(_workflow_runs_on_push_or_pr(p) for p in wf.glob("*.y*ml"))
    const = repo / ".specify" / "memory" / "constitution.md"
    prof.constitution_path = const if const.exists() else None
    ext = repo / ".specify" / "extensions.yml"
    prof.extensions_file = ext if ext.exists() else None
    return prof


def _workflow_runs_on_push_or_pr(path: Path) -> bool:
    """True when the workflow's top-level ``on:`` trigger includes ``push``.

    The ship gate checks CI on the MERGE commit, and only push-triggered workflows run there;
    a pull_request-only workflow never reports on the merge commit, so it does not count.
    """
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return False
    for i, line in enumerate(lines):
        m = re.match(r"""^(?:on|"on"|'on'|true)\s*:\s*(.*)$""", line)
        if not m:
            continue
        inline = m.group(1).split("#", 1)[0].strip()
        if inline:
            return bool(re.search(r"(?<![\w-])push(?![\w-])", inline))
        for nxt in lines[i + 1:]:
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                continue
            if not nxt[0].isspace():
                break  # next top-level key: end of the on: block
            if re.match(r"^\s{1,4}(?:-\s*)?push\s*:?\s*(?:#.*)?$", nxt):
                return True
        return False
    return False


# --------------------------------------------------------------------------------------
# Spec Kit feature directory resolution
# --------------------------------------------------------------------------------------


def resolve_feature_dir(repo: Path, branch: str | None = None, script_type: str = "sh") -> Path | None:
    """Ask Spec Kit itself (check-prerequisites --json --paths-only), falling back to specs/<branch>."""
    script = repo / ".specify" / "scripts" / "bash" / "check-prerequisites.sh"
    if script_type == "ps":
        ps = repo / ".specify" / "scripts" / "powershell" / "check-prerequisites.ps1"
        if ps.exists():
            r = run_shell(f"pwsh -NoProfile -File {shlex.quote(str(ps))} -Json -PathsOnly", repo, 60)
            fd = _parse_paths_json(r.stdout)
            if fd:
                return fd
    if script.exists():
        r = run_shell(f"bash {shlex.quote(str(script))} --json --paths-only", repo, 60)
        fd = _parse_paths_json(r.stdout)
        if fd and fd.exists():
            return fd
    branch = branch or Git(repo).current_branch()
    specs = repo / "specs"
    if branch and specs.is_dir():
        direct = specs / branch
        if direct.is_dir():
            return direct
        m = re.match(r"^(\d{3,})-", branch.split("/")[-1])
        if m:
            matches = sorted(p for p in specs.iterdir() if p.is_dir() and p.name.startswith(m.group(1) + "-"))
            if len(matches) == 1:
                return matches[0]
    return None


def _parse_paths_json(stdout: str) -> Path | None:
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            fd = data.get("FEATURE_DIR")
            if fd:
                return Path(fd)
    return None
