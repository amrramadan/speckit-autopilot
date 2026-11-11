"""Tests for fixes made after the live preflight (Linear server selection, plugin dirs, env, CLI --abandon...)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from speckit_autopilot.config import Config
from speckit_autopilot.linear import parse_snapshot
from speckit_autopilot.repo import detect_profile
from speckit_autopilot.sessions import ToolCall, ToolResult, TurnResult
from speckit_autopilot.state import RunPaths, RunState, StateStore

from .fakeworld import World, make_runner


def test_session_env_disables_claudeai_connectors_unless_they_are_the_linear_server():
    c = Config()
    c.linear_server = "plugin:design:linear"
    assert c.session_env() == {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
    c.linear_server = "claude.ai Linear"
    assert c.session_env() == {}
    assert c.linear_tool_prefix == "mcp__claude_ai_Linear__"
    c.claudeai_connectors = False
    assert c.session_env() == {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}


def test_plugin_dirs_glob_only_directories(tmp_path, monkeypatch):
    (tmp_path / "synced" / "abc" / "design~g2").mkdir(parents=True)
    (tmp_path / "synced" / "abc" / "design~g2.meta.json").write_text("{}")
    c = Config()
    c.plugin_dirs = [str(tmp_path / "synced" / "*" / "design~*")]
    assert c.resolved_plugin_dirs() == [str(tmp_path / "synced" / "abc" / "design~g2")]


def test_reader_ignores_other_linear_servers():
    turn = TurnResult(
        tool_calls=[ToolCall("1", "mcp__claude_ai_Linear__get_issue", {}), ToolCall("2", "mcp__plugin_design_linear__get_workspace", {})],
        tool_results={"1": ToolResult("1", json.dumps({"id": "ENG-1", "title": "wrong ws"})), "2": ToolResult("2", json.dumps({"url": "https://linear.app/acme1"}))},
    )
    snap = parse_snapshot(turn, "mcp__plugin_design_linear__")
    assert snap.issue is None and snap.workspace_url == "https://linear.app/acme1"
    assert any("ignored mcp__claude_ai_Linear__get_issue" in e for e in snap.errors)


def test_empty_tooling_command_disables_detected_one(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest", "build": "vite build"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    prof = detect_profile(tmp_path, None, {"test": None, "build": ""})
    assert prof.tooling["test"] == "pnpm test" and prof.tooling["build"] is None


def test_lettered_spec_gets_numeric_specify_feature_standin(tmp_path):
    w = World(tmp_path)
    r, *_ = make_runner(w)
    r.state = RunState(run_id="x", issue="ENG-1", description="d", repo=str(w.repo))
    r.state.feature_branch = "feature/023b-dom-rule-editor"
    r.state.feature_dir = str(w.repo / "specs" / "023b-dom-rule-editor")
    env = r._worker_env()
    assert env["SPECIFY_FEATURE"] == "023-dom-rule-editor"
    assert env["SPECIFY_FEATURE_DIRECTORY"] == "specs/023b-dom-rule-editor"
    assert env["GIT_BRANCH_NAME"] == "feature/023b-dom-rule-editor"


def test_runner_sessions_get_plugin_dirs_env_and_linear_wait(tmp_path):
    w = World(tmp_path)
    plug = tmp_path / "plugins" / "design~g2"
    plug.mkdir(parents=True)
    r, *_ = make_runner(w, plugin_dirs=[str(tmp_path / "plugins" / "design~*")], linear_server="plugin:design:linear")
    r.state = RunState(run_id="x", issue="ENG-1", description="d", repo=str(w.repo))
    from speckit_autopilot.policy import PolicyContext, PolicyGate

    r.gate = PolicyGate(PolicyContext(phase="implement", repo=w.repo))
    spec = r._spec("implement", "lbl")
    assert spec.plugin_dirs == (str(plug),)
    assert spec.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert spec.wait_mcp == ()
    assert r._spec("linear", "lbl2").wait_mcp == ("plugin:design:linear",)


def test_cli_abandon_archives_state(tmp_path, capsys):
    from speckit_autopilot.__main__ import main

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    paths = RunPaths(repo)
    paths.ensure()
    st = RunState(run_id="r1", issue="ENG-9", description="d", repo=str(repo), status="halted")
    StateStore(paths.state).save(st)
    paths.events.write_text("{}\n")
    assert main(["--repo", str(repo), "--abandon"]) == 0
    assert not paths.state.exists()
    assert (paths.root / "archive" / "r1-abandoned" / "state.json").exists()
    assert "Archived run r1" in capsys.readouterr().out
