"""End-to-end regression tests (fake world, no API calls) for the third adversarial review of the runner:

1. a founder pause (checklists / human-only tasks) keeps the user's --until; only an --until pause drops it;
2. a human-only task whose label an agent removes stays human-only for the run (gate, pause, report);
3. a back-edge to tasks (or earlier) never turns an agent's tick of a human-only task into the new snapshot;
4. on a shared Linear issue, a close-out comment that lost its run id still counts as this run's;
5. a stop during the Definition-of-Done check is never accepted as "already satisfied" on --resume;
6. the early route check counts as asked only once the judge has answered it.
"""

from __future__ import annotations

import re
from pathlib import Path

from .fakeworld import FakeTurn, JudgeScript, World, make_runner
from .test_e2e import decisions, events, phase_starts, report, run, state
from .test_e2e_fixall import OTHER_RUN_COMMENT, SHARED_FRONT_MATTER, TASKS_HUMAN, SlowJudge, tasks_file

CHECKLIST = "# Requirements Checklist\n\n- [x] CHK001 Requirements are testable\n- [ ] CHK002 Success criteria are measurable\n"


# --------------------------------------------------------------------------------------
# 1. --until survives a founder pause
# --------------------------------------------------------------------------------------


def test_a_checklist_pause_keeps_until_so_a_plain_resume_still_stops_before_ship(tmp_path):
    w = World(tmp_path)
    w.checklists = {"requirements.md": CHECKLIST}
    runner, *_ = make_runner(w, checklists_gate="halt", until="ship")
    out = run(runner)
    assert out.status == "paused" and "checklist" in out.reason
    assert state(w).flags.get("until") == "ship"

    p = Path(state(w).feature_dir) / "checklists" / "requirements.md"
    p.write_text(CHECKLIST.replace("- [ ]", "- [x]"))
    runner2, *_ = make_runner(w, resume=True, checklists_gate="halt")  # plain --resume, as the pause items say
    out2 = run(runner2)
    assert out2.status == "paused", out2.reason
    assert out2.reason == "--until ship: stopped before ship"
    assert (w.pushes, w.merges) == (0, 0)

    # the --until stop itself happened now: the next plain --resume continues past it
    runner3, *_ = make_runner(w, resume=True, checklists_gate="halt")
    out3 = run(runner3)
    assert out3.status == "completed", out3.reason
    assert w.merges == 1


def test_a_human_task_pause_keeps_until_linear(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    runner, *_ = make_runner(w, until="linear")
    out = run(runner)
    assert out.status == "paused" and "human-only task(s)" in out.reason
    p = tasks_file(w)
    p.write_text(p.read_text().replace("- [ ] T001", "- [X] T001").replace("- [ ] T007", "- [X] T007"))
    runner2, *_ = make_runner(w, resume=True)
    out2 = run(runner2)
    assert out2.status == "paused", out2.reason
    assert out2.reason == "--until linear: stopped before linear"
    assert w.merges == 1 and not [x for x in w.linear.writes if x[0].endswith("save_comment")]


def test_a_plain_until_pause_is_still_dropped_by_a_plain_resume(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, until="implement")
    assert run(runner).reason == "--until implement: stopped before implement"
    runner2, *_ = make_runner(w, resume=True)
    out2 = run(runner2)
    assert out2.status == "completed", out2.reason


# --------------------------------------------------------------------------------------
# 2. relabelling a human-only task (without ticking it)
# --------------------------------------------------------------------------------------


def _relabel(text: str) -> str:
    return text.replace("T001 FOUNDER GATE: approve", "T001 Approve").replace("T007 *(Operator step — not for agents)* rotate", "T007 Rotate")


def test_a_review_fix_that_removes_human_labels_without_ticking_fails_the_review_gate(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    w.review_blockers_rounds = [1, 0]
    orig = w._apply_edit

    def fix_and_relabel(s):
        orig(s)
        if s.spec.gate.ctx.phase == "review":
            t = w.feature_dir(s) / "tasks.md"
            t.write_text(_relabel(t.read_text()))

    w._apply_edit = fix_and_relabel
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status != "completed", out.reason
    assert w.merges == 0
    failed = [f for g in events(w, "gate", phase="review") for f in g["failed"]]
    assert any(f.startswith("[human-tasks-untouched]") and "T001" in f and "label was removed" in f for f in failed), failed


def test_human_tasks_relabelled_while_paused_still_hold_the_ship_pause(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    runner, *_ = make_runner(w)
    assert run(runner).status == "paused"
    p = tasks_file(w)
    p.write_text(_relabel(p.read_text()))  # labels gone, nothing ticked
    runner2, *_ = make_runner(w, resume=True)
    out2 = run(runner2)
    assert out2.status == "paused", out2.reason
    assert "2 human-only task(s)" in out2.reason
    assert w.merges == 0
    assert "`T001 Approve the Organic copy with the brand owner`" in report(w)


# --------------------------------------------------------------------------------------
# 3. a back-edge to tasks keeps the human-only snapshot's open tasks open
# --------------------------------------------------------------------------------------


def test_routing_back_to_tasks_does_not_launder_an_agent_tick_of_a_human_task(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    w.review_blockers_rounds = [1, 0]
    orig_apply = w._apply_edit

    def fix_and_tick(s):
        orig_apply(s)
        if s.spec.gate.ctx.phase == "review":
            t = w.feature_dir(s) / "tasks.md"
            t.write_text(t.read_text().replace("- [ ] T001", "- [X] T001"))

    w._apply_edit = fix_and_tick
    orig_repair = w._repair

    def repair(s, msg, phase):
        if phase != "review":  # the review fixer never un-ticks
            orig_repair(s, msg, phase)

    w._repair = repair
    orig_script = w._script

    def script(s, msg, phase):
        # the re-run of /speckit-tasks keeps the existing ticks and appends the missing task
        if msg.startswith("/speckit-tasks") and len([x for x in phase_starts(w) if x == "tasks"]) > 1:
            p = w.feature_dir(s) / "tasks.md"
            return FakeTurn("tasks.md updated", action=lambda _s: p.write_text(p.read_text() + "- [ ] T008 Add a None guard test\n"))
        return orig_script(s, msg, phase)

    w._script = script
    js = JudgeScript(w)
    js.route_action = {"review": {"action": "back", "target": "tasks", "reason": "missing work no task covers"}}
    runner, *_ = make_runner(w, js)
    out = run(runner)
    assert phase_starts(w).count("tasks") == 2
    st = state(w)
    assert st.flags["human_before"]["T001"] is False  # the agent's tick never became the snapshot
    assert w.merges == 0
    assert out.status == "paused" and "2 human-only task(s)" in out.reason, out.reason
    assert "- [ ] T001 FOUNDER GATE" in tasks_file(w).read_text()  # the implement repair had to un-tick it
    impl_failed = [f for g in events(w, "gate", phase="implement") for f in g["failed"]]
    assert any(f.startswith("[human-tasks-untouched]") and "T001" in f for f in impl_failed), impl_failed


def test_routing_back_to_tasks_still_takes_a_new_snapshot_for_new_human_tasks(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    w.review_blockers_rounds = [1, 1, 1, 0]
    js = JudgeScript(w)
    js.route_action = {"review": {"action": "back", "target": "tasks", "reason": "missing a founder gate"}}
    orig_script = w._script

    def script(s, msg, phase):
        if msg.startswith("/speckit-tasks") and len([x for x in phase_starts(w) if x == "tasks"]) > 1:
            p = w.feature_dir(s) / "tasks.md"
            return FakeTurn("tasks.md updated", action=lambda _s: p.write_text(p.read_text() + "- [ ] T009 FOUNDER GATE: sign off the launch copy\n"))
        return orig_script(s, msg, phase)

    w._script = script
    runner, *_ = make_runner(w, js)
    out = run(runner)
    assert phase_starts(w).count("tasks") == 2
    assert state(w).flags["human_before"] == {"T001": False, "T007": False, "T009": False}
    assert out.status == "paused" and "3 human-only task(s)" in out.reason, out.reason


# --------------------------------------------------------------------------------------
# 4. shared issue: a close-out comment without the run id
# --------------------------------------------------------------------------------------


def test_a_shared_issue_closeout_comment_that_lost_its_run_id_is_not_posted_again(tmp_path):
    w = World(tmp_path)
    w.spec_front_matter = SHARED_FRONT_MATTER
    w.linear.comments = [dict(OTHER_RUN_COMMENT)]
    orig = w.linear.closeout

    def closeout(prompt, today, run_marker=""):
        out = []
        for name, inp in orig(prompt, today, run_marker):
            if name.endswith("save_comment"):
                inp = dict(inp, body=re.sub(r" · run `[^`]+`", "", inp["body"]))  # dropped by the worker / Linear
            out.append((name, inp))
        return out

    w.linear.closeout = closeout
    runner, *_ = make_runner(w)
    out = run(runner)
    bodies = [c["body"] for c in w.linear.comments]
    assert out.status == "completed", out.reason
    assert len([b for b in bodies if b.startswith("**Autopilot close-out**")]) == 2, bodies  # the other run's + ours
    assert w.linear.issue["status"] == "In Progress"


def test_a_shared_issue_with_two_new_closeout_comments_halts_instead_of_posting_more(tmp_path):
    w = World(tmp_path)
    w.spec_front_matter = SHARED_FRONT_MATTER
    w.linear.comments = [dict(OTHER_RUN_COMMENT)]
    orig = w.linear.closeout

    def closeout(prompt, today, run_marker=""):
        calls = orig(prompt, today, run_marker)
        out = []
        for name, inp in calls:
            if name.endswith("save_comment"):
                inp = dict(inp, body=re.sub(r" · run `[^`]+`", "", inp["body"]))
                out += [(name, inp), (name, dict(inp))]  # a misbehaving worker posts twice
            else:
                out.append((name, inp))
        return out

    w.linear.closeout = closeout
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status == "halted", out.reason
    assert "2 autopilot close-out comments already exist on ENG-142" in out.reason
    assert len([c for c in w.linear.comments if c["body"].startswith("**Autopilot close-out**")]) == 3  # other run's + 2


# --------------------------------------------------------------------------------------
# 5. a stop during the DoD check
# --------------------------------------------------------------------------------------


def test_a_stop_during_the_implement_dod_check_is_verified_again_on_resume(tmp_path):
    w = World(tmp_path)
    js = JudgeScript(w)
    js.verify_fail = {"implement": [["TODO stub in src/filters.py: origin filter returns a placeholder"]] * 5}
    runner, *_ = make_runner(w, js)
    runner.judge = SlowJudge(js, runner, "verify", "implement")
    assert run(runner).status == "stopped"
    assert state(w).phase("implement").status == "running"

    runner2, judge2, _, _ = make_runner(w, js, resume=True)
    out2 = run(runner2)
    assert [r.mode for r in judge2.requests if r.phase == "implement" and r.mode == "verify"], "the DoD was never checked"
    assert state(w).phase("implement").status != "already_satisfied"
    assert out2.status != "completed" and w.merges == 0, out2.reason


def test_an_already_satisfied_phase_whose_dod_passes_is_still_skipped(tmp_path):
    w = World(tmp_path)
    js = JudgeScript(w)
    runner, *_ = make_runner(w, js)
    runner.judge = SlowJudge(js, runner, "verify", "implement")
    assert run(runner).status == "stopped"
    runner2, judge2, _, _ = make_runner(w, js, resume=True)
    out2 = run(runner2)
    assert out2.status == "completed", out2.reason
    assert state(w).phase("implement").status == "already_satisfied"
    assert [r for r in judge2.requests if r.phase == "implement" and r.mode == "verify"]


# --------------------------------------------------------------------------------------
# 6. the early route check
# --------------------------------------------------------------------------------------


def test_a_stop_while_the_early_route_question_is_in_flight_asks_it_again_on_resume(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("H1", "HIGH")], [("H1", "HIGH")], []]
    js = JudgeScript(w)
    js.early_route = {"analyze": {"action": "back", "target": "plan", "reason": "design gap"}}
    runner, *_ = make_runner(w, js)
    runner.judge = SlowJudge(js, runner, "route", "analyze")
    assert run(runner).status == "stopped"
    assert not (state(w).flags.get("early_checked") or {}).get("analyze")

    runner2, *_ = make_runner(w, js, resume=True)
    out2 = run(runner2)
    assert out2.status == "completed", out2.reason
    assert len(js.early_requests) == 1
    assert [(b["from"], b["to"], b.get("early")) for b in state(w).back_edges] == [("analyze", "plan", True)]
    assert [(r["action"], r["target"]) for r in decisions(w, "routing")] == [("back", "plan")]


# --------------------------------------------------------------------------------------
# 7. human-only tasks that are the pipeline's own review / ship steps
# --------------------------------------------------------------------------------------

TASKS_PIPELINE_STEP = TASKS_HUMAN.replace("- [ ] T001 FOUNDER GATE", "- [X] T001 FOUNDER GATE").replace("- [ ] T007 *(Operator", "- [X] T007 *(Operator") + (
    "- [ ] T008 Operator steps (not for agents): `/cc-review`, then `/cc-ship` (it runs `cc-linear-sync`), then the first PR.\n"
)


def test_an_operator_review_and_ship_step_does_not_hold_the_ship_pause(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_PIPELINE_STEP
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status == "completed", out.reason
    assert w.merges == 1
    assert not events(w, "human_tasks_pause")
    assert [e["tasks"] for e in events(w, "human_tasks_pipeline_steps")] == [["T008"]]
    assert "- [ ] T008 Operator steps" in tasks_file(w).read_text()  # never ticked by an agent
    rep = report(w)
    assert "T008 Operator steps (not for agents)" in rep and "this run performs /cc-review" in rep


def test_a_pipeline_step_next_to_a_real_human_task_still_pauses_for_the_real_one(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_PIPELINE_STEP.replace("- [X] T007 *(Operator", "- [ ] T007 *(Operator")
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status == "paused" and out.reason.startswith("1 human-only task(s)"), out.reason
    assert w.merges == 0
