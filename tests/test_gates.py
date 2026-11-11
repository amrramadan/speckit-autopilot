"""Unit tests for every deterministic gate in speckit_autopilot/gates.py, driven by REAL artifact formats.

Fixtures (tests/fixtures/) are synthetic spec.md / plan.md / research.md / tasks.md / constitution.md files and
/speckit.analyze and code-review outputs, modelled line-for-line on real artifacts (Spec Kit 0.7.1, 1.2.1 and 1.2.3
dialects) with the content replaced. Each fixture describes its shape in a leading ``<!-- fixture: … -->`` comment;
"DERIVED" fixtures name the base fixture and the single edit that turns a passing artifact into a failing variant.
spec/template-*.spec-template.md are byte-identical copies of the upstream Spec Kit spec templates.

Git-backed gates run against temp repos with a bare local "origin" (tests/fakeworld.make_repo). gh is always
a fake. No network, no SDK.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.fakeworld import ANALYZE_REPORT, FakeLinear, make_repo, sh
from speckit_autopilot import gates as G
from speckit_autopilot.prompts import REVIEW_TRAILER_REQUEST
from speckit_autopilot.repo import CmdResult

FIX = Path(__file__).parent / "fixtures"


def fx(rel: str) -> str:
    return (FIX / rel).read_text(encoding="utf-8")


def by_name(result: G.GateResult) -> dict[str, G.Check]:
    return {c.name: c for c in result.checks}


def failed_names(result: G.GateResult) -> set[str]:
    return {c.name for c in result.checks if not c.passed and not c.warn_only}


def feature_dir(tmp: Path, **files: str) -> Path:
    """specs/<x>/ under ``tmp`` with each keyword (spec/plan/research/tasks) copied from the named fixture."""
    d = tmp / "specs" / "115-feature"
    d.mkdir(parents=True, exist_ok=True)
    for kind, rel in files.items():
        (d / f"{kind.replace('_', '-')}.md").write_text(fx(rel), encoding="utf-8")
    return d


def put_constitution(repo: Path, rel: str | None) -> None:
    p = repo / ".specify" / "memory" / "constitution.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    if rel is None:
        p.unlink(missing_ok=True)
    else:
        p.write_text(fx(rel), encoding="utf-8")


def git(repo: Path, *args: str) -> str:
    return sh(repo, "git", *args)


def commit_all(repo: Path, msg: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def second_clone(tmp: Path, name: str = "other") -> Path:
    other = tmp / name
    sh(tmp, "git", "clone", "-q", str(tmp / "origin.git"), str(other))
    git(other, "config", "user.email", "o@example.com")
    git(other, "config", "user.name", "Other")
    git(other, "config", "commit.gpgsign", "false")
    return other


# ======================================================================================================================
# Result plumbing
# ======================================================================================================================


def test_gate_result_warn_only_never_fails_and_summary_lists_failures():
    r = G.GateResult("x").add("a", True, "ok").add("b", False, "bad").add("c", False, "soft", warn_only=True)
    assert not r.passed
    assert r.failed() == ["[b] bad"]
    assert r.warnings() == ["[c] soft"]
    assert r.summary().startswith("x: FAIL") and "[b] bad" in r.summary()
    assert G.GateResult("y").add("c", False, "soft", warn_only=True).passed
    assert r.to_dict()["passed"] is False and len(r.to_dict()["checks"]) == 3


def test_fingerprint_ignores_supervisor_decisions_file_and_tracks_content(tmp_path):
    d = feature_dir(tmp_path, spec="spec/bravo-115.spec.md")
    a = G.fingerprint([d], tmp_path)
    (d / "decisions.md").write_text("supervisor-owned")
    assert G.fingerprint([d], tmp_path) == a
    (d / "spec.md").write_text(fx("spec/bravo-115.spec.md") + "\nedit\n")
    assert G.fingerprint([d], tmp_path) != a


# ======================================================================================================================
# feature_start: preconditions (clean / on base / not behind origin / constitution)
# ======================================================================================================================


@pytest.fixture
def repo(tmp_path) -> Path:
    r = make_repo(tmp_path)
    put_constitution(r, "constitution/delta.constitution.md")
    commit_all(r, "real constitution")
    git(r, "push", "-q", "origin", "main")
    return r


def test_preconditions_pass_on_clean_synced_base_with_filled_constitution(repo):
    res = G.preconditions_feature_start(repo, "main")
    assert res.passed, res.failed()
    assert set(by_name(res)) == {"clean-tree", "on-base", "fetch", "base-up-to-date", "constitution"}


def test_preconditions_dirty_tree_untracked_and_modified(repo):
    (repo / "notes.txt").write_text("untracked")
    assert failed_names(G.preconditions_feature_start(repo, "main")) == {"clean-tree"}
    (repo / "notes.txt").unlink()
    (repo / "src" / "app.py").write_text("print('changed')\n")
    res = G.preconditions_feature_start(repo, "main")
    assert failed_names(res) == {"clean-tree"}
    assert "src/app.py" in by_name(res)["clean-tree"].detail


def test_preconditions_ignore_supervisor_dot_autopilot_dir(repo):
    (repo / ".autopilot").mkdir()
    (repo / ".autopilot" / "state.json").write_text("{}")
    assert G.preconditions_feature_start(repo, "main").passed


def test_preconditions_not_on_base(repo):
    git(repo, "checkout", "-q", "-b", "feature/115-x")
    res = G.preconditions_feature_start(repo, "main")
    assert "on-base" in failed_names(res)
    assert "'feature/115-x'" in by_name(res)["on-base"].detail


def test_preconditions_detached_head_at_origin_base_allowed_only_when_asked(repo):
    git(repo, "checkout", "-q", "--detach", "origin/main")
    assert "on-base" in failed_names(G.preconditions_feature_start(repo, "main"))
    assert G.preconditions_feature_start(repo, "main", allow_detached_at_base=True).passed


def test_preconditions_behind_origin_detected_only_after_fetch(tmp_path, repo):
    other = second_clone(tmp_path)
    (other / "README.md").write_text("someone else pushed\n")
    commit_all(other, "upstream change")
    git(other, "push", "-q", "origin", "main")
    stale = G.preconditions_feature_start(repo, "main", fetch=False)
    assert stale.passed, "without a fetch the stale remote-tracking ref hides the gap (why fetch=True is the default)"
    res = G.preconditions_feature_start(repo, "main")
    assert failed_names(res) == {"base-up-to-date"}
    assert "(0, 1)" in by_name(res)["base-up-to-date"].detail


def test_preconditions_ahead_of_origin(repo):
    (repo / "local.txt").write_text("unpushed\n")
    commit_all(repo, "local only")
    res = G.preconditions_feature_start(repo, "main")
    assert failed_names(res) == {"base-up-to-date"}
    assert "(1, 0)" in by_name(res)["base-up-to-date"].detail


def test_preconditions_fetch_failure_is_reported(repo):
    git(repo, "remote", "set-url", "origin", str(repo.parent / "no-such-origin.git"))
    assert "fetch" in failed_names(G.preconditions_feature_start(repo, "main"))


@pytest.mark.parametrize(
    "constitution, ok, why",
    [
        ("constitution/delta.constitution.md", True, "filled in"),
        ("constitution/bravo.constitution.md", True, "filled in"),
        ("constitution/alpha.constitution.md", True, "filled in"),
        ("constitution/template.constitution.md", False, "[PROJECT_NAME]"),
        (None, False, "missing"),
    ],
)
def test_constitution_status_real_files(tmp_path, constitution, ok, why):
    put_constitution(tmp_path, constitution)
    got, detail = G.constitution_status(tmp_path)
    assert got is ok and why in detail


def test_constitution_status_rejects_a_stub(tmp_path):
    p = tmp_path / ".specify" / "memory" / "constitution.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\nversion: 1\n---\n# Constitution\n\nTBD.\n")
    ok, detail = G.constitution_status(tmp_path)
    assert not ok and "looks empty" in detail


def test_preconditions_template_constitution_fails(repo):
    put_constitution(repo, "constitution/template.constitution.md")
    commit_all(repo, "template")
    git(repo, "push", "-q", "origin", "main")
    assert failed_names(G.preconditions_feature_start(repo, "main")) == {"constitution"}


# ======================================================================================================================
# feature_start: exit gate
# ======================================================================================================================


def _start_feature(repo: Path, spec_fixture: str | None, branch: str = "feature/115-test-seams") -> Path:
    git(repo, "checkout", "-q", "-b", branch)
    fd = repo / "specs" / "115-test-seams"
    if spec_fixture:
        fd.mkdir(parents=True)
        (fd / "spec.md").write_text(fx(spec_fixture))
    return fd


def test_gate_feature_start_real_spec_passes(repo):
    (repo / ".specify" / "templates" / "spec-template.md").write_text(fx("spec/template-1.2.3.spec-template.md"))
    fd = _start_feature(repo, "spec/bravo-115.spec.md")
    res = G.gate_feature_start(repo, "main", fd, "feature/115-test-seams")
    assert res.passed, res.failed()
    assert res.facts["branch"] == "feature/115-test-seams"


def test_gate_feature_start_branch_name_mismatch(repo):
    fd = _start_feature(repo, "spec/bravo-115.spec.md", branch="115-test-seams")  # P's git ext names it NNN-slug
    res = G.gate_feature_start(repo, "main", fd, "feature/115-test-seams")
    assert failed_names(res) == {"branch-name"}
    assert G.gate_feature_start(repo, "main", fd, None).passed


def test_gate_feature_start_still_on_base(repo):
    fd = repo / "specs" / "115-test-seams"
    fd.mkdir(parents=True)
    (fd / "spec.md").write_text(fx("spec/bravo-115.spec.md"))
    assert "feature-branch" in failed_names(G.gate_feature_start(repo, "main", fd, None))


@pytest.mark.parametrize("template", ["spec/template-1.2.3.spec-template.md", "spec/template-0.7.1.spec-template.md"])
def test_gate_feature_start_spec_is_the_untouched_template(repo, template):
    (repo / ".specify" / "templates" / "spec-template.md").write_text(fx(template))
    fd = _start_feature(repo, template)
    res = G.gate_feature_start(repo, "main", fd, None)
    assert failed_names(res) == {"spec-not-template", "spec-no-placeholders"}
    assert "[FEATURE NAME]" in by_name(res)["spec-no-placeholders"].detail


def test_gate_feature_start_placeholders_left_in_an_edited_spec(repo):
    fd = _start_feature(repo, "spec/bravo-115.spec.md")
    (fd / "spec.md").write_text(fx("spec/bravo-115.spec.md").replace("**Created**: 2026-11-08", "**Created**: [DATE]"))
    res = G.gate_feature_start(repo, "main", fd, None)
    assert failed_names(res) == {"spec-no-placeholders"}


def test_gate_feature_start_missing_dir_and_spec(repo):
    fd = _start_feature(repo, None)
    assert failed_names(G.gate_feature_start(repo, "main", fd, None)) == {"feature-dir", "spec-exists"}
    fd.mkdir(parents=True)
    assert failed_names(G.gate_feature_start(repo, "main", fd, None)) == {"spec-exists"}


# ======================================================================================================================
# clarify
# ======================================================================================================================


@pytest.mark.parametrize(
    "fixture, today",
    [
        ("spec/bravo-115.spec.md", "2026-11-08"),  # 1.2.3: front matter, wrapped "→ A:", non-Q bullet
        ("spec/alpha-mktc.spec.md", "2026-09-27"),  # "### Session 2026-09-27 (operator answers)", bold Q/A
        ("spec/charlie-035-resolved-with-session.spec.md", "2026-11-10"),  # "## Resolved Clarifications" + "— suffix"
        ("spec/bravo-115-prose-mentions.spec.md", "2026-11-08"),  # backticked + fenced marker mentions
    ],
)
def test_gate_clarify_real_sessions_pass(tmp_path, fixture, today):
    res = G.gate_clarify(feature_dir(tmp_path, spec=fixture), today)
    assert res.passed, res.failed()


def test_gate_clarify_requires_todays_session(tmp_path):
    fd = feature_dir(tmp_path, spec="spec/bravo-115.spec.md")
    res = G.gate_clarify(fd, "2026-11-10")
    assert failed_names(res) == {"clarifications-session"}
    assert "2026-11-08" in by_name(res)["clarifications-session"].detail  # lists the sessions it saw


def test_gate_clarify_nothing_to_clarify_needs_no_session(tmp_path):
    fd = feature_dir(tmp_path, spec="spec/bravo-115.spec.md")
    assert G.gate_clarify(fd, "2026-11-10", nothing_to_clarify=True).passed


def test_gate_clarify_resolved_clarifications_without_session(tmp_path):
    fd = feature_dir(tmp_path, spec="spec/charlie-035-resolved.spec.md")
    assert failed_names(G.gate_clarify(fd, "2026-11-10")) == {"clarifications-session"}
    assert G.gate_clarify(fd, "2026-11-10", nothing_to_clarify=True).passed


def test_gate_clarify_session_outside_the_clarifications_section_does_not_count(tmp_path):
    text = fx("spec/bravo-115.spec.md").replace(
        "## User Scenarios & Testing *(mandatory)*", "## User Scenarios & Testing *(mandatory)*\n\n### Session 2026-11-10\n"
    )
    fd = feature_dir(tmp_path)
    (fd / "spec.md").write_text(text)
    assert failed_names(G.gate_clarify(fd, "2026-11-10")) == {"clarifications-session"}


def test_gate_clarify_date_must_be_the_session_date_not_a_prefix(tmp_path):
    fd = feature_dir(tmp_path, spec="spec/bravo-115.spec.md")
    assert not G.gate_clarify(fd, "2026-11-0").passed


@pytest.mark.parametrize(
    "fixture, count",
    [
        ("spec/alpha-013-open-markers.spec.md", 3),  # 2 real "[NEEDS CLARIFICATION: …]" + 1 real bare reference
        ("spec/bravo-115-wrapped-marker.spec.md", 1),  # question hard-wrapped onto the next line
        ("spec/bravo-115-bare-marker.spec.md", 1),  # bare bracket form in spec.md
    ],
)
def test_gate_clarify_unresolved_markers_fail(tmp_path, fixture, count):
    res = G.gate_clarify(feature_dir(tmp_path, spec=fixture), "2026-11-08")
    assert "no-needs-clarification" in failed_names(res)
    assert by_name(res)["no-needs-clarification"].detail.startswith(f"{count} ")


def test_gate_clarify_missing_spec(tmp_path):
    res = G.gate_clarify(feature_dir(tmp_path), "2026-11-10", nothing_to_clarify=True)
    assert "spec-exists" in failed_names(res)


def test_clarify_reported_nothing_uses_the_skill_phrase():
    assert G.clarify_reported_nothing("No critical ambiguities detected worth formal clarification. Suggest `/speckit-plan`.")
    assert not G.clarify_reported_nothing("**Question:** Which origin field should drive the filter? (FR-001)")
    assert not G.clarify_reported_nothing("")


def test_nc_markers_modes():
    text = fx("spec/alpha-013-open-markers.spec.md")
    assert len(G.nc_markers(text, bare=True)) == 3
    assert len(G.nc_markers(text, bare=False)) == 2  # plan/research mode ignores the bare mention
    assert G.nc_markers("No `[NEEDS CLARIFICATION: x]` here and ```\n[NEEDS CLARIFICATION: y]\n```", bare=True) == []


# ======================================================================================================================
# plan (+ research)
# ======================================================================================================================


@pytest.mark.parametrize(
    "plan, research",
    [
        ("plan/bravo-115.plan.md", "research/bravo-115.research.md"),
        ("plan/charlie-035.plan.md", "research/alpha-013.research.md"),  # "**PASS** —", "**N/A — …**", design rows
        ("plan/alpha-obs.plan.md", "research/alpha-017.research.md"),  # "FAIL" mid-cell in a PASS row
        ("plan/bravo-076.plan.md", "research/bravo-115.research.md"),  # "Violations block merge" principle cell
        ("plan/alpha-pgw.plan.md", "research/bravo-115.research.md"),  # "### NEEDS CLARIFICATION — resolved"
        ("plan/alpha-mkt.plan.md", "research/bravo-115.research.md"),  # "PASS, see Complexity Tracking"
        ("plan/bravo-115-violation-words.plan.md", "research/bravo-115.research.md"),
        ("plan/bravo-115-justified-fail.plan.md", "research/bravo-115.research.md"),
    ],
)
def test_gate_plan_real_plans_pass(tmp_path, plan, research):
    res = G.gate_plan(feature_dir(tmp_path, plan=plan, research=research))
    assert res.passed, res.failed()
    assert by_name(res)["constitution-check"].passed


def test_gate_plan_research_prose_mentions_are_not_markers(tmp_path):
    """Regression: 25 of 277 real plans failed because research.md SAYS 'No `[NEEDS CLARIFICATION]` remain'."""
    for research in ("research/alpha-013.research.md", "research/alpha-017.research.md"):
        assert "[NEEDS CLARIFICATION]" in fx(research)
        res = G.gate_plan(feature_dir(tmp_path / research.split("/")[1], plan="plan/bravo-115.plan.md", research=research))
        assert by_name(res)["no-needs-clarification"].passed


def test_gate_plan_research_with_open_marker_fails(tmp_path):
    res = G.gate_plan(feature_dir(tmp_path, plan="plan/bravo-115.plan.md", research="research/open-marker.research.md"))
    assert failed_names(res) == {"no-needs-clarification"}
    assert by_name(res)["no-needs-clarification"].detail.startswith("1 bracketed")


def test_gate_plan_technical_context_field_still_unresolved(tmp_path):
    res = G.gate_plan(feature_dir(tmp_path, plan="plan/bravo-115-tech-context-open.plan.md", research="research/bravo-115.research.md"))
    assert failed_names(res) == {"no-needs-clarification"}
    assert "1 Technical Context" in by_name(res)["no-needs-clarification"].detail


def test_gate_plan_unjustified_violation_fails(tmp_path):
    res = G.gate_plan(feature_dir(tmp_path, plan="plan/bravo-115-unjustified-fail.plan.md", research="research/bravo-115.research.md"))
    assert failed_names(res) == {"constitution-no-unjustified-violation"}
    assert "§II no floats for money" in by_name(res)["constitution-no-unjustified-violation"].detail


def test_gate_plan_template_placeholder_row_is_not_a_justification(tmp_path):
    res = G.gate_plan(feature_dir(tmp_path, plan="plan/bravo-115-template-complexity.plan.md", research="research/bravo-115.research.md"))
    assert failed_names(res) == {"constitution-no-unjustified-violation", "plan-no-template-leftovers"}


def test_gate_plan_post_design_recheck_section_is_scanned_too(tmp_path):
    """A violation recorded in a separate '## Post-design Constitution re-check' section (1.2.3 variant heading)."""
    res = G.gate_plan(feature_dir(tmp_path, plan="plan/bravo-115-postdesign-violation.plan.md", research="research/bravo-115.research.md"))
    assert failed_names(res) == {"constitution-no-unjustified-violation"}
    assert "§V money, ledger" in by_name(res)["constitution-no-unjustified-violation"].detail


@pytest.mark.parametrize("template", ["plan/template-1.2.3.plan.md", "plan/template-0.7.1-charlie.plan.md"])
def test_gate_plan_untouched_template_fails(tmp_path, template):
    res = G.gate_plan(feature_dir(tmp_path, plan=template, research="research/bravo-115.research.md"))
    assert failed_names(res) == {"no-needs-clarification", "plan-no-template-leftovers"}
    assert "8 Technical Context" in by_name(res)["no-needs-clarification"].detail
    left = by_name(res)["plan-no-template-leftovers"].detail
    assert "ACTION REQUIRED" in left and "[###-feature-name]" in left


def test_gate_plan_missing_constitution_check_heading(tmp_path):
    fd = feature_dir(tmp_path, research="research/bravo-115.research.md")
    (fd / "plan.md").write_text(fx("plan/bravo-115.plan.md").replace("## Constitution Check", "## Principles"))
    assert failed_names(G.gate_plan(fd)) == {"constitution-check"}


def test_gate_plan_missing_files(tmp_path):
    fd = feature_dir(tmp_path, plan="plan/bravo-115.plan.md")
    assert failed_names(G.gate_plan(fd)) == {"research-exists"}
    fd2 = feature_dir(tmp_path / "b", research="research/bravo-115.research.md")
    res = G.gate_plan(fd2)
    assert failed_names(res) == {"plan-exists"} and len(res.checks) == 2  # stops early


def test_gate_plan_conditional_artifacts_are_warn_only(tmp_path):
    fd = feature_dir(tmp_path, plan="plan/bravo-115.plan.md", research="research/bravo-115.research.md")
    # the real plan's Project Structure lists "contracts/api-changes.md" for the new API error code
    (fd / "plan.md").write_text(fx("plan/bravo-115.plan.md") + "\n├── contracts/api-changes.md   # the API endpoint's new 409\n")
    res = G.gate_plan(fd)
    assert res.passed
    warned = {w.split("]")[0].strip("[") for w in res.warnings()}
    assert {"artifact-data-model.md", "artifact-quickstart.md", "artifact-contracts"} <= warned
    for name in ("data-model.md", "quickstart.md"):
        (fd / name).write_text("# x\n")
    (fd / "contracts").mkdir()
    (fd / "contracts" / "api-changes.md").write_text("# api\n")
    assert G.gate_plan(fd).warnings() == []


@pytest.mark.parametrize(
    "row, is_fail",
    [
        ("| §II | Strict TS | ❌ FAIL — uses any |", True),
        ("| §II | Strict TS | **FAIL** |", True),
        ("| §II | Strict TS | ✗ |", True),
        ("| §II | Strict TS | Fail |", True),
        ("| §II | Strict TS | Violation (justified below) |", True),
        ("| §II | Strict TS | VIOLATED |", True),
        ("| §II | Strict TS | ✅ PASS |", False),
        ("| §II | Strict TS | PASS — no FAIL possible |", False),
        ("| Fail-closed defaults | Fails closed when unset | PASS |", False),
        ("| **Governance — Compliance Review** | Violations block merge until amended | **PASS**. |", False),
        ("| FAIL-SAFE | n/a | PASS |", False),
    ],
)
def test_constitution_fail_cell_tokens(row, is_fail):
    assert bool(G.CONST_FAIL_CELL.search(row)) is is_fail


# ======================================================================================================================
# tasks: parse_tasks / task_progress / gate_tasks
# ======================================================================================================================


def test_parse_tasks_bravo_115_markers_and_line_numbers():
    text = fx("tasks/bravo-115.tasks.md")
    tasks = G.parse_tasks(text)
    assert [t.id for t in tasks] == [f"T{n:03d}" for n in range(1, 16)]
    assert all(t.done and t.mark == "X" for t in tasks)
    t006 = next(t for t in tasks if t.id == "T006")
    assert t006.text.startswith("[P] [US1] Add a schema-guard e2e")
    for t in tasks:  # line numbers point at the task line itself
        assert text.split("\n")[t.line - 1].lstrip().startswith(f"- [X] {t.id}")


def test_parse_tasks_variants_bold_ids_lowercase_x_letter_suffix_and_gated():
    content = G.parse_tasks(fx("tasks/alpha-content.tasks.md"))
    assert [t.id for t in content][:3] == ["T001", "T002", "T003"]  # "- [x] **T001** …"
    assert all(t.done and t.mark == "x" for t in content)

    charlie = {t.id: t for t in G.parse_tasks(fx("tasks/charlie-026.tasks.md"))}
    assert {"T003a", "T034a", "T035a", "T035b"} <= set(charlie)
    assert list(charlie).index("T003a") == list(charlie).index("T003") + 1  # suffix keeps file order

    gated = G.parse_tasks(fx("tasks/alpha-020.tasks.md"))
    by_id = {t.id: t for t in gated}
    assert by_id["T005"].mark == "~" and not by_id["T005"].done  # "[~]" gated (operator step)
    assert by_id["T001"].done
    # legend lines "- `[X]` done · `[~]` gated · `[B]` …" are not tasks
    assert all(t.id.startswith("T") and t.id[1:4].isdigit() for t in gated)


def test_parse_tasks_blocked_mark_and_code_fences():
    text = "## Phase 1: Setup\n- [B] T001 Blocked on an open question\n```text\n- [ ] T999 Example in a fence\n```\n"
    tasks = G.parse_tasks(G.without_code_fences(text))
    assert [(t.id, t.mark, t.done) for t in tasks] == [("T001", "B", False)]


def test_parse_tasks_collects_indented_continuation_bodies():
    t003 = next(t for t in G.parse_tasks(fx("tasks/bravo-086.tasks.md")) if t.id == "T003")
    assert "invoice-sync-mode.spec.ts" in t003.body and "Planted control" in t003.body
    founder = next(t for t in G.parse_tasks(fx("tasks/delta-008.tasks.md")) if t.id == "T001")
    assert founder.text.startswith("FOUNDER GATE") and founder.body  # hard-wrapped continuation lines


@pytest.mark.parametrize(
    "fixture, expected",
    [
        ("tasks/bravo-115.tasks.md", (15, 15)),
        ("tasks/alpha-020.tasks.md", (13, 17)),  # 4 gated "[~]" agent tasks are not done; T005 "*(Operator step — …)*" is human-only
        ("tasks/alpha-content.tasks.md", (8, 8)),
        ("tasks/bravo-115-duplicate.tasks.md", (15, 16)),
        ("tasks/alpha-bkt-nonT.tasks.md", (0, 0)),  # "**N001**" IDs are not Spec Kit tasks
    ],
)
def test_task_progress(tmp_path, fixture, expected):
    assert G.task_progress(feature_dir(tmp_path, tasks=fixture)) == expected


def test_task_progress_ignores_front_matter_and_missing_file(tmp_path):
    assert G.task_progress(feature_dir(tmp_path)) == (0, 0)


@pytest.mark.parametrize(
    "fixture",
    [
        "tasks/bravo-115.tasks.md",
        "tasks/alpha-020.tasks.md",
        "tasks/alpha-content.tasks.md",
        "tasks/charlie-026.tasks.md",
        "tasks/delta-008.tasks.md",
        "tasks/bravo-086.tasks.md",
    ],
)
def test_gate_tasks_real_files_pass_under_a_tests_required_constitution(tmp_path, fixture):
    put_constitution(tmp_path, "constitution/bravo.constitution.md")
    res = G.gate_tasks(feature_dir(tmp_path, tasks=fixture), tmp_path)
    assert res.passed, res.failed()
    assert res.facts["tests_required"] is True


def test_gate_tasks_test_named_only_in_a_sub_bullet_counts(tmp_path):
    """Regression: Bravo 086/085 name the test file in an indented sub-bullet, not on the task line."""
    put_constitution(tmp_path, "constitution/bravo.constitution.md")
    tasks = G.parse_tasks(fx("tasks/bravo-086.tasks.md"))
    assert not any(G.TEST_HINT.search(t.text) for t in tasks)
    assert by_name(G.gate_tasks(feature_dir(tmp_path, tasks="tasks/bravo-086.tasks.md"), tmp_path))["test-tasks"].passed


@pytest.mark.parametrize(
    "constitution, required",
    [
        ("constitution/delta.constitution.md", True),  # "No PR without tests"
        ("constitution/bravo.constitution.md", True),  # "require explicit tests before merge"
        ("constitution/alpha.constitution.md", False),  # only "Tests via vitest. Coverage gate: 85%"
    ],
)
def test_tests_required_constitutions(tmp_path, constitution, required):
    put_constitution(tmp_path, constitution)
    res = G.gate_tasks(feature_dir(tmp_path, tasks="tasks/bravo-026-no-tests.tasks.md"), tmp_path)
    assert res.facts["tests_required"] is required
    assert ("test-tasks" in failed_names(res)) is required
    assert ("test-tasks" in by_name(res)) is required


def test_tests_required_can_come_from_the_plan(tmp_path):
    put_constitution(tmp_path, "constitution/alpha.constitution.md")
    fd = feature_dir(tmp_path, tasks="tasks/bravo-026-no-tests.tasks.md")
    (fd / "plan.md").write_text("# Plan\n\nAll new code is test-first (TDD).\n")
    assert "test-tasks" in failed_names(G.gate_tasks(fd, tmp_path))


@pytest.mark.parametrize(
    "fixture, failing, detail",
    [
        ("tasks/alpha-bktc-duplicate.tasks.md", {"unique-ids"}, "T129"),  # a REAL duplicate (T129 twice)
        ("tasks/bravo-115-duplicate.tasks.md", {"unique-ids"}, "T008"),
        ("tasks/bravo-115-malformed.tasks.md", {"task-format"}, "2 checkbox line(s)"),
        ("tasks/alpha-bkt-nonT.tasks.md", {"at-least-one-task"}, "0 task(s)"),
        ("tasks/template-1.2.3.tasks.md", {"task-format", "no-template-leftovers"}, "TXXX"),
    ],
)
def test_gate_tasks_failing_variants(tmp_path, fixture, failing, detail):
    put_constitution(tmp_path, "constitution/alpha.constitution.md")
    res = G.gate_tasks(feature_dir(tmp_path, tasks=fixture), tmp_path)
    assert failed_names(res) == failing
    assert any(detail in c.detail for c in res.checks if not c.passed)


def test_gate_tasks_checkbox_outside_phases_is_not_a_format_error(tmp_path):
    put_constitution(tmp_path, "constitution/alpha.constitution.md")
    fd = feature_dir(tmp_path)
    (fd / "tasks.md").write_text("# Tasks\n\n## Notes\n- [ ] Ask the founder about copy\n\n## Phase 1: Setup\n- [ ] T001 Create src/x.py\n")
    assert G.gate_tasks(fd, tmp_path).passed


def test_gate_tasks_missing_file(tmp_path):
    res = G.gate_tasks(feature_dir(tmp_path), tmp_path)
    assert failed_names(res) == {"tasks-exists"} and len(res.checks) == 1


# ======================================================================================================================
# analyze: parse_analyze / gate_analyze (report text only exists in the assistant's message)
# ======================================================================================================================


@pytest.mark.parametrize(
    "fixture, counts, critical_metric, high_metric, failing",
    [
        # Echo/Bravo: one HIGH; metric written as "· 0 critical issues"; offer is "suggest concrete edits"
        ("analyze/echo-one-high.md", (0, 1, 3, 5), 0, None, {"zero-high"}),
        # "**CRITICAL**" bold cell; metric in a table "| Critical issues | 1 (C1) |"
        ("analyze/echo-bold-critical.md", (1, 2, 3, 4), 1, None, {"zero-critical", "zero-high"}),
        # second pass: only MEDIUM/LOW; a "| H1 | <status> |" re-check table must NOT count as HIGH findings
        ("analyze/alpha-clean-second-pass.md", (0, 0, 3, 3), 0, None, set()),
        # severity cell with a suffix: "CRITICAL (tracked)"
        ("analyze/foxtrot-critical-tracked.md", (1, 3, 5, 6), 1, None, {"zero-critical", "zero-high"}),
        # findings grouped under "### High / ### Medium / ### Low" with NO Severity column; "**High:** 6"
        ("analyze/alpha-grouped-by-heading.md", (0, 6, 18, 8), 0, 6, {"zero-high"}),
        # zero findings (template skeleton): passes
        ("analyze/template-zero-findings.md", (0, 0, 0, 0), 0, None, set()),
        # metrics claim criticals the table does not list -> fail closed
        ("analyze/echo-metric-disagrees.md", (0, 1, 3, 5), 2, None, {"zero-high", "critical-metric-agrees"}),
        ("analyze/echo-high-metric-only.md", (0, 0, 4, 5), 0, 1, {"high-metric-agrees"}),
    ],
)
def test_parse_and_gate_analyze_real_reports(fixture, counts, critical_metric, high_metric, failing):
    report = G.parse_analyze(fx(fixture))
    assert report.found
    assert tuple(report.counts[k] for k in ("CRITICAL", "HIGH", "MEDIUM", "LOW")) == counts
    assert report.critical_metric == critical_metric
    assert report.high_metric == high_metric
    assert not any(f.id.startswith(("FR-", "SC-")) for f in report.findings), "coverage rows are not findings"
    gate = G.gate_analyze(report)
    assert failed_names(gate) == failing
    assert gate.facts["counts"] == report.counts


def test_parse_analyze_finding_fields_and_blocking():
    report = G.parse_analyze(fx("analyze/echo-bold-critical.md"))
    c1 = report.findings[0]
    assert (c1.id, c1.category, c1.severity) == ("C1", "Coverage / design gap", "CRITICAL")
    assert c1.rest.startswith("research.md R4")
    assert report.blocking == 3
    assert report.offered_remediation


def test_parse_analyze_skill_template_example_and_fakeworld_format():
    skeleton = (
        "## Specification Analysis Report\n\n| ID | Category | Severity | Location(s) | Summary | Recommendation |\n"
        "|----|----------|----------|-------------|---------|----------------|\n"
        "| A1 | Duplication | HIGH | spec.md:L120-134 | Two similar requirements ... | Merge phrasing; keep clearer version |\n\n"
        "**Metrics:**\n\n- Total Requirements\n- Critical Issues Count: 0\n"
    )
    r = G.parse_analyze(skeleton)
    assert r.counts["HIGH"] == 1 and r.critical_metric == 0
    fake = ANALYZE_REPORT.format(rows="| C1 | Coverage | CRITICAL | spec.md | gap C1 | fix C1 |\n| L1 | Style | LOW | plan.md | wording | optional |", crit=1, n=1)
    r2 = G.parse_analyze(fake)
    assert [(f.id, f.severity) for f in r2.findings] == [("C1", "CRITICAL"), ("L1", "LOW")]
    assert r2.critical_metric == 1 and r2.offered_remediation


def test_parse_analyze_bold_ids_and_bold_severities_and_headerless_rows():
    text = "## Specification Analysis Report\n\n| **A1** | Duplication | **HIGH** | x | y | z |\n| B2 | Gap | low | x | y | z |\n"
    r = G.parse_analyze(text)
    assert [(f.id, f.severity) for f in r.findings] == [("A1", "HIGH"), ("B2", "LOW")]


def test_parse_analyze_non_severity_headings_do_not_label_rows():
    text = (
        "## Specification Analysis Report\n\n### High-level summary\n\n| ID | Category | Location | Summary |\n|---|---|---|---|\n"
        "| F1 | Drift | plan.md | wording |\n\n### Low\n\n**Coverage summary**\n\n| Requirement | Has task? |\n|---|---|\n| FR-001 | Yes |\n"
    )
    assert G.parse_analyze(text).findings == []


@pytest.mark.parametrize(
    "line, crit",
    [
        ("- Critical Issues Count: 3", 3),
        ("- **Critical Issues Count**: 2", 2),
        ("- Critical issues: **0**", 0),
        ("- **Critical issues:** 1", 1),
        ("| Critical findings | 0 |", 0),
        ("**Metrics:** 36 requirements · 29 tasks · 0 critical issues.", 0),
        ("Ambiguities: 3 · **Duplications:** 3 · **Critical issues:** 0 · **High:** 6", 0),
        ("No critical issues: nothing breaks a constitution rule.", None),
        ("There is one CRITICAL issue and six HIGH ones.", None),
    ],
)
def test_parse_analyze_critical_metric_variants(line, crit):
    assert G.parse_analyze("## Specification Analysis Report\n\n" + line).critical_metric == crit


def test_gate_analyze_missing_report_fails_closed():
    for text in (fx("analyze/not-a-report.md"), "", "Done."):
        report = G.parse_analyze(text)
        assert not report.found
        assert "report-present" in failed_names(G.gate_analyze(report))


def test_gate_analyze_must_be_read_only():
    report = G.parse_analyze(fx("analyze/alpha-clean-second-pass.md"))
    assert G.gate_analyze(report).passed
    gate = G.gate_analyze(report, read_only_ok=False)
    assert failed_names(gate) == {"analyze-read-only"}


# ======================================================================================================================
# cc-review: parse_review / gate_review (report-only; labels blocker / nit / question)
# ======================================================================================================================

ZERO_BLOCKER_REPORTS = [
    "review/bravo-blockers-none.md",  # "## Blockers\nNone."
    "review/bravo-none-prose.md",  # "## Blockers\n\nNone. No secrets, …"
    "review/golf-per-agent-none.md",  # "**Blockers:** none." in every agent section
    "review/subagent-bold-none.md",  # "### Blockers\n\n**None.**"; "Severity: **nit**" lines are not blockers
]
BLOCKER_REPORTS = [
    ("review/alpha-numbered-bold.md", 1),  # "**1. Charts ignore …**"
    ("review/bravo-api-subgroup.md", 1),  # "**API**" group label, then "- **A booking can get stuck …**"
    ("review/echo-count-heading.md", 2),  # "## Blockers (2)"
    ("review/delta-label-list.md", 1),  # "**Blockers**" label + "- `.claude/settings.json:64-77` …"
    ("review/foxtrot-numbered-list.md", 1),  # "1. **A sandbox outage …**"
    ("review/foxtrot-mixed-subsections.md", 1),  # "### Security (primary)\nNone." but "### Pipeline" has one
    ("review/alpha-there-is-one.md", 1),  # "There is one. It's arguable, …"
    ("review/echo-severity-blocker.md", 1),  # agent format "## Blocker\n\n**Severity**: blocker"
]


@pytest.mark.parametrize("fixture", ZERO_BLOCKER_REPORTS)
def test_review_zero_blockers_without_trailer(fixture):
    r = G.parse_review(fx(fixture))
    assert r.explicit_none and not r.blockers_listed and r.blocker_items == 0
    assert r.blockers == 0
    assert G.gate_review(r, None).passed


@pytest.mark.parametrize("fixture", ZERO_BLOCKER_REPORTS)
def test_review_zero_blockers_with_agreeing_trailer(fixture):
    r = G.parse_review(fx(fixture), "REVIEW_SUMMARY blockers=0 nits=4 questions=2")
    assert r.trailer_blockers == 0 and r.blockers == 0
    assert G.gate_review(r, None).passed


@pytest.mark.parametrize("fixture", ZERO_BLOCKER_REPORTS)
def test_review_trailer_claiming_blockers_wins_over_none(fixture):
    r = G.parse_review(fx(fixture), "REVIEW_SUMMARY blockers=2 nits=0 questions=0")
    assert r.blockers == 2
    assert failed_names(G.gate_review(r, None)) == {"zero-must-fix"}


@pytest.mark.parametrize("fixture, at_least", BLOCKER_REPORTS)
def test_review_blockers_listed_fail(fixture, at_least):
    r = G.parse_review(fx(fixture))
    assert r.blockers_listed and r.blockers is not None and r.blockers >= at_least
    gate = G.gate_review(r, None)
    assert "zero-must-fix" in failed_names(gate)
    assert "review-summary" in failed_names(gate)  # no trailer and no explicit none


@pytest.mark.parametrize("fixture, at_least", BLOCKER_REPORTS)
def test_review_trailer_saying_zero_disagrees_with_listed_blockers(fixture, at_least):
    r = G.parse_review(fx(fixture), "REVIEW_SUMMARY blockers=0 nits=0 questions=0")
    assert r.trailer_blockers == 0 and r.blockers >= 1
    assert failed_names(G.gate_review(r, None)) == {"zero-must-fix", "trailer-agrees"}


def test_review_trailer_below_counted_items_disagrees():
    r = G.parse_review(fx("review/echo-count-heading.md"), "REVIEW_SUMMARY blockers=1 nits=0 questions=0")
    assert r.blocker_items == 2 and r.blockers == 2
    gate = G.gate_review(r, None)
    assert failed_names(gate) == {"zero-must-fix", "trailer-agrees"}
    assert "blockers=1" in by_name(gate)["trailer-agrees"].detail


def test_review_unknown_without_any_marker_fails_closed():
    r = G.parse_review("# Review\n\nLooks fine overall. A few small things below.\n\n## Nits\n- rename x")
    assert r.blockers is None
    assert failed_names(G.gate_review(r, None)) == {"review-summary", "zero-must-fix"}


def test_review_downgraded_section_does_not_count_as_blockers():
    text = (
        "## Blockers\n\nNone.\n\n## Nits\n- `a.ts:1`: rename\n\n"
        '## Reviewer "blockers" I downgraded\n- web-reviewer said the CSRF check was missing; it is in `actions.ts:12`.\n'
    )
    r = G.parse_review(text)
    assert r.blockers == 0 and not r.blockers_listed


@pytest.mark.parametrize(
    "line",
    [
        "**Blockers / drift:** none. The constants in all four workspaces are protocol values.",  # real (Delta)
        "**Blockers: None.**",  # real (Echo subagent)
        "**Blockers:** none (reclassified).",  # real (golf)
        "## Blockers (0)\n\nNone.",
        "### Blockers\n\n_None._",
    ],
)
def test_review_explicit_none_variants(line):
    r = G.parse_review("## Security\n" + line + "\n\n## Nits\n- `a.ts:1`: rename\n")
    assert r.explicit_none and r.blockers == 0


def test_review_empty_blockers_heading_is_unknown_not_zero():
    r = G.parse_review("## Blockers\n\n## Nits\n- x\n")
    assert r.blockers is None


@pytest.mark.parametrize(
    "trailer",
    [
        "REVIEW_SUMMARY blockers=0 nits=1 questions=0",
        "`REVIEW_SUMMARY blockers=0 nits=1 questions=0`",
        "REVIEW-SUMMARY: blockers=0 nits=1",
        "**REVIEW_SUMMARY** blockers=0 nits=1",
    ],
)
def test_review_trailer_formats(trailer):
    assert G.parse_review("", trailer).trailer_blockers == 0


def test_review_last_trailer_wins_and_request_prompt_matches_parser():
    r = G.parse_review("REVIEW_SUMMARY blockers=3 nits=0 questions=0", "REVIEW_SUMMARY blockers=0 nits=0 questions=0")
    assert r.trailer_blockers == 0
    example = REVIEW_TRAILER_REQUEST.splitlines()[-1].replace("<number of blocker / must-fix findings>", "0").replace("<n>", "1")
    assert G.parse_review("", example).trailer_blockers == 0


def test_gate_review_appends_tooling_checks():
    ok = G.parse_review(fx("review/bravo-blockers-none.md"))
    tooling = G.ToolingResult(results={"test": CmdResult("pytest -q", 1, "FAILED test_x", "")})
    gate = G.gate_review(ok, tooling)
    assert failed_names(gate) == {"test-passes"}
    assert "FAILED test_x" in by_name(gate)["test-passes"].detail


# ======================================================================================================================
# tooling: run_tooling (flaky test re-run once; timeout not re-run)
# ======================================================================================================================


class ScriptedRunner:
    def __init__(self, script: dict[str, list[CmdResult]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, Path, float]] = []

    def __call__(self, cmd: str, cwd: Path, timeout: float) -> CmdResult:
        self.calls.append((cmd, cwd, timeout))
        seq = self.script.get(cmd)
        return seq.pop(0) if seq else CmdResult(cmd, 0, "ok")


CMDS = {"typecheck": "pnpm typecheck", "lint": "pnpm lint", "test": "pnpm test", "build": None}


def test_run_tooling_order_skips_and_timeout_passthrough(tmp_path):
    run = ScriptedRunner({})
    tr = G.run_tooling(CMDS, tmp_path, run, 600)
    assert [c[0] for c in run.calls] == ["pnpm typecheck", "pnpm lint", "pnpm test"]
    assert all(c[1] == tmp_path and c[2] == 600 for c in run.calls)
    assert tr.skipped == ["build"] and tr.ok and tr.retried == []
    names = {c.name: c for c in tr.checks()}
    assert names["build-passes"].warn_only and names["build-passes"].passed


def test_run_tooling_flaky_test_is_rerun_once_and_passes(tmp_path):
    run = ScriptedRunner({"pnpm test": [CmdResult("pnpm test", 1, "", "1 failed"), CmdResult("pnpm test", 0, "all green")]})
    tr = G.run_tooling(CMDS, tmp_path, run, 60)
    assert [c[0] for c in run.calls].count("pnpm test") == 2
    assert tr.retried == ["test"] and tr.ok
    assert "re-run once" in {c.name: c for c in tr.checks()}["test-passes"].detail


def test_run_tooling_real_test_failure_after_rerun(tmp_path):
    run = ScriptedRunner({"pnpm test": [CmdResult("pnpm test", 1, "", "boom"), CmdResult("pnpm test", 1, "FAIL src/a.test.ts", "")]})
    tr = G.run_tooling(CMDS, tmp_path, run, 60)
    assert not tr.ok and tr.retried == ["test"]
    check = {c.name: c for c in tr.checks()}["test-passes"]
    assert not check.passed and "FAIL src/a.test.ts" in check.detail


def test_run_tooling_timeout_is_not_rerun(tmp_path):
    run = ScriptedRunner({"pnpm test": [CmdResult("pnpm test", 124, "partial", "", 60.0, timed_out=True)]})
    tr = G.run_tooling(CMDS, tmp_path, run, 60)
    assert [c[0] for c in run.calls].count("pnpm test") == 1 and tr.retried == []
    check = {c.name: c for c in tr.checks()}["test-passes"]
    assert not check.passed and "(timed out)" in check.detail


def test_run_tooling_only_tests_are_retried(tmp_path):
    run = ScriptedRunner({"pnpm lint": [CmdResult("pnpm lint", 1, "", "lint error")]})
    tr = G.run_tooling(CMDS, tmp_path, run, 60)
    assert [c[0] for c in run.calls].count("pnpm lint") == 1 and tr.retried == []
    assert not tr.ok


# ======================================================================================================================
# implement: all tasks checked + diff vs base excludes specs/
# ======================================================================================================================


def _feature_with_tasks(repo: Path, tasks_fixture: str) -> Path:
    git(repo, "checkout", "-q", "-b", "feature/115-x")
    fd = repo / "specs" / "115-x"
    fd.mkdir(parents=True)
    (fd / "spec.md").write_text(fx("spec/bravo-115.spec.md"))
    (fd / "tasks.md").write_text(fx(tasks_fixture))
    commit_all(repo, "spec artifacts")
    return fd


def test_gate_implement_passes_with_committed_code_change(repo):
    fd = _feature_with_tasks(repo, "tasks/bravo-115.tasks.md")
    (repo / "src" / "filters.py").write_text("def f():\n    return 1\n")
    commit_all(repo, "code")
    res = G.gate_implement(repo, "main", fd, None)
    assert res.passed, res.failed()
    assert (res.facts["tasks_done"], res.facts["tasks_total"]) == (15, 15)


def test_gate_implement_spec_only_changes_are_not_code(repo):
    fd = _feature_with_tasks(repo, "tasks/bravo-115.tasks.md")
    (repo / ".autopilot").mkdir()
    (repo / ".autopilot" / "decisions.md").write_text("x")
    res = G.gate_implement(repo, "main", fd, None)
    assert failed_names(res) == {"diff-not-empty"}
    assert "only spec artifacts" in by_name(res)["diff-not-empty"].detail


def test_gate_implement_uncommitted_code_counts(repo):
    fd = _feature_with_tasks(repo, "tasks/bravo-115.tasks.md")
    (repo / "src" / "new_module.py").write_text("x = 1\n")
    assert G.gate_implement(repo, "main", fd, None).passed


def test_gate_implement_diff_is_against_origin_base_not_local(repo):
    fd = _feature_with_tasks(repo, "tasks/bravo-115.tasks.md")
    git(repo, "checkout", "-q", "main")
    (repo / "src" / "app.py").write_text("print('local main only')\n")
    commit_all(repo, "unpushed main commit")
    git(repo, "checkout", "-q", "feature/115-x")
    git(repo, "merge", "-q", "--no-edit", "main")
    # the code change lives only in local main: vs origin/main the feature branch DOES differ outside specs/
    assert G.gate_implement(repo, "main", fd, None).passed


def test_gate_implement_open_or_gated_tasks_fail(repo):
    fd = _feature_with_tasks(repo, "tasks/alpha-020.tasks.md")
    (repo / "src" / "x.py").write_text("x = 1\n")
    res = G.gate_implement(repo, "main", fd, None)
    assert failed_names(res) == {"all-tasks-checked"}
    assert "13/17" in by_name(res)["all-tasks-checked"].detail  # T005 "*(Operator step — …)*" is human-only, not counted
    assert by_name(res)["human-only-tasks"].warn_only and res.facts["human_open"][0].startswith("T005 ")


def test_gate_implement_no_tasks_is_not_done(repo):
    fd = _feature_with_tasks(repo, "tasks/alpha-bkt-nonT.tasks.md")
    (repo / "src" / "x.py").write_text("x = 1\n")
    assert "all-tasks-checked" in failed_names(G.gate_implement(repo, "main", fd, None))


def test_gate_implement_includes_tooling(repo):
    fd = _feature_with_tasks(repo, "tasks/bravo-115.tasks.md")
    (repo / "src" / "x.py").write_text("x = 1\n")
    tr = G.ToolingResult(results={"test": CmdResult("pytest -q", 1, "1 failed")}, skipped=["lint"])
    res = G.gate_implement(repo, "main", fd, tr)
    assert failed_names(res) == {"test-passes"}
    assert by_name(res)["lint-passes"].warn_only


# ======================================================================================================================
# ship: ship_facts (merge-commit ancestry; squash via gh PR state) + gate_ship
# ======================================================================================================================


class FakeGh:
    def __init__(self, prs: list[dict[str, Any]] | None = None, runs: list[Any] | None = None, *, fail: bool = False, raw: str | None = None):
        self.prs = prs or []
        self.runs = list(runs or [])
        self.fail = fail
        self.raw = raw
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, repo: Path, *args: str, timeout_s: float = 60.0) -> CmdResult:
        self.calls.append(args)
        if self.fail:
            return CmdResult("gh " + " ".join(args), 1, "", "HTTP 401: Bad credentials")
        if self.raw is not None:
            return CmdResult("gh", 0, self.raw)
        if args[:2] == ("pr", "list"):
            return CmdResult("gh pr list", 0, json.dumps(self.prs))
        if args[:2] == ("run", "list"):
            batch = self.runs.pop(0) if len(self.runs) > 1 else (self.runs[0] if self.runs else [])
            return CmdResult("gh run list", 0, json.dumps(batch))
        return CmdResult("gh", 0, "")


def _push_feature(repo: Path, branch: str = "feature/115-x") -> str:
    git(repo, "checkout", "-q", "-b", branch)
    (repo / "src" / "feature.py").write_text("def f():\n    return 42\n")
    sha = commit_all(repo, "feat: 115")
    git(repo, "push", "-q", "-u", "origin", branch)
    return sha


def test_ship_facts_merge_commit_detected_by_ancestry(repo):
    _push_feature(repo)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request #7 from org/feature/115-x", "feature/115-x")
    merge_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "-q", "origin", "main")
    gh = FakeGh(prs=[{"number": 7, "state": "MERGED", "url": "https://github.com/o/r/pull/7", "mergeCommit": {"oid": merge_sha}, "baseRefName": "main"}])
    f = G.ship_facts(repo, "main", "feature/115-x", gh_runner=gh)
    assert (f.on_base, f.clean, f.base_synced, f.remote_branch, f.merged) == (True, True, True, True, True)
    assert (f.pr_number, f.pr_state, f.merge_commit) == (7, "MERGED", merge_sha)
    assert ("pr", "list", "--head", "feature/115-x") == gh.calls[0][:4]
    assert G.gate_ship(f, "main", ci=None, ci_required=False).passed
    # ancestry alone is enough even when gh knows nothing (e.g. a fast-forward push)
    assert G.ship_facts(repo, "main", "feature/115-x", gh_runner=FakeGh()).merged


def test_ship_facts_pushed_but_not_merged(repo):
    _push_feature(repo)
    gh = FakeGh(prs=[{"number": 7, "state": "OPEN", "url": "u", "mergeCommit": None, "baseRefName": "main"}])
    f = G.ship_facts(repo, "main", "feature/115-x", gh_runner=gh)
    assert not f.merged and not f.on_base and f.pr_state == "OPEN"
    assert failed_names(G.gate_ship(f, "main", ci=None, ci_required=False)) == {"on-base", "merged"}


def test_ship_facts_squash_merge_needs_the_gh_pr_state(repo):
    _push_feature(repo)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--squash", "feature/115-x")
    git(repo, "commit", "-q", "-m", "feat: 115 (#7)")
    squash_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "-q", "origin", "main")
    without_pr = G.ship_facts(repo, "main", "feature/115-x", gh_runner=FakeGh())
    assert not without_pr.merged, "a squash commit is not an ancestor of the feature tip"
    gh = FakeGh(prs=[
        {"number": 6, "state": "CLOSED", "url": "u6", "mergeCommit": None, "baseRefName": "main"},
        {"number": 9, "state": "MERGED", "url": "u9", "mergeCommit": {"oid": "x"}, "baseRefName": "release"},  # other base
        {"number": 7, "state": "MERGED", "url": "u7", "mergeCommit": {"oid": squash_sha}, "baseRefName": "main"},
    ])
    f = G.ship_facts(repo, "main", "feature/115-x", gh_runner=gh)
    assert f.merged and (f.pr_number, f.merge_commit) == (7, squash_sha)
    assert G.gate_ship(f, "main", ci=("success", "1 run(s) green"), ci_required=True).passed


def test_ship_facts_local_branch_only_and_gh_errors(repo):
    git(repo, "checkout", "-q", "-b", "feature/115-x")
    (repo / "src" / "x.py").write_text("x = 1\n")
    commit_all(repo, "local only")
    git(repo, "checkout", "-q", "main")
    f = G.ship_facts(repo, "main", "feature/115-x", gh_runner=FakeGh(fail=True))
    assert not f.remote_branch and not f.merged and f.pr_number is None
    assert any("gh pr list failed" in d for d in f.detail)
    assert G.ship_facts(repo, "main", "feature/115-x", gh_runner=FakeGh(raw="not json")).pr_number is None


def test_ship_facts_branch_without_commits_is_not_merged(repo):
    git(repo, "checkout", "-q", "-b", "feature/115-x")
    git(repo, "push", "-q", "-u", "origin", "feature/115-x")
    git(repo, "checkout", "-q", "main")
    assert not G.ship_facts(repo, "main", "feature/115-x", gh_runner=FakeGh()).merged


def test_ship_facts_base_not_synced_and_dirty(tmp_path, repo):
    _push_feature(repo)
    git(repo, "checkout", "-q", "main")
    other = second_clone(tmp_path)
    git(other, "merge", "-q", "--no-ff", "-m", "Merge #7", "origin/feature/115-x")
    git(other, "push", "-q", "origin", "main")
    (repo / "scratch.txt").write_text("left behind\n")
    f = G.ship_facts(repo, "main", "feature/115-x", gh_runner=FakeGh())
    assert f.merged and f.on_base and not f.base_synced and not f.clean
    assert failed_names(G.gate_ship(f, "main", ci=None, ci_required=False)) == {"base-synced", "clean-tree"}


@pytest.mark.parametrize(
    "ci, required, ok",
    [
        (("success", "2 run(s) green"), True, True),
        (("failure", "ci: failure"), True, False),
        (("timeout", "CI still running"), True, False),
        (("none", "no workflow runs"), True, False),
        (None, True, False),
        (("failure", "billing"), False, True),
        (None, False, True),
    ],
)
def test_gate_ship_ci_required_or_not(ci, required, ok):
    facts = G.ShipFacts(branch="main", on_base=True, clean=True, base_synced=True, remote_branch=True, merged=True, pr_number=7)
    gate = G.gate_ship(facts, "main", ci=ci, ci_required=required)
    assert gate.passed is ok
    assert by_name(gate)["ci-green"].warn_only is (not required)
    assert "via PR #7" in by_name(gate)["merged"].detail


# ======================================================================================================================
# CI: ci_state / wait_for_ci / ci_health
# ======================================================================================================================

RUN_OK = {"name": "ci", "status": "completed", "conclusion": "success", "workflowName": "ci"}
RUN_SKIP = {"name": "docs", "status": "completed", "conclusion": "skipped", "workflowName": "docs-ci"}
RUN_PENDING = {"name": "ci", "status": "in_progress", "conclusion": None, "workflowName": "ci"}
RUN_FAIL = {"name": "verify", "status": "completed", "conclusion": "failure", "workflowName": "CI"}


@pytest.mark.parametrize(
    "runs, state",
    [
        ([RUN_OK, RUN_SKIP], "success"),
        ([RUN_OK, RUN_FAIL], "failure"),
        ([RUN_OK, RUN_PENDING], "pending"),
        ([], "none"),
    ],
)
def test_ci_state(tmp_path, runs, state):
    gh = FakeGh(runs=[runs])
    got, detail = G.ci_state(tmp_path, "abc123", gh_runner=gh)
    assert got == state
    assert gh.calls[0][:4] == ("run", "list", "--commit", "abc123")
    if state == "failure":
        assert "CI: failure" in detail


def test_ci_state_gh_errors_are_pending_not_green(tmp_path):
    assert G.ci_state(tmp_path, "s", gh_runner=FakeGh(fail=True))[0] == "pending"
    assert G.ci_state(tmp_path, "s", gh_runner=FakeGh(raw="<html>"))[0] == "pending"


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(G, "time", c)  # gates reads time.monotonic() through its module global
    return c


def test_wait_for_ci_pending_then_success(tmp_path, clock):
    gh = FakeGh(runs=[[RUN_PENDING], [RUN_PENDING], [RUN_OK]])
    assert G.wait_for_ci(tmp_path, "sha", timeout_s=600, poll_s=30, gh_runner=gh, sleep=clock.sleep) == ("success", "1 run(s) green")
    assert clock.sleeps == [30, 30]


def test_wait_for_ci_failure_returns_immediately(tmp_path, clock):
    state, detail = G.wait_for_ci(tmp_path, "sha", timeout_s=600, poll_s=30, gh_runner=FakeGh(runs=[[RUN_FAIL]]), sleep=clock.sleep)
    assert state == "failure" and clock.sleeps == []


def test_wait_for_ci_timeout(tmp_path, clock):
    state, detail = G.wait_for_ci(tmp_path, "sha", timeout_s=120, poll_s=30, gh_runner=FakeGh(runs=[[RUN_PENDING]]), sleep=clock.sleep)
    assert state == "timeout" and "after 2 min" in detail
    assert sum(clock.sleeps) == 120


def test_wait_for_ci_runs_never_register(tmp_path, clock):
    state, _ = G.wait_for_ci(tmp_path, "sha", timeout_s=3600, poll_s=30, gh_runner=FakeGh(runs=[[]]), sleep=clock.sleep)
    assert state == "none" and len(clock.sleeps) == 3  # bounded grace, not the whole timeout


def _run(conclusion: str, seconds: float, status: str = "completed") -> dict[str, Any]:
    return {"conclusion": conclusion, "status": status, "createdAt": "2026-11-09T10:00:00Z", "updatedAt": f"2026-11-09T10:{int(seconds // 60):02d}:{int(seconds % 60):02d}Z"}


def test_ci_health_billing_blocked_heuristic(tmp_path):
    """Real pattern (step0): every recent run fails in ~2-3 s ('The job was not started because recent account payments have failed')."""
    gh = FakeGh(runs=[[_run("failure", 2), _run("failure", 3), _run("failure", 7)]])
    healthy, detail = G.ci_health(tmp_path, "main", gh_runner=gh)
    assert healthy is False and "billing" in detail
    assert gh.calls[0][:4] == ("run", "list", "--branch", "main")


@pytest.mark.parametrize(
    "runs, expected",
    [
        ([_run("failure", 2), _run("success", 420)], True),  # any green run => functional
        ([_run("failure", 340), _run("failure", 3)], True),  # a long red run => CI runs but is red
        ([_run("in_progress", 0, status="in_progress")], None),
        ([], None),
        ([{"conclusion": "failure", "status": "completed", "createdAt": "garbage"}], True),  # unparseable time: not "instant"
    ],
)
def test_ci_health_other_cases(tmp_path, runs, expected):
    assert G.ci_health(tmp_path, "main", gh_runner=FakeGh(runs=[runs]))[0] is expected


def test_ci_health_gh_unusable(tmp_path):
    assert G.ci_health(tmp_path, "main", gh_runner=FakeGh(fail=True))[0] is None
    assert G.ci_health(tmp_path, "main", gh_runner=FakeGh(raw="nope"))[0] is None


# ======================================================================================================================
# Linear close-out: status_block + gate_linear (data read back through MCP)
# ======================================================================================================================

TODAY = "2026-11-10"
HTML_BLOCK = f"# Demo\n\nIntro.\n\n## Current status\n\n{G.STATUS_START}\n- **Date:** {TODAY}\n- **Just shipped:** ENG-142\n{G.STATUS_END}\n"
VISIBLE_BLOCK = f"# Demo\n\n{G.VISIBLE_START}\nDate: {TODAY}; shipped ENG-142\n{G.VISIBLE_END}\n"


def test_status_block_html_and_visible_markers():
    block, style = G.status_block(HTML_BLOCK)
    assert style == "html" and TODAY in block and "ENG-142" in block
    block, style = G.status_block(VISIBLE_BLOCK)
    assert style == "visible" and TODAY in block
    assert G.status_block(f"x {G.STATUS_START} never closed") == (None, "")
    assert G.status_block(f"{G.STATUS_END} before {G.STATUS_START}") == (None, "")
    assert G.status_block("") == (None, "")


def _comment(body: str) -> dict[str, Any]:
    return {"id": "c", "body": body}


CLOSEOUT = "**Autopilot close-out** for ENG-142 (PR #7, merge abc123)\n- shipped A"


def test_gate_linear_passes(tmp_path):
    issue = {"id": "ENG-142", "status": "Done", "statusType": "completed"}
    project = {"id": "p", "description": HTML_BLOCK}
    res = G.gate_linear(issue, [_comment("unrelated"), _comment(CLOSEOUT)], project, TODAY, "ENG-142")
    assert res.passed, res.failed()
    assert res.facts["autopilot_comments"] == 1 and res.facts["status_marker_style"] == "html"


def test_gate_linear_status_type_from_status_object_and_project_content_key():
    issue = {"id": "ENG-142", "status": {"name": "Done", "type": "completed"}}
    res = G.gate_linear(issue, [_comment(CLOSEOUT)], {"content": VISIBLE_BLOCK}, TODAY, "ENG-142")
    assert res.passed and res.facts["status_marker_style"] == "visible"


@pytest.mark.parametrize(
    "comments, n",
    [
        ([], 0),
        ([_comment(CLOSEOUT), _comment(CLOSEOUT.replace("for", "(retry) for"))], 2),
        ([_comment(CLOSEOUT.replace("ENG-142", "ENG-143"))], 0),  # another issue's close-out
        ([_comment(CLOSEOUT.upper())], 1),  # case-insensitive
    ],
)
def test_gate_linear_exactly_one_closeout_comment(comments, n):
    issue = {"id": "ENG-142", "statusType": "completed"}
    res = G.gate_linear(issue, comments, {"description": HTML_BLOCK}, TODAY, "ENG-142")
    assert res.facts["autopilot_comments"] == n
    assert ("one-autopilot-comment" in failed_names(res)) is (n != 1)


def test_gate_linear_failures():
    assert failed_names(G.gate_linear(None, [], None, TODAY, "ENG-142")) == {"issue-read"}
    started = {"id": "ENG-142", "status": "In Review", "statusType": "started"}
    assert "issue-completed" in failed_names(G.gate_linear(started, [_comment(CLOSEOUT)], {"description": HTML_BLOCK}, TODAY, "ENG-142"))
    done = {"id": "ENG-142", "statusType": "completed"}
    # an issue WITHOUT a project has no overview to update: warn-only skip (e.g. Alpha, whose project was purged)
    no_proj = G.gate_linear(done, [_comment(CLOSEOUT)], None, TODAY, "ENG-142")
    assert no_proj.passed and any("has no project" in w for w in no_proj.warnings())
    # ...but a project that exists and cannot be read back fails closed, as does require_project
    with_proj = {**done, "projectId": "p1"}
    assert failed_names(G.gate_linear(with_proj, [_comment(CLOSEOUT)], None, TODAY, "ENG-142")) == {"project-read"}
    assert failed_names(G.gate_linear(done, [_comment(CLOSEOUT)], None, TODAY, "ENG-142", require_project=True)) == {"project-read"}
    assert failed_names(G.gate_linear(done, [_comment(CLOSEOUT)], {"description": "# Demo\nno block"}, TODAY, "ENG-142")) == {"status-block"}
    stale = G.gate_linear(done, [_comment(CLOSEOUT)], {"description": HTML_BLOCK.replace(TODAY, "2026-11-09")}, TODAY, "ENG-142")
    assert failed_names(stale) == {"status-block-dated-today"}


def test_gate_linear_on_fakeworld_reader_payloads():
    lin = FakeLinear()
    lin.apply(lin.closeout("workflow state whose TYPE is `completed` … Add exactly ONE comment **Autopilot close-out** for ENG-142 … Update the project overview", TODAY))
    data = lin.reader_results()
    issue = G.parse_mcp_json(data["mcp__linear__get_issue"])
    comments = G.parse_mcp_json(data["mcp__linear__list_comments"])["comments"]
    project = G.parse_mcp_json(data["mcp__linear__get_project"])
    assert G.gate_linear(issue, comments, project, TODAY, "ENG-142").passed


# ======================================================================================================================
# parse_mcp_json
# ======================================================================================================================


@pytest.mark.parametrize(
    "payload, expected",
    [
        ('{"id": "ENG-142", "statusType": "completed"}', {"id": "ENG-142", "statusType": "completed"}),
        ('  [{"id": 1}]  ', [{"id": 1}]),
        ([{"type": "text", "text": '{"comments": [{"body": "x"}]}'}], {"comments": [{"body": "x"}]}),
        ([{"type": "text", "text": '{"a":'}, {"type": "text", "text": ' 1}'}], {"a": 1}),  # split across blocks
        ({"already": "parsed"}, {"already": "parsed"}),
        ([{"id": 1}, {"id": 2}], [{"id": 1}, {"id": 2}]),  # a plain list of records, not content blocks
        ('Here is the issue: {"id": "ENG-142"} (fetched)', {"id": "ENG-142"}),
        ("Error: Not found", None),
        ([{"type": "text", "text": "Linear API error: rate limited"}], None),
        ("{not json}", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_mcp_json(payload, expected):
    assert G.parse_mcp_json(payload) == expected


def test_parse_mcp_json_sdk_text_block_objects():
    class TextBlock:  # attribute-style block (e.g. an SDK object) inside a content list
        def __init__(self, text: str):
            self.type = "text"
            self.text = text

    assert G.parse_mcp_json([{"type": "text", "text": '{"x": 1}'}, TextBlock("")]) == {"x": 1}


# ======================================================================================================================
# Fixture hygiene: every fixture is real (or a declared derivation) and secret-free
# ======================================================================================================================


def test_fixtures_declare_their_source_and_hold_no_secrets():
    import re

    secret = re.compile(r"(?i)(sk-[a-z0-9]{16,}|ghp_[a-z0-9]{20,}|github_pat_|AKIA[0-9A-Z]{16}|xox[bp]-|-----BEGIN|eyJ[a-z0-9_-]{20,}\.)")
    files = sorted(p for p in FIX.rglob("*.md"))
    assert len(files) >= 60
    for p in files:
        text = p.read_text(encoding="utf-8")
        assert not secret.search(text), p
        if "spec-template" not in p.name:
            assert "<!-- fixture: " in text, f"{p} has no provenance comment"
