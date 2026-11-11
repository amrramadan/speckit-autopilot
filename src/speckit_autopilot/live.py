"""Live autopilot progress: a status-line row for Claude Code and a refreshing terminal view.

It reads only the run files of each repo (``.autopilot/state.json``, ``.autopilot/lock``, ``.autopilot/run.jsonl``) with
the standard library, so the status line stays fast, and it never shows costs: the runs use the Claude subscription,
so the SDK's USD figures are API-equivalent estimates, not spend.

    python -m speckit_autopilot.live --statusline   < status-line JSON on stdin   (one or more rows, ANSI colours)
    python -m speckit_autopilot.live --watch [--repo DIR] [--interval 5] [--once]

Runs are found through the user-level registry ($SPECKIT_AUTOPILOT_STATE or ~/.local/state/speckit-autopilot/runs/,
written whenever a run starts in a repo) plus the direct children of every directory in $SPECKIT_AUTOPILOT_ROOTS
(os.pathsep-separated). This module deliberately imports nothing from the package, so it stays fast.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SANDBOX_DIR_NAME = ".autopilot-sandboxes"


def _state_dir() -> Path:  # = config.user_state_dir(), duplicated so the status line imports nothing heavy
    env = os.environ.get("SPECKIT_AUTOPILOT_STATE")
    if env:
        return Path(env).expanduser()
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state").expanduser() / "speckit-autopilot"


def scan_roots() -> list[Path]:
    """Extra directories whose children (and their .autopilot-sandboxes/ children) are scanned for runs."""
    return [Path(p).expanduser() for p in os.environ.get("SPECKIT_AUTOPILOT_ROOTS", "").split(os.pathsep) if p]
PHASES: tuple[str, ...] = ("feature_start", "clarify", "plan", "tasks", "analyze", "implement", "review", "ship", "linear")
DONE = ("passed", "already_satisfied", "skipped")
RECENT_S = 12 * 3600  # a run that is not alive is shown for this long after its last update
ACTIVITY_EVENTS = {
    "turn", "judge", "repair", "phase_start", "phase_end", "gate", "escalation", "run_end", "session_ready", "tooling",
    "verify", "halt_notice_posted", "back_edge", "route", "run_start", "human_tasks_snapshot", "tooling_start", "tooling_end",
    "ship_gate", "analyze_nonblocking_done", "pr_created", "merged", "ci", "linear_closeout",
}

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
C = {"reset": "\x1b[0m", "dim": "\x1b[2m", "bold": "\x1b[1m", "red": "\x1b[31m", "green": "\x1b[32m",
     "yellow": "\x1b[33m", "blue": "\x1b[34m", "magenta": "\x1b[35m", "cyan": "\x1b[36m"}
STATUS_STYLE = {  # status -> (icon, colour, label)
    "running": ("●", "green", "running"),
    "paused": ("‖", "yellow", "paused"),
    "halted": ("✖", "red", "halted"),
    "stopped": ("■", "dim", "stopped"),
    "completed": ("✔", "green", "done"),
    "stale": ("?", "yellow", "not running"),
}


def paint(text: str, colour: str, on: bool = True) -> str:
    return f"{C[colour]}{text}{C['reset']}" if on and colour in C else text


def visible_len(text: str) -> int:
    return len(_ANSI.sub("", text))


def clip(text: str, width: int) -> str:
    """Cut ``text`` (which may hold ANSI colours) to ``width`` visible characters."""
    if width <= 0 or visible_len(text) <= width:
        return text
    out, seen, i = [], 0, 0
    while i < len(text) and seen < width - 1:
        m = _ANSI.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        out.append(text[i])
        seen += 1
        i += 1
    return "".join(out) + "…" + C["reset"]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _when(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return None


def _hhmm(iso: str | None) -> str:
    t = _when(iso)
    return time.strftime("%H:%M", time.localtime(t)) if t else "--:--"


def duration(seconds: float) -> str:
    s = max(0, int(seconds))
    h, m = divmod(s // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _tail_events(path: Path, limit: int = 40) -> list[dict[str, Any]]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 96 * 1024))
            lines = fh.read().splitlines()[-limit * 3:]
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in lines:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out[-limit:]


@dataclass
class Run:
    repo: Path
    issue: str = ""
    spec: str = ""
    title: str = ""
    status: str = ""
    alive: bool = False
    pid: int | None = None
    phase: str = ""
    phases: dict[str, dict[str, Any]] = field(default_factory=dict)
    until: str | None = None
    elapsed_s: float = 0.0
    updated: float | None = None
    halt: str = ""
    escalations: int = 0
    back_edges: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    worker: str = ""  # model_mode = auto: the live worker's model/effort, e.g. "opus/high"

    @property
    def name(self) -> str:
        return self.repo.name if self.repo.parent.name != SANDBOX_DIR_NAME else f"{self.repo.name} (sandbox)"

    @property
    def done(self) -> int:
        return sum(1 for p in PHASES if (self.phases.get(p) or {}).get("status") in DONE)

    @property
    def scope(self) -> tuple[str, ...]:
        """The phases this run means to do: --until ship stops before ship."""
        if self.until in PHASES:
            return PHASES[: PHASES.index(self.until)]
        return PHASES

    def activity(self) -> tuple[str, str]:
        """(HH:MM, what) of the latest meaningful event."""
        for e in reversed(self.events):
            ev = e.get("event", "")
            if ev not in ACTIVITY_EVENTS:
                continue
            what = str(e.get("summary") or "").replace("\n", " ").strip()
            if ev == "tooling_start":
                what = f"running {', '.join(k for k, v in (e.get('commands') or {}).items() if v)} ({e.get('stage', '')})"
            elif ev == "tooling_end":
                res = e.get("results") or {}
                what = ("checks passed" if e.get("ok") else "checks FAILED: " + ", ".join(k for k, v in res.items() if (v or {}).get("rc"))) + f" ({e.get('stage', '')})"
            if ev == "judge" and isinstance(e.get("output"), dict):
                what = f"judge {e['output'].get('status', '')}: " + str(e["output"].get("reply") or what)[:200]
            if not what:
                what = f"{ev} {e.get('phase', '')}".strip()
            elif ev not in ("turn",):
                what = f"{ev}: {what}"
            return _hhmm(e.get("ts")), re.sub(r"\s+", " ", what)
        return "--:--", ""


def load_run(repo: Path) -> Run | None:
    root = repo / ".autopilot"
    try:
        st = json.loads((root / "state.json").read_text())
    except (OSError, ValueError):
        return None
    lock: dict[str, Any] = {}
    try:
        lock = json.loads((root / "lock").read_text())
    except (OSError, ValueError):
        pass
    pid = int(lock.get("pid") or 0) or None
    alive = bool(pid) and _pid_alive(int(pid))
    status = str(st.get("status") or "")
    if status == "running" and not alive:
        status = "stale"
    flags = st.get("flags") or {}
    updated = _when(st.get("updated_at"))
    elapsed = float(st.get("elapsed_s") or 0.0)
    if alive and updated:
        elapsed += max(0.0, time.time() - updated)
    halt = st.get("halt") or {}
    return Run(
        repo=repo,
        issue=str(st.get("issue") or ""),
        spec=str(flags.get("spec_id") or ""),
        title=str((st.get("linear") or {}).get("title") or st.get("description") or ""),
        status=status,
        alive=alive,
        pid=pid if alive else None,
        phase=str(st.get("current_phase") or "").split(":")[0],
        phases=st.get("phases") or {},
        until=flags.get("until"),
        elapsed_s=elapsed,
        updated=updated,
        halt=str(halt.get("reason") or "") if isinstance(halt, dict) else str(halt),
        escalations=len(st.get("escalations") or []),
        back_edges=len(st.get("back_edges") or []),
        events=_tail_events(root / "run.jsonl"),
        worker=_worker_label(st),
    )


def _worker_label(st: dict[str, Any]) -> str:
    w = (st.get("flags") or {}).get("worker") or {}
    if not st.get("model_switches") or not isinstance(w, dict):
        return ""  # fixed mode: one model for the whole run, nothing to show
    return f"{w.get('model') or 'default'}/{w.get('effort') or 'default'}"


def repo_dirs() -> list[Path]:
    """Every checkout with autopilot run state: the registry, plus the children of $SPECKIT_AUTOPILOT_ROOTS."""
    cands: list[Path] = []
    try:
        for f in sorted((_state_dir() / "runs").iterdir()):
            try:
                cands.append(Path(f.read_text().strip()))
            except OSError:
                continue
    except OSError:
        pass
    for root in scan_roots():
        for base in (root, root / SANDBOX_DIR_NAME):
            try:
                cands.extend(sorted(base.iterdir()))
            except OSError:
                continue
    out: list[Path] = []
    for d in cands:
        if d not in out and (d / ".autopilot" / "state.json").is_file():
            out.append(d)
    return out


def all_runs(now: float | None = None) -> list[Run]:
    """Runs worth showing: alive ones, and the others updated in the last RECENT_S seconds. Alive first."""
    now = now or time.time()
    runs = [r for r in (load_run(d) for d in repo_dirs()) if r]
    runs = [r for r in runs if r.alive or (r.updated and now - r.updated < RECENT_S)]
    return sorted(runs, key=lambda r: (not r.alive, -(r.updated or 0)))


def bar(run: Run, colour: bool = True) -> str:
    cells = []
    for p in PHASES:
        s = (run.phases.get(p) or {}).get("status", "pending")
        if s in DONE:
            cells.append(paint("▰", "green", colour))
        elif p == run.phase and run.status in ("running", "stale"):
            cells.append(paint("▰", "yellow", colour))
        elif s == "failed" or (p == run.phase and run.status == "halted"):
            cells.append(paint("▰", "red", colour))
        elif p not in run.scope:
            cells.append(paint("▱", "dim", colour))
        else:
            cells.append("▱")
    return "".join(cells)


def _status_bits(run: Run, colour: bool = True) -> str:
    icon, col, label = STATUS_STYLE.get(run.status, ("·", "dim", run.status or "?"))
    return paint(f"{icon} {label}", col, colour)


def status_row(run: Run, width: int, colour: bool = True) -> str:
    """One status-line row for a run: what, where in the pipeline, how long, and the latest activity."""
    rec = run.phases.get(run.phase) or {}
    phase = run.phase or "-"
    extra = []
    if int(rec.get("attempts") or 0) > 1:
        extra.append(f"try {rec['attempts']}")
    if int(rec.get("repairs") or 0):
        extra.append(f"fix {rec['repairs']}")
    if run.worker:
        extra.append(run.worker)
    phase_txt = phase + (f" ({', '.join(extra)})" if extra else "")
    head = " ".join(x for x in (paint("autopilot", "magenta", colour), paint(run.issue, "cyan", colour), run.spec) if x)
    parts = [head, f"{phase_txt} {bar(run, colour)} {run.done}/{len(run.scope)}", _status_bits(run, colour), duration(run.elapsed_s)]
    when, what = run.activity()
    if run.status == "halted" and run.halt:
        parts.append(paint(run.halt, "red", colour))
    elif what:
        parts.append(paint(f"{when} {what}", "dim", colour))
    return clip(" │ ".join(parts), width)


def compact(run: Run, colour: bool = True) -> str:
    icon, col, _label = STATUS_STYLE.get(run.status, ("·", "dim", ""))
    return f"{paint(icon, col, colour)} {run.name} {run.issue} {run.phase} {run.done}/{len(run.scope)}"


def statusline(payload: dict[str, Any], width: int, colour: bool = True) -> list[str]:
    """Rows for Claude Code's status line: this repo's run in full, other live runs in one compact row."""
    ws = payload.get("workspace") or {}
    here = Path(str(ws.get("project_dir") or ws.get("current_dir") or payload.get("cwd") or os.getcwd())).resolve()
    runs = all_runs()
    mine = [r for r in runs if here == r.repo or r.repo in here.parents]
    others = [r for r in runs if r not in mine and r.alive]
    rows = [status_row(r, width, colour) for r in mine[:1]]
    if not mine:  # the workspace root or another folder: every live run, one row each
        rows = [status_row(r, width, colour) for r in others[:3]]
        others = others[3:]
    if others:
        rows.append(clip(paint("also: ", "dim", colour) + "  ".join(compact(r, colour) for r in others), width))
    return rows


def watch_view(runs: list[Run], width: int, colour: bool = True) -> str:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [paint(f"Autopilot live · {now}", "bold", colour) + paint("  (Ctrl-C to quit; no costs shown: subscription)", "dim", colour), ""]
    if not runs:
        lines.append("No autopilot run is active or recent.")
    for r in runs:
        lines.append(paint(r.name, "blue", colour) + "  " + paint(r.issue, "cyan", colour) + "  " + paint(r.title or r.spec, "bold", colour))
        pid = f" · pid {r.pid}" if r.pid else ""
        lines.append(f"  {_status_bits(r, colour)}{pid} · {duration(r.elapsed_s)} · escalations {r.escalations} · back-edges {r.back_edges}")
        steps = []
        for p in PHASES:
            rec = r.phases.get(p) or {}
            s = rec.get("status", "pending")
            if s in DONE:
                steps.append(paint(f"✔ {p}", "green", colour))
            elif p == r.phase and r.status in ("running", "stale"):
                steps.append(paint(f"▶ {p}", "yellow", colour) + (f" ×{rec['attempts']}" if int(rec.get("attempts") or 0) > 1 else ""))
            elif s == "failed" or (p == r.phase and r.status in ("halted", "paused")):
                steps.append(paint(f"✖ {p}" if s == "failed" or r.status == "halted" else f"‖ {p}", "red" if r.status == "halted" else "yellow", colour))
            elif p not in r.scope:
                steps.append(paint(f"· {p}", "dim", colour))
            else:
                steps.append(f"· {p}")
        lines.append("  " + "  ".join(steps))
        lines.append(f"  {bar(r, colour)}  {r.done}/{len(r.scope)} phases")
        if r.halt:
            lines.append("  " + paint(f"stopped because: {r.halt}", "red" if r.status == "halted" else "yellow", colour))
        recent = [e for e in r.events if e.get("event") in ACTIVITY_EVENTS][-6:]
        for e in recent:
            what = re.sub(r"\s+", " ", str(e.get("summary") or "")).strip()
            lines.append(clip(paint(f"    {_hhmm(e.get('ts'))} {e.get('event', ''):<13} {e.get('phase', ''):<10} ", "dim", colour) + what, width))
        lines.append("")
    return "\n".join(clip(x, width) if "\n" not in x else x for x in lines)


def watch(repo: Path | None, interval: float = 5.0, once: bool = False, colour: bool | None = None) -> int:
    colour = sys.stdout.isatty() if colour is None else colour
    try:
        while True:
            runs = [r for r in [load_run(repo)] if r] if repo else all_runs()
            width = os.get_terminal_size().columns if sys.stdout.isatty() else 160
            frame = watch_view(runs, width, colour)
            if once:
                print(frame)
                return 0
            sys.stdout.write("\x1b[H\x1b[2J" + frame + "\n")
            sys.stdout.flush()
            time.sleep(max(1.0, interval))
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--statusline", action="store_true", help="read Claude Code's status-line JSON on stdin, print rows")
    ap.add_argument("--watch", action="store_true", help="refreshing terminal view of the runs")
    ap.add_argument("--repo", default=None, help="with --watch: only this checkout (default: every active or recent run)")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true", help="with --watch: print one frame and exit")
    args = ap.parse_args(argv)
    if args.statusline:
        try:
            raw = sys.stdin.read() if not sys.stdin.isatty() else ""
            payload = json.loads(raw) if raw.strip() else {}
            width = int(os.environ.get("COLUMNS") or 0) or 200
            rows = statusline(payload if isinstance(payload, dict) else {}, max(40, width - 4))
            if rows:
                sys.stdout.write("\n".join(rows))
        except Exception:  # a status line must never fail loudly
            pass
        return 0
    repo = Path(args.repo).resolve() if args.repo else None
    return watch(repo, args.interval, args.once)


if __name__ == "__main__":
    raise SystemExit(main())
