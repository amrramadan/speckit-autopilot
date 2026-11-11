"""config.py: defaults, precedence (defaults < user config < [repos."name"] < repo file < CLI), validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from speckit_autopilot.config import (
    CONTRACT_ENV,
    PHASES,
    REPO_CONFIG_NAME,
    SANDBOX_CONFIG_MODES,
    SHIP_AUTHORIZATION,
    Config,
    ConfigError,
    load_config,
    load_toml,
    validate,
)


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "My-Repo"
    r.mkdir()
    return r


@pytest.fixture
def ws(tmp_path) -> Path:
    return tmp_path / "workspace.autopilot.toml"


def _write(p: Path, text: str) -> Path:
    p.write_text(text)
    return p


def test_defaults(repo, ws):
    cfg = load_config(repo, user_config=ws)  # ws file does not exist
    assert cfg.repo == repo.resolve()
    assert cfg.judge_model == "claude-opus-5-5"
    assert cfg.worker_model is None
    assert cfg.context_reset_pct == 60.0
    assert cfg.max_repairs_per_phase == 2 and cfg.max_back_edges == 3 and cfg.max_back_per_target == 1
    # no limits by default (the runs use the Claude subscription): no budget, no wall clock, no per-phase caps
    assert cfg.total_budget_usd is None and cfg.max_wallclock_h is None and cfg.judge_call_budget_usd is None
    assert cfg.phase_budget_usd == {} and cfg.phase_budget("implement") is None and cfg.phase_budget("unknown") is None
    assert cfg.linear_write_phases == ["linear"] and cfg.push_phases == ["ship"] and cfg.install_phases == ["implement"]
    assert ".specify/memory/constitution.md" in cfg.protected_paths
    assert cfg.status_marker_style == "html" and cfg.spec_id_style == "number"
    assert cfg.ci_required is None and not cfg.allow_escalations and not cfg.dry_run
    # human-owned work and the test baseline keep today's behaviour unless a repo opts in
    assert cfg.human_tasks_block_ship is True and cfg.checklists_gate == "judge" and cfg.test_baseline == "none"
    # pass 2: the OS sandbox on where available, no shared issues, the ship authorisation line first
    assert cfg.sandbox == "auto" and cfg.sandbox_network_domains == [] and cfg.shared_issues == []
    assert cfg.ship_args.splitlines()[0] == SHIP_AUTHORIZATION == "AUTOPILOT-AUTHORIZED: commit push pr merge switch-base"
    assert "gh pr merge --merge" in cfg.ship_args.splitlines()[1]


def test_default_mutables_are_not_shared():
    a, b = Config(), Config()
    a.phase_budget_usd["plan"] = 99.0
    a.protected_paths.append("x")
    assert "plan" not in b.phase_budget_usd and "x" not in b.protected_paths


def test_workspace_file_overrides_defaults(repo, ws):
    _write(ws, 'judge_model = "claude-sonnet-5"\ntotal_budget_usd = 25.0\nmax_repairs_per_phase = 1\n')
    cfg = load_config(repo, user_config=ws)
    assert cfg.judge_model == "claude-sonnet-5" and cfg.total_budget_usd == 25.0 and cfg.max_repairs_per_phase == 1


def test_full_precedence_chain(repo, ws):
    _write(
        ws,
        "\n".join(
            [
                'judge_model = "ws-judge"',
                'test_cmd = "ws-test"',
                'lint_cmd = "ws-lint"',
                'build_cmd = "ws-build"',
                "total_budget_usd = 30.0",
                "",
                '[repos."My-Repo"]',
                'test_cmd = "table-test"',
                'lint_cmd = "table-lint"',
                "",
                '[repos."Other-Repo"]',
                'test_cmd = "other-test"',
            ]
        ),
    )
    _write(repo / REPO_CONFIG_NAME, 'lint_cmd = "repo-lint"\ntotal_budget_usd = 20.0\n')
    cfg = load_config(repo, overrides={"total_budget_usd": 10.0, "judge_model": None}, user_config=ws)
    assert cfg.judge_model == "ws-judge"  # CLI None does not override
    assert cfg.build_cmd == "ws-build"  # only the workspace sets it
    assert cfg.test_cmd == "table-test"  # [repos."My-Repo"] beats workspace top level
    assert cfg.lint_cmd == "repo-lint"  # repo file beats the table
    assert cfg.total_budget_usd == 10.0  # CLI beats everything


def test_repos_table_matches_absolute_path(repo, ws):
    _write(ws, f'[repos."{repo.resolve()}"]\ntest_cmd = "abs-test"\n')
    assert load_config(repo, user_config=ws).test_cmd == "abs-test"


def test_repos_table_for_other_repo_is_ignored(repo, ws):
    _write(ws, '[repos."Someone-Else"]\ntest_cmd = "nope"\n')
    assert load_config(repo, user_config=ws).test_cmd is None


def test_dict_values_merge_instead_of_replace(repo, ws):
    _write(ws, "[phase_budget_usd]\nimplement = 20.0\n")
    _write(repo / REPO_CONFIG_NAME, "[phase_budget_usd]\nplan = 1.0\n")
    cfg = load_config(repo, overrides={"phase_budget_usd": {"review": 9.0}}, user_config=ws)
    assert cfg.phase_budget("implement") == 20.0
    assert cfg.phase_budget("plan") == 1.0
    assert cfg.phase_budget("review") == 9.0
    assert cfg.phase_budget("clarify") is None  # no limit unless one is set


def test_list_values_replace(repo, ws):
    _write(repo / REPO_CONFIG_NAME, 'protected_paths = ["only/this"]\nallowed_reply_commands = ["speckit.specify"]\n')
    cfg = load_config(repo, user_config=ws)
    assert cfg.protected_paths == ["only/this"]
    assert cfg.allowed_reply_commands == ["speckit.specify"]


def test_repo_key_in_files_is_ignored(repo, ws, tmp_path):
    _write(ws, f'repo = "{tmp_path / "elsewhere"}"\n')
    assert load_config(repo, user_config=ws).repo == repo.resolve()


def test_repo_file_equal_to_workspace_file_applied_once(tmp_path):
    """Running against the workspace itself: the same file is not applied twice (no error)."""
    f = _write(tmp_path / REPO_CONFIG_NAME, "total_budget_usd = 12.0\n")
    cfg = load_config(tmp_path, user_config=f)
    assert cfg.total_budget_usd == 12.0


def test_cli_overrides_apply_false_but_skip_none(repo, ws):
    _write(repo / REPO_CONFIG_NAME, "dry_run = true\nallow_escalations = true\n")
    cfg = load_config(repo, overrides={"dry_run": False, "allow_escalations": None, "issue": "ENG-1", "description": "d"}, user_config=ws)
    assert cfg.dry_run is False  # explicit False from the CLI wins
    assert cfg.allow_escalations is True  # None means "not given"
    assert cfg.issue == "ENG-1" and cfg.description == "d"


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "toml,msg",
    [
        ("bogus_key = 1\n", "unknown config key 'bogus_key'"),
        ('until = "deploy"\n', "--until must be one of"),
        ('from_phase = "deploy"\n', "--from must be one of"),
        ("context_reset_pct = 0\n", "context_reset_pct"),
        ("context_reset_pct = 101\n", "context_reset_pct"),
        ("max_repairs_per_phase = -1\n", "repair / back-edge limits"),
        ("max_back_edges = -1\n", "repair / back-edge limits"),
        ('spec_id_style = "uuid"\n', "spec_id_style"),
        ('status_marker_style = "fancy"\n', "status_marker_style"),
        ("total_budget_usd = 0\n", "total_budget_usd"),
        ('push_phases = ["ship", "deploy"]\n', "unknown phase 'deploy'"),
        ('linear_write_phases = ["lin"]\n', "unknown phase 'lin'"),
        ('test_baseline = "main"\n', "test_baseline must be one of none, base"),
        ('checklists_gate = "ask"\n', "checklists_gate must be one of judge, halt"),
        ('human_tasks_block_ship = "yes"\n', "human_tasks_block_ship must be true/false"),
        ('sandbox = "strict"\n', "sandbox must be one of auto, on, off"),
        ('sandbox = true\n', "sandbox must be a string"),
        ('shared_issues = "ENG-1"\n', "shared_issues must be a list of strings"),
        ('sandbox_network_domains = ["a.example", 3]\n', "sandbox_network_domains must be a list of strings"),
    ],
)
def test_validation_errors(repo, ws, toml, msg):
    _write(repo / REPO_CONFIG_NAME, toml)
    with pytest.raises(ConfigError) as ei:
        load_config(repo, user_config=ws)
    assert msg in str(ei.value)


@pytest.mark.parametrize("toml", ['total_budget_usd = "40"\n', 'push_phases = "ship"\n'])
def test_wrongly_typed_values_raise_config_error(repo, ws, toml):
    _write(repo / REPO_CONFIG_NAME, toml)
    with pytest.raises(ConfigError):
        load_config(repo, user_config=ws)


def test_unknown_key_error_names_the_source(repo, ws):
    _write(ws, '[repos."My-Repo"]\nnot_a_key = 1\n')
    with pytest.raises(ConfigError) as ei:
        load_config(repo, user_config=ws)
    assert "[repos.My-Repo]" in str(ei.value) and str(ws) in str(ei.value)


def test_unknown_cli_override_rejected(repo, ws):
    with pytest.raises(ConfigError) as ei:
        load_config(repo, overrides={"nonsense": 1}, user_config=ws)
    assert "CLI" in str(ei.value)


def test_halt_notice_is_a_valid_write_phase():
    cfg = Config(linear_write_phases=["linear", "halt_notice"])
    validate(cfg)


@pytest.mark.parametrize("p", PHASES)
def test_until_and_from_accept_every_phase(p):
    validate(Config(until=p, from_phase=p))


def test_invalid_toml_raises(repo, ws):
    import tomllib

    _write(ws, "this is = = not toml")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_config(repo, user_config=ws)


def test_load_toml_missing_file(tmp_path):
    assert load_toml(tmp_path / "nope.toml") == {}


# ---------------------------------------------------------------- derived values


@pytest.mark.parametrize(
    "server,prefix",
    [
        ("plugin:design:linear", "mcp__plugin_design_linear__"),
        ("linear", "mcp__linear__"),
        ("claude.ai Linear", "mcp__claude_ai_Linear__"),
        ("linear-server", "mcp__linear-server__"),
    ],
)
def test_linear_tool_prefix(server, prefix):
    assert Config(linear_server=server).linear_tool_prefix == prefix


def test_to_dict_is_plain(repo):
    d = Config(repo=repo).to_dict()
    assert d["repo"] == str(repo)
    assert d["phase_budget_usd"] == {} and d["total_budget_usd"] is None


def test_founder_and_baseline_keys_load_from_the_repo_table(repo, ws):
    _write(ws, '[repos."My-Repo"]\ntest_baseline = "base"\nchecklists_gate = "halt"\nhuman_tasks_block_ship = false\n')
    cfg = load_config(repo, user_config=ws)
    assert (cfg.test_baseline, cfg.checklists_gate, cfg.human_tasks_block_ship) == ("base", "halt", False)


def test_sandbox_and_shared_issue_keys_load_from_the_repo_table(repo, ws):
    _write(ws, '[repos."My-Repo"]\nsandbox = "on"\nsandbox_network_domains = ["binaries.prisma.sh"]\nshared_issues = ["ENG-1366"]\n')
    cfg = load_config(repo, user_config=ws)
    assert (cfg.sandbox, cfg.sandbox_network_domains, cfg.shared_issues) == ("on", ["binaries.prisma.sh"], ["ENG-1366"])


def test_sandbox_modes_match_the_session_layer():
    from speckit_autopilot.sessions import SANDBOX_MODES

    assert set(SANDBOX_CONFIG_MODES) == set(SANDBOX_MODES)


def test_contract_env_is_the_documented_pair():
    assert CONTRACT_ENV == {"AUTOPILOT": "1", "AUTOPILOT_UNATTENDED": "1"}
