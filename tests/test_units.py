"""Unit tests for prompts.py, repo.py and report.py (no API calls, temp repos only)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from speckit_autopilot.config import PHASES
import sys
from speckit_autopilot.judge import JudgeRequest
from speckit_autopilot.phases import PHASE_SPECS
from speckit_autopilot.prompts import (
    JUDGE_SYSTEM_PROMPT,
    PHASE_REPLY_FORMATS,
    WORKER_SYSTEM_APPEND,
    RunContext,
    halt_comment_text,
    handoff_brief,
    judge_prompt,
    linear_closeout_prompt,
    linear_reader_prompt,
    repair_message,
    sanitize_reply,
)
from speckit_autopilot.repo import (
    CmdResult,
    Git,
    _workflow_runs_on_push_or_pr,
    detect_package_manager,
    detect_profile,
    detect_tooling,
    resolve_feature_dir,
    run_shell,
)
from speckit_autopilot.report import DecisionLog, render_decisions_md, resume_command, status_summary, write_final_report, write_halt_report
from speckit_autopilot.state import EventLog, RunLock, RunPaths, RunState, StateStore

from .fakeworld import make_repo


def sh(cwd: Path, *args: str) -> str:
    p = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Isolate ~/.claude (user-level skills/commands) from the developer's real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# ######################################################################################
# prompts.py
# ######################################################################################

ALLOWED = ["speckit-specify", "speckit.specify", "/cc-business-context"]


@pytest.mark.parametrize(
    "reply",
    [
        "/speckit-specify ENG-142: Add Organic product filters",
        "/speckit.specify Add filters",
        "  /speckit-specify padded  ",
        "/cc-business-context",
    ],
)
def test_sanitize_reply_allows_handed_back_commands(reply):
    safe, warn = sanitize_reply(reply, ALLOWED)
    assert warn is None
    assert safe == reply.strip()


@pytest.mark.parametrize(
    "reply,cmd",
    [
        ("/clear", "clear"),
        ("/compact keep the spec", "compact"),
        ("/model opus", "model"),
        ("/permissions", "permissions"),
        ("/speckit-implement", "speckit-implement"),
        ("/speckit-specifyX foo", "speckit-specifyX"),
        ("/cc-ship now", "cc-ship"),
        ("/", ""),
        ("  /exit", "exit"),
    ],
)
def test_sanitize_reply_neutralises_other_commands(reply, cmd):
    safe, warn = sanitize_reply(reply, ALLOWED)
    assert not safe.startswith("/")
    assert safe == "Decision: " + reply.strip()
    assert warn and f"/{cmd}" in warn


def test_sanitize_reply_builtins_blocked_even_when_listed():
    safe, warn = sanitize_reply("/clear", ["clear", "compact"])
    assert safe == "Decision: /clear" and warn


@pytest.mark.parametrize("reply", ["B", "yes", "Q1: A, Q2: B", "Approved, apply these edits now:\n1. x", "", "use /clear later"])
def test_sanitize_reply_passes_plain_text_unchanged(reply):
    assert sanitize_reply(reply, ALLOWED) == (reply, None)


def _ctx(tmp_path, **kw) -> RunContext:
    repo = tmp_path / "repo"
    fd = repo / "specs" / "116-organic-filters"
    base = dict(
        issue="ENG-142",
        description="Add Organic product filters",
        repo=repo,
        base_branch="main",
        spec_id="116",
        feature_branch="feature/116-organic-filters",
        feature_dir=fd,
        run_id="run-1",
        linear_title="Organic filters",
        decisions_path=fd / "decisions.md",
    )
    base.update(kw)
    return RunContext(**base)


def test_handoff_brief_contains_required_fields(tmp_path):
    text = handoff_brief(_ctx(tmp_path), "analyze", 2, failed_checks=["[coverage] FR-003 has no task", "[markers] spec.md has TODO"], purpose="repair round")
    assert text.startswith("AUTOPILOT HANDOFF BRIEF")
    assert "- Issue: ENG-142 — Organic filters" in text
    assert "- Feature description: Add Organic product filters" in text
    assert "Spec id: 116" in text and "branch: feature/116-organic-filters" in text and "base: main" in text
    assert "- Feature directory: specs/116-organic-filters" in text  # repo-relative
    assert "- Current phase: analyze (attempt 2) — repair round" in text
    assert "spec.md > plan.md > tasks.md" in text and ".specify/memory/constitution.md" in text
    assert "specs/116-organic-filters/decisions.md" in text and "do not re-open them" in text
    assert "  - [coverage] FR-003 has no task" in text and "  - [markers] spec.md has TODO" in text
    assert text.rstrip().endswith("Reply with the single word READY and nothing else.")


def test_handoff_brief_before_feature_start(tmp_path):
    text = handoff_brief(_ctx(tmp_path, spec_id=None, feature_branch=None, feature_dir=None, decisions_path=None, linear_title=None), "feature_start", 1)
    assert "- Issue: ENG-142\n" in text
    assert "(assigned by feature start)" in text and "(not created yet)" in text
    assert "(none yet)" in text
    assert "exit gate FAILED" not in text


def test_handoff_brief_absolute_path_outside_repo(tmp_path):
    other = tmp_path / "elsewhere" / "specs" / "x"
    assert str(other) in handoff_brief(_ctx(tmp_path, feature_dir=other), "plan", 1)


def _jreq(mode, phase="clarify", payload=None, decisions=None):
    ctx = {"issue": "ENG-142", "description": "Add Organic product filters", "linear_title": "Organic filters", "repo": "/r", "base_branch": "main", "feature_dir": "specs/116-x", "feature_branch": "feature/116-x", "repo_notes": "Bravo uses pnpm."}
    return JudgeRequest(mode=mode, phase=phase, context=ctx, payload=payload or {}, recent_decisions=decisions or [])


def test_judge_prompt_header_common_to_all_modes():
    for mode in ("respond", "answer", "verify", "route"):
        t = judge_prompt(_jreq(mode, "analyze"))
        assert t.startswith(f"MODE: {mode}\nPHASE: analyze\n")
        assert "ISSUE: ENG-142 — Organic filters" in t
        assert "FEATURE DIR: specs/116-x · BRANCH: feature/116-x" in t
        assert "spec.md > plan.md > tasks.md" in t
        assert "REPO-SPECIFIC NOTES" in t and "Bravo uses pnpm." in t


def test_judge_prompt_respond_mode():
    t = judge_prompt(_jreq("respond", "clarify", {"worker_text": "**Recommended:** Option A", "facts": {"unchecked_remaining": 3}, "history": "PIPELINE → worker: /speckit-clarify"}))
    assert "EXPECTED REPLY FORMAT FOR THIS PHASE: " + PHASE_REPLY_FORMATS["clarify"] in t
    assert "DETERMINISTIC FACTS" in t and '"unchecked_remaining": 3' in t
    assert "EARLIER IN THIS SESSION" in t and "/speckit-clarify" in t
    assert "<<<\n**Recommended:** Option A\n>>>" in t
    assert "status (needs_reply|done|failed)" in t


def test_judge_prompt_respond_clips_huge_worker_text():
    big = "A" * 30000 + "MIDDLE" + "Z" * 30000
    t = judge_prompt(_jreq("respond", "review", {"worker_text": big}))
    assert "characters omitted" in t and "MIDDLE" not in t
    assert t.count("A") >= 13000 and t.count("Z") >= 26000


def test_judge_prompt_answer_mode():
    qs = [{"question": "Storage?", "options": [{"label": "URL params (Recommended)"}, {"label": "Local storage"}], "multiSelect": False}]
    t = judge_prompt(_jreq("answer", "feature_start", {"questions": qs, "worker_text": "context before"}))
    assert "AskUserQuestion" in t and "QUESTIONS:" in t and "URL params (Recommended)" in t
    assert "WORKER CONTEXT BEFORE THE QUESTION:\ncontext before" in t


def test_judge_prompt_verify_mode():
    t = judge_prompt(_jreq("verify", "plan", {"gate": {"passed": True, "checks": [{"name": "plan-exists"}]}, "extra": "spec excerpt"}))
    assert f"DEFINITION OF DONE for plan: {PHASE_SPECS['plan'].dod}" in t
    assert "GATE RESULTS:" in t and "plan-exists" in t
    assert "EXTRA CONTEXT:\nspec excerpt" in t


def test_judge_prompt_route_mode():
    t = judge_prompt(_jreq("route", "review", {"failed": ["[tests] 2 failing"], "back_edges_used": 1, "max_back_edges": 3, "targets_used": ["plan"], "worker_text": "last words"}))
    assert "target_phase in [implement, tasks]" in t
    assert "implement: a code defect" in t and "tasks: missing work that no task covers" in t
    assert "Back-edges used so far: 1 / 3; already routed back to: ['plan']" in t
    assert "FAILED CHECKS:\n- [tests] 2 failing" in t
    assert "LAST WORKER OUTPUT:\nlast words" in t


def test_judge_prompt_route_for_phase_without_back_edges():
    t = judge_prompt(_jreq("route", "plan", {"failed": ["x"]}))
    assert "target_phase in [—]" in t and "ROOT CAUSE is upstream: none" in t


def test_judge_prompt_includes_only_the_last_25_decisions():
    decisions = []
    for i in range(30):
        if i % 10 == 3:
            decisions.append({"kind": "routing", "phase": "analyze", "action": "back", "target": "plan", "reason": f"reason-{i:02d}"})
        elif i % 10 == 7:
            decisions.append({"kind": "escalation", "phase": "plan", "reason": f"reason-{i:02d}"})
        else:
            decisions.append({"kind": "decision", "phase": "clarify", "item": f"item-{i:02d}", "chosen": f"choice-{i:02d}", "impact": "low"})
    t = judge_prompt(_jreq("respond", decisions=decisions))
    assert "RECENT DECISIONS (most recent last; stay consistent):" in t
    for i in range(5):
        assert f"-{i:02d}" not in t, i
    for i in range(5, 30):
        assert f"-{i:02d}" in t, i
    assert "- [clarify] item-29 => choice-29 (low)" in t
    assert "- [analyze] routing back -> plan: reason-23" in t
    assert "- [plan] ESCALATION: reason-27" in t
    # order: most recent last
    assert t.index("item-05") < t.index("item-29")


def test_judge_prompt_without_decisions_has_no_section():
    assert "RECENT DECISIONS" not in judge_prompt(_jreq("verify", "plan"))


def _closeout(**kw):
    base = dict(
        tool_prefix="mcp__linear__",
        issue="ENG-142",
        team="Acme",
        project="Demo",
        today="2026-11-10",
        run_id="run-1",
        pr_url="https://github.com/o/r/pull/7",
        merge_commit="0123456789abcdef0123",
        need_state=True,
        need_comment=True,
        need_overview=True,
        high_impact=["auth model: sessions"],
        escalations=["payments: refunds"],
        marker_style="html",
        shipped_summary_hint="Organic filters",
    )
    base.update(kw)
    return linear_closeout_prompt(**base)


def _steps(text: str) -> list[str]:
    return re.findall(r"(?m)^(\d+)\. ", text)


def test_closeout_all_steps_html_markers():
    t = _closeout()
    assert t.startswith("AUTOPILOT LINEAR CLOSE-OUT")
    assert _steps(t) == ["1", "2", "3"]
    assert "workflow state whose TYPE is `completed`" in t and 'team="Acme"' in t
    assert "mcp__linear__list_issue_statuses" in t and "mcp__linear__save_issue" in t
    assert "Add exactly ONE comment to ENG-142" in t and "**Autopilot close-out** · ENG-142 · run `run-1`" in t
    assert "https://github.com/o/r/pull/7" in t and "merge commit `0123456789ab`" in t
    assert "   - auth model: sessions" in t and "   - payments: refunds" in t
    assert "<!-- autopilot:status -->" in t and "<!-- /autopilot:status -->" in t
    assert "- **Date:** 2026-11-10" in t and "- **Just shipped:** ENG-142 — Organic filters" in t
    assert 'Update the project overview of "Demo"' in t


@pytest.mark.parametrize(
    "flags,present,absent",
    [
        (dict(need_state=False), ["Add exactly ONE comment", "Update the project overview"], ["TYPE is `completed`"]),
        (dict(need_comment=False), ["TYPE is `completed`", "Update the project overview"], ["Add exactly ONE comment"]),
        (dict(need_overview=False), ["TYPE is `completed`", "Add exactly ONE comment"], ["Update the project overview", "autopilot:status"]),
        (dict(need_state=False, need_comment=False), ["Update the project overview"], ["TYPE is `completed`", "Add exactly ONE comment"]),
    ],
)
def test_closeout_only_needed_steps_renumbered(flags, present, absent):
    t = _closeout(**flags)
    for p in present:
        assert p in t
    for a in absent:
        assert a not in t
    assert _steps(t) == [str(i) for i in range(1, len(present) + 1)]


def test_closeout_nothing_to_do():
    t = _closeout(need_state=False, need_comment=False, need_overview=False)
    assert "Nothing to change" in t and "Reply DONE" in t
    assert _steps(t) == []


def test_closeout_visible_marker_style():
    t = _closeout(marker_style="visible")
    assert "[autopilot:status]" in t and "[/autopilot:status]" in t
    assert "<!-- autopilot:status -->" not in t


def test_closeout_without_decisions_or_refs():
    t = _closeout(high_impact=[], escalations=[], pr_url=None, merge_commit=None, team=None, project=None)
    assert "   - none" in t and "PR n/a" in t and "merge commit `n/a`" in t
    assert 'team="<the issue team>"' in t and '"<the issue project>"' in t


def test_linear_reader_prompt():
    t = linear_reader_prompt("mcp__x__", "ENG-1", None)
    assert [l.split(".")[0] for l in t.splitlines() if re.match(r"^\d\.", l)] == ["1", "2", "3", "4"]
    assert 'mcp__x__get_issue with id="ENG-1"' in t and "skip if the issue has no project" in t
    assert 'get_project with query="Demo"' in linear_reader_prompt("mcp__x__", "ENG-1", "Demo")


def test_repair_message_and_halt_comment():
    m = repair_message("implement", ["[tests] failing", "[lint] 3 errors"], round_no=1, max_rounds=2)
    assert "round 1/2" in m and "1. [tests] failing\n2. [lint] 3 errors" in m and "do not disable or delete tests" in m
    c = halt_comment_text("ENG-1", "run-1", "budget", None, "python -m speckit_autopilot --resume")
    assert c.startswith("**Autopilot halted** · ENG-1 · run `run-1`") and "Phase: —" in c and "`python -m speckit_autopilot --resume`" in c


def test_system_prompts_keep_the_safety_rules():
    assert "Never edit" in WORKER_SYSTEM_APPEND and "constitution.md" in WORKER_SYSTEM_APPEND
    assert "git add -A" in WORKER_SYSTEM_APPEND and "--no-verify" in WORKER_SYSTEM_APPEND
    assert "never send /clear, /compact" in JUDGE_SYSTEM_PROMPT
    assert set(PHASE_REPLY_FORMATS) == set(PHASES)


# ######################################################################################
# repo.py
# ######################################################################################


def _touch(p: Path, text: str = "") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _plain_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    sh(path, "git", "init", "-q", "-b", "main")
    sh(path, "git", "config", "user.email", "t@example.com")
    sh(path, "git", "config", "user.name", "T")
    sh(path, "git", "config", "commit.gpgsign", "false")
    return path


def test_detect_profile_commands_skills_vs_commands_spelling(tmp_path, fake_home):
    repo = _plain_repo(tmp_path / "r")
    _touch(repo / ".claude/skills/cc-feature-start/SKILL.md")
    _touch(repo / ".claude/commands/cc-review.md")
    _touch(repo / ".claude/skills/speckit-plan/SKILL.md")
    _touch(repo / ".claude/commands/speckit.tasks.md")
    _touch(repo / ".claude/skills/speckit-analyze/SKILL.md")
    _touch(repo / ".claude/commands/speckit.analyze.md")
    _touch(fake_home / ".claude/skills/cc-ship/SKILL.md")  # user-level fallback
    prof = detect_profile(repo)
    assert prof.commands["feature_start"] == "/cc-feature-start"
    assert prof.commands["review"] == "/cc-review"
    assert prof.commands["plan"] == "/speckit-plan"
    assert prof.commands["tasks"] == "/speckit.tasks"
    assert prof.commands["analyze"] == "/speckit-analyze"  # repo skill (hyphen) wins
    assert prof.command_candidates["analyze"] == ["/speckit-analyze", "/speckit.analyze"]
    assert prof.commands["ship"] == "/cc-ship" and prof.command_files["ship"].startswith(str(fake_home))
    assert prof.command_files["tasks"].endswith(".claude/commands/speckit.tasks.md")
    for missing in ("specify", "clarify", "implement"):
        assert missing not in prof.commands


def test_detect_profile_config_overrides(tmp_path, fake_home):
    repo = _plain_repo(tmp_path / "r")
    _touch(repo / ".claude/skills/speckit-plan/SKILL.md")
    _touch(repo / "package.json", json.dumps({"scripts": {"test": "jest", "lint": "eslint"}}))
    prof = detect_profile(repo, cfg_commands={"plan": "/my-plan", "tasks": ""}, cfg_tooling={"test": "make test", "lint": None}, base_branch="develop")
    assert prof.commands["plan"] == "/my-plan" and "tasks" not in prof.commands
    assert prof.tooling["test"] == "make test"
    assert prof.tooling["lint"] == "npm run lint"  # None does not override detection
    assert prof.base_branch == "develop"


def test_detect_profile_speckit_metadata_and_paths(tmp_path, fake_home):
    repo = _plain_repo(tmp_path / "r")
    _touch(repo / ".specify/init-options.json", json.dumps({"speckit_version": "0.7.2", "script": "ps"}))
    _touch(repo / ".specify/memory/constitution.md", "# C")
    _touch(repo / ".specify/extensions.yml", "hooks: {}")
    prof = detect_profile(repo)
    assert prof.speckit_version == "0.7.2" and prof.script_type == "ps"
    assert prof.constitution_path == repo / ".specify/memory/constitution.md"
    assert prof.extensions_file == repo / ".specify/extensions.yml"
    assert prof.base_branch == "main"  # no origin -> default
    assert prof.remote_slug is None and not prof.has_ci and prof.package_manager is None


def test_detect_profile_tolerates_broken_init_options(tmp_path, fake_home):
    repo = _plain_repo(tmp_path / "r")
    _touch(repo / ".specify/init-options.json", "{broken")
    prof = detect_profile(repo)
    assert prof.speckit_version is None and prof.script_type == "sh"


SCRIPTS = {"test": "vitest", "test:ci": "vitest run", "lint": "eslint .", "type-check": "tsc --noEmit", "build": "next build"}


@pytest.mark.parametrize(
    "lock,pm,run",
    [
        ("pnpm-lock.yaml", "pnpm", "pnpm"),
        ("yarn.lock", "yarn", "yarn"),
        ("package-lock.json", "npm", "npm run"),
        ("bun.lockb", "bun", "bun run"),
        ("bun.lock", "bun", "bun run"),
        (None, "npm", "npm run"),
    ],
)
def test_package_manager_and_tooling_from_package_json(tmp_path, lock, pm, run):
    _touch(tmp_path / "package.json", json.dumps({"scripts": SCRIPTS}))
    if lock:
        _touch(tmp_path / lock)
    assert detect_package_manager(tmp_path) == pm
    t = detect_tooling(tmp_path, pm)
    assert t == {"test": f"{run} test:ci", "lint": f"{run} lint", "typecheck": f"{run} type-check", "build": f"{run} build"}


def test_package_manager_field_and_plain_npm_test(tmp_path):
    _touch(tmp_path / "package.json", json.dumps({"packageManager": "pnpm@9.1.0", "scripts": {"test": "jest"}}))
    assert detect_package_manager(tmp_path) == "pnpm"
    assert detect_tooling(tmp_path, "npm") == {"test": "npm test", "lint": None, "typecheck": None, "build": None}


@pytest.mark.parametrize("name", ["typecheck", "type-check", "check-types", "tsc", "types"])
def test_typecheck_script_aliases(tmp_path, name):
    _touch(tmp_path / "package.json", json.dumps({"scripts": {name: "tsc"}}))
    assert detect_tooling(tmp_path, "pnpm")["typecheck"] == f"pnpm {name}"


def test_python_projects(tmp_path):
    _touch(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    assert detect_package_manager(tmp_path) is None
    assert detect_tooling(tmp_path, None)["test"] == "python -m pytest -q"
    _touch(tmp_path / "uv.lock")
    assert detect_package_manager(tmp_path) == "uv"
    assert detect_tooling(tmp_path, "uv")["test"] == "uv run pytest -q"


def test_broken_package_json_is_tolerated(tmp_path):
    _touch(tmp_path / "package.json", "{not json")
    assert detect_package_manager(tmp_path) == "npm"
    assert detect_tooling(tmp_path, "npm") == {"test": None, "lint": None, "typecheck": None, "build": None}


def test_nothing_detected(tmp_path):
    assert detect_package_manager(tmp_path) is None
    assert detect_tooling(tmp_path, None) == {"test": None, "lint": None, "typecheck": None, "build": None}


@pytest.mark.parametrize(
    "yml,expect",
    [
        ("on: [push, pull_request]\njobs: {}\n", True),
        ("name: ci\non:\n  push:\n    branches: [main]\njobs: {}\n", True),
        ("on:\n  pull_request:\njobs: {}\n", False),  # PR-only CI never runs on the merge commit
        ("on: [pull_request]\n", False),
        ('"on":\n  push:\n', True),
        ("on: push\n", True),
        ("on: workflow_dispatch\njobs:\n  b:\n    runs-on: x\n", False),
        ("on:\n  schedule:\n    - cron: '0 0 * * *'\n", False),
        ("on:\n  pull_request_target:\n", False),
    ],
)
def test_workflow_trigger_detection(tmp_path, yml, expect):
    assert _workflow_runs_on_push_or_pr(_touch(tmp_path / "w.yml", yml)) is expect


def test_workflow_push_input_is_not_a_trigger(tmp_path):
    yml = "on: workflow_dispatch\njobs:\n  b:\n    runs-on: x\n    steps:\n      - uses: docker/build-push-action@v5\n        with:\n          push: true\n"
    assert _workflow_runs_on_push_or_pr(_touch(tmp_path / "w.yml", yml)) is False


def test_detect_profile_has_ci(tmp_path, fake_home):
    repo = _plain_repo(tmp_path / "r")
    _touch(repo / ".github/workflows/nightly.yml", "on:\n  schedule:\n    - cron: '0 0 * * *'\n")
    assert detect_profile(repo).has_ci is False
    assert detect_profile(repo).ci_workflows == ["nightly.yml"]
    _touch(repo / ".github/workflows/ci.yaml", "on: [push, pull_request]\n")
    prof = detect_profile(repo)
    assert prof.has_ci is True and prof.ci_workflows == ["ci.yaml", "nightly.yml"]


# ---------------------------------------------------------------- resolve_feature_dir


def test_resolve_feature_dir_direct_branch_dir(tmp_path):
    repo = _plain_repo(tmp_path / "r")
    fd = repo / "specs" / "007-organic-filters"
    fd.mkdir(parents=True)
    assert resolve_feature_dir(repo, "007-organic-filters") == fd


def test_resolve_feature_dir_numeric_prefix_of_prefixed_branch(tmp_path):
    repo = _plain_repo(tmp_path / "r")
    (repo / "specs" / "116-organic-product-filters").mkdir(parents=True)
    (repo / "specs" / "117-other").mkdir()
    assert resolve_feature_dir(repo, "feature/116-organic-filters") == repo / "specs" / "116-organic-product-filters"


def test_resolve_feature_dir_ambiguous_prefix(tmp_path):
    repo = _plain_repo(tmp_path / "r")
    (repo / "specs" / "116-a").mkdir(parents=True)
    (repo / "specs" / "116-b").mkdir()
    assert resolve_feature_dir(repo, "feature/116-c") is None


def test_resolve_feature_dir_no_match(tmp_path):
    repo = _plain_repo(tmp_path / "r")
    assert resolve_feature_dir(repo, "feature/116-x") is None  # no specs/ at all
    (repo / "specs").mkdir()
    assert resolve_feature_dir(repo, "spec-ABC-filters") is None  # key-style, no dir


def test_resolve_feature_dir_uses_current_branch(tmp_path):
    repo = _plain_repo(tmp_path / "r")
    _touch(repo / "README.md", "x")
    sh(repo, "git", "add", "README.md")
    sh(repo, "git", "commit", "-q", "-m", "init")
    sh(repo, "git", "checkout", "-q", "-b", "042-thing")
    (repo / "specs" / "042-thing").mkdir(parents=True)
    assert resolve_feature_dir(repo) == repo / "specs" / "042-thing"


def test_resolve_feature_dir_asks_speckit_script_first(tmp_path):
    repo = _plain_repo(tmp_path / "r")
    target = repo / "specs" / "999-from-script"
    target.mkdir(parents=True)
    (repo / "specs" / "001-branch").mkdir()
    _touch(repo / ".specify/scripts/bash/check-prerequisites.sh", f"echo 'noise'\necho '{json.dumps({'REPO_ROOT': str(repo), 'FEATURE_DIR': str(target)})}'\n")
    assert resolve_feature_dir(repo, "001-branch") == target


def test_resolve_feature_dir_script_pointing_nowhere_falls_back(tmp_path):
    repo = _plain_repo(tmp_path / "r")
    (repo / "specs" / "001-branch").mkdir(parents=True)
    _touch(repo / ".specify/scripts/bash/check-prerequisites.sh", f"echo '{json.dumps({'FEATURE_DIR': str(repo / 'specs' / 'missing')})}'\n")
    assert resolve_feature_dir(repo, "001-branch") == repo / "specs" / "001-branch"


# ---------------------------------------------------------------- Git helpers


@pytest.fixture
def origin_repo(tmp_path) -> Path:
    return make_repo(tmp_path)


def test_git_basic_queries(origin_repo):
    g = Git(origin_repo)
    assert g.current_branch() == "main"
    assert re.fullmatch(r"[0-9a-f]{40}", g.head())
    assert g.head("origin/main") == g.head()
    assert g.origin_head_branch() == "main"
    assert g.is_clean() and g.status_porcelain() == []
    assert g.ref_exists("refs/remotes/origin/main") and not g.ref_exists("refs/heads/nope")
    assert g.branch_exists_remote("main") and not g.branch_exists_remote("feature/x")
    assert g.remote_slug() is None  # local bare origin
    assert g.out("rev-parse", "--verify", "nope") == ""  # failures -> ""


def test_git_status_ignores_autopilot_dir(origin_repo):
    g = Git(origin_repo)
    _touch(origin_repo / ".autopilot" / "state.json", "{}")
    assert g.is_clean()
    _touch(origin_repo / "src" / "new.py", "x = 1\n")
    (origin_repo / "src" / "app.py").write_text("changed\n")
    assert not g.is_clean()
    assert sorted(g.uncommitted_names()) == ["src/app.py", "src/new.py"]


def test_git_uncommitted_names_are_unquoted(origin_repo):
    _touch(origin_repo / "docs" / "a b.md", "x")
    _touch(origin_repo / "docs" / "café.md", "x")
    assert sorted(Git(origin_repo).uncommitted_names()) == ["docs/a b.md", "docs/café.md"]


def test_git_branch_divergence_and_push(origin_repo):
    g = Git(origin_repo)
    base = g.head()
    sh(origin_repo, "git", "checkout", "-q", "-b", "feature/116-x")
    _touch(origin_repo / "src" / "filters.py", "def f(): ...\n")
    sh(origin_repo, "git", "add", "src/filters.py")
    sh(origin_repo, "git", "commit", "-q", "-m", "feat")
    assert g.current_branch() == "feature/116-x"
    assert g.ahead_behind("HEAD", "origin/main") == (1, 0)
    assert g.diff_names("origin/main") == ["src/filters.py"]
    assert g.is_ancestor(base, "HEAD") and not g.is_ancestor("HEAD", base)
    sh(origin_repo, "git", "push", "-q", "-u", "origin", "feature/116-x")
    assert g.fetch().ok
    assert g.branch_exists_remote("feature/116-x")
    assert g.ahead_behind("HEAD", "origin/feature/116-x") == (0, 0)


def test_git_origin_head_fallback(origin_repo):
    g = Git(origin_repo)
    sh(origin_repo, "git", "remote", "set-head", "origin", "-d")
    assert g.origin_head_branch() == "main"


@pytest.mark.parametrize(
    "url,slug",
    [
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo/", "owner/repo"),
        ("ssh://git@github.com/Owner-X/alpha.git", "Owner-X/alpha"),
        ("https://gitlab.com/owner/repo.git", None),
    ],
)
def test_git_remote_slug(tmp_path, url, slug):
    repo = _plain_repo(tmp_path / "r")
    sh(repo, "git", "remote", "add", "origin", url)  # no network: only config
    assert Git(repo).remote_slug() == slug


def test_cmd_result_ok_and_tail():
    assert CmdResult("x", 0).ok and not CmdResult("x", 1).ok and not CmdResult("x", 0, timed_out=True).ok
    r = CmdResult("x", 1, "\n".join(f"l{i}" for i in range(50)), "boom")
    assert r.tail(3) == "l48\nl49\nboom"


def test_run_shell_env_ci_and_timeout(tmp_path, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    r = run_shell('echo "ci=${CI-unset} cc=${CLAUDECODE:-unset} x=$X"', tmp_path, 30, env={"X": "42"})
    assert r.ok and r.stdout.strip() == "ci=unset cc=unset x=42"  # CI changes what suites do (Alpha's DB guard)
    t = run_shell("sleep 5", tmp_path, 0.3)
    assert t.timed_out and t.returncode == 124 and not t.ok


# ######################################################################################
# report.py
# ######################################################################################


@pytest.fixture
def run_env(tmp_path):
    repo = tmp_path / "repo"
    fd = repo / "specs" / "116-organic-filters"
    fd.mkdir(parents=True)
    paths = RunPaths(repo)
    paths.ensure()
    st = RunState(run_id="20261110-000000-abc123", issue="ENG-142", description="Add Organic product filters", repo=str(repo), feature_dir=str(fd), feature_branch="feature/116-organic-filters")
    return paths, st, fd


def _dec(item, chosen, rec=None, followed=None, impact="low"):
    from speckit_autopilot.judge import decision

    return decision(item, chosen, recommended=rec, followed=followed, impact=impact, rationale=f"why {item}", evidence="spec.md §FR-001")


def test_decision_log_renders_into_feature_dir(run_env):
    paths, st, fd = run_env
    log = DecisionLog(paths, st)
    log.add_judge_decisions("clarify", "respond", [_dec("Which field?", "Option B", rec="Option A", followed=False, impact="medium")], attempt=1)
    log.add_judge_decisions("feature_start", "answer", [_dec("Storage?", "URL params", rec="URL params", followed=True), _dec("Name?", "filters")])
    log.add_routing("analyze", "back", "plan", "design gap")
    log.add_routing("review", "back", "plan", "not allowed", applied=False, note="review may only route to implement/tasks")
    log.add_escalation("plan", "payments flow", item="refunds")
    log.add_judge_decisions("plan", "respond", [_dec("Auth?", "sessions", impact="high")])
    log.add_note("implement", "context reset at 70%")
    md = (fd / "decisions.md").read_text()
    assert md.startswith("# Autopilot decisions — ENG-142")
    assert "- Decisions: 4 (followed recommendation: 1, rejected recommendation: 1)" in md
    assert "- High-impact decisions: 1" in md and "- Escalations: 1" in md and "- Routing decisions: 2" in md
    # phases rendered in pipeline order regardless of logging order
    order = [md.index(f"## {p}\n") for p in ("feature_start", "clarify", "plan", "analyze", "implement", "review")]
    assert order == sorted(order)
    assert "### clarify-1: Which field?" in md and "- **Recommended:** Option A · followed: **no**" in md
    assert "### feature_start-2: Name?" in md and "- **Recommended:** — · followed: n/a" in md
    assert "- **Evidence:** spec.md §FR-001" in md
    assert "- **Routing:** back → plan: design gap" in md
    assert "(not applied: review may only route to implement/tasks)" in md
    assert "**Escalation:** payments flow (refunds)" in md
    assert "- Note: context reset at 70%" in md
    entries = log.entries()
    assert len(entries) == 8 and all(e["post_ship"] is False for e in entries)
    assert [e["kind"] for e in log.recent()] == ["decision", "decision", "decision", "routing", "routing", "escalation", "decision"]
    assert len(log.recent(2)) == 2


def test_decision_log_without_feature_dir_only_keeps_jsonl(tmp_path):
    paths = RunPaths(tmp_path)
    st = RunState(run_id="r", issue="I", description="d", repo=str(tmp_path))
    log = DecisionLog(paths, st)
    log.add_judge_decisions("feature_start", "respond", [_dec("a", "b")])
    assert log.feature_file() is None
    assert len(log.entries()) == 1
    assert not list(tmp_path.rglob("decisions.md"))


def test_decision_log_freeze_routes_post_ship_entries_to_autopilot_dir(run_env):
    paths, st, fd = run_env
    log = DecisionLog(paths, st)
    log.add_judge_decisions("review", "respond", [_dec("blocker", "fix")])
    frozen_path = log.freeze()
    assert frozen_path == fd / "decisions.md" and st.decisions_frozen
    before = frozen_path.read_bytes()
    feature_listing = sorted(p.name for p in fd.iterdir())
    log.add_judge_decisions("ship", "respond", [_dec("split commit?", "no")])
    log.add_escalation("linear", "comment failed")
    log.add_routing("linear", "halt", None, "Linear write denied")
    assert frozen_path.read_bytes() == before  # never touched again
    assert sorted(p.name for p in fd.iterdir()) == feature_listing  # nothing new in the feature dir
    post = paths.post_ship_decisions.read_text()
    assert post.startswith("# Autopilot decisions after ship (not committed) — ENG-142")
    assert "split commit?" in post and "comment failed" in post and "Linear write denied" in post
    assert "blocker" not in post
    assert [e["post_ship"] for e in log.entries()] == [False, True, True, True]


def test_decision_log_freeze_is_safe_to_call_twice(run_env):
    paths, st, fd = run_env
    log = DecisionLog(paths, st)
    log.add_judge_decisions("review", "respond", [_dec("a", "b")])
    log.freeze()
    log.add_judge_decisions("ship", "respond", [_dec("late", "x")])
    log.freeze()  # e.g. on resume: the late entry must still stay out of the feature dir
    assert "late" not in (fd / "decisions.md").read_text()


def test_decision_log_redacts(run_env):
    paths, st, fd = run_env
    log = DecisionLog(paths, st)
    log.add_judge_decisions("implement", "respond", [_dec("token", "use gh" "p_AbCdEfGhIjKlMnOpQrStUvWxYz012")])
    assert "ghp_" not in paths.decisions_jsonl.read_text() and "ghp_" not in (fd / "decisions.md").read_text()


def test_render_decisions_md_empty():
    st = RunState(run_id="r", issue="I", description="d", repo="/x")
    md = render_decisions_md(st, [])
    assert "- Decisions: 0 (followed recommendation: 0, rejected recommendation: 0)" in md and md.endswith("\n")


def test_resume_command_exact(tmp_path):
    st = RunState(run_id="r", issue="ENG-142", description="Add Organic product filters", repo=str(tmp_path / "my repo"))
    cmd = resume_command(st, "/venv/bin/python")
    assert cmd == f"/venv/bin/python -m speckit_autopilot --repo '{tmp_path / 'my repo'}' --issue ENG-142 'Add Organic product filters' --resume"
    st.flags = {"allow_escalations": True, "until": "ship"}
    assert resume_command(st, "/venv/bin/python").endswith("--resume --allow-escalations --until ship")
    assert f"{sys.executable} -m speckit_autopilot" in resume_command(st)


def test_write_halt_report_contents(run_env):
    paths, st, fd = run_env
    rec = st.phase("analyze")
    rec.status, rec.attempts, rec.repairs, rec.cost_usd, rec.duration_s = "failed", 2, 1, 1.25, 300
    st.phase("feature_start").status = "passed"
    st.total_cost_usd = 7.5
    st.escalations = [{"phase": "plan", "reason": "payments flow"}]
    st.flags = {"until": "ship"}
    last_judge = {"action": "halt", "target_phase": None, "reason": "cannot fix FR-003 coverage"}
    text = write_halt_report(paths, st, reason="routing exhausted", phase="analyze", failed_checks=["[coverage] FR-003 has no task", "[secret] token gh" "p_AbCdEfGhIjKlMnOpQrStUvWxYz012"], last_judge=last_judge, python="/venv/bin/python", priority="high")
    assert paths.report.read_text() == text
    assert text.startswith("# Autopilot HALTED — ENG-142")
    assert "**Priority:** HIGH" in text
    assert "**Why it stopped:** routing exhausted" in text
    assert "**Phase:** analyze · attempt 2 · repairs used 1" in text
    assert "Cost" not in text and "$7.50" not in text  # no costs: the runs use the subscription
    assert "- [coverage] FR-003 has no task" in text
    assert "ghp_" not in text  # redacted
    assert "```json" in text and "cannot fix FR-003 coverage" in text
    assert "- [plan] payments flow" in text
    assert "| analyze | failed | 2 | 1 | 5.0 min |" in text and "| ship | pending | 0 | 0 | — |" in text and "$1.25" not in text
    assert "```bash\n" + resume_command(st, "/venv/bin/python") + "\n```" in text
    assert "--until ship" in text and "--from <phase>" in text
    assert str(paths.events) in text


def test_write_halt_report_minimal(run_env):
    paths, st, _ = run_env
    text = write_halt_report(paths, st, reason="stopped", phase=None, failed_checks=[], last_judge=None)
    assert "**Phase:** — · attempt 0 · repairs used 0" in text
    assert "- (none recorded)" in text and "(no judge call recorded)" in text and "## Escalations\n\n- none" in text


def test_write_final_report(run_env):
    paths, st, _ = run_env
    st.ship = {"pr_url": "https://github.com/o/r/pull/7", "merge_commit": "abc", "ci": "success"}
    st.back_edges = [{"from": "analyze", "to": "plan", "reason": "gap"}]
    decisions = [{"kind": "decision", "recommended": "A", "followed_recommendation": True, "impact": "high"}, {"kind": "decision", "recommended": "A", "followed_recommendation": False}, {"kind": "decision"}]
    text = write_final_report(paths, st, [], decisions)
    assert text.startswith("# Autopilot COMPLETED — ENG-142")
    assert "PR: https://github.com/o/r/pull/7 · merge commit: abc · CI: success" in text
    assert "- analyze → plan: gap" in text
    assert "Total: 3 · followed recommendation: 1 · rejected recommendation: 1 · no recommendation offered: 1" in text
    assert "- High impact: 1" in text


def test_status_summary_no_run(tmp_path):
    assert status_summary(RunPaths(tmp_path)).startswith("No autopilot run found")


def test_status_summary_with_run(run_env):
    paths, st, _ = run_env
    st.current_phase = "plan"
    st.status = "halted"
    st.total_cost_usd = 3.5
    st.phase("feature_start").status = "passed"
    st.phase("clarify").status = "passed"
    st.phase("plan").status = "failed"
    st.phase("plan").attempts = 2
    st.halt = {"reason": "phase budget for plan exhausted"}
    st.back_edges = [{"to": "plan"}]
    StateStore(paths.state).save(st)
    log = EventLog(paths.events, st.run_id)
    log.emit("phase_start", phase="plan")
    log.emit("phase_start", phase="plan")
    log.emit("run_end", status="halted", summary="HALT: budget")
    RunLock(paths.lock).acquire(st.run_id)
    out = status_summary(paths)
    assert out.splitlines()[0] == "Autopilot ENG-142 — Add Organic product filters"
    assert f"status: halted (process alive, pid {os.getpid()})" in out
    assert "current phase: plan · elapsed" in out and "$" not in out
    assert "resets: 0 · back-edges: 1 · escalations: 0" in out
    assert re.search(r"(?m)^plan\s+failed\s+2\s+0$", out)
    assert "HALT: phase budget for plan exhausted" in out and str(paths.report) in out
    assert "last event:" in out and "run_end" in out
    assert "phase entries: {'plan': 2}" in out
    RunLock(paths.lock).release()
    assert "process alive" not in status_summary(paths)


# ######################################################################################
# fix-all: early route prompt, human-only tasks, trailer request, baseline + pause reports
# ######################################################################################


def test_judge_prompt_early_route_asks_root_cause_before_any_fix():
    t = judge_prompt(_jreq("route", "analyze", {"early": True, "failed": ["[zero-high] 1 HIGH finding(s): ['H1']"], "back_edges_used": 0, "max_back_edges": 3, "targets_used": [], "worker_text": "THE ANALYSIS"}))
    assert "first round; no fix has been applied yet" in t
    assert "target_phase in [clarify, plan, tasks]" in t and "plan: an architecture / design gap in plan.md" in t
    assert "- fix_here when the findings can be fixed in analyze itself" in t
    assert "BLOCKING FINDINGS / FAILED CHECKS:\n- [zero-high] 1 HIGH finding(s): ['H1']" in t
    assert "THE REPORT:\nTHE ANALYSIS" in t
    assert "used all its repair rounds" not in t and "forward is only valid" not in t


def test_judge_prompt_late_route_says_fix_here_is_no_longer_an_option():
    t = judge_prompt(_jreq("route", "review", {"failed": ["x"]}))
    assert "used all its repair rounds" in t and "fix_here is not an option any more" in t


def test_human_tasks_note():
    from speckit_autopilot.prompts import human_tasks_note

    assert human_tasks_note([]) == ""
    n = human_tasks_note(["T001", "T023"])
    assert n.startswith(" HUMAN-ONLY TASKS (reserved for a person): T001, T023. Never tick these; skip them.")


def test_implement_reply_format_mentions_human_only_tasks():
    assert "human_only_open" in PHASE_REPLY_FORMATS["implement"] and "never ask the worker to do or tick them" in PHASE_REPLY_FORMATS["implement"]


def test_analyze_trailer_request_is_not_itself_a_trailer():
    from speckit_autopilot.gates import parse_analyze_trailer
    from speckit_autopilot.prompts import ANALYZE_TRAILER_REQUEST

    assert "ANALYZE_SUMMARY critical=" in ANALYZE_TRAILER_REQUEST and "do not edit any file" in ANALYZE_TRAILER_REQUEST
    assert parse_analyze_trailer(ANALYZE_TRAILER_REQUEST) is None  # placeholders, not numbers
    assert parse_analyze_trailer("ANALYZE_SUMMARY critical=0 high=2 medium=1 low=0") == {"critical": 0, "high": 2, "medium": 1, "low": 0}


def test_closeout_lists_open_human_only_tasks_in_the_comment():
    t = _closeout(human_open=["T023 FOUNDER: sign the courier contract", "T028 FOUNDER: approve pricing"])
    assert 'Open human-only tasks (for a person; the pipeline did not do them):" listing these, verbatim:' in t
    assert "   - T023 FOUNDER: sign the courier contract\n   - T028 FOUNDER: approve pricing" in t
    assert t.index("Open human-only tasks") < t.index("Before posting, call list_comments")
    assert "human-only" not in _closeout(human_open=[]) and "human-only" not in _closeout(human_open=["T1 x"], need_comment=False)


def test_decision_log_renders_early_routing(run_env):
    paths, st, fd = run_env
    log = DecisionLog(paths, st)
    log.add_routing("analyze", "fix_here", None, "fix in analyze", early=True)
    log.add_routing("analyze", "back", "plan", "plan gap", applied=False, note="max_back_edges=0 already used", early=True)
    log.add_routing("analyze", "back", "plan", "late gap")
    rows = log.entries()
    assert [r.get("early") for r in rows] == [True, True, None]
    md = (fd / "decisions.md").read_text()
    assert "- **Routing (early, before any fix):** fix_here → —: fix in analyze" in md
    assert "- **Routing (early, before any fix):** back → plan (not applied: max_back_edges=0 already used): plan gap" in md
    assert "- **Routing:** back → plan: late gap" in md


def test_final_report_pause_items_human_tasks_and_baseline(run_env):
    paths, st, _ = run_env
    st.back_edges = [{"from": "review", "to": "implement", "reason": "defect", "early": True}]
    st.flags["test_baseline"] = {"sha": "0123456789abcdef", "cmd": "pytest -q", "failures": ["tests/a.py::test_x"], "note": "x"}
    st.flags["baseline_used"] = {"implement": "1 failing test(s), all already failing on base: tests/a.py::test_x"}
    text = write_final_report(paths, st, [], [], title="Autopilot PAUSED (1 human-only task)", human_open=["T001 FOUNDER GATE: approve"], pause_items=["`T001 FOUNDER GATE: approve`"], resume="python -m speckit_autopilot --resume")
    assert text.startswith("# Autopilot PAUSED (1 human-only task) — ENG-142")
    assert "## What a person must do before resuming\n\n- `T001 FOUNDER GATE: approve`\n\nThen resume:\n\n```bash\npython -m speckit_autopilot --resume\n```" in text
    assert "- review → implement (early, before any fix): defect" in text
    assert "## Human-only tasks (tasks.md, for a person)\n\n- [ ] T001 FOUNDER GATE: approve" in text
    assert "- Base `0123456789ab`: 1 test(s) already failing before this feature: tests/a.py::test_x" in text
    assert "- Tolerated at implement: 1 failing test(s), all already failing on base" in text
    # nothing of it when not in play
    plain = write_final_report(paths, RunState(run_id="r", issue="ENG-1", description="d", repo=str(paths.repo)), [], [])
    assert "What a person must do" not in plain and "Human-only tasks" not in plain and "Test baseline" not in plain
    assert "## Human-only tasks (tasks.md, for a person)\n\n- none open" in write_final_report(paths, st, [], [], human_open=[])


def test_halt_report_shows_an_unusable_baseline(run_env):
    paths, st, _ = run_env
    st.flags["test_baseline"] = {"sha": None, "cmd": "pnpm test", "failures": None, "note": "`pnpm test` failed on the base commit in a way that cannot be pinned to individual tests (pnpm -r stopped at the first failing package)"}
    st.flags["baseline_notes"] = {"implement": "baseline not applied: the base branch's failing tests are unknown"}
    text = write_halt_report(paths, st, reason="implement failed", phase="implement", failed_checks=["[test-passes] x"], last_judge=None)
    assert "## Test baseline (failures already on the base branch)" in text
    assert "- ⚠️ Base `?`: `pnpm test` failed on the base commit" in text and "(strict)" in text
    assert "- Not tolerated at implement: baseline not applied: the base branch's failing tests are unknown" in text


@pytest.mark.parametrize(
    "cmd,script,expect",
    [
        ("pnpm test", "vitest run", []),
        ("pnpm test", "pnpm -r test", ["pnpm -r stops at the first failing package"]),
        ("pnpm test", "pnpm -r --no-bail test", []),
        ("pnpm -r --if-present test && cd pipeline && pytest -q --cov-fail-under=85", None, ["chains commands", "pnpm -r stops"]),
        ("turbo run test --concurrency=1", None, ["turbo stops at the first failing package"]),
        ("pnpm test --continue", "turbo run test", []),
        ("pytest -x", None, ["bail flag"]),
        (None, None, ["no test command is configured"]),
    ],
)
def test_preflight_baseline_command_notes(tmp_path, cmd, script, expect):
    from speckit_autopilot.preflight import baseline_command_notes

    if script is not None:
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": script}}))
    notes = baseline_command_notes(cmd, tmp_path)
    assert len(notes) == len(expect), notes
    for n, e in zip(notes, expect):
        assert e in n


def test_judge_prompt_recent_decisions_mark_early_routing():
    t = judge_prompt(_jreq("respond", decisions=[{"kind": "routing", "phase": "analyze", "action": "fix_here", "target": None, "reason": "fix in analyze", "early": True}]))
    assert "- [analyze] routing (early check) fix_here -> —: fix in analyze" in t


# ######################################################################################
# pass 2: shared issues, status markers, the unattended contract, the sandbox line, STOPPED reports
# ######################################################################################


@pytest.mark.parametrize(
    "text,expect",
    [
        ('---\nspec: "113"\nlinear:\n  issueId: ENG-1366\n  shared: true\n---\n\n# Spec\n', {"issueId": "ENG-1366", "shared": "true"}),
        ("---\nlinear: { issueId: ENG-94 }\n---\n# x\n", {"issueId": "ENG-94"}),
        ("---\nlinear: {issueId: 'ENG-9', shared: yes}\n---\n", {"issueId": "ENG-9", "shared": "yes"}),
        ("---\nlinear:\n  issueId: ENG-1\n  lastSync: 2026-11-03T14:47:35Z  # synced\n  # a comment\ntitle: x\n---\n", {"issueId": "ENG-1", "lastSync": "2026-11-03T14:47:35Z"}),
        ("---\ntitle: no linear here\n---\nlinear:\n  shared: true\n", {}),  # outside the front matter
        ("# Spec\nlinear: { shared: true }\n", {}),  # no front matter at all
        ("---\nmeta:\n  linear: { shared: true }\n---\n", {}),  # only the top-level key counts
        ("", {}),
    ],
)
def test_linear_front_matter(text, expect):
    from speckit_autopilot.runner import linear_front_matter

    assert linear_front_matter(text) == expect


def test_unescape_status_markers_reads_linear_escaped_visible_markers():
    from speckit_autopilot.gates import status_block
    from speckit_autopilot.runner import unescape_status_markers

    stored = "# P\n\n## Current status\n\n\\[autopilot:status\\]\n- **Date:** 2026-11-10\n\\[/autopilot:status\\]\n- tick \\[X\\] stays escaped\n"
    assert status_block(stored)[0] is None
    fixed = unescape_status_markers({"id": "p", "description": stored})
    block, style = status_block(fixed["description"])
    assert style == "visible" and "2026-11-10" in block and "\\[X\\]" in fixed["description"]
    assert unescape_status_markers(None) is None and unescape_status_markers({"content": "x"}) == {"content": "x"}


def test_shared_issue_gate_denies_saving_the_issue_and_nothing_else(tmp_path):
    from speckit_autopilot.policy import PolicyContext
    from speckit_autopilot.runner import SharedIssueGate

    events = []
    gate = SharedIssueGate(ctx=PolicyContext(phase="linear", repo=tmp_path), log_event=lambda ev, **kw: events.append((ev, kw)), issue="ENG-1366")
    d = gate.check("mcp__linear__save_issue", {"id": "ENG-1366", "state": "Done"}, "hook")
    assert not d.allow and d.rule == "shared-issue" and "ENG-1366 is a shared Linear issue" in d.reason
    assert gate.check("mcp__linear__save_comment", {"issueId": "ENG-1366", "body": "x"}, "hook").allow
    assert gate.check("mcp__linear__save_project", {"id": "p", "patch": []}, "hook").allow
    assert gate.check("mcp__linear__get_issue", {"id": "ENG-1366"}, "hook").allow
    assert [ev for ev, _ in events].count("denial") == 1 and events[0][1]["rule"] == "shared-issue"


def test_closeout_for_a_shared_issue_never_moves_the_status_and_names_the_spec():
    t = _closeout(shared="spec.md front matter linear.shared: true", spec_label="spec 113 (specs/113-refuse-by-name)")
    assert "workflow state whose TYPE is `completed`" not in t and "save_issue with id" not in t
    assert "ENG-142 is a SHARED issue (spec.md front matter linear.shared: true): it also tracks other specs. Never change its status" in t
    assert "Say that this comment is for spec 113 (specs/113-refuse-by-name) only" in t
    assert t.index("SHARED issue") < t.index("1. Add exactly ONE comment")  # the comment is step 1: no status step
    plain = _closeout()
    assert "SHARED" not in plain and "1. Move ENG-142 to its team's workflow state whose TYPE is `completed`" in plain


def test_closeout_marker_fallback_accepts_escaped_visible_markers():
    t = _closeout()
    assert "re-save the block using the visible markers `[autopilot:status]` / `[/autopilot:status]` instead" in t
    assert "may show those as `\\[autopilot:status\\]` / `\\[/autopilot:status\\]`; that is fine" in t
    assert "use the marker text exactly as get_project returns it" in t


def test_linear_prompts_forbid_invented_parameters():
    from speckit_autopilot.prompts import ONLY_LISTED_PARAMS

    assert "user_intent" in ONLY_LISTED_PARAMS
    assert ONLY_LISTED_PARAMS in linear_reader_prompt("mcp__claude_ai_Linear__", "ENG-1", None)
    assert ONLY_LISTED_PARAMS in _closeout()


def test_worker_system_append_starts_with_the_unattended_line():
    from speckit_autopilot.prompts import UNATTENDED_LINE

    assert WORKER_SYSTEM_APPEND.splitlines()[0] == UNATTENDED_LINE == "AUTOPILOT UNATTENDED MODE"
    assert "You are running UNATTENDED" in WORKER_SYSTEM_APPEND.splitlines()[1]


@pytest.mark.parametrize(
    "mode,available,status,text",
    [
        ("auto", (True, "macOS Seatbelt (sandbox-exec)"), "PASS", "auto: active (macOS Seatbelt (sandbox-exec)) for analyze/implement/review (clarify/plan/tasks only in a session of their own); not for feature_start, linear, ship"),
        ("on", (True, "macOS Seatbelt (sandbox-exec)"), "PASS", "on: active"),
        ("auto", (False, "no Bash sandbox on win32"), "WARN", "auto: not available here (no Bash sandbox on win32)"),
        ("on", (False, "bwrap not found"), "FAIL", "sandbox = on but the OS sandbox is not available here (bwrap not found)"),
        ("off", (True, "macOS Seatbelt (sandbox-exec)"), "INFO", "off: worker Bash commands run without the OS sandbox"),
    ],
)
def test_preflight_sandbox_line(monkeypatch, mode, available, status, text):
    from speckit_autopilot import preflight
    from speckit_autopilot.config import Config

    monkeypatch.setattr(preflight, "sandbox_available", lambda: available)
    name, st, detail = preflight.sandbox_item(Config(sandbox=mode))
    assert (name, st) == ("sandbox", status) and text in detail, detail


def test_preflight_sandbox_line_names_extra_hosts(monkeypatch):
    from speckit_autopilot import preflight
    from speckit_autopilot.config import Config

    monkeypatch.setattr(preflight, "sandbox_available", lambda: (True, "ok"))
    assert "extra hosts ['binaries.prisma.sh']" in preflight.sandbox_item(Config(sandbox="auto", sandbox_network_domains=["binaries.prisma.sh"]))[2]


def test_stopped_report_title_and_sandbox_line(run_env):
    from speckit_autopilot.report import sandbox_text

    paths, st, _ = run_env
    text = write_halt_report(paths, st, reason="stopped by SIGTERM", phase="implement", failed_checks=[], last_judge=None, status="stopped")
    assert text.startswith("# Autopilot STOPPED — ENG-142") and "--resume" in text
    assert sandbox_text(st) == "—"
    st.flags["sandbox"] = {"mode": "auto", "active": True, "detail": "macOS Seatbelt (sandbox-exec)"}
    assert sandbox_text(st) == "auto: active (macOS Seatbelt (sandbox-exec)); feature_start, ship and linear run unsandboxed"
    st.flags["sandbox"] = {"mode": "auto", "active": False, "detail": "no Bash sandbox on win32"}
    st.linear["shared"] = "listed in shared_issues"
    final = write_final_report(paths, st, [], [])
    assert "- OS sandbox for worker Bash: auto: not active (no Bash sandbox on win32)" in final
    assert "shared issue (listed in shared_issues): status not changed" in final


def test_run_shell_kills_itself_when_a_stop_came_just_before_it_started(tmp_path):
    import time as _time

    t0 = _time.monotonic()
    res = run_shell("sleep 20", tmp_path, 30, should_stop=lambda: True)
    assert res.returncode != 0 and not res.timed_out and _time.monotonic() - t0 < 10
    assert run_shell("echo fine", tmp_path, 30, should_stop=lambda: False).stdout.strip() == "fine"
