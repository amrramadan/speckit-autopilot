"""End-to-end regressions for the defects found in the adversarial review (FakeSession + FakeJudge, no API calls)."""

from __future__ import annotations

import json
from typing import Any

from speckit_autopilot.sessions import FakeTurn

from .fakeworld import World, make_runner
from .test_e2e import events, linear_writes, paths, pipeline_inbox, report, run, state


def test_analyze_verify_failure_does_not_crash_and_fixes_in_the_same_session(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("M1", "MEDIUM")], [("M1", "MEDIUM")]]
    runner, judge, js, _ = make_runner(w)
    js.analyze_accept_all = True
    js.verify_fail = {"analyze": [["finding M1 was not consciously accepted"]]}
    out = run(runner)
    assert out.status == "completed", out.reason
    assert state(w).phase("analyze").repairs == 1
    assert not events(w, "internal_error")


def test_review_verify_failure_does_not_crash(tmp_path):
    w = World(tmp_path)
    runner, judge, js, _ = make_runner(w)
    js.verify_fail = {"review": [["nit N1 was neither fixed nor accepted"]]}
    out = run(runner)
    assert out.status == "completed", out.reason
    assert state(w).phase("review").repairs == 1


def test_wrong_workspace_halts_without_posting_anything_to_linear(tmp_path):
    w = World(tmp_path)
    w.linear.workspace = "https://linear.app/some-other-workspace"
    runner, *_ = make_runner(w)
    runner.cfg.linear_workspace_url = "https://linear.app/test-ws"
    out = run(runner)
    assert out.status == "halted" and "wrong workspace" in out.reason
    assert w.linear.writes == [] and w.linear.comments == []
    assert not any(s.spec.phase == "halt_notice" for s in w.sessions)


def test_unexpected_exception_becomes_a_high_priority_halt_with_report(tmp_path):
    w = World(tmp_path)
    original = w._script

    def boom(s, msg, phase):
        if msg.startswith("/speckit-plan"):
            raise RuntimeError("CLI connection lost")
        return original(s, msg, phase)

    w._script = boom
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status == "halted" and "internal error" in out.reason
    st = state(w)
    assert st.halt["priority"] == "high" and st.status == "halted"
    assert "internal error: RuntimeError: CLI connection lost" in report(w)
    assert events(w, "internal_error")


def test_from_plan_forces_downstream_phases_to_rerun(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, until="ship")
    assert run(runner).status == "paused"
    sent_before = sum(1 for _, m in w.messages if m.startswith("/speckit-tasks"))
    runner, *_ = make_runner(w, from_phase="plan", until="ship")
    assert run(runner).status == "paused"
    st = state(w)
    assert sum(1 for _, m in w.messages if m.startswith("/speckit-tasks")) == sent_before + 1
    for p in ("plan", "tasks", "analyze", "implement", "review"):
        assert st.phase(p).status == "passed", (p, st.phase(p).status)
    assert st.flags.get("force_rerun") == []


def test_route_back_passes_the_reason_to_the_rerun_phase(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("C1", "CRITICAL")]] * 3 + [[]]
    runner, judge, js, _ = make_runner(w)
    js.analyze_accept_all = True  # judge "accepts" CRITICAL -> gate keeps failing -> route
    js.route_action = {"analyze": {"action": "back", "target": "plan", "reason": "UNIQUE-ROUTE-REASON architecture gap"}}
    out = run(runner)
    plan_cmds = [m for _, m in w.messages if m.startswith("/speckit-plan")]
    assert len(plan_cmds) == 2
    assert "UNIQUE-ROUTE-REASON" in plan_cmds[1] and "UNIQUE-ROUTE-REASON" not in plan_cmds[0]
    briefs = [m for _, m in w.messages if m.startswith("AUTOPILOT HANDOFF BRIEF") and "Current phase: plan" in m]
    assert any("UNIQUE-ROUTE-REASON" in b for b in briefs)
    assert out.status in ("completed", "halted")


def test_linear_readback_failure_never_writes_blind(tmp_path):
    w = World(tmp_path)
    reads = {"n": 0}
    original = w.linear.reader_results

    def flaky():
        reads["n"] += 1
        res = original()
        if reads["n"] >= 3:  # identity read + first close-out read succeed, later reads break
            res["mcp__linear__list_comments"] = "Error: upstream timeout"
        return res

    w.linear.reader_results = flaky
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status == "halted" and "read-back failed" in out.reason
    c = linear_writes(w)
    assert c["save_comment"] <= 1 and c["save_issue"] <= 1, c


def test_s1_turn_cap_resends_the_next_command_in_a_fresh_session(tmp_path):
    w = World(tmp_path)
    w.clarify_questions = 5
    runner, *_ = make_runner(w, max_turns_per_session=6)
    out = run(runner)
    assert out.status == "completed", out.reason
    assert any(m.startswith("/speckit-tasks") for _, m in w.messages)
    assert any("turn cap" in r["reason"] for r in state(w).resets)


def test_force_rerun_survives_a_stop_and_resume(tmp_path):
    w = World(tmp_path)
    w.analyze_rounds = [[("C1", "CRITICAL")]] * 3 + [[]]
    runner, judge, js, _ = make_runner(w)
    js.analyze_accept_all = True
    js.route_action = {"analyze": {"action": "back", "target": "plan", "reason": "gap"}}
    original = w._script
    stopped = {"done": False}

    def stop_on_rerun(s, msg, phase):
        if msg.startswith("/speckit-plan") and sum(1 for _, m in w.messages if m.startswith("/speckit-plan")) == 2 and not stopped["done"]:
            stopped["done"] = True
            runner.stop_requested = True
            return FakeTurn("partial")
        return original(s, msg, phase)

    w._script = stop_on_rerun
    out = run(runner)
    assert out.status == "stopped"
    assert set(state(w).flags.get("force_rerun", [])) >= {"plan", "tasks", "analyze"}
    w._script = original
    runner2, judge2, js2, _ = make_runner(w, resume=True)
    js2.analyze_accept_all = True
    out2 = run(runner2)
    st = state(w)
    assert st.phase("tasks").status == "passed" and st.phase("tasks").attempts >= 2  # re-ran, not "already satisfied"
    assert out2.status == "completed", out2.reason


def test_compaction_forces_a_reset_at_the_next_turn_boundary(tmp_path):
    w = World(tmp_path)
    w.clarify_questions = 3
    original = w._script

    def compact_on_first_question(s, msg, phase):
        t = original(s, msg, phase)
        if msg.startswith("/speckit-clarify") and phase == "clarify":
            t.compaction = True
        return t

    w._script = compact_on_first_question
    runner, *_ = make_runner(w)
    out = run(runner)
    assert out.status == "completed", out.reason
    assert any("auto-compaction" in r["reason"] for r in state(w).resets)
    clarify_cmds = [m for _, m in w.messages if m.startswith("/speckit-clarify")]
    assert len(clarify_cmds) >= 2  # resent in a fresh session after the compaction


def test_ship_gate_ignores_late_escalations_once_shipping_started(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w)
    st_before = None
    out = run(runner)
    assert out.status == "completed"
    st = state(w)
    st.escalations.append({"phase": "ship", "reason": "late"})
    st_before = st
    from speckit_autopilot.state import StateStore

    StateStore(paths(w).state).save(st_before)
    runner2, *_ = make_runner(w, resume=True)
    out2 = run(runner2)
    assert out2.status == "completed"


def test_notify_command_is_not_injectable(tmp_path):
    from speckit_autopilot.report import notify

    marker = tmp_path / "INJECTED"
    out = tmp_path / "out.txt"
    for payload in (f"x; touch {marker}", f"$(touch {marker})", f"`touch {marker}`", f"can't stop'; touch {marker}; echo '"):
        notify(f"printf '%s' {{message}} > {out}", payload, None)
        assert not marker.exists(), payload
    notify(f"printf '%s' {{message}} > {out}", "can't stop: plan failed", None)
    assert out.read_text() == "can't stop: plan failed"  # apostrophes survive
