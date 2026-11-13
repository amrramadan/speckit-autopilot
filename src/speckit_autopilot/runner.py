"""The supervisor state machine (docs/DESIGN.md §3–§4).

run → gate → repair → route → advance or halt, with context resets between session
groups, fresh-session verification, idempotent re-entry (--resume), budgets, a ship
gate, Linear close-out and graceful stop. All AI access goes through the
``SessionFactory`` (workers) and ``Judge`` interfaces so the whole machine runs with
scripted fakes in tests.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import os
import re
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from . import gates as G
from . import repo as R
from .config import CONTRACT_ENV, PHASES, SHIP_AUTHORIZATION, Config, looks_like_issue_key, resolve_linear
from .judge import Judge, JudgeRequest, JudgeResult
from .linear import LinearSnapshot, read_linear
from .models import WorkerChoice, accept_recommendation, choose
from .phases import PHASE_SPECS, SESSION_GROUPS, check_back_edge, downstream, index
from .policy import Decision, PolicyContext, PolicyGate, map_answers
from .prompts import (
    ANALYZE_TRAILER_REQUEST,
    IMPLEMENT_ARGS,
    IMPLEMENT_CONTINUE_ARGS,
    ONLY_LISTED_PARAMS,
    REVIEW_TRAILER_REQUEST,
    WORKER_SYSTEM_APPEND,
    RunContext,
    halt_comment_text,
    handoff_brief,
    human_tasks_note,
    linear_closeout_prompt,
    repair_message,
    sanitize_reply,
    ship_resume_message,
)
from .repo import CmdResult, Git, RepoProfile, detect_profile, gh, kill_running_commands, run_shell
from .report import DecisionLog, notify, resume_command, write_final_report, write_halt_report
from .sessions import SessionFactory, SessionSpec, TurnResult, WorkerSession, sandbox_available, sandbox_settings, tooling_sandbox_available, tooling_sandbox_profile
from .state import EventLog, RunLock, RunPaths, RunState, StateStore, ensure_git_exclude, now_iso, today_iso

T = TypeVar("T")


class Halt(Exception):
    def __init__(self, reason: str, failed: list[str] | None = None, priority: str = "normal", phase: str | None = None, notify_linear: bool = True):
        super().__init__(reason)
        self.notify_linear = notify_linear
        self.reason = reason
        self.failed = failed or []
        self.priority = priority
        self.phase = phase


class StopRequested(Exception):
    pass


@dataclass
class Conversation:
    status: str  # done | failed | turn_cap | reset
    text: str = ""
    judge: JudgeResult | None = None
    reason: str = ""
    turns: int = 0


@dataclass
class PhaseResult:
    passed: bool
    failed: list[str] = field(default_factory=list)
    worker_text: str = ""
    halt: Halt | None = None
    routed_to: str | None = None  # the phase already routed back (early route check); the loop jumps there
    # analyze/review: the last round's deterministic gate passed and only judge-requested work was left. A judge
    # "forward" may then advance (these phases cannot be re-gated without a fresh run)
    gate_passed: bool = False


@dataclass
class RunOutcome:
    status: str  # completed | halted | stopped | paused
    reason: str = ""
    report: Path | None = None


# halts that another attempt cannot fix: a resume would hit the same wall at once
AUTO_RESUME_NEVER = ("wrong workspace", "budget", "wall-clock limit")


def auto_resumable(reason: str) -> bool:
    r = (reason or "").lower()
    return not any(x in r for x in AUTO_RESUME_NEVER)


def run_with_auto_resume(cfg: Config, make_runner: Callable[[Config], "Runner"], echo: Callable[[str], None] = print) -> RunOutcome:
    """Run, and resume a halted run by itself up to ``cfg.auto_resume`` times. Each resume re-runs the halted phase
    with fresh repair rounds (the state, decisions and back-edge counts carry over). Stops early on a halt no retry
    can fix (budget, wall-clock, wrong Linear workspace) or one that repeats word for word."""
    runner = make_runner(cfg)
    outcome = asyncio.run(runner.run())
    last = None
    for n in range(1, max(0, cfg.auto_resume) + 1):
        if outcome.status != "halted" or not auto_resumable(outcome.reason) or outcome.reason == last:
            break
        last = outcome.reason
        echo(f"\nAutopilot halted: {outcome.reason}\nAuto-resume {n}/{cfg.auto_resume}: resuming the run.")
        if getattr(runner, "log", None) is not None:
            runner.log.emit("auto_resume", attempt=n, max=cfg.auto_resume, reason=outcome.reason[:500], summary=f"auto-resume {n}/{cfg.auto_resume} after: {outcome.reason[:160]}")
        cfg = dataclasses.replace(cfg, resume=True, from_phase=None)
        runner = make_runner(cfg)
        outcome = asyncio.run(runner.run())
    return outcome


def slugify(text: str, words: int = 4) -> str:
    stop = {"a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with", "add", "new", "implement", "support", "create", "make", "by", "via", "into"}
    toks = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in stop]
    return "-".join(toks[:words]) or "feature"


CHECKLIST_OPEN = re.compile(r"(?m)^[ \t]*[-*+][ \t]+\[ \]")
CHECKLIST_DONE = re.compile(r"(?m)^[ \t]*[-*+][ \t]+\[[xX]\]")


def open_checklist_items(feature_dir: Path) -> list[tuple[str, int, int]]:
    """(path relative to the feature dir, open items, all items) for every checklists/*.md with an unchecked "- [ ]"
    item — the same items speckit-implement counts before it asks "proceed anyway?"."""
    d = feature_dir / "checklists"
    out: list[tuple[str, int, int]] = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.md")):
        text = G.without_code_fences(G.read(p))
        n_open, n_done = len(CHECKLIST_OPEN.findall(text)), len(CHECKLIST_DONE.findall(text))
        if n_open:
            out.append((p.relative_to(feature_dir).as_posix(), n_open, n_open + n_done))
    return out


_RELABELLED = " (its human-only label was removed)"

_FRONT_MATTER = re.compile(r"\A\ufeff?---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)


def linear_front_matter(text: str) -> dict[str, str]:
    """The ``linear:`` mapping of a spec.md YAML front matter, in either shape the repos use
    (``linear:`` + indented ``issueId: ENG-1366`` / ``shared: true`` lines, or ``linear: { issueId: ENG-94 }``), with
    values unquoted. Empty when there is none. Deliberately tiny: no YAML dependency, and only these shapes occur."""
    m = _FRONT_MATTER.match(text or "")
    if not m:
        return {}
    lines = m.group(1).splitlines()
    for i, line in enumerate(lines):
        km = re.match(r"^linear\s*:\s*(.*?)\s*$", line)
        if not km:
            continue
        rest = km.group(1)
        if rest.startswith("{"):
            pairs = rest.strip("{} \t").split(",")
        else:
            pairs = []
            for sub in lines[i + 1:]:
                if sub.strip() and not sub[:1].isspace():
                    break  # the next top-level key
                if sub.strip() and not sub.lstrip().startswith("#"):
                    pairs.append(sub.strip())
        out: dict[str, str] = {}
        for p in pairs:
            if ":" in p:
                k, v = p.split(":", 1)
                out[k.strip().strip("'\"")] = v.split(" #", 1)[0].strip().strip("'\"")
        return out
    return {}


def _truthy(v: Any) -> bool:
    return str(v or "").strip().lower() in ("true", "yes", "on", "1")


def unescape_status_markers(project: dict[str, Any] | None) -> dict[str, Any] | None:
    """Linear re-serialises Markdown on save and escapes literal brackets ("[X]" read back as "\\[X\\]", seen live in
    both workspaces), so the visible fallback markers may come back as "\\[autopilot:status\\]". Undo exactly that
    before the gate looks for the status block."""
    if not project:
        return project
    out = dict(project)
    for key in ("description", "content"):
        if isinstance(out.get(key), str):
            out[key] = (
                out[key]
                .replace("\\[/autopilot:status\\]", G.VISIBLE_END)
                .replace("\\[\\/autopilot:status\\]", G.VISIBLE_END)
                .replace("\\[autopilot:status\\]", G.VISIBLE_START)
            )
    return out


@dataclass
class SharedIssueGate(PolicyGate):
    """The close-out's policy gate on a shared Linear issue: everything the normal policy allows, except saving the
    issue itself (status, title, description, labels), which belongs to the people who share it."""

    issue: str = ""

    def check(self, tool_name: str, tool_input: dict[str, Any], source: str) -> Decision:
        if tool_name.startswith(self.ctx.linear_tool_prefix) and tool_name.rsplit("__", 1)[-1] == "save_issue":
            d = Decision.deny("shared-issue", f"{self.issue} is a shared Linear issue: its status, title, description and labels are never changed by the pipeline. Only comment and update the project status block.")
            key = f"{self.ctx.phase}:{tool_name}:shared"
            if key not in self._logged:
                self._logged.add(key)
                rec = {"phase": self.ctx.phase, "tool": tool_name, "command": json.dumps(tool_input, default=str)[:300], "rule": d.rule, "reason": d.reason, "source": source}
                self.denials.append(rec)
                self.log_event("denial", **rec)
            return d
        return super().check(tool_name, tool_input, source)


class Runner:
    def __init__(
        self,
        cfg: Config,
        *,
        session_factory: SessionFactory,
        judge: Judge,
        tool_runner: Callable[[str, Path, float], CmdResult] | None = None,
        gh_runner: Callable[..., CmdResult] = gh,
        today: Callable[[], str] = today_iso,
        sleep: Callable[[float], Any] = time.sleep,
        echo: Callable[[str], Any] | None = None,
        profile: RepoProfile | None = None,
        install_signal_handlers: bool = True,
    ):
        self.cfg = cfg
        self.repo = cfg.repo
        self.paths = RunPaths(cfg.repo)
        self.make_session = session_factory
        self.judge = judge
        self.tool_runner = tool_runner or self._run_tool
        self._tooling_unavailable_logged = False
        self.gh = gh_runner
        self.today = today
        self.sleep = sleep
        self.echo = echo
        self._profile = profile
        self.install_signal_handlers = install_signal_handlers
        self.session: WorkerSession | None = None
        self.session_group: str | None = None
        self.reset_pending: str | None = None
        self.stop_requested = False
        self.current_phase: str | None = None
        self.last_worker_text = ""
        self.last_judge: dict[str, Any] | None = None
        self._t0 = time.monotonic()
        self._elapsed_base = 0.0
        self.force_rerun: set[str] = set()
        self.history: list[str] = []
        self._session_seq = 0  # makes session labels (and transcript file names) unique within a run
        self._route_note: list[str] = []
        self._signal_tasks: set[Any] = set()
        # stop machinery (request_stop): the loop the run lives on, an event tooling waits on, and the cancellable
        # in-flight calls (judge, Linear reader) a stop cancels at once instead of waiting up to judge_timeout_min
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._inflight: set[asyncio.Future[Any]] = set()
        self._prev_signals: dict[int, Any] = {}
        self._stop_source: str | None = None
        self.linear_on = True  # resolved per run in run() (_resolve_linear_mode)
        self._session_choice: WorkerChoice | None = None  # model / effort the live worker session was opened with

    # ==================================================================================
    # setup
    # ==================================================================================

    @property
    def profile(self) -> RepoProfile:
        if self._profile is None:
            cfg = self.cfg
            tooling = {"test": cfg.test_cmd, "lint": cfg.lint_cmd, "typecheck": cfg.typecheck_cmd, "build": cfg.build_cmd}
            self._profile = detect_profile(self.repo, cfg.commands, tooling, cfg.base_branch)
        return self._profile

    @property
    def base(self) -> str:
        return self.profile.base_branch

    def _resolve_linear_mode(self) -> None:
        """Decide whether this run uses Linear. An explicit ``linear = on|off`` (config or --linear/--no-linear) always
        wins; ``auto`` keeps the decision a resumed run recorded, so a run never switches Linear on halfway."""
        st = self.state
        recorded = st.flags.get("linear")
        if self.cfg.linear == "auto" and isinstance(recorded, dict) and "enabled" in recorded:
            self.linear_on = bool(recorded["enabled"])
            return
        on, why = resolve_linear(self.cfg)
        self.linear_on = on
        st.flags["linear"] = {"enabled": on, "mode": self.cfg.linear, "why": why}

    def _init_state(self) -> RunState:
        st = RunState(
            run_id=time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            issue=self.cfg.issue,
            description=self.cfg.description,
            repo=str(self.repo),
            base_branch=self.base,
        )
        # start_date: the local date the run began; clarify accepts a "### Session <date>" from then until today
        st.flags = {"allow_escalations": self.cfg.allow_escalations, "until": None if self.cfg.until == "none" else self.cfg.until, "start_date": self.today()}
        return st

    def _archive_previous(self) -> None:
        old = StateStore(self.paths.state).load()
        dest = self.paths.root / "archive" / old.run_id
        dest.mkdir(parents=True, exist_ok=True)
        for p in (self.paths.state, self.paths.events, self.paths.decisions_jsonl, self.paths.report, self.paths.post_ship_decisions):
            if p.exists():
                p.rename(dest / p.name)

    def ctx(self) -> RunContext:
        st = self.state
        return RunContext(
            issue=st.issue,
            description=st.description,
            repo=self.repo,
            base_branch=self.base,
            spec_id=st.flags.get("spec_id"),
            feature_branch=st.feature_branch,
            feature_dir=Path(st.feature_dir) if st.feature_dir else None,
            run_id=st.run_id,
            linear_title=st.linear.get("title"),
            repo_notes=self.cfg.judge_notes,
            decisions_path=(Path(st.feature_dir) / "decisions.md") if st.feature_dir else None,
        )

    @property
    def feature_dir(self) -> Path:
        if not self.state.feature_dir:
            raise Halt("feature directory unknown (feature_start has not run)")
        return Path(self.state.feature_dir)

    def save(self) -> None:
        self.state.elapsed_s = self._elapsed_base + (time.monotonic() - self._t0)
        StateStore(self.paths.state).save(self.state)

    # ==================================================================================
    # main loop
    # ==================================================================================

    async def run(self) -> RunOutcome:
        cfg = self.cfg
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if self.stop_requested:
            self._stop_event.set()
        self.paths.ensure()
        ensure_git_exclude(self.repo)
        lock = RunLock(self.paths.lock)
        stale = None
        store = StateStore(self.paths.state)
        resuming = cfg.resume or cfg.from_phase is not None
        if store.exists() and not resuming:
            prev = store.load()
            if prev.status in ("running", "halted", "stopped", "paused") and prev.issue != cfg.issue:
                raise SystemExit(
                    f"A previous autopilot run for {prev.issue} ({prev.status}) exists in {self.paths.state}. "
                    "Resume it with --resume, restart a phase with --from, or archive it by finishing/halting it first."
                )
            if prev.status in ("running", "halted", "stopped", "paused") and prev.issue == cfg.issue:
                raise SystemExit(f"A previous run for {prev.issue} is {prev.status}. Use --resume (or --from <phase>).")
        stale = lock.acquire("pending")
        try:
            if resuming and store.exists():
                self.state = store.load()
                self._elapsed_base = self.state.elapsed_s
                was_paused = self.state.status == "paused"
                # states from before founder pauses existed only ever paused for --until
                paused_by = self.state.flags.pop("paused_by", "until")
                self.state.status = "running"
                self.state.halt = None
                if self.state.ship.get("merged"):
                    self.state.decisions_frozen = True  # merged: later decisions go to .autopilot/ only, never into the repo
                if cfg.allow_escalations:
                    self.state.flags["allow_escalations"] = True
                if cfg.until == "none":  # run to the end: through ship (merge) and the Linear close-out
                    self.state.flags.pop("until", None)
                elif cfg.until:
                    self.state.flags["until"] = cfg.until
                elif was_paused and paused_by == "until":
                    # the requested --until stop already happened; a plain --resume continues past it. A founder
                    # pause (checklists, human-only tasks) is not that stop: the --until still applies after it.
                    self.state.flags.pop("until", None)
                self.force_rerun = set(self.state.flags.get("force_rerun", []))
            else:
                if store.exists():
                    self._archive_previous()
                self.state = self._init_state()
            lock.path.write_text(json.dumps({"pid": os.getpid(), "started_at": now_iso(), "run_id": self.state.run_id, "host": os.uname().nodename}))
            self._resolve_linear_mode()
            self.log = EventLog(self.paths.events, self.state.run_id, echo=self.echo)
            if stale:
                self.log.emit("stale_lock_replaced", stale=stale)
            self.decisions = DecisionLog(self.paths, self.state)
            if hasattr(self.judge, "log"):
                self.judge.log = self.log  # judge events carry the run id
            self.gate = PolicyGate(
                PolicyContext(
                    phase="feature_start",
                    repo=self.repo,
                    base_branch=self.base,
                    push_phases=tuple(cfg.push_phases),
                    linear_write_phases=tuple(cfg.linear_write_phases),
                    install_phases=tuple(cfg.install_phases),
                    protected_paths=tuple(cfg.protected_paths),
                    linear_tool_prefix=cfg.linear_tool_prefix,
                    linear_enabled=self.linear_on,
                    deny_bulk_add=cfg.deny_bulk_git_add,
                ),
                log_event=self._log_policy,
                ask_handler=self._answer_questions,
            )
            self.gate.ctx.base_sha = str(self.state.flags.get("start_sha") or "")  # a resume keeps the recorded base
            self.state.flags["sandbox"] = self._sandbox_info()
            self.log.emit("run_start", issue=cfg.issue, description=cfg.description, resume=cfg.resume, from_phase=cfg.from_phase, until=cfg.until, sandbox=self.state.flags["sandbox"], linear=self.state.flags.get("linear"), profile={"base": self.base, "commands": self.profile.commands, "tooling": self.profile.tooling, "speckit": self.profile.speckit_version})
            if resuming:
                self._accept_human_ticks_made_while_stopped()
            self.save()
            if self.install_signal_handlers:
                self._install_signals()
            return await self._main()
        finally:
            self._restore_signals()
            await self._close_session("run end")
            if self.stop_requested:
                await self._wait_tooling_gone()  # the lock outlives every command the run started
            try:
                self.save()
            except Exception:
                pass
            lock.release()

    def _sandbox_info(self) -> dict[str, Any]:
        """The OS sandbox mode and whether worker Bash commands are actually sandboxed on this machine."""
        ok, why = sandbox_available()
        mode = self.cfg.sandbox
        active = mode == "on" or (mode == "auto" and ok)
        return {"mode": mode, "active": active, "detail": why if mode != "off" else "off by configuration"}

    def _accept_human_ticks_made_while_stopped(self) -> None:
        """A human-only task ticked between a halt/stop/pause and this --resume was ticked by a person (no agent ran):
        accept it into the implement snapshot. Ticks made before the run stopped (or before a crash, when no
        stop snapshot exists) stay agent ticks and still fail human-tasks-untouched."""
        st = self.state
        at_stop = st.flags.pop("human_at_stop", None)
        before = st.flags.get("human_before")
        carry = st.flags.get("human_carry") or {}
        if not at_stop or (before is None and not carry) or not st.feature_dir:
            return
        was_open = (lambda i: before.get(i) is False) if before is not None else (lambda i: i in carry)
        accepted = [t.id for t in self._run_human_tasks() if t.done and at_stop.get(t.id) is False and was_open(t.id)]
        for i in accepted:
            if before is not None:
                before[i] = True
            else:
                carry.pop(i, None)
        if accepted:
            self.log.emit("human_tasks_ticked_while_stopped", tasks=accepted, summary=f"accepted {len(accepted)} human-only task(s) a person ticked while the run was stopped")

    def _skip_phase(self, phase: str, why: str) -> None:
        rec = self.state.phase(phase)
        if rec.status != "skipped":
            rec.status = "skipped"
            rec.ended_at = now_iso()
            self.log.emit("phase_end", phase=phase, status="skipped", summary=f"{phase} skipped: {why}")
            self.save()

    def _start_index(self) -> int:
        cfg = self.cfg
        if cfg.from_phase:
            i = index(cfg.from_phase)
            for p in PHASES[i:]:
                rec = self.state.phase(p)
                if rec.status in ("passed", "already_satisfied"):
                    rec.status = "pending"
                rec.artifact_hashes = {}
                (self.state.flags.get("early_checked") or {}).pop(p, None)  # a forced re-run is a new phase entry
                if p not in ("ship", "linear"):  # ship/linear keep their own never-twice idempotency checks
                    self.force_rerun.add(p)
            if i < index("ship") and not self.state.ship.get("merged"):
                self.state.decisions_frozen = False  # re-run decisions must reach the decisions.md that cc-ship commits
            if i <= index("implement"):
                # an operator restart of implement (or earlier) takes a new snapshot of the human-only tasks; a plain
                # --resume keeps the first one so a tick made before a crash is still caught
                for k in ("human_before", "human_keys", "human_carry"):
                    self.state.flags.pop(k, None)
            self._persist_force_rerun()
            return i
        if cfg.resume:
            for i, p in enumerate(PHASES):
                if self.state.phase(p).status not in ("passed", "already_satisfied"):
                    return i
            return len(PHASES)
        return 0

    async def _main(self) -> RunOutcome:
        idx = self._start_index()
        try:
            await self._resolve_identity()
            while idx < len(PHASES):
                phase = PHASES[idx]
                until = self.state.flags.get("until")
                if until and idx >= index(until):
                    return self._pause(f"--until {until}: stopped before {phase}")
                self._check_limits(include_phase=False)
                paused = self._founder_pause(phase)
                if paused is not None:
                    return paused
                if phase == "ship":
                    await self._ship_gate()
                if phase == "linear" and not self.linear_on:
                    self._skip_phase(phase, "Linear is off for this run (" + str((self.state.flags.get("linear") or {}).get("why", "linear = off")) + ")")
                    idx += 1
                    continue
                res = await self._run_phase(phase)
                if res.halt:
                    raise res.halt
                if res.routed_to:
                    idx = index(res.routed_to)
                    continue
                if res.passed:
                    idx += 1
                    continue
                target = await self._route(phase, res)
                if target is None:
                    idx += 1  # judge 'forward' and the gate re-check passed
                    continue
                idx = index(target)
            return self._complete()
        except StopRequested:
            src = self._stop_source or "a stop request"
            await self._wait_tooling_gone()
            return await self._halt(Halt(f"stopped by {src} (SIGTERM/SIGINT/--stop); state checkpointed, continue with --resume", phase=self.current_phase), status="stopped")
        except Halt as h:
            return await self._halt(h)
        except Exception as exc:  # SDK/CLI/OS errors: never die without a halt report
            import traceback

            self.log.emit("internal_error", phase=self.current_phase, error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc()[-4000:])
            return await self._halt(Halt(f"internal error: {type(exc).__name__}: {exc}", phase=self.current_phase, priority="high"))

    # ==================================================================================
    # identity (spec id, branch, feature dir) — plain code
    # ==================================================================================

    async def _resolve_identity(self) -> None:
        st = self.state
        cfg = self.cfg
        if st.linear.get("workspace") and cfg.linear_workspace_url and st.linear["workspace"].rstrip("/") != cfg.linear_workspace_url.rstrip("/"):
            raise Halt(f"state was recorded against Linear workspace {st.linear['workspace']}, config now expects {cfg.linear_workspace_url}", notify_linear=False)
        if st.feature_dir and st.feature_branch:
            return
        if not self.linear_on:
            pass  # no tracker: the spec id and slug come from --spec / the description alone
        elif not cfg.linear_workspace_url:
            raise Halt(
                f"linear_workspace_url is not set for {self.repo.name}. Issue keys such as {st.issue} exist in more than one Linear "
                "workspace, so the run refuses to read or write Linear until the expected workspace is pinned in .autopilot.toml "
                "(preflight --dry-run prints the workspace the configured server is connected to)."
            )
        if self.linear_on and not st.linear.get("read"):
            snap = await self._linear_read(project=None)
            if not snap.workspace_url or snap.workspace_url.rstrip("/") != cfg.linear_workspace_url.rstrip("/"):
                raise Halt(
                    f"Linear MCP {cfg.linear_server} is connected to {snap.workspace_url or 'an unknown workspace'}, expected {cfg.linear_workspace_url}. "
                    "Refusing to touch the wrong workspace.",
                    snap.errors,
                    notify_linear=False,
                )
            if snap.issue:
                st.linear.update(
                    read=True,
                    title=snap.issue.get("title"),
                    project=_name(snap.issue.get("project")) or snap.issue.get("projectId"),
                    project_id=snap.issue.get("projectId"),
                    team=_name(snap.issue.get("team")),
                    url=snap.issue.get("url"),
                    workspace=snap.workspace_url,
                    initial_status=snap.issue.get("status"),
                )
            else:
                st.linear["read_error"] = "; ".join(snap.errors) or "issue not found"
            if not snap.issue:
                raise Halt(f"Could not read Linear issue {st.issue} through {cfg.linear_server}: {st.linear.get('read_error')}")
            self.log.emit("linear_issue", title=st.linear.get("title"), project=st.linear.get("project"), workspace=snap.workspace_url, status=snap.issue.get("status"))
        spec_id = cfg.spec or st.flags.get("spec_id")
        if not spec_id and cfg.spec_title_regex and st.linear.get("title"):
            m = re.search(cfg.spec_title_regex, st.linear["title"])
            if m:
                spec_id = m.group(1)
        num = self._next_spec_number()
        if not spec_id:
            if cfg.spec_id_style == "number":
                spec_id = num
            else:
                raise Halt(f"This repo uses spec keys (spec_id_style=key) and none was given. Re-run with --spec <KEY> (e.g. --spec spec-ABC).")
        slug = slugify(cfg.description or st.linear.get("title") or st.issue, cfg.slug_words)
        fmt = {"spec": spec_id, "slug": slug, "num": num, "issue": st.issue, "issue_lower": st.issue.lower()}
        st.flags.update(spec_id=spec_id, slug=slug, num=num)
        # the base commit the feature branch will be cut from: lets ship_facts tell "merged" from "nothing committed yet"
        st.flags.setdefault("start_sha", Git(self.repo).head(f"origin/{self.base}") or Git(self.repo).head())
        if getattr(self, "gate", None) is not None:
            self.gate.ctx.base_sha = str(st.flags.get("start_sha") or "")  # scripts unchanged since then are trusted
        st.feature_branch = cfg.branch_template.format(**fmt)
        st.feature_dir = str(self.repo / cfg.feature_dir_template.format(**fmt))
        self.log.emit("identity", spec_id=spec_id, branch=st.feature_branch, feature_dir=st.feature_dir)
        self.save()

    def _next_spec_number(self) -> str:
        nums = [0]
        specs = self.repo / "specs"
        if specs.is_dir():
            for p in specs.iterdir():
                m = re.match(r"^(\d{3,})", p.name)
                if m:
                    nums.append(int(m.group(1)))
        out = Git(self.repo).out("branch", "-a", "--format=%(refname:short)")
        for b in out.splitlines():
            m = re.search(r"(?:^|/)(\d{3,})[a-z]?-", b)
            if m:
                nums.append(int(m.group(1)))
        return str(max(nums) + 1).zfill(self.cfg.spec_number_width)

    def _worker_env(self) -> dict[str, str]:
        st = self.state
        env = {**self.cfg.worker_env, **CONTRACT_ENV}  # AUTOPILOT=1 / AUTOPILOT_UNATTENDED=1 always win (DESIGN.md §10)
        if st.feature_branch:
            env["GIT_BRANCH_NAME"] = st.feature_branch
        if st.feature_dir:
            rel = Path(st.feature_dir).relative_to(self.repo).as_posix()
            env["SPECIFY_FEATURE_DIRECTORY"] = rel
            name = Path(st.feature_dir).name
            m = re.match(r"^(\d{3,})[a-z]-(.+)$", name)
            env["SPECIFY_FEATURE"] = f"{m.group(1)}-{m.group(2)}" if m else name  # lettered specs (023b-x) fail Spec Kit 0.7.1's branch regex
        return env

    # ==================================================================================
    # phases
    # ==================================================================================

    async def _run_phase(self, phase: str) -> PhaseResult:
        st = self.state
        rec = st.phase(phase)
        rec.attempts += 1
        rec.repairs = 0
        (st.flags.get("worker_reco") or {}).pop(phase, None)  # a new attempt starts from the phase's own model / effort
        rec.status = "running"
        rec.started_at = now_iso()
        t0 = time.monotonic()
        self.current_phase = phase
        st.current_phase = phase
        self.gate.set_phase(phase)
        self.log.emit("phase_start", phase=phase, attempt=rec.attempts, summary=f"{phase} attempt {rec.attempts}")
        self.save()
        try:
            pre = self._preconditions(phase)
            if not pre.passed:
                return PhaseResult(False, halt=Halt(f"preconditions for {phase} failed", pre.failed(), phase=phase))
            if phase not in self.force_rerun and await self._already_satisfied(phase):
                rec.status = "already_satisfied"
                self.log.emit("phase_end", phase=phase, status="already_satisfied", summary=f"{phase} already satisfied")
                return PhaseResult(True)
            rc = st.flags.get("route_context")
            self._route_note = [f"[routed back from {rc['from']}] {rc['reason']}", *rc.get("failed", [])[:12]] if rc and rc.get("to") == phase else []
            handler = getattr(self, f"_phase_{phase}")
            res: PhaseResult = await handler(rec)
            if res.routed_to:
                # the early route check already reset this phase (and the ones in between) to pending
                rec.last_failed_checks = res.failed
                self.log.emit("phase_end", phase=phase, status="routed_back", target=res.routed_to, failed=res.failed, summary=f"{phase} routed back to {res.routed_to} before any fix")
                return res
            rec.status = "passed" if res.passed else "failed"
            rec.last_failed_checks = res.failed
            if res.passed:
                rec.artifact_hashes = self._artifact_hashes(phase)
                (st.flags.get("early_checked") or {}).pop(phase, None)
                self.force_rerun.discard(phase)
                self._persist_force_rerun()
                if rc and rc.get("to") == phase:
                    st.flags.pop("route_context", None)
                self._route_note = []
            self.log.emit("phase_end", phase=phase, status=rec.status, failed=res.failed, summary=f"{phase} {rec.status}")
            await self._end_of_phase_boundary(phase)
            return res
        finally:
            rec.ended_at = now_iso()
            rec.duration_s += time.monotonic() - t0
            self.save()

    # ---------------------------------------------------------------- preconditions
    def _preconditions(self, phase: str) -> G.GateResult:
        r = G.GateResult(f"{phase}:pre")
        st = self.state
        prev = PHASES[index(phase) - 1] if index(phase) > 0 else None
        if phase == "feature_start":
            if st.feature_branch and Git(self.repo).current_branch() == st.feature_branch:
                r.add("resume-on-feature-branch", True, "already on the feature branch (resume)")
                return r
            return G.preconditions_feature_start(self.repo, self.base)
        if prev and st.phase(prev).status not in ("passed", "already_satisfied"):
            r.add("previous-phase", False, f"{prev} has not passed (status {st.phase(prev).status})")
        if phase == "clarify":
            r.add("spec-exists", (self.feature_dir / "spec.md").exists(), "spec.md exists")
        if phase not in ("ship", "linear") and st.feature_branch:
            cur = Git(self.repo).current_branch()
            r.add("on-feature-branch", cur == st.feature_branch, f"current branch {cur!r}, expected {st.feature_branch!r}")
        return r

    def _artifact_hashes(self, phase: str) -> dict[str, str]:
        if not self.state.feature_dir:
            return {}
        fd = self.feature_dir
        if phase in ("analyze",):
            return {"spec_plan_tasks": G.fingerprint([fd / "spec.md", fd / "plan.md", fd / "tasks.md"], self.repo)}
        if phase in ("review",):
            g = Git(self.repo)
            dirty = sorted(g.uncommitted_names())
            content = G.fingerprint([self.repo / p for p in dirty if (self.repo / p).is_file()], self.repo)
            return {"head": g.head(), "worktree": content + ":" + str(len(dirty)), "diff": str(hash(g.out("diff", "HEAD", "--stat")))}
        return {}

    async def _already_satisfied(self, phase: str) -> bool:
        """Idempotency: a phase whose exit gate AND Definition of Done already pass is recorded as satisfied (docs/DESIGN.md §6).
        The DoD is asked again here: a stop or crash during (or before) the phase's own judge check must not let a
        passing deterministic gate stand in for it."""
        st = self.state
        rec = st.phase(phase)
        try:
            gate: G.GateResult | None = None
            extra: Any = None
            if phase == "feature_start":
                gate = G.gate_feature_start(self.repo, self.base, self.feature_dir, st.feature_branch) if st.feature_dir else None
            elif phase == "clarify":
                gate = G.gate_clarify(self.feature_dir, self.today(), since=self._clarify_since())
            elif phase == "plan":
                gate = G.gate_plan(self.feature_dir)
            elif phase == "tasks":
                gate = G.gate_tasks(self.feature_dir, self.repo)
            elif phase in ("analyze", "review"):
                # analyze/review need a fresh run; skip only if nothing changed since they passed (DoD included)
                return bool(rec.artifact_hashes) and rec.artifact_hashes == self._artifact_hashes(phase)
            elif phase == "implement":
                done, total = G.task_progress(self.feature_dir)
                if total == 0 or done < total:
                    return False
                gate = await self._implement_gate()
            elif phase == "ship":
                facts = G.ship_facts(self.repo, self.base, st.feature_branch or "", gh_runner=self.gh, start_sha=st.flags.get("start_sha"))
                if not (facts.merged and facts.on_base and facts.base_synced and facts.clean):
                    return False
                st.ship.update(pr_url=facts.pr_url, pr_number=facts.pr_number, merged=True, merge_commit=facts.merge_commit)
                return (await self._ship_gate_check(facts)).passed  # ship has no judge DoD
            elif phase == "linear":
                snap = await self._linear_read(project=st.linear.get("project_id") or st.linear.get("project"))
                self._check_workspace(snap)
                gate = self._gate_linear(snap)
                extra = self._linear_verify_extra(snap, self._note_shared_issue())
            if gate is None or not gate.passed:
                return False
            failed = await self._verify(phase, gate, extra=extra)
            if failed:
                self.log.emit("idempotency_dod_failed", phase=phase, failed=failed, summary=f"{phase}: gate passes but the DoD does not; running the phase")
            return failed is None
        except (Halt, StopRequested):
            raise
        except Exception as exc:
            self.log.emit("idempotency_check_error", phase=phase, error=repr(exc))
        return False

    async def _end_of_phase_boundary(self, phase: str) -> None:
        """Session-group boundaries and pending context resets happen between phases."""
        group = PHASE_SPECS[phase].group
        nxt = PHASES[index(phase) + 1] if index(phase) + 1 < len(PHASES) else None
        if self.session is not None and (nxt is None or PHASE_SPECS[nxt].group != group):
            await self._close_session(f"end of session group {group}")
        elif self.session is not None and self.reset_pending:
            await self._reset(self.reset_pending)

    # ---------------------------------------------------------------- S1: feature_start
    async def _run_command(self, phase: str, purpose: str, command: str, failed: list[str] | None = None, **kw: Any) -> Conversation:
        """Send a phase command in the group session; after a context reset / compaction / turn cap, resend it
        once or twice in a fresh session (the files on disk carry the progress)."""
        conv = Conversation("failed", reason="not started")
        for attempt in range(3):
            sess = await self._group_session(phase, purpose if attempt == 0 else f"{purpose} (restarted after a reset)", failed=failed)
            conv = await self._converse(phase, sess, command, stop_on_reset=True, **kw)
            if conv.status not in ("reset", "turn_cap"):
                return conv
            await self._reset(self.reset_pending or conv.reason or "turn cap")
        return conv

    def _with_route_note(self, command: str) -> str:
        if not self._route_note:
            return command
        return command + " " + " ".join(self._route_note)[:1500]

    async def _phase_feature_start(self, rec) -> PhaseResult:
        st = self.state
        cmd = self._cmd("feature_start")
        fmt = self._fmt()
        args = self.cfg.feature_start_args.format(**fmt)
        await self._capture_test_baseline()  # the checkout is still the base commit: preconditions passed, no branch yet
        conv = await self._run_command("feature_start", "start the feature (create branch + spec)", f"{cmd} {args}".strip())
        # Skill chaining is impossible for disable-model-invocation skills: run specify ourselves if needed.
        if not (self.feature_dir / "spec.md").exists():
            spec_cmd = self._cmd("specify")
            self.log.emit("feature_start_specify_fallback", reason="spec.md missing after the feature-start command", command=spec_cmd)
            spec_args = self.cfg.specify_args
            if not looks_like_issue_key(st.issue) and spec_args == Config.specify_args:
                spec_args = "{description}"  # no tracker key: the issue label is just the description's slug
            conv = await self._run_command("feature_start", "run specify", f"{spec_cmd} {spec_args.format(**fmt)}".strip())
        return await self._gate_loop(
            "feature_start",
            rec,
            lambda: G.gate_feature_start(self.repo, self.base, self.feature_dir, st.feature_branch),
            conv,
        )

    async def _capture_test_baseline(self) -> None:
        """test_baseline = "base": run the test command once on the base commit and keep the failing test ids (sorted)
        plus the base SHA in state. Kept across resumes; only --from feature_start measures again."""
        st, cfg = self.state, self.cfg
        if cfg.test_baseline != "base":
            return
        have = st.flags.get("test_baseline")
        if have and "feature_start" not in self.force_rerun:
            return
        cmd = self.profile.tooling.get("test")
        g = Git(self.repo)
        if not cmd:
            st.flags["test_baseline"] = {"sha": g.head(), "cmd": None, "failures": None, "note": "no test command is configured, so nothing was measured"}
            self.log.emit("test_baseline_skipped", reason="no test command")
            return
        if g.current_branch() != self.base:
            if not have:
                st.flags["test_baseline"] = {"sha": None, "cmd": cmd, "failures": None, "note": f"feature_start did not start on {self.base}, so the base commit was never measured"}
            self.log.emit("test_baseline_skipped", reason=f"checkout is {g.current_branch()!r}, not {self.base!r}", kept=bool(have))
            return
        sha = g.head()
        dirty_before = set(g.status_porcelain())
        self.log.emit("test_baseline_start", cmd=cmd, sha=sha, summary=f"measuring pre-existing test failures on {self.base} {sha[:10]}")
        res = await self._in_thread(self._stoppable_tool_runner, cmd, self.repo, cfg.tooling_timeout_min * 60)
        failures = G.baseline_from_result(res)
        if failures is None:
            why = "the run timed out" if res.timed_out else G.explain_test_failures((res.stdout or "") + "\n" + (res.stderr or ""))[1]
            note = f"`{cmd}` failed on the base commit in a way that cannot be pinned to individual tests ({why})"
            self.log.emit("test_baseline_unparseable", cmd=cmd, sha=sha, rc=res.returncode, reason=why, tail=res.tail(15), summary=f"WARNING: test baseline unusable ({why}); gates stay strict")
        else:
            note = f"`{cmd}` exit {res.returncode} on the base commit"
        st.flags["test_baseline"] = {"sha": sha, "cmd": cmd, "rc": res.returncode, "failures": sorted(failures) if failures is not None else None, "note": note}
        self.log.emit("test_baseline", sha=sha, rc=res.returncode, failures=st.flags["test_baseline"]["failures"], summary=f"base {sha[:10]}: {len(failures) if failures is not None else '?'} pre-existing failing test(s)")
        if set(g.status_porcelain()) - dirty_before:
            self.log.emit("test_baseline_dirtied_tree", files=sorted(set(g.status_porcelain()) - dirty_before)[:20], summary="WARNING: the base test run left files in the working tree")
        self.save()

    def _clarify_since(self) -> str:
        """First accepted clarify session date: the run's start date (older states: the date in started_at)."""
        today = self.today()
        start = self.state.flags.get("start_date") or (self.state.started_at or "")[:10] or today
        return min(start, today)

    async def _phase_clarify(self, rec) -> PhaseResult:
        conv = await self._run_command("clarify", "clarify the spec", self._with_route_note(self._cmd("clarify")))
        nothing = G.clarify_reported_nothing(conv.text) or G.clarify_reported_nothing(self.last_worker_text)
        self.state.flags["clarify_nothing"] = nothing
        # today is read at gate time: a run that crosses midnight accepts a session dated since the run started
        res = await self._gate_loop("clarify", rec, lambda: G.gate_clarify(self.feature_dir, self.today(), nothing_to_clarify=self.state.flags.get("clarify_nothing", False), since=self._clarify_since()), conv)
        return res

    async def _phase_plan(self, rec) -> PhaseResult:
        conv = await self._run_command("plan", "write the implementation plan", self._with_route_note(self._cmd("plan")))
        return await self._gate_loop("plan", rec, lambda: G.gate_plan(self.feature_dir), conv)

    async def _phase_tasks(self, rec) -> PhaseResult:
        conv = await self._run_command("tasks", "generate tasks.md", self._with_route_note(self._cmd("tasks")))
        return await self._gate_loop("tasks", rec, lambda: G.gate_tasks(self.feature_dir, self.repo), conv)

    # ---------------------------------------------------------------- S2: analyze
    async def _phase_analyze(self, rec) -> PhaseResult:
        cmd = self._cmd("analyze")
        last_failed: list[str] = []
        last_text = ""
        while True:
            # fresh verification: a brand-new session runs analyze every round
            sess = await self._fresh_session("analyze", f"fresh analyze run (round {rec.repairs})")
            before = G.fingerprint([self.feature_dir], self.repo)
            turn = await self._send(sess, "analyze", cmd)
            if turn.timed_out or turn.is_error:
                await self._close_session("analyze turn failed")
                last_failed = [f"[conversation] the analyze run failed: {turn.subtype} {turn.errors[:1]}"]
                if rec.repairs >= self.cfg.max_repairs_per_phase:
                    return PhaseResult(False, last_failed, turn.reply_text)
                rec.repairs += 1
                continue
            text = turn.reply_text if not turn.text else turn.text + ("\n" + turn.final_text if turn.final_text and turn.final_text not in turn.text else "")
            last_text = text
            trailer = await self._send(sess, "analyze", ANALYZE_TRAILER_REQUEST)
            trailer_text = "" if (trailer.timed_out or trailer.is_error) else trailer.reply_text
            report = G.parse_analyze(text, trailer_text)
            read_only = G.fingerprint([self.feature_dir], self.repo) == before
            gate = G.gate_analyze(report, read_only_ok=read_only)
            self._log_gate(gate)
            if report.blocking and not self._early_done("analyze"):
                target = await self._early_route("analyze", gate.failed(), text + "\n\n" + trailer_text)
                if target:
                    return PhaseResult(False, gate.failed(), text, routed_to=target)
            jr = await self._respond("analyze", text + ("\n\n" + trailer_text if trailer_text else ""), facts={"parsed_counts": report.counts, "effective_counts": report.effective_counts, "trailer": report.trailer, "gate_failed": gate.failed(), "round": rec.repairs, "max_rounds": self.cfg.max_repairs_per_phase})
            if not jr.ok:
                await self._close_session("judge failure")
                return PhaseResult(False, ["judge output invalid twice"], text)
            judged = int(jr.data.get("blocking_issues_remaining", 0))
            if judged < report.blocking:
                self.log.emit("judge_disagreement", phase="analyze", judge_blocking=judged, parsed_blocking=report.blocking, summary=f"judge said {judged} blocking, table has {report.blocking}; using parsed count")
                self.decisions.add_note("analyze", f"Judge reported {judged} blocking issues but the parsed findings table has {report.blocking} CRITICAL+HIGH; the parsed count was used.")
            nonblocking = gate.passed and jr.status != "done"  # only LOW/MEDIUM left: the judge still wants edits
            if gate.passed and jr.status == "done":
                v = await self._verify("analyze", gate, extra=text[-20000:])
                if v is None:
                    await self._close_session("analyze verified")
                    return PhaseResult(True, worker_text=text)
                last_failed = ["[judge] " + x for x in v]
            elif not read_only:
                last_failed = gate.failed()
            else:
                last_failed = gate.failed() or ["judge requested fixes for remaining findings"]
            if rec.repairs >= self.cfg.max_repairs_per_phase:
                if nonblocking and not self.cfg.analyze_recheck_nonblocking:
                    # the gate passed (no CRITICAL/HIGH) and only LOW/MEDIUM fixes were asked for: with no round left
                    # they are accepted as they are, not a halt
                    self.decisions.add_note("analyze", f"Gate passed (no CRITICAL/HIGH); no repair round was left, so the remaining LOW/MEDIUM findings were accepted without the judge's fixes (round {rec.repairs}).")
                    self.log.emit("analyze_nonblocking_done", phase="analyze", round=rec.repairs, applied=False, summary="gate passed: no repair round left, remaining LOW/MEDIUM findings accepted, analyze done")
                    await self._close_session("non-blocking findings accepted")
                    self.save()
                    return PhaseResult(True, worker_text=text)
                await self._close_session("analyze repairs exhausted")
                return PhaseResult(False, last_failed, text, gate_passed=gate.passed and read_only)
            rec.repairs += 1
            approval = jr.reply if (jr.status == "needs_reply" and jr.reply.strip()) else self._forced_approval(report, last_failed)
            approval, warn = sanitize_reply(approval, [])
            if warn:
                self.log.emit("reply_sanitized", phase="analyze", warning=warn)
            self.log.emit("repair", phase="analyze", round=rec.repairs, failed=last_failed, summary=f"applying approved analyze edits (round {rec.repairs})")
            if self.reset_pending or self.session is not sess:
                if self.reset_pending:
                    await self._reset(self.reset_pending)
                sess = await self._fresh_session("analyze", "apply approved remediation", failed=last_failed)
            await self._converse("analyze:apply", sess, approval)
            if nonblocking and not self.cfg.analyze_recheck_nonblocking:
                # a fresh analyze of the edited docs finds new LOW items every pass (seen on BOOSTS-PAY-01 and spec-025:
                # repairs and back-edges ran out with the gate passing); implement and review still check the work
                self.decisions.add_note("analyze", f"Gate passed (no CRITICAL/HIGH); the judge's fixes for the remaining findings were applied once and not re-analyzed (round {rec.repairs}).")
                self.log.emit("analyze_nonblocking_done", phase="analyze", round=rec.repairs, summary="gate passed: remaining LOW/MEDIUM fixes applied once, analyze done")
                await self._close_session("non-blocking fixes applied")
                self.save()
                return PhaseResult(True, worker_text=text)
            self._mark_early_done("analyze")  # a fix was applied: only the after-repairs route remains
            await self._close_session("fixes applied; re-verify in a fresh session")
            self.save()

    def _forced_approval(self, report: G.AnalyzeReport, failed: list[str]) -> str:
        rows = [f for f in report.findings if f.severity in ("CRITICAL", "HIGH")]
        lines = ["Approved, apply these edits now:"]
        for i, f in enumerate(rows, 1):
            lines.append(f"{i}. Resolve {f.id} ({f.severity}, {f.category}): apply the recommendation from your report — {f.rest[:400]}")
        for x in failed:
            if not any(f.id in x for f in rows):
                lines.append(f"{len(lines)}. {x}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- S3: implement
    async def _phase_implement(self, rec) -> PhaseResult:
        st = self.state
        if "human_before" not in st.flags:
            # taken once, before the first implement session, and persisted: a resume keeps this first snapshot
            st.flags["human_before"] = self._take_human_snapshot()
            self.log.emit("human_tasks_snapshot", tasks=st.flags["human_before"])
            self.save()
        done, _total = G.task_progress(self.feature_dir)
        conv = await self._implement_sessions(first=done == 0)
        if self._route_note:
            # routed back (e.g. review found a code defect): every task may already be [X]; make the defect explicit
            sess = self.session if (self.session is not None and not self.reset_pending) else await self._fresh_session("implement", "fix what the later phase found", failed=self._route_note)
            conv = await self._converse("implement", sess, repair_message("implement", self._route_note, round_no=1, max_rounds=1) + self._human_note(), facts_fn=self._implement_facts)
        return await self._gate_loop("implement", rec, self._implement_gate, conv)

    def _run_human_tasks(self) -> list[G.Task]:
        """Tasks of tasks.md that are human-only for this run: labelled so now, in the implement snapshot (a label an
        agent rewrote away does not turn a person's task into agent work), or carried over a back-edge (see
        _apply_back_edge) when the task under that id is still the same task."""
        if not self.state.feature_dir:
            return []
        before = self.state.flags.get("human_before") or {}
        carry = self.state.flags.get("human_carry") or {}
        return [t for t in G.feature_tasks(self.feature_dir) if t.human or t.id in before or self._carried(t, carry)]

    @staticmethod
    def _carried(t: G.Task, carry: dict[str, str]) -> bool:
        key = carry.get(t.id)
        return key is not None and (t.human or (bool(key) and G.task_key(t.text) == key))

    def _pipeline_cmds(self) -> tuple[str, ...]:
        """The review and ship commands this run performs itself."""
        cmds = (self.profile.commands.get("review"), self.profile.commands.get("ship"))
        return tuple(c if c.startswith("/") else "/" + c for c in cmds if c)

    def _human_open(self) -> list[str]:
        """Open human-only tasks of tasks.md as "T### text" (empty before tasks.md exists). A task that is the pipeline's
        own review / ship step says so (G.pipeline_step): this run performs it, a person ticks it afterwards."""
        cmds = self._pipeline_cmds()
        out = []
        for t in self._run_human_tasks():
            if not t.done:
                step = G.pipeline_step(t, cmds)
                out.append(f"{t.id} {t.text[:100]}" + (f" (this run performs {' and '.join(cmds)}; tick it once the rest is done)" if step else ""))
        return out

    def _human_note(self) -> str:
        """Names the open human-only tasks, plus any that were open in the snapshot (so a repair names what was ticked)."""
        if not self.state.feature_dir:
            return ""
        before = self.state.flags.get("human_before") or {}
        return human_tasks_note([t.id for t in self._run_human_tasks() if not t.done or before.get(t.id) is False])

    def _take_human_snapshot(self) -> dict[str, bool]:
        """{id: done} of the human-only tasks before the first implement session. A task that was open in the snapshot
        a back-edge discarded stays open here even if it is ticked now: no person ticked it while the run was going
        (that is accepted only across a stop), so an agent's tick never becomes the new snapshot."""
        st = self.state
        carry = st.flags.pop("human_carry", None) or {}
        tasks = G.feature_tasks(self.feature_dir)
        snap = {t.id: t.done for t in tasks if t.human}
        for t in tasks:
            if self._carried(t, carry):
                snap[t.id] = False
        st.flags["human_keys"] = {t.id: G.task_key(t.text) for t in tasks if t.id in snap}
        return snap

    async def _implement_sessions(self, first: bool, failed: list[str] | None = None) -> Conversation:
        cmd = self._cmd("implement")
        no_progress = 0
        sessions = 0
        conv = Conversation("failed", reason="not started")
        while True:
            done_before, total = G.task_progress(self.feature_dir)
            sessions += 1
            if sessions > self.cfg.implement_max_sessions:
                return Conversation("failed", conv.text, reason=f"implement used {self.cfg.implement_max_sessions} sessions")
            purpose = "implement tasks" if first and sessions == 1 else "continue implement (unchecked tasks only)"
            sess = await self._fresh_session("implement", purpose, failed=failed)
            msg = f"{cmd} {IMPLEMENT_ARGS if (first and sessions == 1) else IMPLEMENT_CONTINUE_ARGS}{self._human_note()}"
            conv = await self._converse("implement", sess, msg, facts_fn=self._implement_facts, stop_on_reset=True)
            done_after, total = G.task_progress(self.feature_dir)
            if conv.status in ("reset", "turn_cap"):
                reason = self.reset_pending or "turn cap reached"
                await self._reset(reason)
                no_progress = no_progress + 1 if done_after <= done_before else 0
                if no_progress >= 2:
                    return Conversation("failed", conv.text, reason="two consecutive implement sessions made no progress on tasks.md")
                continue
            return conv

    def _implement_facts(self) -> dict[str, Any]:
        done, total = G.task_progress(self.feature_dir)  # agent tasks only
        return {"tasks_done": done, "tasks_total": total, "unchecked_remaining": total - done, "human_only_open": [h.split(" ", 1)[0] for h in self._human_open()]}

    async def _implement_gate(self) -> G.GateResult:
        done, total = G.task_progress(self.feature_dir)
        tooling = await self._run_tooling() if (total and done == total) else None
        g = G.gate_implement(self.repo, self.base, self.feature_dir, tooling, human_before=self.state.flags.get("human_before"))
        if tooling is None:
            g.add("tooling", False, "tooling not run because tasks remain unchecked")
        relabelled = [x for x in self._human_tampering() if x.endswith(_RELABELLED)]
        if relabelled:  # gate_implement only sees tasks that are still labelled human-only
            g.add("human-task-relabelled", False, f"human-only tasks must keep their label and stay unticked: {relabelled[:8]}")
        return g

    def _human_tampering(self) -> list[str]:
        """Human-only tasks open in the implement snapshot that are now ticked, removed, or no longer labelled
        human-only, ticked or not (a person ticks them only while the run is paused or stopped; nobody relabels
        them: an unlabelled open task would drop out of the ship pause, the report and the close-out)."""
        before = self.state.flags.get("human_before")
        if not before or not self.state.feature_dir:
            return []
        now = {t.id: t for t in G.feature_tasks(self.feature_dir)}
        out = []
        for i, was_done in before.items():
            t = now.get(i)
            if was_done:
                continue
            if t is None:
                out.append(f"{i} (removed from tasks.md)")
            elif t.done or not t.human:
                out.append(f"{i} {t.text[:80]}" + ("" if t.human else _RELABELLED))
        return out

    def _add_human_untouched_check(self, gate: G.GateResult) -> None:
        """Review fixes run after the implement gate: the same never-tick rule is checked again on the review gate."""
        bad = self._human_tampering()
        if self.state.flags.get("human_before"):
            gate.add("human-tasks-untouched", not bad, f"agents must never tick human-only tasks; ticked, removed or relabelled since implement started: {bad[:8]}" if bad else "no human-only task was ticked or removed")

    def _baseline_arg(self) -> dict[str, set[str] | None] | None:
        if self.cfg.test_baseline != "base":
            return None
        b = self.state.flags.get("test_baseline") or {}
        fails = b.get("failures")
        return {"test": set(fails) if fails is not None else None}  # None: base failures unknown, stays strict

    async def _run_tooling(self, stage: str | None = None) -> G.ToolingResult:
        """typecheck / lint / test / build in a worker thread (they can take an hour): the event loop stays free, so a
        stop kills the running command and returns at once, and no later command starts."""
        st = self.state
        stage = stage or self.current_phase or "tooling"
        cmds = dict(self.profile.tooling)
        sandboxed = self._tooling_sandboxed() if self.tool_runner == self._run_tool else None
        self.log.emit("tooling_start", commands=cmds, stage=stage, sandboxed=sandboxed)
        res = await self._in_thread(G.run_tooling, cmds, self.repo, self._stoppable_tool_runner, self.cfg.tooling_timeout_min * 60, baseline=self._baseline_arg())
        self.log.emit("tooling_end", ok=res.ok, stage=stage, results={k: {"rc": v.returncode, "s": round(v.duration_s)} for k, v in res.results.items()}, retried=res.retried, baseline_ok=res.baseline_ok, baseline_notes=res.baseline_notes)
        # the reports list every pre-existing failure a gate tolerated (latest per stage), and why a failure was not
        for key, src in (("baseline_used", res.baseline_ok), ("baseline_notes", res.baseline_notes)):
            if "test" in src:
                st.flags.setdefault(key, {})[stage] = src["test"]
            else:
                (st.flags.get(key) or {}).pop(stage, None)
        if res.baseline_ok:
            self.log.emit("test_baseline_applied", stage=stage, detail=res.baseline_ok.get("test"), summary=f"pre-existing test failures tolerated at {stage}")
        if self.stop_requested:
            raise StopRequested()
        return res

    def _tooling_sandboxed(self) -> bool:
        """Whether the supervisor's tooling runs under the OS sandbox (config tooling_sandbox; see sessions)."""
        mode = self.cfg.tooling_sandbox
        if mode == "off" or (mode == "auto" and self.cfg.sandbox == "off"):
            return False
        ok, why = tooling_sandbox_available()
        if ok:
            return True
        if mode == "on":
            raise Halt(f"tooling_sandbox = on but the supervisor's tooling cannot be sandboxed here ({why}); refusing to run worker code unsandboxed", priority="high")
        if not self._tooling_unavailable_logged and hasattr(self, "log"):
            self._tooling_unavailable_logged = True
            self.log.emit("tooling_sandbox_unavailable", reason=why, summary=f"WARNING: test/lint/typecheck/build run without the OS sandbox ({why})")
        return False

    def _run_tool(self, cmd: str, cwd: Path, timeout_s: float) -> CmdResult:
        """The default tool_runner: the repo's command, sandboxed where possible, without the user's credential-like
        environment variables (tooling_env can still pass one). should_stop: a command started in the instant between a
        stop's kill pass and its Popen kills itself."""
        kind = next((k for k, v in self.profile.tooling.items() if v == cmd), "test")
        profile = tooling_sandbox_profile(self.repo, kind) if self._tooling_sandboxed() else None
        # telemetry writes per-user state outside the sandbox's writable dirs (Next.js, turbo)
        env = {**({"NEXT_TELEMETRY_DISABLED": "1", "TURBO_TELEMETRY_DISABLED": "1"} if profile else {}), **(self.cfg.tooling_env or {})}
        return run_shell(cmd, cwd, timeout_s, env=env or None, should_stop=lambda: self.stop_requested, sandbox_profile=profile, scrub_secrets=True)

    def _stoppable_tool_runner(self, cmd: str, cwd: Path, timeout_s: float) -> CmdResult:
        """``tool_runner`` for the tooling thread: nothing starts after a stop, and a command killed by the stop raises
        StopRequested instead of being retried (run_tooling re-runs a failing test once)."""
        if self.stop_requested:
            raise StopRequested()
        res = self.tool_runner(cmd, cwd, timeout_s)
        if self.stop_requested:
            raise StopRequested()
        return res

    # ---------------------------------------------------------------- S4: review
    async def _phase_review(self, rec) -> PhaseResult:
        cmd = self._cmd("review")
        args = self.cfg.review_args.format(**self._fmt())
        while True:
            sess = await self._fresh_session("review", f"fresh review run (round {rec.repairs})")
            conv1 = await self._send(sess, "review", f"{cmd} {args}".strip())
            report_text = conv1.text or conv1.final_text
            if conv1.timed_out or conv1.is_error:
                await self._close_session("review turn failed")
                failed = [f"[conversation] the review run failed: {conv1.subtype} {conv1.errors[:1]}"]
                if rec.repairs >= self.cfg.max_repairs_per_phase:
                    return PhaseResult(False, failed, report_text)
                rec.repairs += 1
                continue
            trailer = await self._send(sess, "review", REVIEW_TRAILER_REQUEST)
            parsed = G.parse_review(report_text, trailer.reply_text)
            if parsed.blockers and not self._early_done("review"):
                g0 = G.gate_review(parsed, None)
                target = await self._early_route("review", g0.failed(), report_text + "\n\n" + trailer.reply_text)
                if target:
                    return PhaseResult(False, g0.failed(), report_text, routed_to=target)
            jr = await self._respond("review", report_text + "\n\n" + trailer.reply_text, facts={"parsed_blockers": parsed.blockers, "trailer_blockers": parsed.trailer_blockers, "round": rec.repairs})
            if not jr.ok:
                await self._close_session("judge failure")
                return PhaseResult(False, ["judge output invalid twice"], report_text)
            judged = int(jr.data.get("blocking_issues_remaining", 0))
            if parsed.blockers is not None and judged < parsed.blockers:
                self.log.emit("judge_disagreement", phase="review", judge_blocking=judged, parsed_blocking=parsed.blockers)
            if G.gate_review(parsed, None).passed and jr.status in ("done", "needs_reply") and not self.cfg.review_recheck_nonblocking:
                # no blockers: finish in this session. A fresh review after nit fixes finds new nits every pass (seen in
                # practice: 4 rounds of 0 blockers + 1-2 nits, then a halt with nothing must-fix left)
                nit_fix = jr.reply if (jr.status == "needs_reply" and jr.reply.strip()) else None
                return await self._finish_nonblocking_review(rec, sess, parsed, report_text, nit_fix)
            tooling = await self._run_tooling() if (parsed.blockers == 0 and jr.status == "done") else None
            gate = G.gate_review(parsed, tooling)
            if tooling is None and gate.passed:
                gate.add("tooling", False, "tooling not run (fixes pending)")
            self._add_human_untouched_check(gate)
            self._log_gate(gate)
            if gate.passed and jr.status == "done":
                v = await self._verify("review", gate, extra=report_text[-20000:])
                if v is None:
                    await self._close_session("review verified")
                    return PhaseResult(True, worker_text=report_text)
                failed = ["[judge] " + x for x in v]
            else:
                failed = gate.failed() or ["review fixes approved by the judge are pending"]
            if rec.repairs >= self.cfg.max_repairs_per_phase:
                await self._close_session("review repairs exhausted")
                only_pending = G.gate_review(parsed, None).passed and all("fixes pending" in f or "approved by the judge are pending" in f for f in failed)
                return PhaseResult(False, failed, report_text, gate_passed=only_pending)
            rec.repairs += 1
            fix = jr.reply if (jr.status == "needs_reply" and jr.reply.strip()) else repair_message("review", failed, round_no=rec.repairs, max_rounds=self.cfg.max_repairs_per_phase)
            fix, _ = sanitize_reply(fix, [])
            self.log.emit("repair", phase="review", round=rec.repairs, failed=failed)
            if self.reset_pending or self.session is not sess:
                if self.reset_pending:
                    await self._reset(self.reset_pending)
                sess = await self._fresh_session("review", "apply approved review fixes", failed=failed)
            self.gate.set_phase("review")
            await self._converse("review:apply", sess, fix)
            self._mark_early_done("review")  # a fix was applied: only the after-repairs route remains
            await self._close_session("review fixes applied; re-verify in a fresh session")
            self.save()

    async def _finish_nonblocking_review(self, rec, sess: WorkerSession, parsed: G.ReviewReport, report_text: str, nit_fix: str | None) -> PhaseResult:
        """The review found no blockers. The judge's approved nit fixes are applied once (when a repair round is left;
        otherwise they are accepted as they are), then tooling + the DoD check decide, in this same session. A tooling
        or DoD failure is repaired here too; no fresh review runs, since only blockers justify one."""
        max_rounds = self.cfg.max_repairs_per_phase
        if nit_fix is not None:
            applied = rec.repairs < max_rounds
            if applied:
                rec.repairs += 1
                fix, _ = sanitize_reply(nit_fix, [])
                self.log.emit("repair", phase="review", round=rec.repairs, failed=["review nit fixes approved by the judge (no blockers)"])
                sess = await self._review_apply_session(sess, ["review nit fixes approved by the judge"])
                await self._converse("review:apply", sess, fix)
                self._mark_early_done("review")
                self.decisions.add_note("review", f"No blockers; the judge's approved nit fixes were applied once and not re-reviewed (round {rec.repairs}).")
            else:
                self.decisions.add_note("review", "No blockers; no repair round was left, so the remaining nits were accepted without the approved fixes.")
            self.log.emit("review_nonblocking", phase="review", round=rec.repairs, applied=applied, summary="no blockers: nit fixes applied once, no fresh re-review")
            self.save()
        while True:
            tooling = await self._run_tooling()
            gate = G.gate_review(parsed, tooling)
            self._add_human_untouched_check(gate)
            self._log_gate(gate)
            if gate.passed:
                v = await self._verify("review", gate, extra=report_text[-20000:])
                if v is None:
                    await self._close_session("review verified")
                    return PhaseResult(True, worker_text=report_text)
                failed = ["[judge] " + x for x in v]
            else:
                failed = gate.failed()
            if rec.repairs >= max_rounds:
                await self._close_session("review repairs exhausted")
                return PhaseResult(False, failed, report_text)
            rec.repairs += 1
            self.log.emit("repair", phase="review", round=rec.repairs, failed=failed)
            sess = await self._review_apply_session(sess, failed)
            await self._converse("review:apply", sess, repair_message("review", failed, round_no=rec.repairs, max_rounds=max_rounds))
            self._mark_early_done("review")
            self.save()

    async def _review_apply_session(self, sess: WorkerSession, failed: list[str]) -> WorkerSession:
        if self.reset_pending or self.session is not sess:
            if self.reset_pending:
                await self._reset(self.reset_pending)
            sess = await self._fresh_session("review", "apply approved review fixes", failed=failed)
        self.gate.set_phase("review")
        return sess

    # ---------------------------------------------------------------- ship gate + S5
    async def _ship_gate(self) -> None:
        st = self.state
        failed: list[str] = []
        for p in PHASES[: index("ship")]:
            if st.phase(p).status not in ("passed", "already_satisfied"):
                failed.append(f"phase {p} has not passed ({st.phase(p).status})")
        shipping = bool(st.ship.get("started") or st.ship.get("merged"))
        if shipping:
            if not st.decisions_frozen:
                # a restart before ship (--from review) unfroze the log; re-freeze it here too. Before the merge the final
                # render still reaches the decisions.md cc-ship commits; after it nothing is written into the repo (it
                # would leave main dirty: BOOSTS-PAY-01's post-merge clean-tree check failed on exactly that)
                if st.ship.get("merged"):
                    st.decisions_frozen = True
                else:
                    self.decisions.freeze()
                self.log.emit("decisions_frozen", path=str(self.decisions.feature_file()), note="ship already started")
            self.log.emit("ship_gate", open=True, note="ship already started; escalation/budget/tooling checks applied before the first ship command")
            self.save()
            return
        if st.escalations and not st.flags.get("allow_escalations"):
            failed.append(f"{len(st.escalations)} escalation(s) need a human: " + "; ".join(e.get("reason", "")[:120] for e in st.escalations[:3]) + " (re-run with --resume --allow-escalations to ship anyway)")
        if self.cfg.total_budget_usd is not None:  # only when a config sets a USD limit: keep the ship+linear reserve
            reserve = self.cfg.ship_min_remaining_usd + ((self.cfg.phase_budget("linear") or 0.0) if self.linear_on else 0.0)
            remaining = self.cfg.total_budget_usd - st.total_cost_usd
            if remaining < reserve:
                failed.append(f"remaining budget ${remaining:.2f} < ship+linear reserve ${reserve:.2f}")
        if st.ship.get("merged"):
            pass  # resume after merge: the code below would see a clean base; skip re-running tooling
        elif st.phase("ship").status not in ("passed", "already_satisfied"):
            facts = G.ship_facts(self.repo, self.base, st.feature_branch or "", gh_runner=self.gh, start_sha=st.flags.get("start_sha"))
            if not facts.merged:
                tooling = await self._run_tooling("ship gate")
                if not tooling.ok:
                    failed += [c for c in (f"[{c.name}] {c.detail.splitlines()[0]}" for c in tooling.checks() if not c.passed and not c.warn_only)]
        if failed:
            self.log.emit("ship_gate", open=False, failed=failed)
            raise Halt("ship gate closed", failed, phase="ship")
        if not st.decisions_frozen:
            path = self.decisions.freeze()
            self.log.emit("decisions_frozen", path=str(path))
        self.log.emit("ship_gate", open=True)
        self.save()

    def _ci_required(self) -> bool:
        if self.cfg.ci_required is not None:
            return bool(self.cfg.ci_required)
        return self.profile.has_ci

    async def _ship_gate_check(self, facts: G.ShipFacts) -> G.GateResult:
        ci = None
        if self._ci_required() and facts.merged:
            sha = facts.merge_commit or Git(self.repo).head(f"origin/{self.base}")
            # polls gh for up to ci_timeout_min: off the event loop, like tooling; the stoppable sleep ends it on a stop
            ci = await self._in_thread(G.wait_for_ci, self.repo, sha, self.cfg.ci_timeout_min * 60, self.cfg.ci_poll_s, gh_runner=self.gh, sleep=self._stoppable_sleep)
            self.state.ship["ci"] = ci[0]
        return G.gate_ship(facts, self.base, ci=ci, ci_required=self._ci_required())

    async def _phase_ship(self, rec) -> PhaseResult:
        st = self.state
        cmd = self._cmd("ship")
        fmt = self._fmt()
        while True:
            facts = G.ship_facts(self.repo, self.base, st.feature_branch or "", gh_runner=self.gh, start_sha=st.flags.get("start_sha"))
            st.ship.update(pr_url=facts.pr_url, pr_number=facts.pr_number, merged=facts.merged, merge_commit=facts.merge_commit)
            self.save()
            if not (facts.merged and facts.on_base and facts.base_synced):
                sess = await self._fresh_session("ship", "ship the feature")
                unpublished = not facts.remote_branch and not facts.pr_number
                if unpublished and (not st.ship.get("started") or not facts.merged):
                    # First attempt, or nothing published yet (a crash before the push): running cc-ship from
                    # scratch cannot double-ship, while the resume message would claim the branch is pushed.
                    if st.ship.get("started"):
                        self.log.emit("ship_restart", summary="previous ship attempt published nothing; running the ship command again")
                    else:
                        st.ship["started"] = now_iso()
                        self.save()
                    rel = Path(st.feature_dir).relative_to(self.repo).as_posix() if st.feature_dir else "the feature dir"
                    first = f"{cmd} {self._ship_args(fmt)}\nInclude {rel}/decisions.md in the commit."
                else:
                    first = ship_resume_message(facts, self.base, st.feature_branch or "")
                    self.log.emit("ship_resume", facts=dataclasses.asdict(facts), summary="ship resumed idempotently (no second commit/PR)")
                await self._converse("ship", sess, first, facts_fn=lambda: dataclasses.asdict(G.ship_facts(self.repo, self.base, st.feature_branch or "", fetch=False, gh_runner=self.gh, start_sha=st.flags.get("start_sha"))))
                await self._close_session("ship conversation finished")
                facts = G.ship_facts(self.repo, self.base, st.feature_branch or "", gh_runner=self.gh, start_sha=st.flags.get("start_sha"))
                st.ship.update(pr_url=facts.pr_url, pr_number=facts.pr_number, merged=facts.merged, merge_commit=facts.merge_commit)
                self.save()
            gate = await self._ship_gate_check(facts)
            self._log_gate(gate)
            if gate.passed:
                st.ship["completed"] = now_iso()
                return PhaseResult(True)
            if facts.merged and self._ci_required() and st.ship.get("ci") in ("failure", "timeout"):
                raise Halt(
                    "CI is not green on the merge commit after merging. Nothing was reverted (by design). A human must look at it.",
                    gate.failed(),
                    priority="high",
                    phase="ship",
                )
            if rec.repairs >= self.cfg.max_repairs_per_phase:
                return PhaseResult(False, gate.failed())
            rec.repairs += 1
            self.log.emit("repair", phase="ship", round=rec.repairs, failed=gate.failed())

    # ---------------------------------------------------------------- S6: Linear close-out
    async def _phase_linear(self, rec) -> PhaseResult:
        st = self.state
        project = st.linear.get("project_id") or st.linear.get("project") or self.cfg.linear_project
        shared = self._note_shared_issue()
        verify_failures: list[str] = []
        while True:
            snap = await self._reliable_linear_read(project)
            st.linear.setdefault("status_before_closeout", _status_name((snap.issue or {}).get("status")))
            if "closeouts_before" not in st.linear:
                # persisted before anything is written: on a shared issue every close-out comment that appears after
                # this read is this run's, whether or not its run id survived (_closeout_comments)
                st.linear["closeouts_before"] = [_comment_key(c) for c in (snap.comments or []) if G.COMMENT_MARKER.lower() in str(c.get("body", "")).lower()]
                self.save()
            g0 = self._gate_linear(snap)
            if g0.passed and verify_failures:
                # the judge found the close-out text wrong: fix the existing comment / block, never post another
                ours = self._closeout_comments(snap.comments)
                sess = await self._fresh_session("linear", "correct the close-out text", failed=verify_failures)
                await self._converse(
                    "linear",
                    sess,
                    "AUTOPILOT LINEAR CLOSE-OUT CORRECTION. The decision agent found these problems in what was written:\n"
                    + "\n".join(f"- {x}" for x in verify_failures)
                    + f"\nEdit the EXISTING autopilot comment (id {ours[0].get('id') if ours else '?'}) with {self.cfg.linear_tool_prefix}save_comment(id=..., body=...) "
                    f"and/or re-patch only the status block in the project overview. Do not post a new comment. Then reply DONE.",
                )
                await self._close_session("linear correction written")
                snap = await self._reliable_linear_read(project)
                gate = self._gate_linear(snap)
            elif g0.passed:
                gate = g0
            else:
                need_state = not shared and not any(c.name == "issue-completed" and c.passed for c in g0.checks)
                n_comments = g0.facts.get("autopilot_comments", 0)
                if n_comments > 1:
                    raise Halt(f"{n_comments} autopilot close-out comments already exist on {st.issue}; refusing to post more (never delete automatically)", g0.failed(), phase="linear")
                need_comment = n_comments == 0
                need_overview = not all(c.passed for c in g0.checks if c.name.startswith("status-block"))
                dec = [d for d in self.decisions.entries() if d.get("kind") == "decision" and d.get("impact") == "high"]
                prompt = linear_closeout_prompt(
                    tool_prefix=self.cfg.linear_tool_prefix,
                    issue=st.issue,
                    team=st.linear.get("team"),
                    project=project,
                    today=self.today(),
                    run_id=st.run_id,
                    pr_url=st.ship.get("pr_url"),
                    merge_commit=st.ship.get("merge_commit"),
                    need_state=need_state,
                    need_comment=need_comment,
                    need_overview=need_overview and bool(project),
                    high_impact=[f"[{d.get('phase')}] {d.get('item')} → {d.get('chosen')}" for d in dec[:8]],
                    escalations=[f"ESCALATED [{e.get('phase')}]: {e.get('reason')}" for e in st.escalations],
                    marker_style=self.cfg.status_marker_style,
                    shipped_summary_hint=st.description,
                    human_open=self._human_open(),
                    shared=shared,
                    spec_label=self._spec_label(),
                )
                sess = await self._fresh_session("linear", "Linear close-out")
                await self._converse("linear", sess, prompt)
                await self._close_session("linear close-out written")
                st.linear.setdefault("closeout_writes", 0)
                st.linear["closeout_writes"] += 1
                self.save()
                snap = await self._reliable_linear_read(project)
                gate = self._gate_linear(snap)
            self._log_gate(gate)
            if gate.passed:
                v = await self._verify("linear", gate, extra=self._linear_verify_extra(snap, shared))
                if v is None:
                    if shared:
                        st.linear["summary"] = f"{st.issue} is a shared issue ({shared}): status left as {_status_name((snap.issue or {}).get('status'))!r}; close-out comment + status block written"
                    else:
                        st.linear["summary"] = f"{st.issue} completed; close-out comment + status block written"
                    return PhaseResult(True)
                failed = ["[judge] " + x for x in v]
                verify_failures = list(v)
            else:
                failed = gate.failed()
                verify_failures = []
            if rec.repairs >= self.cfg.max_repairs_per_phase:
                return PhaseResult(False, failed)
            rec.repairs += 1
            self.log.emit("repair", phase="linear", round=rec.repairs, failed=failed)

    def _linear_verify_extra(self, snap: LinearSnapshot, shared: str) -> str:
        """What the judge checks the close-out text against."""
        st = self.state
        project_seen = unescape_status_markers(snap.project) or {}
        return json.dumps({"comments": [c.get("body", "")[:3000] for c in self._closeout_comments(snap.comments)], "status_block": G.status_block(str(project_seen.get("description") or project_seen.get("content") or ""))[0], "shipped": {"pr": st.ship.get("pr_url"), "merge_commit": st.ship.get("merge_commit"), "description": st.description}, "shared_issue": shared or None}, default=str)[:15000]

    # ---------------------------------------------------------------- shared Linear issues
    def _shared_issue(self) -> str:
        """Why the Linear issue also tracks other work ("" when it does not): it is listed in ``shared_issues``, or the
        feature's spec.md front matter says ``linear: { shared: true }`` (e.g. two specs that both live
        on one issue). The close-out then never changes the issue's status."""
        st = self.state
        if st.issue.strip().upper() in {k.strip().upper() for k in self.cfg.shared_issues}:
            return "listed in shared_issues"
        if st.feature_dir:
            fm = linear_front_matter(G.read(Path(st.feature_dir) / "spec.md"))
            if _truthy(fm.get("shared")):
                return "spec.md front matter linear.shared: true" + (f", issueId {fm['issueId']}" if fm.get("issueId") else "")
        return ""

    def _note_shared_issue(self) -> str:
        """Compute the shared reason once per check, persist it (the close-out session's policy gate reads it) and log it."""
        st = self.state
        reason = self._shared_issue()
        if reason and st.linear.get("shared") != reason:
            self.log.emit("shared_issue", issue=st.issue, reason=reason, summary=f"{st.issue} is shared ({reason}): the close-out leaves its status alone")
        if reason:
            st.linear["shared"] = reason
        else:
            st.linear.pop("shared", None)
        return reason

    def _spec_label(self) -> str | None:
        st = self.state
        if not st.feature_dir:
            return None
        return f"spec {st.flags.get('spec_id') or Path(st.feature_dir).name} ({Path(st.feature_dir).relative_to(self.repo).as_posix()})"

    def _closeout_comments(self, comments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """Close-out comments that count for this run. A shared issue carries the close-outs of other specs' runs, so
        there only this run's own comments count: those with its run id, and every close-out comment that was not
        there before this run's close-out started (a worker or Linear's re-serialisation may drop the run id; such a
        comment must still count, or each repair round would post another one)."""
        st = self.state
        ours = [c for c in (comments or []) if G.COMMENT_MARKER.lower() in str(c.get("body", "")).lower() and st.issue.lower() in str(c.get("body", "")).lower()]
        if st.linear.get("shared"):
            before = st.linear.get("closeouts_before")
            seen = set(before) if before is not None else None
            ours = [c for c in ours if st.run_id in str(c.get("body", "")) or (seen is not None and _comment_key(c) not in seen)]
        return ours

    def _gate_linear(self, snap: LinearSnapshot) -> G.GateResult:
        """gates.gate_linear on what the reader returned, adjusted for this run: escaped visible status markers are
        read as markers, and on a shared issue other runs' close-out comments are ignored and the issue does not have to
        be completed (a warn-only check reports whether its status stayed what it was before the close-out)."""
        st = self.state
        shared = self._note_shared_issue()
        comments = snap.comments
        if shared and comments is not None:
            mine = {id(c) for c in self._closeout_comments(comments)}
            comments = [c for c in comments if G.COMMENT_MARKER.lower() not in str(c.get("body", "")).lower() or id(c) in mine]
        g = G.gate_linear(snap.issue, comments, unescape_status_markers(snap.project), self.today(), st.issue)
        if shared and snap.issue is not None:
            before = st.linear.get("status_before_closeout")
            now = _status_name(snap.issue.get("status"))
            same = before is None or now == before
            g.facts["shared_issue"] = shared
            g.checks = [
                G.Check("shared-issue-status-unchanged", same, f"{st.issue} is shared ({shared}): its status is not the pipeline's to change; it is {now!r}" + ("" if same else f", was {before!r} before the close-out"), warn_only=True)
                if c.name == "issue-completed"
                else c
                for c in g.checks
            ]
        return g

    def _check_workspace(self, snap: LinearSnapshot) -> None:
        want = self.cfg.linear_workspace_url
        if want and (not snap.workspace_url or snap.workspace_url.rstrip("/") != want.rstrip("/")):
            raise Halt(f"Linear MCP is connected to {snap.workspace_url or 'an unknown workspace'}, expected {want}", phase="linear", notify_linear=False)

    async def _reliable_linear_read(self, project: str | None, tries: int = 3) -> LinearSnapshot:
        """A Linear read that is safe to act on: workspace verified, issue AND comments present. Never write blind."""
        snap = LinearSnapshot()
        for _ in range(tries):
            snap = await self._linear_read(project=project)
            if snap.workspace_url and snap.issue is not None and snap.comments is not None:
                self._check_workspace(snap)
                return snap
        raise Halt(
            f"Linear read-back failed {tries} times ({'; '.join(snap.errors)[:300]}); nothing more was written to Linear.",
            snap.errors,
            phase="linear",
            notify_linear=False,
        )

    async def _linear_read(self, project: str | None) -> LinearSnapshot:
        """Read Linear through MCP and parse the RAW tool results (not the model's paraphrase)."""
        base_ctx = self.gate.ctx if hasattr(self, "gate") else None
        # cancellable: a stop does not wait for the reader session's turn (up to 5 min)
        snap = await self._cancellable(read_linear(
            self.make_session,
            repo=self.repo,
            issue=self.state.issue,
            project=project,
            tool_prefix=self.cfg.linear_tool_prefix,
            model=self.cfg.reader_model,
            base_ctx=base_ctx,
            log_event=self._log_policy,
            plugin_dirs=tuple(self.cfg.resolved_plugin_dirs()),
            server=self.cfg.linear_server,
            env=self.cfg.session_env(),
        ))
        self.state.total_cost_usd += snap.cost_usd
        ph = self.current_phase or "feature_start"
        self.state.phase(ph).cost_usd += snap.cost_usd
        self.log.emit("linear_read", phase=ph, issue_status=(snap.issue or {}).get("status"), comments=len(snap.comments or []), project=bool(snap.project), workspace=snap.workspace_url, errors=snap.errors)
        return snap

    # ==================================================================================
    # generic gate loop + routing
    # ==================================================================================

    async def _gate_loop(self, phase: str, rec, gate_fn: Callable[[], G.GateResult], conv: Conversation) -> PhaseResult:
        while True:
            gate = gate_fn()
            if inspect.isawaitable(gate):
                gate = await gate  # the implement gate runs the tooling (in a thread)
            self._log_gate(gate)
            if gate.passed:
                v = await self._verify(phase, gate)
                if v is None:
                    return PhaseResult(True, worker_text=conv.text)
                failed = ["[judge] " + x for x in v]
            else:
                failed = gate.failed()
                if conv.status == "failed" and conv.reason:
                    self.log.emit("conversation_failed", phase=phase, reason=conv.reason)
                    if not failed:
                        failed = [f"[conversation] the {phase} command did not finish ({conv.status})"]
            if rec.repairs >= self.cfg.max_repairs_per_phase:
                extra = [f"[conversation] {conv.reason}"] if conv.status == "failed" and conv.reason else []
                return PhaseResult(False, failed + extra, conv.text)  # the halt report shows why the conversation failed
            rec.repairs += 1
            self.log.emit("repair", phase=phase, round=rec.repairs, failed=failed, summary=f"repair round {rec.repairs} for {phase}")
            msg = repair_message(phase, failed, round_no=rec.repairs, max_rounds=self.cfg.max_repairs_per_phase)
            if phase == "implement":
                msg += self._human_note()
                ctx_ok = self.session is not None and not self.reset_pending and self.session.turns < self.cfg.max_turns_per_session - 1 and not self._choice_changed("implement")
                if ctx_ok:
                    conv = await self._converse("implement", self.session, msg, facts_fn=self._implement_facts)
                else:
                    if self.reset_pending:
                        await self._reset(self.reset_pending)
                    sess = await self._fresh_session("implement", "repair the implement gate", failed=failed)
                    conv = await self._converse("implement", sess, msg, facts_fn=self._implement_facts)
                    if conv.status in ("reset", "turn_cap"):
                        conv = await self._implement_sessions(first=False, failed=failed)
            else:
                if self.reset_pending and self.session is not None:
                    await self._reset(self.reset_pending)
                conv = await self._run_command(phase, "repair", msg, failed=failed)
            self.save()

    async def _route(self, phase: str, res: PhaseResult) -> str | None:
        st = self.state
        req = self._judge_req("route", phase, {
            "failed": res.failed,
            "worker_text": res.worker_text[-12000:],
            "back_edges_used": len(st.back_edges),
            "max_back_edges": self.cfg.max_back_edges,
            "targets_used": [b.get("to") for b in st.back_edges],
        })
        jr = await self._judge_call(self.judge.route, req)
        self._account_judge(jr, phase)
        action = jr.data.get("action", "halt") if jr.ok else "halt"
        target = jr.data.get("target_phase")
        reason = jr.data.get("reason", jr.error or "")
        if action == "back":
            chk = check_back_edge(phase, target, st.back_edges, self.cfg.max_back_edges, self.cfg.max_back_per_target)
            if not chk.allowed:
                self.decisions.add_routing(phase, "back", target, reason, applied=False, note=chk.reason)
                raise Halt(f"{phase} failed after {self.cfg.max_repairs_per_phase} repairs and routing back is not allowed: {chk.reason}", res.failed, phase=phase)
            return await self._apply_back_edge(phase, target, reason, res.failed)
        if action == "forward":
            # a judge can never overrule a failed deterministic gate: re-check it
            regate = await self._regate(phase)
            # analyze/review cannot be re-gated without a fresh run; their last round's gate stands in when it passed
            # and only judge-requested (non-blocking) work was left
            ok = (regate is not None and regate.passed) or (regate is None and res.gate_passed)
            self.decisions.add_routing(phase, "forward", None, reason, applied=ok, note="" if ok else "deterministic gate still fails")
            if ok:
                st.phase(phase).status = "passed"
                self.log.emit("route", phase=phase, action="forward", reason=reason)
                return None
            raise Halt(f"{phase} failed; the judge asked to move forward but the deterministic gate still fails", res.failed, phase=phase)
        # "fix_here" after the repair rounds are used up means the same as halt
        self.decisions.add_routing(phase, "halt", None, reason, note="" if action == "halt" else f"judge answered {action!r}; no repair rounds left")
        raise Halt(f"{phase} failed after {self.cfg.max_repairs_per_phase} repair round(s); judge routed to halt: {reason}", res.failed, phase=phase)

    async def _apply_back_edge(self, phase: str, target: str, reason: str, failed: list[str], *, early: bool = False) -> str:
        """Record an allowed back-edge and reset ``target`` .. ``phase`` to pending (forced re-runs, persisted)."""
        st = self.state
        self.decisions.add_routing(phase, "back", target, reason, applied=True, note="ok", early=early)
        st.back_edges.append({"from": phase, "to": target, "reason": reason, "ts": now_iso(), **({"early": True} if early else {})})
        st.flags["route_context"] = {"from": phase, "to": target, "reason": reason, "failed": list(failed)[:20]}
        for p in downstream(target, phase):
            st.phase(p).status = "pending"
            st.phase(p).artifact_hashes = {}
            (st.flags.get("early_checked") or {}).pop(p, None)  # each re-run is a new phase entry
            self.force_rerun.add(p)
        if index(target) < index("implement"):
            # tasks.md may be regenerated, so implement takes a new snapshot; the tasks open in this one are carried
            # into it (by id, and by text when the label was rewritten away), so an agent's tick that a gate caught
            # never becomes the new snapshot
            before = st.flags.pop("human_before", None) or {}
            keys = st.flags.pop("human_keys", None) or {}
            carry = dict(st.flags.get("human_carry") or {})
            carry.update({i: keys.get(i, "") for i, done in before.items() if done is False})
            if carry:
                st.flags["human_carry"] = carry
        self._persist_force_rerun()
        self.log.emit("route", phase=phase, action="back", target=target, reason=reason, early=early, summary=f"{phase} → back to {target}{' (early, before any fix)' if early else ''}")
        await self._close_session(f"routing back to {target}")
        self.save()
        return target

    async def _early_route(self, phase: str, failed: list[str], worker_text: str) -> str | None:
        """SPEC §7: on the first round with blocking findings, before any fix, ask whether the root cause is upstream.
        Returns the target when the judge says back and the back-edge limits allow it; otherwise None (repair here)."""
        st = self.state
        req = self._judge_req("route", phase, {
            "early": True,
            "failed": failed,
            "worker_text": worker_text[-12000:],
            "back_edges_used": len(st.back_edges),
            "max_back_edges": self.cfg.max_back_edges,
            "targets_used": [b.get("to") for b in st.back_edges],
        })
        jr = await self._judge_call(self.judge.route, req)
        self._account_judge(jr, phase)
        # asked once the judge has answered (a stop that cancels the call leaves it unasked for --resume); a back-edge
        # below clears it again for the re-run
        self._mark_early_done(phase)
        if not jr.ok:
            self.log.emit("early_route", phase=phase, action="fix_here", error=jr.error, summary=f"{phase}: early route check invalid; repairing in place")
            self.decisions.add_routing(phase, "fix_here", None, f"judge output invalid: {jr.error}"[:300], note="early route check failed; normal repairs", early=True)
            return None
        action, target, reason = jr.data.get("action"), jr.data.get("target_phase"), jr.data.get("reason", "")
        if action != "back":
            self.log.emit("early_route", phase=phase, action=action, reason=reason, summary=f"{phase}: root cause is here ({action}); normal repairs")
            self.decisions.add_routing(phase, "fix_here", None, reason, note="" if action == "fix_here" else f"judge answered {action!r}", early=True)
            return None
        chk = check_back_edge(phase, target, st.back_edges, self.cfg.max_back_edges, self.cfg.max_back_per_target)
        if not chk.allowed:
            self.log.emit("early_route", phase=phase, action="back", target=target, allowed=False, reason=reason, refused=chk.reason, summary=f"{phase}: early route to {target} refused ({chk.reason}); normal repairs")
            self.decisions.add_routing(phase, "back", target, reason, applied=False, note=f"{chk.reason}; repairing in {phase} instead", early=True)
            return None
        return await self._apply_back_edge(phase, target, reason, failed, early=True)

    async def _regate(self, phase: str) -> G.GateResult | None:
        try:
            if phase == "feature_start":
                return G.gate_feature_start(self.repo, self.base, self.feature_dir, self.state.feature_branch)
            if phase == "clarify":
                return G.gate_clarify(self.feature_dir, self.today(), nothing_to_clarify=self.state.flags.get("clarify_nothing", False), since=self._clarify_since())
            if phase == "plan":
                return G.gate_plan(self.feature_dir)
            if phase == "tasks":
                return G.gate_tasks(self.feature_dir, self.repo)
            if phase == "implement":
                return await self._implement_gate()
        except Halt:
            return None
        return None  # analyze/review/ship/linear need a fresh run: forward is never allowed on judgement alone

    # ==================================================================================
    # sessions
    # ==================================================================================

    def _spec(self, phase: str, label: str) -> SessionSpec:
        base = phase.split(":")[0]
        group = SESSION_GROUPS.get(PHASE_SPECS[base].group, (base,)) if base in PHASE_SPECS else (base,)
        rest = group[group.index(base):] if base in group else (base,)
        budget = self._session_budget(rest)  # None (no cap) unless a config sets USD limits
        gate = self.gate
        choice = self._worker_choice(base)
        if base == "linear" and self.state.linear.get("shared"):
            gate = SharedIssueGate(ctx=self.gate.ctx, log_event=self._log_policy, ask_handler=self._answer_questions, issue=self.state.issue)
        return SessionSpec(
            label=label,
            phase=phase,
            cwd=self.repo,
            gate=gate,
            system_append=WORKER_SYSTEM_APPEND,
            budget_usd=budget,
            model=choice.model,
            effort=choice.effort,
            env={**self.cfg.session_env(), **self._worker_env()},
            plugin_dirs=tuple(self.cfg.resolved_plugin_dirs()),
            wait_mcp=(self.cfg.linear_server,) if phase.split(":")[0] in ("linear", "halt_notice") else (),
            # OS-level Bash sandbox (None for feature_start / ship / linear / halt_notice, and when off or unavailable)
            sandbox=sandbox_settings(base, self.cfg.sandbox, self.cfg.sandbox_network_domains, repo=self.repo, env=self._worker_env()),
        )

    async def _open(self, phase: str, purpose: str, failed: list[str] | None = None) -> WorkerSession:
        base_phase = phase.split(":")[0]
        rec = self.state.phase(base_phase)
        self._session_seq += 1
        label = f"{base_phase}-a{rec.attempts}-r{rec.repairs}-{time.strftime('%H%M%S')}-s{self._session_seq}"
        if self.stop_requested:
            raise StopRequested()
        if self._route_note and base_phase == self.current_phase:
            failed = list(failed or []) + [x for x in self._route_note if x not in (failed or [])]
        spec = self._spec(base_phase, label)
        self._note_worker_choice(base_phase, label)
        sess = self.make_session(spec)
        await sess.start()
        self.session = sess
        self.session_group = PHASE_SPECS[base_phase].group
        self.reset_pending = None
        brief = handoff_brief(self.ctx(), base_phase, rec.attempts, failed_checks=failed, purpose=purpose)
        turn = await sess.send(brief, timeout_s=self.cfg.turn_timeout_min * 60)
        self._account(turn, base_phase)
        self.log.emit("session_ready", phase=base_phase, label=label, purpose=purpose)
        return sess

    async def _group_session(self, phase: str, purpose: str, failed: list[str] | None = None) -> WorkerSession:
        group = PHASE_SPECS[phase.split(":")[0]].group
        capped = self.session is not None and self.session.turns >= self.cfg.max_turns_per_session - 1
        if capped and not self.reset_pending:
            self.reset_pending = f"turn cap {self.cfg.max_turns_per_session} reached"
        if self.session is not None and self.session_group == group and not self.reset_pending and not self._choice_changed(phase):
            return self.session  # same group (incl. repairs) while context and turn budget allow
        if self.session is not None and self._choice_changed(phase) and not self.reset_pending:
            self.reset_pending = f"model switch to {self._worker_choice(phase).label()} for {phase.split(':')[0]}"
        if self.session is not None:
            await self._close_session(self.reset_pending or f"new session for {phase}")
        return await self._open(phase, purpose, failed)

    # ---------------------------------------------------------------- worker model / effort (models.py)
    def _worker_choice(self, phase: str) -> WorkerChoice:
        base = phase.split(":")[0]
        rec = self.state.phase(base) if base in PHASES else None
        reco = (self.state.flags.get("worker_reco") or {}).get(base)
        return choose(self.cfg, base, rec.repairs if rec else 0, reco)

    def _choice_changed(self, phase: str) -> bool:
        """auto mode: the live session runs a different model / effort than this phase (or repair round) should use."""
        if self.cfg.model_mode != "auto" or self._session_choice is None:
            return False
        return self._worker_choice(phase).key != self._session_choice.key

    def _note_worker_choice(self, phase: str, label: str) -> None:
        choice = self._worker_choice(phase)
        prev = self.state.flags.get("worker") or {}
        self._session_choice = choice
        self.state.flags["worker"] = {"phase": phase, "model": choice.model, "effort": choice.effort, "reason": choice.reason}
        if self.cfg.model_mode == "auto" and (prev.get("model"), prev.get("effort")) != choice.key:
            frm = f"{prev.get('model') or 'default'}/{prev.get('effort') or 'default'}" if prev else None
            self.state.model_switches.append({"ts": now_iso(), "phase": phase, "from": frm, "to": choice.label(), "reason": choice.reason})
            self.log.emit("model_switch", phase=phase, label=label, previous=frm, to=choice.label(), reason=choice.reason, summary=f"{phase}: worker {frm or '(start)'} -> {choice.label()} ({choice.reason})")

    async def _fresh_session(self, phase: str, purpose: str, failed: list[str] | None = None) -> WorkerSession:
        if self.session is not None:
            await self._close_session(f"fresh session for {phase}: {purpose}")
        return await self._open(phase, purpose, failed)

    async def _close_session(self, reason: str) -> None:
        if self.session is None:
            return
        sess, self.session = self.session, None
        self.session_group = None
        self.history = []  # never carry conversation text across a reset (the judge sees only this session's turns)
        try:
            await sess.close()
        except Exception as exc:  # pragma: no cover
            if hasattr(self, "log"):
                self.log.emit("session_close_error", error=repr(exc))
        if hasattr(self, "log"):
            self.log.emit("session_closed", label=getattr(sess, "label", "?"), reason=reason)

    async def _reset(self, reason: str) -> None:
        self.state.resets.append({"ts": now_iso(), "phase": self.current_phase, "reason": reason})
        self.log.emit("reset", phase=self.current_phase, reason=reason, summary=f"context reset: {reason}")
        await self._close_session(f"reset: {reason}")
        self.reset_pending = None
        self.save()

    # ==================================================================================
    # conversations
    # ==================================================================================

    async def _send(self, sess: WorkerSession, phase: str, text: str) -> TurnResult:
        if self.stop_requested:
            raise StopRequested()
        self._check_limits()
        self.gate.set_phase(phase.split(":")[0])
        if phase.split(":")[0] == "implement" and self.state.feature_dir:
            # policy: a protected file (lockfile / CI config) may be edited only if a task in tasks.md names it
            self.gate.ctx.tasks_text = G.read(self.feature_dir / "tasks.md")
        self.history.append(f"PIPELINE → worker: {text[:1500]}")
        turn = await sess.send(text, timeout_s=self.cfg.turn_timeout_min * 60)
        await self._after_turn(sess, phase, turn)
        if self.stop_requested:
            raise StopRequested()
        return turn

    async def _after_turn(self, sess: WorkerSession, phase: str, turn: TurnResult) -> None:
        base_phase = phase.split(":")[0]
        self._account(turn, base_phase)
        self.last_worker_text = turn.reply_text
        self.history.append(f"WORKER: {turn.reply_text[:3000]}")
        self.history = self.history[-12:]
        pct = await sess.context_pct()
        self.log.emit(
            "turn",
            phase=phase,
            session=getattr(sess, "label", ""),
            turn=sess.turns,
            cost_usd=round(turn.cost_delta_usd, 4),
            context_pct=pct,
            subtype=turn.subtype,
            is_error=turn.is_error,
            tools=[c.name for c in turn.tool_calls][:40],
            summary=turn.summary(240),
        )
        if turn.budget_exceeded:
            raise Halt(f"worker session hit its budget cap (max_budget_usd) during {phase}", phase=base_phase)
        if pct is not None and pct >= self.cfg.context_reset_pct and not self.reset_pending:
            self.reset_pending = f"context {pct:.0f}% ≥ {self.cfg.context_reset_pct:.0f}%"
            self.log.emit("reset_scheduled", phase=phase, reason=self.reset_pending)
        if (turn.compacted or getattr(sess, "compaction_pending", False)) and not (self.reset_pending or "").startswith("auto-compaction"):
            self.reset_pending = "auto-compaction (PreCompact fired): a compacted context is lossy"
            self.log.emit("reset_scheduled", phase=phase, reason=self.reset_pending)
        if sess.turns >= self.cfg.max_turns_per_session - 1 and not self.reset_pending:
            self.reset_pending = f"turn cap {self.cfg.max_turns_per_session} reached"
            self.log.emit("reset_scheduled", phase=phase, reason=self.reset_pending)
        self.save()

    async def _converse(self, phase: str, sess: WorkerSession, first: str, *, facts_fn: Callable[[], dict[str, Any]] | None = None, stop_on_reset: bool = False) -> Conversation:
        msg = first
        turns = 0
        while True:
            if sess.turns >= self.cfg.max_turns_per_session:
                self.log.emit("turn_cap", phase=phase, turns=sess.turns)
                return Conversation("turn_cap", self.last_worker_text, reason=f"turn cap {self.cfg.max_turns_per_session} reached", turns=turns)
            turn = await self._send(sess, phase, msg)
            turns += 1
            if turn.timed_out:
                await self._close_session("turn timeout")
                return Conversation("failed", turn.reply_text, reason=f"worker turn exceeded {self.cfg.turn_timeout_min:g} min and was interrupted", turns=turns)
            if turn.is_error:
                return Conversation("failed", turn.reply_text, reason=f"worker error: {turn.subtype} {turn.errors[:2]} {turn.final_text[:200]}", turns=turns)
            facts = facts_fn() if facts_fn else {}
            jr = await self._respond(phase, turn.reply_text, facts=facts)
            if not jr.ok:
                return Conversation("failed", turn.reply_text, jr, reason="judge output invalid twice", turns=turns)
            if jr.status == "done":
                return Conversation("done", turn.reply_text, jr, turns=turns)
            if jr.status == "failed":
                return Conversation("failed", turn.reply_text, jr, reason="judge: " + (jr.reply or "worker stuck"), turns=turns)
            reply, warn = sanitize_reply(jr.reply, self._allowed_reply_commands())
            if warn:
                self.log.emit("reply_sanitized", phase=phase, warning=warn)
            compacted = (self.reset_pending or "").startswith("auto-compaction")
            if self.reset_pending and (stop_on_reset or compacted):
                return Conversation("reset", turn.reply_text, jr, reason=self.reset_pending, turns=turns)
            msg = reply

    def _allowed_reply_commands(self) -> list[str]:
        cmds = list(self.cfg.allowed_reply_commands)
        spec_cmd = self.profile.commands.get("specify")
        if spec_cmd:
            cmds.append(spec_cmd.lstrip("/"))
        return cmds

    # ==================================================================================
    # judge plumbing
    # ==================================================================================

    def _judge_context(self) -> dict[str, Any]:
        st = self.state
        return {
            "issue": st.issue,
            "description": st.description,
            "linear_title": st.linear.get("title"),
            "repo": str(self.repo),
            "base_branch": self.base,
            "feature_dir": Path(st.feature_dir).relative_to(self.repo).as_posix() if st.feature_dir else None,
            "feature_branch": st.feature_branch,
            "repo_notes": self.cfg.judge_notes,
            "worker_auto": (
                {"current": self._session_choice.label() if self._session_choice else None, "models": list(self.cfg.auto_models), "efforts": list(self.cfg.auto_efforts)}
                if self.cfg.model_mode == "auto" else None
            ),
        }

    async def _judge_call(self, fn: Callable[[JudgeRequest], Any], req: JudgeRequest) -> JudgeResult:
        """Every judge call is bounded in wall-clock time (a stalled judge becomes an invalid result, not a hang) and
        cancellable: a stop request cancels it at once and surfaces as StopRequested."""
        try:
            jr = await self._cancellable(fn(req), timeout_s=self.cfg.judge_timeout_min * 60)
        except asyncio.TimeoutError:
            self.log.emit("judge_timeout", phase=req.phase, mode=req.mode, timeout_min=self.cfg.judge_timeout_min)
            jr = JudgeResult(req.mode, {}, ok=False, error=f"judge timed out after {self.cfg.judge_timeout_min:g} min")
        if self.stop_requested:
            raise StopRequested()
        return jr

    def _judge_req(self, mode: str, phase: str, payload: dict[str, Any]) -> JudgeRequest:
        return JudgeRequest(mode=mode, phase=phase, context=self._judge_context(), payload=payload, recent_decisions=self.decisions.recent(25))

    async def _respond(self, phase: str, worker_text: str, facts: dict[str, Any] | None = None) -> JudgeResult:
        req = self._judge_req("respond", phase, {"worker_text": worker_text, "facts": facts or {}, "history": "\n".join(self.history[:-1])})
        jr = await self._judge_call(self.judge.respond, req)
        self._account_judge(jr, phase)
        if jr.ok:
            self._record_decisions(phase, "respond", jr)
        self.last_judge = jr.data or {"error": jr.error}
        return jr

    async def _verify(self, phase: str, gate: G.GateResult, extra: Any = None) -> list[str] | None:
        """Judge Definition-of-Done check. Returns None on pass, else the failed criteria."""
        spec = PHASE_SPECS[phase]
        if not spec.dod:
            return None
        req = self._judge_req("verify", phase, {"gate": gate.to_dict(), "extra": extra})
        jr = await self._judge_call(self.judge.verify, req)
        self._account_judge(jr, phase)
        self.last_judge = jr.data or {"error": jr.error}
        if not jr.ok:
            return ["judge verify output invalid twice"]
        if jr.data.get("escalate"):
            self._escalate(phase, jr.data.get("escalate_reason", "verify escalation"))
        self.log.emit("verify", phase=phase, passed=jr.data.get("pass"), failed=jr.data.get("failed_criteria"), summary=f"{phase} DoD {'pass' if jr.data.get('pass') else 'FAIL'}")
        if jr.data.get("pass"):
            return None
        if jr.data.get("next_worker") and self.cfg.model_mode == "auto":
            reco, note = accept_recommendation(self.cfg, self._session_choice or self._worker_choice(phase), jr.data.get("next_worker"))
            self.log.emit("model_recommendation", phase=phase, recommended=jr.data.get("next_worker"), outcome=note)
            if reco:
                self.state.flags.setdefault("worker_reco", {})[phase.split(":")[0]] = reco
        return list(jr.data.get("failed_criteria") or ["judge: Definition of Done not met"])

    async def _answer_questions(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        phase = self.current_phase or "feature_start"
        questions = list(tool_input.get("questions") or [])
        req = self._judge_req("answer", phase, {"questions": questions, "worker_text": self.last_worker_text[-8000:]})
        jr = await self._judge_call(self.judge.answer, req)
        self._account_judge(jr, phase)
        chosen: dict[str, Any] = {}
        free: dict[str, str] = {}
        if jr.ok:
            for a in jr.data.get("answers", []):
                if a.get("labels"):
                    chosen[a.get("question", "")] = a.get("labels")
                elif a.get("free_text"):
                    free[a.get("question", "")] = a.get("free_text")
            self._record_decisions(phase, "answer", jr)
        answers = map_answers(questions, chosen, warn=lambda m: self.log.emit("ask_fallback", phase=phase, warning=m), free_text=free)
        self.log.emit("ask_user_question", phase=phase, answers=answers)
        return {**tool_input, "answers": answers}

    def _record_decisions(self, phase: str, mode: str, jr: JudgeResult) -> None:
        self.decisions.add_judge_decisions(phase, mode, jr.decisions, attempt=self.state.phase(phase.split(":")[0]).attempts)
        if jr.data.get("escalate"):
            self._escalate(phase, jr.data.get("escalate_reason") or "judge escalated", item=(jr.decisions[0].get("item") if jr.decisions else ""))

    def _escalate(self, phase: str, reason: str, item: str = "") -> None:
        e = {"phase": phase, "reason": reason, "item": item, "ts": now_iso()}
        if not any(x.get("reason") == reason and x.get("phase") == phase for x in self.state.escalations):
            self.state.escalations.append(e)
            self.decisions.add_escalation(phase, reason, item)
            self.log.emit("escalation", phase=phase, reason=reason, summary=f"ESCALATION: {reason}")

    # ==================================================================================
    # accounting + limits
    # ==================================================================================

    def _account(self, turn: TurnResult, phase: str) -> None:
        self.state.total_cost_usd += turn.cost_delta_usd
        self.state.phase(phase.split(":")[0]).cost_usd += turn.cost_delta_usd

    def _account_judge(self, jr: JudgeResult, phase: str) -> None:
        self.state.total_cost_usd += jr.cost_usd
        self.state.phase(phase.split(":")[0]).cost_usd += jr.cost_usd

    def _session_budget(self, phases: tuple[str, ...] | list[str]) -> float | None:
        """A session's USD cap: None (no cap) unless the config sets a total or a limit for one of these phases."""
        caps: list[float] = []
        limits = [self.cfg.phase_budget(p) for p in phases]
        if any(v is not None for v in limits):
            caps.append(sum(max(0.0, (v or 0.0) - self.state.phase(p).cost_usd) for p, v in zip(phases, limits) if v is not None))
        if self.cfg.total_budget_usd is not None:
            caps.append(self.cfg.total_budget_usd - self.state.total_cost_usd)
        return max(0.25, min(caps)) if caps else None

    def _check_limits(self, include_phase: bool = True) -> None:
        """Only limits a config sets explicitly; by default there are none (the runs use the Claude subscription)."""
        st = self.state
        if self.cfg.total_budget_usd is not None and st.total_cost_usd >= self.cfg.total_budget_usd:
            raise Halt(f"total budget exhausted: ${st.total_cost_usd:.2f} ≥ ${self.cfg.total_budget_usd:.2f}")
        ph = self.current_phase
        limit = self.cfg.phase_budget(ph) if include_phase and ph else None
        if limit is not None and st.phase(ph).cost_usd >= limit:
            raise Halt(f"phase budget for {ph} exhausted: ${st.phase(ph).cost_usd:.2f} ≥ ${limit:.2f}", phase=ph)
        if self.cfg.max_wallclock_h is not None:
            elapsed = self._elapsed_base + (time.monotonic() - self._t0)
            if elapsed >= self.cfg.max_wallclock_h * 3600:
                raise Halt(f"wall-clock limit reached: {elapsed / 3600:.2f} h ≥ {self.cfg.max_wallclock_h} h")

    # ==================================================================================
    # endings
    # ==================================================================================

    def _complete(self) -> RunOutcome:
        st = self.state
        st.status = "completed"
        st.current_phase = None
        self.save()
        human_open = self._human_open()
        write_final_report(self.paths, st, self.log.read(), self.decisions.entries(), human_open=human_open if st.feature_dir else None)
        self.log.emit("run_end", status="completed", cost_usd=round(st.total_cost_usd, 2), human_open=human_open, summary="pipeline completed")
        done = "merged and closed" if self.linear_on else "merged"
        notify(self.cfg.notify_command, f"Autopilot {st.issue}: {done}", self.log)
        return RunOutcome("completed", done, self.paths.report)

    def _snapshot_human_at_stop(self) -> None:
        """Human-only task state when the run stops (halt / stop / pause), so a --resume can tell a person's tick made
        while nothing ran from an agent's tick made before the stop (_accept_human_ticks_made_while_stopped)."""
        st = self.state
        if (st.flags.get("human_before") is not None or st.flags.get("human_carry")) and st.feature_dir:
            try:
                st.flags["human_at_stop"] = {t.id: t.done for t in self._run_human_tasks()}
            except Exception:  # never let a report-side snapshot break a halt
                st.flags.pop("human_at_stop", None)

    def _pause(self, reason: str, items: list[str] | None = None, *, kind: str = "until") -> RunOutcome:
        """Status "paused" (exit 0). ``items`` say what a person must do first; the report adds the resume command.
        ``kind`` ("until", "checklists", "human_tasks") tells --resume whether this was the --until stop."""
        st = self.state
        st.status = "paused"
        st.flags["paused_by"] = kind
        self._snapshot_human_at_stop()
        self.save()
        has_tasks = bool(st.feature_dir) and (Path(st.feature_dir) / "tasks.md").exists()
        write_final_report(
            self.paths,
            st,
            self.log.read(),
            self.decisions.entries(),
            title=f"Autopilot PAUSED ({reason})",
            human_open=self._human_open() if has_tasks else None,
            pause_items=items,
            resume=resume_command(st, self.cfg.python) if items else None,
        )
        self.log.emit("run_end", status="paused", reason=reason, items=items or [], summary=reason)
        notify(self.cfg.notify_command, f"Autopilot {st.issue}: {reason}", self.log)
        return RunOutcome("paused", reason, self.paths.report)

    def _founder_pause(self, phase: str) -> RunOutcome | None:
        """Work only a person may do pauses the run (never halts it): open checklists before implement when
        checklists_gate = "halt", open human-only tasks before ship when human_tasks_block_ship. --resume re-checks."""
        st, cfg = self.state, self.cfg
        if phase == "implement" and cfg.checklists_gate == "halt" and st.feature_dir:
            open_lists = open_checklist_items(self.feature_dir)
            if open_lists:
                rel = Path(st.feature_dir).relative_to(self.repo).as_posix()
                items = [f"`{rel}/{p}`: {n} of {total} item(s) unchecked" for p, n, total in open_lists]
                self.log.emit("checklists_pause", checklists=[{"file": p, "open": n, "total": t} for p, n, t in open_lists])
                return self._pause(
                    f"{sum(n for _, n, _ in open_lists)} unchecked checklist item(s) in {len(open_lists)} file(s) before implement (checklists_gate = halt)",
                    items + ["These are founder-owned checklists: tick them (or fix the spec) in the feature branch, then --resume."],
                    kind="checklists",
                )
        if phase == "ship" and cfg.human_tasks_block_ship and st.feature_dir and not (st.ship.get("started") or st.ship.get("merged")):
            cmds = self._pipeline_cmds()
            pending = [t for t in self._run_human_tasks() if not t.done]
            # "Operator steps (not for agents): /cc-review, then /cc-ship": this run's review passed and ship is next, so
            # pausing for them would ask a person to run the ship by hand
            steps = [t.id for t in pending if G.pipeline_step(t, cmds)]
            if steps:
                self.log.emit("human_tasks_pipeline_steps", tasks=steps, summary=f"{', '.join(steps)}: the pipeline's own review/ship step(s); they do not hold the ship pause")
            open_tasks = [f"{t.id} {t.text[:100]}" for t in pending if t.id not in steps]
            if open_tasks:
                self.log.emit("human_tasks_pause", open=open_tasks)
                rel = Path(st.feature_dir).relative_to(self.repo).as_posix()
                return self._pause(
                    f"{len(open_tasks)} human-only task(s) in tasks.md are still open before ship",
                    [f"`{t}`" for t in open_tasks]
                    + [f"A person does these and ticks them in `{rel}/tasks.md` (or sets human_tasks_block_ship = false to ship without them), then --resume."],
                    kind="human_tasks",
                )
        return None

    async def _halt(self, h: Halt, status: str = "halted") -> RunOutcome:
        st = self.state
        try:
            if self.session is not None:
                await self.session.interrupt()
        except Exception:
            pass
        await self._close_session("halt")
        st.status = status
        st.halt = {"reason": h.reason, "phase": h.phase or self.current_phase, "failed": h.failed, "priority": h.priority, "ts": now_iso()}
        self._snapshot_human_at_stop()
        self.save()
        resume = resume_command(st, self.cfg.python)
        write_halt_report(self.paths, st, reason=h.reason, phase=h.phase or self.current_phase, failed_checks=h.failed, last_judge=self.last_judge, python=self.cfg.python, priority=h.priority, status=status)
        self.log.emit("run_end", status=status, reason=h.reason, failed=h.failed, summary=f"HALT: {h.reason}")
        budget_left = self.cfg.total_budget_usd is None or self.cfg.total_budget_usd - st.total_cost_usd > 0.75
        if self.linear_on and self.cfg.notify_linear_on_halt and h.notify_linear and status == "halted" and budget_left and st.linear.get("read") and not st.linear.get("read_error"):
            await self._halt_notice(h, resume)
        notify(self.cfg.notify_command, f"Autopilot {st.issue} halted: {h.reason[:120]}", self.log)
        return RunOutcome(status, h.reason, self.paths.report)

    async def _halt_notice(self, h: Halt, resume: str) -> None:
        st = self.state
        try:
            snap = await self._linear_read(project=None)
            want = (self.cfg.linear_workspace_url or "").rstrip("/")
            if not snap.workspace_url or snap.workspace_url.rstrip("/") != want:
                self.log.emit("halt_notice_skipped", reason=f"workspace {snap.workspace_url!r} != {want!r}")
                return
            if any("autopilot halted" in str(c.get("body", "")).lower() and st.run_id in str(c.get("body", "")) and h.reason[:60] in str(c.get("body", "")) for c in (snap.comments or [])):
                self.log.emit("halt_notice_skipped", reason="identical halt notice already posted")
                return
            spec = self._spec("linear", f"halt-notice-{time.strftime('%H%M%S')}")
            spec.phase = "halt_notice"
            spec.gate = PolicyGate(dataclasses.replace(self.gate.ctx, phase="halt_notice"), log_event=self._log_policy)
            spec.budget_usd = 0.75 if self.cfg.total_budget_usd is not None else None
            spec.model = self.cfg.reader_model
            spec.read_only = True
            spec.wait_mcp = (self.cfg.linear_server,)
            sess = self.make_session(spec)
            await sess.start()
            try:
                body = halt_comment_text(st.issue, st.run_id, h.reason, h.phase or self.current_phase, resume)
                turn = await sess.send(
                    f"Post exactly one comment on Linear issue {st.issue} using {self.cfg.linear_tool_prefix}save_comment with issueId=\"{st.issue}\" and this body, verbatim. {ONLY_LISTED_PARAMS} Then reply DONE:\n\n{body}",
                    timeout_s=300,
                )
                self._account(turn, st.current_phase or "feature_start")
            finally:
                await sess.close()
            self.log.emit("halt_notice_posted", issue=st.issue)
        except Exception as exc:
            self.log.emit("halt_notice_failed", error=repr(exc))

    # ==================================================================================
    # misc helpers
    # ==================================================================================

    def _cmd(self, key: str) -> str:
        c = self.profile.commands.get(key)
        if not c:
            raise Halt(f"no command found for {key!r} in {self.repo} (checked .claude/skills and .claude/commands); set commands.{key} in .autopilot.toml")
        return c if c.startswith("/") else "/" + c

    def _fmt(self) -> dict[str, str]:
        st = self.state
        return {
            "issue": st.issue,
            "description": st.description,
            "spec": st.flags.get("spec_id", ""),
            "slug": st.flags.get("slug", ""),
            "num": st.flags.get("num", ""),
            "base": self.base,
            "branch": st.feature_branch or "",
            "title": st.linear.get("title") or st.description,
        }

    def _ship_args(self, fmt: dict[str, str]) -> str:
        """ship_args, always starting with the authorisation line repo-side skills look for (DESIGN.md §10), even when
        a repo-level .autopilot.toml overrides ship_args without it."""
        args = self.cfg.ship_args.format(**fmt).strip()
        # The skills honour the line only when it stands alone; the phrase inside a sentence authorises nothing.
        if any(line.strip() == SHIP_AUTHORIZATION for line in args.splitlines()):
            return args
        return f"{SHIP_AUTHORIZATION}\n{args}" if args else SHIP_AUTHORIZATION

    def _early_done(self, phase: str) -> bool:
        """SPEC §7's early route check is asked once per phase entry; persisted so a resumed analyze/review does not
        ask again (cleared when the phase passes, is routed back into, or restarted with --from)."""
        return bool((self.state.flags.get("early_checked") or {}).get(phase))

    def _mark_early_done(self, phase: str) -> None:
        self.state.flags.setdefault("early_checked", {})[phase] = True
        self.save()

    def _log_gate(self, gate: G.GateResult) -> None:
        self.log.emit("gate", phase=gate.phase, passed=gate.passed, failed=gate.failed(), warnings=gate.warnings(), facts={k: v for k, v in gate.facts.items() if k != "detail"}, summary=gate.summary()[:300])

    def _log_policy(self, event: str, **kw: Any) -> None:
        if hasattr(self, "log"):
            self.log.emit(event, **kw)

    # ---------------------------------------------------------------- stopping (SIGTERM / SIGINT / --stop)
    def request_stop(self, source: str = "stop request") -> None:
        """Stop the run gracefully, from anywhere: the loop's signal dispatch, a tooling thread or a coroutine.
        Marks the stop, kills the running tooling commands (whole process groups), then, on the event loop, cancels
        the in-flight judge / Linear-reader call and interrupts the live worker turn. The run ends "stopped"."""
        first = not self.stop_requested
        self.stop_requested = True
        self._stop_source = self._stop_source or source
        killed = kill_running_commands()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._stop_on_loop, source, killed, first)
                return
            except RuntimeError:  # the loop closed between the check and the call
                pass
        if hasattr(self, "log"):
            self.log.emit("signal", signal=source, killed=killed, summary="stop requested: stopping gracefully")

    def _stop_on_loop(self, source: str, killed: list[int], first: bool) -> None:
        if hasattr(self, "log"):
            if first:
                self.log.emit("signal", signal=source, summary="stop requested: stopping gracefully")
            if killed:
                self.log.emit("tooling_killed", pids=killed)
        cancelled = [t for t in list(self._inflight) if not t.done()]
        for task in cancelled:
            task.cancel()
        if cancelled and hasattr(self, "log"):
            self.log.emit("inflight_cancelled", count=len(cancelled), summary=f"cancelled {len(cancelled)} in-flight judge/reader call(s)")
        if self._stop_event is not None:
            self._stop_event.set()
        if self.session is not None:
            t = asyncio.ensure_future(self.session.interrupt())
            self._signal_tasks.add(t)
            t.add_done_callback(self._signal_tasks.discard)

    async def _wait_tooling_gone(self) -> None:
        """After a stop: wait (off the loop) until every tooling command is gone. The stop sent SIGTERM and SIGKILLs a
        process group still alive after R.KILL_GRACE_S, so this ends within the grace period; the run only reports
        "stopped" and releases its lock once nothing it started still runs in the repo."""
        left = await asyncio.to_thread(R.wait_for_commands, R.KILL_GRACE_S + 5.0)
        if left and hasattr(self, "log"):
            self.log.emit("tooling_still_running", pids=left, summary=f"WARNING: {len(left)} tooling process group(s) still running after SIGKILL")

    async def _cancellable(self, coro: Awaitable[T], timeout_s: float | None = None) -> T:
        """Await a judge or Linear-reader call as its own task, so a stop can cancel it mid-flight; that surfaces here
        as StopRequested. ``timeout_s`` bounds it (asyncio.TimeoutError, as before)."""
        if self.stop_requested:
            if inspect.iscoroutine(coro):
                coro.close()  # never started: no "was never awaited" warning
            raise StopRequested()
        task = asyncio.ensure_future(coro)
        self._inflight.add(task)
        try:
            if timeout_s is None:
                return await task
            return await asyncio.wait_for(task, timeout=timeout_s)
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if self.stop_requested and task.cancelled() and not (me is not None and me.cancelling()):
                raise StopRequested() from None
            raise
        finally:
            self._inflight.discard(task)

    async def _in_thread(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run blocking work (tooling, the CI wait) in a worker thread so the event loop stays responsive. A stop
        returns at once with StopRequested; the thread's command was already killed and it starts nothing new."""
        if self.stop_requested:
            raise StopRequested()
        fut = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
        if self._stop_event is None:
            return await fut
        stop = asyncio.ensure_future(self._stop_event.wait())
        try:
            done, _ = await asyncio.wait({fut, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if fut in done:
            return fut.result()
        # stopped: the abandoned thread ends with StopRequested (or its killed command); consume that result quietly
        fut.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        raise StopRequested()

    def _install_signals(self) -> None:
        """SIGTERM / SIGINT -> request_stop, dispatched by the event loop. Tooling and the CI wait run in threads now,
        so the loop is free; a plain signal.signal handler would not do: a signal the kernel delivers to the tooling
        thread only runs its Python handler once the main thread wakes, and an idle loop may not wake for an hour."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            prev = signal.getsignal(sig)
            try:
                loop.add_signal_handler(sig, self.request_stop, signal.Signals(sig).name)
            except (NotImplementedError, RuntimeError, ValueError) as exc:  # no Unix signals / not the main thread
                self.log.emit("signal_handler_unavailable", signal=signal.Signals(sig).name, error=repr(exc))
                continue
            self._prev_signals[sig] = prev

    def _restore_signals(self) -> None:
        loop = self._loop
        for sig, prev in self._prev_signals.items():
            try:
                if loop is not None and not loop.is_closed():
                    loop.remove_signal_handler(sig)
                signal.signal(sig, prev if prev is not None else signal.SIG_DFL)
            except (NotImplementedError, RuntimeError, ValueError, TypeError):
                pass
        self._prev_signals.clear()

    def _stoppable_sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop_requested:
                raise StopRequested()
            self.sleep(min(1.0, max(0.0, end - time.monotonic())))

    def _persist_force_rerun(self) -> None:
        self.state.flags["force_rerun"] = sorted(self.force_rerun)


def _name(v: Any) -> str | None:
    if isinstance(v, dict):
        return v.get("name") or v.get("key") or v.get("id")
    return v if isinstance(v, str) else None


def _comment_key(c: dict[str, Any]) -> str:
    """A Linear comment's identity across reads: its id, else a hash of its body."""
    if c.get("id"):
        return f"id:{c['id']}"
    import hashlib

    return "body:" + hashlib.sha256(str(c.get("body", "")).encode("utf-8", errors="replace")).hexdigest()[:16]


def _status_name(v: Any) -> str | None:
    """A Linear status as returned by get_issue: a name, or an object with one."""
    if isinstance(v, dict):
        return v.get("name") or v.get("type")
    return v if isinstance(v, str) else None
