"""Settings sub-commands (admin.py): TOML text editing, config/model/effort writes, statusline, init."""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from speckit_autopilot import admin
from speckit_autopilot.config import REPO_CONFIG_NAME, user_config_path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


@pytest.fixture
def isolated_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return d


def run(*argv: str) -> int:
    return admin.main(list(argv))


def repo_toml(repo: Path) -> dict:
    return tomllib.loads((repo / REPO_CONFIG_NAME).read_text())


# --------------------------------------------------------------------------------------
# set_key_text
# --------------------------------------------------------------------------------------


class TestSetKeyText:
    def test_insert_into_empty(self):
        assert admin.set_key_text("", "worker_model", "opus") == 'worker_model = "opus"\n'

    def test_replace_keeps_comments_and_other_keys(self):
        text = '# top comment\nworker_model = "sonnet"  # old\njudge_model = "opus"\n'
        out = admin.set_key_text(text, "worker_model", "haiku")
        assert out == '# top comment\nworker_model = "haiku"\njudge_model = "opus"\n'

    def test_insert_goes_before_first_table(self):
        text = 'a_key = 1\n\n[repos."x"]\nworker_model = "opus"\n'
        out = admin.set_key_text(text, "worker_model", "sonnet")
        data = tomllib.loads(out)
        assert data["worker_model"] == "sonnet"
        assert data["repos"]["x"]["worker_model"] == "opus"
        assert out.index('worker_model = "sonnet"') < out.index("[repos")

    def test_keys_inside_tables_are_not_touched(self):
        text = '[repos."x"]\nworker_model = "opus"\n'
        out = admin.set_key_text(text, "worker_model", None)
        assert out == text

    def test_replace_multiline_array(self):
        text = 'plugin_dirs = [\n  "a",\n  "b",\n]\nworker_model = "opus"\n'
        out = admin.set_key_text(text, "plugin_dirs", ["c"])
        assert tomllib.loads(out) == {"plugin_dirs": ["c"], "worker_model": "opus"}

    def test_remove_multiline_array(self):
        text = 'plugin_dirs = [\n  "a",\n]\nworker_model = "opus"\n'
        assert tomllib.loads(admin.set_key_text(text, "plugin_dirs", None)) == {"worker_model": "opus"}

    def test_replace_triple_quoted_string(self):
        text = 'extra = """\nline one\nline two\n"""\nkeep = 1\n'
        out = admin.set_key_text(text, "extra", "short")
        assert tomllib.loads(out) == {"extra": "short", "keep": 1}

    def test_remove_missing_key_is_noop(self):
        text = "# only a comment\n"
        assert admin.set_key_text(text, "worker_model", None) == text

    def test_key_prefix_does_not_match_longer_key(self):
        text = "worker_model_x = 1\n"
        out = admin.set_key_text(text, "worker_model", "opus")
        assert tomllib.loads(out) == {"worker_model_x": 1, "worker_model": "opus"}

    @pytest.mark.parametrize(
        "value",
        [True, 3, 2.5, "s \"q\" é", ["a", "b"], {"plan": 1.5, "implement": 2}],
    )
    def test_values_round_trip(self, value):
        assert tomllib.loads(admin.set_key_text("", "k", value))["k"] == value


class TestParseCliValue:
    def test_string_field_keeps_raw(self):
        assert admin.parse_cli_value("worker_model", "opus") == "opus"
        assert admin.parse_cli_value("worker_model", "123") == "123"

    def test_float_field_promotes_int(self):
        v = admin.parse_cli_value("context_reset_pct", "70")
        assert v == 70.0 and isinstance(v, float)

    def test_list_parses(self):
        assert admin.parse_cli_value("sandbox_network_domains", '["a.com"]') == ["a.com"]


# --------------------------------------------------------------------------------------
# config / model / effort / context-reset / sandbox
# --------------------------------------------------------------------------------------


class TestConfigCommands:
    def test_set_get_unset_repo(self, repo, capsys):
        assert run("config", "set", "worker_model", "sonnet", "--repo", str(repo)) == 0
        assert repo_toml(repo)["worker_model"] == "sonnet"
        capsys.readouterr()
        run("config", "get", "worker_model", "--repo", str(repo))
        assert capsys.readouterr().out.strip() == "sonnet"
        run("config", "unset", "worker_model", "--repo", str(repo))
        assert "worker_model" not in repo_toml(repo)

    def test_set_global_writes_user_config(self, repo):
        run("config", "set", "judge_model", "opus", "--global", "--repo", str(repo))
        assert tomllib.loads(user_config_path().read_text())["judge_model"] == "opus"
        assert not (repo / REPO_CONFIG_NAME).exists()

    def test_repo_overrides_global(self, repo, capsys):
        run("config", "set", "worker_model", "haiku", "--global", "--repo", str(repo))
        run("config", "set", "worker_model", "opus", "--repo", str(repo))
        capsys.readouterr()
        run("config", "get", "worker_model", "--repo", str(repo))
        assert capsys.readouterr().out.strip() == "opus"

    def test_invalid_value_rolls_back_existing_file(self, repo):
        cfg = repo / REPO_CONFIG_NAME
        cfg.write_text('# mine\nworker_model = "opus"\n')
        before = cfg.read_text()
        with pytest.raises(SystemExit, match="not saved"):
            run("config", "set", "context_reset_pct", "500", "--repo", str(repo))
        assert cfg.read_text() == before

    def test_invalid_value_removes_new_file(self, repo):
        with pytest.raises(SystemExit, match="not saved"):
            run("config", "set", "max_turns_per_session", "lots", "--repo", str(repo))
        assert not (repo / REPO_CONFIG_NAME).exists()

    def test_unknown_and_run_only_keys_rejected(self, repo):
        with pytest.raises(SystemExit):
            run("config", "set", "no_such_key", "1", "--repo", str(repo))
        with pytest.raises(SystemExit, match="per-run"):
            run("config", "set", "issue", "X-1", "--repo", str(repo))

    def test_init_template_loads_and_refuses_overwrite(self, repo):
        run("config", "init", "--repo", str(repo))
        assert repo_toml(repo) == {}  # everything commented out
        with pytest.raises(SystemExit, match="exists"):
            run("config", "init", "--repo", str(repo))
        assert run("config", "init", "--force", "--repo", str(repo)) == 0

    def test_show_and_keys_list_no_run_only_keys(self, repo, capsys):
        run("config", "show", "--repo", str(repo))
        out = capsys.readouterr().out
        assert "worker_model = (unset)" in out
        assert not any(line.startswith("issue ") for line in out.splitlines())
        run("config", "keys")
        assert "context_reset_pct:" in capsys.readouterr().out


class TestModelEffort:
    def test_model_role(self, repo):
        run("model", "judge", "claude-opus-5-5", "--repo", str(repo))
        assert repo_toml(repo) == {"judge_model": "claude-opus-5-5"}

    def test_model_all_skips_reader(self, repo):
        run("model", "all", "sonnet", "--repo", str(repo))
        assert repo_toml(repo) == {"worker_model": "sonnet", "judge_model": "sonnet"}

    def test_model_default_unsets(self, repo):
        run("model", "worker", "opus", "--repo", str(repo))
        run("model", "worker", "default", "--repo", str(repo))
        assert "worker_model" not in repo_toml(repo)

    def test_model_show(self, repo, capsys):
        run("model", "--repo", str(repo))
        out = capsys.readouterr().out
        assert "worker" in out and "reader" in out and "suggested:" in out

    def test_effort_all(self, repo):
        run("effort", "all", "xhigh", "--repo", str(repo))
        assert repo_toml(repo) == {"worker_effort": "xhigh", "judge_effort": "xhigh"}

    def test_effort_rejects_unknown_level(self, repo):
        with pytest.raises(SystemExit, match="effort must be"):
            run("effort", "worker", "turbo", "--repo", str(repo))
        assert not (repo / REPO_CONFIG_NAME).exists()

    def test_context_reset_accepts_percent_sign(self, repo):
        run("context-reset", "45%", "--repo", str(repo))
        assert repo_toml(repo)["context_reset_pct"] == 45.0

    @pytest.mark.parametrize("bad", ["0", "101", "abc"])
    def test_context_reset_rejects(self, repo, bad):
        with pytest.raises(SystemExit):
            run("context-reset", bad, "--repo", str(repo))

    def test_sandbox(self, repo):
        run("sandbox", "off", "--repo", str(repo))
        assert repo_toml(repo)["sandbox"] == "off"
        with pytest.raises(SystemExit, match="auto, on or off"):
            run("sandbox", "maybe", "--repo", str(repo))


# --------------------------------------------------------------------------------------
# statusline
# --------------------------------------------------------------------------------------


class TestStatusline:
    def settings(self, repo: Path) -> Path:
        return repo / ".claude" / "settings.local.json"

    def test_install_uninstall_fresh(self, repo, isolated_claude):
        run("statusline", "install", "--repo", str(repo))
        data = json.loads(self.settings(repo).read_text())
        assert admin.STATUSLINE_MARK in data["statusLine"]["command"]
        assert data["statusLine"]["refreshInterval"] == 5
        run("statusline", "uninstall", "--repo", str(repo))
        assert "statusLine" not in json.loads(self.settings(repo).read_text())

    def test_install_saves_and_uninstall_restores_previous(self, repo, isolated_claude):
        s = self.settings(repo)
        s.parent.mkdir(parents=True)
        mine = {"type": "command", "command": "echo mine"}
        s.write_text(json.dumps({"statusLine": mine, "other": 1}))
        run("statusline", "install", "--interval", "9", "--repo", str(repo))
        assert json.loads(s.read_text())["other"] == 1
        run("statusline", "install", "--repo", str(repo))  # reinstall must not overwrite the saved one with ours
        run("statusline", "uninstall", "--repo", str(repo))
        assert json.loads(s.read_text()) == {"statusLine": mine, "other": 1}

    def test_uninstall_leaves_foreign_statusline(self, repo, isolated_claude, capsys):
        s = self.settings(repo)
        s.parent.mkdir(parents=True)
        s.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo mine"}}))
        run("statusline", "uninstall", "--repo", str(repo))
        assert "not installed" in capsys.readouterr().out
        assert json.loads(s.read_text())["statusLine"]["command"] == "echo mine"

    def test_status_and_user_scope(self, repo, isolated_claude, capsys):
        run("statusline", "install", "--scope", "user", "--repo", str(repo))
        assert (isolated_claude / "settings.json").exists()
        capsys.readouterr()
        run("statusline", "status", "--scope", "user", "--repo", str(repo))
        assert "installed" in capsys.readouterr().out

    def test_invalid_json_refused(self, repo, isolated_claude):
        s = self.settings(repo)
        s.parent.mkdir(parents=True)
        s.write_text("{nope")
        with pytest.raises(SystemExit, match="not valid JSON"):
            run("statusline", "install", "--repo", str(repo))
        assert s.read_text() == "{nope"


# --------------------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------------------


class TestInit:
    def test_fresh_repo(self, repo, isolated_claude):
        assert run("init", "--repo", str(repo)) == 0
        for name in ("autopilot-feature-start", "autopilot-review", "autopilot-ship"):
            assert (repo / ".claude" / "skills" / name / "SKILL.md").exists()
        assert (repo / REPO_CONFIG_NAME).exists()
        assert ".autopilot/" in (repo / ".git" / "info" / "exclude").read_text()

    def test_keeps_existing_cc_skill(self, repo, isolated_claude, capsys):
        own = repo / ".claude" / "skills" / "cc-review" / "SKILL.md"
        own.parent.mkdir(parents=True)
        own.write_text("mine")
        run("init", "--repo", str(repo))
        assert "keeping your /cc-review" in capsys.readouterr().out
        assert not (repo / ".claude" / "skills" / "autopilot-review").exists()
        run("init", "--all-skills", "--repo", str(repo))
        assert (repo / ".claude" / "skills" / "autopilot-review" / "SKILL.md").exists()

    def test_does_not_overwrite_without_force(self, repo, isolated_claude):
        run("init", "--repo", str(repo))
        dest = repo / ".claude" / "skills" / "autopilot-ship" / "SKILL.md"
        dest.write_text("edited")
        (repo / REPO_CONFIG_NAME).write_text('worker_model = "opus"\n')
        run("init", "--repo", str(repo))
        assert dest.read_text() == "edited"
        assert repo_toml(repo) == {"worker_model": "opus"}
        run("init", "--force", "--repo", str(repo))
        assert dest.read_text() != "edited"

    def test_with_statusline(self, repo, isolated_claude):
        run("init", "--statusline", "--repo", str(repo))
        data = json.loads((repo / ".claude" / "settings.local.json").read_text())
        assert admin.STATUSLINE_MARK in data["statusLine"]["command"]
