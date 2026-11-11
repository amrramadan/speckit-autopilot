"""model_mode = fixed | auto: per-phase choice, repair escalation, judge recommendations, admin and a full run."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from speckit_autopilot import admin
from speckit_autopilot.config import Config, ConfigError, load_config
from speckit_autopilot.judge import VERIFY_SCHEMA, JudgeOutputError, validate
from speckit_autopilot.models import DEFAULT_PHASE_EFFORTS, DEFAULT_PHASE_MODELS, WorkerChoice, accept_recommendation, choose, escalate
from speckit_autopilot.prompts import judge_prompt
from speckit_autopilot.state import RunPaths, StateStore

from .fakeworld import JudgeScript, World, make_runner

# --------------------------------------------------------------------------------------
# choice
# --------------------------------------------------------------------------------------


def test_fixed_mode_uses_worker_model_and_effort_everywhere():
    cfg = Config(worker_model="opus", worker_effort="high")
    for ph in DEFAULT_PHASE_MODELS:
        for r in range(3):
            assert choose(cfg, ph, r).key == ("opus", "high")


def test_auto_mode_uses_the_per_phase_table_with_overrides():
    cfg = Config(model_mode="auto", phase_models={"plan": "sonnet"}, phase_efforts={"implement": "max"})
    assert choose(cfg, "clarify").key == (DEFAULT_PHASE_MODELS["clarify"], DEFAULT_PHASE_EFFORTS["clarify"])
    assert choose(cfg, "plan").key == ("sonnet", "high")
    assert choose(cfg, "implement").key == ("opus", "max")
    assert choose(cfg, "analyze:apply").key == choose(cfg, "analyze").key


def test_escalation_alternates_effort_then_model_and_is_capped():
    cfg = Config(model_mode="auto")
    assert [choose(cfg, "tasks", r).label() for r in range(4)] == ["sonnet/medium", "sonnet/high", "opus/high", "opus/high"]
    assert "repair round 1" in choose(cfg, "tasks", 1).reason


def test_escalation_hands_a_step_to_the_other_ladder_at_the_top():
    cfg = Config(model_mode="auto", auto_efforts=["medium", "high"])
    assert escalate(cfg, "opus", "high", 3) == ("opus", "high")  # both at the top
    assert escalate(cfg, "sonnet", "high", 1) == ("opus", "high")  # effort full: the model moves
    assert escalate(cfg, "opus", "medium", 2) == ("opus", "high")  # model full: the effort moves


def test_max_escalations_zero_never_escalates():
    cfg = Config(model_mode="auto", auto_max_escalations=0)
    assert choose(cfg, "plan", 5).key == choose(cfg, "plan", 0).key


def test_a_judge_recommendation_replaces_the_ladder():
    cfg = Config(model_mode="auto")
    c = choose(cfg, "plan", 2, {"model": "sonnet", "effort": "medium", "reason": "mechanical fixes"})
    assert c.key == ("sonnet", "medium") and c.reason == "judge: mechanical fixes"


@pytest.mark.parametrize(
    "rec,expect,note",
    [
        ({"model": "opus", "reason": "hard"}, {"model": "opus", "effort": "medium", "reason": "hard"}, "accepted"),
        ({"effort": "high", "reason": "x"}, {"model": "sonnet", "effort": "high", "reason": "x"}, "accepted"),
        ({"model": "gpt-9", "reason": "x"}, None, "not in auto_models"),
        ({"effort": "max", "reason": "x"}, None, "not in auto_efforts"),
        ({"model": "sonnet", "effort": "medium", "reason": "x"}, None, "same as current"),
        ("opus", None, ""),
    ],
)
def test_recommendations_are_validated(rec, expect, note):
    cfg = Config(model_mode="auto")
    got, why = accept_recommendation(cfg, WorkerChoice("sonnet", "medium", "x"), rec)
    assert got == expect and note in why


def test_fixed_mode_ignores_recommendations():
    assert accept_recommendation(Config(), WorkerChoice(None, None, "fixed"), {"model": "opus", "reason": "x"}) == (None, "")


# --------------------------------------------------------------------------------------
# config + schema + prompt
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "toml,msg",
    [
        ('model_mode = "smart"', "model_mode must be one of"),
        ('phase_models = { deploy = "opus" }', "unknown phase 'deploy'"),
        ('phase_efforts = { plan = "extreme" }', "phase_efforts.plan must be one of"),
        ('phase_models = { plan = 3 }', "a table of strings"),
        ("auto_models = []", "auto_models may not be empty"),
        ('auto_efforts = ["huge"]', "unknown level"),
        ("auto_max_escalations = -1", ">= 0"),
    ],
)
def test_bad_model_config_is_rejected(tmp_path, toml, msg):
    (tmp_path / ".autopilot.toml").write_text(toml + "\n")
    with pytest.raises(ConfigError, match=msg):
        load_config(tmp_path)


def test_verify_schema_accepts_next_worker_or_none():
    base = {"pass": False, "failed_criteria": ["x"], "notes": ""}
    validate({**base, "next_worker": {"model": "opus", "effort": None, "reason": "r"}}, VERIFY_SCHEMA)
    validate({**base, "next_worker": None}, VERIFY_SCHEMA)
    validate(base, VERIFY_SCHEMA)
    with pytest.raises(JudgeOutputError):
        validate({**base, "next_worker": {"model": "opus", "reason": "r", "temperature": 1}}, VERIFY_SCHEMA)


def _req(worker_auto):
    from speckit_autopilot.judge import JudgeRequest

    ctx = {"issue": "ENG-1", "description": "d", "repo": "/r", "base_branch": "main", "worker_auto": worker_auto}
    return JudgeRequest(mode="verify", phase="plan", context=ctx, payload={"gate": {}}, recent_decisions=[])


def test_verify_prompt_offers_next_worker_only_in_auto_mode():
    assert "next_worker" not in judge_prompt(_req(None))
    text = judge_prompt(_req({"current": "sonnet/medium", "models": ["sonnet", "opus"], "efforts": ["medium", "high"]}))
    assert "next_worker" in text and "sonnet/medium" in text and "Allowed models: sonnet, opus" in text


# --------------------------------------------------------------------------------------
# admin: speckit-autopilot model auto|fixed
# --------------------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


def test_admin_switches_model_mode_and_shows_the_table(git_repo, capsys):
    assert admin.main(["model", "auto", "--repo", str(git_repo)]) == 0
    out = capsys.readouterr().out
    assert load_config(git_repo).model_mode == "auto"
    assert "model_mode = auto" in out and "plan" in out and "opus / high" in out and "escalation: up to 2" in out
    assert admin.main(["model", "fixed", "--repo", str(git_repo)]) == 0
    assert load_config(git_repo).model_mode == "fixed"
    assert "mode    fixed" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="usage"):
        admin.main(["model", "auto", "opus", "--repo", str(git_repo)])


# --------------------------------------------------------------------------------------
# full runs
# --------------------------------------------------------------------------------------


def _models(w: World) -> list[tuple[str, str | None, str | None]]:
    return [(s.spec.phase, s.spec.model, s.spec.effort) for s in w.sessions if s.spec.phase not in ("linear_read",)]


def _events(w: World, name: str) -> list[dict]:
    return [e for e in (json.loads(x) for x in RunPaths(w.repo).events.read_text().splitlines()) if e.get("event") == name]


def test_fixed_mode_run_is_unchanged(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, worker_model="opus", worker_effort="high")
    out = asyncio.run(runner.run())
    assert out.status == "completed", out.reason
    assert {(m, e) for _, m, e in _models(w)} == {("opus", "high")}
    assert _events(w, "model_switch") == []
    assert StateStore(RunPaths(w.repo).state).load().model_switches == []


def test_auto_mode_run_uses_per_phase_models_and_logs_switches(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, model_mode="auto")
    out = asyncio.run(runner.run())
    assert out.status == "completed", out.reason
    by_phase = {ph: (m, e) for ph, m, e in _models(w)}
    assert by_phase["plan"] == ("opus", "high")
    assert by_phase["implement"] == ("opus", "high")
    assert by_phase["ship"] == ("sonnet", "medium")
    switches = StateStore(RunPaths(w.repo).state).load().model_switches
    assert switches and switches[0]["from"] is None
    assert any(s["phase"] == "plan" and s["to"] == "opus/high" for s in switches)
    assert len(_events(w, "model_switch")) == len(switches)
    assert "## Worker model switches" in RunPaths(w.repo).report.read_text()


def test_auto_mode_escalates_a_repair_round_in_a_fresh_session(tmp_path):
    w = World(tmp_path)
    js = JudgeScript(w)
    js.verify_fail = {"tasks": [["task T3 has no file path"]]}
    runner, *_ = make_runner(w, judge_script=js, model_mode="auto")
    out = asyncio.run(runner.run())
    assert out.status == "completed", out.reason
    tasks = [(m, e) for ph, m, e in _models(w) if ph == "tasks"]
    assert tasks[0] == ("sonnet", "medium") and ("sonnet", "high") in tasks[1:]
    assert any("repair round 1" in s["reason"] for s in StateStore(RunPaths(w.repo).state).load().model_switches)


def test_auto_mode_follows_a_valid_judge_recommendation(tmp_path):
    w = World(tmp_path)
    js = JudgeScript(w)
    js.verify_fail = {"tasks": [["the task graph misses the migration"]]}
    js.verify_next_worker = {"tasks": {"model": "opus", "effort": "high", "reason": "needs deeper reasoning"}}
    runner, *_ = make_runner(w, judge_script=js, model_mode="auto")
    out = asyncio.run(runner.run())
    assert out.status == "completed", out.reason
    tasks = [(m, e) for ph, m, e in _models(w) if ph == "tasks"]
    assert ("opus", "high") in tasks[1:]
    rec = _events(w, "model_recommendation")
    assert rec and rec[0]["outcome"] == "accepted"
    assert any(s["reason"] == "judge: needs deeper reasoning" for s in StateStore(RunPaths(w.repo).state).load().model_switches)


def test_live_row_shows_the_worker_model_only_in_auto_mode(tmp_path):
    from speckit_autopilot.live import load_run, status_row

    root = tmp_path / ".autopilot"
    root.mkdir()
    st = {"issue": "ENG-1", "status": "running", "current_phase": "plan", "phases": {"plan": {"status": "running", "attempts": 1}}, "flags": {"worker": {"model": "opus", "effort": "high"}}}
    (root / "state.json").write_text(json.dumps(st))
    assert load_run(tmp_path).worker == ""
    st["model_switches"] = [{"phase": "plan", "to": "opus/high"}]
    (root / "state.json").write_text(json.dumps(st))
    run = load_run(tmp_path)
    assert run.worker == "opus/high" and "plan (opus/high)" in status_row(run, 200, colour=False)
