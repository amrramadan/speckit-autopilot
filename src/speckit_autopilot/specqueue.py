"""``speckit-autopilot queue ...``: several specs in one repo, one after another.

The queue only decides WHAT runs next. Every spec is an ordinary run in its own process (its own lock, state, gates and
``auto_resume``), so a queue that dies never takes the run with it, and a restarted queue re-attaches to it.

Rules the design rests on:

* progress is on disk (``.autopilot/queue.json``) and tied to run ids, so a finished spec is never launched again;
* the next spec starts only after the previous run completed, from a clean checkout on an up-to-date base branch;
* the queue never resumes a halted run on its own. ``auto_resume`` does that inside the run (it stops on a halt that
  repeats word for word); after that the queue stops and notifies, and ``queue start`` resumes the halted spec once;
* it never stashes, resets or commits anything, and it never ships past a halt, a pause or a ``--stop``.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import ConfigError, load_config, looks_like_issue_key, resolve_linear
from .repo import Git
from .report import notify
from .state import LockHeld, RunLock, RunPaths, RunState, StateStore, atomic_write, ensure_git_exclude, now_iso, pid_alive

ON_HALT = ("stop", "skip")
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# queued → running → done | halted (its run can be resumed) | skipped (its halted run was archived)
OPEN = ("queued", "running", "halted")


class QueueError(RuntimeError):
    pass


@dataclass
class Item:
    key: str
    description: str = ""
    spec: str | None = None
    no_linear: bool = False
    after: list[str] = field(default_factory=list)
    status: str = "queued"
    reason: str = ""
    run_id: str | None = None
    prev_run_id: str | None = None  # the run that was in state.json before this one was launched
    pid: int | None = None  # the launched process: during preflight it does not hold the run lock yet
    pr_url: str | None = None
    added_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    ended_at: str | None = None


@dataclass
class QueueState:
    items: list[Item] = field(default_factory=list)
    on_halt: str = "stop"
    pause_requested: bool = False
    status: str = "idle"  # idle | running | paused | blocked | finished
    reason: str = ""

    def get(self, key: str) -> Item | None:
        """The newest entry for ``key`` (an issue queued again keeps its finished entry)."""
        return next((i for i in reversed(self.items) if i.key == key), None)

    def unmet(self, item: Item) -> list[str]:
        out = []
        for k in item.after:
            dep = self.get(k)
            if dep is None or dep.status != "done":
                out.append(k)
        return out


class QueueStore:
    def __init__(self, repo: Path):
        self.paths = RunPaths(repo)
        self.file = self.paths.root / "queue.json"
        self.log_file = self.paths.root / "queue.log"
        self.lock = RunLock(self.paths.root / "queue.lock")

    def load(self) -> QueueState:
        if not self.file.exists():
            return QueueState()
        try:
            d = json.loads(self.file.read_text(encoding="utf-8"))
            known = {f.name for f in dataclasses.fields(Item)}
            items = [Item(**{k: v for k, v in i.items() if k in known}) for i in d.get("items", [])]
            return QueueState(items, d.get("on_halt", "stop"), bool(d.get("pause_requested")), d.get("status", "idle"), d.get("reason", ""))
        except (ValueError, TypeError) as exc:
            raise QueueError(f"{self.file} is not readable ({exc}); fix or delete it") from exc

    @contextlib.contextmanager
    def edit(self) -> Iterator[QueueState]:
        """Read, change and write queue.json as one step: the CLI may edit it while a queue process runs."""
        self.paths.root.mkdir(parents=True, exist_ok=True)
        with open(self.paths.root / "queue.json.lock", "w") as fh:
            try:
                import fcntl

                fcntl.flock(fh, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - Windows: no advisory lock, the write below is still atomic
                pass
            q = self.load()
            yield q
            atomic_write(self.file, json.dumps(dataclasses.asdict(q), indent=2, ensure_ascii=False) + "\n")


def run_process_alive(pid: int | None) -> bool:
    """``pid`` is a live autopilot process (not an unrelated one that got the number after a reboot)."""
    if not pid or not pid_alive(pid):
        return False
    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return "-m speckit_autopilot" in r.stdout


def _when(ts: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(ts)  # local time with its offset: never compare these as text


def past_run(paths: RunPaths, key: str, since: str = "") -> RunState | None:
    """A completed run of ``key`` started at or after ``since``, current or archived."""
    for p in (paths.state, *sorted((paths.root / "archive").glob("*/state.json"), reverse=True)):
        try:
            st = StateStore(p).load()
        except (OSError, ValueError, TypeError):
            continue
        if st.issue == key and st.status == "completed" and (not since or _when(st.started_at) >= _when(since)):
            return st
    return None


def default_launcher(args: list[str], paths: RunPaths) -> subprocess.Popen[bytes]:
    """One run as a child in its own session, so ending the queue process leaves the run alone."""
    with open(paths.console, "ab", buffering=0) as log:
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} queue launch: {' '.join(args)}\n".encode())
        return subprocess.Popen(
            [sys.executable, "-m", "speckit_autopilot", *args],
            cwd=paths.repo, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONUNBUFFERED": "1"}, start_new_session=True,
        )


class QueueRunner:
    def __init__(self, repo: Path, *, launcher: Callable[[list[str], RunPaths], Any] = default_launcher, sleep: Callable[[float], None] = time.sleep, echo: Callable[[str], None] = print, poll_s: float = 10.0):
        self.repo = repo
        self.store = QueueStore(repo)
        self.paths = self.store.paths
        self.launcher, self.sleep, self.echo, self.poll_s = launcher, sleep, echo, poll_s
        self.interrupted = False
        self.notify_command: str | None = None

    # ---------------------------------------------------------------- helpers
    def say(self, msg: str) -> None:
        self.echo(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

    def announce(self, msg: str) -> None:
        self.say(msg)
        notify(self.notify_command, f"Autopilot queue: {msg}")

    def run_state(self) -> RunState | None:
        return StateStore(self.paths.state).load() if self.paths.state.exists() else None

    def state_of(self, item: Item) -> RunState | None:
        """The saved run of ``item``: the current one, or an archived one (a later run moved it there)."""
        st = self.run_state()
        if st and st.issue == item.key and (st.run_id == item.run_id or (item.run_id is None and st.run_id != item.prev_run_id)):
            return st
        if item.run_id:
            p = self.paths.root / "archive" / item.run_id / "state.json"
            if p.exists():
                return StateStore(p).load()
        return None

    def update(self, item: Item, **kw: Any) -> None:
        with self.store.edit() as q:
            cur = next((i for i in q.items if i.key == item.key and i.added_at == item.added_at), None)
            for target in (item, cur):
                for k, v in kw.items():
                    if target is not None:
                        setattr(target, k, v)

    def finish(self, status: str, reason: str, *, loud: bool = True) -> int:
        with self.store.edit() as q:
            q.status, q.reason = status, reason
        (self.announce if loud else self.say)(reason)
        return 0 if status in ("finished", "paused") else 2

    def live_run(self) -> bool:
        return RunLock(self.paths.lock).holder_alive()

    # ---------------------------------------------------------------- the loop
    def run(self) -> int:
        self.store.lock.acquire("queue")
        try:
            try:
                return self._run()
            except (QueueError, ConfigError) as exc:
                return self.finish("blocked", str(exc))
            except Exception as exc:
                self.finish("blocked", f"the queue failed: {exc!r}")
                raise
        finally:
            self.store.lock.release()

    def _run(self) -> int:
        with self.store.edit() as q:
            q.status, q.reason, q.pause_requested = "running", "", False
        while True:
            self.notify_command = load_config(self.repo).notify_command
            q = self.store.load()
            item = next((i for i in q.items if i.status == "running"), None)
            if item is not None:  # this queue's predecessor died: follow the run it launched
                self.say(f"{item.key}: re-attaching to its run")
                if not self._wait_for_lock(item.pid):
                    return self._interrupted(item)
                code = self._settle(item, None)
                if code is not None:
                    return code
                continue
            if q.pause_requested:
                return self.finish("paused", "paused as requested; `queue start` continues")
            item = next((i for i in q.items if i.status == "halted"), None)
            if item is not None:
                code = self._resume(item)  # once per start: a second halt ends the queue (or skips the spec)
            else:
                item = next((i for i in q.items if i.status == "queued" and not q.unmet(i)), None)
                if item is None:
                    waiting = [f"{i.key} (waits for {', '.join(q.unmet(i))})" for i in q.items if i.status == "queued"]
                    if waiting:
                        return self.finish("blocked", "nothing can start: " + "; ".join(waiting))
                    done = sum(1 for i in q.items if i.status == "done")
                    skipped = [i.key for i in q.items if i.status == "skipped"]
                    return self.finish("finished", f"queue finished: {done} done" + (f", skipped: {', '.join(skipped)}" if skipped else ""))
                code = self._launch(item)
            if code is not None:
                return code

    def _interrupted(self, item: Item | None) -> int:
        tail = f"; the run for {item.key} continues on its own, `queue start` re-attaches" if item and (self.live_run() or run_process_alive(item.pid)) else ""
        return self.finish("paused", "queue process stopped" + tail, loud=False)

    def _wait_for_lock(self, pid: int | None = None) -> bool:
        while self.live_run() or run_process_alive(pid):
            if self.interrupted:
                return False
            self.sleep(self.poll_s)
        return not self.interrupted

    def _wait(self, proc: Any) -> int | None:
        while proc.poll() is None:
            if self.interrupted:
                return None
            self.sleep(self.poll_s)
        return proc.returncode

    def _launch(self, item: Item) -> int | None:
        if self.live_run():
            self.say(f"{item.key}: waiting for the run that is already live in this checkout")
            if not self._wait_for_lock():
                return self._interrupted(None)
        done = past_run(self.paths, item.key, item.added_at)
        if done:  # somebody ran it by hand while it was queued
            self.update(item, status="done", run_id=done.run_id, pr_url=done.ship.get("pr_url"), ended_at=now_iso())
            self.say(f"{item.key}: already completed by run {done.run_id}")
            return None
        prev = self.run_state()
        if prev and prev.status != "completed":
            return self.finish("blocked", f"{item.key} cannot start: the run for {prev.issue} is {prev.status}. Finish it (--resume) or archive it (--abandon), then `queue start`.")
        problem = self._prepare_checkout()
        if problem:
            return self.finish("blocked", f"{item.key} cannot start: {problem}")
        args = ["--repo", str(self.repo)]
        if item.no_linear:
            args.append("--no-linear")
        args += ["--issue", item.key]
        if item.spec:
            args += ["--spec", item.spec]
        if item.description:
            args += ["--", item.description]
        self.update(item, status="running", reason="", run_id=None, prev_run_id=prev.run_id if prev else None, started_at=now_iso(), ended_at=None)
        self.say(f"{item.key}: starting")
        return self._follow(item, args)

    def _resume(self, item: Item) -> int | None:
        if self.live_run():  # somebody resumed it by hand
            if not self._wait_for_lock():
                return self._interrupted(item)
            return self._settle(item, None)
        st = self.state_of(item)
        if st is None:
            return self.finish("blocked", f"{item.key} has no saved run any more: `queue retry {item.key}` starts it again, `queue remove {item.key}` drops it")
        if st.status == "completed":  # finished by hand meanwhile
            return self._settle(item, None)
        cur = self.run_state()
        if cur is None or cur.run_id != st.run_id:
            return self.finish("blocked", f"{item.key}: its run {st.run_id} was archived, so it cannot be resumed. `queue retry {item.key}` starts it again, `queue remove {item.key}` drops it")
        self.update(item, status="running", reason="", ended_at=None)
        self.say(f"{item.key}: resuming its {st.status} run")
        return self._follow(item, ["--repo", str(self.repo), "--resume"])

    def _follow(self, item: Item, args: list[str]) -> int | None:
        self.paths.ensure()
        proc = self.launcher(args, self.paths)
        self.update(item, pid=proc.pid)
        rc = self._wait(proc)
        if rc is None:
            return self._interrupted(item)
        return self._settle(item, rc)

    def _settle(self, item: Item, rc: int | None) -> int | None:
        """The item's run is over: record what became of it. None: the queue goes on."""
        st = self.state_of(item)
        if st is None:  # no run was ever created (preflight, bad config, lock): nothing to resume, so it stays queued
            self.update(item, status="queued", reason="did not start", started_at=None)
            return self.finish("blocked", f"{item.key} did not start (exit code {rc}); see {self.paths.console} and {self.paths.root / 'PREFLIGHT.md'}")
        if st.status == "completed":
            self.update(item, status="done", reason="", run_id=st.run_id, pr_url=st.ship.get("pr_url"), ended_at=now_iso())
            self.announce(f"{item.key} done" + (f" ({item.pr_url})" if item.pr_url else ""))
            return None
        dead = "its process ended without a checkpoint" if st.status == "running" else ""
        reason = " ".join(str(dead or (st.halt or {}).get("reason") or st.flags.get("paused_by") or st.status).split())[:400]
        if rc == 1:
            reason = f"resume did not start (see PREFLIGHT.md / console.log); run is {st.status}: {reason}"
        self.update(item, status="halted", reason=f"{st.status}: {reason}", run_id=st.run_id, ended_at=now_iso())
        if st.status != "halted" or self.store.load().on_halt != "skip":
            # paused (a person's work, --until) and stopped (--stop) always end the queue: somebody asked for that
            return self.finish("blocked", f"{item.key} {st.status}: {reason}. Fix it, then `queue start` resumes it.")
        return self._skip(item, st)

    def _skip(self, item: Item, st: RunState) -> int | None:
        if st.ship.get("started") and not st.ship.get("merged"):
            return self.finish("blocked", f"{item.key} halted while shipping; not skipped, look at its PR first: {item.reason}")
        g = Git(self.repo)
        if not g.is_clean():
            return self.finish("blocked", f"{item.key} {item.reason}. Not skipped: its uncommitted work is in the checkout and the queue never stashes or commits it.")
        from .__main__ import abandon_run

        with contextlib.redirect_stdout(open(os.devnull, "w")):
            code = abandon_run(self.paths)
        if code:
            return self.finish("blocked", f"{item.key} {item.reason}. Not skipped: its run could not be archived.")
        self.update(item, status="skipped")
        self.announce(f"{item.key} skipped ({item.reason}); its branch {st.feature_branch or '?'} is untouched")
        return None

    def _prepare_checkout(self) -> str | None:
        """Clean checkout, on the base branch, fast-forwarded. Returns what is wrong, changing nothing else."""
        g = Git(self.repo)
        if not g.is_clean():
            return "the checkout has uncommitted changes: " + ", ".join(p for _, p in g.status_entries()[:5])
        base = load_config(self.repo).base_branch or g.origin_head_branch() or "main"
        for args in (["fetch", "-q", "origin"], *([["switch", "-q", base]] if g.current_branch() != base else []), ["pull", "-q", "--ff-only", "origin", base]):
            r = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, timeout=300, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            if r.returncode:
                return f"`git {' '.join(args)}` failed: {(r.stderr or r.stdout).strip()[:300]}"
        return None


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def add_items(store: QueueStore, entries: list[str], *, description: str = "", spec: str | None = None, after: list[str] | None = None, no_linear: bool = False, again: bool = False, first: bool = False) -> list[Item]:
    """Validate everything before queueing anything."""
    from .runner import slugify

    repo = store.paths.repo
    if not entries:
        if not description:
            raise QueueError("give at least one issue key, or --description for a spec without one")
        entries, no_linear = [slugify(description, load_config(repo).slug_words)], True
    if len(entries) > 1 and (description or spec):
        raise QueueError("--description and --spec describe one spec: add it on its own (or write KEY=SPEC)")
    new: list[Item] = []
    for e in entries:
        key, _, sp = e.partition("=")
        if not KEY_RE.match(key) or (sp and not KEY_RE.match(sp)):
            raise QueueError(f"not an issue key: {e!r} (use KEY or KEY=SPEC, e.g. ENG-142 or ENG-142=031)")
        cfg = load_config(repo, {"issue": key, "linear": "off" if no_linear else None})
        linear_on, why = resolve_linear(cfg)
        item = Item(key=key, description=description, spec=sp or spec, no_linear=no_linear, after=list(after or []))
        if not description and not (linear_on and looks_like_issue_key(key)):
            raise QueueError(f"{key} needs --description: without Linear there is no issue title to use ({why})")
        if cfg.spec_id_style == "key" and not item.spec and not (linear_on and cfg.spec_title_regex):
            raise QueueError(f"{key} needs a spec id here (spec_id_style = key): write {key}=<SPEC>")
        new.append(item)
    keys = [i.key for i in new]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise QueueError(f"named twice: {', '.join(dup)}")
    with store.edit() as q:
        for i in new:
            old = q.get(i.key)
            if old and old.status in OPEN:
                raise QueueError(f"{i.key} is already in the queue ({old.status})")
            if old and not again:
                raise QueueError(f"{i.key} is already {old.status}; add --again to run it once more")
            ran = past_run(store.paths, i.key)
            if ran and not again:
                raise QueueError(f"{i.key} already completed (run {ran.run_id}); add --again to run it once more")
            cur = StateStore(store.paths.state).load() if store.paths.state.exists() else None
            if cur and cur.issue == i.key and cur.status != "completed":
                raise QueueError(f"{i.key} already has a run here ({cur.status}): resume or abandon it instead of queueing it")
            for dep in i.after:
                if dep == i.key or (q.get(dep) is None and dep not in keys[: keys.index(i.key)]):
                    raise QueueError(f"{i.key}: --after {dep} is not in the queue")
        if first:
            at = next((n for n, i in enumerate(q.items) if i.status == "queued"), len(q.items))
            q.items[at:at] = new
        else:
            q.items += new
    return new


def remove_item(store: QueueStore, key: str) -> None:
    with store.edit() as q:
        item = q.get(key)
        if item is None:
            raise QueueError(f"{key} is not in the queue")
        if item.status == "running":
            raise QueueError(f"{key} is running: stop it first (--stop)")
        deps = [i.key for i in q.items if i.status == "queued" and key in i.after and i is not item]
        if deps and item.status != "done":
            raise QueueError(f"{', '.join(deps)} wait(s) for {key}: remove them first")
        if deps:
            raise QueueError(f"{', '.join(deps)} wait(s) for {key}, which is done: keep it until they ran")
        q.items.remove(item)


def retry_item(store: QueueStore, key: str) -> None:
    with store.edit() as q:
        item = q.get(key)
        if item is None or item.status not in ("halted", "skipped"):
            raise QueueError(f"{key} is {'not in the queue' if item is None else item.status}: only a halted or skipped spec can be retried from the start")
        item.status, item.reason, item.run_id, item.prev_run_id, item.pid, item.started_at, item.ended_at = "queued", "", None, None, None, None, None


def render(store: QueueStore) -> str:
    q = store.load()
    alive = store.lock.holder_alive()
    if q.status == "running" and not alive:
        head = "not running (its process ended; `queue start` re-attaches)"
    else:
        head = q.status + (" · pause requested" if q.pause_requested and alive else "")
    lines = [f"Queue: {head} · on halt: {q.on_halt}" + (f" · pid {store.lock.read().get('pid')}" if alive else "")]
    if q.reason:
        lines.append(f"  {q.reason}")
    if not q.items:
        lines.append("  (empty)")
    marks = {"done": "✓", "running": "▶", "halted": "!", "skipped": "-", "queued": "·"}
    for n, i in enumerate(q.items, 1):
        extra = i.pr_url or i.reason
        if i.status == "queued" and q.unmet(i):
            extra = f"waits for {', '.join(q.unmet(i))}"
        lines.append(f"  {n}. {marks.get(i.status, '?')} {i.key}" + (f" (spec {i.spec})" if i.spec else "") + f" {i.status}" + (f": {extra}" if extra else ""))
    return "\n".join(lines)


def start(store: QueueStore, *, on_halt: str | None, foreground: bool) -> int:
    paths = store.paths
    info = store.lock.read()
    if store.lock.holder_alive():
        raise QueueError(f"the queue is already running (pid {info and info.get('pid')})")
    with store.edit() as q:
        if on_halt:
            q.on_halt = on_halt
        if not any(i.status in OPEN for i in q.items):
            raise QueueError("nothing to run: the queue is empty or finished (`queue add KEY ...`)")
    ensure_git_exclude(paths.repo)
    paths.ensure()
    if foreground:
        r = QueueRunner(paths.repo, echo=lambda m: print(m, flush=True))

        def _stop(*_: Any) -> None:
            r.interrupted = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        return r.run()
    with open(store.log_file, "ab", buffering=0) as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "speckit_autopilot", "queue", "start", "--foreground", "--repo", str(paths.repo)],
            cwd=paths.repo, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONUNBUFFERED": "1"}, start_new_session=True,
        )
    for _ in range(50):  # a queue that cannot even start says so here instead of only in its log
        if store.lock.holder_alive() or proc.poll() is not None:
            break
        time.sleep(0.1)
    print(json.dumps({"pid": proc.pid, "log": str(store.log_file), "console": str(paths.console), "events": str(paths.events)}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="speckit-autopilot queue", description="run several specs in one repo, one after another")
    sub = p.add_subparsers(dest="action", required=True)

    def cmd(name: str, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--repo", default=None, help="target repository (default: the git repo of the current directory)")
        return sp

    a = cmd("add", "add specs to the end of the queue")
    a.add_argument("keys", nargs="*", metavar="KEY[=SPEC]", help="issue key, with an optional spec id")
    a.add_argument("--description", default="", help="feature description (default: the Linear issue title)")
    a.add_argument("--spec", default=None)
    a.add_argument("--after", action="append", default=[], metavar="KEY", help="start only once KEY is done (repeatable)")
    a.add_argument("--no-linear", action="store_true", help="run it without Linear")
    a.add_argument("--first", action="store_true", help="put it before the other queued specs")
    a.add_argument("--again", action="store_true", help="queue a spec that is already done or skipped once more")
    s = cmd("start", "start or continue the queue in the background (resumes a halted spec once)")
    s.add_argument("--on-halt", choices=ON_HALT, default=None, help="stop (default): a halt ends the queue. skip: archive the halted run and go on, when its work is committed")
    s.add_argument("--foreground", action="store_true", help="stay attached instead of running in the background")
    cmd("status", "show the queue").add_argument("--json", action="store_true")
    cmd("pause", "stop the queue after the spec that is running now")
    for name, h in (("remove", "drop a spec that is not running"), ("retry", "queue a halted or skipped spec again from the start")):
        cmd(name, h).add_argument("key")
    cmd("clear", "drop every spec that is not running or halted")
    return p


def main(argv: list[str]) -> int:
    from .__main__ import resolve_repo

    args = build_parser().parse_args(argv)
    store = QueueStore(resolve_repo(args.repo))
    try:
        if args.action == "start":
            return start(store, on_halt=args.on_halt, foreground=args.foreground)
        if args.action == "status" and args.json:
            q = store.load()
            print(json.dumps({**dataclasses.asdict(q), "alive": store.lock.holder_alive()}, indent=2, ensure_ascii=False))
            return 0
        if args.action == "add":
            ensure_git_exclude(store.paths.repo)
            added = add_items(store, args.keys, description=args.description, spec=args.spec, after=args.after, no_linear=args.no_linear, again=args.again, first=args.first)
            print("added " + ", ".join(i.key for i in added))
        elif args.action == "pause":
            if not store.lock.holder_alive():
                raise QueueError("the queue is not running")
            with store.edit() as q:
                q.pause_requested = True
            print("the queue stops after the spec that is running now (to stop that run too: --stop)")
        elif args.action == "remove":
            remove_item(store, args.key)
        elif args.action == "retry":
            retry_item(store, args.key)
        elif args.action == "clear":
            with store.edit() as q:
                q.items = [i for i in q.items if i.status in ("running", "halted")]
        print(render(store))
        return 0
    except (QueueError, ConfigError, LockHeld) as exc:
        print(f"queue: {exc}", file=sys.stderr)
        return 1
