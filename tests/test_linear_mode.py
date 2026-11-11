"""linear = auto | on | off: resolution, policy, admin sub-command, and a full run without Linear."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from speckit_autopilot import admin
from speckit_autopilot.config import PHASES, Config, ConfigError, linear_server_configured, load_config, looks_like_issue_key, resolve_linear
from speckit_autopilot.policy import PolicyContext, evaluate
from speckit_autopilot.state import RunPaths, StateStore

from .fakeworld import World, make_runner


# --------------------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("key,ok", [("ENG-142", True), ("APP2-7", True), ("eng-142", False), ("product-filters", False), ("ENG142", False), ("", False)])
def test_issue_key_shape(key, ok):
    assert looks_like_issue_key(key) is ok


def test_explicit_modes_win(tmp_path):
    assert resolve_linear(Config(repo=tmp_path, issue="x", linear="on"))[0] is True
    assert resolve_linear(Config(repo=tmp_path, issue="ENG-1", linear="off"))[0] is False


def test_auto_is_off_without_a_key_even_with_a_server(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"linear": {}}}))
    on, why = resolve_linear(Config(repo=tmp_path, issue="product-filters"), home=tmp_path)
    assert not on and "not a tracker key" in why
    assert resolve_linear(Config(repo=tmp_path, issue=""), home=tmp_path) == (False, "auto: no --issue given")


def test_auto_is_off_with_a_key_but_no_server(tmp_path):
    on, why = resolve_linear(Config(repo=tmp_path, issue="ENG-1"), home=tmp_path)
    assert not on and "no MCP server named 'linear'" in why


@pytest.mark.parametrize("where", ["repo .mcp.json", "user .claude.json", "project table", "settings.json"])
def test_auto_finds_the_server_where_claude_code_declares_it(tmp_path, where):
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir(), home.mkdir()
    (home / ".claude").mkdir()
    server = {"mcpServers": {"linear": {"type": "http"}}}
    if where == "repo .mcp.json":
        (repo / ".mcp.json").write_text(json.dumps(server))
    elif where == "user .claude.json":
        (home / ".claude.json").write_text(json.dumps(server))
    elif where == "project table":
        (home / ".claude.json").write_text(json.dumps({"projects": {str(repo): server}}))
    else:
        (home / ".claude" / "settings.json").write_text(json.dumps(server))
    assert resolve_linear(Config(repo=repo, issue="ENG-1"), home=home)[0] is True


def test_auto_ignores_another_projects_server(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"projects": {"/elsewhere": {"mcpServers": {"linear": {}}}}}))
    assert resolve_linear(Config(repo=tmp_path / "repo", issue="ENG-1"), home=tmp_path)[0] is False


def test_auto_reads_claude_config_dir(tmp_path, monkeypatch):
    d = tmp_path / "cfgdir"
    d.mkdir()
    (d / ".claude.json").write_text(json.dumps({"mcpServers": {"linear": {}}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    assert linear_server_configured(Config(repo=tmp_path, issue="ENG-1"))[0] is True


def test_auto_trusts_plugin_servers_and_a_pinned_workspace(tmp_path):
    assert resolve_linear(Config(repo=tmp_path, issue="ENG-1", linear_server="plugin:design:linear"), home=tmp_path)[0] is True
    on, why = resolve_linear(Config(repo=tmp_path, issue="ENG-1", linear_workspace_url="https://linear.app/acme"), home=tmp_path)
    assert on and "pinned" in why


def test_unreadable_mcp_files_count_as_no_server(tmp_path):
    (tmp_path / ".mcp.json").write_text("{not json")
    (tmp_path / ".claude.json").write_text("[]")
    assert resolve_linear(Config(repo=tmp_path, issue="ENG-1"), home=tmp_path)[0] is False


def test_linear_mode_is_validated(tmp_path):
    (tmp_path / ".autopilot.toml").write_text('linear = "sometimes"\n')
    with pytest.raises(ConfigError, match="linear must be one of"):
        load_config(tmp_path)


# --------------------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["mcp__linear__get_issue", "mcp__linear__save_comment", "mcp__claude_ai_Linear__list_issues"])
def test_policy_denies_every_linear_tool_when_off(tmp_path, tool):
    ctx = PolicyContext(phase="linear", repo=tmp_path, linear_enabled=False)
    d = evaluate(tool, {}, ctx)
    assert not d.allow and d.rule == "linear-off"


def test_policy_still_allows_linear_reads_when_on(tmp_path):
    assert evaluate("mcp__linear__get_issue", {}, PolicyContext(phase="plan", repo=tmp_path)).allow


# --------------------------------------------------------------------------------------
# admin: speckit-autopilot linear [auto|on|off]
# --------------------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    import subprocess

    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


def test_admin_sets_and_shows_linear(git_repo, capsys):
    assert admin.main(["linear", "off", "--repo", str(git_repo)]) == 0
    assert 'linear = "off"' in (git_repo / ".autopilot.toml").read_text()
    assert admin.main(["linear", "--repo", str(git_repo)]) == 0
    assert "linear = off" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="auto, on or off"):
        admin.main(["linear", "maybe", "--repo", str(git_repo)])


def test_admin_explains_auto(git_repo, capsys):
    admin.main(["linear", "auto", "--repo", str(git_repo)])
    capsys.readouterr()
    admin.main(["linear", "--repo", str(git_repo)])
    out = capsys.readouterr().out
    assert "linear = auto" in out and "with an issue key" in out and "no MCP server named 'linear'" in out


@pytest.mark.parametrize("has_server,mode", [(False, "off"), (True, "on")])
def test_init_writes_linear_explicitly(git_repo, capsys, has_server, mode):
    if has_server:
        (git_repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"linear": {}}}))
    assert admin.main(["init", "--repo", str(git_repo)]) == 0
    assert load_config(git_repo).linear == mode
    assert f"linear         {mode}" in capsys.readouterr().out


# --------------------------------------------------------------------------------------
# a full run with Linear off
# --------------------------------------------------------------------------------------


def test_run_without_linear_ships_and_skips_the_closeout(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, linear="off", issue="product-filters", linear_workspace_url=None)
    out = asyncio.run(runner.run())
    assert out.status == "completed", out.reason
    assert out.reason == "merged"
    st = StateStore(RunPaths(w.repo).state).load()
    assert {p: st.phase(p).status for p in PHASES} == {**{p: "passed" for p in PHASES[:-1]}, "linear": "skipped"}
    assert st.flags["linear"] == {"enabled": False, "mode": "off", "why": "linear = off"}
    assert w.merges == 1
    assert w.linear.writes == [] and not st.linear.get("read")
    assert not [s for s in w.sessions if s.spec.phase in ("linear", "linear_read", "halt_notice")]
    events = [json.loads(line) for line in RunPaths(w.repo).events.read_text().splitlines()]
    assert any(e.get("event") == "phase_end" and e.get("phase") == "linear" and e.get("status") == "skipped" for e in events)
    assert "- Linear: off (linear = off)" in RunPaths(w.repo).report.read_text()


def test_a_halt_without_linear_posts_no_notice(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, linear="off", issue="product-filters", linear_workspace_url=None, until="ship")
    out = asyncio.run(runner.run())
    assert out.status == "paused"
    assert w.linear.writes == []
    runner2, *_ = make_runner(w, linear="auto", issue="product-filters", linear_workspace_url=None, resume=True, until="none")
    out2 = asyncio.run(runner2.run())
    assert out2.status == "completed", out2.reason
    st = StateStore(RunPaths(w.repo).state).load()
    assert st.phase("linear").status == "skipped" and w.linear.writes == []
