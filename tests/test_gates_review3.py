"""Gate regressions from the third adversarial review, on real formats:

(a) ANALYZE_SUMMARY: every complete summary line counts, the largest stated count per severity wins;
(b) human-only tags a repo defines in its tasks.md legend ([OPERATOR], [op], [REVIEW]) and tags that say so themselves
    ([AIP §10 — performed by the operator, not by the implementer]); legend lines and task lines modelled on
    three real repos (synthetic content);
(c) label look-alikes that name an authorisation rule or a document, not a person's task;
(d) human-only tasks that are the pipeline's own /cc-review and /cc-ship steps;
(e) the test baseline never excuses a failure outside the tests (teardown, hook, obsolete snapshot), and an excused
    first run is not re-run for flakiness.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from speckit_autopilot import gates as G
from speckit_autopilot.repo import CmdResult

from .test_gates_fixall import EXPECTED_FAILURES, ScriptedRunner, failed_names, output, run_result, tooling_with

# ======================================================================================================================
# (a) ANALYZE_SUMMARY
# ======================================================================================================================

REPORT_NO_TABLE_HIGH2 = """## Specification Analysis Report

Two problems stand out. The plan never says how the origin filter handles products without an origin (a HIGH
finding: FR-001 cannot be tested), and the tasks skip the migration the data model needs (HIGH: T004 would fail).
One wording issue in spec.md is MEDIUM.

ANALYZE_SUMMARY critical=0 high=2 medium=1 low=0
"""


def test_a_zero_trailer_reply_does_not_override_the_reports_own_summary():
    r = G.parse_analyze(REPORT_NO_TABLE_HIGH2, "ANALYZE_SUMMARY critical=0 high=0 medium=0 low=0")
    assert r.trailer == {"critical": 0, "high": 2, "medium": 1, "low": 0}
    assert r.blocking == 2
    gate = G.gate_analyze(r)
    assert not gate.passed and "zero-high" in failed_names(gate)


def test_the_largest_count_per_severity_wins_across_summary_lines():
    text = "ANALYZE_SUMMARY critical=1 high=0 medium=4 low=0\n...\nANALYZE_SUMMARY critical=0 high=2 medium=0 low=3"
    assert G.parse_analyze_trailer(text) == {"critical": 1, "high": 2, "medium": 4, "low": 3}
    # the reply's larger count still wins over the report's (unchanged behaviour)
    r = G.parse_analyze(REPORT_NO_TABLE_HIGH2, "ANALYZE_SUMMARY critical=1 high=2 medium=1 low=0")
    assert r.trailer == {"critical": 1, "high": 2, "medium": 1, "low": 0}


# ======================================================================================================================
# (b) human-only tags defined by the repo's legend
# ======================================================================================================================

ALPHA_BKTC = """# Tasks: BKTC cutover

**Organization**: phases follow the runbook's **execution order** (FR-027: release → worker → settings →
inbound → flags → one real order → rollback/record), not story priority — for a cutover the order IS the
safety argument. Every task carries its story label; `[OPERATOR]` marks a task that is executed by a person
on the box or from the operator's machine and is **never** run by an agent (Protocol §5, FR-030). An
`[OPERATOR]` task is "done" when its stated proof is recorded in `specs/spec-BKTC-cutover-app-instance/evidence.md`
(created by T004), never on the strength of a log line alone (FR-028).

## Phase 2: Foundational

- [X] T009 [P] Record Decisions D2, D4 and D6 (as taken) and D5 (as "operator input at cutover") in the runbook §0 table if any differ from the spec's Decisions table; make the two tables identical
- [ ] T010 [OPERATOR] Box + Mac pre-flight per quickstart.md §0: `sms-toggle.sh status` (ON on both OTP units), `rail-worker-toggle.sh status` (mode OFF, pending non-legacy 0 — or "?" with the
- [ ] T011 [OPERATOR] [US1] Cut and publish the code-only release per quickstart.md §1 / runbook §3 (copy the 20260914 kit; remove `stage-data.sh`; drop the three payments-pause guards in `cutover.
"""

ALPHA_CARDW = """# Tasks — spec-CARDW

Legend: `[x]` done (evidence in the commit), `[ ]` open, `[op]` operator act (never autonomous).

- [x] T065 Smaller items: the stub's lack of a `pending` callback is documented where it lives; the gateway
- [ ] T066 [op] Record, per host, that nginx `access_log` is suppressed for `/api/payment/gateway/return`
- [x] T067 [op] On the first wallet sandbox payment, confirm the wallet redirect verifies and carries
"""

BRAVO_040 = """# Tasks: launch readiness review

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1…US5)
- **[REVIEW]**: Performed when the review is conducted, **after merge** — deliberately left unticked
- Include exact file paths in descriptions

## Phase 8: Polish

- [X] T042 Record any findings surfaced while building in `docs/post-spec-040-cleanup.md`, including anything discovered that belongs to no source record

## Phase 9: The review itself — `[REVIEW]`, performed after merge

- [ ] T043 [REVIEW] Refresh `docs/launch-readiness/gate-register.md` from the two runs' *Outstanding at the run's conclusion* tables (`docs/friends-family/readiness.md:159`, `docs/soft-pilot/sign-
- [ ] T044 [REVIEW] Close each entry that can be closed, attaching evidence of the type its `Closes through` demands with observation, by, and on; anything unverified stays `outstanding` (SC-017,

## Notes

- `[P]` = different files, no dependencies
- `[REVIEW]` = performed after merge, when the review's inputs exist; **never ticked by the implementer**
"""

BRAVO_041_T072 = "- [X] T072 [AIP §10 — performed by the operator, not by the implementer] Dual-locale browser run of `apps/admin/src/app/[locale]/(ops)/expansion/page.tsx` (RTL at `/he-IL/expa\n"

FOXTROT_004 = """# Tasks: production hardening

**Organization**: Grouped by user story in priority order (P1 → P2). **`[H]` = human-gated** (Protocol §11: server/replica/backup/account/DNS/drill actions the operator performs; Claude Code never executes these autonomously). **`[code]`** tasks are CI-verifiable immediately; **Live** tasks additionally depend on Phase-2 provisioning.

- [ ] T005 [H] **Gate 0c** — provision the **staging-parity CX11** (Hetzner; spec-001 prerequisite) — the target of the sanitized snapshot + both drills (R6/D2)
- [X] T006 [P] [US1] [code] Author `infra/observability/alerts/replica-lag.yml` — **P3** when sustained lag > 5 min (ADR-016 failover-readiness; **not** the superseded "> 30 s for 1 min"); CI-valid
"""


def human_ids(text: str) -> list[str]:
    return [t.id for t in G.parse_tasks(text) if t.human]


@pytest.mark.parametrize(
    "text, human",
    [
        (ALPHA_BKTC, ["T010", "T011"]),
        (ALPHA_CARDW, ["T066", "T067"]),
        (BRAVO_040, ["T043", "T044"]),
        (BRAVO_041_T072, ["T072"]),
        (FOXTROT_004, ["T005"]),  # `[code]` shares the legend line with `[H]` but is agent work
    ],
    ids=["alpha-BKTC-OPERATOR", "alpha-CARDW-op", "bravo-040-REVIEW", "bravo-041-AIP", "foxtrot-004-H-not-code"],
)
def test_tags_the_repos_legend_reserves_for_a_person_are_human_only(text, human):
    assert human_ids(text) == human


def test_a_legend_tag_counts_only_among_the_leading_tags_and_only_in_its_own_file():
    assert human_ids("- [ ] T010 [OPERATOR] Box pre-flight\n") == []  # no legend: an unknown tag says nothing
    in_prose = ALPHA_BKTC + "- [ ] T012 Write the runbook section for the `[OPERATOR]` steps\n"
    assert "T012" not in human_ids(in_prose)


def test_ticking_a_legend_tagged_task_now_fails_the_implement_gate(tmp_path):
    from tests.fakeworld import make_repo

    repo = make_repo(tmp_path)
    fd = repo / "specs" / "001-bktc"
    fd.mkdir(parents=True)
    (fd / "tasks.md").write_text(ALPHA_BKTC)
    before = {t.id: t.done for t in G.human_tasks(fd)}
    assert before == {"T010": False, "T011": False}
    (fd / "tasks.md").write_text(ALPHA_BKTC.replace("- [ ] T010", "- [X] T010").replace("- [ ] T011", "- [X] T011"))
    assert G.task_progress(fd) == (1, 1)  # the operator tasks are not agent work
    res = G.gate_implement(repo, "main", fd, None, human_before=before)
    assert "human-tasks-untouched" in failed_names(res)


# ======================================================================================================================
# (c) label look-alikes (SYNTHETIC near-real lines: the repos have none today)
# ======================================================================================================================


@pytest.mark.parametrize(
    "text",
    [
        "Owner-only (`requireOwner`) guard on PATCH /api/listings/:id in apps/api/src/listings/routes.ts",
        "Owner-only: DELETE /api/listings/:id returns 403 for non-owners (FR-012)",
        "Operator-only (RBAC ops:write) endpoints in apps/api/src/ops/routes.ts",
        "Operator: add the /ops/stuck-orders page in apps/admin/src/app/ops/stuck-orders/page.tsx",
        "Person: add the person table and its migration in db/migrations/0042_person.sql",
        "Founder review: GET /admin/reviews lists pending reviews (apps/api/src/admin/reviews.ts)",
        "Write docs/runbooks/bank-transfer-cutover.md (operator steps for the cutover, one per line)",
    ],
)
def test_authorisation_rules_endpoints_and_runbook_docs_stay_agent_work(text):
    assert G.is_human_task(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Owner-only: rotate the webhook secret in the provider console",
        "OPERATOR: rotate the staging API key",
        "**Operator**: rotate the staging API key",
        "Operator — when authorized, verify Search Console property/access",
        "PERSON: SC-001 (a person submits 20 listings with photos from a filled template; time it)",
        "Founder review: tick the boxes in specs/001-platform-01-monorepo/checklists/requirements.md",
        "Register the flags in the local Unleash instance. *(Operator step — flag constants are declared in `constants.ts`)*",
        "Review per docs/workflows/review.md (operator step: `/cc-review` is user-invoked): the read-only agents",
    ],
)
def test_real_label_forms_stay_human(text):
    assert G.is_human_task(text) is True


# ======================================================================================================================
# (d) human-only tasks that are the pipeline's own steps
# ======================================================================================================================

PIPELINE_STEP_LINES = [
    # golf 004 T077 / 005 T099 / 006 T079 / 008 T083, golf 003 T212, Bravo 078 T062 / T063 (verbatim)
    "- [ ] T077 Operator steps (not for agents): `/cc-review` (`api-reviewer`, `db-architect`, `integration-reviewer`,\n  `security-review`), then `/cc-ship` (it runs `cc-linear-sync`, which moves ENG-94 forward), then the PR.\n",
    "- [ ] T099 Operator steps (not for agents): `/cc-review`, then `/cc-ship` (it runs `cc-linear-sync`, which moves ENG-96\n  forward), then the PR.\n",
    "- [X] T079 Operator steps (not for agents): `/cc-review`, then `/cc-ship` (it runs `cc-linear-sync`), then the first PR.\n",
    "- [ ] T212 Review per docs/workflows/review.md (operator step: `/cc-review` is user-invoked): the read-only\n  `api-reviewer`, `db-architect`, `security-review`, `web-reviewer`, `integration-reviewer` and `feature-validator`\n",
    "- [x] T062 **Operator-run**: `/cc-review`. Its reviewers are `security-review` (permissions, token\n  clearing), `web-reviewer` (no web change; skip if the operator agrees), and a courier-focused\n",
    "- [x] T063 **Operator-run**: `/cc-ship` for `specs/078-driver-app-sdk57-test-runner/`. It runs the\n  gate and syncs Linear ENG-1322 (Todo → In Progress → In Review). It is user-invoked, so ask the\n",
]


@pytest.mark.parametrize("line", PIPELINE_STEP_LINES, ids=["T077", "T099", "T079", "T212", "T062", "T063"])
def test_operator_review_and_ship_steps_are_pipeline_steps(line):
    (t,) = G.parse_tasks(line)
    assert t.human
    assert G.pipeline_step(t, ("/cc-review", "/cc-ship"))


@pytest.mark.parametrize(
    "line",
    [
        "- [ ] T001 FOUNDER GATE: approve the release notes, then `/cc-ship`\n",
        "- [ ] T007 *(Operator step — not for agents)* rotate the staging API key in the dashboard\n",
        "- [ ] T064 FOUNDER: the human smoke test (quickstart §3; protocol §10) before /cc-ship\n",
    ],
)
def test_a_persons_task_that_merely_mentions_ship_is_not_a_pipeline_step(line):
    (t,) = G.parse_tasks(line)
    assert t.human and not G.pipeline_step(t, ("/cc-review", "/cc-ship"))


def test_pipeline_steps_follow_the_configured_command_names():
    (t,) = G.parse_tasks("- [ ] T050 **Operator-run**: `/repo-review` for the branch\n")
    assert not G.pipeline_step(t, ("/cc-review", "/cc-ship"))
    assert G.pipeline_step(t, ("/repo-review", "/repo-ship"))


# ======================================================================================================================
# (e) the test baseline and non-test failures (real runner output)
# ======================================================================================================================


@pytest.mark.parametrize(
    "name, why",
    [
        ("vitest-teardown-error", "error during close"),
        ("jest-globalteardown-error", "globalTeardown"),
        ("jest-obsolete-red", "obsolete"),
        ("jest-obsolete-green", "obsolete"),
        ("pytest-unconfigure-error", "pytest"),
    ],
)
def test_failures_outside_any_test_are_unattributable(name, why):
    ids, reason = G.explain_test_failures(output(name))
    assert ids is None, reason
    assert why.lower() in reason.lower()


@pytest.mark.parametrize("name, red", [("vitest-teardown-error", "a.test.js > suite > pre-existing red"), ("jest-globalteardown-error", "a.test.js > suite > pre-existing red"), ("jest-obsolete-red", "a.test.js > suite > pre-existing red"), ("pytest-unconfigure-error", "test_a.py::test_red")])
def test_the_baseline_never_excuses_a_run_that_also_failed_outside_its_tests(name, red):
    excused, detail = G.baseline_verdict(run_result(name), {red})
    assert not excused, detail
    assert detail.startswith("baseline not applied")


def test_an_already_excused_failing_run_is_not_rerun_for_flakiness():
    runner = ScriptedRunner({"pnpm test": [run_result("vitest-multi", "pnpm test")] * 2})
    tr = G.run_tooling({"typecheck": None, "lint": None, "test": "pnpm test", "build": None}, Path("/tmp"), runner, 60, baseline={"test": EXPECTED_FAILURES["vitest-multi"]})
    assert runner.calls == ["pnpm test"]
    assert tr.passed("test") and tr.retried == [] and "test" in tr.baseline_ok


def test_a_failing_run_with_new_failures_is_still_rerun_once():
    runner = ScriptedRunner({"pnpm test": [run_result("vitest-multi", "pnpm test")] * 2})
    tr = G.run_tooling({"typecheck": None, "lint": None, "test": "pnpm test", "build": None}, Path("/tmp"), runner, 60, baseline={"test": set()})
    assert runner.calls == ["pnpm test", "pnpm test"] and tr.retried == ["test"] and not tr.passed("test")


def test_without_a_baseline_a_failing_test_run_is_rerun_once():
    tr = tooling_with([CmdResult("pnpm test", 1, "x", ""), CmdResult("pnpm test", 0, "ok", "")], None)
    assert tr.retried == ["test"] and tr.passed("test")
    assert by_name_ok(tr)


def by_name_ok(tr: G.ToolingResult) -> bool:
    return all(c.passed for c in tr.checks() if c.name == "test-passes")


def test_the_worker_is_told_that_a_persons_tagged_task_is_never_its_own():
    from speckit_autopilot.prompts import WORKER_SYSTEM_APPEND

    assert "[OPERATOR]" in WORKER_SYSTEM_APPEND and "[REVIEW]" in WORKER_SYSTEM_APPEND
    assert "the decision agent \\\ncannot do it either" not in WORKER_SYSTEM_APPEND  # one logical line in the prompt
    assert "cannot do it either: do not do it, do not write its evidence, and never tick it." in WORKER_SYSTEM_APPEND
