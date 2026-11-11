"""Permission gate (docs/DESIGN.md §7).

``evaluate()`` is a pure function: (tool name, tool input, phase context) -> Decision.
``PolicyGate`` adapts it to the two SDK enforcement points:

* a ``PreToolUse`` hook, which fires for EVERY tool call, including tools that are
  auto-approved (``allowed_tools``, edits under ``acceptEdits``, and anything
  allow-listed by the repo's own ``.claude/settings*.json``). Deny rules are enforced
  here so nothing slips past through auto-approval.
* ``can_use_tool``, which only fires for tools that are not auto-approved
  (Bash, MCP tools, AskUserQuestion). It re-checks the deny rules and routes
  AskUserQuestion to the judge.
"""

from __future__ import annotations

import fnmatch
import glob as globmod
import json
import os
import re
import shlex
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

# --------------------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str = ""
    rule: str = ""
    log: bool = False  # log even when allowed (dependency installs, merges, pushes)

    @staticmethod
    def ok(rule: str = "default", reason: str = "", log: bool = False) -> "Decision":
        return Decision(True, reason, rule, log)

    @staticmethod
    def deny(rule: str, reason: str) -> "Decision":
        return Decision(False, reason, rule, True)


@dataclass
class PolicyContext:
    phase: str
    repo: Path
    base_branch: str = "main"
    push_phases: tuple[str, ...] = ("ship",)
    linear_write_phases: tuple[str, ...] = ("linear",)
    install_phases: tuple[str, ...] = ("implement",)
    protected_paths: tuple[str, ...] = ()
    linear_tool_prefix: str = "mcp__linear__"
    linear_enabled: bool = True  # False (linear = off): every Linear tool is denied, reads included
    tasks_text: str = ""  # tasks.md content, used for "unless a task explicitly requires it"
    extra_ship_phases: tuple[str, ...] = ()  # phases (besides push_phases) allowed to merge/checkout base
    deny_bulk_add: bool = True  # `git add -A/.`, `git commit -a` would sweep unrelated untracked files
    # real paths of the scripts that ran in this phase (kept in UNSANDBOXED_PHASES only; PolicyGate.set_phase clears it)
    ran_scripts: set[str] = field(default_factory=set)
    # the commit the run started from (state.flags["start_sha"], set by the runner): a repo script byte-identical to its
    # copy there was reviewed and merged by a person, so the inline code in it is read like a code file (see _base_identical)
    base_sha: str = ""

    @property
    def ship_like(self) -> bool:
        """Phases allowed to push/merge/switch to base. feature_start may only publish its own branch."""
        return (self.phase in self.push_phases and self.phase != "feature_start") or self.phase in self.extra_ship_phases


# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------

_HOME = os.path.expanduser("~")

# credential stores: never read, print or copy
_CREDENTIAL_PATTERNS = [
    r"(^|[\s'\"=:/])~/\.ssh(/|\b)",
    r"\$\{?HOME\}?/\.ssh(/|\b)",
    r"/Users/[^/\s]+/\.ssh(/|\b)",
    r"/home/[^/\s]+/\.ssh(/|\b)",
    r"(^|[\s'\"=:/])~/\.aws(/|\b)",
    r"\$\{?HOME\}?/\.aws(/|\b)",
    r"/Users/[^/\s]+/\.aws(/|\b)",
    r"(^|[\s'\"=:/])~/\.config/gh(/|\b)",
    r"\.config/gh/hosts\.yml",
    r"(^|[\s'\"=/])\.netrc\b",
    r"\.git-credentials\b",
    r"(^|[\s'\"=/])~/\.npmrc\b",
    r"\$\{?HOME\}?/\.npmrc\b",
    r"\.pypirc\b",
    r"\.docker/config\.json",
    r"\.kube/config\b",
    r"\.config/gcloud(/|\b)",
    r"\.claude/\.credentials\.json",
    r"\.claude\.json\b",
    r"\bid_(rsa|ed25519|ecdsa|dsa)\b",
]
_CREDENTIAL_RE = re.compile("|".join(_CREDENTIAL_PATTERNS))
# .env files (but not .env.example / .env.sample / .env.template / .env.test.example)
_DOTENV_RE = re.compile(r"(^|[\s'\"=:/<])\.env(\.[A-Za-z0-9_-]+)*(?=$|[\s'\";|&>)*?\[])")
_DOTENV_SAFE_SUFFIX = re.compile(r"\.env(\.[A-Za-z0-9_-]+)*\.(example|sample|template|dist|defaults)\b|\.env\.(example|sample|template|dist|defaults)\b")


def _mentions_dotenv(text: str) -> bool:
    for m in _DOTENV_RE.finditer(text):
        token = text[m.start():m.end() + 40]
        if _DOTENV_SAFE_SUFFIX.search(token.split()[0] if token.split() else token):
            continue
        return True
    return False


def is_credential_path(path: str) -> bool:
    p = path.strip()
    if not p:
        return False
    expanded = os.path.expanduser(p)
    norm = expanded.replace("\\", "/")
    for candidate in (p, norm):
        if _CREDENTIAL_RE.search(" " + candidate):
            return True
    base = os.path.basename(norm)
    if base == ".env" or (base.startswith(".env.") and not re.search(r"\.(example|sample|template|dist|defaults)(\.|$)", base)):
        return True
    if norm.startswith(os.path.join(_HOME, ".ssh")) or norm.startswith(os.path.join(_HOME, ".aws")):
        return True
    return False


def _glob_match(rel: str, pattern: str) -> bool:
    rel = rel.lstrip("./") if rel.startswith("./") else rel
    if fnmatch.fnmatch(rel, pattern):
        return True
    if "/" not in pattern and fnmatch.fnmatch(os.path.basename(rel), pattern):
        return True  # lockfile names match at any depth
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return rel == prefix or rel.startswith(prefix + "/")
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(rel, pattern[3:]) or fnmatch.fnmatch(os.path.basename(rel), pattern[3:])
    return False


def rel_to_repo(path: str, repo: Path) -> str | None:
    """Return ``path`` relative to ``repo`` (posix), or None if it is outside the repo."""
    if not path:
        return None
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = repo / p
    try:
        rp = Path(os.path.normpath(str(p)))
        rr = Path(os.path.normpath(str(repo.resolve() if repo.exists() else repo)))
        rp_res = Path(os.path.normpath(str(rp.resolve()))) if rp.exists() or rp.parent.exists() else rp
        for candidate in (rp_res, rp):
            try:
                return candidate.relative_to(rr).as_posix()
            except ValueError:
                continue
        # repo itself may be referenced through a symlink
        try:
            return rp.relative_to(Path(os.path.normpath(str(repo)))).as_posix()
        except ValueError:
            return None
    except OSError:
        return None


_TMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")

CONSTITUTION = ".specify/memory/constitution.md"
# Never editable by a worker, whatever the phase or tasks.md says (docs/DESIGN.md §7 + "don't modify my cc-* commands or the
# Spec Kit files" + anything that could plant hooks/aliases that bypass this policy).
ALWAYS_PROTECTED = (
    CONSTITUTION,
    ".autopilot/**",
    ".autopilot.toml",
    ".git/**",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/hooks/**",
    ".claude/skills/**",
    ".claude/commands/**",
    ".claude/agents/**",
    ".mcp.json",
    ".husky/**",
    ".githooks/**",
    "lefthook.yml",
    ".lefthook.yml",
    ".specify/scripts/**",
    ".specify/templates/**",
    ".specify/extensions/**",
    ".specify/extensions.yml",
)


def _task_requires(path_rel: str, tasks_text: str) -> bool:
    if not tasks_text:
        return False
    base = os.path.basename(path_rel)
    for line in tasks_text.splitlines():
        if not re.match(r"^\s*-\s*\[[ xX]\]", line):
            continue
        if path_rel in line or re.search(r"(?<![\w.-])" + re.escape(base) + r"(?![\w.-])", line):
            return True
    return False


def _cf_match(rel: str, pattern: str) -> bool:
    """Case-insensitive glob match (APFS, the default macOS filesystem, is case-insensitive)."""
    return _glob_match(rel.casefold(), pattern.casefold())


def _is_ancestor_of(rel: str, pattern: str) -> bool:
    """True if deleting/moving ``rel`` would take the protected ``pattern`` with it (rm -rf .specify)."""
    head = pattern.split("*", 1)[0].rstrip("/")
    if not head:
        return False
    r = rel.rstrip("/").casefold()
    return r in ("", ".") or head.casefold() == r or head.casefold().startswith(r + "/")


def _protected_reason(rel: str, ctx: PolicyContext, *, deleting: bool = False) -> Decision | None:
    if re.search(r"(^|/)decisions\.md$", rel, re.I) and rel.casefold().startswith("specs/"):
        return Decision.deny("protected-file", f"{rel} is the autopilot audit trail; only the supervisor writes it.")
    for pat in ALWAYS_PROTECTED:
        if _cf_match(rel, pat) or (deleting and _is_ancestor_of(rel, pat)):
            what = "the constitution" if pat == CONSTITUTION else f"{pat} (hooks, settings, skills, Spec Kit files, git internals and autopilot state are off-limits)"
            return Decision.deny("protected-file", f"Changing {rel} is never allowed in an unattended run: it is {what}.")
    for pat in ctx.protected_paths:
        if _cf_match(rel, pat) or (deleting and _is_ancestor_of(rel, pat)):
            if ctx.phase == "implement" and not deleting and _task_requires(rel, ctx.tasks_text):
                return Decision.ok("protected-file-task", f"{rel} is protected but a task in tasks.md explicitly names it.", log=True)
            return Decision.deny(
                "protected-file",
                f"{rel} is protected (CI config / lockfile / constitution). Edit it only if a task in tasks.md explicitly requires it.",
            )
    return None


def check_edit_path(path: str, ctx: PolicyContext) -> Decision:
    if not path:
        return Decision.ok("edit")
    if is_credential_path(path):
        return Decision.deny("credential-file", f"Editing credential/secret files is not allowed ({path}).")
    rel = rel_to_repo(path, ctx.repo)
    if rel is None:
        if any(os.path.realpath(os.path.expanduser(path)).startswith(t) or os.path.expanduser(path).startswith(t) for t in _TMP_PREFIXES):
            return Decision.ok("edit-tmp")
        return Decision.deny("outside-repo", f"Edits outside the repository are not allowed ({path}).")
    d = _protected_reason(rel, ctx) or _installed_package_reason(rel) or _ran_script_reason(path, ctx)
    return d if d is not None else Decision.ok("edit")


def _installed_package_reason(rel: str) -> Decision | None:
    """Installed packages are the package manager's: a file planted there (node_modules/.bin/vitest) runs under a
    trusted name."""
    if set(Path(rel).parts) & {"node_modules", ".venv", "venv", "site-packages"}:
        return Decision.deny("installed-package", f"{rel} is inside installed packages (node_modules / a virtualenv); change dependencies through the package manager, never by writing there.")
    return None


def _ran_script_reason(path: str, ctx: PolicyContext) -> Decision | None:
    """Where nothing but this text policy checks a command, a script that already ran in the phase stays as it was
    checked: a deferred or parallel run of it would execute the new, unchecked content."""
    if ctx.phase not in UNSANDBOXED_PHASES or not ctx.ran_scripts:
        return None
    real = os.path.normpath(os.path.realpath(os.path.expanduser(path) if os.path.isabs(os.path.expanduser(path)) else os.path.join(str(ctx.repo), path)))
    if real in ctx.ran_scripts:
        return Decision.deny("ran-script", f"{path} already ran in {ctx.phase}; it cannot be rewritten in the same phase (a deferred or parallel run would execute unchecked content).")
    return None


# --------------------------------------------------------------------------------------
# Bash parsing
#
# A shell policy built on parsing is best-effort, so it is layered:
#   1. normalisation (line continuations, heredoc bodies, process/command substitution);
#   2. a structural pass per simple command, unwrapping shell keywords, wrappers (env, timeout,
#      xargs, find -exec ...) and nested interpreters (bash -c, eval, env -S), recursively;
#   3. a raw-text backstop over the de-quoted command for the always-deny rules, which also
#      catches commands embedded in python/node one-liners and other forms the parser misses.
# False denials are acceptable (the worker is told why and can rephrase); false allows are not.
# --------------------------------------------------------------------------------------

_CONT = re.compile(r"\\\r?\n")
_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||\n|(?<![<>&0-9])&(?![>&])")
_SUBST = re.compile(r"\$\(([^()]*(?:\([^()]*\)[^()]*)*)\)|`([^`]*)`|[<>]\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
_RESERVED = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "time", "coproc", "{", "}", "(", ")", "case", "esac", "in", "select", "function"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "mksh", "tcsh", "csh"}
_SCRIPTERS = {"python", "python3", "python2", "node", "nodejs", "perl", "ruby", "php", "deno", "bun", "osascript", "pwsh", "powershell"}

# wrapper -> (options that take a value, flags without value). Unknown dashed tokens are skipped as flags.
_WRAPPER_OPTS: dict[str, set[str]] = {
    "command": set(),
    "builtin": set(),
    "exec": {"-a"},
    "time": set(),
    "nice": {"-n", "--adjustment"},
    "nohup": set(),
    "noglob": set(),
    "caffeinate": {"-t", "-w"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "gtimeout": {"-s", "--signal", "-k", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "env": {"-u", "--unset", "-C", "--chdir", "-P"},
    "chronic": set(),
    "ionice": {"-c", "-n", "-p"},
    "unbuffer": set(),
    "doas": {"-u"},
    "watch": {"-n", "--interval", "-d"},
    # macOS: `arch -arm64 cmd` / `arch -arch x86_64 -e VAR=v cmd`, `xcrun [--sdk x] cmd`, `taskpolicy -c utility cmd`
    "arch": {"-arch", "-e", "-d"},
    "xcrun": {"--sdk", "--toolchain"},
    "taskpolicy": {"-c", "-d", "-g", "-p"},
    "setsid": set(),
    "flock": {"-w", "--wait", "--timeout", "-E", "--conflict-exit-code"},
    # npm-land env wrappers: `cross-env NODE_ENV=test cmd`, `dotenv -e .env -- cmd`, `env-cmd -f .env cmd`
    "cross-env": set(),
    "cross-env-shell": set(),
    "dotenv": {"-e", "-v", "-p"},
    "env-cmd": {"-f", "--file", "-e", "--environments", "-r", "--rc-file"},
}
_POSITIONAL_AFTER_OPTS = {"timeout": 1, "gtimeout": 1, "flock": 1}  # timeout DURATION cmd, flock LOCKFILE cmd
# wrappers whose command is everything after "--" when there is one (`dotenv -c development -- cmd`)
_DASHDASH_WRAPPERS = frozenset({"dotenv", "env-cmd"})


def normalize(command: str) -> str:
    return _CONT.sub(" ", command or "")


_HEREDOC_OP = re.compile(r"<<(-?)[ \t]*((?:\\.|'[^'\n]*'|\"[^\"\n]*\"|[^\s;&|<>()])+)")


def _lex_shell(text: str, *, drop_comments: bool = False, cut_heredocs: bool = True) -> tuple[str, list[tuple[str, str]]]:
    """One quote-aware pass over shell text: find heredocs and comments the way the shell does.

    A ``<<`` or ``#`` inside quotes or a comment is not an operator, so ``echo '<<EOF'`` cannot hide the lines
    after it as a "heredoc body". Bodies of real heredocs are cut out (``cut_heredocs``) and returned as
    ``(operator line, body)``; the operator itself becomes ``<<HEREDOC``. Comments are dropped on request (script
    files are full of them). The delimiter match is looser than bash's (surrounding blanks allowed), which only
    ever ends a body early, i.e. checks more text as commands.
    """
    out: list[str] = []
    bodies: list[tuple[str, str]] = []
    pending: list[tuple[str, bool]] = []
    quote = ""
    i, n, line_start = 0, len(text), 0
    while i < n:
        c = text[i]
        if quote == "'":
            out.append(c)
            quote = "" if c == "'" else quote
            i += 1
            continue
        if quote == '"':
            if c == "\\" and i + 1 < n:
                out.append(text[i:i + 2])
                i += 2
                continue
            out.append(c)
            quote = "" if c == '"' else quote
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if c in "'\"":
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "#" and (i == 0 or text[i - 1] in " \t\n;&|()"):
            j = text.find("\n", i)
            j = n if j < 0 else j
            if not drop_comments:
                out.append(text[i:j])
            i = j
            continue
        if text.startswith("<<", i) and not text.startswith("<<<", i):
            m = _HEREDOC_OP.match(text, i)
            delim = re.sub(r"[\\'\"]", "", m.group(2)) if m else ""
            if m and delim:
                pending.append((delim, m.group(1) == "-"))
                out.append("<<HEREDOC " if cut_heredocs else m.group(0))
                i = m.end()
                continue
        if c == "\n" and pending:
            op_line = text[line_start:i]
            out.append(c)
            i += 1
            for delim, _tabs in pending:
                body: list[str] = []
                while i < n:
                    j = text.find("\n", i)
                    j = n if j < 0 else j
                    line = text[i:j]
                    i = min(j + 1, n)
                    if line.strip(" \t") == delim:
                        if not cut_heredocs:
                            out.append(line + "\n")
                        break
                    body.append(line)
                    if not cut_heredocs:
                        out.append(line + "\n")
                bodies.append((op_line, "\n".join(body)))
            pending = []
            line_start = i
            continue
        if c == "\n":
            line_start = i + 1
        out.append(c)
        i += 1
    return "".join(out), bodies


def _strip_heredocs(command: str) -> tuple[str, list[tuple[str, str]]]:
    """Remove heredoc bodies but KEEP the rest of the operator line (it still runs). Returns (cmd, [(op_line, body)])."""
    return _lex_shell(command)


def strip_shell_comments(text: str) -> str:
    """Shell script text without comments (heredoc bodies kept verbatim: they are data, or commands for a nested shell)."""
    return _lex_shell(text, drop_comments=True, cut_heredocs=False)[0]


def _strip_groups(part: str) -> str:
    part = part.strip()
    while part[:1] in ("(", "{") and not part.startswith("${"):
        part = part[1:].lstrip()
    while part.endswith(")") and part.count("(") < part.count(")"):
        part = part[:-1].rstrip()
    while part.endswith("}") and part.count("{") < part.count("}"):
        part = part[:-1].rstrip()
    return part


def split_segments(command: str) -> list[str]:
    """Split a shell command into simple-command segments (best effort), including substitutions and heredoc lines."""
    command = normalize(command)
    stripped, _ = _strip_heredocs(command)
    segments: list[str] = []
    for m in _SUBST.finditer(stripped):
        inner = next((g for g in m.groups() if g), None)
        if inner:
            segments.extend(split_segments(inner))
    for part in _SEGMENT_SPLIT.split(stripped):
        part = _strip_groups(part)
        if part:
            segments.append(part)
    return segments


def _mask_quotes(text: str) -> str:
    """Same-length copy of ``text`` with quoted characters (and backslash-escaped ones) replaced by ``_``, so
    operators can be found the way the shell finds them; the quote characters themselves are kept."""
    out = list(text)
    quote = ""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            if quote == "$'" and c == "\\" and i + 1 < n:
                out[i] = out[i + 1] = "_"
                i += 2
                continue
            if quote == '"' and c == "\\" and i + 1 < n:
                out[i] = out[i + 1] = "_"
                i += 2
                continue
            if c == quote[-1]:
                quote = ""
            else:
                out[i] = "_"
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out[i + 1] = "_"
            i += 2
            continue
        if c == "$" and text.startswith("$'", i):
            quote = "$'"
            i += 2
            continue
        if c in "'\"":
            quote = c
        i += 1
    return "".join(out)


def _substitutions(text: str) -> list[tuple[int, int, str]]:
    """Top-level ``$(…)``, ``<(…)``, ``>(…)`` and backtick substitutions as (start, end, body), with parentheses
    balanced and quotes respected (unlike _SUBST, which only handles one level of nesting)."""
    out: list[tuple[int, int, str]] = []
    quote = ""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote == "'":
            quote = "" if c == "'" else quote
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c == "'" and not quote:
            quote = "'"
            i += 1
            continue
        if c == '"':
            quote = "" if quote == '"' else '"'
            i += 1
            continue
        if c in "$<>" and text.startswith("(", i + 1) and not text.startswith("((", i + 1):
            depth, j, q = 1, i + 2, ""
            while j < n and depth:
                ch = text[j]
                if q:
                    if ch == "\\" and q == '"':
                        j += 2
                        continue
                    q = "" if ch == q else q
                elif ch == "\\":
                    j += 1
                elif ch in "'\"":
                    q = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                j += 1
            out.append((i, j, text[i + 2:j - 1] if not depth else text[i + 2:]))
            i = j
            continue
        if c == "`":
            j = text.find("`", i + 1)
            j = n if j < 0 else j + 1
            out.append((i, j, text[i + 1:j - 1]))
            i = j
            continue
        i += 1
    return out


# substitutions kept verbatim in the outer command (path idioms and generated-code checks read them); any other
# body is replaced by a placeholder there, so its quoted text is not tokenized as the outer command's arguments
_SIMPLE_SUBST = re.compile(r"^\s*(?:<|(?:dirname|basename|git\s+rev-parse|pwd|cat|cd|realpath|readlink|fnm|brew|pyenv|rbenv|nodenv|direnv|mise|rtx)\b)[^\n]*$")


def split_segments_quoted(command: str) -> list[str]:
    """Like split_segments, but ``;``/``|``/``&&`` inside quotes do not split (an awk or sed program stays one
    argument). Used where a garbage fragment would look like a program path; the rule checks keep the more
    conservative split_segments."""
    command = normalize(command)
    stripped, _ = _strip_heredocs(command)
    segments: list[str] = []
    pieces: list[str] = []
    last = 0
    for a, b, body in _substitutions(stripped):
        segments.extend(split_segments_quoted(body))
        keep = _SIMPLE_SUBST.match(body) is not None
        pieces.append(stripped[last:a] + (stripped[a:b] if keep else stripped[a:a + 2] + "__SUBST__)"))
        last = b
    stripped = "".join(pieces) + stripped[last:]
    masked = _mask_quotes(stripped)
    start = 0
    for m in _SEGMENT_SPLIT.finditer(masked):
        part = _strip_groups(stripped[start:m.start()])
        if part:
            segments.append(part)
        start = m.end()
    part = _strip_groups(stripped[start:])
    if part:
        segments.append(part)
    return segments


def _shlex(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def tokenize(segment: str) -> list[str]:
    """Tokens of the program that actually runs: env assignments, shell keywords and wrappers are peeled off."""
    out = _shlex(segment)
    while out:
        t = out[0]
        base = os.path.basename(t)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) or t in _RESERVED:
            out.pop(0)
            continue
        if base in _WRAPPER_OPTS:
            out.pop(0)
            if base in _DASHDASH_WRAPPERS and "--" in out:
                out = out[out.index("--") + 1:]
                continue
            if base == "env" and out and out[0] in ("-S", "--split-string"):
                return _shlex(" ".join(out[1:]))
            if base == "env" and out and out[0].startswith("-S") and len(out[0]) > 2:
                return _shlex(out[0][2:] + " " + " ".join(out[1:]))
            takes = _WRAPPER_OPTS[base]
            while out and out[0].startswith("-") and out[0] != "-":
                opt = out.pop(0)
                if opt in takes and "=" not in opt and out:
                    out.pop(0)
            for _ in range(_POSITIONAL_AFTER_OPTS.get(base, 0)):
                if out:
                    out.pop(0)
            continue
        break
    return out


def _git_sub(tokens: list[str]) -> tuple[str | None, list[str], list[str]]:
    """``git -C x -c k=v push -f`` -> ("push", ["-f"], ["k=v"])."""
    i = 1
    overrides: list[str] = []
    while i < len(tokens):
        t = tokens[i]
        if t in ("-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env"):
            i += 2
            continue
        if t == "-c":
            if i + 1 < len(tokens):
                overrides.append(tokens[i + 1])
            i += 2
            continue
        if t.startswith("-c") and len(t) > 2:
            overrides.append(t[2:])
            i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t, tokens[i + 1:], overrides
    return None, [], overrides


def _short_flag_cluster_has(args: list[str], letter: str) -> bool:
    for a in args:
        if a.startswith("-") and not a.startswith("--") and len(a) > 1 and letter in a[1:] and not re.match(r"^-\d", a):
            return True
    return False


def _expand(token: str, cwd: Path) -> str:
    t = token
    for var in ("${HOME}", "$HOME", "~"):
        if t == var or t.startswith(var + "/"):
            t = _HOME + t[len(var):]
            break
    for var in ("${PWD}", "$PWD", "$(pwd)", "`pwd`"):
        if t == var or t.startswith(var + "/"):
            t = str(cwd) + t[len(var):]
            break
    if not os.path.isabs(t):
        t = os.path.join(str(cwd), t)
    return os.path.normpath(t)


def _dangerous_delete_target(token: str, repo: Path) -> str | None:
    """Why deleting ``token`` recursively is catastrophic, or None."""
    if token in ("*", "./*", ".*", "./.*", "**"):
        return "every file in the repository"
    t = _expand(token.rstrip("/*") or "/", repo)
    real_repo = os.path.normpath(os.path.realpath(str(repo)))
    repo_n = os.path.normpath(str(repo))
    homes = {"/", _HOME, "/Users", "/home", os.path.dirname(_HOME)}
    if t in homes or re.match(r"^/(Users|home)/[^/]+$", t):
        return f"{t} (root / a home directory)"
    for r in (repo_n, real_repo):
        if t == r or r.startswith(t.rstrip("/") + "/"):
            return f"{t} (the repository or one of its parent directories)"
        if t == os.path.join(r, ".git") or t.startswith(os.path.join(r, ".git") + "/"):
            return f"{t} (git internals: unpushed commits would be lost)"
    return None


_PIPE_TO_SHELL = re.compile(
    r"\b(curl|wget|fetch|http|aria2c)\b[^;&\n]*\|\s*(?:sudo\s+(?:-\S+\s+)*)?(?:\S*/)?(?:env\s+)?((?:ba|z|da|k|fi|c|tc)?sh|python\d*(?:\.\d+)?|node|perl|ruby|php|deno|bun|osascript|source)\b"
)
_SHELL_PROCSUB = re.compile(r"(\b(?:ba|z|da|k)?sh|\bsource|(?:^|[;&|]\s*)\.)\s+(?:-c\s+)?[\"']?\s*(?:<\(|\$\()\s*(?:curl|wget)\b")
_DOWNLOAD_THEN_RUN = re.compile(r"\b(curl|wget)\b[^;&\n|]*\s-[oO]\s*(\S+)[^\n]*?(?:&&|;)\s*(?:ba|z|da)?sh\s+\2\b")

_INSTALL_ADD = [
    ("npm", {"install", "i", "add", "isntall", "in"}),
    ("pnpm", {"add", "install", "i"}),
    ("yarn", {"add"}),
    ("bun", {"add", "install", "i"}),
    ("pip", {"install"}),
    ("pip3", {"install"}),
    ("uv", {"add"}),
    ("poetry", {"add"}),
    ("cargo", {"add", "install"}),
    ("go", {"get", "install"}),
    ("gem", {"install"}),
    ("composer", {"require"}),
    ("deno", {"add", "install"}),
]
_LOCKFILE_INSTALL = {
    ("npm", "ci"), ("pnpm", "install"), ("pnpm", "i"), ("yarn", "install"), ("yarn", ""), ("bun", "install"),
    ("bun", "i"), ("uv", "sync"), ("poetry", "install"), ("npm", "install"), ("npm", "i"),
}
_PM_VALUE_OPTS = {"-C", "--dir", "--filter", "-F", "--workspace", "-w", "--prefix", "--cwd", "--registry", "--loglevel", "--reporter"}


def _classify_install(tokens: list[str]) -> str | None:
    """Return 'global', 'add', 'lockfile' or None."""
    if not tokens:
        return None
    prog = os.path.basename(tokens[0])
    if prog in ("python", "python3") and len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] in ("pip", "pip3"):
        tokens = ["pip"] + tokens[3:]
        prog = "pip"
    if prog == "uv" and len(tokens) >= 3 and tokens[1] == "pip" and tokens[2] == "install":
        tokens = ["pip", "install"] + tokens[3:]
        prog = "pip"
    if prog in ("brew", "apt", "apt-get", "yum", "dnf", "port", "snap", "choco", "winget", "pipx", "npx"):
        if prog == "npx":
            return None
        if len(tokens) > 1 and any(t in ("install", "reinstall", "upgrade", "add") for t in tokens[1:3]):
            return "global"
        return None
    rest = tokens[1:]
    if any(a in ("-g", "--global", "--location=global") or a.startswith("--location=global") for a in rest):
        if prog in ("npm", "pnpm", "yarn", "bun"):
            return "global"
    # find the subcommand: skip options and the values of value-taking options; `yarn workspace <name> <sub>`
    words: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a.startswith("-"):
            if a in _PM_VALUE_OPTS and "=" not in a:
                i += 1
            i += 1
            continue
        words.append(a)
        i += 1
    if prog == "yarn" and words[:1] == ["workspace"]:
        words = words[2:]
    if prog == "yarn" and words[:1] == ["global"]:
        return "global"
    sub = words[0] if words else ""
    pkgs = words[1:]
    if prog == "pip" and sub == "install":
        args = rest[rest.index("install") + 1:] if "install" in rest else []
        req_vals = {args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-r", "--requirement", "-e", "--editable", "-c", "--constraint")}
        named = [a for a in args if not a.startswith("-") and a not in req_vals]
        return "add" if named else "lockfile"
    for p, subs in _INSTALL_ADD:
        if prog == p and sub in subs:
            if prog in ("cargo", "go") and sub == "install":
                return "global"
            if (prog, sub) in _LOCKFILE_INSTALL and not pkgs:
                return "lockfile"
            return "add" if pkgs or sub not in ("install", "i") else "lockfile"
    if (prog, sub) in _LOCKFILE_INSTALL:
        return "lockfile"
    return None


_SYSTEM_DESTRUCTIVE = re.compile(
    r"\b(shutdown|reboot|halt|mkfs(\.\w+)?|diskutil\s+(erase\w*|partition\w*|zeroDisk|secureErase)|launchctl\s+(unload|remove|bootout))\b"
    r"|\bdd\b[^|;]*\bof=/dev/|:\(\)\s*\{\s*:\|:&\s*\};:|\bchmod\s+-R\s+[0-7]{3,4}\s+(/|~)(\s|$)|\bchown\s+-R\s+\S+\s+(/|~)(\s|$)"
)
_SECRET_DUMP = re.compile(
    r"\b(security\s+(find-generic-password|find-internet-password|dump-keychain|export)|gh\s+auth\s+token"
    r"|gh\s+auth\s+status\b[^|;&\n]*\s(-t|--show-token)\b|git\s+credential(-[\w-]+)?\s+(fill|get)\b|op\s+(read|item\s+get)"
    r"|vault\s+(read|kv\s+get)|aws\s+configure\s+(get|export-credentials)|gcloud\s+auth\s+print-(access|identity)-token)"
    r"|(?:echo|printf|cat)\b[^;&|\n]*\$\{?[A-Za-z_]*(TOKEN|SECRET|API_KEY|APIKEY|PASSWORD|PASSWD|PRIVATE_KEY|ACCESS_KEY)[A-Za-z_]*\}?",
    re.I,
)

# Raw backstop over the DE-QUOTED command (quotes, backslashes and brackets removed): always-deny rules.
_BACKSTOP: list[tuple[str, re.Pattern[str], str]] = [
    ("force-push", re.compile(r"\bgit\b[^\n;&|]*?\bpush\b[^\n;&|]*?[\s,](--force\b|--force-with-lease|--force-if-includes|--mirror\b|-[a-zA-Z]*f[a-zA-Z]*\b|\+[\w./-]+)"), "Force pushes (--force, -f, --force-with-lease, +refspec, --mirror) are never allowed."),
    ("reset-hard", re.compile(r"\bgit\b[^\n;&|]*?\breset\b[^\n;&|]*?[\s,]--hard\b"), "git reset --hard is never allowed."),
    ("clean-force", re.compile(r"\bgit\b[^\n;&|]*?\bclean\b[^\n;&|]*?[\s,](-[a-zA-Z]*f[a-zA-Z]*|--force)\b"), "git clean -f is never allowed."),
    ("branch-force-delete", re.compile(r"\bgit\b[^\n;&|]*?\bbranch\b[^\n;&|]*?[\s,](-[a-zA-Z]*D[a-zA-Z]*|-[a-zA-Z]*d[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*d[a-zA-Z]*|--delete\b[^\n;&|]*--force|--force\b[^\n;&|]*--delete)\b"), "Force-deleting branches (git branch -D / -df) is never allowed."),
    ("no-verify", re.compile(r"--no-veri\w*|\bcore\.hooksPath\b|\bHUSKY=0\b|\bLEFTHOOK=0\b|\bSKIP_HOOKS?=", re.I), "Skipping git hooks (--no-verify, core.hooksPath, HUSKY=0) is never allowed."),
    ("gh-admin-merge", re.compile(r"\bpr\b[^\n;&|]*?\bmerge\b[^\n;&|]*?--admin\b"), "gh pr merge --admin bypasses branch protection and is never allowed."),
    ("sudo", re.compile(r"(?:^|[\s;&|(,\[])(sudo|doas|pkexec)\b"), "sudo / privilege escalation is never allowed."),
    ("git-alias", re.compile(r"\balias\.[\w-]+\s*=|\bgit\b[^\n;&|]*\bconfig\b[^\n;&|]*\b(alias\.|core\.sshCommand|credential\.|clean\.requireForce|core\.hooksPath)", re.I), "Defining git aliases or rewriting hook/credential/clean settings is never allowed."),
    ("gh-alias", re.compile(r"\bgh\s+alias\s+(set|import)\b"), "gh aliases are never allowed."),
    ("history-rewrite", re.compile(r"\bgit\b[^\n;&|]*\b(filter-branch|filter-repo)\b"), "History rewrites are never allowed."),
]


def _dequote(s: str) -> str:
    return re.sub(r"[\"'`\\\[\]]", " ", s).replace(",", " , ")


def _backstop(cmd: str) -> Decision | None:
    flat = _dequote(cmd)
    for rule, pat, reason in _BACKSTOP:
        if pat.search(flat):
            return Decision.deny(rule, reason)
    return None


def _credential_check(cmd: str, repo: Path) -> Decision | None:
    joined = re.sub(r"[\"'\\]", "", cmd)  # the shell concatenates quoted pieces: ~/.s'sh' == ~/.ssh
    for text in (cmd, _dequote(cmd), joined):
        if _SECRET_DUMP.search(text):
            return Decision.deny("credentials", "Reading or printing credentials/tokens is never allowed.")
        if _CREDENTIAL_RE.search(" " + text) or _mentions_dotenv(text) or re.search(r"(^|[\s'\"=:/<])\.ssh/|(^|[\s'\"=:/<])\.aws/", text):
            return Decision.deny("credentials", "Commands that touch credential files (~/.ssh, ~/.aws, .env*, token stores) are not allowed.")
    for tok in re.findall(r"[^\s;&|<>()'\"]+", _dequote(cmd)):
        if any(ch in tok for ch in "*?[") and len(tok) < 200:
            try:
                root = repo if not os.path.isabs(os.path.expanduser(tok)) else Path("/")
                matches = globmod.glob(os.path.expanduser(tok), root_dir=str(root)) if root.exists() else []
            except (OSError, ValueError):
                matches = []
            if any(is_credential_path(m) for m in matches[:200]):
                return Decision.deny("credentials", f"The glob {tok} matches credential/secret files.")
        elif is_credential_path(tok) and not tok.startswith("-"):
            return Decision.deny("credentials", f"{tok} is a credential/secret file.")
    return None


def _credential_text_check(text: str) -> Decision | None:
    """The text-only part of the credential check, for script/program content (no glob expansion of code tokens)."""
    for t in (text, re.sub(r"[\"'\\]", "", text)):
        if _SECRET_DUMP.search(t):
            return Decision.deny("credentials", "Reading or printing credentials/tokens is never allowed.")
        if _CREDENTIAL_RE.search(" " + t) or _mentions_dotenv(t) or re.search(r"(^|[\s'\"=:/<])\.ssh/|(^|[\s'\"=:/<])\.aws/", t):
            return Decision.deny("credentials", "Code that touches credential files (~/.ssh, ~/.aws, .env*, token stores) is not allowed.")
    return None


_DEV_STREAM = re.compile(r"^/dev/(null|stdout|stderr|tty|fd/\d+)$")
_WRITE_PROGS = {"rm", "rmdir", "unlink", "shred", "truncate", "trash", "mv", "cp", "ln", "install", "tee", "sed", "perl", "dd", "touch", "mkdir", "chmod", "chown", "rsync", "ditto", "patch"}


def _write_targets(toks: list[str], seg: str) -> tuple[list[str], list[str]]:
    """(write targets, delete targets) of one simple command."""
    masked = _mask_quotes(seg)  # a quoted ">" is text, not a redirect
    writes: list[str] = [seg[m.start(1):m.end(1)] for m in re.finditer(r"(?<![<>0-9&])>>?\|?\s*([^\s;&|<>]+)", masked)]
    writes += [seg[m.start(1):m.end(1)] for m in re.finditer(r"\b\d?>>?\s*([^\s;&|<>&]+)", masked)]
    # `"$(cmd 2>/dev/null)"` leaves the closing paren/quote on the captured target
    writes = [re.sub(r"[)\"'`]+$", "", (w.split() or [""])[0]).lstrip("\"'`") for w in writes]
    writes = [w for w in writes if w and w not in ("/dev/null", "&1", "&2", "/dev/stderr", "/dev/stdout")]
    deletes: list[str] = []
    if not toks:
        return writes, deletes
    prog = os.path.basename(toks[0])
    args = [a for a in toks[1:] if not a.startswith("-")]
    if prog in ("rm", "rmdir", "unlink", "shred", "trash", "rimraf"):
        deletes += args
    elif prog in ("truncate", "touch", "mkdir", "chmod", "chown"):
        writes += args[1:] if prog in ("chmod", "chown") else args
    elif prog in ("mv",):
        deletes += args[:-1]
        writes += args[-1:]
    elif prog in ("cp", "ln", "install", "ditto", "rsync"):
        writes += args[-1:]
        if prog == "rsync" and "--delete" in " ".join(toks):
            deletes += args[-1:]
    elif prog == "tee":
        writes += args
    elif prog in ("sed", "perl") and any(re.match(r"^-[A-Za-z]*i", a) or a.startswith("--in-place") for a in toks[1:]):
        has_e = any(a in ("-e", "--expression") for a in toks[1:])
        writes += args if has_e else args[1:]
    elif prog == "dd":
        writes += [a[3:] for a in toks[1:] if a.startswith("of=")]
    elif prog == "patch":
        writes += args
    elif prog == "git":
        sub, gargs, _ = _git_sub(toks)
        if sub in ("rm", "mv"):
            (deletes if sub == "rm" else writes).extend(a for a in gargs if not a.startswith("-"))
        elif sub in ("restore", "checkout") and "--" in gargs:
            writes += gargs[gargs.index("--") + 1:]
        elif sub == "checkout" and len([a for a in gargs if not a.startswith("-")]) >= 2 and not any(a in ("-b", "-B", "-c", "-C", "--orphan") for a in gargs):
            writes += [a for a in gargs if not a.startswith("-")][1:]  # git checkout <rev> <paths>
        elif sub == "restore":
            writes += [a for a in gargs if not a.startswith("-")]
        elif sub == "apply":
            writes += ["."]  # cannot know: treat as touching the repo root (protected-path ancestor check skipped)
    elif prog == "find" and any(a in ("-delete",) for a in toks) or (prog == "find" and any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in toks) and re.search(r"-exec\w*\s+(rm|shred|unlink)\b", seg)):
        roots = []
        for a in toks[1:]:
            if a.startswith("-") or a in ("(", ")", "!"):
                break
            roots.append(a)
        deletes += roots or ["."]
    return writes, deletes


def _check_paths(toks: list[str], seg: str, ctx: PolicyContext) -> Decision | None:
    writes, deletes = _write_targets(toks, seg)
    prog = os.path.basename(toks[0]) if toks else ""
    recursive = prog in ("rm", "trash", "find", "rsync", "rimraf") and (
        prog in ("find", "rsync", "rimraf") or any(a in ("-r", "-R", "--recursive") for a in toks[1:]) or _short_flag_cluster_has(toks[1:], "r") or _short_flag_cluster_has(toks[1:], "R")
    )
    for t in deletes:
        if recursive or prog in ("rm", "rmdir", "trash", "find", "rimraf"):
            why = _dangerous_delete_target(t, ctx.repo)
            if why:
                return Decision.deny("rm-recursive-root", f"Deleting {why} is never allowed.")
    for t in writes + deletes:
        if not t or t.startswith("-") or t in ("<<HEREDOC",) or _DEV_STREAM.match(t):
            continue
        expanded = _expand(t, ctx.repo)
        rel = rel_to_repo(expanded, ctx.repo)
        if rel is None:
            if expanded.startswith(_TMP_PREFIXES) or os.path.realpath(expanded).startswith(_TMP_PREFIXES) or expanded in ("/dev/null",):
                continue
            return Decision.deny("outside-repo", f"Shell writes outside the repository are not allowed ({t}).")
        if rel in ("", ".") and t in deletes:
            return Decision.deny("rm-recursive-root", "Deleting the repository root is never allowed.")
        d = _protected_reason(rel, ctx, deleting=t in deletes)
        if d is not None and not d.allow:
            return Decision.deny(d.rule, d.reason.replace("Changing", "Shell writes to").replace("Edit it", "Change it"))
        if t in writes:
            d = _installed_package_reason(rel) or _ran_script_reason(expanded, ctx)
            if d is None and ctx.ran_scripts and ctx.phase in UNSANDBOXED_PHASES and (t.endswith("/") or os.path.isdir(expanded)):
                under = os.path.normpath(os.path.realpath(expanded)) + "/"
                if any(r.startswith(under) for r in ctx.ran_scripts):
                    d = Decision.deny("ran-script", f"{t} holds a script that already ran in {ctx.phase}; it cannot be rewritten in the same phase.")
            if d is not None:
                return d
    return None


def _nested_commands(toks: list[str]) -> list[str]:
    """Command strings executed by interpreters / runners inside this simple command."""
    if not toks:
        return []
    prog = os.path.basename(toks[0])
    out: list[str] = []
    if prog in _SHELLS:
        for i, a in enumerate(toks[1:], 1):
            if a.startswith("-") and not a.startswith("--") and "c" in a[1:] and i + 1 < len(toks):
                out.append(toks[i + 1])
                break
    elif prog == "eval":
        out.append(" ".join(toks[1:]))
    elif prog in ("xargs", "parallel"):
        val_opts = {"-I", "-i", "-n", "-P", "-L", "-l", "-s", "-E", "-e", "-d", "-a", "--delimiter", "--max-args", "--max-procs", "-j", "--jobs"}
        i = 1
        while i < len(toks) and toks[i].startswith("-"):
            i += 2 if toks[i] in val_opts else 1
        if i < len(toks):
            out.append(" ".join(shlex.quote(t) for t in toks[i:]))
    elif prog == "find":
        for i, a in enumerate(toks):
            if a in ("-exec", "-execdir", "-ok", "-okdir"):
                rest = []
                for b in toks[i + 1:]:
                    if b in (";", "+", "\\;"):
                        break
                    rest.append(b)
                if rest:
                    out.append(" ".join(shlex.quote(t) for t in rest))
    elif prog == "script":
        for i, a in enumerate(toks[1:], 1):
            if a in ("-c", "--command") and i + 1 < len(toks):
                out.append(toks[i + 1])
                break
        else:
            # BSD/macOS: script [-adeFkpqr] [-t time] [file [command ...]]
            rest = toks[1:]
            while rest and rest[0].startswith("-"):
                rest = rest[2:] if rest[0] == "-t" else rest[1:]
            if len(rest) > 1:
                out.append(" ".join(shlex.quote(t) for t in rest[1:]))
    elif prog in ("watch",):
        out.append(" ".join(toks[1:]))
    elif prog in ("concurrently", "conc"):
        vals = {"-n", "--names", "-c", "--prefix-colors", "-p", "--prefix", "-l", "--prefix-length", "-t", "--timestamp-format",
                "-s", "--success", "--restart-tries", "--restart-after", "--max-processes", "-m", "--default-input-target", "--handle-input",
                "--name-separator", "--pad-prefix", "--cwd", "-P", "--passthrough-arguments"}
        for w in _words(toks[1:], vals):
            m = re.match(r"^(npm|pnpm|yarn|bun):(.+)$", w)
            out.append(f"{m.group(1)} run {shlex.quote(m.group(2))}" if m else w)
    return out


# --------------------------------------------------------------------------------------
# Script content scanning
#
# "Write a script in one turn, run it in the next" would get past every rule above, so a command that runs a
# local script is also judged by what the script contains: shell scripts, package.json scripts and Makefile
# recipes are evaluated as if typed (same phase context, recursively, depth <= SCRIPT_MAX_DEPTH); other languages
# get the raw backstop and the credential check over their text. Only files the worker could have written are
# read: regular files in the repo (vendored/build dirs excluded) or in temp dirs. A missing file is allowed (the
# command fails anyway); an unreadable or oversized one is denied. Writing a file and running it in the same
# command line is denied, because the check would read the old content.
# --------------------------------------------------------------------------------------

SCRIPT_MAX_BYTES = 512 * 1024
SCRIPT_MAX_DEPTH = 4
# installed packages: never read, except the entry points a command runs by name (node_modules/.bin/<name>, .venv/bin/
# <name>) when they are regular files: pnpm's shims, or something the worker planted (a symlink into a package is not read)
_VENDOR_DIRS = frozenset({"node_modules", ".venv", "venv"})
_VENDOR_BIN = re.compile(r"(?:^|/)(?:node_modules/\.bin|\.venv/bin|venv/bin)/[^/]+$")
_SHELL_FAMILY = {"bash", "sh", "zsh", "dash", "ksh", "mksh", "csh", "tcsh", "fish"}
# Phases that never run under the OS sandbox (sessions.SANDBOX_EXEMPT_PHASES): there a script's content is the only
# check, so a script that already ran there may not be rewritten and none may run deferred or in the background.
UNSANDBOXED_PHASES = frozenset({"feature_start", "ship", "linear", "halt_notice"})
_TSX_FAMILY = {"tsx", "ts-node", "ts-node-esm", "ts-node-script", "esno", "esrun", "vite-node", "jiti", "babel-node", "swc-node"}
_INTERP_VALUE_OPTS: dict[str, set[str]] = {
    "shell": {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"},
    "python": {"-W", "-X", "--check-hash-based-pycs"},
    "node": {"-r", "--require", "--import", "--loader", "--experimental-loader", "-C", "--conditions", "--input-type", "--env-file", "--env-file-if-exists", "--title", "--disable-warning", "--watch-path", "--test-reporter", "--test-reporter-destination", "--experimental-config-file", "--run"},
    "tsx": {"--tsconfig", "-r", "--require", "--import", "--env-file", "--loader", "-C", "--conditions", "-P", "--project", "-O", "--compiler-options"},
    "perl": {"-I", "-M", "-m"},
    "ruby": {"-I", "-r", "-C", "-E"},
    "php": {"-c", "-d"},
    "deno": {"-c", "--config", "--import-map", "--lock", "--cert", "--location", "--seed", "--v8-flags", "--env-file", "--ext", "-L", "--log-level"},
    "awk": {"-F", "-v", "-i", "-l", "--field-separator", "--assign", "--include", "--load"},
    "osascript": {"-l", "-s"},
    "expect": {"-D"},
}
_INLINE_FLAGS: dict[str, set[str]] = {
    "shell": set(),
    "python": {"-c", "-m"},
    "node": {"-e", "--eval", "-p", "--print", "--test", "--run"},
    "tsx": {"-e", "--eval", "-p", "--print"},
    "perl": {"-e", "-E"},
    "ruby": {"-e"},
    "php": {"-r"},
    "deno": {"eval"},
    "awk": set(),
    "osascript": {"-e"},
    "expect": {"-c"},
    "tclsh": set(),
    "wish": set(),
}
_CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx", ".rb", ".pl", ".php", ".awk"}
_SHELL_EXT = {".sh", ".bash", ".zsh", ".ksh", ".dash", ".csh", ".tcsh", ".fish"}
_LIFECYCLE = ("preinstall", "install", "postinstall", "prepublish", "preprepare", "prepare", "postprepare")
_ENV_INIT_OK = re.compile(r"^\s*(?:\S*/)?(brew\s+shellenv|fnm\s+env|pyenv\s+init|rbenv\s+init|nodenv\s+init|direnv\s+export|mise\s+(?:activate|env)|rtx\s+env)\b")
# Variables that make an innocent-looking program load code from a file (or another program) first.
_EXEC_ENV_FILES = {"BASH_ENV": "shell", "ENV": "shell"}
_EXEC_ENV_DENY = {"LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "GIT_SSH_COMMAND", "GIT_SSH", "GIT_EXEC_PATH", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_EXTERNAL_DIFF", "PERL5OPT", "PROMPT_COMMAND", "npm_config_script_shell", "NPM_CONFIG_SCRIPT_SHELL", "npm_config_node_options", "NPM_CONFIG_NODE_OPTIONS"}


@dataclass(frozen=True)
class _Scan:
    """Script-scanning state threaded through evaluate_bash."""

    cwd: Path
    chain: tuple[str, ...] = ()  # scripts being evaluated, outermost first (cycle and depth guard)
    script: Path | None = None  # the script file whose content is being evaluated ($0)
    vars: tuple[tuple[str, str], ...] = ()  # NAME=path assignments resolved from that script
    written: frozenset[str] = frozenset()  # paths written earlier in the same command line (outer levels included)

    def into(self, key: str, cwd: Path, script: Path | None = None, vars: tuple[tuple[str, str], ...] = ()) -> "_Scan":
        return _Scan(cwd, self.chain + (key,), script, vars, self.written)


@dataclass(frozen=True)
class _Ref:
    """Something a command runs whose content can be checked."""

    kind: str  # "file" | "pkg" | "make" | "deny"
    path: Path  # the script file, package.json or Makefile
    lang: str = "shell"  # kind == "file": "shell" or "code"
    name: str = ""  # package script name / make target
    cwd: Path | None = None  # where the content runs
    text: str = ""  # kind in ("pkg", "make"): the commands
    reason: str = ""  # kind == "deny"


def _interp_family(prog: str) -> str | None:
    if prog in _SHELL_FAMILY:
        return "shell"
    if re.fullmatch(r"python[\d.]*|pypy[\d.]*", prog):
        return "python"
    if prog in ("node", "nodejs"):
        return "node"
    if prog in _TSX_FAMILY:
        return "tsx"
    if prog in ("perl", "ruby", "php", "deno"):
        return prog
    if prog in ("awk", "gawk", "mawk", "nawk"):
        return "awk"
    # osascript (`do shell script "…"`), expect (`spawn` / `exec`), tclsh / wish (`exec`): code that can run commands
    if prog in ("osascript", "expect", "tclsh", "wish"):
        return prog
    return None


def _awk_target(args: list[str]) -> tuple[str, str | None, str | None, bool]:
    """awk -f PROGFILE (also gawk -E) runs a program file; otherwise the first operand is the program text."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a in ("-f", "-E", "--file", "--exec") and i + 1 < len(args):
            return "awk", args[i + 1], None, False
        if a.startswith(("--file=", "--exec=")):
            return "awk", a.split("=", 1)[1], None, False
        if not a.startswith("-") or a == "-":
            break
        i += 2 if (a in _INTERP_VALUE_OPTS["awk"] and "=" not in a) else 1
    return "awk", None, None, False


# Short-option clusters: `perl -lne CODE`, `perl -0777ne CODE`, `perl -eCODE`, `ruby -ne CODE`, `node -pe CODE`. Parsed
# with an ALLOWLIST, the way perl/ruby parse them: switches without a value (_CLUSTER_FLAGS) may precede the inline
# switch; a value switch (_CLUSTER_REST) takes the rest of the word (or, from _CLUSTER_NEXT and nothing left, the next
# argument); 0 / l (perl) and 0 / W (ruby) take the digits that follow. Anything else (a space, `-`, an unknown letter,
# perl's -0x) ends the cluster as "flags", so the next word is still taken as the script and scanned: an unknown
# shape costs at worst a false denial, never an unchecked script (perl '-w e' evil.pl runs evil.pl, not inline code).
_CLUSTER_INLINE = {"perl": "eE", "ruby": "e", "node": "e", "tsx": "e"}
_CLUSTER_FLAGS = {"perl": "acfgnpstTuUwWXhvVS", "ruby": "acdlnpsSvwyhU"}
_CLUSTER_REST = {"perl": "CdDFiIxmM", "ruby": "CEFiIrxXT"}
_CLUSTER_NEXT = {"perl": "IMm", "ruby": "CEIrX"}
# switch -> (digits it takes, at most how many), measured on perl 5.34 / ruby: -0777 and -l777 take three octal digits,
# ruby -W one (-W0I = -W0 -I), ruby -0 three
_CLUSTER_DIGITS = {"perl": {"0": ("01234567", 3), "l": ("01234567", 3)}, "ruby": {"0": ("01234567", 3), "W": ("01234567", 1)}}


def _short_cluster(fam: str, a: str) -> tuple[str, str | None]:
    """Classify one short-option word of perl / ruby / node / tsx: ("inline", code attached to the switch, or None when
    the program is the next argument), ("value-next", None) when the cluster's last switch takes the next argument, or
    ("flags", None)."""
    if fam not in _CLUSTER_INLINE or not a.startswith("-") or a.startswith("--") or len(a) < 2:
        return "flags", None
    s = a[1:]
    if fam in ("node", "tsx"):  # node has no general clustering, but accepts -pe / -ep (print + eval)
        return ("inline", None) if s in ("pe", "ep") else ("flags", None)
    digits = _CLUSTER_DIGITS[fam]
    j = 0
    while j < len(s):
        ch = s[j]
        if ch in _CLUSTER_INLINE[fam]:
            return "inline", (s[j + 1:] or None)
        if ch in _CLUSTER_REST[fam]:
            if fam == "perl" and ch in "CFi":  # perl: these values stop at a space ('-C -I dir', '-i.bak -I dir')
                k = s.find(" ", j + 1)
                if k < 0:
                    return "flags", None
                j = k  # the space rule below decides what follows
                continue
            return ("value-next", None) if j + 1 == len(s) and ch in _CLUSTER_NEXT[fam] else ("flags", None)
        if fam == "ruby" and ch == "K":  # -Kx: one kcode character, then the cluster goes on (`-KxI dir` = -Kx -I dir)
            j += 2
            continue
        if fam == "perl" and ch == " ":  # perl: spaces, then more switches only after a `-` (`'- -I' dir`), else the end
            while j < len(s) and s[j] == " ":
                j += 1
            if j < len(s) and s[j] == "-":
                j += 1
                continue
            return "flags", None
        if fam == "ruby" and ch == "W" and j + 1 < len(s) and s[j + 1] == ":":
            return "flags", None  # -W:category takes the rest of the word
        if ch in digits:
            j += 1
            if fam == "perl" and ch == "0" and j < len(s) and s[j] == "x":
                return "flags", None  # -0xHHH: the rest of the word is the record separator (-0X is -0 then -X)
            allowed, most = digits[ch]
            if fam == "perl" and ch == "l" and j < len(s) and s[j] == "0":
                most = 4  # perl -l reads 3 octal digits, or 4 when the first is 0 (-l0777)
            k = 0
            while j < len(s) and k < most and s[j] in allowed:
                j += 1
                k += 1
            continue
        if ch in _CLUSTER_FLAGS[fam]:
            j += 1
            continue
        return "flags", None
    return "flags", None


def _interpreter_target(toks: list[str]) -> tuple[str, str | None, str | None, bool] | None:
    """(family, script token, python module, program read from stdin) for an interpreter invocation, else None."""
    if not toks:
        return None
    fam = _interp_family(os.path.basename(toks[0]))
    if fam == "awk":
        return _awk_target(_drop_redirects(toks[1:]))
    if fam == "expect" and "-f" in toks[1:]:
        i = toks.index("-f")
        return fam, (toks[i + 1] if i + 1 < len(toks) else None), None, False
    if fam is None and os.path.basename(toks[0]) == "bun":
        words = _take_opts(toks[1:], _PM_VALUES["bun"])[1]
        words = words[1:] if words[:1] == ["run"] else words
        if words and ("/" in words[0] or Path(words[0]).suffix.lower() in _CODE_EXT | _SHELL_EXT):
            return "bun", words[0], None, False
        return None
    if fam is None:
        return None
    vals, inline = _INTERP_VALUE_OPTS.get(fam, set()), _INLINE_FLAGS.get(fam, set())
    args = _drop_redirects(toks[1:])
    if fam == "tsx" and args[:1] == ["watch"]:
        args = args[1:]
    if fam == "deno":
        if args[:1] and args[0] in inline:
            return fam, None, None, False
        if args[:1] == ["run"]:
            args = args[1:]
        elif args[:1] and not args[0].startswith("-") and not re.search(r"[./]", args[0]):
            return fam, None, None, False  # deno task/fmt/lint/test/...: not a script file
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a == "-":
            return fam, None, None, True
        if not (a.startswith("-") or (fam == "shell" and a.startswith("+"))) or len(a) == 1:
            break
        name = a.split("=", 1)[0]
        if fam == "python" and not a.startswith("--") and "c" not in a[1:] and "m" in a[1:]:
            return fam, None, (args[i + 1] if i + 1 < len(args) else None), False
        if a in ("--version", "-V", "--help", "-h") or (a == "-v" and fam != "ruby") or name in inline:  # ruby -v runs the script
            return fam, None, None, False
        if not a.startswith("--"):
            cluster = a[1:]
            if fam == "shell":
                if "c" in cluster:
                    return fam, None, None, False
                if "s" in cluster:
                    return fam, None, None, True
                i += 2 if ("o" in cluster or "O" in cluster) else 1
                continue
            if fam == "python" and "c" in cluster:
                return fam, None, None, False
            if fam == "python" and "m" in cluster:
                return fam, None, (args[i + 1] if i + 1 < len(args) else None), False
            kind, _attached = _short_cluster(fam, a)
            if kind == "inline":  # perl -lne CODE: the program is inline code, not a script file
                return fam, None, None, False
            if kind == "value-next":
                i += 2
                continue
        if name in vals and "=" not in a:
            i += 2
            continue
        i += 1
    if i < len(args):
        return (fam, None, None, True) if args[i] == "-" else (fam, args[i], None, False)
    return fam, None, None, True


_PRELOAD_OPTS = {
    "shell": {"--rcfile", "--init-file"},
    "node": {"-r", "--require", "--import", "--loader", "--experimental-loader"},
    "tsx": {"-r", "--require", "--import", "--loader"},
    "ruby": {"-r"},
}


def _preloads(fam: str, toks: list[str]) -> list[tuple[str, str]]:
    """Files an interpreter loads before the program (``node -r f``, ``bash --rcfile f``)."""
    opts = _PRELOAD_OPTS.get(fam, set())
    out: list[tuple[str, str]] = []
    args = toks[1:]
    for i, a in enumerate(args):
        if a == "--":
            break
        name, _, val = a.partition("=")
        if name in opts:
            v = val or (args[i + 1] if i + 1 < len(args) else "")
            if v:
                out.append((v, "shell" if fam == "shell" else "code"))
        elif fam == "ruby" and a.startswith("-") and not a.startswith("--"):
            # a cluster whose value switch is -r: `ruby -wr lib.rb` / `ruby -wrlib.rb` require (run) that file
            s = a[1:]
            j = 0
            while j < len(s) and s[j] in _CLUSTER_FLAGS["ruby"]:
                j += 1
            if j < len(s) and s[j] == "r":
                v = s[j + 1:] or (args[i + 1] if i + 1 < len(args) else "")
                if v:
                    out.append((v, "code"))
    return out


def _drop_redirects(args: list[str]) -> list[str]:
    """Arguments without shell redirections (``< f``, ``2>&1``, ``>out``, ``<<HEREDOC``), which are not argv."""
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if re.fullmatch(r"\d*(<<<|<<-?|<>|<|>>|>\||>|&>>|&>)", a) or a == "<<HEREDOC":
            skip = a != "<<HEREDOC"
            continue
        if re.match(r"^\d*(<<?|>>?|&>>?)\S", a):
            continue
        out.append(a)
    return out


def _reads_program_from_stdin(toks: list[str]) -> bool:
    t = _interpreter_target(toks)
    return bool(t and t[3])


def _inline_code(toks: list[str]) -> str | None:
    """The program text an interpreter gets on its command line (python -c, node -e/-p, perl -e, ruby -e, php -r,
    deno eval, bun -e, osascript -e, an awk program), or None. perl and ruby join every -e (`perl -le 'do "x.pl"' -e 1`
    runs both), clustered switches included (perl -lne CODE, perl -eCODE, ruby -ne CODE, node -pe CODE)."""
    codes = _inline_codes(toks)
    return "\n".join(codes) if codes else None


def _inline_codes(toks: list[str]) -> list[str]:
    codes: list[str] = []
    first = _inline_code_first(toks)
    if first is not None:
        codes.append(first)
    fam = _interp_family(os.path.basename(toks[0])) if toks else None
    if fam not in _CLUSTER_INLINE:
        return codes
    args = _drop_redirects(toks[1:])
    vals = _INTERP_VALUE_OPTS.get(fam, set())
    exact = _INLINE_FLAGS.get(fam, set()) - {"-m", "--test", "--run"}
    i = 0
    while i < len(args):  # switches end at the first operand (the script, or @ARGV after -e) or at --
        a = args[i]
        if a == "--" or a == "-" or not a.startswith("-"):
            break
        name, eq, val = a.partition("=")
        code: str | None = None
        step = 1
        if a in exact:
            code, step = (args[i + 1] if i + 1 < len(args) else None), 2
        elif eq and name in exact and name.startswith("--"):
            code = val
        else:
            kind, attached = _short_cluster(fam, a)
            if kind == "inline":
                code, step = (attached, 1) if attached is not None else ((args[i + 1] if i + 1 < len(args) else None), 2)
            elif kind == "value-next" or (a in vals and "=" not in a):
                step = 2
        if code is not None and code not in codes:
            codes.append(code)
        i += step
    return codes


def _inline_code_first(toks: list[str]) -> str | None:
    """The first inline program on the command line (the original rule, kept for every interpreter)."""
    if not toks:
        return None
    prog = os.path.basename(toks[0])
    fam = _interp_family(prog)
    args = _drop_redirects(toks[1:])
    if fam == "awk":
        if _awk_target(args)[1] is not None:
            return None
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--":
                return args[i + 1] if i + 1 < len(args) else None
            if not a.startswith("-") or a == "-":
                return a
            i += 2 if (a in _INTERP_VALUE_OPTS["awk"] and "=" not in a) else 1
        return None
    if fam == "deno" and args[:1] == ["eval"]:
        return args[1] if len(args) > 1 else None
    flags = {"-e", "--eval", "-p", "--print"} if prog in ("bun", "osascript") else _INLINE_FLAGS.get(fam or "", set()) - {"-m", "--test", "--run"}
    for i, a in enumerate(args):
        name, eq, val = a.partition("=")
        if a in flags and i + 1 < len(args):
            return args[i + 1]
        if eq and name in flags and name.startswith("--"):
            return val
        if fam == "python" and re.fullmatch(r"-[A-Za-z]*c", a) and i + 1 < len(args):  # python -Ic CODE
            return args[i + 1]
    return None


_CODE_STRING = re.compile(r"""(['"`])((?:\\.|(?!\1)[^\\\n]){1,400})\1""")
# perl / ruby quote-like operators: q{…} qq(…) qx[…] qw<…> q/…/ (perl, optional space before the delimiter) and
# %q{…} %Q(…) %x[…] %w<…> %{…} (ruby): `do q{./x.pl}`, `load %q{x.rb}`, `qx{a | sh}` name files and commands too
_QUOTE_LIKE = re.compile(
    r"(?:(?<![\w$@%&>-])(?:qq|qx|qw|q)\s*|(?<![\w$@&])%[qQwWx]?(?=[{(\[<|!/]))"
    r"(?:\{([^{}\n]{1,400})\}|\(([^()\n]{1,400})\)|\[([^\[\]\n]{1,400})\]|<([^<>\n]{1,400})>|([/|!])((?:\\.|(?!\5)[^\\\n]){1,400})\5)"
)


def _code_literals(code: str) -> list[str]:
    """The string literals of a piece of code: quoted strings and perl / ruby quote-like operators."""
    out = [m.group(2) for m in _CODE_STRING.finditer(code or "")]
    for m in _QUOTE_LIKE.finditer(code or ""):
        out.append(next(g for g in (m.group(1), m.group(2), m.group(3), m.group(4), m.group(6)) if g is not None))
    return out
_INTERP_WORD = re.compile(r"^(?:(?:ba|z|da|k|mk|c|tc|fi)?sh|source|\.|node|python[\d.]*|perl|ruby|php|deno|bun|tsx|g?awk|osascript)$")
_CODE_WRITE = re.compile(r"(?i)\bopen\s*\(|write|rename|copy|move|symlink|unlink|chmod|truncate|\bfs\.|shutil|pathlib|os\.(?:system|replace)|File\.|IO\.|Deno\.|fopen|file_put_contents")


def _lang_by_name(path: Path) -> str:
    """How a script referenced by name from code would run: its shebang, else its extension."""
    try:
        with open(path, "rb") as fh:
            first = fh.readline(300).decode("utf-8", errors="replace")
    except OSError:
        first = ""
    if first.startswith("#!"):
        return _lang_of(path, first)
    return "code" if path.suffix.lower() in _CODE_EXT else "shell"


_BASE_IDENTICAL: dict[tuple[str, str, int, int], bool] = {}


def _base_identical(path: Path, ctx: "PolicyContext") -> bool:
    """Whether ``path`` (a regular file in the repo, not a symlink) is byte-identical to the same path in the run's base
    commit: a script a person reviewed and merged, not one the worker wrote or changed. The commit is a SHA the runner
    recorded at run start, so moving a branch cannot fake it; .autopilot/ (the run state) is always protected."""
    if not re.fullmatch(r"[0-9a-f]{7,64}", ctx.base_sha or ""):
        return False
    rel = rel_to_repo(str(path), ctx.repo)
    if not rel or rel.startswith(".."):
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    key = (ctx.base_sha, rel, st.st_mtime_ns, st.st_size)
    if key not in _BASE_IDENTICAL:
        try:
            r = subprocess.run(["git", "-C", str(ctx.repo), "cat-file", "blob", f"{ctx.base_sha}:{rel}"], capture_output=True, timeout=10)
            _BASE_IDENTICAL[key] = r.returncode == 0 and r.stdout == Path(path).read_bytes()
        except (OSError, subprocess.SubprocessError):
            _BASE_IDENTICAL[key] = False
    return _BASE_IDENTICAL[key]


def _referenced_scripts(code: str, cwds: list[Path], scan: "_Scan", ctx: "PolicyContext", *, broad: bool) -> list["_Ref"]:
    """Local scripts a piece of code names in its string literals (subprocess.run(['bash', 'scripts/x.sh']),
    execSync('bash scripts/x.sh'), system("./x.sh")): existing files the worker could have written, only. ``broad``
    (inline -c/-e code): any path-like word; otherwise (a code file, which names much else) only shell scripts and
    words that follow an interpreter name."""
    words: list[tuple[str, bool]] = []
    for lit in _code_literals(code):
        ws = lit.split()
        for j, w in enumerate(ws):
            words.append((w.strip(";,)("), j > 0 and bool(_INTERP_WORD.match(os.path.basename(ws[j - 1])))))
    refs: list[_Ref] = []
    seen: set[str] = set()
    for w, after_interp in words[:200]:
        if not w or w in seen or "://" in w or len(w) > 300:
            continue
        seen.add(w)
        suffix = Path(w).suffix.lower()
        if not (suffix in _SHELL_EXT or after_interp or (broad and ("/" in w or suffix in _CODE_EXT))):
            continue
        for p in _resolve_paths(w, cwds, scan, ctx.repo) or []:
            if p.is_file():
                r = _file_ref(p, _lang_by_name(p), ctx)
                if r:
                    refs.append(r)
        if len(refs) >= 20:
            break
    return refs


def _unresolvable(tok: str) -> bool:
    return bool(re.search(r"[$`]|\{\}", tok))


def _brace_expand(s: str, limit: int = 64) -> list[str]:
    """Bash brace expansion of one word ("scripts/{a,b}.sh", "x{1..3}"); ``${VAR}`` is left alone."""
    depth, start = 0, -1
    for i, ch in enumerate(s):
        if ch == "{" and (i == 0 or s[i - 1] != "$"):
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                body = s[start + 1:i]
                parts: list[str] = []
                d, cur = 0, ""
                for c in body:
                    if c == "," and d == 0:
                        parts.append(cur)
                        cur = ""
                        continue
                    d += (c == "{") - (c == "}")
                    cur += c
                parts.append(cur)
                if len(parts) == 1:
                    m = re.fullmatch(r"(-?\d+)\.\.(-?\d+)|([A-Za-z])\.\.([A-Za-z])", body)
                    if not m:
                        start = -1
                        continue
                    if m.group(1) is not None:
                        a, b = int(m.group(1)), int(m.group(2))
                        parts = [str(n) for n in (range(a, b + 1) if a <= b else range(a, b - 1, -1))][:limit]
                    else:
                        a, b = ord(m.group(3)), ord(m.group(4))
                        parts = [chr(n) for n in (range(a, b + 1) if a <= b else range(a, b - 1, -1))][:limit]
                out: list[str] = []
                for p in parts:
                    out += _brace_expand(s[:start] + p + s[i + 1:], limit)
                    if len(out) >= limit:
                        break
                return out[:limit]
    return [s]


_SCRIPT_DIR_IDIOMS = (
    re.compile(r"\$\(\s*cd\s+[\"']?\$\(\s*dirname\s+[\"']?\$\{?(?:0|BASH_SOURCE(?:\[0\])?)\}?[\"']?\s*\)[\"']?\s*(?:&&|;)\s*pwd(?:\s+-P)?\s*\)"),
    re.compile(r"\$\(\s*dirname\s+[\"']?\$\{?(?:0|BASH_SOURCE(?:\[0\])?)\}?[\"']?\s*\)"),
    re.compile(r"\$\{(?:0|BASH_SOURCE(?:\[0\])?)%/\*\}"),
)
_REPO_ROOT_IDIOM = re.compile(r"\$\(\s*git\s+rev-parse\s+--show-toplevel\s*\)")


def _subst_vars(tok: str, scan: _Scan, repo: Path) -> str:
    """Replace the variables a script path is usually built from; whatever is left unknown stays as ``$...``."""
    t = tok
    if scan.script is not None:
        for pat in _SCRIPT_DIR_IDIOMS:
            t = pat.sub(str(scan.script.parent), t)
    t = _REPO_ROOT_IDIOM.sub(str(repo), t)
    t = re.sub(r"\$\{?PWD\}?|\$\(\s*pwd\s*\)", str(scan.cwd), t)
    t = re.sub(r"\$\{?HOME\}?", _HOME, t)
    for _ in range(3):
        for name, val in scan.vars:
            t = re.sub(r"\$\{" + re.escape(name) + r"\}|\$" + re.escape(name) + r"\b", val.replace("\\", "\\\\"), t)
    return t


def _script_vars(content: str, script: Path, cwd: Path, repo: Path) -> tuple[tuple[str, str], ...]:
    """NAME=<dir> assignments of a script that resolve to a path (SCRIPT_DIR, ROOT, ...), for resolving later paths."""
    out: dict[str, str] = {}
    for m in re.finditer(r"(?m)^\s*(?:export\s+|readonly\s+|local\s+|declare\s+(?:-\w+\s+)*)?([A-Za-z_]\w*)=(.+?)\s*$", content):
        name, raw = m.group(1), m.group(2)
        up = len(re.findall(r"/\.\.(?=[/\"')\s]|$)", raw))
        if "dirname" in raw and ("$0" in raw or "BASH_SOURCE" in raw):
            val = str(script.parent)
            for _ in range(up):
                val = os.path.dirname(val)
        elif "rev-parse --show-toplevel" in raw:
            val = str(repo)
        else:
            val = _subst_vars(raw.strip().strip("\"'"), _Scan(cwd, script=script, vars=tuple(out.items())), repo)
            if _unresolvable(val) or re.search(r"[\s;&|<>()]", val):
                continue
        out[name] = val
    return tuple(out.items())


def _resolve_paths(tok: str, cwds: list[Path], scan: _Scan, repo: Path) -> list[Path] | None:
    """Candidate files for a script token (one per possible cwd and brace alternative), or None when the path cannot
    be known. Expands like the shell: braces ("{a,b}", "{1..3}"), "~+" ($PWD), "~" and globs; "~-" ($OLDPWD) is unknown."""
    t = _subst_vars(tok, scan, repo)
    if t == "~-" or t.startswith("~-/"):
        return None
    if t == "~+" or t.startswith("~+/"):
        t = str(scan.cwd) + t[2:]
    words = _brace_expand(t) if "{" in t else [t]
    out: list[Path] = []
    for w in words:
        if _unresolvable(w):
            guessed = _guess_root_paths(w, scan, repo)
            if guessed is None:
                return None
            out += guessed
            continue
        w = os.path.expanduser(w)
        bases = [Path(w)] if os.path.isabs(w) else [c / w for c in cwds]
        for b in bases:
            if any(ch in str(b) for ch in "*?["):
                out += [Path(p) for p in sorted(globmod.glob(str(b)))[:50]]
            else:
                out.append(Path(os.path.normpath(str(b))))
    return list(dict.fromkeys(out))


def _guess_root_paths(t: str, scan: _Scan, repo: Path) -> list[Path] | None:
    """``$ROOT/x/y.sh`` where ROOT comes from a function or command (``_PROJECT_ROOT=$(_find_project_root)``): try
    the repo root and the script's ancestor directories. None (cannot be known) unless one of them has the file."""
    m = re.fullmatch(r"(?:\$\{?[A-Za-z_]\w*\}?|\$\([^()]*\))(/[^$`]+)", t)
    if not m:
        return None
    rest = m.group(1).lstrip("/")
    roots = [repo]
    if scan.script is not None:
        d = scan.script.parent
        for _ in range(12):
            roots.append(d)
            if d == repo or d.parent == d:
                break
            d = d.parent
    roots.append(scan.cwd)
    found = [Path(os.path.normpath(str(r / rest))) for r in dict.fromkeys(roots) if (r / rest).exists()]
    return found or None


def _writable_caches() -> tuple[str, ...]:
    """The package caches the OS sandbox lets implement/review write (sessions._package_cache_dirs), as real paths."""
    from .sessions import _package_cache_dirs

    return tuple(os.path.realpath(p).rstrip("/") + "/" for p in _package_cache_dirs())


def _scannable(path: Path, ctx: PolicyContext, lang: str = "shell") -> tuple[bool, str]:
    """(is it ours to scan, display path). Ours = anything the worker could have written: the repo, a temp dir or a
    package cache the sandbox makes writable (~/.npm, the pnpm store, …), outside installed-package dirs — except a
    regular file that is a command's entry point there (node_modules/.bin/<name>, .venv/bin/<name>). Build output
    (dist/, build/) is read like any other file when a command runs it."""
    real = os.path.realpath(str(path))
    rel = rel_to_repo(real, ctx.repo)
    shown = rel if rel is not None else real
    if rel is None and not (real.startswith(_TMP_PREFIXES) or str(path).startswith(_TMP_PREFIXES) or (real + "/").startswith(_writable_caches())):
        return False, real
    parts = set(Path(shown).parts)
    if parts & _VENDOR_DIRS and not (_VENDOR_BIN.search(str(path)) and not os.path.islink(str(path)) and _VENDOR_BIN.search(real)):
        return False, shown
    return True, shown


def _read_text(path: Path) -> tuple[str | None, str | None]:
    """(content, None), (None, None) when there is nothing to read (missing / a directory), or (None, why not)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot be read ({exc.strerror or exc})"
    import stat as statmod

    if statmod.S_ISDIR(st.st_mode):
        return None, None
    if not statmod.S_ISREG(st.st_mode):
        return None, "is not a regular file, so its content cannot be checked"
    if st.st_size > SCRIPT_MAX_BYTES:
        return None, f"is larger than {SCRIPT_MAX_BYTES // 1024} KB, so its content cannot be checked"
    try:
        return Path(path).read_bytes().decode("utf-8", errors="replace"), None
    except OSError as exc:
        return None, f"cannot be read ({exc.strerror or exc})"


def _file_ref(path: Path, lang: str, ctx: PolicyContext) -> _Ref | None:
    ours, display = _scannable(path, ctx, lang)
    if not ours:
        return None
    if _VENDOR_BIN.search(str(path)):
        # an installed entry point (a pnpm / cmd-shim wrapper): its text is checked (a planted `git push --force`, a
        # named script), not its plumbing, which resolves the real target through variables and symlink loops
        lang = "code"
    return _Ref("file", path, lang=lang, name=display)


def _drop_comment_lines(code: str, path: Path) -> str:
    """Code without whole-line comments: they never run, and header comments often mention things like ``.env`` in
    usage notes. Only whole lines are dropped (``#`` lines for #-comment languages; ``//`` lines and ``/* … */``
    blocks that start and end a line for the C-like ones), so code sharing a line with a comment is kept."""
    first = code.split("\n", 1)[0]
    if path.suffix.lower() in (".py", ".rb", ".pl", ".pm", ".sh", ".bash") or re.match(r"#!.*\b(python|ruby|perl)", first):
        return re.sub(r"(?m)^[ \t]*#.*$", "", code)
    code = re.sub(r"(?m)^[ \t]*/\*(?:[^*]|\*(?!/))*\*/[ \t]*$", "", code)
    return re.sub(r"(?m)^[ \t]*//.*$", "", code)


def _lang_of(path: Path, content: str) -> str:
    """How a file runs when executed directly (./x): its shebang's interpreter, else as a shell script. Without a
    shebang execve fails with ENOEXEC and the shell runs the file itself, whatever its extension: `//; cmd` and a
    `/* … */` block in a .js file then run as shell commands, so they must be checked as such."""
    first = content.split("\n", 1)[0]
    if first.startswith("#!"):
        return "shell" if re.search(r"(^|[/\s])((ba|z|da|k|mk|c|tc)?sh|fish)(\s|$)", first[2:]) else "code"
    if "\x00" in content[:4096]:
        return "code"
    return "shell"


def _find_up(start: Path, name: str, stop: Path | None) -> Path | None:
    cur = Path(os.path.normpath(str(start)))
    stop_s = os.path.normpath(str(stop)) if stop else None
    for _ in range(40):
        cand = cur / name
        if cand.is_file():
            return cand
        if stop_s and os.path.normpath(str(cur)) == stop_s or cur.parent == cur:
            return None
        cur = cur.parent
    return None


def _scan_root(start: Path, ctx: PolicyContext) -> Path | None:
    """Where the upward search for package.json/Makefile stops: the repo root, or the temp dir root."""
    if rel_to_repo(str(start), ctx.repo) is not None:
        return Path(os.path.normpath(str(ctx.repo)))
    return None


_JSON_CACHE: dict[str, tuple[tuple[int, int], Any]] = {}


def _load_package(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """(package.json data, None) | (None, None) if missing/invalid JSON (the package manager fails anyway) | (None, why)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot be read ({exc.strerror or exc})"
    key = os.path.realpath(str(path))
    sig = (st.st_mtime_ns, st.st_size)
    hit = _JSON_CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1], None
    text, err = _read_text(path)
    if err:
        return None, err
    try:
        data = json.loads(text or "")
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    if len(_JSON_CACHE) > 512:
        _JSON_CACHE.clear()
    _JSON_CACHE[key] = (sig, data)
    return data, None


def _workspace_root(start: Path, ctx: PolicyContext) -> Path:
    stop = _scan_root(start, ctx)
    cur = Path(os.path.normpath(str(start)))
    for _ in range(40):
        if (cur / "pnpm-workspace.yaml").is_file():
            return cur
        data, _err = _load_package(cur / "package.json")
        if data and data.get("workspaces"):
            return cur
        if (stop and cur == stop) or cur.parent == cur:
            break
        cur = cur.parent
    return stop or Path(os.path.normpath(str(start)))


def _workspace_globs(root: Path) -> list[str]:
    globs: list[str] = []
    yml = root / "pnpm-workspace.yaml"
    if yml.is_file():
        try:
            text = yml.read_text(errors="replace")
        except OSError:
            text = ""
        in_pkgs = False
        for line in text.splitlines():
            body = line.split("#", 1)[0].rstrip()
            if not body.strip():
                continue
            if not line[:1].isspace() and not body.lstrip().startswith("-"):
                flow = re.match(r"^packages:\s*\[(.*)\]\s*$", body.strip())
                if flow:
                    globs += [g.strip().strip("'\"") for g in flow.group(1).split(",") if g.strip()]
                in_pkgs = body.strip() == "packages:"
                continue
            m = re.match(r"^\s*-\s*['\"]?([^'\"]+?)['\"]?\s*$", body)
            if in_pkgs and m:
                globs.append(m.group(1))
    data, _err = _load_package(root / "package.json")
    ws = (data or {}).get("workspaces")
    if isinstance(ws, dict):
        ws = ws.get("packages")
    if isinstance(ws, list):
        globs += [w for w in ws if isinstance(w, str)]
    return globs


def _workspace_packages(root: Path) -> list[Path]:
    """package.json files of the workspace projects under ``root`` (the root itself only if a pattern names it)."""
    found: list[Path] = []
    excluded: set[str] = set()
    globs = _workspace_globs(root)
    for g in globs:
        neg = g.startswith("!")
        pat = g.lstrip("!").rstrip("/")
        for p in globmod.glob(os.path.join(str(root), pat, "package.json"), recursive=True)[:2000]:
            if "node_modules" in Path(p).parts:
                continue
            (excluded.add(os.path.normpath(p)) if neg else found.append(Path(os.path.normpath(p))))
    return [p for p in dict.fromkeys(found) if str(p) not in excluded]


def _select_packages(pkgs: list[Path], selectors: list[str], cwd: Path, root: Path) -> list[Path]:
    """Packages a --filter/--workspace selector names; every package when a selector cannot be resolved cheaply."""
    chosen: list[Path] = []
    for sel in selectors:
        if sel.startswith("!"):
            continue  # exclusions only narrow the set; checking more packages is the safe side
        s = sel
        if "..." in s or "^" in s or s.startswith("[") or "[" in s:
            return pkgs  # dependency / changed-since selectors
        s = s.strip("{}")
        if s.startswith((".", "/")) or "/" in s and not s.startswith("@"):
            for base in (cwd, root):
                target = os.path.normpath(os.path.join(str(base), s))
                chosen += [p for p in pkgs if os.path.normpath(str(p.parent)) == target or str(p.parent).startswith(target + os.sep)]
            continue
        for p in pkgs:
            data, _err = _load_package(p)
            name = str((data or {}).get("name") or "")
            if name and (fnmatch.fnmatchcase(name, s) or name.split("/")[-1] == s):
                chosen.append(p)
    return list(dict.fromkeys(chosen)) or pkgs


@dataclass
class _PMCall:
    scripts: list[str] = field(default_factory=list)
    exec_cmd: str | None = None  # a command the package manager runs (npx x, pnpm exec x, pnpm <bin> fallback)
    fallback: bool = False  # exec_cmd runs only when no script named scripts[0] exists (pnpm/yarn/bun NAME)
    dirs: list[str] = field(default_factory=list)
    selectors: list[str] = field(default_factory=list)
    recursive: bool = False
    root: bool = False
    include_root: bool = False
    install: bool = False
    start_default: bool = False  # npm start without a start script runs `node server.js`


_PM_VALUES: dict[str, set[str]] = {
    "pnpm": {"-C", "--dir", "--filter", "-F", "--filter-prod", "--reporter", "--loglevel", "--workspace-concurrency", "--resume-from", "--test-pattern", "--changed-files-ignore-pattern", "--store-dir", "--virtual-store-dir", "--modules-dir", "--lockfile-dir", "--cache-dir", "--registry", "--package", "--script-shell", "--node-options"},
    "npm": {"--prefix", "-C", "-w", "--workspace", "--loglevel", "--registry", "--cache", "--userconfig", "--globalconfig", "--script-shell", "--node-options", "-p", "--package", "-c", "--call"},
    "yarn": {"--cwd", "--modules-folder", "--cache-folder", "--registry", "--mutex", "--network-timeout", "-p", "--package", "--from", "--include", "--exclude", "-j", "--jobs", "--since"},
    "bun": {"--cwd", "--filter", "-F", "--env-file", "-r", "--preload", "-c", "--config", "--tsconfig-override", "-d", "--define", "-l", "--loader", "-p", "--package"},
    "turbo": {"--filter", "-F", "--concurrency", "--cache-dir", "--log-order", "--output-logs", "--env-mode", "--summarize", "--log-prefix", "--ui", "--remote-cache-timeout", "--profile", "--global-deps", "--team", "--token", "--api", "--login", "--heap", "--trace", "--cpuprofile", "--anon-profile", "--cache", "--graph", "--framework-inference", "--continue", "--affected-base"},
}
_PNPM_INSTALL = {"install", "i", "add", "update", "up", "upgrade", "rebuild", "rb", "remove", "rm", "uninstall", "un", "link", "ln", "unlink", "import", "install-test", "it", "dedupe", "patch-commit", "approve-builds"}
_PNPM_BUILTIN = {
    "audit", "list", "ls", "ll", "la", "outdated", "why", "licenses", "publish", "pack", "root", "bin", "store", "env", "setup", "config", "c",
    "get", "set", "server", "deploy", "doctor", "self-update", "init", "help", "cat-file", "cat-index", "find-hash", "catalog", "sbom", "view",
    "info", "login", "logout", "whoami", "dist-tag", "owner", "team", "token", "access", "search", "docs", "home", "repo", "bugs", "version",
    "completion", "fetch", "prune", "patch", "patch-remove", "ignored-builds", "create", "unpublish", "deprecate",
}
_NPM_INSTALL = {"install", "i", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal", "isntall", "add", "ci", "clean-install", "install-clean", "ic", "update", "up", "upgrade", "rebuild", "rb", "install-test", "it", "install-ci-test", "cit", "uninstall", "remove", "rm", "r", "un", "unlink", "link", "ln", "dedupe", "ddp", "prune"}
_YARN_INSTALL = {"install", "add", "remove", "up", "upgrade", "upgrade-interactive", "dedupe", "import", "link", "unlink", "rebuild"}
_YARN_BUILTIN = {
    "info", "list", "why", "config", "cache", "init", "global", "npm", "pack", "publish", "version", "set", "plugin", "constraints", "explain", "bin",
    "licenses", "outdated", "owner", "team", "tag", "login", "logout", "policies", "audit", "check", "create", "generate-lock-entry", "autoclean",
    "help", "unplug", "patch", "patch-commit", "stage", "search", "sdks", "version",
}
_BUN_BUILTIN = {"test", "repl", "install", "i", "add", "a", "remove", "rm", "update", "link", "unlink", "pm", "outdated", "publish", "patch", "patch-commit", "why", "init", "create", "c", "upgrade", "build", "audit", "info", "x"}
_TURBO_BUILTIN = {"prune", "gen", "generate", "login", "logout", "link", "unlink", "daemon", "info", "ls", "query", "telemetry", "scan", "boundaries", "completion", "bin", "devtools", "docs", "config"}


def _take_opts(args: list[str], values: set[str]) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Leading options (with their values) and the args from the first non-option word on."""
    opts: list[tuple[str, str | None]] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return opts, args[i + 1:]
        if not a.startswith("-") or a == "-":
            break
        if a.startswith("--") and "=" in a:
            k, v = a.split("=", 1)
            opts.append((k, v))
        elif a in values and i + 1 < len(args):
            opts.append((a, args[i + 1]))
            i += 1
        elif not a.startswith("--") and len(a) > 2 and a[:2] in values:
            opts.append((a[:2], a[2:]))
        else:
            opts.append((a, None))
        i += 1
    return opts, args[i:]


def _all_opts(args: list[str], values: set[str]) -> list[tuple[str, str | None]]:
    """Every option before ``--``, wherever it appears (with its value)."""
    opts: list[tuple[str, str | None]] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a.startswith("-") and a != "-":
            o, _rest = _take_opts(args[i:i + 2], values)
            if o:
                opts.append(o[0])
                i += 2 if o[0][1] is not None and "=" not in a and a in values else 1
                continue
        i += 1
    return opts


def _words(args: list[str], values: set[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a.startswith("-"):
            i += 2 if (a in values and "=" not in a) else 1
            continue
        out.append(a)
        i += 1
    return out


def _apply_pm_opts(call: _PMCall, opts: list[tuple[str, str | None]], pm: str) -> None:
    for k, v in opts:
        if k in ("-C", "--dir", "--prefix", "--cwd") and v:
            call.dirs.append(v)
        elif k in ("--filter", "-F", "--filter-prod") and v:
            call.selectors.append(v)
        elif k in ("-w", "--workspace") and pm == "npm" and v:
            call.selectors.append(v)
        elif k in ("-r", "--recursive", "--workspaces", "--ws"):
            call.recursive = True
        elif k in ("-w", "--workspace-root") and pm == "pnpm":
            call.root = True
        elif k == "--include-workspace-root":
            call.include_root = True
        elif k == "--ignore-scripts":
            call.install = False


def _shell_or_join(rest: list[str], shell_mode: bool) -> str | None:
    if not rest:
        return None
    return " ".join(rest) if shell_mode else " ".join(shlex.quote(t) for t in rest)


def _pm_call(toks: list[str]) -> _PMCall | None:
    """How a package-manager / task-runner command line runs scripts, or None."""
    if not toks:
        return None
    prog = os.path.basename(toks[0])
    args = toks[1:]
    if prog == "corepack" and args[:1] and args[0] in ("pnpm", "npm", "npx", "yarn", "pnpx", "yarnpkg"):
        prog, args = args[0], args[1:]
    if prog in ("node", "nodejs") and "--run" in args:
        i = args.index("--run")
        return _PMCall(scripts=[args[i + 1]]) if i + 1 < len(args) else None
    c = _PMCall()
    if prog == "pnpm":
        opts, rest = _take_opts(args, _PM_VALUES["pnpm"])
        _apply_pm_opts(c, opts, "pnpm")
        if not rest:
            return c
        cmd, tail = rest[0], rest[1:]
        if cmd in ("recursive", "multi", "m"):
            # `pnpm recursive run x` == `pnpm -r run x`; bare `pnpm m` installs every workspace package
            inner = (_pm_call(["pnpm"] + tail) if tail else None) or _PMCall(install=not tail)
            inner.recursive = True
            inner.selectors = c.selectors + inner.selectors
            inner.dirs = c.dirs + inner.dirs
            inner.root = inner.root or c.root
            return inner
        if cmd in ("run", "run-script"):
            o2, r2 = _take_opts(tail, _PM_VALUES["pnpm"])
            _apply_pm_opts(c, o2, "pnpm")
            c.scripts = r2[:1]
        elif cmd in ("test", "t", "tst"):
            c.scripts = ["test"]
        elif cmd in ("start", "stop", "restart"):
            c.scripts = ["restart", "stop", "start"] if cmd == "restart" else [cmd]
            c.start_default = cmd == "start"
        elif cmd in ("exec", "dlx"):
            o2, r2 = _take_opts(tail, _PM_VALUES["pnpm"] | {"--package"})
            _apply_pm_opts(c, [o for o in o2 if o[0] not in ("-c",)], "pnpm")
            c.exec_cmd = _shell_or_join(r2, any(k in ("-c", "--shell-mode") for k, _ in o2))
        elif cmd in _PNPM_INSTALL:
            c.install = not any(k == "--ignore-scripts" for k, _ in opts) and "--ignore-scripts" not in tail
            c.scripts = ["test"] if cmd in ("install-test", "it") else []
        elif cmd not in _PNPM_BUILTIN:
            c.scripts, c.exec_cmd, c.fallback = [cmd], _shell_or_join(rest, False), True
        return c
    if prog == "npm":
        opts, rest = _take_opts(args, _PM_VALUES["npm"])
        _apply_pm_opts(c, _all_opts(args, _PM_VALUES["npm"]), "npm")  # npm reads its flags anywhere before `--`
        if not rest:
            return c
        cmd, tail = rest[0], rest[1:]
        if cmd in ("run", "run-script", "rum", "urn"):
            c.scripts = _take_opts(tail, _PM_VALUES["npm"])[1][:1]
        elif cmd in ("test", "t", "tst"):
            c.scripts = ["test"]
        elif cmd in ("start", "stop", "restart"):
            c.scripts = ["restart", "stop", "start"] if cmd == "restart" else [cmd]
            c.start_default = cmd == "start"
        elif cmd in ("exec", "x"):
            o2, r2 = _take_opts(tail, _PM_VALUES["npm"])
            call = next((v for k, v in o2 if k in ("-c", "--call")), None)
            c.exec_cmd = call if call else _shell_or_join(r2, False)
        elif cmd == "explore":
            # `npm explore <pkg> -- <cmd>` runs <cmd> through a shell inside node_modules/<pkg>; the outer shell has
            # already expanded $PWD & co. to the caller's directory
            c.exec_cmd = _shell_or_join(tail[tail.index("--") + 1:] if "--" in tail else tail[1:], True)
        elif cmd in _NPM_INSTALL:
            c.install = "--ignore-scripts" not in args
            c.scripts = ["test"] if cmd in ("install-test", "it", "install-ci-test", "cit") else []
        return c
    if prog in ("npx", "pnpx", "bunx"):
        opts, rest = _take_opts(args, _PM_VALUES["npm"] | {"--bun"})
        call = next((v for k, v in opts if k in ("-c", "--call")), None)
        c.exec_cmd = call if call else _shell_or_join(rest, False)
        return c
    if prog in ("yarn", "yarnpkg"):
        opts, rest = _take_opts(args, _PM_VALUES["yarn"])
        _apply_pm_opts(c, opts, "yarn")
        if not rest:
            c.install = True
            return c
        cmd, tail = rest[0], rest[1:]
        if cmd == "workspace" and len(tail) >= 2:
            inner = _pm_call(["yarn"] + tail[1:]) or _PMCall()
            inner.selectors.append(tail[0])
            inner.dirs = c.dirs + inner.dirs
            return inner
        if cmd == "workspaces":
            sub = _words(tail, _PM_VALUES["yarn"])
            if sub[:1] == ["foreach"]:
                sub = sub[1:]
            if sub[:1] == ["run"] and len(sub) > 1:
                c.scripts, c.recursive = [sub[1]], True
            elif sub[:1] == ["exec"] and len(sub) > 1:
                c.exec_cmd = _shell_or_join(sub[1:], False)
            return c
        if cmd == "run":
            r2 = _take_opts(tail, _PM_VALUES["yarn"])[1]
            c.scripts, c.exec_cmd, c.fallback = r2[:1], _shell_or_join(r2, False), True
        elif cmd in ("test", "start"):
            c.scripts = [cmd]
        elif cmd in ("exec", "dlx", "node"):
            r2 = _take_opts(tail, _PM_VALUES["yarn"])[1]
            c.exec_cmd = _shell_or_join(["node"] + r2 if cmd == "node" else r2, False)
        elif cmd in _YARN_INSTALL:
            c.install = "--ignore-scripts" not in args
        elif cmd not in _YARN_BUILTIN:
            c.scripts, c.exec_cmd, c.fallback = [cmd], _shell_or_join(rest, False), True
        return c
    if prog == "bun":
        opts, rest = _take_opts(args, _PM_VALUES["bun"])
        _apply_pm_opts(c, opts, "bun")
        if not rest or any(k in ("-e", "--eval", "-p", "--print") for k, _ in opts):
            return c
        cmd = rest[0]
        if cmd == "run":
            o2, r2 = _take_opts(rest[1:], _PM_VALUES["bun"])
            _apply_pm_opts(c, o2, "bun")
            target, more = (r2[0], r2[1:]) if r2 else ("", [])
        elif cmd == "x":
            c.exec_cmd = _shell_or_join(rest[1:], False)
            return c
        elif cmd in ("install", "i", "add", "a", "remove", "rm", "update", "link"):
            c.install = "--ignore-scripts" not in args
            return c
        elif cmd in _BUN_BUILTIN:
            return c
        else:
            target, more = cmd, rest[1:]
        if target and not ("/" in target or Path(target).suffix.lower() in _CODE_EXT | _SHELL_EXT):
            c.scripts, c.exec_cmd, c.fallback = [target], _shell_or_join([target] + more, False), True
        return c  # a file target is handled by _interpreter_target
    if prog == "turbo":
        opts, rest = _take_opts(args, _PM_VALUES["turbo"])
        if not rest:
            return None
        tasks = _words(rest[1:], _PM_VALUES["turbo"]) if rest[0] in ("run", "watch") else ([] if rest[0] in _TURBO_BUILTIN else _words(rest, _PM_VALUES["turbo"]))
        sels = [v for k, v in opts if k in ("--filter", "-F") and v]
        i = 0
        while i < len(rest):
            a = rest[i]
            if a.startswith("--filter=") or (a.startswith("-F") and len(a) > 2):
                sels.append(a.split("=", 1)[1] if "=" in a else a[2:])
            elif a in ("--filter", "-F") and i + 1 < len(rest):
                sels.append(rest[i + 1])
                i += 1
            i += 1
        c.recursive, c.selectors = True, sels
        for t in tasks:
            pkg, _, task = t.rpartition("#")
            c.scripts.append(task)
            if pkg == "//":
                c.include_root = True
            elif pkg:
                c.selectors.append(pkg)
        return c if c.scripts else None
    if prog == "lerna":
        words = _words(args, {"--scope", "--ignore", "--concurrency"})
        if words[:1] == ["run"] and len(words) > 1:
            return _PMCall(scripts=[words[1]], recursive=True)
    if prog in ("run-s", "run-p", "npm-run-all", "npm-run-all2"):
        # package scripts by name or glob ("build:*"); arguments after "--" go to the scripts
        words = _words(args, {"--max-parallel", "--npm-path"})
        return _PMCall(scripts=words) if words else None
    if prog == "nx":
        return _nx_call(args)
    return None


_NX_BUILTIN = frozenset({"graph", "show", "list", "add", "generate", "g", "migrate", "report", "reset", "daemon", "init", "connect", "view-logs", "release", "format", "format:check", "format:write", "exec", "watch", "sync", "sync:check", "repair", "import", "login", "logout", "mcp", "configure-ai-agents"})
_NX_VALUES = {"-t", "--target", "--targets", "-p", "--projects", "-c", "--configuration", "--parallel", "--exclude", "--base", "--head", "--output-style", "--files"}


def _nx_call(args: list[str]) -> _PMCall | None:
    """nx targets as package scripts (nx infers a project's targets from its package.json scripts): `nx run a:x`,
    `nx x a`, `nx run-many -t x [-p a,b]`, `nx affected -t x`. Targets defined only in project.json are not followed."""
    opts = _all_opts(args, _NX_VALUES)
    words = _words(args, _NX_VALUES)
    targets = [t for k, v in opts if k in ("-t", "--target", "--targets") and v for t in v.split(",") if t]
    projects = [x for k, v in opts if k in ("-p", "--projects") and v for x in v.split(",") if x]
    if not words:
        return None
    if words[0] in ("run-many", "affected"):
        return _PMCall(scripts=targets, recursive=True, selectors=projects) if targets else None
    if words[0] == "run" and len(words) > 1:
        proj, _, rest = words[1].partition(":")
        return _PMCall(scripts=[rest.split(":")[0]], selectors=[proj]) if rest else _PMCall(scripts=[proj], recursive=True)
    if words[0] in _NX_BUILTIN:
        return None
    if ":" in words[0]:
        proj, _, rest = words[0].partition(":")
        return _PMCall(scripts=[rest.split(":")[0]], selectors=[proj])
    return _PMCall(scripts=[words[0]], selectors=words[1:2], recursive=not words[1:2])


def _runner_command(toks: list[str]) -> tuple[str, str | None] | None:
    """(command, cwd override) that a Python runner executes: uv run / poetry run / pipenv run / pdm run."""
    if len(toks) < 2:
        return None
    prog = os.path.basename(toks[0])
    if prog not in ("uv", "poetry", "pipenv", "pdm", "hatch", "rye") or toks[1] != "run":
        return None
    vals = {"--directory", "--project", "--with", "--with-requirements", "--python", "-p", "--env-file", "--extra", "--group", "--package", "--index", "--index-url", "--extra-index-url", "--find-links", "--refresh-package", "--reinstall-package", "--no-binary-package", "--only-binary-package", "--config-setting", "-C", "--prerelease", "--resolution", "--link-mode", "--exclude-newer", "--cache-dir", "--config-file", "-e", "--env"}
    opts, rest = _take_opts(toks[2:], vals)
    cwd = next((v for k, v in opts if k in ("--directory",) and v), None)
    if any(k == "--script" for k, _ in opts) and rest:
        rest = ["python"] + rest
    if rest and Path(rest[0]).suffix == ".py":
        rest = ["python"] + rest
    return (" ".join(shlex.quote(t) for t in rest), cwd) if rest else None


def _lifecycle_on_base(pj: Path, name: str, ctx: PolicyContext) -> str | None:
    """The text of a lifecycle script on the base branch (committed tooling is trusted), or None if unknown."""
    rel = rel_to_repo(str(pj), ctx.repo)
    if rel is None:
        return None
    data = _base_package(rel, ctx)
    scripts = (data or {}).get("scripts")
    return scripts.get(name) if isinstance(scripts, dict) and isinstance(scripts.get(name), str) else None


_BASE_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any] | None]] = {}


def _base_package(rel: str, ctx: PolicyContext) -> dict[str, Any] | None:
    import subprocess
    import time

    key = (str(ctx.repo), ctx.base_branch, rel)
    hit = _BASE_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    data: dict[str, Any] | None = None
    for ref in (ctx.base_branch, f"origin/{ctx.base_branch}"):
        try:
            out = subprocess.run(["git", "-C", str(ctx.repo), "show", f"{ref}:{rel}"], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            break
        if out.returncode == 0:
            try:
                parsed = json.loads(out.stdout.decode("utf-8", errors="replace"))
                data = parsed if isinstance(parsed, dict) else None
            except ValueError:
                data = None
            break
    _BASE_CACHE[key] = (time.monotonic(), data)
    return data


def _pm_refs(call: _PMCall, cwds: list[Path], ctx: PolicyContext) -> tuple[list[_Ref], list[tuple[str, Path]]]:
    """Package scripts a package-manager call runs (as refs) and the commands it executes directly (with their cwd)."""
    refs: list[_Ref] = []
    nested: list[tuple[str, Path]] = []
    for base in cwds:
        starts = [Path(os.path.normpath(os.path.join(str(base), os.path.expanduser(d)))) for d in call.dirs] or [base]
        for start in starts:
            pj = _find_up(start, "package.json", _scan_root(start, ctx))
            ours = pj is not None and _scannable(pj, ctx)[0]
            if not ours or pj is None:
                if call.exec_cmd:
                    nested.append((call.exec_cmd, start))
                continue
            needs_pkgs = bool(call.scripts or call.install or call.start_default)
            if not needs_pkgs and not (call.recursive or call.selectors):
                if call.exec_cmd:
                    nested.append((call.exec_cmd, start))
                continue
            chosen: list[Path]
            if call.recursive or call.selectors:
                wroot = _workspace_root(pj.parent, ctx)
                pkgs = _workspace_packages(wroot)
                root_pj = wroot / "package.json"
                chosen = _select_packages(pkgs, call.selectors, start, wroot) if call.selectors else list(pkgs)
                if call.include_root or call.root:
                    chosen.append(root_pj)
                if not chosen:
                    chosen = [root_pj]
            elif call.root:
                chosen = [_workspace_root(pj.parent, ctx) / "package.json"]
            else:
                chosen = [pj]
            if call.install:
                wroot = _workspace_root(pj.parent, ctx)
                chosen = list(dict.fromkeys(chosen + [wroot / "package.json"] + _workspace_packages(wroot)))
            found_main = False
            for p in (dict.fromkeys(chosen) if needs_pkgs else ()):
                data, err = _load_package(p)
                display = _scannable(p, ctx)[1]
                if err:
                    refs.append(_Ref("deny", p, name=display, reason=err))
                    continue
                scripts = (data or {}).get("scripts")
                scripts = scripts if isinstance(scripts, dict) else {}
                names: list[str] = []
                for s in call.scripts:
                    # npm-run-all globs ("build:*", "dir*") name every matching script
                    for nm in ([k for k in scripts if fnmatch.fnmatchcase(k, s)] if any(ch in s for ch in "*?[") else [s]):
                        names += [f"pre{nm}", nm, f"post{nm}"]
                if call.scripts and isinstance(scripts.get(call.scripts[0]), str):
                    found_main = True
                for nm in dict.fromkeys(names):
                    if isinstance(scripts.get(nm), str):
                        refs.append(_Ref("pkg", p, name=nm, cwd=p.parent, text=scripts[nm]))
                if call.start_default and not isinstance(scripts.get("start"), str):
                    r = _file_ref(p.parent / "server.js", "code", ctx)
                    if r:
                        refs.append(r)
                if call.install:
                    for nm in _LIFECYCLE:
                        text = scripts.get(nm)
                        if isinstance(text, str) and text.strip() and _lifecycle_on_base(p, nm, ctx) != text:
                            refs.append(_Ref("pkg", p, name=nm, cwd=p.parent, text=text))
            if call.exec_cmd and (not call.fallback or not found_main):
                where = [p.parent for p in chosen] if (call.recursive or call.selectors) else [start]
                nested += [(call.exec_cmd, w) for w in dict.fromkeys(where)]
    return refs, nested


def _make_refs(toks: list[str], cwds: list[Path], ctx: PolicyContext) -> list[_Ref]:
    """Recipes `make [targets]` would run (best effort: simple variables, explicit rules, prerequisites)."""
    vals = {"-C", "--directory", "-f", "--file", "--makefile", "-I", "--include-dir", "-o", "--old-file", "-W", "--what-if", "--new-file", "--assume-new", "--assume-old", "-l", "--load-average"}
    opts, rest = _take_opts(toks[1:], vals)
    i = 0
    targets: list[str] = []
    while i < len(rest):
        a = rest[i]
        if a.startswith("-"):
            k = a.split("=", 1)[0]
            if k in ("-C", "--directory", "-f", "--file", "--makefile") and "=" not in a and i + 1 < len(rest):
                opts.append((k, rest[i + 1]))
                i += 2
                continue
            opts.append((k, a.split("=", 1)[1] if "=" in a else None))
        elif "=" not in a:
            targets.append(a)
        i += 1
    refs: list[_Ref] = []
    for base in cwds:
        d = base
        for k, v in opts:
            if k in ("-C", "--directory") and v:
                d = Path(os.path.normpath(os.path.join(str(d), os.path.expanduser(v))))
        files = [v for k, v in opts if k in ("-f", "--file", "--makefile") and v]
        try:
            present = set(os.listdir(d))
        except OSError:
            present = set()
        # exact names first: on a case-insensitive filesystem "makefile" would also open "Makefile"
        candidates = [Path(os.path.normpath(os.path.join(str(d), f))) for f in files] or [d / n for n in ("GNUmakefile", "makefile", "Makefile") if n in present]
        mk = next((c for c in candidates if c.is_file()), None)
        if mk is None:
            continue
        ours, display = _scannable(mk, ctx)
        if not ours:
            continue
        text, err = _read_text(mk)
        if not err:
            text, err = _make_with_includes(text or "", d, ctx)
        if err:
            refs.append(_Ref("deny", mk, name=display, reason=err))
            continue
        for tgt, recipe in _make_recipes(text or "", targets, d):
            refs.append(_Ref("make", mk, name=tgt, cwd=d, text=recipe))
    return refs


_MAKE_INCLUDE = re.compile(r"^[ \t]*(?:-include|sinclude|include)[ \t]+(?P<files>[^\n#]+)", re.M)


def _make_with_includes(text: str, cwd: Path, ctx: PolicyContext, depth: int = 0) -> tuple[str, str | None]:
    """The makefile with each `include` / `-include` / `sinclude` line replaced by the files it names (relative to
    where make runs, globs allowed, nested up to 3 deep), so their rules and recipes are checked too."""
    if depth >= 3 or not _MAKE_INCLUDE.search(text):
        return text, None
    out: list[str] = []
    for line in text.split("\n"):
        m = _MAKE_INCLUDE.match(line)
        if not m:
            out.append(line)
            continue
        for word in m.group("files").split():
            if "$" in word:
                continue  # a variable: make would expand it; an unknown include stays unread
            cand = cwd / os.path.expanduser(word)
            for f in sorted(globmod.glob(str(cand)))[:20] if any(ch in word for ch in "*?[") else [str(cand)]:
                path = Path(f)
                if not path.is_file():
                    continue
                ours, display = _scannable(path, ctx)
                if not ours:
                    continue
                inc, err = _read_text(path)
                if err:
                    return text, f"included makefile {display}: {err}"
                inc, err = _make_with_includes(inc or "", cwd, ctx, depth + 1)
                if err:
                    return text, err
                out.append(inc)
    return "\n".join(out), None


def _make_shell_calls(line: str) -> list[str]:
    """Bodies of the `$(shell …)` / `${shell …}` functions in a makefile line (parentheses balanced)."""
    out: list[str] = []
    for m in re.finditer(r"\$([({])shell\s", line):
        open_c, close_c = m.group(1), ")" if m.group(1) == "(" else "}"
        depth, i = 1, m.end()
        while i < len(line) and depth:
            depth += (line[i] == open_c) - (line[i] == close_c)
            i += 1
        out.append(line[m.end(): i - 1 if depth == 0 else i].strip())
    return out


def _make_recipes(text: str, targets: list[str], cwd: Path) -> list[tuple[str, str]]:
    """What `make [targets]` runs: the recipes of the goals and their prerequisites (explicit rules first, then the
    matching pattern rules, then .DEFAULT), plus every `$(shell …)` (make runs those while reading the makefile)."""
    text = re.sub(r"\\\r?\n[ \t]*", " ", text)
    variables: dict[str, str] = {"MAKE": "make", "CURDIR": str(cwd)}
    rules: dict[str, tuple[list[str], list[str]]] = {}
    shell_calls: list[str] = []
    order: list[str] = []
    default = ""
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("\t"):
            for t in current:
                rules[t][1].append(line[1:])
            continue
        body = line.split("#", 1)[0].rstrip()
        if not body.strip():
            continue
        shell_calls += _make_shell_calls(body)
        m = re.match(r"^(?:export\s+|override\s+)?([A-Za-z_.][\w.-]*)\s*(::?=|:::=|\?=|\+=|!=|=)\s*(.*)$", body)
        if m:
            name, op, val = m.groups()
            if op == "!=":
                shell_calls.append(val)
            if name == ".DEFAULT_GOAL":
                default = val.strip()
            variables[name] = (variables.get(name, "") + " " + val).strip() if op == "+=" else val
            current = []
            continue
        m = re.match(r"^([^:=]+?)\s*::?(?!=)\s*(.*)$", body)
        if m and not body.lstrip().startswith(("include", "-include", "sinclude", "ifeq", "ifneq", "ifdef", "ifndef", "else", "endif", "define", "endef")):
            names = m.group(1).split()
            prereq, _, inline = m.group(2).partition(";")
            current = names
            for n in current:
                rules.setdefault(n, ([], []))
                rules[n][0].extend(prereq.split("|")[0].split())
                if inline.strip():
                    rules[n][1].append(inline.strip())
                if n not in order and "%" not in n:
                    order.append(n)
            continue
        current = []

    def expand(s: str) -> str:
        for _ in range(5):
            new = re.sub(r"\$\(([A-Za-z_][\w.-]*)\)|\$\{([A-Za-z_][\w.-]*)\}", lambda mm: variables.get(mm.group(1) or mm.group(2), mm.group(0)), s)
            if new == s:
                break
            s = new
        return s.replace("$$", "$")

    patterns = [(n, re.compile(re.escape(n).replace("%", "(.+)", 1))) for n in rules if "%" in n]

    def rule_for(t: str) -> list[tuple[list[str], list[str], str]]:
        """(prerequisites, recipe, stem) of every rule that can build ``t`` (fail closed: all matching patterns)."""
        if t in rules and (rules[t][1] or not patterns):
            return [(rules[t][0], rules[t][1], "")]
        found = [(rules[n][0], rules[n][1], m.group(1)) for n, rx in patterns if (m := rx.fullmatch(t)) and rules[n][1]]
        if t in rules:
            found.insert(0, (rules[t][0], rules[t][1], ""))
        if not found and ".DEFAULT" in rules:
            found = [([], rules[".DEFAULT"][1], "")]
        return found

    goals = targets or ([default] if default else [t for t in order if not t.startswith(".")][:1])
    out: list[tuple[str, str]] = [("(shell function)", expand(v)) for v in shell_calls]
    seen: set[str] = set()
    queue = [expand(g) for g in goals]
    while queue and len(seen) < 50:
        t = queue.pop(0)
        if t in seen:
            continue
        seen.add(t)
        for prereqs, recipe, stem in rule_for(t):
            pre = [expand(p).replace("%", stem) if stem else expand(p) for p in prereqs]
            queue += pre
            for line in recipe:
                line = line.replace("$@", t).replace("$*", stem).replace("$<", pre[0] if pre else "")
                cmd = expand(line).lstrip("@-+ \t")
                if cmd.strip():
                    out.append((t, cmd))
    return out


def _content_writes(toks: list[str], seg: str, cwds: list[Path], scan: _Scan, repo: Path) -> tuple[set[str], bool]:
    """Paths whose content this simple command (re)writes, as real paths; dirs mean "anything under it".
    The flag is True when a target cannot be resolved (a variable)."""
    raw: list[str] = []
    dirs: list[str] = []
    writes, _deletes = _write_targets(toks, seg)
    prog = os.path.basename(toks[0]) if toks else ""
    if prog in ("touch", "mkdir", "chmod", "chown", "truncate"):
        writes = _write_targets([], seg)[0]
    writes = [w.strip("\"'") for w in writes]
    raw += [w for w in writes if w and w not in ("<<HEREDOC", ".") and not w.startswith("&") and not _DEV_STREAM.match(w)]
    args = toks[1:]
    if prog in ("curl", "wget"):
        for i, a in enumerate(args):
            if a in ("-o", "--output", "--output-document") and i + 1 < len(args):
                raw.append(args[i + 1])
            elif re.match(r"^--output(-document)?=", a):
                raw.append(a.split("=", 1)[1])
            elif prog == "curl" and re.match(r"^-[a-zA-Z]*o.", a) and not a.startswith("--"):
                raw.append(a[a.index("o") + 1:])
            elif prog == "wget" and a.startswith("-O") and len(a) > 2:
                raw.append(a[2:])
            elif prog == "wget" and a == "-O" and i + 1 < len(args):
                raw.append(args[i + 1])
            elif (prog == "curl" and (a in ("-O", "--remote-name") or re.match(r"^-[a-zA-Z]*O[a-zA-Z]*$", a))) or (prog == "wget" and not any(x.startswith("-O") for x in args)):
                urls = [u for u in args if re.match(r"^[a-z]+://", u)]
                raw += [os.path.basename(u.split("?", 1)[0]) or "index.html" for u in urls]
            elif prog == "wget" and a in ("-P", "--directory-prefix") and i + 1 < len(args):
                dirs.append(args[i + 1])
    elif prog in ("tar", "bsdtar", "gtar") and any(re.match(r"^-?[a-zA-Z]*x", a) or a in ("--extract", "--get") for a in args[:2]):
        dirs.append(next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-C", "--directory")), "."))
    elif prog in ("unzip", "7z", "7za", "cpio", "ditto"):
        dirs.append(next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-d",)), "."))
    elif prog == "git":
        sub, gargs, _ = _git_sub(toks)
        pos = [a for a in gargs if not a.startswith("-")]
        switching = sub in ("checkout", "switch") and "--" not in gargs and not any(a in ("-b", "-B", "-c", "-C", "--orphan") for a in gargs) and len(pos) == 1
        if sub in ("pull", "merge", "rebase", "reset", "cherry-pick", "revert", "am", "apply") or switching or (sub == "stash" and pos[:1] and pos[0] in ("pop", "apply")):
            dirs.append(str(repo))
    unresolved = False
    # the files a patch changes are named inside the diff; an interpreter's inline code may write anywhere
    if prog in ("patch", "gpatch"):
        unresolved = True
    code = _inline_code(toks)
    if code is not None and _CODE_WRITE.search(code):
        unresolved = True
    out: set[str] = set()
    for t, is_dir in [(w, False) for w in raw] + [(d, True) for d in dirs]:
        paths = _resolve_paths(t, cwds, scan, repo)
        if paths is None:
            unresolved = True
            continue
        # a copy / move / link / checkout into a directory writes files under it ("cp x.sh scripts/")
        out |= {os.path.normpath(os.path.realpath(str(p))) + ("/" if is_dir or t.endswith("/") or os.path.isdir(p) else "") for p in paths}
    return out, unresolved


def _overlaps(path: Path, written: frozenset[str] | set[str]) -> bool:
    p = os.path.normpath(os.path.realpath(str(path)))
    for w in written:
        if w.endswith("/"):
            if p == w.rstrip("/") or p.startswith(w):
                return True
        elif p == w:
            return True
    return False


def _env_assignments(seg: str) -> list[tuple[str, str]]:
    """NAME=value pairs a simple command sets for itself (leading assignments, env, export/declare -x)."""
    toks = _shlex(seg)
    out: list[tuple[str, str]] = []
    i = 0
    if toks[:1] and toks[0] in ("export", "declare", "typeset", "local", "readonly"):
        i = 1
        while i < len(toks) and toks[i].startswith("-"):
            i += 1
    while i < len(toks):
        t = toks[i]
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", t)
        if m:
            out.append((m.group(1), m.group(2)))
        elif t in ("-u", "--unset", "-C", "--chdir"):
            i += 1
        elif not (os.path.basename(t) == "env" or t.startswith("-")):
            break
        i += 1
    return out


def _exec_env_refs(seg: str, cwds: list[Path], scan: _Scan, ctx: PolicyContext) -> tuple[list[_Ref], Decision | None]:
    refs: list[_Ref] = []
    for name, value in _env_assignments(seg):
        if name in _EXEC_ENV_DENY:
            return refs, Decision.deny("exec-env", f"Setting {name} makes other programs run extra code; it is not allowed in an unattended run.")
        files: list[tuple[str, str]] = []
        if name in _EXEC_ENV_FILES:
            files.append((value, _EXEC_ENV_FILES[name]))
        elif name in ("NODE_OPTIONS", "RUBYOPT"):
            parts = _shlex(value)
            for j, p in enumerate(parts):
                if p in ("-r", "--require", "--import", "--loader", "--experimental-loader") and j + 1 < len(parts):
                    files.append((parts[j + 1], "code"))
                elif re.match(r"^--(require|import|loader|experimental-loader)=", p):
                    files.append((p.split("=", 1)[1], "code"))
                elif name == "RUBYOPT" and p.startswith("-r") and len(p) > 2:
                    files.append((p[2:], "code"))
        for tok, lang in files:
            if name not in _EXEC_ENV_FILES and not tok.startswith((".", "/", "~", "$")) and "/" not in tok:
                continue  # --require <package name> resolves from node_modules, not a local file
            paths = _resolve_paths(tok, cwds, scan, ctx.repo)
            if paths is None:
                return refs, Decision.deny("script-path", f"{name}={value}: the file it loads cannot be resolved, so it cannot be checked.")
            refs += [r for r in (_file_ref(p, lang, ctx) for p in paths) if r]
    return refs, None


def _explicit_refs(tok: str, lang: str, cwds: list[Path], scan: _Scan, ctx: PolicyContext, what: str) -> tuple[list[_Ref], Decision | None]:
    """Refs for a script the command names explicitly. A script that does not exist yet is denied at the top level
    (the command would fail now, so the only way it runs is after a later write or in the background, unchecked);
    inside a scanned script a missing file is usually behind a condition (`[ -x "$basedir/node" ] && exec …`)."""
    paths = _resolve_paths(tok, cwds, scan, ctx.repo)
    if paths is None:
        return [], Decision.deny("script-path", f"The {what} {tok} cannot be resolved (variables), so its content cannot be checked; run it by its literal path.")
    refs = [r for r in (_file_ref(p, lang, ctx) for p in paths) if r]
    written_here = any(_overlaps(p, scan.written) for p in paths)  # reported as write-then-run, the real reason
    special = tok.startswith(("<(", ">(")) or re.match(r"^[a-z][\w+.-]*:", tok)  # process substitution, URL / npm: specifier
    if not scan.chain and paths and not written_here and not special and not any(os.path.lexists(p) for p in paths) and any(_scannable(p, ctx, lang)[0] for p in paths):
        where = " or ".join(dict.fromkeys((rel_to_repo(str(c), ctx.repo) or str(c)) or "." for c in cwds)) if not os.path.isabs(os.path.expanduser(tok)) else ""
        hint = f" (relative to {where}; the shell's current directory persists between commands)" if where else ""
        return refs, Decision.deny("script-missing", f"The {what} {tok} does not exist yet{hint}, so its content cannot be checked; write it first, then run it in a separate command.")
    return refs, None


def _find_bin(start: Path, name: str, ctx: PolicyContext) -> list[Path]:
    """node_modules/.bin/<name> and .venv/bin/<name> from ``start`` up to the repo root: what a package script's (or
    npx / pnpm exec / uv run) bare program name runs."""
    out: list[Path] = []
    stop = os.path.normpath(str(ctx.repo))
    cur = Path(os.path.normpath(str(start)))
    for _ in range(40):
        for sub in ("node_modules/.bin", ".venv/bin", "venv/bin"):
            cand = cur / sub / name
            if os.path.lexists(cand):
                out.append(cand)
        if os.path.normpath(str(cur)) == stop or cur.parent == cur or rel_to_repo(str(cur), ctx.repo) is None:
            break
        cur = cur.parent
    return out


def _script_refs(toks: list[str], seg: str, cwds: list[Path], scan: _Scan, ctx: PolicyContext) -> tuple[list[_Ref], list[tuple[str, Path]], Decision | None]:
    """Scripts this simple command runs (refs), commands it runs through a runner (with cwd), or a denial."""
    refs: list[_Ref] = []
    nested: list[tuple[str, Path]] = []
    if not toks:
        return refs, nested, None
    prog = os.path.basename(toks[0])
    target = _interpreter_target(toks)
    if target is not None:
        fam, tok, module, _stdin = target
        lang = "shell" if fam == "shell" else "code"
        for pre, plang in _preloads(fam, toks):
            if pre.startswith((".", "/", "~", "$")) or "/" in pre or plang == "shell":
                paths = _resolve_paths(pre, cwds, scan, ctx.repo)
                if paths is None:
                    return refs, nested, Decision.deny("script-path", f"The preloaded file {pre} cannot be resolved, so its content cannot be checked.")
                refs += [r for r in (_file_ref(p, plang, ctx) for p in paths) if r]
        if module:
            for c in cwds:
                base = c / module.replace(".", "/")
                for cand in (base.with_suffix(".py"), base / "__main__.py"):
                    r = _file_ref(cand, "code", ctx) if cand.is_file() else None
                    if r:
                        refs.append(r)
        if tok is not None:
            r2, d = _explicit_refs(tok, lang, cwds, scan, ctx, "script path")
            refs += r2
            if d is not None:
                return refs, nested, d
        # `bash < file` / `python3 < file`: the program is the redirected file
        if target[3]:
            for src in re.findall(r"(?<![<0-9])<(?![<(&])\s*([^\s;&|<>()]+)", seg):
                r2, d = _explicit_refs(src, lang, cwds, scan, ctx, "script")
                refs += r2
                if d is not None:
                    return refs, nested, d
        code = _inline_code(toks)
        if code:
            # python -c "subprocess.run(['bash', 'scripts/x.sh'])", node -e "execSync('bash x.sh')", awk 'BEGIN{system(…)}'
            # inline code inside a script unchanged since the base commit was written by a person: the files it names
            # are judged like a code file's (shell scripts / after an interpreter name), not every path-like word, so
            # `slurp("docs/product/roadmap.md")` in scripts/check-references.sh is a read, not a Markdown "script"
            trusted = scan.script is not None and _base_identical(scan.script, ctx)
            refs += _referenced_scripts(code, cwds, scan, ctx, broad=not trusted)
    elif prog in ("source", "."):
        if len(toks) > 1:
            r2, d = _explicit_refs(toks[1], "shell", cwds, scan, ctx, "sourced file")
            refs += r2
            if d is not None:
                return refs, nested, d
    elif "/" in toks[0] and re.fullmatch(r"[^\s'\"(){};|&<>*?]+", toks[0]):
        r2, d = _explicit_refs(toks[0], "shell", cwds, scan, ctx, "program path")
        if d is not None:
            return refs, nested, d
        for r in r2:
            text, _err = _read_text(r.path)
            r3 = _file_ref(r.path, _lang_of(r.path, text) if text is not None else "shell", ctx)
            if r3:
                refs.append(r3)
    elif prog in ("make", "gmake"):
        refs += _make_refs(toks, cwds, ctx)
    if "/" not in toks[0] and re.fullmatch(r"[\w.+-]+", toks[0]):
        for c in cwds:
            for p in _find_bin(c, toks[0], ctx):
                text, _err = _read_text(p) if not os.path.islink(p) else (None, None)
                r = _file_ref(p, _lang_of(p, text) if text is not None else "shell", ctx)
                if r:
                    refs.append(r)
    call = _pm_call(toks)
    if call is not None:
        r2, n2 = _pm_refs(call, cwds, ctx)
        refs += r2
        nested += n2
    runner = _runner_command(toks)
    if runner is not None:
        cmd, d = runner
        for c in cwds:
            nested.append((cmd, Path(os.path.normpath(os.path.join(str(c), d))) if d else c))
    env_refs, deny = _exec_env_refs(seg, cwds, scan, ctx)
    refs += env_refs
    return refs, nested, deny


def _generated_code(toks: list[str], seg: str, cwds: list[Path], scan: _Scan, ctx: PolicyContext) -> tuple[list[_Ref], Decision | None, bool]:
    """`bash -c "$(…)"`, `eval "$(…)"`, `source <(…)`, `bash <(…)`: code produced by a command at run time."""
    inners: list[str] = []
    if toks:
        prog = os.path.basename(toks[0])
        code = None
        if prog == "eval":
            code = " ".join(toks[1:])
        elif prog in _SHELL_FAMILY:
            for i, a in enumerate(toks[1:], 1):
                if a.startswith("-") and not a.startswith("--") and "c" in a[1:] and i + 1 < len(toks):
                    code = toks[i + 1]
                    break
        if code is not None:
            m = re.fullmatch(r"\s*[\"']?\s*(?:\$\((.*)\)|`(.*)`)\s*[\"']?\s*", code, re.S)
            if m:
                inners.append(m.group(1) if m.group(1) is not None else m.group(2))
    m = re.match(r"^\s*(?:source|\.|(?:\S*/)?(?:ba|z|da|k|mk)?sh)\s+(?:-\S+\s+)*<\((.*)\)\s*$", seg, re.S)
    if m:
        inners.append(m.group(1))
    refs: list[_Ref] = []
    if not inners:
        return refs, None, False
    for inner in inners:
        itoks = _shlex(inner.strip())
        files: list[str] = []
        if itoks[:1] == ["cat"] and len(itoks) > 1 and all(not t.startswith("-") for t in itoks[1:]):
            files = itoks[1:]
        elif re.fullmatch(r"\s*<\s*\S+\s*", inner):
            files = [inner.strip().lstrip("<").strip()]
        elif _ENV_INIT_OK.match(inner):
            continue
        else:
            return refs, Decision.deny("generated-code", "Running code that a command prints at run time (eval \"$(…)\", bash -c \"$(…)\", source <(…)) cannot be checked; run a script file instead."), True
        for f in files:
            paths = _resolve_paths(f, cwds, scan, ctx.repo)
            if paths is None:
                return refs, Decision.deny("script-path", f"The script {f} cannot be resolved, so its content cannot be checked."), True
            refs += [r for r in (_file_ref(p, "shell", ctx) for p in paths) if r]
    return refs, None, True


# --------------------------------------------------------------------------------------
# Literal arguments. The rule checks split a command line conservatively (a `|` or `;` inside quotes splits too), which
# also catches commands hidden in strings a shell later runs (bash -c "…; env", eval, xargs, os.system). Two kinds of
# quoted argument are provably data to the shell, so the fragments inside them are left out of two checks only:
#   * every quoted argument of grep / egrep / fgrep (it never runs its arguments): the environment-dump check;
#   * the single-quoted inline program of perl / ruby / python / node (perl -e '…m{md|sh|yml}…'), which the code
#     checks scan as code: the "… | sh" pipeline check, which then runs over that program's own string literals.
# Only when _mask_quotes provably reads the quotes as the shell does: no comment (an apostrophe in one would open a
# quote for _mask_quotes only), no unclosed quote, no heredoc, and no double-quoted string with an unbalanced or
# quote-holding $( / ${ / backtick (the shell opens a new quoting context inside those). Otherwise nothing is exempt.
# --------------------------------------------------------------------------------------

_GREP_PROGS = frozenset({"grep", "egrep", "fgrep"})
_LITERAL_CODE_FAMS = frozenset({"perl", "ruby", "python", "node", "tsx"})
_WORD_BREAK = " \t\n;&|()<>"


def _dq_body_ok(body: str) -> bool:
    """A double-quoted string's body ends where the shell ends it: every $( / ${ / backtick in it is closed in it, with
    no quote, backslash or comment inside one (those start a new parsing context for the shell)."""
    stack: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if stack and ch in "'\"\\#":
            return False
        if body.startswith("$(", i) or body.startswith("${", i):
            stack.append(")" if body[i + 1] == "(" else "}")
            i += 2
            continue
        if ch == "`":
            if stack and stack[-1] == "`":
                stack.pop()
            else:
                stack.append("`")
        elif stack and ch == "(" and stack[-1] == ")":
            stack.append(")")
        elif stack and ch == stack[-1]:
            stack.pop()
        i += 1
    return not stack


def _quoted_spans(text: str) -> list[tuple[int, int, str, bool]] | None:
    """The quoted strings of ``text`` as (start, end, quote, literal), quotes included; ``literal``: single-quoted, or
    double-quoted without $ or backtick ($'…' strings are not literal). None when the shell may read the quoting
    differently from _mask_quotes (see above)."""
    masked = _mask_quotes(text)
    if "`" in masked:  # bash ends `…` at the next backtick even inside quotes: _mask_quotes cannot be trusted
        return None
    spans: list[tuple[int, int, str, bool]] = []
    i, n = 0, len(text)
    while i < n:
        c = masked[i]
        if c == "#" and (i == 0 or masked[i - 1] in _WORD_BREAK):
            return None
        if c in "'\"" and text[i] == c:
            ansi = c == "'" and i > 0 and text[i - 1] == "$" and masked[i - 1] == "$"
            j = i + 1
            while j < n and not (masked[j] == c and text[j] == c):
                j += 1
            if j >= n:
                return None
            body = text[i + 1:j]
            if c == '"' and not _dq_body_ok(body):
                return None
            spans.append((i, j + 1, c, not ansi and (c == "'" or ("$" not in body and "`" not in body))))
            i = j + 1
            continue
        i += 1
    return spans


def _blank_literal_args(text: str, kinds: frozenset[str]) -> tuple[str, list[str]] | None:
    """``text`` with the exempt literal arguments (kinds "grep" / "code", see above) replaced by '_', and the inline
    programs blanked (kind "code"); None when nothing is exempt."""
    spans = _quoted_spans(text)
    if not spans:
        return None
    masked = _mask_quotes(text)
    # anything that can change what a command name runs (a function or alias named grep/perl, PATH, hash, enable, a
    # sourced file), or that hides another command inside a grep segment ($( <( >( =( { glob qualifiers): no grep
    # exemption; only the redefinitions turn off the inline-program one (its spans must equal the -e programs anyway)
    redefines = re.search(r"\w\s*\(\s*\)|\b(?:function|alias|PATH|hash|enable|source|unalias|unset)\b|(?:^|[\s;&|])\.\s", masked)
    if redefines:
        return None
    grep_ok = "grep" in kinds and not re.search(r"[(){}]", masked)
    cuts = [0]
    for m in _SEGMENT_SPLIT.finditer(masked):
        cuts += [m.start(), m.end()]
    cuts.append(len(text))
    blank: list[tuple[int, int]] = []
    codes: list[str] = []
    for a, b in zip(cuts[0::2], cuts[1::2]):
        inside = [(s, e, q) for s, e, q, lit in spans if lit and a <= s and e <= b]
        if not inside:
            continue
        try:
            words = shlex.split(text[a:b], posix=True)
        except ValueError:
            continue
        if grep_ok and words and words[0] in _GREP_PROGS:  # the bare name, first word: no assignment, keyword or path
            blank += [(s, e) for s, e, _q in inside]
            continue
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):  # FILES="$FILES" perl -e '…'
            words.pop(0)
        if not words or "/" in words[0]:
            continue
        prog = words[0]
        if "code" in kinds and _interp_family(prog) in _LITERAL_CODE_FAMS:
            seg_codes = _inline_codes(words)
            for s, e, q in inside:
                if q == "'" and text[s + 1:e - 1] in seg_codes:
                    blank.append((s, e))
                    codes.append(text[s + 1:e - 1])
    if not blank:
        return None
    out: list[str] = []
    last = 0
    for s, e in sorted(blank):
        out.append(text[last:s] + "'_'")
        last = e
    out.append(text[last:])
    return "".join(out), codes


def _pipelines(stripped: str) -> list[list[str]]:
    out: list[list[str]] = []
    for chain in re.split(r"\|\||&&|;|\n|(?<![<>&0-9|])&(?![>&])", stripped):
        parts = [_strip_groups(p) for p in re.split(r"(?<!\|)\|&?(?!\|)", chain)]
        parts = [p for p in parts if p]
        if len(parts) > 1:
            out.append(parts)
    return out


def _checked_pipelines(text: str, literal_ok: bool) -> list[list[str]]:
    """_pipelines of ``text``, with grep's literal arguments and single-quoted inline programs left whole (see
    "Literal arguments"); the string literals of those programs are split as command lines of their own."""
    blanked = _blank_literal_args(text, frozenset({"grep", "code"})) if literal_ok else None
    out = _pipelines(blanked[0] if blanked else text)
    for code in (blanked[1] if blanked else []):
        for lit in _code_literals(code):
            out += _pipelines(lit)
    return out


def _piped_program_refs(stripped: str, cwds: list[Path], scan: _Scan, ctx: PolicyContext, *, literal_ok: bool = False) -> tuple[list[_Ref], Decision | None]:
    """`… | bash` / `… | python3 -`: the program comes from a pipe. `cat FILE | bash` is checked as FILE; anything
    else (generated or decoded text) cannot be checked and is denied. ``literal_ok`` (no heredoc in the command): a
    single-quoted inline perl/ruby/python/node program is not split at its `|`; its string literals are checked as
    command lines instead (system("x | sh"))."""
    refs: list[_Ref] = []
    pipelines = _checked_pipelines(stripped, literal_ok)
    for _a, _b, body in _substitutions(stripped):  # $(…) / `…` bodies run even inside double quotes: "$(gen | bash)"
        pipelines += _checked_pipelines(body, literal_ok)
    for parts in pipelines:
        for i in range(1, len(parts)):
            ctoks = tokenize(parts[i])
            if not _reads_program_from_stdin(ctoks):
                continue
            lang = "shell" if _interp_family(os.path.basename(ctoks[0])) == "shell" else "code"
            ptoks = tokenize(parts[i - 1])
            files = [t for t in ptoks[1:] if not t.startswith("-")] if ptoks[:1] == ["cat"] else []
            if not files:
                return refs, Decision.deny("stdin-script", "Piping generated text into an interpreter (… | bash, … | python3) cannot be checked; write the commands out or run a script file.")
            for f in files:
                paths = _resolve_paths(f, cwds, scan, ctx.repo)
                if paths is None:
                    return refs, Decision.deny("script-path", f"The script {f} cannot be resolved, so its content cannot be checked.")
                refs += [r for r in (_file_ref(p, lang, ctx) for p in paths) if r]
    return refs, None


def _scan_ref(ref: _Ref, ctx: PolicyContext, scan: _Scan) -> Decision | None:
    """Evaluate what a ref runs; a denial is reported as "script <path>: <inner reason>"."""
    display = _ref_label(ref, ctx)
    if ref.kind == "deny":
        return Decision.deny("script-unreadable", f"script {display}: {ref.reason}.")
    real = os.path.normpath(os.path.realpath(str(ref.path)))
    key = f"{ref.kind}:{real}#{ref.name}" if ref.kind != "file" else f"file:{real}"
    if key in scan.chain:
        return None  # already being checked further up (a script that runs itself)
    recurses = not (ref.kind == "file" and ref.lang == "code")  # code files get a text check, nothing nested
    if recurses and len(scan.chain) >= SCRIPT_MAX_DEPTH:
        return Decision.deny("script-depth", f"script {display}: scripts nest more than {SCRIPT_MAX_DEPTH} levels deep, so the inner ones cannot be checked; run the inner script directly.")
    if ref.kind == "file":
        content, err = _read_text(ref.path)
        if _VENDOR_BIN.search(str(ref.path)) and (err or "\x00" in (content or "")[:4096]):
            return None  # a compiled entry point (.venv/bin/ruff): not a script
        if err:
            return Decision.deny("script-unreadable", f"script {display}: {err}.")
        if content is None:
            return None
        if ref.lang == "code":
            code = _drop_comment_lines(content, ref.path)
            d = _backstop(code) or _credential_text_check(code)
            if d is None:
                # a shell script the code runs by name (execSync('bash scripts/x.sh'), system("./x.sh"))
                inner = scan.into(key, scan.cwd, script=ref.path)
                for r in _referenced_scripts(code, [scan.cwd, ref.path.parent], inner, ctx, broad=False):
                    d2 = _scan_ref(r, ctx, inner)
                    if d2 is not None and not d2.allow:
                        d = d2
                        break
        else:
            text = strip_shell_comments(content)
            vars_ = _script_vars(text, ref.path, scan.cwd, ctx.repo)
            d = evaluate_bash(text, ctx, _scan=scan.into(key, scan.cwd, script=ref.path, vars=vars_))
    else:
        d = evaluate_bash(ref.text, ctx, _scan=scan.into(key, ref.cwd or scan.cwd))
    if d is not None and not d.allow:
        return Decision.deny(d.rule, f"script {display}: {d.reason}")
    if d is not None and d.log:
        return Decision.ok(d.rule, f"script {display}: {d.reason}", log=True)
    return None


def _ref_label(ref: _Ref, ctx: PolicyContext) -> str:
    where = rel_to_repo(os.path.realpath(str(ref.path)), ctx.repo) or rel_to_repo(str(ref.path), ctx.repo) or str(ref.path)
    return f"{where}#{ref.name}" if ref.kind in ("pkg", "make") else where


def evaluate_bash(command: str, ctx: PolicyContext, _depth: int = 0, *, cwd: str | Path | None = None, _scan: _Scan | None = None, background: bool = False) -> Decision:
    """Decide a shell command line. ``cwd`` is the shell's working directory (the hook reports it; default: the repo).
    ``background``: the command runs detached (Bash run_in_background, or an outer command that defers it)."""
    if _depth > 6:
        return Decision.deny("too-nested", "The command nests interpreters too deeply to be checked.")
    scan = _scan or _Scan(Path(os.path.expanduser(str(cwd))) if cwd else ctx.repo)
    cmd = normalize(command)
    stripped, bodies = _strip_heredocs(cmd)
    for text in (cmd,):
        if _PIPE_TO_SHELL.search(text) or _SHELL_PROCSUB.search(text) or _DOWNLOAD_THEN_RUN.search(text) or _PIPE_TO_SHELL.search(_dequote(text)):
            return Decision.deny("pipe-to-shell", "Running downloaded content (curl|sh, wget|python, source <(curl ...)) is never allowed.")
    if _SYSTEM_DESTRUCTIVE.search(stripped):
        return Decision.deny("system-destructive", "System-level destructive commands are never allowed.")
    d = _backstop(stripped)
    if d is not None:
        return d
    d = _credential_check(stripped, ctx.repo)
    if d is not None:
        return d
    # heredoc bodies fed to a shell are commands, fed to another interpreter they are code; elsewhere they are data
    for op_line, body in bodies:
        op_toks = tokenize(op_line.strip().split("<<", 1)[0]) if op_line.strip() else []
        op = os.path.basename(op_toks[0]) if op_toks else ""
        if op in _SHELLS or op in ("eval", "source", "."):
            d = evaluate_bash(body, ctx, _depth + 1, _scan=scan, background=background)
            if not d.allow:
                return d
        elif op in _SCRIPTERS or _interp_family(op):
            d = _backstop(body) or _credential_text_check(body)
            if d is not None:
                return d

    segments = split_segments(cmd)
    qsegments = split_segments_quoted(cmd)
    deferred = background or _defers(stripped, segments)
    # pass 1: what this command line writes (a script written here and run here is checked against stale content)
    cwds = [scan.cwd]
    written: set[str] = set(scan.written)
    unresolved_write = False
    for seg in qsegments:
        toks = tokenize(seg)
        if not toks:
            continue
        cwds = _track_cd(toks, cwds, scan, ctx)
        w, unknown = _content_writes(toks, seg, cwds, scan, ctx.repo)
        written |= w
        unresolved_write = unresolved_write or (unknown and not scan.chain)
    scan = _Scan(scan.cwd, scan.chain, scan.script, scan.vars, frozenset(written))

    # pass 2: the rule checks, over the conservative split (a fragment of a quoted string is checked too)
    log = False
    notes: list[str] = []
    cwds = [scan.cwd]
    values: dict[str, tuple[str, str | None]] = {}  # shell variables assigned so far in this command line / script
    # environment dumps: over the conservative split too, except inside grep's literal arguments (grep -E "a|export|b")
    grep_blank = None if bodies else _blank_literal_args(stripped, frozenset({"grep"}))
    for seg in (split_segments(grep_blank[0]) if grep_blank else segments):
        if seg.strip() in ("env", "printenv", "set", "export", "export -p", "declare -x", "declare -p", "compgen -v"):
            return Decision.deny("credentials", "Dumping the environment is not allowed (it may print secrets).")
        etoks = tokenize(seg)
        if etoks and (os.path.basename(etoks[0]) == "printenv" or (os.path.basename(etoks[0]) == "env" and len(etoks) == 1)):
            return Decision.deny("credentials", "Dumping the environment is not allowed (it may print secrets).")
    for seg in segments:
        assigned = _assignment(seg, values)
        if assigned is not None:
            values[assigned[0]] = assigned[1]
        toks = tokenize(seg)
        if not toks:
            continue
        prog = os.path.basename(toks[0])
        for t in toks[1:]:
            if not t.startswith("-") and is_credential_path(t):
                return Decision.deny("credentials", f"{t} is a credential/secret file.")
        # code from a substitution (eval "$(cat f)") is checked as the file in pass 3, not as the text "$(cat f)"
        generated = _generated_code(toks, seg, [scan.cwd], scan, ctx)[2]
        for inner in ([] if generated else _nested_commands(toks)):
            itoks = tokenize(inner)
            target = _interpreter_target(itoks)
            if prog in ("xargs", "parallel", "find") and target and not target[1] and target[3]:
                return Decision.deny("script-path", f"{prog} hands file names to {os.path.basename(itoks[0])}, so the script it runs cannot be checked; run each script directly.")
            for where in cwds:
                d = evaluate_bash(inner, ctx, _depth + 1, _scan=_Scan(where, scan.chain, scan.script, scan.vars, scan.written), background=deferred)
                if not d.allow:
                    return d
                log = log or d.log
        cwds = _track_cd(toks, cwds, scan, ctx)
        if prog in _SCRIPTERS:
            for i, a in enumerate(toks[1:], 1):
                if a in ("-c", "-e", "--eval", "-E", "-r", "--run") and i + 1 < len(toks):
                    d = _backstop(toks[i + 1]) or _credential_check(toks[i + 1], ctx.repo)
                    if d is not None:
                        return d
            for code in _inline_codes(toks):  # also clustered switches (perl -lne CODE, node -pe CODE, perl -eCODE)
                d = _backstop(code) or _credential_check(code, ctx.repo)
                if d is not None:
                    return d
        if prog in ("sudo", "doas", "su", "pkexec"):
            return Decision.deny("sudo", "sudo / privilege escalation is never allowed.")
        if prog in ("ssh", "scp", "sftp", "mosh", "telnet", "ssh-copy-id") or (prog == "rsync" and any(re.match(r"^[^/\s]*[\w.-]+:", a) for a in toks[1:] if not a.startswith("-"))):
            return Decision.deny("remote-shell", "Remote shells / copies (ssh, scp, sftp, rsync host:) are never allowed in an unattended run.")
        if prog == "docker" and any(a in ("-H", "--host", "--context", "-c") or a.startswith("--host=") or a.startswith("--context=") for a in toks[1:3]):
            return Decision.deny("remote-shell", "docker against a remote host/context is not allowed.")
        if prog in ("kubectl", "helm") and any(a in ("apply", "delete", "exec", "install", "upgrade", "uninstall", "rollout", "scale", "patch", "replace", "edit") for a in toks[1:4]):
            return Decision.deny("cluster-write", f"{prog} mutations are never allowed.")
        d = _publish_check(toks)
        if d is not None:
            return d
        d = _check_paths(toks, seg, ctx)
        if d is not None:
            return d
        if prog == "git":
            gtoks, unknown = _git_with_values(seg, values)
            d = _evaluate_git(gtoks, ctx)
            gsub = _git_sub(gtoks)[0]
            if d.allow and unknown and gsub in _GIT_FLAG_SUBS:
                d = Decision.deny("git-variable-arg", f"git {gsub} with an argument held in a variable or a command's output ({unknown[0]}) cannot be checked (it could be --force, --hard or -f); spell the arguments out.")
        elif prog == "gh":
            d = _evaluate_gh(toks, ctx)
        else:
            d = None
            kind = _classify_install(toks)
            if kind == "global":
                return Decision.deny("install-global", "Global/system package installs are never allowed.")
            if kind == "add":
                if ctx.phase not in ctx.install_phases:
                    return Decision.deny("install-phase", f"Adding dependencies is only allowed during {', '.join(ctx.install_phases)} (current phase: {ctx.phase}).")
                log = True
                notes.append(f"dependency install: {seg.strip()}")
            elif kind == "lockfile":
                log = True
                notes.append(f"lockfile install: {seg.strip()}")
        if d is not None:
            if not d.allow:
                return d
            log = log or d.log
            if d.reason:
                notes.append(d.reason)

    # pass 3: what runs scripts, over the quote-aware split (an awk program is one argument, not a program path)
    refs: list[_Ref] = []
    cwds = [scan.cwd]
    for seg in qsegments:
        toks = tokenize(seg)
        if not toks:
            continue
        if not scan.chain and re.search(r"[$`]", toks[0]) and not re.match(r"^(\$\{?HOME\}?|\$\{?PWD\}?)/", toks[0]):
            return Decision.deny("variable-command", f"The program to run comes from a variable or substitution ({toks[0]}), so it cannot be checked; spell the command out.")
        cwds = _track_cd(toks, cwds, scan, ctx)
        seg_refs, nested, d = _script_refs(toks, seg, cwds, scan, ctx)
        if d is not None:
            return d
        gen_refs, d, _generated = _generated_code(toks, seg, cwds, scan, ctx)
        if d is not None:
            return d
        refs += seg_refs + gen_refs
        for inner, where in nested:
            d = evaluate_bash(inner, ctx, _depth + 1, _scan=_Scan(where, scan.chain, scan.script, scan.vars, scan.written), background=deferred)
            if not d.allow:
                return d
            log = log or d.log
    pipe_refs, d = _piped_program_refs(stripped, cwds, scan, ctx, literal_ok=not bodies)
    if d is not None:
        return d
    refs += pipe_refs
    runs = [r for r in dict.fromkeys(refs) if r.kind in ("file", "pkg", "make")]
    if deferred and runs and not scan.chain and ctx.phase in UNSANDBOXED_PHASES:
        return Decision.deny("deferred-script", f"In {ctx.phase} (no OS sandbox) a script may not run in the background or after a delay ({_ref_label(runs[0], ctx)}): it could be rewritten after this check. Run it in the foreground, on its own.")
    for ref in dict.fromkeys(refs):
        if ref.kind != "deny" and _overlaps(ref.path, frozenset(written)):
            return Decision.deny("write-then-run", f"This command writes {_ref_label(ref, ctx)} and runs it in the same command line; the content cannot be checked before it runs. Write it first, then run it in a separate command.")
        if unresolved_write and ref.kind != "deny":
            return Decision.deny("write-then-run", f"This command writes files it does not name (a path in a variable, a patch, inline code) and runs {_ref_label(ref, ctx)}; spell the written path out or run it in a separate command.")
    for ref in dict.fromkeys(refs):
        d = _scan_ref(ref, ctx, scan)
        if d is not None and not d.allow:
            return d
        if d is not None and d.log:
            log = True
            notes.append(d.reason)
    if not scan.chain and ctx.phase in UNSANDBOXED_PHASES:
        ctx.ran_scripts |= {os.path.normpath(os.path.realpath(str(r.path))) for r in runs if r.kind in ("file", "make")}
    return Decision.ok("bash", "; ".join(notes), log=log)


# A command that runs later than its check: detached (&, nohup, setsid, disown, at, batch), after a delay or a wait loop.
_BACKGROUND_AMP = re.compile(r"(?<![<>&0-9|])&(?![>&|])")
_DEFERRING_WORDS = frozenset({"nohup", "setsid", "disown", "sleep", "wait", "until", "while", "at", "batch", "watch"})


def _defers(stripped: str, segments: list[str]) -> bool:
    if _BACKGROUND_AMP.search(_mask_quotes(stripped)):
        return True
    return any((_shlex(seg)[:1] or [""])[0].lstrip("({!") in _DEFERRING_WORDS for seg in segments)


# git subcommands whose dangerous flags (--force, --hard, -f/-d/-x) could hide in a variable; checkout/branch take
# "$BRANCH_NAME" in Spec Kit's own create-new-feature.sh, so they only get the known values substituted.
_GIT_FLAG_SUBS = frozenset({"push", "reset", "clean"})
# set by the autopilot itself (the unattended contract): a branch name or feature directory, never a flag
_SAFE_GIT_VARS = frozenset({"GIT_BRANCH_NAME", "SPECIFY_FEATURE", "SPECIFY_FEATURE_DIRECTORY"})
_GIT_REF_QUERY = re.compile(r"^\s*git\s+(?:-C\s+\S+\s+)*(?:rev-parse|branch\s+--show-current|symbolic-ref|merge-base|describe|for-each-ref|name-rev|show-ref)\b[^|;&<>`$]*$")
_ASSIGN = re.compile(r"^\s*(?:(?:export|readonly|local)\s+|(?:declare|typeset)\s+(?:-\w+\s+)*)?([A-Za-z_]\w*)=(.*)$", re.S)


def _subst_value(body: str) -> tuple[str, str | None]:
    """What a ``$(…)`` prints, as far as its text tells: ("ref", None) for a git query (a ref or SHA, never a flag),
    ("literal", words) for echo / printf of literal words, else ("unknown", None)."""
    if _GIT_REF_QUERY.match(body):
        return "ref", None
    m = re.match(r"^\s*(echo|printf)\s+(.*)$", body, re.S)
    if m and not re.search(r"[$`]", m.group(2)):
        words = _shlex(m.group(2))
        if m.group(1) == "echo":
            while words and re.fullmatch(r"-[neE]+", words[0]):
                words.pop(0)
        else:
            if words[:1] == ["--"]:
                words.pop(0)
            if words and "%" in words[0]:
                words.pop(0)
        return "literal", " ".join(words)
    return "unknown", None


def _expand_known(tok: str, values: dict[str, tuple[str, str | None]]) -> str:
    def rep(m: re.Match[str]) -> str:
        kind, val = values.get(m.group(1) or m.group(2), ("unknown", None))
        return val if kind == "literal" and val is not None else ("__REF__" if kind == "ref" else m.group(0))

    return re.sub(r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)", rep, tok)


def _assignment(seg: str, values: dict[str, tuple[str, str | None]]) -> tuple[str, tuple[str, str | None]] | None:
    """(NAME, value) when ``seg`` only assigns a shell variable (F=--force, export F=…, B=$(git rev-parse …))."""
    m = _ASSIGN.match(seg)
    if not m:
        return None
    name, raw = m.group(1), m.group(2).strip()
    sm = re.fullmatch(r"\"?(?:\$\((.*)\)|`(.*)`)\"?", raw, re.S)
    if sm:
        return name, _subst_value(sm.group(1) if sm.group(1) is not None else sm.group(2))
    toks = _shlex(raw)
    if len(toks) != 1:
        return None  # `F=x cmd …` sets F for one command; it does not define a variable for later ones
    v = _expand_known(toks[0], values)
    return name, (("unknown", None) if re.search(r"[$`]", v) else ("literal", v))


def _git_with_values(seg: str, values: dict[str, tuple[str, str | None]]) -> tuple[list[str], list[str]]:
    """The git command's tokens with what is known substituted (variables assigned earlier in the command line,
    echo/printf output, git queries), and the arguments whose value is still unknown."""
    text = seg
    for a, b, body in sorted(_substitutions(seg), reverse=True):
        kind, val = _subst_value(body)
        text = text[:a] + (shlex.quote(val or "") if kind == "literal" else ("__REF__" if kind == "ref" else "$__UNKNOWN__")) + text[b:]
    out: list[str] = []
    unknown: list[str] = []
    for t in tokenize(text):
        v = _expand_known(t, values)
        if v != t and re.fullmatch(r"\$\{?[A-Za-z_]\w*\}?", t):
            out += v.split() or [""]
        else:
            out.append(v)
        names = re.findall(r"\$\{?([A-Za-z_]\w*)", v)
        if "`" in v or any(n not in _SAFE_GIT_VARS for n in names):
            unknown.append(t)
    return out, unknown


def _track_cd(toks: list[str], cwds: list[Path], scan: _Scan, ctx: PolicyContext) -> list[Path]:
    """Directories a later relative path may be resolved against after `cd`/`pushd` (all are tried)."""
    if not toks or toks[0] not in ("cd", "pushd", "chdir"):
        return cwds
    args = [a for a in toks[1:] if not a.startswith("-") or a == "-"]
    if not args:
        return list(dict.fromkeys(cwds + [Path(_HOME)]))
    paths = _resolve_paths(args[0], cwds, scan, ctx.repo)
    if not paths:
        return list(dict.fromkeys(cwds + [ctx.repo]))
    return list(dict.fromkeys(cwds + [Path(os.path.normpath(str(p))) for p in paths]))[-8:]


_PUBLISH_SUBCOMMANDS = {"publish", "unpublish", "deprecate", "adduser", "add-user", "login", "token", "owner", "access", "dist-tag"}
_PUBLISH_TOOLS = {"twine": "upload", "uv": "publish", "poetry": "publish", "flit": "publish", "hatch": "publish", "pdm": "publish", "cargo": "publish", "gem": "push", "changeset": "publish"}


def _publish_check(toks: list[str]) -> Decision | None:
    """Publishing packages (or touching registry credentials) is never part of a feature pipeline."""
    if not toks:
        return None
    prog = os.path.basename(toks[0])
    words: list[str] = []
    if prog in ("npm", "pnpm", "yarn", "bun"):
        words = _words(toks[1:], _PM_VALUES.get(prog, set()))
        if prog == "yarn" and words[:1] == ["npm"]:
            words = words[1:]
        hit = bool(words) and words[0] in _PUBLISH_SUBCOMMANDS and not (words[0] == "dist-tag" and words[1:2] == ["ls"])
    elif prog in _PUBLISH_TOOLS:
        hit = _PUBLISH_TOOLS[prog] in toks[1:3]
    else:
        hit = prog in ("np", "semantic-release", "release-it")
    if hit:
        return Decision.deny("package-publish", "Publishing packages or changing registry credentials is never part of this pipeline.")
    return None


def _norm_ref(ref: str) -> str:
    r = ref
    for p in ("refs/heads/", "heads/", "refs/remotes/"):
        if r.startswith(p):
            r = r[len(p):]
    return r


def _is_base_ref(ref: str, ctx: PolicyContext) -> bool:
    r = _norm_ref(ref)
    return r in (ctx.base_branch, f"origin/{ctx.base_branch}") or r in ("-", "@{-1}") or re.fullmatch(r"@\{-\d+\}", r) is not None


def _evaluate_git(toks: list[str], ctx: PolicyContext) -> Decision:
    sub, args, overrides = _git_sub(toks)
    for ov in overrides:
        key = ov.split("=", 1)[0].lower()
        if key.startswith("alias.") or key in ("core.hookspath", "core.sshcommand", "clean.requireforce") or key.startswith("credential."):
            return Decision.deny("git-config-override", f"git -c {key}=... is never allowed (it can bypass hooks or this policy).")
    if sub is None:
        return Decision.ok("git")
    pos = [a for a in args if not a.startswith("-")]
    if sub == "push":
        force = any(a in ("--force", "-f", "--force-if-includes", "--mirror", "--prune") or a.startswith("--force-with-lease") for a in args)
        force = force or _short_flag_cluster_has(args, "f") or any(a.startswith("+") and len(a) > 1 for a in pos)
        if force:
            return Decision.deny("force-push", "Force pushes (--force, -f, --force-with-lease, +refspec, --mirror, --prune) are never allowed.")
        deleting = any(a in ("--delete", "-d") for a in args) or _short_flag_cluster_has(args, "d") or any(a.startswith(":") and len(a) > 1 for a in pos)
        refs = [r.split(":", 1)[-1] for r in pos[1:]]
        if deleting and any(_norm_ref(r) == ctx.base_branch for r in refs):
            return Decision.deny("delete-base", f"Deleting the base branch ({ctx.base_branch}) on the remote is never allowed.")
        if deleting and not ctx.ship_like:
            return Decision.deny("push-phase", f"Deleting remote branches is only allowed during the ship phase (current phase: {ctx.phase}).")
        if not ctx.ship_like and not _allowed_feature_push(args, ctx):
            return Decision.deny("push-phase", f"git push is only allowed during the ship phase (current phase: {ctx.phase}).")
        return Decision.ok("git-push", f"git push {' '.join(args)}", log=True)
    if sub == "reset" and ("--hard" in args or "--merge" in args or "--keep" in args):
        return Decision.deny("reset-hard", f"git reset {'--hard' if '--hard' in args else '--merge/--keep'} is never allowed.")
    if sub == "clean" and ("--force" in args or _short_flag_cluster_has(args, "f")):
        return Decision.deny("clean-force", "git clean -f is never allowed.")
    if sub == "branch":
        short = [a for a in args if a.startswith("-") and not a.startswith("--")]
        dflag = any(a in ("-d", "--delete") for a in args) or any("d" in a[1:] for a in short)
        fflag = any(a in ("-f", "--force") for a in args) or any("f" in a[1:] for a in short)
        if "-D" in args or any("D" in a[1:] for a in short) or (dflag and fflag):
            return Decision.deny("branch-force-delete", "Force-deleting branches (git branch -D / -df) is never allowed.")
        if not ctx.ship_like and (fflag or any("M" in a[1:] or "m" in a[1:] or "C" in a[1:] or "c" in a[1:] for a in short) or any(a in ("--move", "--copy", "--force") for a in args)) and any(_norm_ref(p) == ctx.base_branch for p in pos):
            return Decision.deny("base-mutation", f"Moving, copying or force-setting the base branch ({ctx.base_branch}) is only allowed during the ship phase.")
    if sub == "update-ref" and any(_norm_ref(p) == ctx.base_branch for p in pos) and not ctx.ship_like:
        return Decision.deny("base-mutation", f"Rewriting the base branch ref ({ctx.base_branch}) is only allowed during the ship phase.")
    if sub == "fetch" and not ctx.ship_like and ctx.phase != "feature_start":
        if any(":" in p and _norm_ref(p.split(":", 1)[1].lstrip("+")) == ctx.base_branch for p in pos):
            return Decision.deny("base-mutation", f"Fetching into the local base branch ({ctx.base_branch}) is only allowed during feature_start/ship.")
    if sub == "stash":
        action = pos[0] if pos else "push"
        if action in ("drop", "clear"):
            return Decision.deny("stash-destroy", "git stash drop/clear destroys work and is never allowed.")
        if action not in ("list", "show"):
            return Decision.deny("stash", "git stash is not allowed (the stash is shared across worktrees and hides work); commit or leave changes in place.")
    if ctx.deny_bulk_add and sub == "add" and any(a in ("-A", "--all", ".", "./", ":/", "*", "-u", "--update", ":(top)", ":/*") or (a.startswith("-") and not a.startswith("--") and ("A" in a[1:] or "u" in a[1:])) for a in args):
        return Decision.deny("bulk-add", "Bulk staging (git add -A / . / -u / --all) is not allowed: stage the changed files explicitly by path.")
    if ctx.deny_bulk_add and sub == "commit" and any(a in ("-a", "--all") or (re.match(r"^-[A-Za-z]+$", a) and "a" in a[1:] and not a.startswith("--")) for a in args):
        return Decision.deny("bulk-add", "git commit -a is not allowed: stage the changed files explicitly by path, then commit.")
    if sub in ("filter-branch", "filter-repo", "replace"):
        return Decision.deny("history-rewrite", "History rewrites are never allowed.")
    if sub == "rebase" and any(a in ("-i", "--interactive") for a in args):
        return Decision.deny("interactive", "Interactive commands cannot run unattended.")
    if sub == "config" and any(re.match(r"(?i)^(alias\.|core\.hookspath|core\.sshcommand|credential\.|clean\.requireforce)", p) for p in pos):
        return Decision.deny("git-alias", "Defining git aliases or rewriting hook/credential/clean settings is never allowed.")
    if sub == "commit":
        prev = ""
        for a in args:
            if prev in ("-m", "-F", "-c", "-C", "--message", "--file", "--author", "--date"):
                prev = ""
                continue
            if re.match(r"^-[A-Za-z]+$", a) and "n" in a[1:]:
                return Decision.deny("no-verify", "git commit -n (--no-verify) is never allowed.")
            prev = a
    # discarding uncommitted work wholesale is equivalent to reset --hard
    if sub in ("checkout", "restore"):
        paths = args[args.index("--") + 1:] if "--" in args else (list(pos) if sub == "restore" else [p for p in pos if p in (".", "./", ":/", "*", ":(top)")])
        staged_only = sub == "restore" and "--staged" in args and "--worktree" not in args and "-W" not in args
        if not staged_only and any(p in (".", "./", ":/", "*", ":(top)", "-A") for p in paths):
            return Decision.deny("discard-work", f"git {sub} of the whole tree discards uncommitted work (like reset --hard) and is never allowed.")
    if sub in ("merge", "pull") and not ctx.ship_like:
        if sub == "merge" and any(a in ("--abort", "--quit") for a in args):
            return Decision.ok("git")
        if sub == "pull" and ctx.phase == "feature_start":
            return Decision.ok("git-pull", "git pull during feature_start", log=True)
        return Decision.deny(f"{sub}-phase", f"git {sub} is only allowed during the ship phase (current phase: {ctx.phase}).")
    if sub in ("checkout", "switch") and "--" not in args:
        reset_targets = [args[j + 1] for j, a in enumerate(args[:-1]) if a in ("-B", "-C")]
        creating_new = any(a in ("-b", "-c", "--orphan") for a in args)
        plain = [] if creating_new else [a for a in args if a in ("-", "@{-1}") or not a.startswith("-")]
        if any(_is_base_ref(t, ctx) for t in plain[:1] + reset_targets):
            if ctx.ship_like:
                return Decision.ok("git-checkout-base", "switch to base during ship", log=True)
            if ctx.phase == "feature_start":
                return Decision.ok("git-checkout-base", "checkout base during feature_start", log=True)
            return Decision.deny("checkout-base", f"Switching to (or resetting) the base branch ({ctx.base_branch}) is only allowed during the ship phase (current phase: {ctx.phase}).")
    if sub == "tag" and not ctx.ship_like and pos and not any(a in ("-l", "--list") for a in args):
        return Decision.deny("tag-phase", "Creating tags is only allowed during the ship phase.")
    if sub in ("merge", "pull"):
        return Decision.ok(f"git-{sub}", f"git {sub} {' '.join(args)}", log=True)
    return Decision.ok("git")


def _allowed_feature_push(args: list[str], ctx: PolicyContext) -> bool:
    """feature_start may publish the new feature branch (never the base branch)."""
    if ctx.phase != "feature_start" or "feature_start" not in ctx.push_phases:
        return False
    refs = [a for a in args if not a.startswith("-")]
    return not any(_norm_ref(r.split(":", 1)[-1]) == ctx.base_branch or r in ("HEAD:" + ctx.base_branch,) for r in refs[1:])


_GH_VALUE_FLAGS = {
    "-R", "--repo", "--hostname", "-X", "--method", "-H", "--header", "-f", "-F", "--field", "--raw-field", "--input",
    "-q", "--jq", "-t", "--template", "-b", "--body", "-B", "--base", "--head", "-T", "--title", "-l", "--label",
    "-a", "--assignee", "-m", "--milestone", "-r", "--reviewer", "-p", "--project", "-L", "--limit", "-s", "--state",
    "-A", "--author", "--body-file", "-e", "--editor", "--subject", "--match-head-commit", "--json", "-w", "--workflow",
    "--ref", "-c", "--commit", "--branch", "-u", "--user",
}
_GH_API_WRITE_OK = re.compile(r"^/?repos/[^/]+/[^/]+/pulls(/\d+(/merge)?)?/?$")


def _gh_words(args: list[str]) -> list[str]:
    words: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            if a in _GH_VALUE_FLAGS and "=" not in a:
                i += 1
            i += 1
            continue
        words.append(a)
        i += 1
    return words


def _evaluate_gh(toks: list[str], ctx: PolicyContext) -> Decision:
    args = toks[1:]
    words = _gh_words(args)
    if not words:
        return Decision.ok("gh")
    group = words[0]
    action = words[1] if len(words) > 1 else ""
    if group == "alias" and action in ("set", "import"):
        return Decision.deny("gh-alias", "gh aliases are never allowed.")
    if group == "auth" and action in ("token", "refresh", "login", "logout", "setup-git", "switch"):
        return Decision.deny("credentials", f"gh auth {action} is not allowed.")
    if group in ("secret", "ssh-key", "gpg-key", "variable") and action not in ("list",):
        return Decision.deny("credentials", f"gh {group} {action} is not allowed.")
    if group == "repo" and action in ("delete", "archive", "rename", "edit", "sync", "set-default", "unarchive"):
        return Decision.deny("gh-repo-admin", f"gh repo {action} is never allowed.")
    if group == "issue" and action in ("create", "close", "delete", "edit", "transfer", "reopen", "lock", "pin", "comment"):
        if not ctx.ship_like:
            return Decision.deny("gh-issue", f"gh issue {action} changes remote state and is not allowed (current phase: {ctx.phase}).")
    if group == "pr":
        if action == "merge" and any(a == "--admin" or a.startswith("--admin=") for a in args):
            return Decision.deny("gh-admin-merge", "gh pr merge --admin bypasses branch protection and is never allowed.")
        if action in ("merge", "create", "ready", "close", "reopen", "edit", "comment", "review", "lock"):
            if not ctx.ship_like:
                return Decision.deny("gh-pr-phase", f"gh pr {action} is only allowed during the ship phase (current phase: {ctx.phase}).")
            return Decision.ok("gh-pr", f"gh pr {action}", log=True)
    if group == "release" and action in ("create", "delete", "edit", "upload", "delete-asset"):
        if not ctx.ship_like:
            return Decision.deny("gh-release-phase", "gh release changes are only allowed during the ship phase.")
    if group == "workflow" and action in ("run", "enable", "disable"):
        if not ctx.ship_like:
            return Decision.deny("gh-workflow-phase", "Triggering/changing workflows is only allowed during the ship phase.")
    if group == "run" and action in ("cancel", "delete", "rerun"):
        if not ctx.ship_like:
            return Decision.deny("gh-run-phase", f"gh run {action} is only allowed during the ship phase.")
    if group == "api":
        method = "GET"
        endpoint = words[1] if len(words) > 1 else ""
        for i, a in enumerate(args):
            if a in ("-X", "--method") and i + 1 < len(args):
                method = args[i + 1].upper()
            elif a.startswith("--method="):
                method = a.split("=", 1)[1].upper()
            elif a.startswith("-X") and len(a) > 2:
                method = a[2:].upper()
            elif a in ("-f", "-F", "--field", "--raw-field", "--input") or re.match(r"^-[fF].+", a) or re.match(r"^--(field|raw-field|input)=", a):
                if method == "GET":
                    method = "POST"
        if endpoint == "graphql":
            body = " ".join(args)
            method = "POST" if re.search(r"\bmutation\b", body) else "GET"
        if method == "DELETE":
            return Decision.deny("gh-api-delete", "gh api DELETE calls are never allowed.")
        if method != "GET":
            if not ctx.ship_like:
                return Decision.deny("gh-api-write", f"Mutating gh api calls ({method}) are only allowed during the ship phase.")
            if not _GH_API_WRITE_OK.match(endpoint):
                return Decision.deny("gh-api-write", f"In ship, gh api writes are limited to pull-request endpoints (got {method} {endpoint}).")
    return Decision.ok("gh")


# --------------------------------------------------------------------------------------
# Top-level evaluation
# --------------------------------------------------------------------------------------

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead", "LS"}
SHELL_TOOLS = {"Bash", "Monitor", "PowerShell"}
# Tools a worker may use without further checks. Everything else (unknown or newly added CLI tools such as
# RemoteTrigger, CronCreate, EnterWorktree, ...) is denied by default.
SAFE_TOOLS = {
    "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput", "TaskStop", "KillShell", "KillBash",
    "BashOutput", "ToolSearch", "Skill", "SlashCommand", "Task", "Agent", "WebFetch", "WebSearch", "AskUserQuestion",
    "ListMcpResourcesTool", "ReadMcpResourceTool", "ListMcpResources", "ReadMcpResource", "StructuredOutput", "LSP",
    "ExitPlanMode", "SendMessage",
}
WORKER_DISALLOWED_TOOLS = ("Monitor", "PowerShell", "RemoteTrigger", "CronCreate", "CronDelete", "CronList", "EnterWorktree", "ExitWorktree", "ScheduleWakeup", "EnterPlanMode", "PushNotification")
_LINEAR_READ_VERBS = ("get_", "list_", "search_", "extract_", "whoami", "query_")
_LINEAR_NEVER = re.compile(r"^(delete_|merge_|unshare_|retire_|restore_|share_|mark_|submit_|resolve_|update_diff|save_diff|prepare_)|_diff")
_LINEAR_PHASE_WRITES = {
    "linear": {"save_issue", "save_comment", "save_project", "save_status_update"},
    "halt_notice": {"save_comment"},
}
_GENERIC_MCP_READ = re.compile(r"^(get|list|search|read|query|fetch|find|describe|show|lookup|retrieve|resolve[-_]library|view|count|inspect|check)(_|$)")
_MCP_WRITE_VERB = re.compile(r"(^|_)(create|update|delete|remove|merge|push|set|save|mark|send|post|run|execute|write|upload|publish|archive|move|add|edit|patch|put|close|cancel|approve|trigger|deploy)(_|$)")


def _edit_target(tool_name: str, tool_input: dict[str, Any]) -> str:
    return str(tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or "")


def evaluate(tool_name: str, tool_input: dict[str, Any], ctx: PolicyContext, cwd: str | Path | None = None) -> Decision:
    """Decide one tool call. ``cwd`` is the worker shell's current directory (the PreToolUse hook reports it)."""
    tool_input = tool_input or {}
    if tool_name in SHELL_TOOLS:
        if tool_input.get("dangerouslyDisableSandbox"):
            return Decision.deny("sandbox-escape", "Running a command outside the OS sandbox (dangerouslyDisableSandbox) is never allowed; run it sandboxed.")
        return evaluate_bash(str(tool_input.get("command", "")), ctx, cwd=_shell_cwd(cwd, ctx), background=bool(tool_input.get("run_in_background")))
    if tool_name == "SandboxNetworkAccess":
        host = str(tool_input.get("host") or "?")
        return Decision.deny("sandbox-network", f"Network access to {host} is outside the sandbox's allowed domains (package registries); it is not allowed in an unattended run.")
    if tool_name in EDIT_TOOLS:
        return check_edit_path(_edit_target(tool_name, tool_input), ctx)
    if tool_name in READ_TOOLS:
        target = str(tool_input.get("file_path") or tool_input.get("path") or "")
        pattern = str(tool_input.get("pattern") or "") if tool_name == "Glob" else ""
        glob_ = str(tool_input.get("glob") or "")
        for t in (target, pattern, glob_):
            if not t:
                continue
            if is_credential_path(t) or (t == target and is_credential_path(os.path.realpath(_expand(t, ctx.repo)))):
                return Decision.deny("credentials", f"Reading credential/secret files is not allowed ({t}).")
        return Decision.ok("read")
    if tool_name.startswith("mcp__"):
        return _evaluate_mcp(tool_name, tool_input, ctx)
    if tool_name in SAFE_TOOLS:
        return Decision.ok("tool")
    return Decision.deny("unknown-tool", f"The {tool_name} tool is not allowed in this unattended pipeline.")


def _shell_cwd(cwd: str | Path | None, ctx: PolicyContext) -> Path | None:
    """The hook's cwd, used to resolve relative script paths; ignored unless it is the repo (or inside it) or a temp dir."""
    if not cwd:
        return None
    p = Path(os.path.expanduser(str(cwd)))
    if rel_to_repo(str(p), ctx.repo) is not None or os.path.realpath(str(p)).startswith(_TMP_PREFIXES):
        return p
    return None


def is_linear_tool(tool_name: str, ctx: PolicyContext) -> bool:
    return tool_name.startswith(ctx.linear_tool_prefix) or bool(re.match(r"^mcp__.*linear.*__", tool_name, re.I))


def linear_tool_is_read(tool_name: str) -> bool:
    short = tool_name.rsplit("__", 1)[-1]
    return short.startswith(_LINEAR_READ_VERBS)


def _evaluate_mcp(tool_name: str, tool_input: dict[str, Any], ctx: PolicyContext) -> Decision:
    short = tool_name.rsplit("__", 1)[-1]
    if is_linear_tool(tool_name, ctx) and not ctx.linear_enabled:
        return Decision.deny("linear-off", "Linear is off for this run (linear = off): do not read or write Linear.")
    if is_linear_tool(tool_name, ctx) and not tool_name.startswith(ctx.linear_tool_prefix):
        # another Linear server (e.g. the claude.ai connector) may be a DIFFERENT workspace: never touch it
        return Decision.deny(
            "linear-wrong-server",
            f"Only the configured Linear server ({ctx.linear_tool_prefix}*) may be used; {tool_name} may point at a different workspace.",
        )
    if is_linear_tool(tool_name, ctx):
        if linear_tool_is_read(tool_name):
            return Decision.ok("linear-read")
        if _LINEAR_NEVER.search(short):
            return Decision.deny("linear-destructive", f"Linear {short} is never allowed in this pipeline.")
        allowed = _LINEAR_PHASE_WRITES.get(ctx.phase, set()) if (ctx.phase in ctx.linear_write_phases or ctx.phase == "halt_notice") else set()
        if ctx.phase in ctx.linear_write_phases and ctx.phase != "linear":
            allowed = _LINEAR_PHASE_WRITES["linear"]
        if short in allowed:
            if ctx.phase == "halt_notice" and tool_input.get("id"):
                return Decision.deny("linear-write-phase", "The halt notice may only create one new comment, not edit existing ones.")
            return Decision.ok("linear-write", f"Linear write {short}", log=True)
        return Decision.deny(
            "linear-write-phase",
            f"Linear writes ({short}) are only allowed during the close-out phase, and only {sorted(_LINEAR_PHASE_WRITES['linear'])} (current phase: {ctx.phase}).",
        )
    if _GENERIC_MCP_READ.match(short) and not _MCP_WRITE_VERB.search(short):
        return Decision.ok("mcp-read")
    return Decision.deny("mcp-write", f"MCP tool {tool_name} may change external state and is not allowed in this pipeline.")


# --------------------------------------------------------------------------------------
# SDK adapters
# --------------------------------------------------------------------------------------

AskHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class PolicyGate:
    """Stateful adapter holding the current phase, a logger and the AskUserQuestion handler."""

    ctx: PolicyContext
    log_event: Callable[..., Any] = lambda *a, **k: None
    ask_handler: AskHandler | None = None
    denials: list[dict[str, Any]] = field(default_factory=list)
    _logged: set[str] = field(default_factory=set)
    cwd: str | None = None  # the worker shell's cwd, as last reported by the PreToolUse hook (`cd` persists across calls)

    def set_phase(self, phase: str) -> None:
        if phase != self.ctx.phase:
            self.ctx.ran_scripts.clear()  # "a script that ran here may not be rewritten here" holds per phase
        self.ctx.phase = phase

    def check(self, tool_name: str, tool_input: dict[str, Any], source: str) -> Decision:
        try:
            d = evaluate(tool_name, tool_input, self.ctx, cwd=self.cwd)
        except Exception as exc:  # fail closed: a policy bug must never become an allow
            d = Decision.deny("policy-error", f"The command could not be checked ({type(exc).__name__}: {exc}); rephrase it more simply.")
        key = f"{self.ctx.phase}:{tool_name}:{json.dumps(tool_input, sort_keys=True, default=str)[:2000]}"
        if not d.allow:
            if key not in self._logged:
                self._logged.add(key)
                rec = {
                    "phase": self.ctx.phase,
                    "tool": tool_name,
                    "command": _describe(tool_name, tool_input),
                    "rule": d.rule,
                    "reason": d.reason,
                    "source": source,
                }
                self.denials.append(rec)
                self.log_event("denial", **rec)
        elif d.log and key not in self._logged:
            self._logged.add(key)
            self.log_event("policy_allow_logged", phase=self.ctx.phase, tool=tool_name, command=_describe(tool_name, tool_input), rule=d.rule, note=d.reason)
        return d

    # PreToolUse hook: (input_data, tool_use_id, context) -> dict
    async def pre_tool_use_hook(self, input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        tool_name = str(input_data.get("tool_name", ""))
        tool_input = input_data.get("tool_input") or {}
        if input_data.get("cwd"):
            self.cwd = str(input_data["cwd"])
        if tool_name == "AskUserQuestion":
            return {}
        d = self.check(tool_name, tool_input, "hook")
        if d.allow:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"AUTOPILOT POLICY ({d.rule}): {d.reason}",
            }
        }

    # can_use_tool: (tool_name, input, context) -> PermissionResult
    async def can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        if tool_name == "AskUserQuestion":
            if self.ask_handler is None:
                return PermissionResultDeny(message="No decision agent available; answer with your recommended option and continue.")
            try:
                updated = await self.ask_handler(tool_input)
            except Exception as exc:  # pragma: no cover - defensive
                self.log_event("ask_handler_error", phase=self.ctx.phase, error=repr(exc))
                return PermissionResultDeny(message=f"Decision agent failed ({exc!r}); use the recommended option.")
            return PermissionResultAllow(updated_input=updated)
        d = self.check(tool_name, tool_input, "can_use_tool")
        if d.allow:
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(message=f"AUTOPILOT POLICY ({d.rule}): {d.reason}")


def _describe(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name in SHELL_TOOLS:
        return str(tool_input.get("command", ""))[:500]
    if tool_name in EDIT_TOOLS or tool_name in READ_TOOLS:
        return str(tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern") or "")
    return json.dumps(tool_input, default=str)[:300]


# --------------------------------------------------------------------------------------
# AskUserQuestion answer mapping
# --------------------------------------------------------------------------------------

_RECOMMENDED_RE = re.compile(r"\(?\s*recommended\s*\)?", re.I)


def map_answers(
    questions: list[dict[str, Any]],
    chosen: dict[str, Any],
    warn: Callable[[str], Any] = lambda m: None,
    free_text: dict[str, str] | None = None,
) -> dict[str, str]:
    """Map the judge's chosen labels onto the exact ``answers`` dict AskUserQuestion expects.

    ``chosen`` maps question text -> label (str) or labels (list[str]); option letters/indices are accepted.
    ``free_text`` maps question text -> a deliberate free-text answer (used only when no label was chosen).
    A label that matches no option falls back to the option marked Recommended, else the first option,
    and logs a warning (docs/DESIGN.md §4). Multi-select labels are joined with ", ".
    """
    answers: dict[str, str] = {}
    norm_chosen = {_norm(k): v for k, v in (chosen or {}).items()}
    norm_free = {_norm(k): v for k, v in (free_text or {}).items()}
    for q in questions or []:
        qtext = str(q.get("question", ""))
        options = [o for o in (q.get("options") or []) if isinstance(o, dict)]
        labels = [str(o.get("label", "")) for o in options]
        multi = bool(q.get("multiSelect"))
        raw = chosen.get(qtext) if chosen and qtext in chosen else norm_chosen.get(_norm(qtext))
        values = raw if isinstance(raw, list) else ([raw] if raw not in (None, "") else [])
        picked: list[str] = []
        unmatched: list[str] = []
        for v in values:
            m = _match_label(str(v), labels)
            if m:
                if m not in picked:
                    picked.append(m)
            elif str(v).strip():
                unmatched.append(str(v))
        if picked:
            answers[qtext] = ", ".join(picked if multi else picked[:1])
            if unmatched:
                warn(f"AskUserQuestion: ignored unknown label(s) {unmatched} for {qtext!r}")
            continue
        ft = (free_text or {}).get(qtext) or norm_free.get(_norm(qtext))
        if not values and ft and str(ft).strip():
            answers[qtext] = str(ft).strip()
            continue
        fallback = next(
            (labels[i] for i, o in enumerate(options) if _RECOMMENDED_RE.search(str(o.get("label", ""))) or re.match(r"(?i)^\s*\(?recommended", str(o.get("description", "")))),
            None,
        )
        reason = "the option marked Recommended"
        if fallback is None and labels:
            fallback, reason = labels[0], "the first option"
        if fallback is None:
            fallback, reason = (str(ft).strip() if ft else "Proceed with your recommended default."), "free-text default"
        warn(f"AskUserQuestion: no valid judge label for {qtext!r} (got {raw!r}); falling back to {reason}: {fallback!r}")
        answers[qtext] = fallback
    return answers


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _match_label(value: str, labels: list[str]) -> str | None:
    v = _norm(value)
    for l in labels:
        if _norm(l) == v:
            return l
    stripped = _norm(_RECOMMENDED_RE.sub("", value))
    for l in labels:
        if _norm(_RECOMMENDED_RE.sub("", l)) == stripped and stripped:
            return l
    # option letter / index ("A", "Option B", "2")
    m = re.match(r"^(?:option\s+)?([a-z]|\d+)[.):]?$", v)
    if m and labels:
        key = m.group(1)
        idx = (ord(key) - ord("a")) if key.isalpha() else int(key) - 1
        if 0 <= idx < len(labels):
            return labels[idx]
    return None
