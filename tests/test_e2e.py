"""Scripted end-to-end tests (spec §12): the real Runner state machine, real gates, real policy,
real git (temp repo + bare origin), with FakeSession workers, a FakeJudge, a fake Linear and a
fake gh. No API calls, no network.

Scenarios 1-10 follow SPEC.md §12 in order; the extra tests cover --until, the run lock, the
total budget and the per-turn timeout.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from speckit_autopilot.config import PHASES
from speckit_autopilot.gates import status_block, task_progress
from speckit_autopilot.prompts import ANALYZE_TRAILER_REQUEST, IMPLEMENT_ARGS, IMPLEMENT_CONTINUE_ARGS
from speckit_autopilot.report import resume_command
from speckit_autopilot.state import EventLog, LockHeld, RunPaths, RunState, StateStore

from .fakeworld import TODAY, JudgeScript, SimulatedCrash, World, make_runner

# Known bug outside runner.py (reported, not fixed here): speckit_autopilot/gates.py ship_facts() sets
# merged=True whenever the feature tip is an ancestor of origin/<base>. Before cc-ship commits, the
# feature branch has no commits of its own (its tip IS the base commit), so it is "merged" before
# anything shipped. Remove these xfail markers once ship_facts is fixed.
SHIP_FACTS_BUG = (
    "gates.ship_facts treats a feature branch with no commits of its own as merged (tip is trivially an ancestor "
    "of origin/<base>), so the ship gate skips the full tooling run and a ship resume is told 'already merged'"
)

# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def run(runner) -> Any:
    return asyncio.run(runner.run())


def paths(w: World) -> RunPaths:
    return RunPaths(w.repo)


def state(w: World) -> RunState:
    return StateStore(paths(w).state).load()


def events(w: World, name: str | None = None, **match: Any) -> list[dict[str, Any]]:
    out = EventLog(paths(w).events).read()
    if name is not None:
        out = [e for e in out if e.get("event") == name]
    return [e for e in out if all(e.get(k) == v for k, v in match.items())]


def decisions(w: World, kind: str | None = None) -> list[dict[str, Any]]:
    p = paths(w).decisions_jsonl
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
    return [r for r in rows if kind is None or r.get("kind") == kind]


def report(w: World) -> str:
    return paths(w).report.read_text()


def sessions(w: World, phase: str) -> list:
    return [s for s in w.sessions if s.spec.phase == phase]


def pipeline_inbox(sess) -> list[str]:
    """Messages the pipeline sent to a worker session, without the handoff brief."""
    return [m for m in sess.inbox if not m.startswith("AUTOPILOT HANDOFF BRIEF")]


def label_of(w: World, prefix: str, nth: int = 0) -> str:
    return [lbl for lbl, m in w.messages if m.startswith(prefix)][nth]


def phase_starts(w: World) -> list[str]:
    return [e["phase"] for e in events(w, "phase_start")]


def linear_writes(w: World) -> Counter:
    return Counter(name.rsplit("__", 1)[-1] for name, _ in w.linear.writes)


def closeout_comments(w: World) -> list[dict[str, Any]]:
    return [c for c in w.linear.comments if "Autopilot close-out" in c["body"]]


def remote_branch_sha(w: World, branch: str) -> str:
    out = w.git("ls-remote", "origin", f"refs/heads/{branch}")
    return out.split()[0] if out else ""


def dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def assert_base_clean(w: World) -> None:
    assert w.git("rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert w.git("status", "--porcelain") == ""
    assert w.git("rev-parse", "main") == w.git("rev-parse", "origin/main")


def assert_nothing_shipped(w: World, st: RunState) -> None:
    assert (w.pushes, w.pr_creates, w.merges) == (0, 0, 0)
    assert remote_branch_sha(w, st.feature_branch) == ""
    assert w.linear.issue["statusType"] == "started"
    assert w.linear.writes == []


# --------------------------------------------------------------------------------------
# 1. happy path through all 9 phases
# --------------------------------------------------------------------------------------


def test_01_happy_path_all_nine_phases(tmp_path):
    w = World(tmp_path)
    runner, judge, js, tr = make_runner(w)
    out = run(runner)

    assert out.status == "completed", out.reason
    st = state(w)
    assert st.status == "completed"
    assert {p: st.phase(p).status for p in PHASES} == {p: "passed" for p in PHASES}
    assert all(st.phase(p).attempts == 1 for p in PHASES)
    assert phase_starts(w) == list(PHASES)
    assert st.resets == [] and st.back_edges == [] and st.escalations == []
    assert events(w, "denial") == []

    # exactly one push, one PR, one merge; the feature branch is merged into origin/main
    assert (w.pushes, w.pr_creates, w.merges) == (1, 1, 1)
    feature_tip = remote_branch_sha(w, st.feature_branch)
    assert feature_tip
    subprocess.run(["git", "merge-base", "--is-ancestor", feature_tip, "origin/main"], cwd=w.repo, check=True)
    assert st.ship["merged"] is True and st.ship["pr_number"] == 1 and st.ship["merge_commit"] == w.merge_sha

    # base branch clean and in sync at the end
    assert_base_clean(w)

    # decisions.md: in the feature dir, frozen before ship and committed on main
    fd = Path(st.feature_dir)
    rel = fd.relative_to(w.repo).as_posix()
    dec = fd / "decisions.md"
    assert dec.is_file()
    assert st.decisions_frozen
    assert w.git("show", f"main:{rel}/decisions.md") == dec.read_text().strip()
    assert dec.read_text().startswith("# Autopilot decisions — ENG-142")
    assert "## clarify" in dec.read_text()

    # Linear: completed, one close-out comment, status block dated today, rest of the overview untouched
    assert w.linear.issue["statusType"] == "completed"
    ours = closeout_comments(w)
    assert len(ours) == 1 and "ENG-142" in ours[0]["body"] and st.run_id in ours[0]["body"]
    block, style = status_block(w.linear.project["description"])
    assert block is not None and style == "html" and TODAY in block
    assert w.linear.project["description"].startswith("# Demo project\n\nIntro text that must never change.\n")
    assert linear_writes(w) == Counter(save_issue=1, save_comment=1, save_project=1)
    assert not any("Autopilot halted" in c["body"] for c in w.linear.comments)

    # reports + git hygiene + lock
    assert report(w).startswith("# Autopilot COMPLETED — ENG-142")
    assert ".autopilot/" in (w.repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert not paths(w).lock.exists()

    # session groups (spec §4): S1 is one shared session; every other group gets its own; all closed
    s1 = sessions(w, "feature_start")
    assert len(s1) == 1
    assert [m.split()[0] for m in pipeline_inbox(s1[0]) if m.startswith("/")] == [
        "/cc-feature-start", "/speckit-specify", "/speckit-clarify", "/speckit-plan", "/speckit-tasks"]
    for ph in ("analyze", "implement", "review", "ship", "linear"):
        assert len(sessions(w, ph)) == 1, ph
    assert all(s.closed for s in w.sessions)
    assert all(s.inbox[0].startswith("AUTOPILOT HANDOFF BRIEF") for s in w.sessions if s.spec.phase in PHASES)


# --------------------------------------------------------------------------------------
# 2. a clarify question answered with a NON-recommended option
# --------------------------------------------------------------------------------------


def test_02_clarify_non_recommended_option(tmp_path):
    w = World(tmp_path)
    js = JudgeScript(w)
    js.clarify_choice = "B"
    js.clarify_follow_recommendation = False
    runner, *_ = make_runner(w, js)
    out = run(runner)
    assert out.status == "completed", out.reason

    # the worker got exactly "B", in the same session that asked the question
    assert w.clarify_answers == ["B"]
    assert (label_of(w, "/speckit-clarify"), "B") in w.messages
    st = state(w)
    spec_md = (Path(st.feature_dir) / "spec.md").read_text()
    assert f"### Session {TODAY}" in spec_md and "→ A: B" in spec_md

    # decisions.jsonl records the rejected recommendation with its rationale
    clar = [d for d in decisions(w, "decision") if d["phase"] == "clarify"]
    assert len(clar) == 1
    d = clar[0]
    assert d["followed_recommendation"] is False
    assert d["recommended"] == "Option A" and d["chosen"] == "Option B"
    assert d["rationale"] == "B is better"
    assert d["mode"] == "respond" and d["post_ship"] is False

    # ... and decisions.md (committed on main) renders it
    rel = Path(st.feature_dir).relative_to(w.repo).as_posix()
    md = w.git("show", f"main:{rel}/decisions.md")
    assert "rejected recommendation: 1" in md
    assert "followed: **no**" in md
    assert "**Rationale:** B is better" in md
    assert "rejected recommendation: 1" in report(w)


# --------------------------------------------------------------------------------------
# 3. analyze CRITICAL -> approved edits applied in the same session -> fresh re-verify passes
# --------------------------------------------------------------------------------------


def test_03_analyze_critical_fix_then_fresh_reverify(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("C1", "CRITICAL"), ("M1", "MEDIUM")], [("M1", "MEDIUM")]]
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status == "completed", out.reason

    an = sessions(w, "analyze")
    assert len(an) == 2, "analyze must re-run in a brand-new session after fixes"
    first, second = an
    assert first.label != second.label
    approval = "Approved, apply these edits now:\n1. fix C1"
    # the session that ran analyze (and gave the ANALYZE_SUMMARY trailer) is the one that applied the approved edits ...
    assert pipeline_inbox(first) == ["/speckit-analyze", ANALYZE_TRAILER_REQUEST, approval]
    assert first.closed
    # ... and it never verifies its own fix: a fresh session re-runs analyze
    assert pipeline_inbox(second) == ["/speckit-analyze", ANALYZE_TRAILER_REQUEST]
    assert "fresh analyze run (round 1)" in second.inbox[0]
    assert w.edits_applied == [approval]
    assert w.analyze_calls == 2

    st = state(w)
    assert "<!-- edit 1 -->" in (Path(st.feature_dir) / "tasks.md").read_text()
    rec = st.phase("analyze")
    assert (rec.status, rec.attempts, rec.repairs) == ("passed", 1, 1)
    gates = events(w, "gate", phase="analyze")
    assert [g["passed"] for g in gates] == [False, True]
    assert any(f.startswith("[zero-critical] 1 CRITICAL") for f in gates[0]["failed"])
    assert len(events(w, "repair", phase="analyze")) == 1
    # the CRITICAL one was fixed, the MEDIUM one consciously accepted
    items = {d["item"]: d["chosen"] for d in decisions(w, "decision") if d["phase"] == "analyze"}
    assert items == {"finding C1": "fix", "finding M1": "accept"}


# --------------------------------------------------------------------------------------
# 4. analyze routes back to plan; downstream phases re-run; max 1 back-edge per target
# --------------------------------------------------------------------------------------


def test_04_analyze_routes_back_to_plan_and_downstream_reruns(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("H1", "HIGH")]] * 3 + [[]]  # fails initial + 2 repairs, passes after the back-edge
    js = JudgeScript(w)
    js.route_action = {"analyze": {"action": "back", "target": "plan", "reason": "architecture gap in plan.md"}}
    runner, *_ = make_runner(w, js)
    out = run(runner)
    assert out.status == "completed", out.reason

    st = state(w)
    assert phase_starts(w) == ["feature_start", "clarify", "plan", "tasks", "analyze", "plan", "tasks", "analyze", "implement", "review", "ship", "linear"]
    attempts = {p: st.phase(p).attempts for p in PHASES}
    assert attempts == {"feature_start": 1, "clarify": 1, "plan": 2, "tasks": 2, "analyze": 2, "implement": 1, "review": 1, "ship": 1, "linear": 1}
    assert w.analyze_calls == 4
    assert len(st.back_edges) == 1
    assert (st.back_edges[0]["from"], st.back_edges[0]["to"]) == ("analyze", "plan")

    # the re-run plan got a fresh session whose brief says attempt 2
    plan2 = [s for s in w.sessions if s.spec.phase == "plan"]
    assert len(plan2) == 1 and "Current phase: plan (attempt 2)" in plan2[0].inbox[0]
    assert plan2[0].label.startswith("plan-a2-")

    # every routing decision is logged (decisions.md + REPORT.md)
    # round 1 asked the early root-cause question first (the judge said fix here), then repairs ran out
    routes = decisions(w, "routing")
    assert [(r["phase"], r["action"], r["target"], r["applied"], r.get("early", False)) for r in routes] == [
        ("analyze", "fix_here", None, True, True),
        ("analyze", "back", "plan", True, False),
    ]
    rel = Path(st.feature_dir).relative_to(w.repo).as_posix()
    assert "**Routing:** back → plan: architecture gap in plan.md" in w.git("show", f"main:{rel}/decisions.md")
    assert "- analyze → plan: architecture gap in plan.md" in report(w)
    assert (w.pushes, w.merges) == (1, 1)


def test_04b_second_back_edge_to_same_target_halts(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("H1", "HIGH")]]  # analyze never passes
    js = JudgeScript(w)
    js.route_action = {"analyze": {"action": "back", "target": "plan", "reason": "plan gap"}}
    runner, *_ = make_runner(w, js)
    out = run(runner)

    assert out.status == "halted"
    st = state(w)
    assert len(st.back_edges) == 1  # the second request was refused
    assert "already routed back to plan 1 time(s) (limit 1)" in st.halt["reason"]
    assert st.halt["phase"] == "analyze"
    assert [st.phase(p).attempts for p in ("plan", "tasks", "analyze")] == [2, 2, 2]
    routes = decisions(w, "routing")
    late = [(r["action"], r["target"], r["applied"]) for r in routes if not r.get("early")]
    assert late == [("back", "plan", True), ("back", "plan", False)]
    # each analyze entry asked the early root-cause question once, before its first fix
    assert [(r["action"], r.get("early")) for r in routes if r.get("early")] == [("fix_here", True), ("fix_here", True)]
    assert "implement" not in phase_starts(w)
    assert_nothing_shipped(w, st)


# --------------------------------------------------------------------------------------
# 5. implement with a context reset in the middle
# --------------------------------------------------------------------------------------


def test_05_implement_context_reset_continues_on_unchecked_tasks(tmp_path):
    w = World(tmp_path)
    w.implement_ctx_high_after = 2  # the 2nd implement turn reports context >= 60%
    progress_at_continue: list[tuple[int, int]] = []
    script = w.script

    def spy(s, msg):
        if IMPLEMENT_CONTINUE_ARGS in msg:
            progress_at_continue.append(task_progress(w.feature_dir(s)))
        return script(s, msg)

    w.script = spy  # type: ignore[method-assign]
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status == "completed", out.reason

    st = state(w)
    assert len(st.resets) == 1
    assert st.resets[0]["phase"] == "implement" and st.resets[0]["reason"].startswith("context 95%")
    assert events(w, "reset", phase="implement")

    impl = sessions(w, "implement")
    assert len(impl) == 2
    first, second = impl
    assert first.label != second.label, "every session needs its own label (transcript file)"
    assert pipeline_inbox(first) == [f"/speckit-implement {IMPLEMENT_ARGS}", "continue"]
    assert first.closed
    # the new session starts from the brief + the continue message, never the old conversation
    assert "continue implement (unchecked tasks only)" in second.inbox[0]
    assert pipeline_inbox(second) == [f"/speckit-implement {IMPLEMENT_CONTINUE_ARGS}"]
    # ... and only the unchecked task (T005) was left when it started
    assert progress_at_continue == [(4, 5)]

    assert task_progress(Path(st.feature_dir)) == (5, 5)
    assert "- [ ]" not in (Path(st.feature_dir) / "tasks.md").read_text()
    assert "[implement] context 95%" in report(w)


# --------------------------------------------------------------------------------------
# 6. tests fail -> repair -> pass
# --------------------------------------------------------------------------------------


def test_06_tests_fail_repair_then_pass(tmp_path):
    w = World(tmp_path)
    runner, judge, js, tr = make_runner(w, tool_results={"pytest -q": [1, 1, 0]})  # fail, flaky re-run fails, then green
    out = run(runner)
    assert out.status == "completed", out.reason

    gates = events(w, "gate", phase="implement")
    assert [g["passed"] for g in gates] == [False, True]
    failed = gates[0]["failed"]
    assert failed == ["[test-passes] `pytest -q` exit 1 in 0s (re-run once to rule out flakiness)\nFAILED test_x"]
    first_tooling = events(w, "tooling_end")[0]
    assert first_tooling["ok"] is False and first_tooling["retried"] == ["test"]
    assert tr.calls[:4] == ["ruff check .", "pytest -q", "pytest -q", "ruff check ."]

    reps = events(w, "repair", phase="implement")
    assert len(reps) == 1 and reps[0]["failed"] == failed
    seen = [m for ph, m in w.repairs_seen if ph == "implement"]
    assert len(seen) == 1
    for check in failed:
        assert check in seen[0], "the failed check text must be sent verbatim"
    # context was low, so the repair went to the same implement session
    impl = sessions(w, "implement")
    assert len(impl) == 1 and any(m.startswith("AUTOPILOT REPAIR (phase implement, round 1/2)") for m in impl[0].inbox)
    rec = state(w).phase("implement")
    assert (rec.status, rec.attempts, rec.repairs) == ("passed", 1, 1)


# --------------------------------------------------------------------------------------
# 7. too many repairs -> halt with REPORT.md
# --------------------------------------------------------------------------------------


def test_07_too_many_repairs_halts_with_report(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, tool_results={"pytest -q": [1] * 50})
    out = run(runner)

    assert out.status == "halted"
    st = state(w)
    assert st.status == "halted"
    assert st.halt["phase"] == "implement"
    assert st.halt["reason"].startswith("implement failed after 2 repair round(s); judge routed to halt")
    rec = st.phase("implement")
    assert (rec.status, rec.attempts, rec.repairs) == ("failed", 1, 2)
    assert len([1 for ph, _ in w.repairs_seen if ph == "implement"]) == 2
    assert [(r["action"], r["phase"]) for r in decisions(w, "routing")] == [("halt", "implement")]

    rep = report(w)
    assert rep.startswith("# Autopilot HALTED — ENG-142")
    assert "## Failed checks" in rep and "- [test-passes] `pytest -q` exit 1" in rep
    assert "**Phase:** implement · attempt 1 · repairs used 2" in rep
    assert resume_command(st) in rep and "--resume" in rep

    # optional Linear halt comment: posted exactly once, carrying the resume command
    halts = [c for c in w.linear.comments if "Autopilot halted" in c["body"]]
    assert len(halts) == 1
    assert len(events(w, "halt_notice_posted")) == 1
    notice = [m for lbl, m in w.messages if lbl.startswith("halt-notice")]
    assert len(notice) == 1 and "--resume" in notice[0] and "save_comment" in notice[0]
    assert "review" not in phase_starts(w)
    assert_nothing_shipped(w, st)
    assert not paths(w).lock.exists()


# --------------------------------------------------------------------------------------
# 8. an escalation closes the ship gate; --resume --allow-escalations ships
# --------------------------------------------------------------------------------------


def test_08_escalation_closes_ship_gate_until_allowed(tmp_path):
    w = World(tmp_path)
    js = JudgeScript(w)
    js.escalate_in = "plan"
    runner, *_ = make_runner(w, js)
    out = run(runner)

    assert out.status == "halted"
    st = state(w)
    assert st.halt["reason"] == "ship gate closed" and st.halt["phase"] == "ship"
    assert any("1 escalation(s) need a human: legal" in f for f in st.halt["failed"])
    assert [(e["phase"], e["reason"]) for e in st.escalations] == [("plan", "legal")]
    assert {p: st.phase(p).status for p in PHASES[:7]} == {p: "passed" for p in PHASES[:7]}
    assert "ship" not in phase_starts(w)
    assert events(w, "ship_gate", open=False)
    rep = report(w)
    assert "ship gate closed" in rep and "- [plan] legal" in rep
    assert "⚠️ **Escalation:** legal" in (Path(st.feature_dir) / "decisions.md").read_text()
    assert_nothing_shipped(w, st)

    # a plain --resume keeps the gate closed
    runner, *_ = make_runner(w, resume=True)
    assert run(runner).status == "halted"
    assert state(w).halt["reason"] == "ship gate closed"
    assert_nothing_shipped(w, st)

    # --resume --allow-escalations opens it and ships
    runner, *_ = make_runner(w, resume=True, allow_escalations=True)
    out = run(runner)
    assert out.status == "completed", out.reason
    st = state(w)
    assert st.flags["allow_escalations"] is True
    assert len(st.escalations) == 1
    assert (w.pushes, w.pr_creates, w.merges) == (1, 1, 1)
    assert w.linear.issue["statusType"] == "completed" and len(closeout_comments(w)) == 1
    closeout = [m for _, m in w.messages if m.startswith("AUTOPILOT LINEAR CLOSE-OUT")]
    assert len(closeout) == 1 and "ESCALATED [plan]: legal" in closeout[0]
    rel = Path(st.feature_dir).relative_to(w.repo).as_posix()
    assert "⚠️ **Escalation:** legal" in w.git("show", f"main:{rel}/decisions.md")
    assert_base_clean(w)


# --------------------------------------------------------------------------------------
# 9. a crash during ship, then --resume: no double push / PR / merge / Linear close-out
# --------------------------------------------------------------------------------------


def test_09_crash_during_ship_then_resume_is_idempotent(tmp_path):
    w = World(tmp_path)
    w.ship_crash_after_push = True
    runner, *_ = make_runner(w)
    with pytest.raises(SimulatedCrash, match="simulated crash after push"):
        run(runner)

    st = state(w)
    run_id = st.run_id
    assert st.status == "running" and st.phase("ship").status == "running" and st.ship.get("started")
    assert (w.pushes, w.pr_creates, w.merges) == (1, 1, 0)
    pushed_tip = remote_branch_sha(w, st.feature_branch)
    assert pushed_tip
    # a hard crash leaves its lock behind (dead pid)
    paths(w).lock.write_text(json.dumps({"pid": dead_pid(), "started_at": "x", "run_id": run_id, "host": socket.gethostname()}))

    runner, *_ = make_runner(w, resume=True)  # a NEW Runner over the same repo
    out = run(runner)
    assert out.status == "completed", out.reason
    st = state(w)
    assert st.run_id == run_id
    assert events(w, "stale_lock_replaced")
    assert (st.phase("ship").status, st.phase("ship").attempts) == ("passed", 2)

    # no second push / PR / merge
    assert (w.pushes, w.pr_creates, w.merges) == (1, 1, 1)
    assert remote_branch_sha(w, st.feature_branch) == pushed_tip
    ship_msgs = [m for _, m in w.messages if m.startswith(("/cc-ship", "AUTOPILOT SHIP RESUME"))]
    assert len(ship_msgs) == 2
    assert ship_msgs[0].startswith("/cc-ship")
    assert ship_msgs[1].startswith("AUTOPILOT SHIP RESUME") and "Do NOT commit again and do NOT open another PR" in ship_msgs[1]
    assert len(events(w, "ship_resume")) == 1
    assert w.git("log", "--format=%s", "origin/main").splitlines().count("feat: organic filters") == 1

    # Linear closed exactly once
    assert linear_writes(w) == Counter(save_issue=1, save_comment=1, save_project=1)
    assert len(closeout_comments(w)) == 1
    assert_base_clean(w)

    # resuming a completed run touches nothing
    n_sessions = len(w.sessions)
    runner, *_ = make_runner(w, resume=True)
    assert run(runner).status == "completed"
    assert len(w.sessions) == n_sessions
    assert (w.pushes, w.pr_creates, w.merges) == (1, 1, 1)
    assert linear_writes(w) == Counter(save_issue=1, save_comment=1, save_project=1)


def test_09c_crash_during_ship_before_push_then_resume_ships_once(tmp_path):
    """Today the resume is told 'the feature is already merged — only switch to main and pull', which
    carries the uncommitted feature work onto main; the ship gate then fails on a dirty base and halts."""
    w = World(tmp_path)
    w.ship_crash_before_push = True
    runner, *_ = make_runner(w)
    with pytest.raises(SimulatedCrash, match="simulated crash before push"):
        run(runner)
    st = state(w)
    assert st.ship.get("started") and st.phase("ship").status == "running"
    assert (w.pushes, w.pr_creates, w.merges) == (0, 0, 0)
    assert remote_branch_sha(w, st.feature_branch) == ""

    runner, *_ = make_runner(w, resume=True)
    out = run(runner)
    assert out.status == "completed", out.reason
    # nothing was published before the crash, so cc-ship simply runs again: one push, one PR, one merge
    assert (w.pushes, w.pr_creates, w.merges) == (1, 1, 1)
    ship_msgs = [m for _, m in w.messages if m.startswith(("/cc-ship", "AUTOPILOT SHIP RESUME"))]
    assert [m.split()[0] for m in ship_msgs] == ["/cc-ship", "/cc-ship"]
    assert linear_writes(w) == Counter(save_issue=1, save_comment=1, save_project=1)
    assert_base_clean(w)


def test_09b_crash_after_linear_writes_then_resume_does_not_close_twice(tmp_path):
    w = World(tmp_path)
    w.linear_crash_after_write = True
    runner, *_ = make_runner(w)
    with pytest.raises(SimulatedCrash, match="after the Linear close-out writes"):
        run(runner)
    assert linear_writes(w) == Counter(save_issue=1, save_comment=1, save_project=1)
    assert state(w).phase("linear").status == "running"

    runner, *_ = make_runner(w, resume=True)
    out = run(runner)
    assert out.status == "completed", out.reason
    st = state(w)
    assert st.phase("linear").status == "already_satisfied"
    assert st.phase("ship").attempts == 1  # ship is not re-entered
    assert linear_writes(w) == Counter(save_issue=1, save_comment=1, save_project=1)
    assert len(closeout_comments(w)) == 1
    assert len([m for _, m in w.messages if m.startswith("AUTOPILOT LINEAR CLOSE-OUT")]) == 1
    assert (w.pushes, w.pr_creates, w.merges) == (1, 1, 1)


# --------------------------------------------------------------------------------------
# 10. a denied git push outside ship
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["implement", "review"])
def test_10_git_push_outside_ship_is_denied_and_logged(tmp_path, phase):
    w = World(tmp_path)
    w.ship_attempt_push_in = phase
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status == "completed", out.reason
    assert w.push_attempted

    den = events(w, "denial")
    assert len(den) == 1
    d = den[0]
    assert (d["phase"], d["tool"], d["command"], d["rule"]) == (phase, "Bash", "git push origin HEAD", "push-phase")
    assert f"current phase: {phase}" in d["reason"]
    denied = [x for s in sessions(w, phase) for x in s.denied]
    assert len(denied) == 1 and denied[0][2].startswith("AUTOPILOT POLICY (push-phase)")
    # the only push of the run is cc-ship's
    assert (w.pushes, w.merges) == (1, 1)
    assert not any(("git push" in json.dumps(inp)) for s in sessions(w, phase) for name, inp in s.allowed)


# --------------------------------------------------------------------------------------
# extras: --until, lock, budget, turn timeout
# --------------------------------------------------------------------------------------


def test_until_ship_pauses_before_shipping_then_resume_ships(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, until="ship")
    out = run(runner)

    assert out.status == "paused"
    st = state(w)
    assert st.status == "paused"
    assert st.phase("review").status == "passed"
    assert "ship" not in phase_starts(w) and st.phase("ship").status == "pending"
    assert report(w).startswith("# Autopilot PAUSED (--until ship: stopped before ship)")
    assert_nothing_shipped(w, st)
    assert not paths(w).lock.exists()

    # the operator reviews the branch, then resumes without --until: it ships
    runner, *_ = make_runner(w, resume=True)
    out = run(runner)
    assert out.status == "completed", out.reason
    assert (w.pushes, w.pr_creates, w.merges) == (1, 1, 1)
    assert phase_starts(w)[-2:] == ["ship", "linear"]


def test_ship_gate_runs_full_suite_before_shipping(tmp_path):
    """Spec §8: the ship gate is open only if the full test/lint/typecheck/build suite passes *now*."""
    w = World(tmp_path)
    runner, judge, js, tr = make_runner(w, tool_results={"pytest -q": [0, 0, 1, 1]})  # green at implement + review, red at the ship gate
    out = run(runner)
    st = state(w)
    assert tr.calls.count("pytest -q") == 4
    assert out.status == "halted" and st.halt["reason"] == "ship gate closed"
    assert any(f.startswith("[test-passes] `pytest -q` exit 1") for f in st.halt["failed"])
    assert_nothing_shipped(w, st)


def test_lock_held_by_live_process_refuses_second_run(tmp_path):
    w = World(tmp_path)
    paths(w).ensure()
    other = subprocess.Popen(["sleep", "60"])
    try:
        held = {"pid": other.pid, "started_at": "2026-11-10T08:00:00", "run_id": "other-run", "host": socket.gethostname()}
        paths(w).lock.write_text(json.dumps(held))
        runner, *_ = make_runner(w)
        with pytest.raises(LockHeld, match=f"pid {other.pid}"):
            run(runner)
        assert json.loads(paths(w).lock.read_text()) == held  # untouched
        assert w.sessions == []
        assert not paths(w).state.exists()
        assert w.git("rev-parse", "--abbrev-ref", "HEAD") == "main"
    finally:
        other.kill()
        other.wait()


def test_budget_exhaustion_halts(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, total_budget_usd=0.03)
    out = run(runner)

    assert out.status == "halted"
    st = state(w)
    assert st.halt["reason"].startswith("total budget exhausted")
    assert st.total_cost_usd >= 0.03
    assert "total budget exhausted" in report(w)
    assert len(phase_starts(w)) < len(PHASES)
    assert_nothing_shipped(w, st)


def test_turn_timeout_interrupts_then_repair_passes(tmp_path):
    w = World(tmp_path)
    w.hang_in = {"plan": 1}
    runner, *_ = make_runner(w, turn_timeout_min=0.002)  # 0.12 s per turn
    out = run(runner)
    assert out.status == "completed", out.reason

    timed_out = [e for e in events(w, "turn", phase="plan") if e["subtype"] == "timeout"]
    assert len(timed_out) == 1 and timed_out[0]["is_error"] is True
    hung = [s for s in w.sessions if s.label == timed_out[0]["session"]]
    assert len(hung) == 1 and hung[0].interrupted and hung[0].closed
    reps = events(w, "repair", phase="plan")
    assert len(reps) == 1
    # the conversation failure is logged, but only gate checks are sent to the worker (no worker text across a reset)
    assert any("worker turn exceeded" in e["reason"] for e in events(w, "conversation_failed", phase="plan"))
    assert not any(f.startswith("[conversation]") for f in reps[0]["failed"])
    assert any(f.startswith("[plan-exists]") for f in reps[0]["failed"])
    # the repair ran in a new session that was told the failed checks
    repair_sess = [s for s in w.sessions if any(m.startswith("AUTOPILOT REPAIR (phase plan") for m in s.inbox)]
    assert len(repair_sess) == 1 and repair_sess[0] is not hung[0]
    assert "The pipeline's exit gate FAILED" in repair_sess[0].inbox[0]
    rec = state(w).phase("plan")
    assert (rec.status, rec.attempts, rec.repairs) == ("passed", 1, 1)


def test_turn_timeout_every_attempt_halts(tmp_path):
    w = World(tmp_path)
    w.hang_in = {"plan": 99}
    runner, *_ = make_runner(w, turn_timeout_min=0.002)
    out = run(runner)

    assert out.status == "halted"
    st = state(w)
    assert st.halt["phase"] == "plan"
    assert any(f.startswith("[conversation] worker turn exceeded") for f in st.halt["failed"])
    assert len([e for e in events(w, "turn", phase="plan") if e["subtype"] == "timeout"]) == 3  # initial + 2 repairs
    assert "[conversation] worker turn exceeded" in report(w)
    assert_nothing_shipped(w, st)


# --------------------------------------------------------------------------------------
# analyze with only LOW/MEDIUM findings left: the judge's fixes are applied once, no fresh re-analyze loop
# --------------------------------------------------------------------------------------


def test_analyze_nonblocking_fixes_are_applied_once_and_the_phase_passes(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("M1", "MEDIUM"), ("L1", "LOW")], [("L2", "LOW")], [("L3", "LOW")], [("L4", "LOW")]]
    runner, _judge, js, _tr = make_runner(w)
    js.analyze_fix_nonblocking = True
    out = run(runner)
    assert out.status == "completed", out.reason
    assert w.analyze_calls == 1  # no fresh analyze chasing new LOW items
    assert w.edits_applied == ["Approved, apply these edits now:\n1. fix M1\n2. fix L1"]
    rec = state(w).phase("analyze")
    assert (rec.status, rec.repairs) == ("passed", 1)
    assert events(w, "analyze_nonblocking_done", phase="analyze")


def test_analyze_recheck_nonblocking_keeps_the_old_loop(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("L1", "LOW")], [("L2", "LOW")], [("L3", "LOW")], [("L4", "LOW")], [("L5", "LOW")]]
    runner, _judge, js, _tr = make_runner(w, analyze_recheck_nonblocking=True)
    js.analyze_fix_nonblocking = True
    run(runner)
    assert w.analyze_calls > 1 and not events(w, "analyze_nonblocking_done")


def test_until_none_lets_a_halted_run_ship(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, until="ship")
    assert run(runner).status == "paused"
    store = StateStore(paths(w).state)
    st = store.load()
    st.status, st.halt = "halted", {"reason": "ship gate closed"}  # halted, not paused: a plain --resume keeps --until
    store.save(st)
    runner, *_ = make_runner(w, resume=True, until="none")
    out = run(runner)
    assert out.status == "completed", out.reason
    assert (w.pushes, w.pr_creates, w.merges) == (1, 1, 1)
