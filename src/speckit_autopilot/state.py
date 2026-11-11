"""Checkpoint, run lock, event log, transcripts and secret redaction.

Everything the supervisor persists lives under ``<repo>/.autopilot/`` (which is
added to ``.git/info/exclude`` so it never dirties the tree). Every write goes
through :func:`redact` first.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import re
import socket
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

AUTOPILOT_DIR = ".autopilot"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def today_iso() -> str:
    return _dt.date.today().isoformat()


# --------------------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------------------

_REDACTED = "[REDACTED]"

# (pattern, replacement) pairs, applied in order. Keep them conservative: false
# positives only cost readability of a log line, false negatives leak secrets.
_REDACTION_RULES: list[tuple[re.Pattern[str], str]] = [
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), _REDACTED),
    # Authorization / auth headers (keep the header name)
    (
        re.compile(r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key|x-auth-token)\s*[:=]\s*(?:\"|')?(?!\[REDACTED\])(?:(?:bearer|basic|token|digest)\s+)?[^\s\"',}]+"),
        r"\1: " + _REDACTED,
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer " + _REDACTED),
    # Vendor token formats
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"), _REDACTED),
    (re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{20,}"), _REDACTED),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), _REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), _REDACTED),
    (re.compile(r"\blin_(?:api|oauth)_[A-Za-z0-9]{20,}"), _REDACTED),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), _REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), _REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), _REDACTED),
    (re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"), _REDACTED),
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), _REDACTED),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), _REDACTED),
    # URLs with embedded credentials: scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"), r"\1" + _REDACTED + "@"),
    # .env style assignments of secret-looking keys: FOO_SECRET=..., DB_PASSWORD="..."
    (
        re.compile(
            r"(?im)^(\s*(?:export\s+)?[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|PWD|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY|CREDENTIALS?|DATABASE_URL|DSN|WEBHOOK)[A-Z0-9_]*\s*=\s*)(.+)$"
        ),
        r"\1" + _REDACTED,
    ),
    # the same assignments in the middle of a line (logs, shell commands)
    (
        re.compile(
            r"\b([A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY|CREDENTIALS?|DATABASE_URL)[A-Z0-9_]*\s*=\s*)(?!\[REDACTED\])(\"[^\"]*\"|'[^']*'|[^\s\"']+)"
        ),
        r"\1" + _REDACTED,
    ),
    # query-string credentials: ?access_token=..., &api_key=..., &sig=...
    (re.compile(r"(?i)([?&](?:access_token|refresh_token|id_token|token|api_?key|apikey|key|secret|password|passwd|sig|signature|client_secret)=)(?!\[REDACTED\])[^&\s#'\"]+"), r"\1" + _REDACTED),
    # YAML / log / camelCase / lowercase assignments: password: x, apiKey = "x", api_token=x
    (
        re.compile(
            r"(?i)\b((?:[a-z0-9_]*[_-])?(?:password|passwd|secret|client_secret|api[_-]?key|apikey|api[_-]?token|access[_-]?token|auth[_-]?token|refresh[_-]?token|private[_-]?key)\b\s*[:=]\s*)(?!\[REDACTED\])(\"[^\"\n]{3,}\"|'[^'\n]{3,}'|[^\s\"',;]{3,})"
        ),
        r"\1" + _REDACTED,
    ),
    # JSON-ish "password": "..." / "token": "..."
    (
        re.compile(r'(?i)("(?:[a-z0-9_]*?)(?:secret|token|password|passwd|api_?key|access_?key|private_?key|client_secret)"\s*:\s*")[^"]{4,}(")'),
        r"\1" + _REDACTED + r"\2",
    ),
]


def redact(text: str) -> str:
    """Remove secrets from ``text``. Idempotent."""
    if not text:
        return text
    out = text
    for pat, repl in _REDACTION_RULES:
        out = pat.sub(repl, out)
    return out


_SECRET_KEY = re.compile(r"(?i)(secret|token|passw(?:or)?d|api_?key|access_?key|private_?key|credential|database_url|\bdsn\b|authorization|cookie)")
_NOT_SECRET_KEY = re.compile(r"(?i)(_tokens|tokens_|max_?tokens|token_?count|input_tokens|output_tokens|cache_\w*tokens|tokenizer|api_?key_?source|key_source)")


def redact_obj(obj: Any) -> Any:
    """Recursively redact every string inside a JSON-like object; values under secret-named keys are dropped."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k)
            if isinstance(v, str) and v and _SECRET_KEY.search(ks) and not _NOT_SECRET_KEY.search(ks):
                out[k] = _REDACTED
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunPaths:
    repo: Path

    @property
    def root(self) -> Path:
        return self.repo / AUTOPILOT_DIR

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def lock(self) -> Path:
        return self.root / "lock"

    @property
    def events(self) -> Path:
        return self.root / "run.jsonl"

    @property
    def transcripts(self) -> Path:
        return self.root / "transcripts"

    @property
    def report(self) -> Path:
        return self.root / "REPORT.md"

    @property
    def decisions_jsonl(self) -> Path:
        return self.root / "decisions.jsonl"

    @property
    def post_ship_decisions(self) -> Path:
        return self.root / "decisions-post-ship.md"

    @property
    def console(self) -> Path:
        return self.root / "console.log"

    @property
    def stop_request(self) -> Path:
        return self.root / "STOP"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.transcripts.mkdir(parents=True, exist_ok=True)
        register_repo(self.repo)


def register_repo(repo: Path) -> None:
    """Record ``repo`` in the user-level run registry, so the status line and ``--watch --all`` can find its runs."""
    import hashlib

    from .config import user_state_dir

    try:
        reg = user_state_dir() / "runs"
        reg.mkdir(parents=True, exist_ok=True)
        path = str(Path(repo).resolve())
        (reg / hashlib.sha1(path.encode()).hexdigest()[:16]).write_text(path + "\n")
    except OSError:
        pass  # the registry is a convenience for the live view; never fail a run over it


def ensure_git_exclude(repo: Path, entry: str = f"{AUTOPILOT_DIR}/") -> bool:
    """Add ``entry`` to ``.git/info/exclude`` (works for worktrees too). Returns True if added."""
    git_dir = repo / ".git"
    if git_dir.is_file():  # worktree: "gitdir: /path/to/.git/worktrees/x"
        content = git_dir.read_text().strip()
        if content.startswith("gitdir:"):
            wt = Path(content.split(":", 1)[1].strip())
            if not wt.is_absolute():
                wt = (repo / wt).resolve()
            common = wt / "commondir"
            git_dir = (wt / common.read_text().strip()).resolve() if common.exists() else wt
    if not git_dir.is_dir():
        return False
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text().splitlines() if exclude.exists() else []
    if entry in existing or entry.rstrip("/") in existing:
        return False
    with exclude.open("a") as fh:
        if existing and existing[-1] != "":
            fh.write("\n")
        fh.write(f"# spec-kit autopilot runtime state\n{entry}\n")
    return True


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------------------
# Run lock
# --------------------------------------------------------------------------------------


class LockHeld(RuntimeError):
    pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


@dataclass
class RunLock:
    path: Path
    pid: int = field(default_factory=os.getpid)

    def read(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def holder_alive(self) -> bool:
        info = self.read()
        return bool(info) and pid_alive(int(info.get("pid", -1)))

    def acquire(self, run_id: str) -> dict[str, Any] | None:
        """Take the lock. Raises LockHeld if a live process holds it.

        Returns the stale lock info if one was replaced (so the caller can log it).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pid": self.pid, "started_at": now_iso(), "run_id": run_id, "host": socket.gethostname()}
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                info = self.read()
                if info and int(info.get("pid", -1)) == self.pid:
                    return None  # re-entrant
                if info and pid_alive(int(info.get("pid", -1))) and info.get("host", socket.gethostname()) == socket.gethostname():
                    raise LockHeld(
                        f"Another autopilot run holds {self.path} (pid {info.get('pid')}, started {info.get('started_at')}, run {info.get('run_id')})."
                    )
                # stale: atomically swap in our lock, then re-read to make sure we won any race
                stale = info
                tmp = self.path.with_name(f".{self.path.name}.{self.pid}.tmp")
                tmp.write_text(json.dumps(payload))
                os.replace(tmp, self.path)
                time.sleep(0.05)
                again = self.read()
                if not again or int(again.get("pid", -1)) != self.pid:
                    raise LockHeld(f"Another autopilot run took {self.path} at the same time (pid {again.get('pid') if again else '?'}).")
                return stale or {"stale": True}
            else:
                with os.fdopen(fd, "w") as fh:
                    json.dump(payload, fh)
                return None

    def release(self) -> None:
        info = self.read()
        if info and int(info.get("pid", -1)) == self.pid:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------------------
# Event log + transcripts
# --------------------------------------------------------------------------------------


class EventLog:
    """Append-only JSONL event log. Every record is redacted before it hits disk."""

    def __init__(self, path: Path, run_id: str = "", echo: Any = None):
        self.path = path
        self.run_id = run_id
        self.echo = echo  # optional callable(str) for console output
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        rec = {"ts": now_iso(), "run_id": self.run_id, "event": event, **fields}
        rec = redact_obj(_jsonable(rec))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if self.echo:
            summary = rec.get("summary") or rec.get("reason") or rec.get("phase") or ""
            self.echo(f"[{rec['ts']}] {event} {summary}".rstrip())
        return rec

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out


class TranscriptWriter:
    """Full per-session transcript (JSONL, redacted)."""

    def __init__(self, directory: Path, label: str):
        directory.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        self.path = directory / f"{safe}.jsonl"

    def write(self, kind: str, payload: Any) -> None:
        rec = redact_obj(_jsonable({"ts": now_iso(), "kind": kind, "payload": payload}))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


# --------------------------------------------------------------------------------------
# Checkpoint state
# --------------------------------------------------------------------------------------


@dataclass
class PhaseRecord:
    status: str = "pending"  # pending | running | passed | failed | skipped | already_satisfied
    attempts: int = 0  # number of times the phase body was (re)entered (includes back-edge re-runs)
    repairs: int = 0  # repair rounds used in the current entry
    started_at: str | None = None
    ended_at: str | None = None
    duration_s: float = 0.0
    cost_usd: float = 0.0
    last_failed_checks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    artifact_hashes: dict[str, str] = field(default_factory=dict)  # fingerprints at pass time (idempotency)


@dataclass
class RunState:
    run_id: str
    issue: str
    description: str
    repo: str
    base_branch: str = "main"
    feature_branch: str | None = None
    feature_dir: str | None = None
    current_phase: str | None = None
    status: str = "running"  # running | halted | completed | stopped
    started_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    elapsed_s: float = 0.0  # accumulated wall clock across resumes
    total_cost_usd: float = 0.0
    phases: dict[str, PhaseRecord] = field(default_factory=dict)
    back_edges: list[dict[str, Any]] = field(default_factory=list)
    resets: list[dict[str, Any]] = field(default_factory=list)
    escalations: list[dict[str, Any]] = field(default_factory=list)
    model_switches: list[dict[str, Any]] = field(default_factory=list)  # model_mode=auto: worker model/effort changes
    decisions_frozen: bool = False
    ship: dict[str, Any] = field(default_factory=dict)  # started, pr_url, merge_commit, ci, ...
    linear: dict[str, Any] = field(default_factory=dict)  # state_done, comment_done, overview_done, ...
    halt: dict[str, Any] | None = None
    flags: dict[str, Any] = field(default_factory=dict)  # allow_escalations, until, ...

    def phase(self, name: str) -> PhaseRecord:
        if name not in self.phases:
            self.phases[name] = PhaseRecord()
        return self.phases[name]

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunState":
        d = dict(d)
        pr_fields = {f.name for f in dataclasses.fields(PhaseRecord)}
        phases = {k: PhaseRecord(**{fk: fv for fk, fv in v.items() if fk in pr_fields}) for k, v in (d.pop("phases", {}) or {}).items()}
        known = {f.name for f in dataclasses.fields(cls)}
        st = cls(**{k: v for k, v in d.items() if k in known})
        st.phases = phases
        return st


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> RunState:
        return RunState.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, state: RunState) -> None:
        state.updated_at = now_iso()
        atomic_write(self.path, json.dumps(redact_obj(state.to_dict()), indent=2, ensure_ascii=False) + "\n")
