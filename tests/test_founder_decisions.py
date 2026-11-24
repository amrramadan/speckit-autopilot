"""founder_decisions = "agent": human-only tasks that are only a decision are put to the judge; nothing else is."""

from __future__ import annotations

import asyncio

import pytest

from speckit_autopilot import gates as G
from speckit_autopilot.config import Config, ConfigError, load_config
from speckit_autopilot.judge import GATE_SCHEMA, JudgeOutputError, JudgeRequest, decision, validate
from speckit_autopilot.prompts import judge_prompt

from .fakeworld import JudgeScript, World, make_runner
from .test_e2e import decisions, events, report, run, sessions, state
from .test_e2e_fixall import TASKS_HUMAN, implement_messages, tasks_file


def task(line: str, legend: str = "") -> G.Task:
    return G.parse_tasks(f"{legend}\n\n- [ ] T001 {line}\n")[-1]


def gate_requests(runner):
    return [r for r in runner.judge.requests if r.mode == "gate"]


# ---------------------------------------------------------------- classifier


@pytest.mark.parametrize("line", [
    "FOUNDER GATE: approve widening **EXC-005** to `app/[locale]/p/[parent]`",
    "[H] FOUNDER GATE: approve widening **EXC-005** to the two routes",
    "[P] [US1] [H] approve the copy",
    "**FOUNDER GATE**: ratify the retention period",
    "Founder decision: which of the two layouts ships",
    "FOUNDER GATE (answered in the PR): choose the default locale",
    "Owner sign-off: the pricing table",
    "**Gate: the founder approves EXC-003 for the new route**",
])
def test_decisions_are_recognised(line):
    t = task(line)
    assert t.human and G.is_decision_task(t)


@pytest.mark.parametrize("line", [
    "Approve button in src/ui.tsx",  # agent work
    "*(Operator step — not for agents)* rotate the staging API key",
    "Operator act: approve the DNS change in the console",
    "PERSON: approve the build on a real device",
    "[RUN] approve the release",
    "[MANUAL] choose a device and run the suite",
    "[H] run the screen reader over /search",
    "[H] verify the checkout on an iPhone",
    "FOUNDER GATE: run the device matrix and report",
    "FOUNDER GATE: confirm on the host that the timer fires",
    "FOUNDER GATE: approve the copy; must not be ticked by an agent",
    "[H] approve the cutover (operator step: run it on the host)",
])
def test_work_a_person_performs_is_not_a_decision(line):
    assert not G.is_decision_task(task(line))


def test_a_tag_the_legend_reserves_is_not_a_decision():
    t = task("[OPERATOR] approve the window", "`[OPERATOR]` marks a task that is executed by a person")
    assert t.human and not G.is_decision_task(t)


def test_a_delegated_task_is_agent_work_only_while_it_is_the_same_task(tmp_path):
    f = tmp_path / "tasks.md"
    f.write_text("- [x] T001 FOUNDER GATE: approve the copy (decided by the decision agent)\n- [ ] T002 build\n")
    key = G.task_key("FOUNDER GATE: approve the copy")
    assert G.task_progress(tmp_path) == (0, 1)
    assert G.task_progress(tmp_path, {"T001": key}) == (1, 2)
    f.write_text("- [x] T001 FOUNDER GATE: approve the budget\n- [ ] T002 build\n")
    assert G.task_progress(tmp_path, {"T001": key}) == (0, 1)
    assert G.task_progress(tmp_path, {"T001": ""}) == (0, 1)


# ---------------------------------------------------------------- config, schema, prompt


def test_config_default_and_validation(tmp_path):
    assert Config(repo=tmp_path).founder_decisions == "human"
    (tmp_path / ".autopilot.toml").write_text('founder_decisions = "agent"\n')
    assert load_config(tmp_path).founder_decisions == "agent"
    (tmp_path / ".autopilot.toml").write_text('founder_decisions = "always"\n')
    with pytest.raises(ConfigError, match="founder_decisions"):
        load_config(tmp_path)


def test_gate_schema_and_prompt():
    ok = {"outcome": "decide", "decision": decision("x", "approve"), "escalate": False, "escalate_reason": ""}
    validate(ok, GATE_SCHEMA)
    with pytest.raises(JudgeOutputError):
        validate({**ok, "outcome": "approve"}, GATE_SCHEMA)
    text = judge_prompt(JudgeRequest("gate", "implement", {}, {"task_id": "T007", "task_text": "FOUNDER GATE: approve EXC-005"}))
    assert "TASK T007:" in text and "approve EXC-005" in text and "leave_to_person" in text and "constitution.md" in text


# ---------------------------------------------------------------- end to end


def test_default_never_asks_the_judge(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    runner, *_ = make_runner(w)
    assert run(runner).status == "paused"
    assert not gate_requests(runner) and "delegated" not in state(w).flags


def test_a_decided_gate_is_ticked_by_the_worker_and_operator_work_stays(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    runner, *_ = make_runner(w, founder_decisions="agent", human_tasks_block_ship=False)
    out = run(runner)
    assert out.status == "completed", out.reason

    assert [r.payload["task_id"] for r in gate_requests(runner)] == ["T001"]  # never T007, the operator step
    st = state(w)
    assert st.flags["human_before"] == {"T007": False}
    assert st.flags["delegated"]["T001"]["chosen"] == "approve the Organic copy as written"
    first = implement_messages(w)[0]
    assert "DELEGATED DECISIONS" in first and "- T001: approve the Organic copy as written" in first
    assert "never as a person's own approval" in first
    assert "HUMAN-ONLY TASKS (reserved for a person): T007." in first
    assert "DELEGATED DECISIONS" in sessions(w, "implement")[0].spec.system_append
    gate = events(w, "gate", phase="implement")[-1]
    assert gate["passed"] and gate["facts"]["tasks_total"] == 6
    assert any(d["item"].startswith("T001 (founder decision, delegated)") for d in decisions(w, "decision"))
    rep = report(w)
    assert "## Founder decisions taken by the decision agent" in rep and "T007" in rep.split("## Human-only tasks")[1]


def test_left_to_a_person_still_pauses_and_is_asked_once(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    js = JudgeScript(w)
    js.gates = {"T001": "leave_to_person"}
    runner, *_ = make_runner(w, js, founder_decisions="agent")
    out = run(runner)
    assert out.status == "paused" and "2 human-only task(s)" in out.reason
    assert len(gate_requests(runner)) == 1
    assert state(w).flags["human_before"] == {"T001": False, "T007": False}
    runner2, *_ = make_runner(w, js, founder_decisions="agent", resume=True)
    assert run(runner2).status == "paused" and not gate_requests(runner2)
    asyncio.run(runner._delegate_founder_decisions())  # implement entered again (a repair, a back-edge): not re-asked
    assert len(gate_requests(runner)) == 1


@pytest.mark.parametrize("bad", [None, {"outcome": "decide"}])
def test_invalid_judge_output_leaves_the_task_with_the_person(tmp_path, bad):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    js = JudgeScript(w)
    js.gates = {"T001": bad}
    runner, *_ = make_runner(w, js, founder_decisions="agent")
    out = run(runner)
    assert out.status == "paused" and "2 human-only task(s)" in out.reason
    assert not state(w).flags.get("delegated")


def test_a_worker_that_ticks_other_human_tasks_still_fails_the_gate(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    w.tick_human = True
    runner, *_ = make_runner(w, founder_decisions="agent", human_tasks_block_ship=False)
    run(runner)
    bad = [f for g in events(w, "gate", phase="implement") for f in g["failed"] if f.startswith("[human-tasks-untouched]")]
    assert bad and "T007" in bad[0] and "T001" not in bad[0]


def test_an_undone_delegated_task_fails_all_tasks_checked(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    w.follow_delegated = False
    runner, *_ = make_runner(w, founder_decisions="agent", human_tasks_block_ship=False)
    assert run(runner).status == "halted"
    assert any("5/6 agent tasks" in f for g in events(w, "gate", phase="implement") for f in g["failed"])


def test_an_escalated_decision_holds_the_ship(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    js = JudgeScript(w)
    js.gates = {"T001": {"outcome": "decide", "decision": decision("g", "approve", impact="high"), "escalate": True, "escalate_reason": "legal text"}}
    runner, *_ = make_runner(w, js, founder_decisions="agent", human_tasks_block_ship=False)
    out = run(runner)
    assert out.status == "halted" and "legal text" in " ".join(state(w).halt["failed"]) and w.pushes == 0


def test_turning_it_on_after_a_pause_delegates_on_the_next_implement_entry(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN
    runner, *_ = make_runner(w)
    assert run(runner).status == "paused"
    runner2, *_ = make_runner(w, founder_decisions="agent", human_tasks_block_ship=False, resume=True, from_phase="implement")
    out = run(runner2)
    assert out.status == "completed", out.reason
    assert state(w).flags["delegated"]["T001"] and len(gate_requests(runner2)) == 1
    merged = w.git("show", f"main:{tasks_file(w).relative_to(w.repo).as_posix()}")
    assert "- [X] T001 FOUNDER GATE" in merged and "- [ ] T007" in merged


def test_a_gate_the_agent_already_ticked_is_never_offered(tmp_path):
    w = World(tmp_path)
    w.tasks_md = TASKS_HUMAN.replace("- [ ] T001", "- [X] T001")
    runner, *_ = make_runner(w, founder_decisions="agent", human_tasks_block_ship=False)
    run(runner)
    assert not gate_requests(runner)
