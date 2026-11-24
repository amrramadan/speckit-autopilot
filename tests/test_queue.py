"""speckit-autopilot queue: several specs in one repo, one after another (real git, fake runs, no API calls)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import speckit_autopilot.__main__ as cli
from speckit_autopilot import specqueue as Q
from speckit_autopilot.state import RunLock, RunPaths, RunState, StateStore, ensure_git_exclude

from .fakeworld import World, make_runner
from .test_e2e import run as run_pipeline
from .test_e2e import state as world_state


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    origin, r = tmp_path / "origin.git", tmp_path / "app"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        git(r, "config", k, v)
    (r / ".autopilot.toml").write_text('linear = "on"\nlinear_workspace_url = "https://linear.app/acme"\n')
    git(r, "add", ".autopilot.toml")
    git(r, "commit", "-q", "-m", "init")
    git(r, "remote", "add", "origin", str(origin))
    git(r, "push", "-q", "-u", "origin", "main")
    git(r, "remote", "set-head", "origin", "main")
    ensure_git_exclude(r)
    return r


class FakeProc:
    def __init__(self, rc: int, pid: int = 999_999):
        self.returncode, self.pid = rc, pid

    def poll(self):
        return self.returncode


class Runs:
    """A launcher that plays runs: ``script[key]`` is the list of statuses its successive processes end with."""

    def __init__(self, repo: Path, **script):
        self.repo, self.script, self.calls, self.n, self.hook = repo, script, [], 0, None

    def __call__(self, args, paths):
        self.calls.append(args)
        store = StateStore(paths.state)
        resume = "--resume" in args
        key = store.load().issue if resume else args[args.index("--issue") + 1]
        outcomes = self.script.get(key.replace("-", "_"), ["completed"])
        status = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if self.hook:
            self.hook(key, status)
        if status == "preflight":
            return FakeProc(1)
        if resume:
            st = store.load()
        else:
            if store.exists():
                old = store.load()
                d = paths.root / "archive" / old.run_id
                d.mkdir(parents=True)
                paths.state.rename(d / "state.json")
            self.n += 1
            st = RunState(f"run-{self.n}", key, "d", str(self.repo), feature_branch=f"feature/{key}")
        st.status = status.split(":")[0]
        st.halt = {"reason": status.partition(":")[2] or "gate failed"} if st.status == "halted" else None
        if st.status == "completed":
            st.ship = {"pr_url": f"https://example.com/{key}", "merged": True, "started": True}
        store.save(st)
        return FakeProc({"completed": 0, "paused": 0, "halted": 2, "stopped": 3, "running": -9}[st.status])

    def started(self):
        return [a[a.index("--issue") + 1] if "--issue" in a else "resume" for a in self.calls]


def drain(repo, runs, **kw):
    r = Q.QueueRunner(repo, launcher=runs, sleep=lambda s: None, echo=lambda m: None, **kw)
    return r.run(), r


def add(repo, *keys, **kw):
    return Q.add_items(Q.QueueStore(repo), list(keys), **kw)


def statuses(repo):
    return {i.key: i.status for i in Q.QueueStore(repo).load().items}


# ---------------------------------------------------------------- order and progress


def test_specs_run_in_order_and_finish(repo):
    add(repo, "ENG-1", "ENG-2=031")
    runs = Runs(repo)
    assert drain(repo, runs)[0] == 0
    assert runs.calls == [
        ["--repo", str(repo), "--issue", "ENG-1"],
        ["--repo", str(repo), "--issue", "ENG-2", "--spec", "031"],
    ]
    q = Q.QueueStore(repo).load()
    assert q.status == "finished" and [i.status for i in q.items] == ["done", "done"]
    assert q.items[0].pr_url == "https://example.com/ENG-1" and q.items[0].run_id == "run-1"


def test_a_finished_spec_is_never_launched_again_after_a_restart(repo):
    add(repo, "ENG-1", "ENG-2")
    runs = Runs(repo, ENG_2=["halted", "completed"])
    assert drain(repo, runs)[0] == 2
    assert drain(repo, runs)[0] == 0
    assert runs.started() == ["ENG-1", "ENG-2", "resume"]
    with pytest.raises(Q.QueueError, match="already done"):
        add(repo, "ENG-1")
    with pytest.raises(Q.QueueError, match="nothing to run"):
        Q.start(Q.QueueStore(repo), on_halt=None, foreground=True)


def test_specs_added_while_it_runs_are_picked_up(repo):
    add(repo, "ENG-1")
    runs = Runs(repo)
    runs.hook = lambda key, _s: add(repo, "ENG-9") if key == "ENG-1" else None
    assert drain(repo, runs)[0] == 0
    assert runs.started() == ["ENG-1", "ENG-9"]


def test_after_waits_for_the_dependency_whatever_the_order(repo):
    add(repo, "ENG-1", "ENG-2")
    add(repo, "ENG-3", after=["ENG-2"], first=True)
    runs = Runs(repo)
    drain(repo, runs)
    assert runs.started() == ["ENG-1", "ENG-2", "ENG-3"]


def test_pause_stops_after_the_current_spec(repo):
    add(repo, "ENG-1", "ENG-2")
    runs = Runs(repo)

    def pause(*_):
        with Q.QueueStore(repo).edit() as q:
            q.pause_requested = True

    runs.hook = pause
    assert drain(repo, runs)[0] == 0
    assert statuses(repo) == {"ENG-1": "done", "ENG-2": "queued"} and Q.QueueStore(repo).load().status == "paused"
    runs.hook = None
    assert drain(repo, runs)[0] == 0 and statuses(repo)["ENG-2"] == "done"


# ---------------------------------------------------------------- halts


def test_a_halt_stops_the_queue_and_start_resumes_it_once(repo):
    add(repo, "ENG-1", "ENG-2")
    runs = Runs(repo, ENG_1=["halted:gate x failed"])
    assert drain(repo, runs)[0] == 2
    q = Q.QueueStore(repo).load()
    assert q.status == "blocked" and "gate x failed" in q.reason and statuses(repo) == {"ENG-1": "halted", "ENG-2": "queued"}
    assert len(runs.calls) == 1  # no blind resume
    assert drain(repo, runs)[0] == 2
    assert runs.started() == ["ENG-1", "resume"]  # one resume per start, then it stops again


@pytest.mark.parametrize("end", ["paused", "stopped", "running"])
def test_paused_stopped_and_crashed_runs_end_the_queue_even_with_skip(repo, end):
    add(repo, "ENG-1", "ENG-2")
    with Q.QueueStore(repo).edit() as q:
        q.on_halt = "skip"
    runs = Runs(repo, ENG_1=[end])
    assert drain(repo, runs)[0] == 2
    assert runs.started() == ["ENG-1"] and statuses(repo)["ENG-1"] == "halted"
    if end == "running":
        assert "without a checkpoint" in Q.QueueStore(repo).load().reason


def test_skip_archives_the_halted_run_and_leaves_dependants_waiting(repo):
    add(repo, "ENG-1", "ENG-2", "ENG-3")
    add(repo, "ENG-4", after=["ENG-1"])
    with Q.QueueStore(repo).edit() as q:
        q.on_halt = "skip"
    runs = Runs(repo, ENG_1=["halted"])
    runs.hook = lambda key, _s: git(repo, "switch", "-q", "-c", "feature/ENG-1") if key == "ENG-1" else None
    code, _ = drain(repo, runs)
    assert runs.started() == ["ENG-1", "ENG-2", "ENG-3"]
    assert statuses(repo) == {"ENG-1": "skipped", "ENG-2": "done", "ENG-3": "done", "ENG-4": "queued"}
    assert code == 2 and "ENG-4 (waits for ENG-1)" in Q.QueueStore(repo).load().reason
    assert (repo / ".autopilot/archive/run-1-abandoned/state.json").exists()
    assert git(repo, "branch", "--show-current") == "main" and "feature/ENG-1" in git(repo, "branch")


def test_skip_never_touches_uncommitted_work(repo):
    add(repo, "ENG-1", "ENG-2")
    with Q.QueueStore(repo).edit() as q:
        q.on_halt = "skip"
    runs = Runs(repo, ENG_1=["halted"])
    runs.hook = lambda *_: (repo / "wip.py").write_text("x = 1\n")
    assert drain(repo, runs)[0] == 2
    assert statuses(repo)["ENG-1"] == "halted" and (repo / "wip.py").exists() and (repo / ".autopilot/state.json").exists()
    assert "never stashes" in Q.QueueStore(repo).load().reason


def test_skip_never_drops_a_run_that_started_shipping(repo):
    add(repo, "ENG-1", "ENG-2")
    with Q.QueueStore(repo).edit() as q:
        q.on_halt = "skip"
    runs = Runs(repo, ENG_1=["halted"])
    orig = StateStore.save

    def save(self, st):
        st.ship = {"started": True}
        orig(self, st)

    mp = pytest.MonkeyPatch()
    mp.setattr(StateStore, "save", save)
    try:
        assert drain(repo, runs)[0] == 2
    finally:
        mp.undo()
    assert statuses(repo)["ENG-1"] == "halted" and runs.started() == ["ENG-1"]


def test_retry_starts_again_only_after_the_old_run_is_archived(repo):
    add(repo, "ENG-1")
    runs = Runs(repo, ENG_1=["halted", "completed"])
    drain(repo, runs)
    Q.retry_item(Q.QueueStore(repo), "ENG-1")
    assert drain(repo, runs)[0] == 2 and "--abandon" in Q.QueueStore(repo).load().reason
    assert cli.abandon_run(RunPaths(repo)) == 0
    assert drain(repo, runs)[0] == 0 and runs.started() == ["ENG-1", "ENG-1"]


def test_a_halted_spec_whose_run_was_abandoned_is_not_resumed(repo):
    add(repo, "ENG-1")
    runs = Runs(repo, ENG_1=["halted"])
    drain(repo, runs)
    cli.abandon_run(RunPaths(repo))
    assert drain(repo, runs)[0] == 2
    assert runs.started() == ["ENG-1"] and "queue retry ENG-1" in Q.QueueStore(repo).load().reason


def test_a_halted_spec_finished_by_hand_counts_as_done(repo):
    add(repo, "ENG-1", "ENG-2")
    runs = Runs(repo, ENG_1=["halted"])
    drain(repo, runs)
    store = StateStore(RunPaths(repo).state)
    st = store.load()
    st.status = "completed"
    store.save(st)
    assert drain(repo, runs)[0] == 0
    assert runs.started() == ["ENG-1", "ENG-2"] and statuses(repo)["ENG-1"] == "done"


# ---------------------------------------------------------------- the checkout


def test_a_dirty_checkout_blocks_before_anything_starts(repo):
    add(repo, "ENG-1")
    (repo / "stray.txt").write_text("x")
    runs = Runs(repo)
    assert drain(repo, runs)[0] == 2
    assert not runs.calls and "stray.txt" in Q.QueueStore(repo).load().reason and statuses(repo)["ENG-1"] == "queued"


def test_the_base_branch_is_checked_out_and_fast_forwarded(repo, tmp_path):
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        git(other, "config", k, v)
    (other / "new.txt").write_text("n")
    git(other, "add", "new.txt")
    git(other, "commit", "-q", "-m", "new")
    git(other, "push", "-q")
    git(repo, "switch", "-q", "-c", "feature/old")
    add(repo, "ENG-1")
    assert drain(repo, Runs(repo))[0] == 0
    assert git(repo, "branch", "--show-current") == "main" and (repo / "new.txt").exists()


def test_a_diverged_base_blocks(repo, tmp_path):
    test_the_base_branch_is_checked_out_and_fast_forwarded(repo, tmp_path)
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    (repo / "local.txt").write_text("l")
    git(repo, "add", "local.txt")
    git(repo, "commit", "-q", "-m", "local")
    add(repo, "ENG-2")
    runs = Runs(repo)
    assert drain(repo, runs)[0] == 2
    assert not runs.calls and "pull" in Q.QueueStore(repo).load().reason


def test_an_unfinished_run_of_another_issue_blocks(repo):
    add(repo, "ENG-1")
    RunPaths(repo).ensure()
    StateStore(RunPaths(repo).state).save(RunState("r0", "ENG-7", "d", str(repo), status="halted"))
    runs = Runs(repo)
    assert drain(repo, runs)[0] == 2
    assert not runs.calls and "ENG-7 is halted" in Q.QueueStore(repo).load().reason


def test_a_run_that_fails_preflight_stays_queued_and_blocks(repo):
    add(repo, "ENG-1", "ENG-2")
    runs = Runs(repo, ENG_1=["preflight", "completed"])
    assert drain(repo, runs)[0] == 2
    assert statuses(repo) == {"ENG-1": "queued", "ENG-2": "queued"} and "PREFLIGHT.md" in Q.QueueStore(repo).load().reason
    assert drain(repo, runs)[0] == 0


def test_a_resume_that_fails_preflight_stays_halted(repo):
    add(repo, "ENG-1")
    runs = Runs(repo, ENG_1=["halted", "preflight"])
    drain(repo, runs)
    assert drain(repo, runs)[0] == 2
    assert statuses(repo)["ENG-1"] == "halted" and "resume did not start" in Q.QueueStore(repo).load().reason


# ---------------------------------------------------------------- processes


def hold_run_lock(repo):
    RunPaths(repo).ensure()
    RunLock(RunPaths(repo).lock).acquire("other")


def test_it_waits_for_a_live_run_before_starting(repo):
    add(repo, "ENG-1")
    hold_run_lock(repo)
    runs, waits = Runs(repo), []

    def sleep(_s):
        waits.append(len(runs.calls))
        RunPaths(repo).lock.unlink()

    r = Q.QueueRunner(repo, launcher=runs, sleep=sleep, echo=lambda m: None)
    assert r.run() == 0 and waits == [0] and runs.started() == ["ENG-1"]


def test_a_restarted_queue_follows_the_run_its_predecessor_launched(repo):
    add(repo, "ENG-1", "ENG-2")
    with Q.QueueStore(repo).edit() as q:
        q.items[0].status = "running"
    hold_run_lock(repo)
    runs = Runs(repo)

    def sleep(_s):  # the surviving run completes
        StateStore(RunPaths(repo).state).save(RunState("r1", "ENG-1", "d", str(repo), status="completed"))
        RunPaths(repo).lock.unlink()

    r = Q.QueueRunner(repo, launcher=runs, sleep=sleep, echo=lambda m: None)
    assert r.run() == 0 and runs.started() == ["ENG-2"]
    assert statuses(repo) == {"ENG-1": "done", "ENG-2": "done"}


def test_a_run_still_in_preflight_is_followed_not_launched_twice(repo, monkeypatch):
    add(repo, "ENG-1")
    with Q.QueueStore(repo).edit() as q:
        q.items[0].status, q.items[0].pid = "running", 4242  # no run lock yet
    alive = [True, False, False, False]
    monkeypatch.setattr(Q, "run_process_alive", lambda pid: alive.pop(0) if pid == 4242 and alive else False)
    runs = Runs(repo)

    def sleep(_s):
        StateStore(RunPaths(repo).state).save(RunState("r1", "ENG-1", "d", str(repo), status="completed"))

    r = Q.QueueRunner(repo, launcher=runs, sleep=sleep, echo=lambda m: None)
    assert r.run() == 0 and not runs.calls and statuses(repo) == {"ENG-1": "done"}


def test_run_process_alive_ignores_unrelated_processes():
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "-m", "speckit_autopilot"])
    try:
        assert Q.run_process_alive(ours.pid) and not Q.run_process_alive(other.pid) and not Q.run_process_alive(None)
    finally:
        for p in (ours, other):
            p.kill()
            p.wait()
    assert not Q.run_process_alive(ours.pid)


def test_run_times_are_compared_as_times_not_text(repo):
    """The hour the clocks go back: 02:30+01:00 is later than 02:45+02:00."""
    paths = RunPaths(repo)
    paths.ensure()
    StateStore(paths.state).save(RunState("r", "ENG-1", "d", str(repo), status="completed", started_at="2026-10-25T02:30:00+01:00"))
    assert Q.past_run(paths, "ENG-1", "2026-10-25T02:45:00+02:00")
    assert not Q.past_run(paths, "ENG-1", "2026-10-25T02:31:00+01:00")


def test_a_signal_leaves_the_run_alone_and_the_item_running(repo):
    add(repo, "ENG-1")

    class Endless(FakeProc):
        def poll(self):
            return None

    r = Q.QueueRunner(repo, launcher=lambda a, p: Endless(0), echo=lambda m: None)

    def interrupt(_s):
        r.interrupted = True

    r.sleep = interrupt
    assert r.run() == 0
    assert statuses(repo)["ENG-1"] == "running" and not r.store.lock.holder_alive()
    assert r.store.load().items[0].pid == 999_999  # a restart follows this process even before it takes the run lock


def test_runs_get_their_own_session(repo, monkeypatch):
    seen = {}
    monkeypatch.setattr(Q.subprocess, "Popen", lambda argv, **kw: seen.update(kw, argv=argv))
    RunPaths(repo).ensure()
    Q.default_launcher(["--repo", str(repo), "--issue", "ENG-1"], RunPaths(repo))
    assert seen["start_new_session"] is True and seen["argv"][1:] == ["-m", "speckit_autopilot", "--repo", str(repo), "--issue", "ENG-1"]


def test_only_one_queue_per_checkout(repo):
    add(repo, "ENG-1")
    store = Q.QueueStore(repo)
    store.lock.acquire("queue")
    with pytest.raises(Q.QueueError, match="already running"):
        Q.start(store, on_halt=None, foreground=True)


def test_a_spec_run_by_hand_while_queued_is_not_run_again(repo):
    add(repo, "ENG-1")
    time.sleep(1.1)
    StateStore(RunPaths(repo).state).save(RunState("hand", "ENG-1", "d", str(repo), status="completed"))
    runs = Runs(repo)
    assert drain(repo, runs)[0] == 0 and not runs.calls and statuses(repo)["ENG-1"] == "done"


def test_background_start_runs_the_queue_in_its_own_process(repo, capsys):
    """The real detached path, up to the point where a run would start (a dirty checkout blocks it)."""
    add(repo, "ENG-1")
    (repo / "stray.txt").write_text("x")
    assert Q.main(["start", "--repo", str(repo)]) == 0
    out = json.loads(capsys.readouterr().out)
    for _ in range(100):
        if Q.QueueStore(repo).load().status == "blocked":
            break
        time.sleep(0.1)
    assert Q.QueueStore(repo).load().status == "blocked" and "stray.txt" in Path(out["log"]).read_text()


# ---------------------------------------------------------------- add / remove / CLI


@pytest.mark.parametrize("keys,kw,msg", [
    ([], {}, "at least one"),
    (["ENG-1", "ENG-1"], {}, "named twice"),
    (["../x"], {}, "not an issue key"),
    (["--x"], {}, "not an issue key"),
    (["ENG-1"], {"after": ["ENG-0"]}, "not in the queue"),
    (["ENG-1"], {"after": ["ENG-1"]}, "not in the queue"),
    (["ENG-1", "ENG-2"], {"description": "d"}, "describe one spec"),
    (["ENG-1"], {"no_linear": True}, "needs --description"),
    (["feature-x"], {}, "needs --description"),
])
def test_add_validates_before_queueing(repo, keys, kw, msg):
    with pytest.raises(Q.QueueError, match=msg):
        add(repo, *keys, **kw)
    assert statuses(repo) == {}


def test_add_without_linear_is_labelled_by_its_description(repo):
    add(repo, description="Add product filters")
    runs = Runs(repo)
    assert drain(repo, runs)[0] == 0
    assert runs.calls == [["--repo", str(repo), "--no-linear", "--issue", "product-filters", "--", "Add product filters"]]


def test_a_spec_key_repo_needs_a_spec(repo):
    (repo / ".autopilot.toml").write_text('linear = "on"\nlinear_workspace_url = "https://linear.app/acme"\nspec_id_style = "key"\n')
    with pytest.raises(Q.QueueError, match="needs a spec id"):
        add(repo, "ENG-1")
    add(repo, "ENG-1=spec-ABC")


def test_add_refuses_a_spec_that_already_ran_or_has_a_run(repo):
    RunPaths(repo).ensure()
    StateStore(RunPaths(repo).state).save(RunState("r0", "ENG-1", "d", str(repo), status="completed"))
    with pytest.raises(Q.QueueError, match="already completed"):
        add(repo, "ENG-1")
    add(repo, "ENG-1", again=True)
    StateStore(RunPaths(repo).state).save(RunState("r1", "ENG-2", "d", str(repo), status="halted"))
    with pytest.raises(Q.QueueError, match="already has a run"):
        add(repo, "ENG-2")


def test_again_really_runs_again(repo):
    add(repo, "ENG-1")
    runs = Runs(repo)
    drain(repo, runs)
    time.sleep(1.1)  # run timestamps have a resolution of one second
    add(repo, "ENG-1", again=True)
    assert drain(repo, runs)[0] == 0 and runs.started() == ["ENG-1", "ENG-1"]


def test_a_requeued_spec_that_fails_preflight_is_not_done_because_of_its_old_run(repo):
    add(repo, "ENG-1")
    runs = Runs(repo, ENG_1=["completed", "preflight"])
    drain(repo, runs)
    time.sleep(1.1)
    add(repo, "ENG-1", again=True)
    assert drain(repo, runs)[0] == 2
    assert [i.status for i in Q.QueueStore(repo).load().items] == ["done", "queued"]


def test_a_halted_run_that_is_not_the_current_one_is_never_resumed(repo):
    add(repo, "ENG-1")
    runs = Runs(repo, ENG_1=["halted"])
    drain(repo, runs)
    paths = RunPaths(repo)
    d = paths.root / "archive" / "run-1"
    d.mkdir(parents=True)
    paths.state.rename(d / "state.json")
    StateStore(paths.state).save(RunState("other", "ENG-8", "d", str(repo), status="halted"))
    assert drain(repo, runs)[0] == 2  # --resume would resume ENG-8 instead
    assert runs.started() == ["ENG-1"] and "was archived" in Q.QueueStore(repo).load().reason


def test_remove_rules(repo):
    store = Q.QueueStore(repo)
    add(repo, "ENG-1", "ENG-2")
    add(repo, "ENG-3", after=["ENG-1"])
    with pytest.raises(Q.QueueError, match="wait"):
        Q.remove_item(store, "ENG-1")
    with store.edit() as q:
        q.get("ENG-2").status = "running"
    with pytest.raises(Q.QueueError, match="running"):
        Q.remove_item(store, "ENG-2")
    Q.remove_item(store, "ENG-3")
    Q.remove_item(store, "ENG-1")
    assert statuses(repo) == {"ENG-2": "running"}


def test_cli_round_trip(repo, capsys):
    assert cli.main(["queue", "add", "ENG-1", "ENG-2=031", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "added ENG-1, ENG-2" in out and "2. · ENG-2 (spec 031) queued" in out
    assert cli.main(["queue", "add", "ENG-1", "--repo", str(repo)]) == 1
    assert "already in the queue" in capsys.readouterr().err
    assert cli.main(["queue", "pause", "--repo", str(repo)]) == 1
    capsys.readouterr()
    assert cli.main(["queue", "status", "--json", "--repo", str(repo)]) == 0
    d = json.loads(capsys.readouterr().out)
    assert [i["key"] for i in d["items"]] == ["ENG-1", "ENG-2"] and d["alive"] is False
    assert cli.main(["queue", "clear", "--repo", str(repo)]) == 0 and statuses(repo) == {}
    assert git(repo, "status", "--porcelain") == ""  # queue files are never seen by git


def test_a_broken_queue_file_is_reported_not_overwritten(repo, capsys):
    add(repo, "ENG-1")
    f = Q.QueueStore(repo).file
    f.write_text("{oops")
    assert cli.main(["queue", "add", "ENG-2", "--repo", str(repo)]) == 1
    assert "not readable" in capsys.readouterr().err and f.read_text() == "{oops"


def test_notify_command_hears_about_halts(repo, tmp_path):
    out = tmp_path / "msgs"
    (repo / ".autopilot.toml").write_text(f'linear = "on"\nlinear_workspace_url = "https://linear.app/acme"\nnotify_command = "echo \\"$AUTOPILOT_MESSAGE\\" >> {out}"\n')
    git(repo, "commit", "-q", "-am", "cfg")
    git(repo, "push", "-q")
    add(repo, "ENG-1", "ENG-2")
    drain(repo, Runs(repo, ENG_2=["halted:needs a person"]))
    text = out.read_text()
    assert "ENG-1 done" in text and "ENG-2 halted: needs a person" in text


# ---------------------------------------------------------------- a run launched with the key alone


def test_the_linear_title_becomes_the_description(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, description="")
    out = run_pipeline(runner)
    assert out.status == "completed", out.reason
    assert world_state(w).description == w.linear.issue["title"]


def test_cli_accepts_a_key_alone_only_with_linear(repo, monkeypatch):
    monkeypatch.setattr(cli, "detach", lambda argv, paths: 77)
    assert cli.main(["--repo", str(repo), "--issue", "ENG-1", "--detach"]) == 77
    with pytest.raises(SystemExit, match="description is required"):
        cli.main(["--repo", str(repo), "--no-linear", "--issue", "ENG-1", "--detach"])
