"""Setuid programs under the OS sandbox (2026-11-10, foxtrot's DOM evaluator tests).

macOS Seatbelt refuses to exec a setuid program from a sandboxed process (EPERM even under "(allow default)"), so a
test suite that runs `ps` failed only under the tooling sandbox. repo.sandbox_shim_dir() builds non-setuid,
ad-hoc-signed copies that go first on PATH for sandboxed tooling commands and sandboxed worker sessions.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from speckit_autopilot import repo as R
from speckit_autopilot.sessions import sandbox_env, tooling_sandbox_profile

MAC = sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec")
needs_seatbelt = pytest.mark.skipif(not MAC, reason="macOS sandbox-exec only")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


@needs_seatbelt
def test_ps_runs_inside_the_tooling_sandbox_only_through_the_shim(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_SHIM_DIR", str(tmp_path / "shims"))
    repo = _repo(tmp_path)
    prof = tooling_sandbox_profile(repo, "test")
    # The control: the real setuid binary cannot run in the sandbox.
    bare = R.run_shell("/bin/ps -o pid= -p $$", repo, 60, sandbox_profile=prof)
    assert bare.returncode != 0 and "not permitted" in (bare.stdout + bare.stderr).lower()
    # By name, PATH finds the shim first and it works.
    r = R.run_shell("command -v ps && ps -o pid= -p $$", repo, 60, sandbox_profile=prof)
    assert r.returncode == 0, r.stderr
    first, pid = r.stdout.split()
    assert first == str(tmp_path / "shims" / "ps") and pid.isdigit()
    assert not (Path(first).stat().st_mode & stat.S_ISUID)
    # Unsandboxed commands keep the user's PATH untouched.
    plain = R.run_shell("command -v ps", repo, 60)
    assert plain.stdout.strip() != first


@needs_seatbelt
def test_the_shim_is_rebuilt_when_the_source_binary_changes(tmp_path):
    base = tmp_path / "shims"
    assert R.sandbox_shim_dir(base) == str(base)
    (base / ".ps.source").write_text("0:0")  # as if /bin/ps changed in an OS update
    R._SHIM_CACHE.pop(str(base), None)
    assert R.sandbox_shim_dir(base) == str(base)
    assert (base / ".ps.source").read_text() != "0:0"


def test_no_shims_off_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    assert R.sandbox_shim_dir(tmp_path / "linux-shims") is None
    env = {"PATH": "/usr/bin:/bin"}
    monkeypatch.setattr(R, "sandbox_shim_dir", lambda base=None: None)
    assert R.with_shims_on_path(env) == env


def test_with_shims_on_path_puts_the_shim_first_once(monkeypatch):
    monkeypatch.setattr(R, "sandbox_shim_dir", lambda base=None: "/shims")
    assert R.with_shims_on_path({"PATH": "/a:/shims:/b"})["PATH"] == "/shims:/a:/b"


def test_sandboxed_worker_sessions_get_the_shims_on_path(monkeypatch):
    monkeypatch.setattr(R, "sandbox_shim_dir", lambda base=None: "/shims")
    env = sandbox_env({"enabled": True})
    assert env["PATH"].split(os.pathsep)[0] == "/shims" and "npm_config_store_dir" in env
    assert sandbox_env(None) == {} and sandbox_env({"enabled": False}) == {}


def test_preflight_warns_when_no_shim_could_be_built(monkeypatch):
    from speckit_autopilot import preflight
    from speckit_autopilot.config import Config

    monkeypatch.setattr(preflight, "tooling_sandbox_available", lambda: (True, "seatbelt"))
    monkeypatch.setattr(preflight, "sandbox_shim_dir", lambda: None)
    assert preflight.tooling_sandbox_item(Config())[1] == "WARN"
    monkeypatch.setattr(preflight, "sandbox_shim_dir", lambda: "/shims")
    name, st, detail = preflight.tooling_sandbox_item(Config())
    assert st == "PASS" and "`ps`" in detail
