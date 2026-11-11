"""Gates added in the "fix all" round, tested on REAL formats:

(a) human-only tasks: fixtures/tasks/human-labels.tasks.md holds 51 task lines (28 human-only, 23 look-alikes that
    are agent work) modelled on five real repos with synthetic content; fixtures/tasks/golf-024-founder.tasks.md is a
    full tasks.md of the same kind with a done FOUNDER GATE and two open FOUNDER tasks. The classifier was measured on 15,153 real task lines
    (145 human-only, every one reviewed by hand; no false positive).
(b) clarify session-date tolerance (``since``).
(c) test-failure baseline: fixtures/runners/ holds stdout/stderr captured from the real runners (vitest 4.1, jest 30,
    pytest 9 + pytest-cov 7, node --test spec/TAP, playwright 1.61, pnpm -r, turbo 2.9) run on throwaway tests, plus
    documented mocha / go test output (not installed here); MANIFEST.json records command, exit code and provenance.
(d) the ANALYZE_SUMMARY trailer, on top of the real analyze reports in fixtures/analyze/.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.fakeworld import make_repo, sh
from speckit_autopilot import gates as G
from speckit_autopilot.repo import CmdResult

FIX = Path(__file__).parent / "fixtures"
RUNNERS = FIX / "runners"
MANIFEST: dict[str, dict] = json.loads((RUNNERS / "MANIFEST.json").read_text(encoding="utf-8"))


def fx(rel: str) -> str:
    return (FIX / rel).read_text(encoding="utf-8")


def by_name(result: G.GateResult) -> dict[str, G.Check]:
    return {c.name: c for c in result.checks}


def failed_names(result: G.GateResult) -> set[str]:
    return {c.name for c in result.checks if not c.passed and not c.warn_only}


def run_result(name: str, cmd: str | None = None) -> CmdResult:
    """The captured run ``name`` as the CmdResult repo.run_shell would have returned."""
    m = MANIFEST[name]
    return CmdResult(cmd or m["cmd"], m["exit"], (RUNNERS / f"{name}.out").read_text(encoding="utf-8"), (RUNNERS / f"{name}.err").read_text(encoding="utf-8"), 12.0)


def output(name: str) -> str:
    r = run_result(name)
    return r.stdout + "\n" + r.stderr


# ======================================================================================================================
# (a) human-only tasks
# ======================================================================================================================

EXPECT = re.compile(r"^<!-- expect: (?P<kind>human|agent) \| (?P<src>\S+) \| (?P<why>.*) -->$", re.M)


def human_label_blocks() -> list[tuple[str, str, str, str]]:
    text = fx("tasks/human-labels.tasks.md")
    marks = list(EXPECT.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group("kind"), m.group("src"), m.group("why"), text[m.end(): end]))
    return out


BLOCKS = human_label_blocks()


def test_human_label_fixture_is_large_and_mixed():
    kinds = [b[0] for b in BLOCKS]
    assert len(BLOCKS) >= 12 and kinds.count("human") >= 12 and kinds.count("agent") >= 12
    assert {b[1].split("/")[0] for b in BLOCKS} == {"golf", "foxtrot", "alpha", "charlie", "bravo"}


@pytest.mark.parametrize("kind, src, why, block", BLOCKS, ids=[f"{b[0]}-{b[1].split('/specs/')[0]}-{b[1].rsplit(':', 1)[1]}" for b in BLOCKS])
def test_real_task_lines_classify_as_expected(kind, src, why, block):
    tasks = G.parse_tasks(block)
    assert len(tasks) == 1, src
    assert tasks[0].human is (kind == "human"), f"{src}: {why}\n{tasks[0].text[:160]}"


@pytest.mark.parametrize(
    "text, human",
    [
        # SYNTHETIC look-alikes: a bare label needs ":" or a dash, so these stay agent work
        ("Operator (admin) dashboard page in apps/admin/src/app/ops/page.tsx", False),
        ("Operator, owner and admin roles in packages/rbac/src/roles.ts", False),
        ("Founder approval flow UI in apps/web/src/approvals.tsx", False),
        ("Human-readable error messages for the import wizard", False),
        ("Personal data export endpoint (FR-012) in apps/api/src/export.ts", False),
        ("Founders' guide page in docs/guide.md", False),
        ("[P] Gate: the founder-ticked checklist is committed before T005", False),
        # SYNTHETIC label variants of the real forms
        ("**FOUNDER GATE**: a founder ticks the boxes", True),
        ("[P] [US2] HUMAN GATE — two reviewers sign off on 0042_x.sql", True),
        ("Owner-only: rotate the webhook secret in the provider console", True),
        ("Ship the release notes (not for agents)", True),
    ],
)
def test_is_human_task_label_rules(text, human):
    assert G.is_human_task(text) is human


def test_wrapped_first_paragraph_counts_but_sub_bullets_do_not():
    assert G.is_human_task("Schema Council paired review of `0042_x.sql` — two reviewers. **HUMAN GATE — MUST NOT be", "ticked by any agent.**\n")
    assert not G.is_human_task("Run the baseline checks.", "- note: the operator steps (not for agents) come later\n")


def golf_024(tmp: Path, *, edit: tuple[str, str] | None = None) -> Path:
    text = fx("tasks/golf-024-founder.tasks.md")
    if edit:
        assert text.count(edit[0]) == 1, edit[0]
        text = text.replace(*edit)
    fd = tmp / "specs" / "024-seed-01-supply-playbook"
    fd.mkdir(parents=True, exist_ok=True)
    (fd / "tasks.md").write_text(text, encoding="utf-8")
    return fd


T009_OPEN = "- [ ] T009 [US1] Rehearse V2, V3 and V4"
T009_DONE = "- [X] T009 [US1] Rehearse V2, V3 and V4"
T023_OPEN = "- [ ] T023 [US5] FOUNDER: rehearse V8"
T023_DONE = "- [X] T023 [US5] FOUNDER: rehearse V8"


def test_parse_tasks_flags_the_real_founder_tasks(tmp_path):
    tasks = G.feature_tasks(golf_024(tmp_path))
    assert len(tasks) == 28
    assert [(t.id, t.done) for t in tasks if t.human] == [("T001", True), ("T023", False), ("T028", False)]
    # T008 asks a founder to read the translation, T010 sends gaps to the founders: both stay agent work
    assert not {t.id for t in tasks if t.human} & {"T008", "T010"}


def test_task_progress_counts_agent_tasks_only(tmp_path):
    assert G.task_progress(golf_024(tmp_path)) == (24, 25)  # T009 (an agent rehearsal) is open
    assert G.task_progress(golf_024(tmp_path / "b", edit=(T009_OPEN, T009_DONE))) == (25, 25)


def test_human_tasks_lists_human_only_tasks_with_done_flags(tmp_path):
    hs = G.human_tasks(golf_024(tmp_path))
    assert [(t.id, t.done, t.human) for t in hs] == [("T001", True, True), ("T023", False, True), ("T028", False, True)]
    assert G.human_tasks(tmp_path / "missing") == []


@pytest.fixture
def repo(tmp_path) -> Path:
    return make_repo(tmp_path)


def feature_in_repo(repo: Path, edit: tuple[str, str] | None = None) -> Path:
    sh(repo, "git", "checkout", "-q", "-b", "feature/024-seed-01")
    fd = golf_024(repo, edit=edit)
    (repo / "src" / "playbook.py").write_text("STEPS = 8\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "work")
    return fd


def test_gate_implement_passes_with_open_founder_tasks_and_warns(repo):
    fd = feature_in_repo(repo, edit=(T009_OPEN, T009_DONE))
    res = G.gate_implement(repo, "main", fd, None)
    assert res.passed, res.failed()
    assert (res.facts["tasks_done"], res.facts["tasks_total"]) == (25, 25)
    assert [h.split()[0] for h in res.facts["human_open"]] == ["T023", "T028"]
    assert res.facts["human_open"][0].startswith("T023 [US5] FOUNDER: rehearse V8 on a scratch copy") and len(res.facts["human_open"][0]) == 5 + 80
    warn = by_name(res)["human-only-tasks"]
    assert warn.warn_only and not warn.passed and "T023" in warn.detail and "T028" in warn.detail
    assert "25/25 agent tasks" in by_name(res)["all-tasks-checked"].detail and "3 human-only" in by_name(res)["all-tasks-checked"].detail
    assert "human-tasks-untouched" not in by_name(res)  # only checked when human_before is given


def test_gate_implement_open_agent_task_still_fails(repo):
    fd = feature_in_repo(repo)
    res = G.gate_implement(repo, "main", fd, None)
    assert failed_names(res) == {"all-tasks-checked"}
    assert "24/25" in by_name(res)["all-tasks-checked"].detail


def test_gate_implement_fails_when_an_agent_ticks_a_founder_task(repo):
    before = {t.id: t.done for t in G.human_tasks(golf_024(repo.parent / "before"))}
    assert before == {"T001": True, "T023": False, "T028": False}
    fd = feature_in_repo(repo, edit=(T009_OPEN, T009_DONE))
    (fd / "tasks.md").write_text((fd / "tasks.md").read_text().replace(T023_OPEN, T023_DONE))
    res = G.gate_implement(repo, "main", fd, None, human_before=before)
    assert failed_names(res) == {"human-tasks-untouched"}
    assert "T023 [US5] FOUNDER" in by_name(res)["human-tasks-untouched"].detail
    assert [h.split()[0] for h in res.facts["human_open"]] == ["T028"]


def test_gate_implement_human_before_untouched_and_removed(repo):
    fd = feature_in_repo(repo, edit=(T009_OPEN, T009_DONE))
    before = {t.id: t.done for t in G.human_tasks(fd)}
    ok = G.gate_implement(repo, "main", fd, None, human_before=before)
    assert ok.passed and by_name(ok)["human-tasks-untouched"].passed
    # a founder task deleted from tasks.md is caught too (it would otherwise vanish from every count)
    text = (fd / "tasks.md").read_text()
    start = text.index("- [ ] T028 FOUNDER")
    (fd / "tasks.md").write_text(text[:start] + text[text.index("\n---", start):])
    gone = G.gate_implement(repo, "main", fd, None, human_before=before)
    assert failed_names(gone) == {"human-tasks-untouched"} and "T028 (removed from tasks.md)" in by_name(gone)["human-tasks-untouched"].detail


def test_gate_implement_only_human_tasks_is_not_done(repo):
    fd = feature_in_repo(repo)
    (fd / "tasks.md").write_text("# Tasks\n\n## Phase 0: Gates\n\n- [ ] T001 FOUNDER GATE: a founder ticks the boxes\n")
    res = G.gate_implement(repo, "main", fd, None)
    assert "all-tasks-checked" in failed_names(res) and "0/0 agent tasks" in by_name(res)["all-tasks-checked"].detail


def test_gate_tasks_reports_human_task_ids(tmp_path):
    res = G.gate_tasks(golf_024(tmp_path), tmp_path)
    assert res.facts["human_tasks"] == ["T001", "T023", "T028"]


# ======================================================================================================================
# (b) clarify: since <= session date <= today
# ======================================================================================================================


def clarify_dir(tmp: Path) -> Path:
    d = tmp / "specs" / "115-feature"
    d.mkdir(parents=True)
    (d / "spec.md").write_text(fx("spec/bravo-115.spec.md"), encoding="utf-8")  # one "### Session 2026-11-08"
    return d


@pytest.mark.parametrize(
    "today, since, ok",
    [
        ("2026-11-10", None, False),  # today only (unchanged default)
        ("2026-11-08", None, True),
        ("2026-11-10", "2026-11-08", True),  # run started on the 8th, resumed on the 10th
        ("2026-11-10", "2026-11-07", True),
        ("2026-11-10", "2026-11-09", False),  # session predates the run
        ("2026-11-07", "2026-11-01", False),  # session after "today" never counts
        ("2026-11-08", "2026-11-08", True),
    ],
)
def test_gate_clarify_since_window(tmp_path, today, since, ok):
    res = G.gate_clarify(clarify_dir(tmp_path), today, since=since)
    assert by_name(res)["clarifications-session"].passed is ok
    if not ok:
        assert "2026-11-08" in by_name(res)["clarifications-session"].detail


def test_gate_clarify_since_keeps_nothing_to_clarify(tmp_path):
    assert G.gate_clarify(clarify_dir(tmp_path), "2026-11-10", nothing_to_clarify=True, since="2026-11-09").passed


# ======================================================================================================================
# (c) test-failure baseline
# ======================================================================================================================

EXPECTED_FAILURES: dict[str, set[str] | None] = {
    # vitest 4.1 (default reporter, CI=1; FAIL lines on stderr, summary on stdout)
    "vitest-pass": set(),
    "vitest-single": {"cart.test.ts > cart > applies the coupon"},
    "vitest-multi": {"src/orders/fsm.test.ts > order FSM > cancel > refuses after shipping", "src/orders/fsm.test.ts > order FSM > starts pending", "src/pay/gateway.test.ts > rejects a bad HMAC"},
    "vitest-multi-fixed": {"src/orders/fsm.test.ts > order FSM > cancel > refuses after shipping", "src/pay/gateway.test.ts > rejects a bad HMAC"},
    "vitest-multi-new": {"src/ok.test.ts > totals the cart", "src/orders/fsm.test.ts > order FSM > cancel > refuses after shipping", "src/pay/gateway.test.ts > rejects a bad HMAC"},
    "vitest-projects": {"|@demo/web| src/index.test.ts > renders", "|@demo/api| src/index.test.ts > serves"},
    "vitest-importerr": None,  # "Failed Suites 1": Cannot find module
    "vitest-syntaxerr": None,  # "Failed Suites 1": Transform failed (ANSI-coloured oxc error)
    "vitest-nofiles": None,  # "No test files found, exiting with code 1"
    "vitest-unhandled": None,  # all tests passed but "Errors  1 error" (unhandled rejection)
    # jest 30 (everything on stderr; code frames carry ANSI codes even with CI=1)
    "jest-pass": set(),
    "jest-single": {"cart.test.js > cart > applies the coupon"},
    "jest-multi": {"src/orders/fsm.test.js > order FSM > cancel > refuses after shipping", "src/orders/fsm.test.js > order FSM > starts pending", "src/pay/gateway.test.js > rejects a bad HMAC"},
    "jest-importerr": None,  # "Test suite failed to run"
    # pytest 9 (-q and default); setup ERRORs are attributable to their test
    "pytest-pass": set(),
    "pytest-single": {"test_math.py::test_sub"},
    "pytest-multi": {"tests/integration/test_db.py::test_plain", "tests/integration/test_db.py::test_select", "tests/unit/test_prices.py::test_rounding", "tests/unit/test_prices.py::TestCurrency::test_symbol", "tests/unit/test_prices.py::test_eq[1-2]"},
    "pytest-multi-default": {"tests/integration/test_db.py::test_plain", "tests/integration/test_db.py::test_select", "tests/unit/test_prices.py::test_rounding", "tests/unit/test_prices.py::TestCurrency::test_symbol", "tests/unit/test_prices.py::test_eq[1-2]"},
    "pytest-importerr": None,  # "ERROR collecting" / "Interrupted: 1 error during collection"
    "pytest-cov-fail": None,  # tests pass, "FAIL Required test coverage of 85% not reached" (foxtrot's --cov-fail-under=85)
    # node --test, Node 24 (spec reporter is the default even when piped) and TAP
    "nodetest-pass": set(),
    "nodetest-pass-tap": set(),
    "nodetest-multi": {"lib/crawl.test.mjs > honours disallow", "lib/parse.test.mjs > extracts the title", "lib/parse.test.mjs > inner fails"},
    "nodetest-multi-tap": {"crawl.test.mjs > robots > honours disallow", "parse.test.mjs > extracts the title", "parse.test.mjs > outer > inner fails"},
    "nodetest-syntaxerr": None,
    "nodetest-syntaxerr-tap": None,
    # playwright 1.61: dot reporter (default with CI=1) and list; flaky tests are not failures; line:col dropped
    "playwright-pass": set(),
    "playwright-pass-list": set(),
    "playwright-multi": {"tests/account.spec.ts > logs in", "tests/checkout.spec.ts > checkout > pays with COD"},
    "playwright-multi-list": {"tests/account.spec.ts > logs in", "tests/checkout.spec.ts > checkout > pays with COD"},
    "playwright-retries": {"tests/flaky.spec.ts > always fails"},
    "playwright-retries-list": {"tests/flaky.spec.ts > always fails"},
    "playwright-importerr": None,
    "playwright-importerr-list": None,
    # workspace wrappers: package-qualified ids; a bailing wrapper hides packages that never ran
    "pnpm-r-bail": None,  # ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL
    "pnpm-r-nobail": {"[packages/api] src/orders.test.ts > lists orders", "[packages/web] src/home.test.ts > home > renders hero"},
    "pnpm-r-nobail-branch": {"[packages/web] src/home.test.ts > home > renders hero"},
    "turbo-bail": None,  # "Tasks: 0 successful, 3 total" + one failed: two packages never ran
    "turbo-continue": {"[@demo/api] src/orders.test.ts > lists orders", "[@demo/web] src/home.test.ts > home > renders hero"},
    "turbo-continue-branch": {"[@demo/web] src/home.test.ts > home > renders hero"},
    # documented formats (runner not installed here)
    "mocha-pass": set(),
    "mocha-fail": {"Array > #indexOf() > should return the index when present", "Cart > totals > applies the coupon"},
    "mocha-hook": None,
    "go-pass": set(),
    "go-fail": {"example.com/crawler/price > TestParsePrice", "example.com/crawler/price > TestRobots/disallow"},
    "go-build-failed": None,
    # third review (real runners): a red test plus a failure outside any test is never pinned test by test
    "vitest-teardown-error": None,  # "error during close Error: teardown boom" (globalSetup teardown)
    "jest-globalteardown-error": None,  # "Jest: Got error running globalTeardown"
    "jest-obsolete-red": None,  # "Snapshots: 1 obsolete" fails the run under --ci
    "jest-obsolete-green": None,  # every test passes, exit 1 for the obsolete snapshot alone
    "pytest-unconfigure-error": None,  # a conftest pytest_unconfigure hook raised after the summary
}


def test_every_runner_fixture_has_an_expectation():
    assert set(EXPECTED_FAILURES) == set(MANIFEST)


@pytest.mark.parametrize("name", sorted(EXPECTED_FAILURES))
def test_parse_test_failures_on_real_runner_output(name):
    got = G.parse_test_failures(output(name))
    assert got == EXPECTED_FAILURES[name], G.explain_test_failures(output(name))[1]
    if MANIFEST[name]["exit"] == 0:
        assert got == set()


def test_parse_test_failures_strips_ansi_from_real_turbo_output():
    raw = output("turbo-continue")
    assert "\x1b[41m\x1b[1m FAIL" in raw  # turbo forces colour on the child vitest
    assert G.parse_test_failures(raw) == G.parse_test_failures(G.ANSI.sub("", raw))


def test_parse_test_failures_progress_redraws_and_crlf():
    raw = output("vitest-single").replace("\n", "\r\n").replace(" RUN  v4.1.10", " RUN  v4.1.10 ⠋ collecting\r RUN  v4.1.10")
    assert G.parse_test_failures(raw) == {"cart.test.ts > cart > applies the coupon"}


def test_pnpm_single_segment_package_prefix():
    """DERIVED from pnpm-r-nobail: packages at the workspace root print "web test: …" (no packages/ directory)."""
    raw = output("pnpm-r-nobail").replace("packages/", "")
    assert G.parse_test_failures(raw) == {"[api] src/orders.test.ts > lists orders", "[web] src/home.test.ts > home > renders hero"}


@pytest.mark.parametrize(
    "name, edit, why",
    [
        # DERIVED: one FAIL line dropped, so the summary's "3 failed" cannot be matched test by test
        ("vitest-multi", (" FAIL  src/orders/fsm.test.ts > order FSM > starts pending\n", ""), "reports 3 failure(s) but 2"),
        # DERIVED: output cut before the summary
        ("vitest-single", ("      Tests  1 failed | 1 passed (2)\n", ""), "without the runner's summary"),
        # DERIVED: turbo says a third package failed but its output names no failing test (e.g. a crash before vitest)
        ("turbo-continue", ("Failed:    @demo/api#test, @demo/web#test", "Failed:    @demo/api#test, @demo/db#test, @demo/web#test"), "package @demo/db failed"),
        # DERIVED: pnpm marks a package Failed whose own output shows only passing tests
        ("pnpm-r-nobail", ("packages/db test: Done", "packages/db test: Failed"), "package packages/db failed"),
        # DERIVED: a per-test timeout is never excused
        ("jest-single", ("expect(received).toBe(expected) // Object.is equality", "thrown: \"Exceeded timeout of 5000 ms for a test.\""), "a timeout"),
        # DERIVED: pytest stopped at the first failure (-x)
        ("pytest-single", ("1 failed, 1 passed in", "!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!\n1 failed, 1 passed in"), "stopped early"),
    ],
)
def test_parse_test_failures_returns_none_when_not_attributable(name, edit, why):
    raw = output(name)
    assert raw.count(edit[0]) == 1, edit[0]
    ids, reason = G.explain_test_failures(raw.replace(*edit))
    assert ids is None and why in reason, reason


def test_parse_test_failures_garbage_and_empty():
    assert G.parse_test_failures("") is None
    assert G.parse_test_failures("sh: pnpm: command not found\n") is None
    assert G.parse_test_failures("Error: connect ECONNREFUSED 127.0.0.1:5432\n") is None


def test_baseline_from_result():
    assert G.baseline_from_result(CmdResult("pnpm test", 0, "anything")) == set()
    assert G.baseline_from_result(CmdResult("pnpm test", 124, "partial", "", 600.0, timed_out=True)) is None
    assert G.baseline_from_result(run_result("vitest-multi")) == EXPECTED_FAILURES["vitest-multi"]
    assert G.baseline_from_result(run_result("pnpm-r-bail")) is None


class ScriptedRunner:
    def __init__(self, script: dict[str, list[CmdResult]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    def __call__(self, cmd: str, cwd: Path, timeout: float) -> CmdResult:
        self.calls.append(cmd)
        seq = self.script.get(cmd)
        return seq.pop(0) if seq else CmdResult(cmd, 0, "ok")


def tooling_with(test_runs: list[CmdResult], baseline: dict[str, set[str]] | None, cmd: str = "pnpm test") -> G.ToolingResult:
    cmds = {"typecheck": "pnpm typecheck", "lint": None, "test": cmd, "build": None}
    return G.run_tooling(cmds, Path("/tmp"), ScriptedRunner({cmd: test_runs}), 60, baseline=baseline)


def agrees(tr: G.ToolingResult) -> bool:
    return tr.ok == all(c.passed or c.warn_only for c in tr.checks())


@pytest.mark.parametrize(
    "base, branch",
    [("vitest-multi", "vitest-multi-fixed"), ("turbo-continue", "turbo-continue-branch"), ("pnpm-r-nobail", "pnpm-r-nobail-branch"), ("vitest-multi", "vitest-multi")],
)
def test_run_tooling_excuses_only_preexisting_failures(base, branch):
    baseline = {"test": G.baseline_from_result(run_result(base))}
    tr = tooling_with([run_result(branch, "pnpm test"), run_result(branch, "pnpm test")], baseline)
    assert tr.ok and agrees(tr) and tr.retried == []  # already excused: a red-on-base suite is not run twice
    n = len(EXPECTED_FAILURES[branch])
    assert tr.baseline_ok["test"].startswith(f"{n} failing test(s), all already failing on base: ")
    checks = {c.name: c for c in tr.checks()}
    assert checks["test-passes"].passed and "all already failing on base" in checks["test-passes"].detail
    assert checks["test-preexisting-failures"].warn_only and not checks["test-preexisting-failures"].passed


def test_run_tooling_names_new_failures():
    baseline = {"test": G.baseline_from_result(run_result("vitest-multi-fixed"))}
    tr = tooling_with([run_result("vitest-multi-new", "pnpm test")] * 2, baseline)
    assert not tr.ok and agrees(tr) and not tr.baseline_ok
    check = {c.name: c for c in tr.checks()}["test-passes"]
    assert not check.passed
    assert "1 new failing test(s) not failing on base (2 pre-existing): src/ok.test.ts > totals the cart" in check.detail
    assert tr.baseline_notes["test"] in check.detail


@pytest.mark.parametrize(
    "base, branch, cmd, why",
    [
        ("vitest-multi", "vitest-importerr", "pnpm test", "a test file failed to load"),
        ("pnpm-r-nobail", "pnpm-r-bail", "pnpm test", "stopped early"),
        ("turbo-continue", "turbo-bail", "pnpm test", "turbo stopped"),
        # foxtrot's test_cmd chains pnpm -r and pytest: an early pre-existing failure would hide the pytest suite
        ("vitest-multi", "vitest-multi-fixed", "pnpm -r --if-present test && uv run --directory pipeline pytest -q", "chains commands"),
        ("pytest-single", "pytest-single", "uv run pytest -x -q", "stops at the first failure"),
    ],
)
def test_run_tooling_fails_as_today_when_the_baseline_cannot_vouch(base, branch, cmd, why):
    baseline = {"test": G.baseline_from_result(run_result(base))}
    tr = tooling_with([run_result(branch, cmd)] * 2, baseline, cmd=cmd)
    assert not tr.ok and agrees(tr) and why in tr.baseline_notes["test"]


def test_run_tooling_unknown_or_empty_baseline_and_no_baseline():
    unknown = tooling_with([run_result("vitest-multi", "pnpm test")] * 2, {"test": None})  # base itself unparseable
    assert not unknown.ok and "unknown" in unknown.baseline_notes["test"]
    clean_base = tooling_with([run_result("vitest-single", "pnpm test")] * 2, {"test": set()})
    assert not clean_base.ok and "1 new failing test(s)" in clean_base.baseline_notes["test"]
    plain = tooling_with([run_result("vitest-single", "pnpm test")] * 2, None)
    assert not plain.ok and not plain.baseline_notes and not plain.baseline_ok and agrees(plain)
    other_kind = tooling_with([run_result("vitest-single", "pnpm test")] * 2, {"lint": set()})
    assert not other_kind.ok and not other_kind.baseline_notes


def test_run_tooling_flaky_pass_and_timeout_ignore_the_baseline():
    baseline = {"test": EXPECTED_FAILURES["vitest-multi"]}
    flaky = tooling_with([run_result("vitest-single", "pnpm test"), CmdResult("pnpm test", 0, "ok")], baseline)
    assert flaky.ok and not flaky.baseline_ok and flaky.retried == ["test"]
    timeout = tooling_with([CmdResult("pnpm test", 124, output("vitest-multi"), "", 60.0, timed_out=True)], baseline)
    assert not timeout.ok and timeout.retried == [] and "timed out" in timeout.baseline_notes["test"]


def test_baseline_never_excuses_typecheck_or_lint():
    cmds = {"typecheck": "pnpm typecheck", "lint": "pnpm lint", "test": "pnpm test", "build": None}
    run = ScriptedRunner({"pnpm lint": [run_result("vitest-multi", "pnpm lint")]})
    tr = G.run_tooling(cmds, Path("/tmp"), run, 60, baseline={"test": EXPECTED_FAILURES["vitest-multi"], "lint": EXPECTED_FAILURES["vitest-multi"]})
    assert not tr.ok and "lint" not in tr.baseline_ok and not tr.passed("lint") and tr.passed("test")


def test_gate_implement_and_review_accept_an_excused_test_run(repo):
    fd = feature_in_repo(repo, edit=(T009_OPEN, T009_DONE))
    tr = tooling_with([run_result("vitest-multi-fixed", "pnpm test")] * 2, {"test": EXPECTED_FAILURES["vitest-multi"]})
    res = G.gate_implement(repo, "main", fd, tr)
    assert res.passed, res.failed()
    assert any("test-preexisting-failures" in w for w in res.warnings())
    review = G.gate_review(G.parse_review("Blockers: none"), tr)
    assert review.passed


# ======================================================================================================================
# (d) ANALYZE_SUMMARY trailer
# ======================================================================================================================


@pytest.mark.parametrize(
    "text, expected",
    [
        ("ANALYZE_SUMMARY critical=0 high=1 medium=3 low=5", {"critical": 0, "high": 1, "medium": 3, "low": 5}),
        ("`ANALYZE_SUMMARY critical=0 high=0 medium=2 low=1`", {"critical": 0, "high": 0, "medium": 2, "low": 1}),
        ("**ANALYZE_SUMMARY** LOW=4 Medium=3 HIGH=2 critical=1", {"critical": 1, "high": 2, "medium": 3, "low": 4}),
        ("```\nANALYZE_SUMMARY: critical=0 high=0 medium=0 low=0\n```", {"critical": 0, "high": 0, "medium": 0, "low": 0}),
        ("ANALYZE_SUMMARY critical=0 high=0 medium=0 low=0\n...\nANALYZE_SUMMARY critical=1 high=0 medium=0 low=0", {"critical": 1, "high": 0, "medium": 0, "low": 0}),
        ("ANALYZE_SUMMARY critical=2 high=1 medium=0 low=0\nANALYZE_SUMMARY critical=0 high=0", {"critical": 2, "high": 1, "medium": 0, "low": 0}),  # incomplete line ignored
        ("Report.\n> ANALYZE_SUMMARY critical=0, high=0, medium=1, low=0", {"critical": 0, "high": 0, "medium": 1, "low": 0}),
        ("The summary: ANALYZE_SUMMARY critical=0 high=0 medium=0 low=0", None),  # only a line of its own counts
        ("ANALYZE_SUMMARY critical=0 high=0", None),
        ("The ANALYZE_SUMMARY line is below.", None),
        ("", None),
    ],
)
def test_parse_analyze_trailer(text, expected):
    assert G.parse_analyze_trailer(text) == expected


def test_parse_analyze_is_backward_compatible_and_stores_the_trailer():
    report = G.parse_analyze(fx("analyze/echo-one-high.md"))
    assert report.trailer is None and report.blocking == 1 and report.effective_counts == report.counts
    with_trailer = G.parse_analyze(fx("analyze/echo-one-high.md"), "ANALYZE_SUMMARY critical=0 high=1 medium=3 low=5")
    assert with_trailer.trailer == {"critical": 0, "high": 1, "medium": 3, "low": 5}
    # a trailer in the follow-up reply and one inside the report text both count: the larger count wins
    both = G.parse_analyze(fx("analyze/echo-one-high.md") + "\nANALYZE_SUMMARY critical=0 high=0 medium=0 low=0\n", "ANALYZE_SUMMARY critical=0 high=1 medium=3 low=5")
    assert both.trailer["high"] == 1


def test_gate_analyze_agreeing_trailer_adds_no_warning():
    gate = G.gate_analyze(G.parse_analyze(fx("analyze/echo-one-high.md"), "ANALYZE_SUMMARY critical=0 high=1 medium=3 low=5"))
    assert failed_names(gate) == {"zero-high"} and "trailer-matches-table" not in by_name(gate)
    assert gate.facts["trailer"] == {"critical": 0, "high": 1, "medium": 3, "low": 5}


def test_gate_analyze_trailer_raises_blocking_counts_over_the_table():
    report = G.parse_analyze(fx("analyze/alpha-clean-second-pass.md"), "ANALYZE_SUMMARY critical=1 high=2 medium=3 low=3")
    assert report.counts["CRITICAL"] == 0 and report.effective_counts == {"CRITICAL": 1, "HIGH": 2, "MEDIUM": 3, "LOW": 3}
    assert report.blocking == 3
    gate = G.gate_analyze(report)
    assert failed_names(gate) == {"zero-critical", "zero-high"}
    assert "the ANALYZE_SUMMARY trailer says 2, the table lists 0" in by_name(gate)["zero-high"].detail
    warn = by_name(gate)["trailer-matches-table"]
    assert warn.warn_only and "per-severity maximum" in warn.detail
    assert gate.facts["counts"] == report.counts and gate.facts["effective_counts"]["HIGH"] == 2


def test_gate_analyze_trailer_below_the_table_cannot_lower_it():
    report = G.parse_analyze(fx("analyze/echo-bold-critical.md"), "ANALYZE_SUMMARY critical=0 high=0 medium=3 low=4")
    gate = G.gate_analyze(report)
    assert {"zero-critical", "zero-high"} <= failed_names(gate) and by_name(gate)["trailer-matches-table"].warn_only
    assert report.blocking == 3


# SYNTHETIC: a report with neither a findings table nor an explicit "no issues found" sentence (the shape the existing
# findings-parsed check fails closed on).
TABLELESS = "## Specification Analysis Report\n\nThe artifacts agree. Coverage looks complete and the constitution is respected.\n"


@pytest.mark.parametrize(
    "trailer, failing, parsed_passes",
    [
        ("", {"findings-parsed"}, False),  # unchanged: fail closed
        ("ANALYZE_SUMMARY critical=0 high=0 medium=0 low=0", set(), True),  # explicit zero via the trailer
        ("ANALYZE_SUMMARY critical=0 high=0 medium=2 low=1", set(), True),  # counts come from the trailer
        ("ANALYZE_SUMMARY critical=0 high=1 medium=0 low=0", {"zero-high"}, True),
    ],
)
def test_gate_analyze_tableless_report_and_trailer(trailer, failing, parsed_passes):
    gate = G.gate_analyze(G.parse_analyze(TABLELESS, trailer))
    assert failed_names(gate) == failing
    assert by_name(gate)["findings-parsed"].passed is parsed_passes


def test_gate_analyze_tableless_zero_trailer_contradicted_by_metrics():
    gate = G.gate_analyze(G.parse_analyze(TABLELESS + "\n- Critical Issues Count: 2\n", "ANALYZE_SUMMARY critical=0 high=0 medium=0 low=0"))
    assert {"findings-parsed", "critical-metric-agrees"} <= failed_names(gate)


def test_gate_analyze_existing_real_reports_unchanged_without_trailer():
    for rel, failing in [("analyze/template-zero-findings.md", set()), ("analyze/foxtrot-critical-tracked.md", {"zero-critical", "zero-high"})]:
        gate = G.gate_analyze(G.parse_analyze(fx(rel)))
        assert failed_names(gate) == failing and "trailer-matches-table" not in by_name(gate)


# ======================================================================================================================
# fixture hygiene for the new non-markdown fixtures
# ======================================================================================================================


def test_runner_fixtures_are_listed_sanitised_and_secret_free():
    secret = re.compile(r"(?i)(sk-[a-z0-9]{16,}|ghp_[a-z0-9]{20,}|github_pat_|AKIA[0-9A-Z]{16}|xox[bp]-|-----BEGIN|eyJ[a-z0-9_-]{20,}\.)")
    files = {p.name for p in RUNNERS.iterdir()} - {"MANIFEST.json"}
    assert files == {f"{n}{ext}" for n in MANIFEST for ext in (".out", ".err")}
    for name, meta in MANIFEST.items():
        assert {"cmd", "exit", "runner", "source"} <= set(meta), name
        assert meta["source"].startswith(("captured", "DOCUMENTED")), name
        for ext in (".out", ".err"):
            text = (RUNNERS / f"{name}{ext}").read_text(encoding="utf-8")
            assert not secret.search(text) and "/Users/" not in text and "claude-501" not in text, (name, ext)
    captured = [n for n, m in MANIFEST.items() if m["source"].startswith("captured")]
    assert {n.split("-")[0] for n in captured} >= {"vitest", "jest", "pytest", "nodetest", "playwright", "pnpm", "turbo"}
