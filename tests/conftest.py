"""Keep every test away from the real user config and run registry."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_user_dirs(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("user")
    monkeypatch.setenv("SPECKIT_AUTOPILOT_CONFIG", str(home / "config.toml"))
    monkeypatch.setenv("SPECKIT_AUTOPILOT_STATE", str(home / "state"))
    monkeypatch.delenv("SPECKIT_AUTOPILOT_ROOTS", raising=False)
    # Linear auto-detection reads Claude Code's MCP config: never the developer's real ~/.claude.json
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "claude"))
