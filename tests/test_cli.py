"""__main__.py: argument parsing, repo resolution and the read-only subcommands.

Only code paths that return before any session/judge is created are exercised; the
paths that would reach the SDK are cut off by patching ``load_config``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import claude_agent_sdk
import pytest

import speckit_autopilot.__main__ as cli
from speckit_autopilot.config import PHASES, ConfigError
from speckit_autopilot.state import RunPaths, RunState, StateStore


@pytest.fixture(autouse=True)
def _no_real_sdk(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("real claude_agent_sdk entry point used in a CLI unit test")

    monkeypatch.setattr(claude_agent_sdk, "query", _boom)
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _boom)


class _Stop(Exception):
    pass


@pytest.fixture
def capture_config(monkeypatch):
    """Replace load_config so main() stops right after computing the overrides."""
    seen: dict = {}

    def fake(repo, overrides=None, user_config=None):
        seen["repo"] = repo
        seen["overrides"] = overrides
        raise _Stop()

    monkeypatch.setattr(cli, "load_config", fake)
    return seen


@pytest.fixture
def empty_repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    return r


# ======================================================================================
# parser
# ======================================================================================


def test_parser_defaults():
    a = cli.build_parser().parse_args([])
    assert a.description is None and a.repo is None and a.issue is None
    assert not a.resume and not a.dry_run and not a.status and not a.report and not a.stop and not a.detach
    assert a.until is None and a.from_phase is None and a.budget is None


def test_parser_full_run_line():
    a = cli.build_parser().parse_args(["--repo", "bravo", "--issue", "ENG-142", "Add faceted product filters", "--until", "ship", "--budget", "12.5", "--allow-escalations", "--spec", "116"])
    assert a.repo == "bravo" and a.issue == "ENG-142" and a.description == "Add faceted product filters"
    assert a.until == "ship" and a.budget == 12.5 and a.allow_escalations and a.spec == "116"


@pytest.mark.parametrize("flag", ["--until", "--from"])
@pytest.mark.parametrize("phase", PHASES)
def test_until_and_from_accept_every_phase(flag, phase):
    a = cli.build_parser().parse_args([flag, phase])
    assert (a.until if flag == "--until" else a.from_phase) == phase


@pytest.mark.parametrize("flag", ["--until", "--from"])
@pytest.mark.parametrize("bad", ["deploy", "Ship", "speckit-plan", ""])
def test_until_and_from_reject_unknown_phase(flag, bad, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.build_parser().parse_args([flag, bad])
    assert ei.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_dry_run_flags():
    a = cli.build_parser().parse_args(["--issue", "X-1", "--dry-run", "--no-judge-ping", "--preflight-run-tests"])
    assert a.dry_run and a.no_judge_ping and a.preflight_run_tests


def test_budget_must_be_a_number():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--budget", "lots"])


# ======================================================================================
# resolve_repo
# ======================================================================================


def test_resolve_repo_relative_name_is_cwd_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the name does not exist relative to cwd
    assert cli.resolve_repo("Some-Repo-That-Is-Not-Here") == (tmp_path / "Some-Repo-That-Is-Not-Here").resolve()


def test_resolve_repo_prefers_existing_relative_path(tmp_path, monkeypatch):
    (tmp_path / "local-repo").mkdir()
    monkeypatch.chdir(tmp_path)
    assert cli.resolve_repo("local-repo") == (tmp_path / "local-repo").resolve()


def test_resolve_repo_absolute_and_home(tmp_path, monkeypatch):
    assert cli.resolve_repo(str(tmp_path)) == tmp_path.resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "proj").mkdir()
    assert cli.resolve_repo("~/proj") == (tmp_path / "proj").resolve()


def test_resolve_repo_defaults_to_cwd_git_repo(empty_repo, monkeypatch):
    monkeypatch.chdir(empty_repo)
    assert cli.resolve_repo(None).resolve() == empty_repo.resolve()


def test_resolve_repo_requires_repo_outside_git(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as ei:
        cli.resolve_repo(None)
    assert "--repo is required" in str(ei.value.code)


# ======================================================================================
# read-only subcommands
# ======================================================================================


def test_status_on_empty_repo(empty_repo, capsys):
    assert cli.main(["--repo", str(empty_repo), "--status"]) == 0
    out = capsys.readouterr().out
    assert "No autopilot run found" in out
    assert not (empty_repo / ".autopilot").exists()  # read-only


def test_report_on_empty_repo(empty_repo, capsys):
    assert cli.main(["--repo", str(empty_repo), "--report"]) == 0
    out = capsys.readouterr().out
    assert "(no " in out and "REPORT.md" in out
    assert not (empty_repo / ".autopilot").exists()


def test_status_and_report_with_a_previous_run(empty_repo, capsys):
    paths = RunPaths(empty_repo)
    fd = empty_repo / "specs" / "116-x"
    fd.mkdir(parents=True)
    (fd / "decisions.md").write_text("# Autopilot decisions — ENG-142\n")
    st = RunState(run_id="r1", issue="ENG-142", description="Add filters", repo=str(empty_repo), feature_dir=str(fd), current_phase="plan", status="halted")
    st.phase("feature_start").status = "passed"
    st.halt = {"reason": "phase budget exhausted"}
    StateStore(paths.state).save(st)
    paths.report.write_text("# Autopilot HALTED — ENG-142\n")
    assert cli.main(["--repo", str(empty_repo), "--status"]) == 0
    out = capsys.readouterr().out
    assert "ENG-142" in out and "halted" in out and "HALT: phase budget exhausted" in out
    assert cli.main(["--repo", str(empty_repo), "--report"]) == 0
    out = capsys.readouterr().out
    assert "# Autopilot HALTED — ENG-142" in out and "# Autopilot decisions — ENG-142" in out


def test_stop_without_a_live_run(empty_repo, capsys):
    assert cli.main(["--repo", str(empty_repo), "--stop"]) == 1
    assert "No live autopilot run" in capsys.readouterr().out


def test_stop_with_a_stale_lock(empty_repo, capsys):
    p = subprocess.Popen(["true"])
    p.wait()
    paths = RunPaths(empty_repo)
    paths.root.mkdir()
    paths.lock.write_text(json.dumps({"pid": p.pid}))
    assert cli.main(["--repo", str(empty_repo), "--stop"]) == 1


# ======================================================================================
# new-run argument checks (stop before any session is created)
# ======================================================================================


def test_linear_on_requires_an_issue_key(empty_repo):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--repo", str(empty_repo), "--linear", "some description"])
    assert "pass --issue <KEY>" in str(ei.value.code)


def _real_config_then_detach(monkeypatch) -> dict:
    """Run main() up to --detach with the real load_config, keeping the Config it built."""
    seen: dict = {}
    real = cli.load_config

    def spy(repo, overrides=None, user_config=None):
        seen["cfg"] = real(repo, overrides, user_config)
        return seen["cfg"]

    monkeypatch.setattr(cli, "load_config", spy)
    monkeypatch.setattr(cli, "detach", lambda argv, paths: 0)
    return seen


def test_without_linear_the_issue_is_optional_and_the_run_is_labelled_by_its_description(empty_repo, monkeypatch):
    seen = _real_config_then_detach(monkeypatch)
    assert cli.main(["--repo", str(empty_repo), "--no-linear", "--detach", "Add product filters to the catalog"]) == 0
    assert seen["cfg"].linear == "off"
    assert seen["cfg"].issue == "product-filters-catalog"


def test_auto_linear_without_a_server_runs_a_tracker_key_without_linear(empty_repo, monkeypatch):
    seen = _real_config_then_detach(monkeypatch)
    assert cli.main(["--repo", str(empty_repo), "--issue", "ENG-7", "--detach", "Add filters"]) == 0
    assert seen["cfg"].linear == "auto" and seen["cfg"].issue == "ENG-7"


def test_linear_flags_are_exclusive(empty_repo):
    with pytest.raises(SystemExit):
        cli.main(["--repo", str(empty_repo), "--linear", "--no-linear", "--issue", "ENG-1", "x"])


def test_linear_flag_reaches_the_overrides(empty_repo, capture_config):
    with pytest.raises(_Stop):
        cli.main(["--repo", str(empty_repo), "--no-linear", "x"])
    assert capture_config["overrides"]["linear"] == "off"


def test_description_is_required_for_a_new_run(empty_repo):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--repo", str(empty_repo), "--issue", "ENG-1"])
    assert "description is required" in str(ei.value.code)


def test_dry_run_does_not_need_a_description(empty_repo, capture_config):
    with pytest.raises(_Stop):
        cli.main(["--repo", str(empty_repo), "--issue", "ENG-1", "--dry-run"])
    assert capture_config["overrides"]["dry_run"] is True
    assert capture_config["overrides"]["description"] == ""


def test_overrides_passed_to_config(empty_repo, capture_config):
    with pytest.raises(_Stop):
        cli.main(["--repo", str(empty_repo), "--issue", "ENG-1", "Do the thing", "--until", "review", "--budget", "7", "--judge-model", "j", "--worker-model", "w", "--spec", "042"])
    o = capture_config["overrides"]
    assert capture_config["repo"] == empty_repo.resolve()
    assert o["issue"] == "ENG-1" and o["description"] == "Do the thing"
    assert o["until"] == "review" and o["from_phase"] is None
    assert "total_budget_usd" not in o and "max_wallclock_h" not in o  # --budget / --wallclock-h are ignored: no limits
    assert o["judge_model"] == "j" and o["worker_model"] == "w" and o["spec"] == "042"
    assert o["allow_escalations"] is None  # flag absent -> do not override the config file
    assert o["resume"] is False and o["dry_run"] is False


def test_resume_takes_issue_and_description_from_state(empty_repo, capture_config):
    StateStore(RunPaths(empty_repo).state).save(RunState(run_id="r", issue="ENG-9", description="from state", repo=str(empty_repo)))
    with pytest.raises(_Stop):
        cli.main(["--repo", str(empty_repo), "--resume", "--allow-escalations"])
    o = capture_config["overrides"]
    assert o["issue"] == "ENG-9" and o["description"] == "from state" and o["resume"] is True and o["allow_escalations"] is True


def test_from_phase_also_reads_state(empty_repo, capture_config):
    StateStore(RunPaths(empty_repo).state).save(RunState(run_id="r", issue="ENG-9", description="d9", repo=str(empty_repo)))
    with pytest.raises(_Stop):
        cli.main(["--repo", str(empty_repo), "--from", "plan"])
    assert capture_config["overrides"]["from_phase"] == "plan" and capture_config["overrides"]["issue"] == "ENG-9"


def test_config_error_becomes_clean_exit(empty_repo, monkeypatch):
    def bad(*a, **k):
        raise ConfigError("context_reset_pct must be within 1..100")

    monkeypatch.setattr(cli, "load_config", bad)
    with pytest.raises(SystemExit) as ei:
        cli.main(["--repo", str(empty_repo), "--issue", "ENG-1", "x"])
    assert str(ei.value.code) == "config error: context_reset_pct must be within 1..100"
