"""Sandbox and tooling regressions from the third adversarial review:

1. a stop escalates to SIGKILL when a tooling command ignores SIGTERM, and the run ends "stopped" (and releases its lock)
   only once the command is gone;
2. the supervisor's own test/lint/typecheck/build run under a Seatbelt profile (macOS) where the OS sandbox is on: no
   writes outside the repo / temp / package caches, none to the repo's hooks, Spec Kit scripts, Claude settings or the
   autopilot state, no reads of credential files, localhost-only network (build keeps the network), secrets out of the
   environment;
3. worker sessions' sandbox write-protects the policy's always-protected paths (denyWrite), denies reads of ~/.npmrc and
   the repo's local secret env files, and unsets secret-named environment variables (credentials.envVars);
4. tooling no longer gets CI=1 from the supervisor (Alpha's dev-database guard is off under CI).
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from speckit_autopilot import repo as R
from speckit_autopilot import sessions as S
from speckit_autopilot.config import load_config
from speckit_autopilot.repo import CmdResult, run_shell

from .fakeworld import World, make_repo, make_runner
from .test_e2e import events, run

MAC = sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec")
needs_seatbelt = pytest.mark.skipif(not MAC, reason="macOS sandbox-exec only")


# --------------------------------------------------------------------------------------
# 1. a stop that SIGTERM does not end
# --------------------------------------------------------------------------------------

TERM_IGNORING = "trap '' TERM; sleep 12; echo done-late > late_marker.txt"


def test_a_stop_kills_a_term_ignoring_tooling_command_before_the_run_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "KILL_GRACE_S", 0.5)
    w = World(tmp_path)
    runner, *_ = make_runner(w, test_cmd=TERM_IGNORING)
    started: list[float] = []

    def tool(cmd: str, cwd: Path, timeout: float) -> CmdResult:
        if cmd != TERM_IGNORING:
            return CmdResult(cmd, 0, "ok", "")
        loop = runner._loop
        loop.call_soon_threadsafe(loop.call_later, 0.3, runner.request_stop, "SIGTERM")
        started.append(time.monotonic())
        return run_shell(cmd, cwd, timeout)

    runner.tool_runner = tool
    t0 = time.monotonic()
    out = run(runner)
    took = time.monotonic() - t0
    assert out.status == "stopped", out.reason
    assert took < 8, f"the stop took {took:.1f}s"
    assert not R._RUNNING, "a tooling command was still registered when the run ended"
    assert not runner.paths.lock.exists()
    time.sleep(0.5)
    assert not (w.repo / "late_marker.txt").exists()
    assert events(w, "tooling_killed")


def test_kill_running_commands_escalates_to_sigkill_after_the_grace_period(tmp_path):
    results: list[CmdResult] = []
    th = threading.Thread(target=lambda: results.append(run_shell("trap '' TERM; sleep 20", tmp_path, 60)))
    th.start()
    deadline = time.monotonic() + 5
    while not R._RUNNING and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.2)  # let the shell install its trap
    t0 = time.monotonic()
    assert R.kill_running_commands(grace_s=0.3)
    assert R.wait_for_commands(5.0) == []
    th.join(5)
    assert results and results[0].returncode != 0 and time.monotonic() - t0 < 4


# --------------------------------------------------------------------------------------
# 2. the supervisor's tooling sandbox
# --------------------------------------------------------------------------------------


def _profile(repo: Path, tmp: Path, kind: str = "test", **kw) -> str:
    return S.tooling_sandbox_profile(repo, kind, temp_dirs=[str(tmp / "scratch")], cache_dirs=[str(tmp / "cache")], deny_read=[str(tmp / "home" / ".ssh")], **kw)


@needs_seatbelt
def test_tooling_profile_confines_writes_reads_and_network(tmp_path):
    repo = make_repo(tmp_path)
    for d in ("scratch", "cache", "outside", "home/.ssh"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / ".ssh" / "id_ed25519").write_text("PRIVATE")
    (repo / ".husky").mkdir()
    prof = _profile(repo, tmp_path)
    script = (
        f"echo ok > {repo}/src/written.txt && echo repo-ok; "
        f"echo x > {tmp_path}/scratch/t.txt && echo tmp-ok; "
        f"echo x > {tmp_path}/cache/c.txt && echo cache-ok; "
        f"echo x > {tmp_path}/outside/o.txt 2>/dev/null || echo outside-denied; "
        f"echo x > {repo}/.git/hooks/pre-commit 2>/dev/null || echo hooks-denied; "
        f"echo x >> {repo}/.git/config 2>/dev/null || echo gitconfig-denied; "
        f"echo x > {repo}/.husky/pre-commit 2>/dev/null || echo husky-denied; "
        f"echo x > {repo}/.specify/scripts/x.sh 2>/dev/null || echo specify-denied; "
        f"mkdir -p {repo}/.autopilot 2>/dev/null; echo x > {repo}/.autopilot/state.json 2>/dev/null || echo state-denied; "
        f"cat {tmp_path}/home/.ssh/id_ed25519 2>/dev/null || echo ssh-read-denied; "
        "git status --porcelain >/dev/null && echo git-ok"
    )
    r = run_shell(script, repo, 30, sandbox_profile=prof)
    out = r.stdout.split()
    assert {"repo-ok", "tmp-ok", "cache-ok", "outside-denied", "hooks-denied", "gitconfig-denied", "husky-denied", "specify-denied", "state-denied", "ssh-read-denied", "git-ok"} <= set(out), r.stdout + r.stderr
    assert not (tmp_path / "outside" / "o.txt").exists() and "PRIVATE" not in r.stdout


@needs_seatbelt
def test_tooling_profile_allows_localhost_but_not_other_hosts_except_for_build(tmp_path):
    repo = make_repo(tmp_path)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: [srv.accept() for _ in range(2)], daemon=True).start()
    code = (
        "import socket\n"
        f"socket.create_connection(('localhost', {port}), timeout=3); print('local-ok')\n"
        "s = socket.socket(); s.settimeout(1)\n"
        "try:\n    s.connect(('10.255.255.1', 9)); print('external-open')\n"
        "except PermissionError: print('external-denied')\n"
        "except OSError: print('external-attempted')\n"
    )
    (tmp_path / "net.py").write_text(code)
    test_run = run_shell(f"python3 {tmp_path}/net.py", repo, 30, sandbox_profile=_profile(repo, tmp_path, "test"))
    assert "local-ok" in test_run.stdout and "external-denied" in test_run.stdout, test_run.stdout + test_run.stderr
    build_run = run_shell(f"python3 {tmp_path}/net.py", repo, 30, sandbox_profile=_profile(repo, tmp_path, "build", open_network=True))
    assert "local-ok" in build_run.stdout and "external-denied" not in build_run.stdout, build_run.stdout + build_run.stderr
    srv.close()


def test_tooling_env_drops_secrets_but_keeps_what_tests_need(tmp_path, monkeypatch):
    for k, v in {"GH_TOKEN": "ghp_x", "ANTHROPIC_API_KEY": "sk-x", "AWS_SECRET_ACCESS_KEY": "a", "SSH_AUTH_SOCK": "/tmp/agent", "NPM_TOKEN": "n", "DATABASE_URL": "postgres://u@localhost/db_test", "MY_FLAG": "1"}.items():
        monkeypatch.setenv(k, v)
    r = run_shell('echo "gh=${GH_TOKEN:-unset} ak=${ANTHROPIC_API_KEY:-unset} aws=${AWS_SECRET_ACCESS_KEY:-unset} ssh=${SSH_AUTH_SOCK:-unset} npm=${NPM_TOKEN:-unset} db=$DATABASE_URL flag=$MY_FLAG extra=$EXTRA_TOKEN"', tmp_path, 30, env={"EXTRA_TOKEN": "explicit"}, scrub_secrets=True)
    assert r.stdout.strip() == "gh=unset ak=unset aws=unset ssh=unset npm=unset db=postgres://u@localhost/db_test flag=1 extra=explicit"


def test_the_runner_runs_its_tooling_sandboxed_where_the_os_sandbox_is_on(tmp_path, monkeypatch):
    w = World(tmp_path)
    runner, *_ = make_runner(w, sandbox="auto", tooling_sandbox="auto")
    seen: list[tuple[str, str | None, bool]] = []

    def fake_run_shell(cmd, cwd, timeout_s=600.0, env=None, should_stop=None, *, sandbox_profile=None, scrub_secrets=False):
        seen.append((cmd, sandbox_profile, scrub_secrets))
        return CmdResult(cmd, 0, "ok", "")

    monkeypatch.setattr("speckit_autopilot.runner.run_shell", fake_run_shell)
    monkeypatch.setattr("speckit_autopilot.runner.tooling_sandbox_available", lambda: (True, "test"))
    runner.tool_runner = runner._run_tool
    runner.cfg.build_cmd = "pnpm build"
    runner._profile = None
    out = run(runner)
    assert out.status == "completed", out.reason
    by_cmd = {c: (p, s) for c, p, s in seen}
    assert set(by_cmd) >= {"pytest -q", "ruff check .", "pnpm build"}
    assert all(s for _, s in by_cmd.values())  # secrets scrubbed for every tooling command
    test_prof, build_prof = by_cmd["pytest -q"][0], by_cmd["pnpm build"][0]
    assert test_prof and "(deny network-outbound)" in test_prof and str(os.path.realpath(w.repo)) in test_prof
    assert build_prof and "(deny network-outbound)" not in build_prof
    assert events(w, "tooling_start")[0]["sandboxed"] is True


def test_tooling_sandbox_off_or_unavailable_runs_unsandboxed_and_says_so(tmp_path, monkeypatch):
    w = World(tmp_path)
    runner, *_ = make_runner(w, sandbox="auto", tooling_sandbox="auto")
    seen: list[str | None] = []
    monkeypatch.setattr("speckit_autopilot.runner.run_shell", lambda cmd, cwd, t=0, env=None, should_stop=None, *, sandbox_profile=None, scrub_secrets=False: (seen.append(sandbox_profile), CmdResult(cmd, 0, "ok", ""))[1])
    monkeypatch.setattr("speckit_autopilot.runner.tooling_sandbox_available", lambda: (False, "no sandbox-exec"))
    runner.tool_runner = runner._run_tool
    assert run(runner).status == "completed"
    assert seen and all(p is None for p in seen)
    assert events(w, "tooling_start")[0]["sandboxed"] is False
    assert events(w, "tooling_sandbox_unavailable")


def test_tooling_sandbox_on_but_unavailable_halts_instead_of_running_unsandboxed(tmp_path, monkeypatch):
    w = World(tmp_path)
    runner, *_ = make_runner(w, sandbox="auto", tooling_sandbox="on")
    monkeypatch.setattr("speckit_autopilot.runner.tooling_sandbox_available", lambda: (False, "no sandbox-exec"))
    runner.tool_runner = runner._run_tool
    out = run(runner)
    assert out.status == "halted" and "tooling_sandbox = on" in out.reason


# --------------------------------------------------------------------------------------
# 3. the worker sandbox: protected paths, credential files, secret env vars
# --------------------------------------------------------------------------------------


def test_worker_sandbox_write_protects_the_policys_always_protected_paths(tmp_path):
    repo = make_repo(tmp_path)
    sb = S.sandbox_settings("implement", "on", repo=repo)
    deny = set(sb["filesystem"]["denyWrite"])
    real = os.path.realpath(repo)
    for rel in (".git/hooks", ".git/config", ".husky", ".githooks", "lefthook.yml", ".lefthook.yml", ".specify/scripts", ".specify/templates",
                ".specify/extensions", ".specify/memory/constitution.md", ".claude/settings.json", ".claude/hooks", ".claude/skills", ".mcp.json",
                ".autopilot", ".autopilot.toml"):
        assert os.path.join(real, rel) in deny, rel
    assert not any(d.endswith("/.git") for d in deny)  # commits must still work


def test_worker_sandbox_denies_npmrc_and_local_secret_env_files(tmp_path):
    repo = make_repo(tmp_path)
    for rel in (".env", ".env.local", "apps/web/.env.production.local", ".env.example", "apps/web/.env.test"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("X=1\n")
    sb = S.sandbox_settings("implement", "on", repo=repo)
    deny = set(sb["filesystem"]["denyRead"])
    real = os.path.realpath(repo)
    assert os.path.expanduser("~/.npmrc") in deny
    assert os.path.join(real, ".env.local") in deny and os.path.join(real, "apps/web/.env.production.local") in deny
    assert os.path.join(real, ".env.example") not in deny and os.path.join(real, "apps/web/.env.test") not in deny


def test_worker_sandbox_unsets_secret_env_vars_for_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    monkeypatch.setenv("MY_FLAG", "1")
    sb = S.sandbox_settings("implement", "on", repo=make_repo(tmp_path))
    names = {e["name"]: e["mode"] for e in sb["credentials"]["envVars"]}
    assert names.get("GH_TOKEN") == "deny" and names.get("ANTHROPIC_API_KEY") == "deny" and "MY_FLAG" not in names


def test_worker_sessions_get_the_repo_specific_sandbox(tmp_path):
    w = World(tmp_path)
    runner, *_ = make_runner(w, sandbox="on")
    run(runner)
    impl = [s for s in w.sessions if s.spec.phase == "implement"]
    assert impl and impl[0].spec.sandbox and os.path.join(os.path.realpath(w.repo), ".husky") in impl[0].spec.sandbox["filesystem"]["denyWrite"]


# --------------------------------------------------------------------------------------
# 4. CI is not forced on
# --------------------------------------------------------------------------------------


def test_run_shell_does_not_set_ci(tmp_path, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    r = run_shell('echo "ci=${CI-unset}"', tmp_path, 30)
    assert r.stdout.strip() == "ci=unset"


# --------------------------------------------------------------------------------------
# config and preflight
# --------------------------------------------------------------------------------------


def test_tooling_sandbox_config_is_validated(tmp_path):
    from speckit_autopilot.config import ConfigError

    ws = tmp_path / "ws.toml"
    ws.write_text('tooling_sandbox = "sometimes"\n')
    with pytest.raises(ConfigError, match="tooling_sandbox must be one of"):
        load_config(tmp_path / "repo", user_config=ws)
    ws.write_text('tooling_sandbox = "off"\n')
    assert load_config(tmp_path / "repo", user_config=ws).tooling_sandbox == "off"


@pytest.mark.parametrize(
    "mode, sandbox, available, status",
    [("auto", "auto", True, "PASS"), ("auto", "auto", False, "WARN"), ("on", "auto", False, "FAIL"), ("off", "auto", True, "INFO"), ("auto", "off", True, "INFO")],
)
def test_preflight_reports_the_tooling_sandbox(monkeypatch, mode, sandbox, available, status):
    from speckit_autopilot import preflight
    from speckit_autopilot.config import Config

    monkeypatch.setattr(preflight, "tooling_sandbox_available", lambda: (available, "why"))
    monkeypatch.setattr(preflight, "sandbox_shim_dir", lambda: "/shims")
    name, st, _detail = preflight.tooling_sandbox_item(Config(tooling_sandbox=mode, sandbox=sandbox))
    assert name == "tooling sandbox" and st == status


def test_sandboxed_tooling_turns_off_telemetry_that_writes_outside_the_sandbox(tmp_path, monkeypatch):
    w = World(tmp_path)
    runner, *_ = make_runner(w, tooling_env={"X": "1"})
    seen: list[dict | None] = []
    monkeypatch.setattr("speckit_autopilot.runner.run_shell", lambda cmd, cwd, t=0, env=None, should_stop=None, *, sandbox_profile=None, scrub_secrets=False: (seen.append(env), CmdResult(cmd, 0, "ok", ""))[1])
    monkeypatch.setattr("speckit_autopilot.runner.tooling_sandbox_available", lambda: (True, "test"))
    runner._run_tool("pytest -q", w.repo, 10)
    assert seen[-1] == {"NEXT_TELEMETRY_DISABLED": "1", "TURBO_TELEMETRY_DISABLED": "1", "X": "1"}
