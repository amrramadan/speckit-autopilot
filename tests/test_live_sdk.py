"""OPT-IN live checks of the SDK facts the supervisor relies on (spec §1 "verify empirically").

Skipped unless AUTOPILOT_LIVE=1. Uses the cheapest model and a throwaway temp repo; costs a
few cents on the current Claude Code login. Run:

    AUTOPILOT_LIVE=1 .venv/bin/python -m pytest tests/test_live_sdk.py -q -s
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from speckit_autopilot.policy import PolicyContext, PolicyGate, map_answers
from speckit_autopilot.sessions import SDKWorkerSession, SessionSpec, scrub_parent_session_env
from speckit_autopilot.state import EventLog

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("AUTOPILOT_LIVE") != "1", reason="live SDK test (set AUTOPILOT_LIVE=1)")]

MODEL = os.environ.get("AUTOPILOT_LIVE_MODEL", "claude-haiku-5-5")


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    (r / "README.md").write_text("live test\n")
    return r


def _session(tmp_path: Path, repo: Path, gate: PolicyGate, label: str) -> SDKWorkerSession:
    spec = SessionSpec(
        label=label,
        phase="implement",
        cwd=repo,
        gate=gate,
        system_append="You are a terse test assistant. Follow instructions exactly.",
        budget_usd=0.5,
        model=MODEL,
        setting_sources=(),  # isolate from user/project settings
    )
    return SDKWorkerSession(spec, EventLog(tmp_path / "run.jsonl", "live"), tmp_path / "transcripts")


def test_cost_is_cumulative_and_context_usage(tmp_path):
    scrub_parent_session_env()
    repo = _repo(tmp_path)
    gate = PolicyGate(PolicyContext(phase="implement", repo=repo))

    async def go():
        s = _session(tmp_path, repo, gate, "cost")
        await s.start()
        try:
            t1 = await s.send("Reply with exactly: one", timeout_s=180)
            pct = await s.context_pct()
            t2 = await s.send("Reply with exactly: two", timeout_s=180)
            return t1, t2, pct, s.total_cost_usd
        finally:
            await s.close()

    t1, t2, pct, total = asyncio.run(go())
    print(f"\nturn1 delta={t1.cost_delta_usd:.6f} session_total={t1.session_cost_usd:.6f}")
    print(f"turn2 delta={t2.cost_delta_usd:.6f} session_total={t2.session_cost_usd:.6f} context_pct={pct}")
    assert not t1.is_error and not t2.is_error
    assert "one" in t1.reply_text.lower() and "two" in t2.reply_text.lower()
    # cumulative: the second ResultMessage carries the running total, so total == sum of deltas
    assert t2.session_cost_usd >= t1.session_cost_usd > 0
    assert abs((t1.cost_delta_usd + t2.cost_delta_usd) - total) < 1e-9
    assert pct is not None and 0 < pct < 100


def test_ask_user_question_answer_shape(tmp_path):
    scrub_parent_session_env()
    repo = _repo(tmp_path)
    asked: list[dict] = []

    async def handler(tool_input):
        asked.append(tool_input)
        qs = tool_input.get("questions", [])
        answers = map_answers(qs, {q["question"]: q["options"][-1]["label"] for q in qs})
        return {**tool_input, "answers": answers}

    gate = PolicyGate(PolicyContext(phase="clarify", repo=repo), ask_handler=handler)

    async def go():
        s = _session(tmp_path, repo, gate, "ask")
        s.spec.allowed_tools = ("Read",)
        await s.start()
        try:
            return await s.send(
                "Use the AskUserQuestion tool exactly once to ask me 'Which color do you prefer?' with the two options "
                "'Red' and 'Blue'. After you get the answer, reply with only the chosen color word.",
                timeout_s=240,
            )
        finally:
            await s.close()

    t = asyncio.run(go())
    print("\nreply:", t.reply_text, "| tools:", [c.name for c in t.tool_calls])
    assert asked, "AskUserQuestion never reached can_use_tool"
    assert "blue" in t.reply_text.lower()


def test_pre_tool_use_hook_denies_push(tmp_path):
    scrub_parent_session_env()
    repo = _repo(tmp_path)
    events: list[tuple[str, dict]] = []
    gate = PolicyGate(PolicyContext(phase="implement", repo=repo), log_event=lambda e, **k: events.append((e, k)))

    async def go():
        s = _session(tmp_path, repo, gate, "deny")
        await s.start()
        try:
            return await s.send("Run this exact Bash command once: git push origin main --dry-run   Then reply with the word FINISHED.", timeout_s=240)
        finally:
            await s.close()

    t = asyncio.run(go())
    denials = [k for e, k in events if e == "denial"]
    print("\nreply:", t.reply_text[:200], "| denials:", denials)
    assert denials and denials[0]["rule"] == "push-phase"
    assert any("AUTOPILOT POLICY" in r.text() for r in t.tool_results.values()), "deny reason did not reach the model"


def test_background_agent_result_is_awaited(tmp_path):
    """cc-review-style: reviewer AGENTS run in the background and their report arrives in a later, CLI-injected turn.
    The turn must only complete when the CLI is idle again (session_state_changed), so the report is captured."""
    scrub_parent_session_env()
    repo = _repo(tmp_path)
    gate = PolicyGate(PolicyContext(phase="review", repo=repo))

    async def go():
        s = _session(tmp_path, repo, gate, "bg-agent")
        await s.start()
        try:
            return await s.send(
                "Use the Agent tool with run_in_background=true and a general-purpose subagent whose only job is to reply "
                "with exactly the text AGENT-DONE-4711. Do not wait for it: end your turn right away by replying STARTED. "
                "When you are notified that the agent finished, reply with the agent's result text.",
                timeout_s=300,
            ), s
        finally:
            await s.close()

    t, s = asyncio.run(go())
    print("\ntext:", t.text[-300:], "| seen_state:", s.seen_state, "| cli_state:", s.cli_state, "| active:", s.active_tasks)
    assert "AGENT-DONE-4711" in (t.text + t.final_text), "the background agent's result never reached the turn"


def test_background_shell_does_not_hold_the_turn(tmp_path):
    """A background shell (it may never finish, e.g. a dev server) must not keep the turn open."""
    scrub_parent_session_env()
    repo = _repo(tmp_path)
    gate = PolicyGate(PolicyContext(phase="implement", repo=repo))

    async def go():
        s = _session(tmp_path, repo, gate, "bg-shell")
        await s.start()
        try:
            return await s.send(
                "Use the Bash tool with run_in_background=true to run exactly: sleep 120 . Do not wait for it: reply STARTED.",
                timeout_s=100,
            )
        finally:
            await s.close()

    t = asyncio.run(go())
    assert not t.timed_out and "STARTED" in t.reply_text


def test_slash_prompt_runs_user_only_skill(tmp_path):
    """cc-*/speckit-* skills are disable-model-invocation: true; a prompt starting with /name must still run them."""
    scrub_parent_session_env()
    repo = _repo(tmp_path)
    d = repo / ".claude" / "skills" / "echo-probe"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: echo-probe\ndescription: test probe\nuser-invocable: true\ndisable-model-invocation: true\n---\n"
        "Reply with exactly the text SKILL-RAN followed by a dash and the arguments you were given ($ARGUMENTS), nothing else.\n"
    )
    gate = PolicyGate(PolicyContext(phase="implement", repo=repo))

    async def go():
        s = _session(tmp_path, repo, gate, "slash")
        s.spec.setting_sources = ("project",)
        await s.start()
        try:
            info = await s.server_info()
            t = await s.send("/echo-probe abc123", timeout_s=180)
            return info, t
        finally:
            await s.close()

    info, t = asyncio.run(go())
    names = [c.get("name") if isinstance(c, dict) else c for c in (info or {}).get("commands", [])]
    print("\ncommands include echo-probe:", "echo-probe" in names, "| reply:", t.reply_text)
    assert "echo-probe" in names
    assert "SKILL-RAN" in t.reply_text and "abc123" in t.reply_text


def test_os_sandbox_blocks_outside_writes_and_allows_repo_work(tmp_path):
    """The OS sandbox (sessions.sandbox_settings) stops what the shell policy cannot see — a python one-liner writing
    to $HOME — while repo writes and `git commit` (.git internals) still work."""
    from speckit_autopilot.sessions import sandbox_available, sandbox_settings

    ok, why = sandbox_available()
    if not ok:
        pytest.skip(f"no OS sandbox here: {why}")
    scrub_parent_session_env()
    repo = _repo(tmp_path)
    subprocess.run(["git", "config", "user.email", "live@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "live"], cwd=repo, check=True)
    outside = Path(os.path.expanduser("~")) / f"autopilot-sandbox-live-{os.getpid()}"
    outside.unlink(missing_ok=True)
    gate = PolicyGate(PolicyContext(phase="implement", repo=repo))

    async def go():
        s = _session(tmp_path, repo, gate, "sandbox")
        s.spec.sandbox = sandbox_settings("implement", "on")
        await s.start()
        try:
            return await s.send(
                "Run these two commands with the Bash tool, one call each, exactly as written, even if the first fails, "
                "then reply DONE.\n"
                f"1. python3 -c \"open('{outside}','w').write('x')\"\n"
                "2. echo sandboxed > inside.txt && git add inside.txt && git commit -q -m sandboxed && git log --oneline -1",
                timeout_s=300,
            )
        finally:
            await s.close()

    try:
        t = asyncio.run(go())
        created = outside.exists()
    finally:
        outside.unlink(missing_ok=True)
    print("\nreply:", t.reply_text[:200], "| results:", [r.text()[:120] for r in t.tool_results.values()])
    assert any("Operation not permitted" in r.text() for r in t.tool_results.values()), "the outside write was not refused"
    assert not created, "the sandbox let a Bash command write outside the repo"
    assert (repo / "inside.txt").read_text().strip() == "sandboxed"
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "sandboxed" in log, "git commit inside the sandbox did not land"
