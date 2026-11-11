"""judge.py: schema validation, FakeJudge retry semantics, SDKJudge JSON handling (no API calls)."""

from __future__ import annotations

import asyncio
import copy
import json

import claude_agent_sdk
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from speckit_autopilot.judge import (
    ANSWER_SCHEMA,
    RESPOND_SCHEMA,
    ROUTE_SCHEMA,
    SCHEMAS,
    VERIFY_SCHEMA,
    FakeJudge,
    JudgeOutputError,
    JudgeRequest,
    JudgeResult,
    SDKJudge,
    _extract_json,
    decision,
    respond,
    validate,
)
from speckit_autopilot.state import EventLog


@pytest.fixture(autouse=True)
def _no_real_sdk(monkeypatch):
    """Any accidental use of the real SDK entry points fails loudly instead of calling the API."""

    def _boom(*a, **k):
        raise AssertionError("real claude_agent_sdk.query()/ClaudeSDKClient used in a unit test")

    monkeypatch.setattr(claude_agent_sdk, "query", _boom)
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _boom)


# ======================================================================================
# validate()
# ======================================================================================

VALID = {
    "respond": respond("needs_reply", "B", [decision("Which field?", "Option B", recommended="Option A", impact="medium")], blocking=1),
    "answer": {
        "answers": [{"question": "Storage?", "labels": ["URL params"]}, {"question": "Other?", "labels": [], "free_text": "neither"}],
        "decisions": [decision("Storage?", "URL params")],
        "escalate": False,
        "escalate_reason": "",
    },
    "verify": {"pass": False, "failed_criteria": ["plan.md misses FR-002"], "notes": "gap"},
    "route": {"action": "back", "target_phase": "plan", "reason": "design gap"},
}


@pytest.mark.parametrize("mode", sorted(VALID))
def test_valid_outputs_pass(mode):
    validate(VALID[mode], SCHEMAS[mode])


def test_valid_variants():
    validate({"pass": True, "failed_criteria": [], "notes": "", "escalate": True, "escalate_reason": "pii"}, VERIFY_SCHEMA)
    validate({"action": "halt", "reason": "x"}, ROUTE_SCHEMA)  # target_phase optional
    validate({"action": "forward", "target_phase": None, "reason": "x"}, ROUTE_SCHEMA)
    validate(respond("done"), RESPOND_SCHEMA)
    d = decision("q", "a")
    d["recommended"] = None
    d["followed_recommendation"] = None
    validate(respond("done", decisions=[d]), RESPOND_SCHEMA)


def _mut(mode, fn):
    d = copy.deepcopy(VALID[mode])
    fn(d)
    return d


INVALID = [
    ("respond", lambda d: d.pop("reply"), "missing required field 'reply'"),
    ("respond", lambda d: d.update(status="maybe"), "not in"),
    ("respond", lambda d: d.update(blocking_issues_remaining=-1), "minimum"),
    ("respond", lambda d: d.update(blocking_issues_remaining=True), "expected ['integer']"),
    ("respond", lambda d: d.update(blocking_issues_remaining=1.5), "expected ['integer']"),
    ("respond", lambda d: d.update(escalate="yes"), "expected ['boolean']"),
    ("respond", lambda d: d.update(decisions={"item": "x"}), "expected ['array']"),
    ("respond", lambda d: d["decisions"][0].update(impact="huge"), "$.decisions[0].impact"),
    ("respond", lambda d: d["decisions"][0].pop("rationale"), "$.decisions[0]: missing required field 'rationale'"),
    ("respond", lambda d: d["decisions"][0].update(followed_recommendation="yes"), "followed_recommendation"),
    ("answer", lambda d: d["answers"][0].update(labels="URL params"), "$.answers[0].labels"),
    ("answer", lambda d: d["answers"][0].pop("question"), "missing required field 'question'"),
    ("answer", lambda d: d.pop("escalate_reason"), "escalate_reason"),
    ("answer", lambda d: d["answers"][0].update(labels=[1]), "$.answers[0].labels[0]"),
    ("verify", lambda d: d.update({"pass": "true"}), "expected ['boolean']"),
    ("verify", lambda d: d.pop("failed_criteria"), "failed_criteria"),
    ("verify", lambda d: d.update(failed_criteria=[{"x": 1}]), "$.failed_criteria[0]"),
    ("route", lambda d: d.update(action="sideways"), "not in"),
    ("route", lambda d: d.update(target_phase=3), "$.target_phase"),
    ("route", lambda d: d.pop("reason"), "missing required field 'reason'"),
]


@pytest.mark.parametrize("mode,mutate,msg", INVALID, ids=[f"{m}-{i}" for i, (m, _, _) in enumerate(INVALID)])
def test_invalid_outputs_raise(mode, mutate, msg):
    with pytest.raises(JudgeOutputError) as ei:
        validate(_mut(mode, mutate), SCHEMAS[mode])
    assert msg in str(ei.value)


@pytest.mark.parametrize("bad", [None, [], "done", 3])
@pytest.mark.parametrize("mode", sorted(SCHEMAS))
def test_non_object_top_level_rejected(mode, bad):
    with pytest.raises(JudgeOutputError):
        validate(bad, SCHEMAS[mode])


def test_validate_enforces_additional_properties():
    """additionalProperties=false is enforced here too, so FakeJudge behaves like the SDK's output_format."""
    with pytest.raises(JudgeOutputError):
        validate({**VALID["verify"], "unexpected": 1}, VERIFY_SCHEMA)


# ======================================================================================
# helpers + JudgeResult
# ======================================================================================


def test_respond_helper_shape():
    r = respond("needs_reply", "yes", blocking=2, escalate=True, reason="payments")
    assert r == {"status": "needs_reply", "reply": "yes", "decisions": [], "blocking_issues_remaining": 2, "escalate": True, "escalate_reason": "payments"}
    validate(r, RESPOND_SCHEMA)


@pytest.mark.parametrize(
    "kw,followed",
    [
        (dict(recommended="A", followed=None), False),  # recommended != chosen
        (dict(recommended="B", followed=None), True),  # recommended == chosen
        (dict(recommended=None, followed=None), None),  # no recommendation -> null
        (dict(recommended="A", followed=True), True),  # explicit wins
    ],
)
def test_decision_helper_followed_inference(kw, followed):
    d = decision("Which?", "B", **kw)
    assert d["followed_recommendation"] is followed
    assert set(d) == {"item", "recommended", "chosen", "followed_recommendation", "rationale", "evidence", "impact"}
    validate(respond("done", decisions=[d]), RESPOND_SCHEMA)


def test_judge_result_accessors_defaults():
    jr = JudgeResult("respond", {})
    assert jr.status == "failed" and jr.reply == "" and jr.decisions == [] and not jr.escalate and jr.escalate_reason == ""
    jr = JudgeResult("respond", respond("done", "x", [decision("q", "a")], escalate=True, reason="pii"))
    assert jr.status == "done" and jr.reply == "x" and len(jr.decisions) == 1 and jr.escalate and jr.escalate_reason == "pii"
    jr.decisions.append({})  # returns a copy
    assert len(jr.decisions) == 1


# ======================================================================================
# FakeJudge
# ======================================================================================


def _req(mode="respond", phase="clarify"):
    return JudgeRequest(mode=mode, phase=phase, context={"issue": "ENG-1", "description": "d", "repo": "/r", "base_branch": "main"}, payload={"worker_text": "hi"})


def test_fake_judge_returns_valid_output_first_try():
    calls = []
    fj = FakeJudge(lambda r: calls.append(r.mode) or respond("done"), cost_usd=0.01)
    jr = asyncio.run(fj.respond(_req()))
    assert jr.ok and jr.status == "done" and jr.cost_usd == 0.01
    assert calls == ["respond"]
    assert len(fj.requests) == 1


def test_fake_judge_retries_once_then_fails():
    calls = []
    fj = FakeJudge(lambda r: calls.append(1) or None)
    jr = asyncio.run(fj.verify(_req()))
    assert not jr.ok
    assert jr.data == {}
    assert jr.error == "no output"
    assert len(calls) == 2  # exactly one retry
    assert jr.status == "failed"


def test_fake_judge_invalid_then_valid_recovers():
    outs = [{"pass": "nope"}, {"pass": True, "failed_criteria": [], "notes": ""}]
    fj = FakeJudge(lambda r: outs.pop(0))
    jr = asyncio.run(fj.verify(_req()))
    assert jr.ok and jr.data["pass"] is True
    assert outs == []


def test_fake_judge_invalid_twice_reports_last_error():
    fj = FakeJudge(lambda r: {"action": "jump", "reason": "x"})
    jr = asyncio.run(fj.route(_req()))
    assert not jr.ok and "jump" in jr.error


@pytest.mark.parametrize("method", ["respond", "answer", "verify", "route"])
def test_judge_helpers_set_the_mode(method):
    seen = []
    fj = FakeJudge(lambda r: seen.append(r.mode) or None)
    req = _req(mode="something-else")
    asyncio.run(getattr(fj, method)(req))
    assert req.mode == method
    assert set(seen) == {method}


# ======================================================================================
# SDKJudge._extract_json
# ======================================================================================


@pytest.mark.parametrize(
    "text,expect",
    [
        ('{"pass": true, "failed_criteria": [], "notes": ""}', {"pass": True, "failed_criteria": [], "notes": ""}),
        ('```json\n{"action": "halt", "reason": "x"}\n```', {"action": "halt", "reason": "x"}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('Here is my decision:\n{"a": {"b": [1, 2]}}\nThanks.', {"a": {"b": [1, 2]}}),
        ("  \n {\"a\": \"}\"}  \n", {"a": "}"}),
        ("no json at all", None),
        ("{broken json", None),
        ('{"a": 1} and then {"b": 2}', None),
        ("", None),
    ],
)
def test_extract_json(text, expect):
    assert _extract_json(text) == expect


# ======================================================================================
# SDKJudge.call with a fake query()
# ======================================================================================


def _result(structured=None, text="", cost=0.02, is_error=False, subtype="success"):
    return ResultMessage(
        subtype=subtype,
        duration_ms=10,
        duration_api_ms=8,
        is_error=is_error,
        num_turns=1,
        session_id="judge-s",
        total_cost_usd=cost,
        result=text,
        structured_output=structured,
    )


class FakeQuery:
    """Stands in for claude_agent_sdk.query: one scripted list of messages (or an exception) per call."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.prompts: list[str] = []
        self.options: list = []

    def __call__(self, *, prompt, options=None, **kw):
        self.prompts.append(prompt)
        self.options.append(options)
        script = self.scripts.pop(0)

        async def gen():
            if isinstance(script, Exception):
                raise script
            for m in script:
                yield m

        return gen()


@pytest.fixture
def sdk_judge(tmp_path):
    log = EventLog(tmp_path / "run.jsonl", "r")
    return SDKJudge(tmp_path, "claude-opus-5-5", 1.5, log, tmp_path / "transcripts", "SYSTEM", effort="high", max_turns=7)


def test_sdk_judge_structured_output_first_try(monkeypatch, sdk_judge):
    data = {"pass": True, "failed_criteria": [], "notes": "ok"}
    fq = FakeQuery([[AssistantMessage(content=[TextBlock("checking"), ToolUseBlock("t1", "Read", {"file_path": "spec.md"})], model="m"), _result(structured=data, cost=0.03)]])
    monkeypatch.setattr(claude_agent_sdk, "query", fq)
    jr = asyncio.run(sdk_judge.verify(_req("verify", "plan")))
    assert jr.ok and jr.data == data
    assert jr.cost_usd == pytest.approx(0.03)
    assert len(fq.prompts) == 1
    assert "MODE: verify" in fq.prompts[0] and "DEFINITION OF DONE for plan" in fq.prompts[0]
    opts = fq.options[0]
    assert opts.output_format == {"type": "json_schema", "schema": VERIFY_SCHEMA}
    assert opts.allowed_tools == ["Read", "Grep", "Glob"] and opts.tools == ["Read", "Grep", "Glob"]
    assert opts.permission_mode == "dontAsk"
    assert opts.max_budget_usd == 1.5 and opts.max_turns == 7 and opts.model == "claude-opus-5-5" and opts.effort == "high"
    assert opts.system_prompt == "SYSTEM"
    events = sdk_judge.log.read()
    assert [e["event"] for e in events] == ["judge"]
    # transcript written
    files = list((sdk_judge.transcripts_dir).glob("*-judge-001-plan-verify.jsonl"))
    assert files and "tool_use" in files[0].read_text()


def test_sdk_judge_falls_back_to_result_text(monkeypatch, sdk_judge):
    fq = FakeQuery([[_result(structured=None, text='```json\n{"action": "halt", "reason": "stuck"}\n```')]])
    monkeypatch.setattr(claude_agent_sdk, "query", fq)
    jr = asyncio.run(sdk_judge.route(_req("route", "analyze")))
    assert jr.ok and jr.data == {"action": "halt", "reason": "stuck"}


def test_sdk_judge_retries_once_with_error_note_and_sums_cost(monkeypatch, sdk_judge):
    fq = FakeQuery([[_result(structured={"status": "done"}, cost=0.02)], [_result(structured=respond("done"), cost=0.05)]])
    monkeypatch.setattr(claude_agent_sdk, "query", fq)
    jr = asyncio.run(sdk_judge.respond(_req()))
    assert jr.ok and jr.status == "done"
    assert len(fq.prompts) == 2
    assert "YOUR PREVIOUS OUTPUT WAS INVALID" not in fq.prompts[0]
    assert "YOUR PREVIOUS OUTPUT WAS INVALID" in fq.prompts[1] and "missing required field 'reply'" in fq.prompts[1]
    assert jr.cost_usd == pytest.approx(0.07)  # each query() is a fresh session: costs add up
    assert [e["event"] for e in sdk_judge.log.read()] == ["judge_invalid", "judge"]


def test_sdk_judge_gives_up_after_two_invalid_outputs(monkeypatch, sdk_judge):
    fq = FakeQuery([[_result(structured=None, text="I think B")], [_result(structured=None, is_error=True, subtype="error_max_turns", text="")]])
    monkeypatch.setattr(claude_agent_sdk, "query", fq)
    jr = asyncio.run(sdk_judge.respond(_req()))
    assert not jr.ok and jr.data == {}
    assert "error_max_turns" in jr.error
    assert len(fq.prompts) == 2


def test_sdk_judge_survives_an_exception_on_first_attempt(monkeypatch, sdk_judge):
    fq = FakeQuery([RuntimeError("CLI crashed"), [_result(structured=VALID["route"])]])
    monkeypatch.setattr(claude_agent_sdk, "query", fq)
    jr = asyncio.run(sdk_judge.route(_req("route", "analyze")))
    assert jr.ok and jr.data["target_phase"] == "plan"
    assert "judge call raised RuntimeError: CLI crashed" in fq.prompts[1]


def test_sdk_judge_two_exceptions_fail(monkeypatch, sdk_judge):
    fq = FakeQuery([RuntimeError("a"), RuntimeError("b")])
    monkeypatch.setattr(claude_agent_sdk, "query", fq)
    jr = asyncio.run(sdk_judge.route(_req("route", "analyze")))
    assert not jr.ok and "RuntimeError: b" in jr.error


def test_schemas_are_json_serialisable():
    for s in SCHEMAS.values():
        json.dumps(s)
    assert ANSWER_SCHEMA["properties"]["answers"]["items"]["required"] == ["question", "labels"]


def test_route_schema_accepts_fix_here_for_the_early_check():
    validate({"action": "fix_here", "target_phase": None, "reason": "root cause is in analyze"}, ROUTE_SCHEMA)
    assert ROUTE_SCHEMA["properties"]["action"]["enum"] == ["forward", "back", "halt", "fix_here"]
