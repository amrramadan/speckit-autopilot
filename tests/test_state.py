"""state.py: run lock, checkpoint store, atomic writes, git exclude, redaction, event log."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from speckit_autopilot.state import (
    AUTOPILOT_DIR,
    EventLog,
    LockHeld,
    PhaseRecord,
    RunLock,
    RunPaths,
    RunState,
    StateStore,
    TranscriptWriter,
    atomic_write,
    ensure_git_exclude,
    pid_alive,
    redact,
    redact_obj,
)

R = "[REDACTED]"


def _dead_pid() -> int:
    """A pid that certainly belonged to a process which has exited and been reaped."""
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("hi\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


# ======================================================================================
# RunLock
# ======================================================================================


def test_pid_alive_basics():
    assert pid_alive(os.getpid())
    assert not pid_alive(0)
    assert not pid_alive(-5)
    assert not pid_alive(_dead_pid())


def test_lock_acquire_and_release(tmp_path):
    lock = RunLock(tmp_path / ".autopilot" / "lock")
    assert lock.acquire("run-1") is None  # fresh lock: nothing stale replaced
    info = lock.read()
    assert info["pid"] == os.getpid()
    assert info["run_id"] == "run-1"
    assert info["host"] == socket.gethostname()
    assert lock.holder_alive()
    lock.release()
    assert not lock.path.exists()
    assert lock.read() is None
    assert not lock.holder_alive()


def test_lock_live_pid_refuses_with_lockheld(tmp_path):
    path = tmp_path / "lock"
    other = RunLock(path, pid=os.getppid())  # a live process that is not us
    assert other.acquire("other-run") is None
    me = RunLock(path)
    with pytest.raises(LockHeld) as ei:
        me.acquire("my-run")
    assert "other-run" in str(ei.value)
    # the holder's lock is untouched
    assert RunLock(path).read()["pid"] == os.getppid()
    # release by a non-holder is a no-op
    me.release()
    assert path.exists()


def test_lock_stale_pid_is_replaced_and_reported(tmp_path):
    path = tmp_path / "lock"
    dead = _dead_pid()
    path.write_text(json.dumps({"pid": dead, "started_at": "x", "run_id": "old", "host": socket.gethostname()}))
    lock = RunLock(path)
    stale = lock.acquire("new")
    assert stale is not None and stale["pid"] == dead and stale["run_id"] == "old"
    info = lock.read()
    assert info["pid"] == os.getpid() and info["run_id"] == "new"


def test_lock_corrupt_file_is_treated_as_stale(tmp_path):
    path = tmp_path / "lock"
    path.write_text("{not json")
    stale = RunLock(path).acquire("new")
    assert stale == {"stale": True}
    assert RunLock(path).read()["run_id"] == "new"


def test_lock_from_another_host_is_not_trusted(tmp_path):
    """A live-looking pid recorded by another host cannot be checked, so it is replaced."""
    path = tmp_path / "lock"
    path.write_text(json.dumps({"pid": os.getppid(), "run_id": "remote", "host": "some-other-host.invalid"}))
    stale = RunLock(path).acquire("local")
    assert stale["run_id"] == "remote"
    assert RunLock(path).read()["run_id"] == "local"


def test_lock_is_reentrant_for_the_same_pid(tmp_path):
    path = tmp_path / "lock"
    a = RunLock(path)
    assert a.acquire("r1") is None
    b = RunLock(path)  # same pid
    assert b.acquire("r2") is None
    # re-entrant acquire does not rewrite the lock
    assert RunLock(path).read()["run_id"] == "r1"
    b.release()
    assert not path.exists()


# ======================================================================================
# StateStore / RunState / PhaseRecord
# ======================================================================================


def _populated_state() -> RunState:
    st = RunState(run_id="20261110-000000-abcdef", issue="ENG-142", description="Add Organic product filters", repo="/tmp/repo")
    st.feature_branch = "feature/116-faceted-filters"
    st.feature_dir = "/tmp/repo/specs/116-faceted-filters"
    st.current_phase = "analyze"
    st.total_cost_usd = 3.25
    st.elapsed_s = 120.5
    rec = st.phase("clarify")
    rec.status = "passed"
    rec.attempts = 2
    rec.repairs = 1
    rec.cost_usd = 0.75
    rec.duration_s = 33.0
    rec.last_failed_checks = ["[no-markers] spec.md still has NEEDS CLARIFICATION"]
    rec.notes = ["note"]
    rec.artifact_hashes = {"spec.md": "abc123"}
    st.phase("analyze").status = "running"
    st.back_edges.append({"from": "analyze", "to": "plan", "reason": "gap"})
    st.resets.append({"phase": "implement", "reason": "context 70%"})
    st.escalations.append({"phase": "plan", "reason": "payments"})
    st.ship = {"pr_url": "https://github.com/o/r/pull/1", "merged": False}
    st.linear = {"read": True, "title": "Organic filters"}
    st.halt = {"reason": "x", "phase": "analyze"}
    st.flags = {"allow_escalations": True, "until": "ship"}
    st.decisions_frozen = True
    return st


def test_state_store_round_trip_including_phase_records(tmp_path):
    store = StateStore(tmp_path / ".autopilot" / "state.json")
    assert not store.exists()
    st = _populated_state()
    store.save(st)
    assert store.exists()
    back = store.load()
    assert isinstance(back.phases["clarify"], PhaseRecord)
    assert back.phases["clarify"] == st.phases["clarify"]
    assert back.phases["analyze"].status == "running"
    assert back.to_dict() == st.to_dict()
    # the file is plain, pretty JSON
    raw = json.loads(store.path.read_text())
    assert raw["phases"]["clarify"]["artifact_hashes"] == {"spec.md": "abc123"}


def test_state_phase_creates_record_lazily():
    st = RunState(run_id="r", issue="I", description="d", repo="/x")
    assert "plan" not in st.phases
    rec = st.phase("plan")
    assert rec.status == "pending" and rec.attempts == 0
    assert st.phase("plan") is rec


def test_state_from_dict_ignores_unknown_top_level_keys():
    d = RunState(run_id="r", issue="I", description="d", repo="/x").to_dict()
    d["some_future_field"] = 1
    st = RunState.from_dict(d)
    assert st.run_id == "r"


def test_state_store_save_redacts_secrets(tmp_path):
    store = StateStore(tmp_path / "state.json")
    st = RunState(run_id="r", issue="I", description="use key sk-ant-" "api03-AbCdEfGhIjKlMnOp to call", repo="/x")
    st.halt = {"reason": "push failed for https://bob:hunter2@github.com/o/r.git"}
    store.save(st)
    text = store.path.read_text()
    assert "sk-ant-api03" not in text and "hunter2" not in text
    assert R in text


def test_state_store_save_bumps_updated_at(tmp_path):
    store = StateStore(tmp_path / "state.json")
    st = RunState(run_id="r", issue="I", description="d", repo="/x")
    st.updated_at = "1999-01-01T00:00:00+00:00"
    store.save(st)
    assert st.updated_at != "1999-01-01T00:00:00+00:00"


# ======================================================================================
# atomic_write
# ======================================================================================


def test_atomic_write_creates_parents_and_replaces(tmp_path):
    p = tmp_path / "a" / "b" / "file.txt"
    atomic_write(p, "one")
    assert p.read_text() == "one"
    atomic_write(p, "two ✓")
    assert p.read_text(encoding="utf-8") == "two ✓"
    assert [x.name for x in p.parent.iterdir()] == ["file.txt"]  # no temp files left


def test_atomic_write_failure_keeps_original_and_cleans_up(tmp_path):
    p = tmp_path / "file.txt"
    p.write_text("original")
    with pytest.raises(TypeError):
        atomic_write(p, 123)  # type: ignore[arg-type]  # fh.write(int) fails mid-write
    assert p.read_text() == "original"
    assert [x.name for x in tmp_path.iterdir()] == ["file.txt"]


# ======================================================================================
# ensure_git_exclude
# ======================================================================================


def test_ensure_git_exclude_normal_repo_and_idempotent(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    exclude = repo / ".git" / "info" / "exclude"
    before = exclude.read_text() if exclude.exists() else ""
    assert ensure_git_exclude(repo) is True
    text = exclude.read_text()
    assert text.startswith(before)
    assert f"{AUTOPILOT_DIR}/" in text.splitlines()
    # idempotent
    assert ensure_git_exclude(repo) is False
    assert ensure_git_exclude(repo) is False
    assert exclude.read_text().splitlines().count(f"{AUTOPILOT_DIR}/") == 1
    # and git really ignores the runtime dir
    (repo / ".autopilot").mkdir()
    (repo / ".autopilot" / "state.json").write_text("{}")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_ensure_git_exclude_accepts_existing_entry_without_slash(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("*.log\n.autopilot")  # no trailing newline, no trailing slash
    assert ensure_git_exclude(repo) is False
    assert exclude.read_text() == "*.log\n.autopilot"


def test_ensure_git_exclude_appends_on_a_new_line(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("*.log")  # no trailing newline
    assert ensure_git_exclude(repo) is True
    lines = exclude.read_text().splitlines()
    assert lines[0] == "*.log"
    assert ".autopilot/" in lines


def test_ensure_git_exclude_worktree_writes_common_exclude(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "wt-branch", str(wt))
    assert (wt / ".git").is_file()
    assert ensure_git_exclude(wt) is True
    assert ".autopilot/" in (repo / ".git" / "info" / "exclude").read_text().splitlines()
    (wt / ".autopilot").mkdir()
    (wt / ".autopilot" / "x").write_text("1")
    assert _git(wt, "status", "--porcelain", "--untracked-files=all") == ""


def test_ensure_git_exclude_not_a_repo(tmp_path):
    assert ensure_git_exclude(tmp_path) is False


# ======================================================================================
# Redaction
# ======================================================================================

SECRETS = {
    "anthropic": "sk-ant-" "api03-AbCdEfGhIjKlMnOpQrStUvWx-yz_0123456789",
    "openai_legacy": "sk-" "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "openai_project": "sk-" "proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "github_ghp": "gh" "p_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "github_gho": "gh" "o_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "github_pat": "github_" "pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
    "linear": "lin_" "api_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "slack_bot": "xo" "xb-1234567890-abcdefghijKLMN",
    "slack_user": "xo" "xp-1234567890-1234567890-abcdefABCDEF",
    "aws": "AK" "IAIOSFODNN7EXAMPLE",
    "jwt": "eyJhbGciOiJIUzI1NiJ9." "eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
}


@pytest.mark.parametrize("name,secret", sorted(SECRETS.items()))
def test_redact_vendor_tokens(name, secret):
    text = f"the worker printed {secret} in its output"
    out = redact(text)
    assert secret not in out
    assert out == f"the worker printed {R} in its output"


@pytest.mark.parametrize(
    "text,leak,expect",
    [
        ("Authorization: Bearer abcdefghijklmnop1234", "abcdefghijklmnop1234", "Authorization: " + R),
        ("curl -H 'Authorization: Basic dXNlcjpwYXNzd29yZA==' https://x", "dXNlcjpwYXNzd29yZA==", "curl -H 'Authorization: " + R + "' https://x"),
        ('curl -H "authorization: Bearer abc.def.ghi1234567" https://x', "abc.def.ghi1234567", 'curl -H "authorization: ' + R + '" https://x'),
        ("header x-api-key: abcd1234efgh5678", "abcd1234efgh5678", "header x-api-key: " + R),
        ("token is bearer abcdefghijklmnopqrstuvwxyz ok", "abcdefghijklmnopqrstuvwxyz", "token is Bearer " + R + " ok"),
    ],
)
def test_redact_auth_headers(text, leak, expect):
    out = redact(text)
    assert leak not in out
    assert out == expect


@pytest.mark.parametrize(
    "text,expect",
    [
        ("postgres://admin:hunter2@db.example.com:5432/app", f"postgres://{R}@db.example.com:5432/app"),
        ("git clone https://user:p4ssw0rd@github.com/o/r.git", f"git clone https://{R}@github.com/o/r.git"),
        ("redis://default:s3cr3t@cache:6379/0", f"redis://{R}@cache:6379/0"),
    ],
)
def test_redact_url_credentials(text, expect):
    assert redact(text) == expect


@pytest.mark.parametrize(
    "text,expect",
    [
        ("FOO_SECRET=supersecretvalue", f"FOO_SECRET={R}"),
        ("DATABASE_URL=postgres://localhost/app", f"DATABASE_URL={R}"),
        ('export STRIPE_SECRET_KEY="abc def"', f"export STRIPE_SECRET_KEY={R}"),
        ("DB_PASSWORD='hunter2'", f"DB_PASSWORD={R}"),
        ("  db_password = hunter2", f"  db_password = {R}"),
        ("ANTHROPIC_API_KEY=sk-ant-" "api03-xxxxxxxxxxxxxxxx", f"ANTHROPIC_API_KEY={R}"),
        ("SENTRY_DSN=https://abc@o1.ingest.sentry.io/1", f"SENTRY_DSN={R}"),
    ],
)
def test_redact_dotenv_lines(text, expect):
    assert redact(text) == expect


def test_redact_dotenv_multiline_file_keeps_harmless_lines():
    env = "NODE_ENV=production\nPORT=3000\nJWT_SECRET=abc123\nDATABASE_URL=postgres://u:p@h/db\nPUBLIC_URL=https://example.com\n"
    out = redact(env)
    assert "NODE_ENV=production" in out
    assert "PORT=3000" in out
    assert "PUBLIC_URL=https://example.com" in out
    assert f"JWT_SECRET={R}" in out
    assert f"DATABASE_URL={R}" in out
    assert "abc123" not in out and "u:p@h" not in out


@pytest.mark.parametrize(
    "text,expect",
    [
        ("run with API_TOKEN=abc123xyz ./deploy.sh", f"run with API_TOKEN={R} ./deploy.sh"),
        ('env GITHUB_TOKEN="quoted value" gh pr list', f"env GITHUB_TOKEN={R} gh pr list"),
        ("cd app && AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG pnpm deploy", f"cd app && AWS_SECRET_ACCESS_KEY={R} pnpm deploy"),
    ],
)
def test_redact_mid_line_assignments(text, expect):
    assert redact(text) == expect


@pytest.mark.parametrize(
    "text,leak",
    [
        ('{"password": "hunter2hunter2", "user": "bob"}', "hunter2hunter2"),
        ('{"client_secret": "abcdefgh", "id": 1}', "abcdefgh"),
        ('{"apiKey": "zzzzzzzz"}', "zzzzzzzz"),
        ('{"api_key": "abcdefgh"}', "abcdefgh"),
        ('{"refresh_token": "rrrrrrrr"}', "rrrrrrrr"),
    ],
)
def test_redact_json_secret_fields(text, leak):
    out = redact(text)
    assert leak not in out
    assert R in out
    assert '"user": "bob"' in redact('{"password": "hunter2hunter2", "user": "bob"}')


def test_redact_pem_private_key():
    pem = "key:\n-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabc\n-----END RSA PRIVATE KEY-----\ndone"
    assert redact(pem) == f"key:\n{R}\ndone"


@pytest.mark.parametrize(
    "text",
    [
        "Edit src/app.py and specs/007-older-feature/spec.md",
        "/home/dev/src/alpha/src/lib/auth/token-service.ts",
        "git push -u origin feature/007-faceted-filters",
        "git commit -m 'feat: add password reset flow'",
        "https://github.com/o/r/pull/1 merged as 3f2a9c1b4d5e6f708192a3b4c5d6e7f8091a2b3c",
        "uuid 123e4567-e89b-12d3-a456-426614174000",
        "The token count is 1234; see the password policy section of spec.md",
        "TOKEN_COUNT is a variable name",
        "task-1234 skip-this sk-short risk-assessment-notes-for-the-team",
        "REVIEW_SUMMARY blockers=0 nits=2 questions=1",
        "| A | origin_country column |",
        "- **FR-001**: System MUST store an API key per merchant",
        '{"input_tokens": 1234, "output_tokens": 56}',
        "",
    ],
)
def test_redact_leaves_ordinary_text_alone(text):
    assert redact(text) == text


def test_redact_handles_none_like_empty():
    assert redact("") == ""


@pytest.mark.parametrize(
    "text",
    [
        "key sk-ant-" "api03-AbCdEfGhIjKlMnOpQrStUvWx",
        "FOO_SECRET=x\nDATABASE_URL=postgres://u:p@h/db",
        "postgres://admin:hunter2@db/app",
        '{"password": "hunter2hunter2"}',
        "run API_TOKEN=abc ./x",
        "curl -H 'Authorization: Bearer abcdefghijkl' https://x",
    ],
)
def test_redact_is_idempotent(text):
    once = redact(text)
    assert redact(once) == once


def test_redact_is_idempotent_for_unquoted_authorization_header():
    once = redact("Authorization: Bearer abcdefghijkl https://api.example.com/x")
    assert once == "Authorization: [REDACTED] https://api.example.com/x"
    assert redact(once) == once  # second pass currently eats the URL


def test_redact_obj_recurses_through_containers():
    obj = {
        "a": "token sk-ant-" "api03-AbCdEfGhIjKlMnOp",
        "nested": {"list": ["gh" "p_AbCdEfGhIjKlMnOpQrStUvWx", 3, None, True], "ok": "plain"},
        "tuple": ("xo" "xb-1234567890-abcdefghij",),
        "n": 1.5,
    }
    out = redact_obj(obj)
    assert out["a"] == f"token {R}"
    assert out["nested"]["list"] == [R, 3, None, True]
    assert out["nested"]["ok"] == "plain"
    assert out["tuple"] == [R]
    assert out["n"] == 1.5
    # input not mutated
    assert obj["a"].startswith("token sk-ant")


def test_redact_obj_redacts_values_under_secret_keys():
    out = redact_obj({"password": "hunter2hunter2", "env": {"API_TOKEN": "abc123xyz"}})
    assert out["password"] == R
    assert out["env"]["API_TOKEN"] == R


# ======================================================================================
# EventLog / TranscriptWriter / RunPaths
# ======================================================================================


def test_event_log_writes_redacted_jsonl(tmp_path):
    echoed: list[str] = []
    log = EventLog(tmp_path / "logs" / "run.jsonl", run_id="run-1", echo=echoed.append)
    rec = log.emit(
        "turn",
        phase="clarify",
        summary="worker used gh" "p_AbCdEfGhIjKlMnOpQrStUvWxYz0123",
        tools=["Bash"],
        detail={"cmd": "export DATABASE_URL=postgres://u:pw@h/db", "path": Path("/tmp/x")},
    )
    raw = (tmp_path / "logs" / "run.jsonl").read_text(encoding="utf-8")
    assert "ghp_AbCd" not in raw and "u:pw@h" not in raw
    lines = raw.splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event"] == "turn" and parsed["run_id"] == "run-1" and parsed["phase"] == "clarify"
    assert parsed["summary"] == f"worker used {R}"
    assert parsed["detail"]["path"] == "/tmp/x"  # Paths serialised
    assert rec == parsed
    assert echoed and "turn" in echoed[0]


def test_event_log_echo_is_redacted(tmp_path):
    echoed: list[str] = []
    log = EventLog(tmp_path / "run.jsonl", echo=echoed.append)
    log.emit("turn", summary="worker used gh" "p_AbCdEfGhIjKlMnOpQrStUvWxYz0123")
    assert "ghp_" not in echoed[0]


def test_event_log_read_skips_corrupt_lines(tmp_path):
    log = EventLog(tmp_path / "run.jsonl")
    log.emit("a")
    with log.path.open("a") as fh:
        fh.write("{truncated\n\n")
    log.emit("b", phase="plan")
    events = log.read()
    assert [e["event"] for e in events] == ["a", "b"]
    assert EventLog(tmp_path / "missing.jsonl").read() == []


def test_transcript_writer_sanitises_label_and_redacts(tmp_path):
    tw = TranscriptWriter(tmp_path / "t", "judge 001/clarify:respond")
    assert tw.path.parent == tmp_path / "t"
    assert "/" not in tw.path.name and " " not in tw.path.name and ":" not in tw.path.name
    tw.write("assistant_text", {"text": "my key is lin_" "api_AbCdEfGhIjKlMnOpQrStUvWxYz"})
    rec = json.loads(tw.path.read_text().splitlines()[0])
    assert rec["kind"] == "assistant_text"
    assert rec["payload"]["text"] == f"my key is {R}"


def test_run_paths_layout(tmp_path):
    p = RunPaths(tmp_path)
    assert p.root == tmp_path / ".autopilot"
    assert p.state.name == "state.json" and p.lock.name == "lock" and p.events.name == "run.jsonl"
    assert p.post_ship_decisions == tmp_path / ".autopilot" / "decisions-post-ship.md"
    p.ensure()
    assert p.root.is_dir() and p.transcripts.is_dir()
