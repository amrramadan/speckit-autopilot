"""Worker sessions.

``WorkerSession`` is the only interface the runner uses. ``SDKWorkerSession`` wraps
``claude_agent_sdk.ClaudeSDKClient``; ``FakeSession`` replays scripted turns for tests.

Closing a session and opening a new one is the programmatic ``/clear``: the new
client starts with an empty context and only files on disk carry state across.
"""

from __future__ import annotations

import abc
import asyncio
import inspect
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .policy import UNSANDBOXED_PHASES, PolicyGate
from .state import EventLog, TranscriptWriter


# --------------------------------------------------------------------------------------
# Turn results
# --------------------------------------------------------------------------------------


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    tool_use_id: str
    content: Any
    is_error: bool = False

    def text(self) -> str:
        c = self.content
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for b in c:
                if isinstance(b, dict):
                    parts.append(str(b.get("text", "")))
                else:
                    parts.append(str(getattr(b, "text", b)))
            return "\n".join(parts)
        return "" if c is None else str(c)


@dataclass
class TurnResult:
    text: str = ""  # all assistant text of the turn, in order
    final_text: str = ""  # the ResultMessage.result (last assistant message text)
    is_error: bool = False
    subtype: str = "success"
    errors: list[str] = field(default_factory=list)
    cost_delta_usd: float = 0.0
    session_cost_usd: float = 0.0
    duration_s: float = 0.0
    timed_out: bool = False
    num_turns: int = 0
    session_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: dict[str, ToolResult] = field(default_factory=dict)
    compacted: bool = False
    budget_exceeded: bool = False
    interrupted: bool = False

    @property
    def reply_text(self) -> str:
        """What the worker 'said' at the end of the turn."""
        return self.final_text or self.text

    def summary(self, n: int = 300) -> str:
        t = " ".join((self.reply_text or "").split())
        return t[:n] + ("…" if len(t) > n else "")


class SessionError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------------------


class WorkerSession(abc.ABC):
    label: str
    turns: int
    total_cost_usd: float
    compaction_pending: bool

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def send(self, text: str, timeout_s: float) -> TurnResult: ...

    @abc.abstractmethod
    async def context_pct(self) -> float | None: ...

    @abc.abstractmethod
    async def interrupt(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    async def server_info(self) -> dict[str, Any] | None:
        return None

    async def mcp_status(self) -> dict[str, Any] | None:
        return None

    async def __aenter__(self) -> "WorkerSession":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


@dataclass
class SessionSpec:
    """Everything needed to open one worker session."""

    label: str
    phase: str
    cwd: Path
    gate: PolicyGate
    system_append: str
    budget_usd: float | None  # None: no budget cap (the default: runs use the Claude subscription)
    model: str | None = None
    effort: str | None = None
    allowed_tools: tuple[str, ...] = ("Read", "Glob", "Grep", "Edit", "Write", "TodoWrite", "Skill")
    setting_sources: tuple[str, ...] = ("user", "project", "local")
    permission_mode: str = "acceptEdits"
    read_only: bool = False  # reader sessions (Linear read-back): no edits at all
    env: dict[str, str] = field(default_factory=dict)  # e.g. GIT_BRANCH_NAME / SPECIFY_FEATURE_DIRECTORY
    plugin_dirs: tuple[str, ...] = ()  # extra local plugins (--plugin-dir), e.g. the claude.ai-synced "design" plugin with Linear
    wait_mcp: tuple[str, ...] = ()  # MCP servers to wait for (connected) right after connect
    sandbox: dict[str, Any] | None = None  # ClaudeAgentOptions.sandbox (OS-level Bash sandbox, see sandbox_settings); None = off


SessionFactory = Callable[[SessionSpec], WorkerSession]


# --------------------------------------------------------------------------------------
# Fake session (tests)
# --------------------------------------------------------------------------------------


@dataclass
class FakeTurn:
    text: str = "OK"
    cost_usd: float = 0.01  # incremental cost of this turn
    context_pct: float = 10.0
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    tool_results: dict[str, Any] = field(default_factory=dict)  # tool name -> raw result payload
    ask: list[dict[str, Any]] | None = None  # AskUserQuestion "questions" payload
    compaction: bool = False
    is_error: bool = False
    subtype: str = "success"
    hang: bool = False  # simulate a turn that never finishes (timeout path)
    action: Callable[["FakeSession"], Any] | None = None  # side effect (mutate repo) when tool calls are allowed


FakeScript = Callable[["FakeSession", str], "FakeTurn | Awaitable[FakeTurn]"]


class FakeSession(WorkerSession):
    """Scripted worker. ``script(session, message)`` returns a FakeTurn.

    Tool calls listed in the FakeTurn are pushed through the real PolicyGate (hook +
    can_use_tool) so policy behaviour is exercised end-to-end; the ``action`` runs only
    if every tool call was allowed. AskUserQuestion payloads are routed through
    ``gate.can_use_tool`` exactly like the SDK would, and the answers are recorded in
    ``self.answers``.
    """

    def __init__(self, spec: SessionSpec, script: FakeScript, registry: list["FakeSession"] | None = None):
        self.spec = spec
        self.label = spec.label
        self.script = script
        self.turns = 0
        self.total_cost_usd = 0.0
        self.compaction_pending = False
        self.inbox: list[str] = []
        self.answers: list[dict[str, Any]] = []
        self.denied: list[tuple[str, dict[str, Any], str]] = []
        self.allowed: list[tuple[str, dict[str, Any]]] = []
        self._ctx = 0.0
        self.started = False
        self.closed = False
        self.interrupted = False
        if registry is not None:
            registry.append(self)

    async def start(self) -> None:
        self.started = True

    async def send(self, text: str, timeout_s: float) -> TurnResult:
        if self.closed:
            raise SessionError("send on closed session")
        self.inbox.append(text)
        self.turns += 1
        start = time.monotonic()
        res = self.script(self, text)
        turn: FakeTurn = await res if inspect.isawaitable(res) else res  # type: ignore[assignment]
        if turn.hang:
            try:
                await asyncio.wait_for(asyncio.sleep(timeout_s + 5), timeout=timeout_s)
            except asyncio.TimeoutError:
                await self.interrupt()
                return TurnResult(text="", final_text="", is_error=True, subtype="timeout", timed_out=True, interrupted=True, duration_s=time.monotonic() - start)
        calls: list[ToolCall] = []
        results: dict[str, ToolResult] = {}
        all_allowed = True
        gate = self.spec.gate
        if turn.ask:
            from claude_agent_sdk import PermissionResultAllow

            r = await gate.can_use_tool("AskUserQuestion", {"questions": turn.ask}, None)
            if isinstance(r, PermissionResultAllow):
                self.answers.append((r.updated_input or {}).get("answers", {}))
        for i, (name, inp) in enumerate(turn.tool_calls):
            tid = f"fake-{self.turns}-{i}"
            calls.append(ToolCall(tid, name, inp))
            hook = await gate.pre_tool_use_hook({"tool_name": name, "tool_input": inp}, tid, None)
            denied_msg = None
            if hook.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
                denied_msg = hook["hookSpecificOutput"].get("permissionDecisionReason", "denied")
            elif name not in self.spec.allowed_tools:
                from claude_agent_sdk import PermissionResultDeny

                r = await gate.can_use_tool(name, inp, None)
                if isinstance(r, PermissionResultDeny):
                    denied_msg = r.message
            if denied_msg:
                all_allowed = False
                self.denied.append((name, inp, denied_msg))
                results[tid] = ToolResult(tid, denied_msg, is_error=True)
            else:
                self.allowed.append((name, inp))
                payload = turn.tool_results.get(name, "ok")
                results[tid] = ToolResult(tid, payload)
        if turn.action and all_allowed:
            out = turn.action(self)
            if inspect.isawaitable(out):
                await out
        self.total_cost_usd += turn.cost_usd
        self._ctx = turn.context_pct
        if turn.compaction:
            self.compaction_pending = True
        return TurnResult(
            text=turn.text,
            final_text=turn.text,
            is_error=turn.is_error,
            subtype=turn.subtype,
            cost_delta_usd=turn.cost_usd,
            session_cost_usd=self.total_cost_usd,
            duration_s=time.monotonic() - start,
            num_turns=1,
            session_id=f"fake-{self.label}",
            tool_calls=calls,
            tool_results=results,
            compacted=turn.compaction,
            budget_exceeded=turn.subtype == "error_max_budget_usd",
        )

    async def context_pct(self) -> float | None:
        return self._ctx

    async def interrupt(self) -> None:
        self.interrupted = True

    async def close(self) -> None:
        self.closed = True

    async def server_info(self) -> dict[str, Any] | None:
        return {"commands": [{"name": n.lstrip("/")} for n in getattr(self, "commands", [])]}


# --------------------------------------------------------------------------------------
# Real SDK session
# --------------------------------------------------------------------------------------

# Variables a parent Claude Code session exports that would couple the worker CLI to it
# (IDE auto-connect, peer messaging, child-session bookkeeping). See DESIGN.md §SDK.
_LEAKY_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
)


# --------------------------------------------------------------------------------------
# OS-level Bash sandbox (ClaudeAgentOptions.sandbox; macOS Seatbelt / Linux bubblewrap)
#
# Verified live with claude-agent-sdk 0.2.180 / bundled CLI 2.1.321 on macOS (DESIGN.md h.1): with the sandbox on,
# a Bash write outside the repo fails ("Operation not permitted"), repo writes and `git commit` work, writes to
# .git/hooks and .git/config are refused, $TMPDIR is rewritten to a writable /tmp/claude-<uid>, a host outside
# allowedDomains reaches can_use_tool as the pseudo-tool SandboxNetworkAccess (the policy denies it),
# dangerouslyDisableSandbox is ignored when allowUnsandboxedCommands is false, and the PreToolUse hook still sees
# every Bash call (with the shell's current cwd). pnpm cannot test-link its home store from inside the sandbox, so
# the store is named explicitly (sandbox_env) and made writable, else it puts a fresh store inside the repo.
# --------------------------------------------------------------------------------------

SANDBOX_MODES = ("on", "off", "auto")
# Phases that need the user's git/gh credentials or the network: remote fetch/push (Spec Kit's create-new-feature
# runs `git ls-remote`/`git fetch` to number the branch), gh, and the Linear close-out.
SANDBOX_EXEMPT_PHASES = UNSANDBOXED_PHASES  # the policy's stricter rules for these phases rely on the same set
# Phases that install dependencies and run tests/builds: package registries, package caches, local ports (test
# suites talk to a local Postgres/Redis and bind ephemeral ports).
SANDBOX_TOOLCHAIN_PHASES = frozenset({"implement", "review"})
# npm/pnpm/corepack, yarn, uv/pip, and next/font/google (fetched at `next build` time in four of the repos).
DEFAULT_SANDBOX_DOMAINS = ("registry.npmjs.org", "registry.yarnpkg.com", "pypi.org", "files.pythonhosted.org", "fonts.googleapis.com", "fonts.gstatic.com")
# Never readable from sandboxed Bash, whatever the command looks like (the policy's credential rules see only text).
# ~/.npmrc may hold registry tokens; npm and pnpm both carry on (pnpm warns) when it cannot be read.
SANDBOX_DENY_READ = (
    "~/.ssh", "~/.aws", "~/.config/gh", "~/.config/gcloud", "~/.kube", "~/.gnupg", "~/.docker/config.json",
    "~/.netrc", "~/.git-credentials", "~/.pypirc", "~/.npmrc", "~/.claude.json", "~/.claude/.credentials.json",
)
# A repo's local secret env files (Next.js / dotenv convention: .env.local and .env.*.local are git-ignored and hold the
# developer's keys; .env.production* the deploy's). .env, .env.test and .env.example stay readable: test suites load them.
_SECRET_ENV_FILE = re.compile(r"^\.env(?:\.[\w-]+)*\.local$|^\.env\.prod(?:uction)?(?:\.[\w-]+)*$")
_SKIP_DIRS = frozenset({"node_modules", ".git", ".venv", "venv", "vendor", "dist", "build", ".next", ".turbo", ".autopilot"})


def local_secret_env_files(repo: Path, depth: int = 3) -> list[str]:
    """Real paths of the repo's local secret env files, down to ``depth`` directories (apps/web/.env.local)."""
    out: list[str] = []
    root = Path(os.path.realpath(str(repo)))

    def walk(d: Path, level: int) -> None:
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            if e.is_file(follow_symlinks=False) and _SECRET_ENV_FILE.match(e.name):
                out.append(os.path.join(str(d), e.name))
            elif level < depth and e.is_dir(follow_symlinks=False) and e.name not in _SKIP_DIRS:
                walk(Path(e.path), level + 1)

    walk(root, 0)
    return sorted(out)


def protected_write_paths(repo: Path) -> list[str]:
    """Real paths of the policy's always-protected files and directories in ``repo`` (policy.ALWAYS_PROTECTED): git hooks
    and config (also where a worktree or core.hooksPath puts them), .husky / .githooks / lefthook, the Spec Kit
    scripts, templates and constitution, Claude settings, hooks, skills, commands and agents, .mcp.json and the
    autopilot's own state. A code-level write (python -c, node -e) never shows its target to the text policy; the
    sandbox stops it. The rest of .git stays writable: commits must work."""
    from .policy import ALWAYS_PROTECTED
    from .repo import git

    root = os.path.realpath(str(repo))
    rels: list[str] = []
    for pat in ALWAYS_PROTECTED:
        p = pat[:-3] if pat.endswith("/**") else pat
        if "*" in p:
            continue
        rels += [".git/hooks", ".git/config"] if p == ".git" else [p]
    out = [os.path.join(root, r) for r in rels]
    for what in ("hooks", "config"):
        res = git(Path(root), "rev-parse", "--git-path", what, timeout_s=10)
        if res.ok and res.stdout.strip():
            p = res.stdout.strip()
            out.append(os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p)))
    return list(dict.fromkeys(os.path.normpath(p) for p in out))


def secret_env_entries(extra: dict[str, str] | None = None) -> list[dict[str, str]]:
    """sandbox.credentials.envVars: every credential-like variable the worker CLI inherits is unset for its sandboxed
    commands (the CLI itself keeps its API credentials)."""
    from .repo import secret_env_names

    names = set(secret_env_names()) | set(secret_env_names(extra or {}))
    return [{"name": n, "mode": "deny"} for n in sorted(names) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n)]


def sandbox_available() -> tuple[bool, str]:
    """(usable, why) for the CLI's Bash sandbox on this machine."""
    import os
    import shutil
    import sys

    if sys.platform == "darwin":
        ok = os.path.exists("/usr/bin/sandbox-exec")
        return ok, "macOS Seatbelt (sandbox-exec)" if ok else "/usr/bin/sandbox-exec not found"
    if sys.platform.startswith("linux"):
        missing = [b for b in ("bwrap", "socat") if shutil.which(b) is None]
        return (not missing), ("bubblewrap + socat (not verified live)" if not missing else f"missing {', '.join(missing)}")
    return False, f"no Bash sandbox on {sys.platform}"


def pnpm_store_dir() -> str:
    """pnpm's global store base (without the vN suffix, so pnpm 9 and 10 each pick their own layout)."""
    import os
    import sys

    for var in ("npm_config_store_dir", "NPM_CONFIG_STORE_DIR"):
        if os.environ.get(var):
            return os.path.expanduser(os.environ[var])
    if os.environ.get("PNPM_HOME"):
        return os.path.join(os.environ["PNPM_HOME"], "store")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/pnpm/store")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "pnpm", "store")


def _package_cache_dirs() -> list[str]:
    import os
    import sys

    home = os.path.expanduser("~")
    mac = sys.platform == "darwin"
    caches = os.path.join(home, "Library", "Caches") if mac else (os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache"))
    return [
        pnpm_store_dir(),
        os.path.join(caches, "pnpm"),
        os.path.expanduser(os.environ.get("npm_config_cache") or "~/.npm"),
        os.path.expanduser(os.environ.get("UV_CACHE_DIR") or "~/.cache/uv"),
        os.path.join(caches, "pip"),
        os.path.join(caches, "Yarn" if mac else "yarn"),
    ]


def _temp_dirs() -> list[str]:
    """/tmp itself is not writable in the sandbox by default (only the rewritten $TMPDIR); the policy allows temp
    writes, and some test suites hard-code /tmp paths."""
    import os
    import tempfile

    out = ["/tmp", "/private/tmp"]
    real = os.path.realpath(tempfile.gettempdir())
    if real.startswith(("/private/var/folders/", "/var/folders/")):
        out.append(real)
    return out


def sandbox_settings(phase: str, mode: str, network_domains: list[str] | None = None, *, repo: Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """ClaudeAgentOptions.sandbox for a worker session of ``phase``, or None (no sandbox).

    ``mode``: "off" -> None; "on" -> always sandboxed (outside the exempt phases); "auto" -> sandboxed when
    ``sandbox_available()`` (macOS with sandbox-exec, Linux with bubblewrap + socat). Exempt phases (feature_start,
    ship, linear, halt_notice) always get None. ``network_domains`` are added to every sandboxed phase's allowed
    hosts; implement/review also get DEFAULT_SANDBOX_DOMAINS, local ports and the package caches. With ``repo``, the
    policy's always-protected paths are write-denied (filesystem.denyWrite) and the repo's local secret env files
    read-denied; credential-like variables of the CLI's environment (and ``env``) are unset for sandboxed commands
    (credentials.envVars). Those keys follow the bundled CLI 2.1.321 settings schema.
    """
    m = (mode or "off").strip().lower()
    if m not in SANDBOX_MODES:
        raise ValueError(f"sandbox mode must be one of {', '.join(SANDBOX_MODES)} (got {mode!r})")
    base = (phase or "").split(":")[0]
    if m == "off" or base in SANDBOX_EXEMPT_PHASES:
        return None
    if m == "auto" and not sandbox_available()[0]:
        return None
    import os

    toolchain = base in SANDBOX_TOOLCHAIN_PHASES
    domains = list(DEFAULT_SANDBOX_DOMAINS) if toolchain else []
    domains += [d.strip() for d in (network_domains or []) if d and d.strip()]
    writable = _temp_dirs() + (_package_cache_dirs() if toolchain else [])
    deny_read = [os.path.expanduser(p) for p in SANDBOX_DENY_READ] + (local_secret_env_files(repo) if repo is not None else [])
    fs: dict[str, Any] = {
        "allowWrite": list(dict.fromkeys(os.path.normpath(p) for p in writable)),
        "denyRead": list(dict.fromkeys(deny_read)),
    }
    if repo is not None:
        fs["denyWrite"] = protected_write_paths(repo)
    out: dict[str, Any] = {
        "enabled": True,
        "autoAllowBashIfSandboxed": False,  # every Bash call still goes through can_use_tool (and always the hook)
        "allowUnsandboxedCommands": False,  # dangerouslyDisableSandbox is ignored (the policy also denies it)
        "network": {"allowedDomains": list(dict.fromkeys(domains)), "allowLocalBinding": toolchain},
        "filesystem": fs,
    }
    env_entries = secret_env_entries(env)
    if env_entries:
        out["credentials"] = {"envVars": env_entries}
    return out


# --------------------------------------------------------------------------------------
# The supervisor's own tooling (test / lint / typecheck / build) under the OS sandbox
#
# The gates run code the worker wrote (a test file, a package.json script, a vitest / eslint / next config) with the
# user's rights, outside any Claude session. Where the OS sandbox is on, those commands run under a Seatbelt profile
# with the worker sandbox's limits: writes only in the repo (never its protected paths), temp dirs and the package
# caches; no reads of credential files; outbound network only to localhost (test suites talk to a local Postgres /
# Redis), except for build commands, which keep the network (next/font fetches Google Fonts at build time in four of
# the repos; a domain allowlist needs the CLI's proxy, which plain sandbox-exec has not). Verified on macOS with
# sandbox-exec: later deny rules override earlier allows; localhost connects and DNS work; other hosts get EPERM.
# --------------------------------------------------------------------------------------

TOOLING_NETWORK_KINDS = frozenset({"build"})
# Docker-style sockets would hand the sandboxed command the host (docker run -v ~/.ssh:/x).
_CONTAINER_SOCKETS = (r"docker\.sock$", r"/\.orbstack/", r"/\.colima/", r"podman[^/]*\.sock$", r"containerd\.sock$")


def tooling_sandbox_available() -> tuple[bool, str]:
    """(usable, why) for running the supervisor's tooling commands under sandbox-exec (macOS only)."""
    import sys

    if sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"):
        return True, "macOS Seatbelt (sandbox-exec)"
    return False, f"the tooling sandbox needs macOS sandbox-exec (platform {sys.platform})"


def _sbpl(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def tooling_sandbox_profile(
    repo: Path,
    kind: str,
    *,
    temp_dirs: list[str] | None = None,
    cache_dirs: list[str] | None = None,
    deny_read: list[str] | None = None,
    open_network: bool | None = None,
) -> str:
    """The Seatbelt profile one tooling command of ``kind`` ("test", "lint", "typecheck", "build") runs under."""

    def real(p: str) -> str:  # Seatbelt matches canonical paths (/tmp is /private/tmp)
        return os.path.realpath(os.path.expanduser(p))

    writable = [real(str(repo))] + [real(p) for p in (temp_dirs if temp_dirs is not None else _temp_dirs())]
    writable += [real(p) for p in (cache_dirs if cache_dirs is not None else _package_cache_dirs())]
    reads = [real(p) for p in (deny_read if deny_read is not None else SANDBOX_DENY_READ)]
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write* " + " ".join(f"(subpath {_sbpl(p)})" for p in dict.fromkeys(writable)) + ' (literal "/dev/null") (literal "/dev/zero")'
        ' (literal "/dev/dtracehelper") (literal "/dev/stdout") (literal "/dev/stderr") (regex #"^/dev/(tty|fd/|ttys|ptmx)"))',
        "(deny file-write* " + " ".join(f"(subpath {_sbpl(p)})" for p in protected_write_paths(repo)) + ")",
        "(deny file-read* " + " ".join(f"(subpath {_sbpl(p)})" for p in dict.fromkeys(reads)) + ")",
    ]
    if not (kind in TOOLING_NETWORK_KINDS if open_network is None else open_network):
        lines += [
            "(deny network-outbound)",
            '(allow network-outbound (remote ip "localhost:*"))',
            "(allow network-outbound (remote unix-socket))",
        ]
    lines.append("(deny network-outbound " + " ".join(f'(remote unix-socket (regex #"{rx}"))' for rx in _CONTAINER_SOCKETS) + ")")
    return "\n".join(lines) + "\n"


def sandbox_env(sandbox: dict[str, Any] | None) -> dict[str, str]:
    """Environment a sandboxed worker needs (merged under SessionSpec.env by SDKWorkerSession)."""
    if not sandbox or not sandbox.get("enabled"):
        return {}
    from .repo import with_shims_on_path

    # PATH: sandboxed Bash cannot run setuid programs such as /bin/ps; the shim copies come first (repo.SETUID_SHIMS).
    return with_shims_on_path({"npm_config_store_dir": pnpm_store_dir()})


def scrub_parent_session_env() -> list[str]:
    """Remove parent-session variables from os.environ (options.env cannot delete them)."""
    import os

    removed = [k for k in _LEAKY_ENV if k in os.environ]
    for k in removed:
        os.environ.pop(k, None)
    return removed


class SDKWorkerSession(WorkerSession):
    """``ClaudeSDKClient`` wrapper: one instance == one context window."""

    def __init__(self, spec: SessionSpec, log: EventLog, transcripts_dir: Path, cli_path: str | None = None):
        self.spec = spec
        self.label = spec.label
        self.log = log
        self.cli_path = cli_path
        self.turns = 0
        self.total_cost_usd = 0.0
        self.compaction_pending = False
        self.session_id: str | None = None
        self.api_key_source: str | None = None
        self._client: Any = None
        self._stderr_tail: list[str] = []
        self.tw = TranscriptWriter(transcripts_dir, f"{time.strftime('%Y%m%d-%H%M%S')}-{spec.label}")
        self.broken = False
        # Background tasks (Task/Agent tools, run_in_background Bash): cc-review spawns reviewer agents and
        # its report arrives in a LATER, CLI-injected turn. Track them so a turn is only "done" when they are.
        self.active_tasks: set[str] = set()
        self.post_task_grace_s = 180.0
        # CLI session state (session_state_changed, opted in via CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS): the turn is
        # complete once our ResultMessage arrived AND the CLI reports "idle" (no follow-up turn owed, SDK #1190).
        self.cli_state: str | None = None
        self.seen_state = False
        self._abandon_tasks = False

    # -- lifecycle ----------------------------------------------------------------------
    def _options(self) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

        spec = self.spec
        hooks = {
            # on_failure="block": a hook error must deny, never allow (the hook is the only check for auto-approved tools)
            "PreToolUse": [HookMatcher(matcher=None, hooks=[spec.gate.pre_tool_use_hook], timeout=60, on_failure="block")],
            "PreCompact": [HookMatcher(matcher=None, hooks=[self._on_precompact], timeout=30)],
        }
        kwargs: dict[str, Any] = dict(
            cwd=str(spec.cwd),
            system_prompt={"type": "preset", "preset": "claude_code", "append": spec.system_append},
            setting_sources=list(spec.setting_sources),
            permission_mode=spec.permission_mode,
            allowed_tools=list(spec.allowed_tools),
            can_use_tool=spec.gate.can_use_tool,
            hooks=hooks,
            stderr=self._on_stderr,
            max_buffer_size=64 * 1024 * 1024,
        )
        if spec.budget_usd is not None:
            kwargs["max_budget_usd"] = max(0.05, float(spec.budget_usd))
        kwargs["env"] = {"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1", **sandbox_env(spec.sandbox), **dict(spec.env or {})}
        if spec.plugin_dirs:
            kwargs["plugins"] = [{"type": "local", "path": p} for p in spec.plugin_dirs]
        from .policy import WORKER_DISALLOWED_TOOLS

        disallowed = list(WORKER_DISALLOWED_TOOLS)
        if spec.read_only:
            disallowed += ["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"]
        kwargs["disallowed_tools"] = disallowed
        if spec.model:
            kwargs["model"] = spec.model
        if spec.effort:
            kwargs["effort"] = spec.effort
        if self.cli_path:
            kwargs["cli_path"] = self.cli_path
        if spec.sandbox is not None:
            kwargs["sandbox"] = spec.sandbox
        return ClaudeAgentOptions(**kwargs)

    async def start(self) -> None:
        import warnings

        from claude_agent_sdk import ClaudeSDKClient

        with warnings.catch_warnings():
            # Expected: allowed_tools auto-approve Read/Edit/...; every call is still gated by the PreToolUse hook.
            warnings.filterwarnings("ignore", message=".*can_use_tool will not be invoked.*")
            opts = self._options()
        self._client = ClaudeSDKClient(opts)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*can_use_tool will not be invoked.*")
            await self._client.connect()
        self.log.emit("session_open", label=self.label, phase=self.spec.phase, budget_usd=self.spec.budget_usd, transcript=str(self.tw.path))
        if self.spec.wait_mcp:
            st = await self.wait_for_mcp(list(self.spec.wait_mcp))
            self.log.emit("mcp_ready", label=self.label, servers=st)

    async def close(self) -> None:
        if self._client is None:
            return
        client, self._client = self._client, None
        try:
            await asyncio.wait_for(client.disconnect(), timeout=30)
        except Exception as exc:  # pragma: no cover - best effort
            self.log.emit("session_close_error", label=self.label, error=repr(exc))
        self.log.emit("session_close", label=self.label, turns=self.turns, cost_usd=round(self.total_cost_usd, 4))

    # -- callbacks ----------------------------------------------------------------------
    def _on_stderr(self, line: str) -> None:
        self._stderr_tail.append(line)
        del self._stderr_tail[:-200]
        self.tw.write("stderr", line)

    async def _on_precompact(self, input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        self.compaction_pending = True
        self.log.emit("precompact", label=self.label, phase=self.spec.phase, trigger=input_data.get("trigger"))
        return {}

    # -- turns --------------------------------------------------------------------------
    async def send(self, text: str, timeout_s: float) -> TurnResult:
        if self._client is None:
            raise SessionError("session not started")
        self.turns += 1
        self.tw.write("user", text)
        start = time.monotonic()
        acc = _TurnAccumulator()
        self._abandon_tasks = False
        try:
            await self._client.query(text)
            async with asyncio.timeout(timeout_s):
                await self._drain(acc)
        except TimeoutError:
            self.log.emit("turn_timeout", label=self.label, phase=self.spec.phase, timeout_s=timeout_s)
            await self._interrupt_and_drain(acc)
            res = acc.to_result(self, time.monotonic() - start)
            res.timed_out = True
            res.is_error = True
            res.interrupted = True
            res.subtype = "timeout"
            return res
        except Exception as exc:  # CLIConnectionError, ProcessError, ResultError, CLIJSONDecodeError, MessageParseError ...
            self.broken = True
            self.log.emit("session_error", label=self.label, phase=self.spec.phase, error=f"{type(exc).__name__}: {exc}", stderr_tail=self._stderr_tail[-20:])
            res = acc.to_result(self, time.monotonic() - start)
            res.is_error = True
            res.subtype = "sdk_error"
            res.errors = [f"{type(exc).__name__}: {exc}"]
            return res
        return acc.to_result(self, time.monotonic() - start)

    async def _drain(self, acc: "_TurnAccumulator") -> None:
        it = self._client.receive_messages().__aiter__()
        while True:
            try:
                if acc.result_msg is not None and not self.active_tasks and not self.seen_state:
                    # our turn ended while background work ran; that work is now finished — give the CLI a
                    # grace window to inject the follow-up turn that carries the results (e.g. the review report)
                    msg = await asyncio.wait_for(it.__anext__(), timeout=self.post_task_grace_s)
                else:
                    msg = await it.__anext__()
            except (StopAsyncIteration, asyncio.TimeoutError):
                return
            self._record(msg)
            if acc.add(msg, self):
                return

    async def _stop_background(self) -> None:
        self._abandon_tasks = True
        for tid in list(self.active_tasks):
            try:
                await asyncio.wait_for(self._client.stop_task(tid), timeout=30)
            except Exception as exc:  # pragma: no cover - best effort
                self.log.emit("stop_task_failed", label=self.label, task=tid, error=repr(exc))
        self.active_tasks.clear()

    async def _interrupt_and_drain(self, acc: "_TurnAccumulator") -> None:
        try:
            await self._stop_background()
            await asyncio.wait_for(self._client.interrupt(), timeout=70)
            async with asyncio.timeout(120):
                await self._drain(acc)
        except Exception as exc:
            self.broken = True
            self.log.emit("interrupt_failed", label=self.label, error=repr(exc))

    def _record(self, msg: Any) -> None:
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    self.tw.write("assistant_text", {"text": b.text, "sub": msg.parent_tool_use_id})
                elif isinstance(b, ToolUseBlock):
                    self.tw.write("tool_use", {"id": b.id, "name": b.name, "input": b.input, "sub": msg.parent_tool_use_id})
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    content = b.content if isinstance(b.content, str) else str(b.content)
                    self.tw.write("tool_result", {"id": b.tool_use_id, "is_error": b.is_error, "content": content[:20000]})
        elif isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                self.session_id = msg.data.get("session_id", self.session_id)
                if self.api_key_source is None:
                    self.api_key_source = msg.data.get("apiKeySource")
                    self.log.emit("session_init", label=self.label, session_id=self.session_id, api_key_source=self.api_key_source, model=msg.data.get("model"))
            elif msg.subtype == "compact_boundary":
                self.compaction_pending = True
                self.log.emit("compact_boundary", label=self.label, data=msg.data.get("compact_metadata"))
            self.tw.write("system", {"subtype": msg.subtype})
        elif isinstance(msg, ResultMessage):
            self.tw.write("result", {"subtype": msg.subtype, "is_error": msg.is_error, "cost": msg.total_cost_usd, "num_turns": msg.num_turns, "result": (msg.result or "")[:20000], "errors": msg.errors, "terminal_reason": getattr(msg, "terminal_reason", None)})

    def _turn_done(self) -> bool:
        if self._abandon_tasks:
            return True
        if self.seen_state:
            return self.cli_state == "idle"
        return not self.active_tasks

    # -- introspection ------------------------------------------------------------------
    async def context_pct(self) -> float | None:
        if self._client is None:
            return None
        try:
            usage = await asyncio.wait_for(self._client.get_context_usage(), timeout=70)
            return float(usage.get("percentage")) if usage and usage.get("percentage") is not None else None
        except Exception as exc:
            self.log.emit("context_usage_error", label=self.label, error=repr(exc))
            return None

    async def interrupt(self) -> None:
        if self._client is not None:
            try:
                await self._stop_background()
                await asyncio.wait_for(self._client.interrupt(), timeout=70)
            except Exception as exc:  # pragma: no cover
                self.log.emit("interrupt_failed", label=self.label, error=repr(exc))

    async def server_info(self) -> dict[str, Any] | None:
        # NOTE: get_server_info is `async def` in 0.2.180 (verified live; older docs list it as sync)
        if self._client is None:
            return None
        res = self._client.get_server_info()
        return await res if asyncio.iscoroutine(res) else res

    async def wait_for_mcp(self, names: list[str], timeout_s: float = 45.0) -> dict[str, str]:
        """Poll get_mcp_status until the named servers leave 'pending' (HTTP MCP servers connect asynchronously)."""
        deadline = time.monotonic() + timeout_s
        statuses: dict[str, str] = {}
        while True:
            st = await self.mcp_status() or {}
            statuses = {s.get("name"): s.get("status") for s in st.get("mcpServers", []) if s.get("name") in names}
            if all(statuses.get(n) not in (None, "pending") for n in names) or time.monotonic() > deadline:
                return statuses
            await asyncio.sleep(1.5)

    async def mcp_status(self) -> dict[str, Any] | None:
        if self._client is None:
            return None
        try:
            return await asyncio.wait_for(self._client.get_mcp_status(), timeout=70)
        except Exception as exc:
            self.log.emit("mcp_status_error", label=self.label, error=repr(exc))
            return None


try:
    from claude_agent_sdk._internal.query import DEFERRING_TASK_TYPES as _WAIT_TASK_TYPES
except ImportError:  # pragma: no cover
    _WAIT_TASK_TYPES = frozenset({"local_agent", "local_workflow"})


class _TurnAccumulator:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.calls: list[ToolCall] = []
        self.results: dict[str, ToolResult] = {}
        self.result_msg: Any = None  # our own turn's ResultMessage
        self.last_result: Any = None  # latest ResultMessage (own or injected follow-up); carries the running cost total
        self.compacted = False
        self.injected_turns = 0

    def add(self, msg: Any, sess: "SDKWorkerSession") -> bool:
        """Return True when the turn is complete: our ResultMessage arrived and no background task is pending."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

        try:
            from claude_agent_sdk.types import TERMINAL_TASK_STATUSES, TaskNotificationMessage, TaskStartedMessage, TaskUpdatedMessage
        except ImportError:  # pragma: no cover - older SDKs
            TERMINAL_TASK_STATUSES, TaskNotificationMessage, TaskStartedMessage, TaskUpdatedMessage = frozenset(), (), (), ()

        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock) and msg.parent_tool_use_id is None:
                    self.texts.append(b.text)
                elif isinstance(b, ToolUseBlock):
                    self.calls.append(ToolCall(b.id, b.name, dict(b.input or {})))
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    self.results[b.tool_use_id] = ToolResult(b.tool_use_id, b.content, bool(b.is_error))
        elif TaskStartedMessage and isinstance(msg, TaskStartedMessage):
            # only delegated agent work owes a follow-up turn (background shells/monitors may never finish)
            ttype = msg.task_type or (msg.data or {}).get("task_type")
            if ttype in _WAIT_TASK_TYPES:
                sess.active_tasks.add(msg.task_id)
        elif TaskNotificationMessage and isinstance(msg, TaskNotificationMessage):
            if str(msg.status) in TERMINAL_TASK_STATUSES:
                sess.active_tasks.discard(msg.task_id)
        elif TaskUpdatedMessage and isinstance(msg, TaskUpdatedMessage):
            status = msg.status or (msg.patch or {}).get("status")
            if status in TERMINAL_TASK_STATUSES:
                sess.active_tasks.discard(msg.task_id)
        elif isinstance(msg, SystemMessage) and msg.subtype == "session_state_changed":
            sess.cli_state = (msg.data or {}).get("state")
            sess.seen_state = True
            return self.result_msg is not None and sess._turn_done()
        elif isinstance(msg, SystemMessage) and msg.subtype == "compact_boundary":
            self.compacted = True
        elif isinstance(msg, ResultMessage):
            origin = getattr(msg, "origin", None)
            kind = origin.get("kind") if isinstance(origin, dict) else getattr(origin, "kind", None)
            own = origin is None or kind in (None, "human")
            if own:
                self.result_msg = msg
                self.last_result = msg
            elif self.result_msg is not None:
                self.last_result = msg  # follow-up turn injected by background work we are waiting for
                self.injected_turns += 1
            else:
                return False  # leftover injected turn from earlier background work; keep waiting for ours
            return self.result_msg is not None and sess._turn_done()
        return False

    def to_result(self, sess: SDKWorkerSession, duration: float) -> TurnResult:
        r = self.last_result if self.last_result is not None else self.result_msg
        last = getattr(sess, "_last_total", sess.total_cost_usd)
        total = float(getattr(r, "total_cost_usd", None) or last) if r is not None else last
        # total_cost_usd is cumulative per session (verified live): count only the increase. A drop means the
        # SDK's running total was reset (/clear, ConversationResetMessage): then the new total is all new spend.
        delta = total - last if total >= last else total
        sess._last_total = total
        sess.total_cost_usd += max(0.0, delta)
        text = "\n\n".join(t for t in self.texts if t.strip())
        final = (getattr(r, "result", None) or "") if r is not None else ""
        subtype = getattr(r, "subtype", "incomplete") if r is not None else "incomplete"
        is_error = bool(getattr(r, "is_error", True)) if r is not None else True
        return TurnResult(
            text=text,
            final_text=final or (self.texts[-1] if self.texts else ""),
            is_error=is_error,
            subtype=subtype,
            errors=list(getattr(r, "errors", None) or []) if r is not None else ["no result message"],
            cost_delta_usd=max(0.0, delta),
            session_cost_usd=sess.total_cost_usd,
            duration_s=duration,
            num_turns=int(getattr(r, "num_turns", 0) or 0) if r is not None else 0,
            session_id=getattr(r, "session_id", None) or sess.session_id,
            tool_calls=self.calls,
            tool_results=self.results,
            compacted=self.compacted or sess.compaction_pending,
            budget_exceeded=subtype == "error_max_budget_usd",
        )


def sdk_session_factory(log: EventLog, transcripts_dir: Path, cli_path: str | None = None) -> SessionFactory:
    def make(spec: SessionSpec) -> WorkerSession:
        return SDKWorkerSession(spec, log, transcripts_dir, cli_path=cli_path)

    return make
