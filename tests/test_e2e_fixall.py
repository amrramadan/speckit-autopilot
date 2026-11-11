"""End-to-end tests (fake world, no API calls) for the fix-all runner work:

1. human-only tasks: snapshot before implement (kept on resume), listed in implement messages, never ticked, a pause
   before ship while any is open, listed in the final report and the Linear close-out;
2. founder-owned checklists (checklists_gate = "halt"): a pause before implement while any item is open;
3. the test baseline (test_baseline = "base"): measured once on the base commit, tolerated test by test;
4. clarify accepts a session dated since the run started (runs that cross midnight);
5. the ANALYZE_SUMMARY trailer turn;
6. early routing (SPEC §7): round 1 of analyze / review asks whether the root cause is upstream.

Pass 2:
7. a stop (SIGTERM / SIGINT / --stop) cancels an in-flight judge call and kills a running tooling command at once;
   tooling runs off the event loop;
8. shared Linear issues: the close-out comments and updates the status block but never changes the issue's status;
9. the OS sandbox per phase, and the unattended contract (env, system-prompt line, ship authorisation);
10. escaped visible status markers, the persisted early-route check, human-only tasks across stops and review fixes.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

import pytest

from speckit_autopilot import gates as G
from speckit_autopilot.config import SHIP_AUTHORIZATION
from speckit_autopilot.judge import FakeJudge, JudgeRequest, JudgeResult
from speckit_autopilot.prompts import ANALYZE_TRAILER_REQUEST, IMPLEMENT_ARGS, UNATTENDED_LINE, judge_prompt
from speckit_autopilot.repo import CmdResult, run_shell
from speckit_autopilot.runner import SharedIssueGate, open_checklist_items

from .fakeworld import TODAY, FakeTurn, JudgeScript, SimulatedCrash, World, make_runner
from .test_e2e import decisions, events, linear_writes, phase_starts, pipeline_inbox, report, run, sessions, state

TASKS_HUMAN = """# Tasks: Organic filters

## Phase 1: Setup
- [ ] T001 FOUNDER GATE: approve the Organic copy with the brand owner
- [ ] T002 Create module src/filters.py
- [ ] T003 [P] Add test file tests/test_filters.py

## Phase 2: User Story 1 - Filter by origin (Priority: P1)
- [ ] T004 [US1] Implement origin filter in src/filters.py
- [ ] T005 [US1] Write tests for the origin filter in tests/test_filters.py

## Phase 3: Polish
- [ ] T006 Update docs/filters.md
- [ ] T007 *(Operator step — not for agents)* rotate the staging API key in the dashboard
"""

# pytest -q output as repo.run_shell captures it (formats verified against gates.parse_test_failures)
OLD = "..F..\nFAILED tests/test_old.py::test_legacy - assert 1 == 2\n1 failed, 4 passed in 0.12s\n"
OLD_NEW = (
    "..FF.\nFAILED tests/test_old.py::test_legacy - assert 1 == 2\nFAILED tests/test_new.py::test_feature - assert 0\n"
    "2 failed, 3 passed in 0.12s\n"
)
COLLECT_ERROR = "ERROR collecting tests/test_x.py\n!!!!!!!! Interrupted: 1 error during collection !!!!!!!!\n1 error in 0.10s\n"


def tasks_file(w: World) -> Path:
    return Path(state(w).feature_dir) / "tasks.md"


def implement_messages(w: World) -> list[str]:
    return [m for s in sessions(w, "implement") for m in pipeline_inbox(s)]


def routes(w: World) -> list[tuple]:
    return [(r["phase"], r["action"], r["target"], r["applied"], r.get("early", False)) for r in decisions(w, "routing")]


# --------------------------------------------------------------------------------------
# 1. human-only tasks
# --------------------------------------------------------------------------------------


def test_human_tasks_are_listed_skipped_and_pause_the_run_before_ship_until_ticked(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    runner, *_ = make_runner(w)
    out = run(runner)

    assert out.status == "paused", out.reason
    assert "2 human-only task(s) in tasks.md are still open before ship" in out.reason
    st = state(w)
    assert st.status == "paused"
    assert st.flags["human_before"] == {"T001": False, "T007": False}  # snapshot taken before the first implement session
    assert st.phase("implement").status == "passed" and st.phase("ship").attempts == 0
    assert (w.pushes, w.pr_creates, w.merges) == (0, 0, 0)

    # the implement messages name the human-only tasks; the worker skipped them
    first = implement_messages(w)[0]
    assert first.startswith(f"/speckit-implement {IMPLEMENT_ARGS} HUMAN-ONLY TASKS (reserved for a person): T001, T007. Never tick these; skip them.")
    text = tasks_file(w).read_text()
    assert "- [ ] T001 FOUNDER GATE" in text and "- [ ] T007 *(Operator step" in text and "- [X] T006" in text
    gate = events(w, "gate", phase="implement")[-1]
    assert gate["passed"] and any(x.startswith("[human-only-tasks] 2 human-only task(s) left for a person") for x in gate["warnings"])
    assert gate["facts"]["tasks_done"] == gate["facts"]["tasks_total"] == 5  # agent tasks only
    # the judge saw the human-only tasks as facts, not as remaining work
    implement_respond = [r for r in runner.judge.requests if r.mode == "respond" and r.phase == "implement"]
    assert implement_respond[-1].payload["facts"]["human_only_open"] == ["T001", "T007"]
    assert implement_respond[-1].payload["facts"]["unchecked_remaining"] == 0

    rep = report(w)
    assert rep.startswith("# Autopilot PAUSED (2 human-only task(s)")
    assert "## What a person must do before resuming" in rep
    assert "`T001 FOUNDER GATE: approve the Organic copy with the brand owner`" in rep
    assert "--resume" in rep and "-m speckit_autopilot" in rep
    assert events(w, "human_tasks_pause")

    # a --resume while they are still open pauses again (re-checked, nothing shipped)
    runner2, *_ = make_runner(w, resume=True)
    assert run(runner2).status == "paused"
    assert w.pushes == 0

    # the founder does the work and ticks both, then resumes: the run ships
    p = tasks_file(w)
    p.write_text(p.read_text().replace("- [ ] T001", "- [X] T001").replace("- [ ] T007", "- [X] T007"))
    runner3, *_ = make_runner(w, resume=True)
    out3 = run(runner3)
    assert out3.status == "completed", out3.reason
    assert (w.pushes, w.merges) == (1, 1)
    assert "## Human-only tasks (tasks.md, for a person)\n\n- none open" in report(w)


def test_an_agent_that_ticks_a_human_task_fails_the_gate_and_open_ones_reach_the_report_and_linear(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    w.tick_human = True  # a misbehaving implementer
    runner, *_ = make_runner(w, human_tasks_block_ship=False)
    out = run(runner)
    assert out.status == "completed", out.reason

    gates = events(w, "gate", phase="implement")
    assert not gates[0]["passed"]
    bad = [f for f in gates[0]["failed"] if f.startswith("[human-tasks-untouched]")]
    assert bad and "T001 FOUNDER GATE" in bad[0] and "T007" in bad[0]
    repair = [m for ph, m in w.repairs_seen if ph == "implement"]
    assert repair and "human-tasks-untouched" in repair[0] and "HUMAN-ONLY TASKS (reserved for a person): T001, T007" in repair[0]
    assert gates[-1]["passed"]

    # shipped without them (human_tasks_block_ship = false): the report and the Linear close-out say what is left
    rep = report(w)
    assert "- [ ] T001 FOUNDER GATE: approve the Organic copy with the brand owner" in rep
    assert "- [ ] T007 *(Operator step — not for agents)*" in rep
    closeout = [m for _, m in w.messages if m.startswith("AUTOPILOT LINEAR CLOSE-OUT")]
    assert len(closeout) == 1
    assert "Open human-only tasks (for a person; the pipeline did not do them)" in closeout[0]
    assert "   - T001 FOUNDER GATE: approve the Organic copy with the brand owner" in closeout[0]
    assert "T007" in closeout[0]


def test_resume_after_a_crash_keeps_the_first_human_snapshot(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    w.tick_human = True
    original = w._script

    def crash_after_founder_tick(s, msg, phase):
        t = original(s, msg, phase)
        if phase == "implement" and t.action is not None:
            act = t.action

            def act_then_crash(_s):
                act(_s)
                if "- [X] T001" in (w.feature_dir(s) / "tasks.md").read_text():
                    raise SimulatedCrash("crash right after the agent ticked the founder task")

            t.action = act_then_crash
        return t

    w._script = crash_after_founder_tick
    runner, *_ = make_runner(w, human_tasks_block_ship=False)
    with pytest.raises(SimulatedCrash, match="ticked the founder task"):
        run(runner)
    st = state(w)
    assert st.status == "running" and st.phase("implement").status == "running"
    assert st.flags["human_before"] == {"T001": False, "T007": False}
    assert "- [X] T001" in tasks_file(w).read_text()

    w._script = original
    w.tick_human = False
    runner2, *_ = make_runner(w, resume=True, human_tasks_block_ship=False)
    out = run(runner2)
    assert out.status == "completed", out.reason
    st = state(w)
    assert st.flags["human_before"] == {"T001": False, "T007": False}  # not re-taken on --resume
    gates = events(w, "gate", phase="implement")
    assert any(f.startswith("[human-tasks-untouched]") and "T001" in f for g in gates for f in g["failed"])
    assert "- [ ] T001" in G.read(Path(st.feature_dir) / "tasks.md")  # the repair un-ticked it


# --------------------------------------------------------------------------------------
# 2. founder-owned checklists
# --------------------------------------------------------------------------------------

CHECKLIST = "# Requirements Checklist\n\n- [x] CHK001 Requirements are testable\n- [ ] CHK002 Success criteria are measurable\n- [ ] CHK003 Edge cases are listed\n"


def test_checklists_gate_halt_pauses_before_implement_until_every_item_is_ticked(tmp_path):
    w = World(tmp_path)
    w.checklists = {"requirements.md": CHECKLIST, "ux.md": "# UX\n- [X] CHK010 done\n"}
    runner, *_ = make_runner(w, checklists_gate="halt")
    out = run(runner)

    assert out.status == "paused", out.reason
    assert "2 unchecked checklist item(s) in 1 file(s) before implement" in out.reason
    assert "implement" not in phase_starts(w)
    st = state(w)
    assert st.phase("analyze").status == "passed" and st.phase("implement").attempts == 0
    rep = report(w)
    rel = Path(st.feature_dir).relative_to(w.repo).as_posix()
    assert f"`{rel}/checklists/requirements.md`: 2 of 3 item(s) unchecked" in rep
    assert "ux.md" not in rep.split("## What a person must do")[1].split("Then resume")[0]
    assert "tick them (or fix the spec) in the feature branch, then --resume" in rep and "--resume" in rep
    assert events(w, "checklists_pause")[0]["checklists"] == [{"file": "checklists/requirements.md", "open": 2, "total": 3}]

    p = Path(st.feature_dir) / "checklists" / "requirements.md"
    p.write_text(CHECKLIST.replace("- [ ]", "- [x]"))
    runner2, *_ = make_runner(w, resume=True, checklists_gate="halt")
    out2 = run(runner2)
    assert out2.status == "completed", out2.reason
    assert phase_starts(w).count("implement") == 1


def test_checklists_gate_judge_keeps_todays_behaviour(tmp_path):
    w = World(tmp_path)
    w.checklists = {"requirements.md": CHECKLIST}
    runner, *_ = make_runner(w)  # default checklists_gate = "judge"
    out = run(runner)
    assert out.status == "completed", out.reason
    assert events(w, "checklists_pause") == []


def test_open_checklist_items_counts_like_speckit_implement(tmp_path):
    fd = tmp_path / "feature"
    (fd / "checklists").mkdir(parents=True)
    (fd / "checklists" / "a.md").write_text("- [ ] one\n  - [ ] nested\n* [x] two\n- [X] three\n```\n- [ ] in a code fence\n```\n")
    (fd / "checklists" / "b.md").write_text("- [x] all done\n")
    (fd / "checklists" / "notes.txt").write_text("- [ ] not markdown\n")
    assert open_checklist_items(fd) == [("checklists/a.md", 2, 4)]
    assert open_checklist_items(tmp_path / "missing") == []


# --------------------------------------------------------------------------------------
# 3. test baseline
# --------------------------------------------------------------------------------------


def test_test_baseline_measured_once_on_base_tolerates_only_preexisting_failures(tmp_path):
    w = World(tmp_path)
    base_sha = w.git("rev-parse", "HEAD")
    runner, judge, js, tr = make_runner(w, tool_results={"pytest -q": [(1, OLD)] * 7}, test_baseline="base")
    out = run(runner)
    assert out.status == "completed", out.reason

    assert tr.on_branch[0] == ("pytest -q", "main")  # measured before the feature branch existed
    assert [c for c in tr.on_branch if c[1] == "main"] == [("pytest -q", "main")]
    st = state(w)
    b = st.flags["test_baseline"]
    assert (b["sha"], b["failures"], b["cmd"], b["rc"]) == (base_sha, ["tests/test_old.py::test_legacy"], "pytest -q", 1)
    assert set(st.flags["baseline_used"]) >= {"implement", "review"}
    assert "already failing on base: tests/test_old.py::test_legacy" in st.flags["baseline_used"]["implement"]

    gate = events(w, "gate", phase="implement")[-1]
    assert gate["passed"]
    assert any(x.startswith("[test-preexisting-failures] 1 failing test(s), all already failing on base") for x in gate["warnings"])
    rep = report(w)
    assert "## Test baseline (failures already on the base branch)" in rep
    assert f"- Base `{base_sha[:12]}`: 1 test(s) already failing before this feature: tests/test_old.py::test_legacy" in rep
    assert "- Tolerated at implement: 1 failing test(s), all already failing on base" in rep


def test_test_baseline_a_new_failing_test_still_fails_the_gate(tmp_path):
    w = World(tmp_path)
    seq = [(1, OLD), (1, OLD_NEW), (1, OLD_NEW), (1, OLD), (1, OLD)]  # base, implement x2 (run + retry), repair x2
    runner, judge, js, tr = make_runner(w, tool_results={"pytest -q": seq}, test_baseline="base")
    out = run(runner)
    assert out.status == "completed", out.reason

    gates = events(w, "gate", phase="implement")
    assert not gates[0]["passed"]
    failed = [f for f in gates[0]["failed"] if f.startswith("[test-passes]")]
    assert failed and "1 new failing test(s) not failing on base (1 pre-existing): tests/test_new.py::test_feature" in failed[0]
    repair = [m for ph, m in w.repairs_seen if ph == "implement"]
    assert repair and "tests/test_new.py::test_feature" in repair[0]
    assert gates[-1]["passed"]


def test_test_baseline_unparseable_base_run_warns_and_stays_strict(tmp_path):
    w = World(tmp_path)
    runner, judge, js, tr = make_runner(w, tool_results={"pytest -q": [(1, COLLECT_ERROR)] + [(1, OLD)] * 30}, test_baseline="base")
    out = run(runner)
    assert out.status == "halted"
    st = state(w)
    assert st.halt["phase"] == "implement"
    assert st.flags["test_baseline"]["failures"] is None
    warn = events(w, "test_baseline_unparseable")
    assert warn and "ERROR collecting" in warn[0]["reason"]
    assert any("[test-passes]" in f and "baseline not applied: the base branch's failing tests are unknown" in f for f in st.halt["failed"])
    rep = report(w)
    assert "cannot be pinned to individual tests" in rep and "Every failing test fails the gates (strict)" in rep
    assert "- Not tolerated at implement: baseline not applied" in rep


def test_test_baseline_is_kept_across_resume_and_never_remeasured(tmp_path):
    w = World(tmp_path)
    runner, _, _, tr = make_runner(w, tool_results={"pytest -q": [(1, OLD)] * 3}, test_baseline="base", until="review")
    assert run(runner).status == "paused"
    before = state(w).flags["test_baseline"]
    runner2, _, _, tr2 = make_runner(w, tool_results={"pytest -q": [(1, OLD)] * 4}, test_baseline="base", resume=True)
    out = run(runner2)
    assert out.status == "completed", out.reason
    assert state(w).flags["test_baseline"] == before
    assert all(branch != "main" for _, branch in tr2.on_branch)  # the resumed run never went back to measure base
    assert "review" in state(w).flags["baseline_used"]


def test_test_baseline_none_is_the_default_and_strict(tmp_path):
    w = World(tmp_path)
    runner, _, _, tr = make_runner(w, tool_results={"pytest -q": [(1, OLD)] * 30})
    out = run(runner)
    assert out.status == "halted" and state(w).halt["phase"] == "implement"
    assert "test_baseline" not in state(w).flags
    assert all(branch != "main" for _, branch in tr.on_branch)


# --------------------------------------------------------------------------------------
# 4. clarify across midnight
# --------------------------------------------------------------------------------------


def test_clarify_accepts_a_session_dated_on_the_run_start_day_after_midnight(tmp_path):
    w = World(tmp_path)
    day = {"v": TODAY}
    write = w._write_clarifications

    def write_then_midnight(s):
        write(s)  # "### Session 2026-11-10"
        day["v"] = "2026-11-11"

    w._write_clarifications = write_then_midnight
    runner, *_ = make_runner(w, until="plan")
    runner.today = lambda: day["v"]
    out = run(runner)
    assert out.status == "paused", out.reason
    st = state(w)
    assert st.flags["start_date"] == TODAY
    assert (st.phase("clarify").status, st.phase("clarify").repairs) == ("passed", 0)
    gate = events(w, "gate", phase="clarify")[-1]
    assert gate["passed"]
    # without the run-start date the same spec fails today's check
    assert not G.gate_clarify(Path(st.feature_dir), "2026-11-11").passed
    assert G.gate_clarify(Path(st.feature_dir), "2026-11-11", since=TODAY).passed


# --------------------------------------------------------------------------------------
# 5. ANALYZE_SUMMARY trailer
# --------------------------------------------------------------------------------------


def test_analyze_trailer_turn_counts_beat_a_table_that_misses_a_high_finding(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("L2", "LOW")], [("L2", "LOW")]]
    w.analyze_trailers = ["ANALYZE_SUMMARY critical=0 high=1 medium=0 low=1"]  # round 2: counts of the table
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status == "completed", out.reason

    an = sessions(w, "analyze")
    assert [pipeline_inbox(s)[:2] for s in an] == [["/speckit-analyze", ANALYZE_TRAILER_REQUEST]] * 2
    gates = events(w, "gate", phase="analyze")
    assert [g["passed"] for g in gates] == [False, True]
    high = [f for f in gates[0]["failed"] if f.startswith("[zero-high]")]
    assert high and "the ANALYZE_SUMMARY trailer says 1, the table lists 0" in high[0]
    assert any(x.startswith("[trailer-matches-table]") for x in gates[0]["warnings"])
    assert gates[0]["facts"]["trailer"] == {"critical": 0, "high": 1, "medium": 0, "low": 1}
    assert events(w, "judge_disagreement", phase="analyze")  # the judge saw no HIGH row; the trailer count won
    assert len(w.edits_applied) == 1


# --------------------------------------------------------------------------------------
# 6. early routing (round 1, before any fix)
# --------------------------------------------------------------------------------------


def test_analyze_round1_early_route_back_to_plan_applies_no_fix(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("H1", "HIGH")], []]
    js = JudgeScript(w)
    js.early_route = {"analyze": {"action": "back", "target": "plan", "reason": "EARLY-PLAN-GAP: plan.md never designed the filter index"}}
    runner, judge, *_ = make_runner(w, js)
    out = run(runner)
    assert out.status == "completed", out.reason

    assert phase_starts(w) == ["feature_start", "clarify", "plan", "tasks", "analyze", "plan", "tasks", "analyze", "implement", "review", "ship", "linear"]
    assert w.analyze_calls == 2 and w.edits_applied == []  # routed before any fix
    assert events(w, "repair", phase="analyze") == []
    assert [r for r in judge.requests if r.mode == "respond" and r.phase == "analyze"] and len([r for r in judge.requests if r.mode == "respond" and r.phase == "analyze"]) == 1
    early = js.early_requests
    assert len(early) == 1 and early[0].payload["early"] is True
    assert any(f.startswith("[zero-high] 1 HIGH finding(s): ['H1']") for f in early[0].payload["failed"])
    prompt = judge_prompt(early[0])
    assert "no fix has been applied yet" in prompt and "fix_here" in prompt and "plan: an architecture / design gap in plan.md" in prompt

    st = state(w)
    assert [(b["from"], b["to"], b.get("early")) for b in st.back_edges] == [("analyze", "plan", True)]
    assert routes(w) == [("analyze", "back", "plan", True, True)]
    plan_cmds = [m for _, m in w.messages if m.startswith("/speckit-plan")]
    assert len(plan_cmds) == 2 and "EARLY-PLAN-GAP" in plan_cmds[1] and "[zero-high]" in plan_cmds[1]
    assert st.phase("analyze").attempts == 2 and st.phase("analyze").status == "passed"
    assert "- analyze → plan (early, before any fix): EARLY-PLAN-GAP" in report(w)
    end = [e for e in events(w, "phase_end", phase="analyze") if e["status"] == "routed_back"]
    assert len(end) == 1 and end[0]["target"] == "plan"


def test_analyze_early_route_fix_here_runs_the_normal_repairs_and_is_asked_once(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("H1", "HIGH")], [("H1", "HIGH")], []]
    runner, judge, js, _ = make_runner(w)
    out = run(runner)
    assert out.status == "completed", out.reason

    assert len(js.early_requests) == 1  # two blocking rounds, one early question (the first fix was then applied)
    assert routes(w) == [("analyze", "fix_here", None, True, True)]
    assert len(w.edits_applied) == 2 and state(w).back_edges == []
    assert phase_starts(w).count("analyze") == 1


def test_analyze_early_route_refused_by_the_limits_repairs_in_place(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("H1", "HIGH")], []]
    js = JudgeScript(w)
    js.early_route = {"analyze": {"action": "back", "target": "plan", "reason": "plan gap"}}
    runner, *_ = make_runner(w, js, max_back_edges=0)
    out = run(runner)
    assert out.status == "completed", out.reason

    rows = [r for r in decisions(w, "routing")]
    assert [(r["action"], r["target"], r["applied"], r.get("early")) for r in rows] == [("back", "plan", False, True)]
    assert "max_back_edges=0 already used; repairing in analyze instead" in rows[0]["note"]
    assert state(w).back_edges == [] and len(w.edits_applied) == 1
    assert "plan" not in phase_starts(w)[3:]
    rel = Path(state(w).feature_dir).relative_to(w.repo).as_posix()
    assert "**Routing (early, before any fix):** back → plan (not applied: max_back_edges=0 already used" in w.git("show", f"main:{rel}/decisions.md")


def test_review_round1_early_route_back_to_implement(tmp_path):
    w = World(tmp_path)
    w.review_blockers_rounds = [1, 0]
    js = JudgeScript(w)
    js.early_route = {"review": {"action": "back", "target": "implement", "reason": "EARLY-CODE-DEFECT: the filter crashes on None"}}
    runner, *_ = make_runner(w, js)
    out = run(runner)
    assert out.status == "completed", out.reason

    assert phase_starts(w) == ["feature_start", "clarify", "plan", "tasks", "analyze", "implement", "review", "implement", "review", "ship", "linear"]
    assert w.review_calls == 2
    assert not any(m.startswith("Approved, apply these fixes now") for _, m in w.messages)  # no fix before routing
    st = state(w)
    assert [(b["from"], b["to"], b.get("early")) for b in st.back_edges] == [("review", "implement", True)]
    assert routes(w) == [("review", "back", "implement", True, True)]
    fix = [m for m in implement_messages(w) if m.startswith("AUTOPILOT REPAIR (phase implement")]
    assert fix and "EARLY-CODE-DEFECT" in fix[0] and "[zero-must-fix]" in fix[0]
    assert (st.phase("implement").attempts, st.phase("review").attempts) == (2, 2)


def test_review_early_route_fix_here_keeps_the_fix_loop(tmp_path):
    w = World(tmp_path)
    w.review_blockers_rounds = [1, 0]
    runner, judge, js, _ = make_runner(w)
    out = run(runner)
    assert out.status == "completed", out.reason
    assert [r.phase for r in js.early_requests] == ["review"]
    assert routes(w) == [("review", "fix_here", None, True, True)]
    assert sum(1 for _, m in w.messages if m.startswith("Approved, apply these fixes now")) == 1
    assert state(w).back_edges == []


def test_early_route_invalid_judge_output_falls_back_to_repairs(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("H1", "HIGH")], []]
    js = JudgeScript(w)
    js.early_route = {"analyze": {"action": "sideways", "reason": "not a valid action"}}
    runner, *_ = make_runner(w, js)
    out = run(runner)
    assert out.status == "completed", out.reason
    rows = decisions(w, "routing")
    assert [(r["action"], r.get("early")) for r in rows] == [("fix_here", True)]
    assert rows[0]["reason"].startswith("judge output invalid") and len(w.edits_applied) == 1


def test_fix_here_after_the_repairs_are_used_up_halts(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("H1", "HIGH")]]  # never fixed
    js = JudgeScript(w)
    js.route_action = {"analyze": {"action": "fix_here", "reason": "still fixable here"}}
    runner, *_ = make_runner(w, js)
    out = run(runner)
    assert out.status == "halted"
    assert "judge routed to halt: still fixable here" in out.reason
    late = [r for r in decisions(w, "routing") if not r.get("early")]
    assert [(r["action"], r["note"]) for r in late] == [("halt", "judge answered 'fix_here'; no repair rounds left")]


def test_from_implement_takes_a_new_human_snapshot(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    runner, *_ = make_runner(w)
    assert run(runner).status == "paused"
    assert state(w).flags["human_before"] == {"T001": False, "T007": False}
    # the founder approves the copy; the operator restarts implement explicitly
    p = tasks_file(w)
    p.write_text(p.read_text().replace("- [ ] T001", "- [X] T001"))
    runner2, *_ = make_runner(w, from_phase="implement")
    out = run(runner2)
    assert out.status == "paused" and "1 human-only task(s)" in out.reason  # T007 is still open
    st = state(w)
    assert st.flags["human_before"] == {"T001": True, "T007": False}
    assert not any(f.startswith("[human-tasks-untouched]") for g in events(w, "gate", phase="implement") for f in g["failed"])
    assert "`T007 *(Operator step — not for agents)* rotate the staging API key in the dashboard`" in report(w)


# --------------------------------------------------------------------------------------
# 7. stopping promptly: in-flight judge calls are cancelled, tooling runs off the event loop
# --------------------------------------------------------------------------------------


class SlowJudge(FakeJudge):
    """A judge whose first matching call never returns on its own (a stalled SDK call). It asks the runner to stop
    shortly after the call started, from the event loop, like the SIGTERM handler does."""

    def __init__(self, js: JudgeScript, runner, mode: str, phase: str):
        super().__init__(js)
        self.runner, self.mode, self.phase = runner, mode, phase
        self.stalled: list[JudgeRequest] = []
        self.cancelled = False

    async def call(self, req: JudgeRequest) -> JudgeResult:
        if req.mode == self.mode and req.phase.split(":")[0] == self.phase and not self.stalled:
            self.stalled.append(req)
            asyncio.get_running_loop().call_later(0.05, self.runner.request_stop, "SIGTERM")
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return await super().call(req)


def test_a_stop_cancels_an_in_flight_judge_call_at_once_and_the_run_resumes(tmp_path):
    w = World(tmp_path)
    runner, _judge, js, _ = make_runner(w)
    slow = SlowJudge(js, runner, "verify", "plan")
    runner.judge = slow
    t0 = time.monotonic()
    out = run(runner)
    took = time.monotonic() - t0

    assert out.status == "stopped", out.reason
    assert took < 10, f"the stop waited {took:.1f}s for the judge (judge_timeout_min is {runner.cfg.judge_timeout_min} min)"
    assert slow.cancelled and slow.stalled and slow.stalled[0].mode == "verify"
    assert "stopped by SIGTERM" in out.reason and "--resume" in out.reason
    st = state(w)
    assert st.status == "stopped" and st.halt["phase"] == "plan"
    assert st.phase("plan").status != "passed" and st.phase("clarify").status == "passed"
    assert events(w, "signal") and events(w, "inflight_cancelled")
    assert report(w).startswith("# Autopilot STOPPED — ENG-142")

    runner2, *_ = make_runner(w, resume=True)
    out2 = run(runner2)
    assert out2.status == "completed", out2.reason
    assert state(w).phase("plan").status in ("passed", "already_satisfied")


def test_a_stop_while_the_judge_answers_an_ask_user_question_ends_the_run_stopped(tmp_path):
    w = World(tmp_path)
    w.ask_in_feature_start = True
    runner, _judge, js, _ = make_runner(w)
    slow = SlowJudge(js, runner, "answer", "feature_start")
    runner.judge = slow
    t0 = time.monotonic()
    out = run(runner)
    assert out.status == "stopped", out.reason
    assert time.monotonic() - t0 < 10 and slow.cancelled
    fs = sessions(w, "feature_start")
    assert fs and fs[0].answers == []  # the question was never answered (the policy denied it on the stop)
    assert any(e.get("error", "").startswith("StopRequested") for e in events(w, "ask_handler_error"))
    st = state(w)
    assert st.status == "stopped" and st.phase("feature_start").status != "passed"


def test_a_stop_kills_a_slow_test_run_and_the_event_loop_stays_free(tmp_path):
    """The stop is scheduled with loop.call_later from inside the test command: it can only fire while the command
    runs if tooling runs off the event loop. The real `sleep 30` is then killed (process group) and never retried."""
    w = World(tmp_path)
    runner, *_ = make_runner(w, test_cmd="sleep 30")
    calls: list[str] = []
    results: list[CmdResult] = []

    def tool(cmd: str, cwd: Path, timeout: float) -> CmdResult:
        calls.append(cmd)
        if cmd != "sleep 30":
            return CmdResult(cmd, 0, "ok", "")
        loop = runner._loop
        loop.call_soon_threadsafe(loop.call_later, 0.3, runner.request_stop, "SIGTERM")
        res = run_shell(cmd, cwd, timeout)
        results.append(res)
        return res

    runner.tool_runner = tool
    t0 = time.monotonic()
    out = run(runner)
    took = time.monotonic() - t0

    assert out.status == "stopped", out.reason
    assert took < 15, f"the stop took {took:.1f}s: the test command blocked the event loop"
    assert calls.count("sleep 30") == 1  # killed, not retried as a "failing" test
    assert results and results[0].returncode != 0 and results[0].duration_s < 10
    assert events(w, "tooling_killed")[0]["pids"]
    st = state(w)
    assert st.status == "stopped" and st.halt["phase"] == "implement" and st.phase("implement").status != "passed"
    assert not events(w, "tooling_end")  # the stopped run did not report a tooling result


def test_a_stop_during_the_base_test_measurement_returns_at_once_and_resume_measures_again(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, test_baseline="base")
    seen: list[str] = []

    def hung_test_runner(cmd: str, cwd: Path, timeout: float) -> CmdResult:
        seen.append(cmd)
        loop = runner._loop
        loop.call_soon_threadsafe(loop.call_later, 0.1, runner.request_stop, "SIGINT")
        t0 = time.monotonic()
        while not runner.stop_requested and time.monotonic() - t0 < 20:  # a command that only ends when killed
            time.sleep(0.01)
        return CmdResult(cmd, -2, "", "interrupted")

    runner.tool_runner = hung_test_runner
    t0 = time.monotonic()
    out = run(runner)
    assert out.status == "stopped" and time.monotonic() - t0 < 10, out.reason
    assert seen == ["pytest -q"] and "stopped by SIGINT" in out.reason
    st = state(w)
    assert "test_baseline" not in st.flags and st.feature_branch and w.git("rev-parse", "--abbrev-ref", "HEAD") == "main"

    runner2, *_ = make_runner(w, resume=True, test_baseline="base")
    out2 = run(runner2)
    assert out2.status == "completed", out2.reason
    assert state(w).flags["test_baseline"]["failures"] == []


def test_sigterm_reaches_the_runner_while_tooling_runs_and_the_handlers_are_restored(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w)
    runner.install_signal_handlers = True
    before = signal.getsignal(signal.SIGTERM)

    def tool(cmd: str, cwd: Path, timeout: float) -> CmdResult:
        if cmd == "pytest -q":
            os.kill(os.getpid(), signal.SIGTERM)  # what `--stop` sends
            t0 = time.monotonic()
            while not runner.stop_requested and time.monotonic() - t0 < 10:
                time.sleep(0.01)
            return CmdResult(cmd, -15, "", "terminated")
        return CmdResult(cmd, 0, "ok", "")

    runner.tool_runner = tool
    out = run(runner)
    assert out.status == "stopped" and "stopped by SIGTERM" in out.reason, out.reason
    assert signal.getsignal(signal.SIGTERM) is before
    assert [e["signal"] for e in events(w, "signal")] == ["SIGTERM"]


# --------------------------------------------------------------------------------------
# 8. shared Linear issues
# --------------------------------------------------------------------------------------

SHARED_FRONT_MATTER = '---\nspec: "116"\nslug: organic-filters\nlinear:\n  issueId: ENG-142\n  shared: true\n---\n\n'
OTHER_RUN_COMMENT = {"id": "c-other", "body": "**Autopilot close-out** · ENG-142 · run `20261101-090000-abc123`\n- spec 112 shipped"}


def test_shared_issue_in_spec_front_matter_is_commented_but_its_status_never_changes(tmp_path):
    w = World(tmp_path)
    w.spec_front_matter = SHARED_FRONT_MATTER
    w.linear.comments = [dict(OTHER_RUN_COMMENT)]  # spec 112's run already closed out on the same issue
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status == "completed", out.reason

    c = linear_writes(w)
    assert c["save_issue"] == 0 and c["save_comment"] == 1 and c["save_project"] == 1, c
    assert w.linear.issue["status"] == "In Progress" and w.linear.issue["statusType"] == "started"
    st = state(w)
    assert st.linear["shared"] == "spec.md front matter linear.shared: true, issueId ENG-142"
    mine = [x for x in w.linear.comments if st.run_id in x["body"]]
    assert len(mine) == 1 and len(w.linear.comments) == 2  # its own comment; spec 112's is left alone

    prompt = [m for s in sessions(w, "linear") for m in pipeline_inbox(s) if m.startswith("AUTOPILOT LINEAR CLOSE-OUT")][0]
    assert "ENG-142 is a SHARED issue (spec.md front matter linear.shared: true" in prompt
    assert "workflow state whose TYPE is `completed`" not in prompt and "for spec 008 (specs/008-organic-product-filters) only" in prompt
    assert all(isinstance(s.spec.gate, SharedIssueGate) for s in sessions(w, "linear"))
    gate = [g for g in events(w, "gate", phase="linear") if g["passed"]][-1]
    assert gate["facts"]["shared_issue"].startswith("spec.md front matter") and gate["facts"]["autopilot_comments"] == 1
    assert events(w, "shared_issue")
    assert "shared issue (spec.md front matter linear.shared: true, issueId ENG-142): status not changed" in report(w)


def test_shared_issue_listed_in_config_is_never_completed(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, shared_issues=["eng-142"])
    out = run(runner)
    assert out.status == "completed", out.reason
    assert linear_writes(w)["save_issue"] == 0 and w.linear.issue["statusType"] == "started"
    assert state(w).linear["shared"] == "listed in shared_issues"
    assert "status left as 'In Progress'" in state(w).linear["summary"]


def test_a_non_shared_issue_is_still_completed(tmp_path):
    w = World(tmp_path)
    w.spec_front_matter = '---\nlinear: { issueId: ENG-142 }\n---\n\n'
    runner, *_ = make_runner(w)
    assert run(runner).status == "completed"
    assert linear_writes(w)["save_issue"] == 1 and w.linear.issue["statusType"] == "completed"
    assert "shared" not in state(w).linear
    assert not any(isinstance(s.spec.gate, SharedIssueGate) for s in sessions(w, "linear"))


# --------------------------------------------------------------------------------------
# 9. the OS sandbox per phase, and the unattended contract
# --------------------------------------------------------------------------------------


def test_worker_sessions_carry_the_sandbox_per_phase_and_the_unattended_contract(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, sandbox="on", sandbox_network_domains=["binaries.prisma.sh"])
    out = run(runner)
    assert out.status == "completed", out.reason

    by_phase = {}
    for s in w.sessions:
        by_phase.setdefault(s.spec.phase, []).append(s.spec)
    for phase in ("feature_start", "ship", "linear", "linear_read"):
        assert all(spec.sandbox is None for spec in by_phase[phase]), phase
    for phase in ("analyze", "implement", "review"):
        for spec in by_phase[phase]:
            sb = spec.sandbox
            assert sb and sb["enabled"] and sb["allowUnsandboxedCommands"] is False, phase
            assert "binaries.prisma.sh" in sb["network"]["allowedDomains"]
    assert "registry.npmjs.org" in by_phase["implement"][0].sandbox["network"]["allowedDomains"]
    assert "registry.npmjs.org" not in by_phase["analyze"][0].sandbox["network"]["allowedDomains"]
    assert state(w).flags["sandbox"]["mode"] == "on" and state(w).flags["sandbox"]["active"] is True

    workers = [s for s in w.sessions if s.spec.phase not in ("linear_read",)]
    assert all(s.spec.env["AUTOPILOT"] == "1" and s.spec.env["AUTOPILOT_UNATTENDED"] == "1" for s in workers)
    assert all(s.spec.system_append.splitlines()[0] == UNATTENDED_LINE == "AUTOPILOT UNATTENDED MODE" for s in workers)
    ship = [m for s in sessions(w, "ship") for m in pipeline_inbox(s)][0]
    assert ship.splitlines()[0] == f"/cc-ship {SHIP_AUTHORIZATION}"
    assert "OS sandbox for worker Bash: on: active" in report(w)


def test_sandbox_off_gives_no_session_a_sandbox(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, sandbox="off")
    assert run(runner).status == "completed"
    assert all(s.spec.sandbox is None for s in w.sessions)
    assert state(w).flags["sandbox"] == {"mode": "off", "active": False, "detail": "off by configuration"}


def test_ship_authorisation_is_kept_when_a_repo_overrides_ship_args_without_it(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, ship_args="commit, push and merge to {base} with gh pr merge --merge.")
    assert run(runner).status == "completed"
    ship = [m for s in sessions(w, "ship") for m in pipeline_inbox(s)][0]
    assert ship.startswith(f"/cc-ship {SHIP_AUTHORIZATION}\ncommit, push and merge to main with gh pr merge --merge.")
    assert ship.count(SHIP_AUTHORIZATION) == 1


def test_ship_authorisation_stays_on_a_line_of_its_own(tmp_path):
    """The merged cc-ship skills accept the line only when it stands alone: appended text goes on later lines, and
    the phrase inside a sentence does not count as the line."""
    for i, ship_args in enumerate((SHIP_AUTHORIZATION, f"commit and merge; {SHIP_AUTHORIZATION} as agreed")):
        (tmp_path / str(i)).mkdir()
        w = World(tmp_path / str(i))
        runner, *_ = make_runner(w, ship_args=ship_args)
        assert run(runner).status == "completed"
        ship = [m for s in sessions(w, "ship") for m in pipeline_inbox(s)][0]
        lines = ship.splitlines()
        assert lines[0] == f"/cc-ship {SHIP_AUTHORIZATION}", lines
        assert lines[-1].startswith("Include ") and lines[-1].endswith("/decisions.md in the commit.")


# --------------------------------------------------------------------------------------
# 10. markers, the persisted early-route check, human-only tasks across stops and review fixes
# --------------------------------------------------------------------------------------


def test_escaped_visible_status_markers_still_pass_the_linear_gate(tmp_path):
    w = World(tmp_path)
    w.linear.visible_markers = True
    w.linear.escape_brackets = True  # Linear stores "[autopilot:status]" as "\[autopilot:status\]"
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status == "completed", out.reason
    raw = w.linear.project["description"]
    assert "\\[autopilot:status\\]" in raw and G.status_block(raw)[0] is None  # the raw text alone would fail the gate
    assert linear_writes(w)["save_project"] == 1  # passed on the first read-back, no second write
    gate = [g for g in events(w, "gate", phase="linear") if g["passed"]][-1]
    assert gate["facts"]["status_marker_style"] == "visible"


def test_a_resumed_analyze_does_not_ask_the_early_route_question_again(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("C1", "CRITICAL")], [("C1", "CRITICAL")], []]
    original = w._script
    crashed = {"done": False}

    def crash_while_applying(s, msg, phase):
        if msg.startswith("Approved, apply these edits now") and not crashed["done"]:
            crashed["done"] = True
            raise SimulatedCrash("crash while applying the approved analyze edits")
        return original(s, msg, phase)

    w._script = crash_while_applying
    runner, _j, js, _ = make_runner(w)
    with pytest.raises(SimulatedCrash):
        run(runner)
    assert len(js.early_requests) == 1 and state(w).flags["early_checked"] == {"analyze": True}

    w._script = original
    runner2, _j2, js2, _ = make_runner(w, resume=True)
    out = run(runner2)
    assert out.status == "completed", out.reason
    assert js2.early_requests == []  # the same phase entry: asked once, across the crash
    st = state(w)
    assert st.phase("analyze").status == "passed" and "analyze" not in (st.flags.get("early_checked") or {})


def test_a_person_ticking_a_human_task_while_the_run_is_halted_in_implement_is_accepted(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    runner, *_ = make_runner(w, tool_results={"pytest -q": [1] * 6})  # the implement gate fails until resumed
    out = run(runner)
    assert out.status == "halted" and state(w).phase("implement").status == "failed", out.reason
    assert state(w).flags["human_at_stop"] == {"T001": False, "T007": False}

    p = tasks_file(w)
    p.write_text(p.read_text().replace("- [ ] T001", "- [X] T001"))  # the founder approves the copy meanwhile
    runner2, *_ = make_runner(w, resume=True)
    out2 = run(runner2)
    assert out2.status == "paused" and "1 human-only task(s)" in out2.reason, out2.reason  # T007 is still open
    st = state(w)
    assert st.flags["human_before"] == {"T001": True, "T007": False} and "human_at_stop" in st.flags
    assert events(w, "human_tasks_ticked_while_stopped")[0]["tasks"] == ["T001"]
    # the implement gate now passes as it stands (no repair un-ticked the founder's tick)
    assert st.phase("implement").status == "already_satisfied" and "- [X] T001" in p.read_text()


def test_a_review_fix_that_ticks_a_human_task_fails_the_review_gate(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    w.review_blockers_rounds = [1, 0]
    original = w._apply_edit

    def fix_and_tick(s):
        original(s)
        if s.spec.gate.ctx.phase == "review":
            tasks = w.feature_dir(s) / "tasks.md"
            tasks.write_text(tasks.read_text().replace("- [ ] T001", "- [X] T001"))

    w._apply_edit = fix_and_tick
    runner, *_ = make_runner(w, human_tasks_block_ship=False)
    out = run(runner)
    assert out.status == "completed", out.reason
    review_gates = events(w, "gate", phase="review")
    assert any(f.startswith("[human-tasks-untouched]") and "T001" in f for g in review_gates for f in g["failed"])
    assert review_gates[-1]["passed"] and "- [ ] T001" in tasks_file(w).read_text()  # the repair un-ticked it


def test_an_agent_that_relabels_a_human_task_and_ticks_it_fails_the_implement_gate(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    original = w._implement_one_phase

    def relabel_and_tick(s):
        original(s)
        tasks = w.feature_dir(s) / "tasks.md"
        tasks.write_text(tasks.read_text().replace("- [ ] T001 FOUNDER GATE: approve", "- [X] T001 Approve"))

    w._implement_one_phase = relabel_and_tick
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status == "halted", out.reason
    failed = [f for g in events(w, "gate", phase="implement") for f in g["failed"]]
    assert any(f.startswith("[human-task-relabelled]") and "T001 Approve the Organic copy with the brand owner (its human-only label was removed)" in f for f in failed)
