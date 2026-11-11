"""sessions.py + budget accounting.

FakeSession <-> PolicyGate wiring, AskUserQuestion routing, env scrubbing, per-turn cost
deltas from the SDK's cumulative ``total_cost_usd`` (built from the real
claude_agent_sdk dataclasses, never a CLI), SDKWorkerSession.send against a stub client,
and Runner._check_limits. No API calls.
"""

from __future__ import annotations

import asyncio
import os

import claude_agent_sdk
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from speckit_autopilot.config import Config
from speckit_autopilot.policy import PolicyContext, PolicyGate, map_answers
from speckit_autopilot.runner import Halt
from speckit_autopilot.sessions import (
    _LEAKY_ENV,
    FakeSession,
    FakeTurn,
    SDKWorkerSession,
    SessionError,
    SessionSpec,
    ToolCall,
    ToolResult,
    TurnResult,
    DEFAULT_SANDBOX_DOMAINS,
    _TurnAccumulator,
    pnpm_store_dir,
    sandbox_available,
    sandbox_env,
    sandbox_settings,
    scrub_parent_session_env,
)
from speckit_autopilot.state import EventLog, RunState

from .fakeworld import World, make_runner


@pytest.fixture(autouse=True)
def _no_real_sdk(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("real claude_agent_sdk entry point used in a unit test")

    monkeypatch.setattr(claude_agent_sdk, "query", _boom)
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _boom)


def _gate(repo, phase="implement", ask_handler=None, events=None):
    ctx = PolicyContext(phase=phase, repo=repo, base_branch="main", protected_paths=tuple(Config().protected_paths))
    log = (lambda ev, **kw: events.append((ev, kw))) if events is not None else (lambda *a, **k: None)
    return PolicyGate(ctx, log_event=log, ask_handler=ask_handler)


def _spec(repo, gate, phase="implement", **kw):
    return SessionSpec(label=f"S-{phase}", phase=phase, cwd=repo, gate=gate, system_append="APPEND", budget_usd=2.0, **kw)


def _fake(repo, turns, phase="implement", ask_handler=None, events=None, **kw):
    gate = _gate(repo, phase, ask_handler, events)
    it = iter(turns)
    sess = FakeSession(_spec(repo, gate, phase, **kw), lambda s, m: next(it))
    return sess, gate


# ======================================================================================
# FakeSession + PolicyGate
# ======================================================================================


def test_fake_session_denied_tool_blocks_action_and_records_denial(tmp_path):
    ran = []
    events: list = []
    sess, gate = _fake(tmp_path, [FakeTurn("pushed", tool_calls=[("Bash", {"command": "git push origin HEAD"})], action=lambda s: ran.append(1))], events=events)
    res = asyncio.run(sess.send("go", timeout_s=5))
    assert ran == []  # the side effect never happens
    assert sess.allowed == []
    assert len(sess.denied) == 1
    name, inp, msg = sess.denied[0]
    assert name == "Bash" and inp == {"command": "git push origin HEAD"}
    assert msg.startswith("AUTOPILOT POLICY (")
    tid = res.tool_calls[0].id
    assert res.tool_results[tid].is_error and res.tool_results[tid].content == msg
    assert gate.denials and gate.denials[0]["source"] == "hook" and gate.denials[0]["phase"] == "implement"
    assert events and events[0][0] == "denial"


def test_fake_session_one_denied_call_blocks_the_whole_action(tmp_path):
    ran = []
    sess, _ = _fake(
        tmp_path,
        [FakeTurn("x", tool_calls=[("Edit", {"file_path": str(tmp_path / "src" / "a.py")}), ("Edit", {"file_path": str(tmp_path / ".specify" / "memory" / "constitution.md")})], action=lambda s: ran.append(1))],
    )
    res = asyncio.run(sess.send("go", 5))
    assert ran == []
    assert [n for n, _ in sess.allowed] == ["Edit"]
    assert len(sess.denied) == 1 and "constitution" in sess.denied[0][1]["file_path"]
    assert [r.is_error for r in res.tool_results.values()] == [False, True]


def test_fake_session_allowed_tools_run_action_and_return_payloads(tmp_path):
    ran = []
    sess, gate = _fake(
        tmp_path,
        [FakeTurn("done", tool_calls=[("Edit", {"file_path": str(tmp_path / "src" / "a.py")}), ("Read", {"file_path": str(tmp_path / "README.md")})], tool_results={"Read": "# readme"}, action=lambda s: ran.append(s.label))],
    )
    res = asyncio.run(sess.send("go", 5))
    assert ran == ["S-implement"]
    assert sess.denied == [] and gate.denials == []
    assert [c.name for c in res.tool_calls] == ["Edit", "Read"]
    payloads = [r.content for r in res.tool_results.values()]
    assert payloads == ["ok", "# readme"]
    assert res.reply_text == "done" and res.session_id == "fake-S-implement"


def test_fake_session_tool_outside_allowed_tools_goes_through_can_use_tool(tmp_path):
    sess, gate = _fake(tmp_path, [FakeTurn("ls", tool_calls=[("Bash", {"command": "ls -la"})])])
    assert "Bash" not in sess.spec.allowed_tools
    asyncio.run(sess.send("go", 5))
    assert sess.allowed == [("Bash", {"command": "ls -la"})]
    assert gate.denials == []


def test_fake_session_linear_write_denied_outside_linear_phase(tmp_path):
    sess, gate = _fake(tmp_path, [FakeTurn("x", tool_calls=[("mcp__linear__save_issue", {"id": "ENG-1"})])], phase="implement")
    asyncio.run(sess.send("go", 5))
    assert sess.denied and gate.denials[0]["rule"] == "linear-write-phase"


def test_ask_user_question_routed_through_can_use_tool(tmp_path):
    seen = []
    questions = [{"question": "Which storage?", "header": "Storage", "multiSelect": False, "options": [{"label": "URL params (Recommended)", "description": "x"}, {"label": "Local storage", "description": "y"}]}]

    async def handler(tool_input):
        seen.append(tool_input)
        return {**tool_input, "answers": map_answers(tool_input["questions"], {"Which storage?": "Local storage"})}

    sess, gate = _fake(tmp_path, [FakeTurn("asked", ask=questions)], phase="feature_start", ask_handler=handler)
    asyncio.run(sess.send("go", 5))
    assert seen == [{"questions": questions}]
    assert sess.answers == [{"Which storage?": "Local storage"}]
    assert gate.denials == []  # AskUserQuestion is never a policy denial


def test_ask_user_question_without_handler_records_no_answers(tmp_path):
    sess, _ = _fake(tmp_path, [FakeTurn("asked", ask=[{"question": "Q?", "options": [{"label": "A"}]}])], ask_handler=None)
    asyncio.run(sess.send("go", 5))
    assert sess.answers == []


def test_gate_can_use_tool_ask_returns_allow_with_updated_input(tmp_path):
    async def handler(ti):
        return {**ti, "answers": {"Q?": "A"}}

    gate = _gate(tmp_path, ask_handler=handler)
    r = asyncio.run(gate.can_use_tool("AskUserQuestion", {"questions": [{"question": "Q?"}]}, None))
    assert isinstance(r, PermissionResultAllow)
    assert r.updated_input == {"questions": [{"question": "Q?"}], "answers": {"Q?": "A"}}


def test_fake_session_cost_deltas_and_session_total(tmp_path):
    sess, _ = _fake(tmp_path, [FakeTurn("a", cost_usd=0.10), FakeTurn("b", cost_usd=0.25, compaction=True), FakeTurn("c", cost_usd=0.0, subtype="error_max_budget_usd", is_error=True)])
    r1 = asyncio.run(sess.send("1", 5))
    r2 = asyncio.run(sess.send("2", 5))
    r3 = asyncio.run(sess.send("3", 5))
    assert (r1.cost_delta_usd, r1.session_cost_usd) == (0.10, pytest.approx(0.10))
    assert (r2.cost_delta_usd, r2.session_cost_usd) == (0.25, pytest.approx(0.35))
    assert r2.compacted and sess.compaction_pending
    assert r3.budget_exceeded and r3.is_error
    assert sess.total_cost_usd == pytest.approx(0.35)
    assert sess.turns == 3 and sess.inbox == ["1", "2", "3"]


def test_fake_session_hang_times_out_and_interrupts(tmp_path):
    sess, _ = _fake(tmp_path, [FakeTurn(hang=True)])
    r = asyncio.run(sess.send("go", timeout_s=0.01))
    assert r.timed_out and r.interrupted and r.is_error and r.subtype == "timeout"
    assert sess.interrupted


def test_fake_session_context_pct_and_lifecycle(tmp_path):
    sess, _ = _fake(tmp_path, [FakeTurn("x", context_pct=42.0)])

    async def go():
        async with sess as s:
            await s.send("x", 5)
            assert await s.context_pct() == 42.0
        assert sess.closed
        with pytest.raises(SessionError):
            await sess.send("again", 5)

    asyncio.run(go())
    assert sess.started


# ======================================================================================
# scrub_parent_session_env
# ======================================================================================


def test_scrub_parent_session_env_removes_only_listed(monkeypatch):
    for k in _LEAKY_ENV:
        monkeypatch.setenv(k, "1")
    keep = {"CLAUDE_CODE_USE_BEDROCK": "0", "ANTHROPIC_API_KEY_HELPER_TTL": "x", "CLAUDE_CONFIG_DIR": "/tmp/c", "PATH": os.environ.get("PATH", "")}
    for k, v in keep.items():
        monkeypatch.setenv(k, v)
    removed = scrub_parent_session_env()
    assert sorted(removed) == sorted(_LEAKY_ENV)
    for k in _LEAKY_ENV:
        assert k not in os.environ
    for k, v in keep.items():
        assert os.environ.get(k) == v
    assert scrub_parent_session_env() == []  # nothing left to remove


def test_scrub_parent_session_env_partial(monkeypatch):
    for k in _LEAKY_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SSE_PORT", "1234")
    assert sorted(scrub_parent_session_env()) == ["CLAUDECODE", "CLAUDE_CODE_SSE_PORT"]


def test_leaky_env_list_contents():
    assert "CLAUDECODE" in _LEAKY_ENV and "CLAUDE_CODE_SSE_PORT" in _LEAKY_ENV and "CLAUDE_CODE_ENTRYPOINT" in _LEAKY_ENV
    # never scrub credentials or provider routing
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "PATH", "HOME"):
        assert k not in _LEAKY_ENV


# ======================================================================================
# TurnResult + _TurnAccumulator (real SDK dataclasses)
# ======================================================================================


def _rm(total=None, *, result="final", origin=None, subtype="success", is_error=False, errors=None, num_turns=3, session_id="sess-1"):
    return ResultMessage(
        subtype=subtype,
        duration_ms=100,
        duration_api_ms=90,
        is_error=is_error,
        num_turns=num_turns,
        session_id=session_id,
        total_cost_usd=total,
        result=result,
        errors=errors,
        origin=origin,
    )


@pytest.fixture
def sdk_sess(tmp_path):
    gate = _gate(tmp_path)
    log = EventLog(tmp_path / "run.jsonl", "r")
    return SDKWorkerSession(_spec(tmp_path, gate), log, tmp_path / "transcripts")


def _feed(acc, sess, msgs):
    done = False
    for m in msgs:
        assert not done, "message after the turn completed"
        done = acc.add(m, sess)
    return done


def test_turn_result_reply_text_and_summary():
    assert TurnResult(text="all", final_text="").reply_text == "all"
    assert TurnResult(text="all", final_text="fin").reply_text == "fin"
    t = TurnResult(final_text="word " * 100)
    s = t.summary(20)
    assert len(s) == 21 and s.endswith("…")
    assert TurnResult(final_text="a\n\n  b").summary() == "a b"


def test_tool_result_text_variants():
    assert ToolResult("t", "plain").text() == "plain"
    assert ToolResult("t", [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]).text() == "a\nb"
    assert ToolResult("t", [TextBlock("x")]).text() == "x"
    assert ToolResult("t", None).text() == ""
    assert ToolResult("t", 5).text() == "5"


def test_accumulator_collects_text_tools_and_results(sdk_sess):
    acc = _TurnAccumulator()
    msgs = [
        SystemMessage("init", {"session_id": "sess-1"}),
        AssistantMessage(content=[TextBlock("Looking at the spec."), ToolUseBlock("tu1", "Bash", {"command": "ls"})], model="m"),
        AssistantMessage(content=[TextBlock("sub-agent chatter"), ToolUseBlock("tu2", "Read", {"file_path": "x"})], model="m", parent_tool_use_id="tu0"),
        UserMessage(content=[ToolResultBlock("tu1", "a.py\nb.py", False), ToolResultBlock("tu2", [{"type": "text", "text": "x"}], True)]),
        UserMessage(content="a plain user string is ignored"),
        AssistantMessage(content=[TextBlock("Done. Q1?"), TextBlock("   ")], model="m"),
        _rm(0.10, result="Done. Q1?"),
    ]
    assert _feed(acc, sdk_sess, msgs)
    r = acc.to_result(sdk_sess, 1.5)
    assert r.text == "Looking at the spec.\n\nDone. Q1?"  # sub-agent text and blank blocks excluded
    assert r.final_text == "Done. Q1?"
    assert [(c.id, c.name, c.input) for c in r.tool_calls] == [("tu1", "Bash", {"command": "ls"}), ("tu2", "Read", {"file_path": "x"})]
    assert isinstance(r.tool_calls[0], ToolCall)
    assert r.tool_results["tu1"].content == "a.py\nb.py" and not r.tool_results["tu1"].is_error
    assert r.tool_results["tu2"].is_error and r.tool_results["tu2"].text() == "x"
    assert r.num_turns == 3 and r.session_id == "sess-1" and r.duration_s == 1.5
    assert not r.is_error and r.subtype == "success" and not r.compacted and not r.budget_exceeded


def test_accumulator_final_text_falls_back_to_last_text(sdk_sess):
    acc = _TurnAccumulator()
    _feed(acc, sdk_sess, [AssistantMessage(content=[TextBlock("one")], model="m"), AssistantMessage(content=[TextBlock("two")], model="m"), _rm(0.01, result=None)])
    r = acc.to_result(sdk_sess, 0)
    assert r.final_text == "two" and r.reply_text == "two"


def test_cost_delta_counts_only_the_increase_of_cumulative_total(sdk_sess):
    deltas = []
    for total in (0.10, 0.25, 0.25, 0.70):
        acc = _TurnAccumulator()
        _feed(acc, sdk_sess, [AssistantMessage(content=[TextBlock("x")], model="m"), _rm(total)])
        r = acc.to_result(sdk_sess, 0)
        deltas.append(r.cost_delta_usd)
        assert r.session_cost_usd == pytest.approx(total)
    assert deltas == pytest.approx([0.10, 0.15, 0.0, 0.45])
    assert sum(deltas) == pytest.approx(sdk_sess.total_cost_usd)
    assert sdk_sess.total_cost_usd == pytest.approx(0.70)


def test_cost_delta_zero_when_result_has_no_cost(sdk_sess):
    sdk_sess.total_cost_usd = 0.4
    acc = _TurnAccumulator()
    _feed(acc, sdk_sess, [_rm(None)])
    r = acc.to_result(sdk_sess, 0)
    assert r.cost_delta_usd == 0.0 and r.session_cost_usd == pytest.approx(0.4)


def test_cost_delta_after_running_total_reset_is_not_double_counted(sdk_sess):
    deltas = []
    for total in (2.0, 0.05, 0.12):  # reset between the 1st and 2nd result
        acc = _TurnAccumulator()
        _feed(acc, sdk_sess, [_rm(total)])
        deltas.append(acc.to_result(sdk_sess, 0).cost_delta_usd)
    assert deltas == pytest.approx([2.0, 0.05, 0.07])


@pytest.mark.parametrize("origin", [{"kind": "task_notification"}, {"kind": "background"}, type("O", (), {"kind": "scheduled"})()])
def test_injected_result_is_skipped_and_ours_is_used(sdk_sess, origin):
    acc = _TurnAccumulator()
    injected = _rm(0.30, result="background task finished", origin=origin)
    ours = _rm(0.40, result="our answer", origin={"kind": "human"})
    assert acc.add(AssistantMessage(content=[TextBlock("working")], model="m"), sdk_sess) is False
    assert acc.add(injected, sdk_sess) is False
    assert acc.result_msg is None
    assert acc.add(ours, sdk_sess) is True
    r = acc.to_result(sdk_sess, 0)
    assert r.final_text == "our answer"
    assert r.cost_delta_usd == pytest.approx(0.40)  # cumulative: includes the injected turn's spend once


def test_result_with_no_origin_is_ours(sdk_sess):
    acc = _TurnAccumulator()
    assert acc.add(_rm(0.01, origin=None), sdk_sess) is True


def test_compact_boundary_sets_compacted(sdk_sess):
    acc = _TurnAccumulator()
    _feed(acc, sdk_sess, [SystemMessage("compact_boundary", {"compact_metadata": {"trigger": "auto", "pre_tokens": 150000}}), SystemMessage("status", {"status": "ok"}), _rm(0.01)])
    assert acc.to_result(sdk_sess, 0).compacted


def test_compaction_pending_on_session_also_marks_compacted(sdk_sess):
    sdk_sess.compaction_pending = True
    acc = _TurnAccumulator()
    _feed(acc, sdk_sess, [_rm(0.01)])
    assert acc.to_result(sdk_sess, 0).compacted


def test_error_results_and_budget_exceeded(sdk_sess):
    acc = _TurnAccumulator()
    _feed(acc, sdk_sess, [_rm(1.99, subtype="error_max_budget_usd", is_error=True, errors=["budget"], result=None)])
    r = acc.to_result(sdk_sess, 0)
    assert r.is_error and r.budget_exceeded and r.subtype == "error_max_budget_usd" and r.errors == ["budget"]
    # API failure trap: subtype success but is_error true
    acc = _TurnAccumulator()
    _feed(acc, sdk_sess, [_rm(2.0, subtype="success", is_error=True, result="API Error: 529 overloaded")])
    r = acc.to_result(sdk_sess, 0)
    assert r.is_error and not r.budget_exceeded and r.final_text.startswith("API Error")


def test_incomplete_turn_without_result(sdk_sess):
    sdk_sess.total_cost_usd = 0.2
    acc = _TurnAccumulator()
    _feed(acc, sdk_sess, [AssistantMessage(content=[TextBlock("partial")], model="m")])
    r = acc.to_result(sdk_sess, 0)
    assert r.is_error and r.subtype == "incomplete" and r.errors == ["no result message"]
    assert r.cost_delta_usd == 0.0 and r.final_text == "partial"


# ======================================================================================
# SDKWorkerSession.send against a stub client (no CLI, no API)
# ======================================================================================


class StubClient:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.queries: list[str] = []
        self.interrupts = 0

    async def query(self, text):
        self.queries.append(text)

    def receive_messages(self):
        script = self.scripts.pop(0)

        async def gen():
            if script == "hang":
                await asyncio.sleep(3600)
            for m in script:
                yield m

        return gen()

    async def interrupt(self):
        self.interrupts += 1

    async def disconnect(self):
        pass

    async def get_context_usage(self):
        return {"percentage": 37.5}


def test_sdk_session_send_accumulates_cost_and_records_transcript(sdk_sess):
    client = StubClient(
        [
            [SystemMessage("init", {"session_id": "abc", "apiKeySource": "none", "model": "m"}), AssistantMessage(content=[TextBlock("hi"), ToolUseBlock("t1", "Read", {"file_path": "x"})], model="m"), UserMessage(content=[ToolResultBlock("t1", "content", False)]), _rm(0.12, result="hi")],
            [_rm(0.30, result="next", origin={"kind": "task_notification"}), _rm(0.42, result="ours")],
        ]
    )
    sdk_sess._client = client

    async def go():
        r1 = await sdk_sess.send("first", 5)
        r2 = await sdk_sess.send("second", 5)
        return r1, r2, await sdk_sess.context_pct()

    r1, r2, pct = asyncio.run(go())
    assert client.queries == ["first", "second"]
    assert r1.cost_delta_usd == pytest.approx(0.12) and r2.cost_delta_usd == pytest.approx(0.30)
    assert r2.final_text == "ours"
    assert sdk_sess.total_cost_usd == pytest.approx(0.42) and sdk_sess.turns == 2
    assert sdk_sess.session_id == "abc" and sdk_sess.api_key_source == "none"
    assert pct == 37.5
    kinds = [__import__("json").loads(line)["kind"] for line in sdk_sess.tw.path.read_text().splitlines()]
    assert kinds[:2] == ["user", "system"] and "tool_use" in kinds and "tool_result" in kinds and kinds.count("result") == 3
    assert "session_init" in [e["event"] for e in sdk_sess.log.read()]


def test_sdk_session_timeout_interrupts_and_drains(sdk_sess):
    client = StubClient(["hang", [_rm(0.05, subtype="error_during_execution", is_error=True, result=None)]])
    sdk_sess._client = client
    r = asyncio.run(sdk_sess.send("slow", timeout_s=0.05))
    assert client.interrupts == 1
    assert r.timed_out and r.interrupted and r.is_error and r.subtype == "timeout"
    assert r.cost_delta_usd == pytest.approx(0.05)
    assert not sdk_sess.broken
    assert "turn_timeout" in [e["event"] for e in sdk_sess.log.read()]


def test_sdk_session_send_before_start_raises(sdk_sess):
    with pytest.raises(SessionError):
        asyncio.run(sdk_sess.send("x", 1))


def test_sdk_session_precompact_hook_flags_reset(sdk_sess):
    out = asyncio.run(sdk_sess._on_precompact({"trigger": "auto"}, None, None))
    assert out == {} and sdk_sess.compaction_pending
    assert sdk_sess.log.read()[-1]["event"] == "precompact"


def test_sdk_session_options(tmp_path):
    gate = _gate(tmp_path)
    log = EventLog(tmp_path / "run.jsonl")
    spec = _spec(tmp_path, gate, read_only=True, env={"GIT_BRANCH_NAME": "feature/1-x"}, model="claude-sonnet-5", effort="medium")
    spec.budget_usd = 0.0
    s = SDKWorkerSession(spec, log, tmp_path / "t", cli_path="/opt/claude")
    o = s._options()
    assert o.cwd == str(tmp_path)
    assert o.system_prompt == {"type": "preset", "preset": "claude_code", "append": "APPEND"}
    assert o.max_budget_usd == 0.05  # floor
    assert o.env == {"GIT_BRANCH_NAME": "feature/1-x", "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"}
    assert set(o.disallowed_tools) >= {"Monitor", "PowerShell", "RemoteTrigger", "CronCreate"}
    assert o.hooks["PreToolUse"][0].on_failure == "block"
    assert set(o.disallowed_tools) >= {"Edit", "Write", "Bash"}
    assert o.model == "claude-sonnet-5" and o.effort == "medium" and str(o.cli_path) == "/opt/claude"
    assert o.can_use_tool == gate.can_use_tool
    assert set(o.hooks) == {"PreToolUse", "PreCompact"}
    assert o.permission_mode == "acceptEdits" and o.setting_sources == ["user", "project", "local"]
    assert o.sandbox is None  # no sandbox unless the spec asks for one


# ======================================================================================
# OS-level Bash sandbox (sandbox_settings / sandbox_env / SessionSpec.sandbox)
# ======================================================================================


def test_sdk_session_options_pass_the_sandbox_and_its_env(tmp_path):
    sb = sandbox_settings("implement", "on")
    spec = _spec(tmp_path, _gate(tmp_path), sandbox=sb, env={"GIT_BRANCH_NAME": "feature/1-x"})
    o = SDKWorkerSession(spec, EventLog(tmp_path / "run.jsonl"), tmp_path / "t")._options()
    assert o.sandbox == sb
    assert o.env["npm_config_store_dir"] == pnpm_store_dir() and o.env["GIT_BRANCH_NAME"] == "feature/1-x"
    # the SDK merges it into --settings for the CLI (no process is started here)
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    settings = __import__("json").loads(SubprocessCLITransport("x", o)._build_settings_value())
    assert settings["sandbox"]["enabled"] is True and settings["sandbox"]["allowUnsandboxedCommands"] is False


def test_spec_env_overrides_sandbox_env(tmp_path):
    spec = _spec(tmp_path, _gate(tmp_path), sandbox=sandbox_settings("implement", "on"), env={"npm_config_store_dir": "/custom/store"})
    o = SDKWorkerSession(spec, EventLog(tmp_path / "run.jsonl"), tmp_path / "t")._options()
    assert o.env["npm_config_store_dir"] == "/custom/store"


def test_sandbox_settings_off_and_exempt_phases():
    assert sandbox_settings("implement", "off") is None
    for phase in ("feature_start", "ship", "linear", "halt_notice", "linear:read"):
        assert sandbox_settings(phase, "on") is None, phase
    with pytest.raises(ValueError):
        sandbox_settings("implement", "maybe")


def test_sandbox_settings_toolchain_phases():
    for phase in ("implement", "review"):
        sb = sandbox_settings(phase, "on")
        assert sb["enabled"] is True and sb["autoAllowBashIfSandboxed"] is False and sb["allowUnsandboxedCommands"] is False
        assert set(DEFAULT_SANDBOX_DOMAINS) <= set(sb["network"]["allowedDomains"])
        assert sb["network"]["allowLocalBinding"] is True
        w = sb["filesystem"]["allowWrite"]
        assert "/tmp" in w and os.path.normpath(pnpm_store_dir()) in w and os.path.expanduser("~/.npm") in w
        assert os.path.expanduser("~") not in w  # never the home directory itself
        assert os.path.expanduser("~/.ssh") in sb["filesystem"]["denyRead"]


def test_sandbox_settings_docs_phases_get_no_network_ports_or_caches():
    for phase in ("clarify", "plan", "tasks", "analyze"):
        sb = sandbox_settings(phase, "on")
        assert sb["network"] == {"allowedDomains": [], "allowLocalBinding": False}
        assert os.path.normpath(pnpm_store_dir()) not in sb["filesystem"]["allowWrite"]


def test_sandbox_settings_extra_domains_are_added_once():
    sb = sandbox_settings("analyze", "on", ["api.github.com", " ", "api.github.com"])
    assert sb["network"]["allowedDomains"] == ["api.github.com"]
    sb = sandbox_settings("implement", "on", ["registry.npmjs.org", "binaries.prisma.sh"])
    assert sb["network"]["allowedDomains"].count("registry.npmjs.org") == 1 and "binaries.prisma.sh" in sb["network"]["allowedDomains"]


def test_sandbox_settings_auto_follows_availability(monkeypatch):
    import speckit_autopilot.sessions as S

    monkeypatch.setattr(S, "sandbox_available", lambda: (False, "nope"))
    assert sandbox_settings("implement", "auto") is None
    assert sandbox_settings("implement", "on") is not None  # "on" is explicit
    monkeypatch.setattr(S, "sandbox_available", lambda: (True, "yes"))
    assert sandbox_settings("implement", "AUTO")["enabled"] is True


def test_sandbox_available_by_platform(monkeypatch):
    import shutil
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/" + b if b == "bwrap" else None)
    ok, why = sandbox_available()
    assert not ok and "socat" in why
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/" + b)
    assert sandbox_available()[0]
    monkeypatch.setattr(sys, "platform", "win32")
    assert sandbox_available() == (False, "no Bash sandbox on win32")


def test_pnpm_store_dir_and_sandbox_env(monkeypatch):
    monkeypatch.setenv("npm_config_store_dir", "/x/store")
    assert pnpm_store_dir() == "/x/store"
    monkeypatch.delenv("npm_config_store_dir")
    monkeypatch.delenv("NPM_CONFIG_STORE_DIR", raising=False)
    monkeypatch.setenv("PNPM_HOME", "/p/home")
    assert pnpm_store_dir() == "/p/home/store"
    assert sandbox_env(None) == {} and sandbox_env({"enabled": False}) == {}
    from speckit_autopilot import repo as R

    monkeypatch.setattr(R, "sandbox_shim_dir", lambda base=None: None)  # no setuid shims (non-macOS)
    assert sandbox_env({"enabled": True}) == {"npm_config_store_dir": "/p/home/store"}
    monkeypatch.setattr(R, "sandbox_shim_dir", lambda base=None: "/shims")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert sandbox_env({"enabled": True}) == {"npm_config_store_dir": "/p/home/store", "PATH": "/shims:/usr/bin:/bin"}


# ======================================================================================
# Runner budget accounting + limits
# ======================================================================================


@pytest.fixture
def runner(tmp_path):
    world = World(tmp_path)
    r, _judge, _js, _tr = make_runner(world)
    r.state = RunState(run_id="r", issue="ENG-142", description="d", repo=str(world.repo))
    return r


def test_check_limits_passes_under_budget(runner):
    runner.current_phase = "plan"
    runner.state.total_cost_usd = 39.99
    runner.state.phase("plan").cost_usd = 4.99
    runner._check_limits()


def test_no_limits_by_default(runner):
    runner.current_phase = "implement"
    runner.state.total_cost_usd = 10_000.0
    runner.state.phase("implement").cost_usd = 10_000.0
    runner._elapsed_base = 1000 * 3600.0
    runner._check_limits()  # no budget, no phase cap, no wall clock unless a config sets one
    assert runner._session_budget(("implement",)) is None


def test_check_limits_total_budget(runner):
    runner.cfg.total_budget_usd = 40.0
    runner.state.total_cost_usd = 40.0
    with pytest.raises(Halt) as ei:
        runner._check_limits()
    assert "total budget exhausted" in ei.value.reason and "$40.00" in ei.value.reason


def test_check_limits_phase_budget(runner):
    runner.cfg.phase_budget_usd = {"plan": 5.0}
    runner.current_phase = "plan"
    runner.state.phase("plan").cost_usd = 5.0
    with pytest.raises(Halt) as ei:
        runner._check_limits()
    assert "phase budget for plan exhausted" in ei.value.reason and ei.value.phase == "plan"


def test_check_limits_phase_budget_from_config(runner):
    runner.cfg.phase_budget_usd = {**runner.cfg.phase_budget_usd, "implement": 0.5}
    runner.current_phase = "implement"
    runner.state.phase("implement").cost_usd = 0.49
    runner._check_limits()
    runner.state.phase("implement").cost_usd = 0.5
    with pytest.raises(Halt):
        runner._check_limits()


def test_check_limits_ignores_other_phases_spend(runner):
    runner.current_phase = "clarify"
    runner.state.phase("plan").cost_usd = 99.0
    runner._check_limits()  # only total and the current phase count; total is still 0


def test_check_limits_wall_clock_includes_previous_runs(runner):
    runner.cfg.max_wallclock_h = 1.0
    runner._elapsed_base = 3599.0
    runner._check_limits()
    runner._elapsed_base = 3600.0
    with pytest.raises(Halt) as ei:
        runner._check_limits()
    assert "wall-clock limit reached" in ei.value.reason


def test_account_adds_deltas_to_total_and_base_phase(runner):
    runner._account(TurnResult(cost_delta_usd=0.25), "analyze")
    runner._account(TurnResult(cost_delta_usd=0.10), "analyze:verify")
    from speckit_autopilot.judge import JudgeResult

    runner._account_judge(JudgeResult("verify", {}, cost_usd=0.05), "analyze:verify")
    assert runner.state.total_cost_usd == pytest.approx(0.40)
    assert runner.state.phase("analyze").cost_usd == pytest.approx(0.40)
    assert "analyze:verify" not in runner.state.phases


def test_accounting_then_limit_halts(runner):
    runner.cfg.total_budget_usd = 1.0
    runner.current_phase = "clarify"
    for _ in range(3):
        runner._account(TurnResult(cost_delta_usd=0.3), "clarify")
        runner._check_limits()
    runner._account(TurnResult(cost_delta_usd=0.3), "clarify")
    with pytest.raises(Halt):
        runner._check_limits()
