"""A scripted world for end-to-end tests: a temp git repo with a bare origin, a fake
Spec Kit / cc-* worker (FakeSession script), a fake Linear, a fake gh and a scripted judge.
No API calls anywhere.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from speckit_autopilot.config import Config
from speckit_autopilot.gates import parse_tasks
from speckit_autopilot.judge import FakeJudge, JudgeRequest, decision, respond
from speckit_autopilot.repo import CmdResult
from speckit_autopilot.sessions import FakeSession, FakeTurn, SessionSpec

TODAY = "2026-11-10"

CONSTITUTION = """# Demo Constitution

## Core Principles

### I. Tests
Every business rule MUST have an explicit named test. No PR without tests.

### II. Simplicity
Prefer the existing stack. New frameworks need a recorded justification.

## Governance
The constitution supersedes all other practices. Amendments require approval.

**Version**: 1.0.0 | **Ratified**: 2026-01-01 | **Last Amended**: 2026-01-01
""" + ("\n<!-- padding -->" * 20)

SPEC_TEMPLATE = """# Feature Specification: [FEATURE NAME]

**Feature Branch**: `[###-feature-name]`
**Created**: [DATE]

## User Scenarios & Testing *(mandatory)*

## Requirements *(mandatory)*
- **FR-001**: System MUST [NEEDS CLARIFICATION: something]
"""

SKILLS = ["cc-feature-start", "cc-review", "cc-ship", "speckit-specify", "speckit-clarify", "speckit-plan", "speckit-tasks", "speckit-analyze", "speckit-implement"]


def sh(cwd: Path, *args: str) -> str:
    p = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {p.stderr}")
    return p.stdout.strip()


def make_repo(tmp: Path) -> Path:
    origin = tmp / "origin.git"
    sh(tmp, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp / "repo"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "config", "user.email", "t@example.com")
    sh(repo, "git", "config", "user.name", "Test")
    sh(repo, "git", "config", "commit.gpgsign", "false")
    (repo / ".specify" / "memory").mkdir(parents=True)
    (repo / ".specify" / "memory" / "constitution.md").write_text(CONSTITUTION)
    (repo / ".specify" / "templates").mkdir(parents=True)
    (repo / ".specify" / "templates" / "spec-template.md").write_text(SPEC_TEMPLATE)
    for s in SKILLS:
        d = repo / ".claude" / "skills" / s
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {s}\ndisable-model-invocation: true\n---\n{s}\n")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('hello')\n")
    (repo / "specs").mkdir()
    (repo / "specs" / "007-older-feature").mkdir()
    (repo / "specs" / "007-older-feature" / "spec.md").write_text("# old\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "init")
    sh(repo, "git", "remote", "add", "origin", str(origin))
    sh(repo, "git", "push", "-q", "-u", "origin", "main")
    sh(repo, "git", "remote", "set-head", "origin", "main")
    return repo


# --------------------------------------------------------------------------------------
# Fake Linear + gh
# --------------------------------------------------------------------------------------


class SimulatedCrash(BaseException):
    """A hard crash (SIGKILL, power loss): not an Exception, so the runner's catch-all cannot turn it into a halt."""


@dataclass
class FakeLinear:
    workspace: str = "https://linear.app/test-ws"
    issue: dict[str, Any] = field(default_factory=lambda: {"id": "ENG-142", "title": "Add Organic product filters", "status": "In Progress", "statusType": "started", "project": "Demo", "projectId": "proj-1", "team": "Acme", "url": "https://linear.app/x/ENG-142"})
    comments: list[dict[str, Any]] = field(default_factory=list)
    project: dict[str, Any] = field(default_factory=lambda: {"id": "proj-1", "name": "Demo", "description": "# Demo project\n\nIntro text that must never change.\n"})
    writes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Linear re-serialises Markdown on save: literal brackets come back escaped ("[X]" -> "\\[X\\]", seen live).
    # visible_markers: the close-out worker writes [autopilot:status] markers (the fallback) instead of HTML comments.
    visible_markers: bool = False
    escape_brackets: bool = False

    def reader_results(self) -> dict[str, Any]:
        return {
            "mcp__linear__get_workspace": json.dumps({"id": "w", "name": "WS", "url": self.workspace}),
            "mcp__linear__get_issue": json.dumps(self.issue),
            "mcp__linear__list_comments": json.dumps({"comments": self.comments}),
            "mcp__linear__get_project": json.dumps(self.project),
        }

    def closeout(self, prompt: str, today: str, run_marker: str = "") -> list[tuple[str, dict[str, Any]]]:
        calls: list[tuple[str, dict[str, Any]]] = []
        if "workflow state whose TYPE is `completed`" in prompt:
            calls.append(("mcp__linear__save_issue", {"id": "ENG-142", "state": "Done"}))
        if "Add exactly ONE comment" in prompt:
            first = re.search(r"\*\*Autopilot close-out\*\*[^\n]*", prompt).group(0)
            calls.append(("mcp__linear__save_comment", {"issueId": "ENG-142", "body": first + "\n- shipped A\n- shipped B\n- shipped C"}))
        if "Update the project overview" in prompt:
            start, end = ("[autopilot:status]", "[/autopilot:status]") if self.visible_markers else ("<!-- autopilot:status -->", "<!-- /autopilot:status -->")
            calls.append(("mcp__linear__save_project", {"id": "proj-1", "patch": [{"op": "append", "text": f"\n\n## Current status\n\n{start}\n- **Date:** {today}\n- **Just shipped:** ENG-142\n{end}\n"}]}))
        return calls

    def apply(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        for name, inp in calls:
            self.writes.append((name, inp))
            short = name.rsplit("__", 1)[-1]
            if short == "save_issue":
                self.issue["status"], self.issue["statusType"] = "Done", "completed"
            elif short == "save_comment":
                self.comments.append({"id": f"c{len(self.comments)}", "body": inp["body"]})
            elif short == "save_project":
                for op in inp.get("patch", []):
                    text = op.get("text", "")
                    if self.escape_brackets:
                        text = text.replace("[", "\\[").replace("]", "\\]")
                    self.project["description"] += text


class FakeGh:
    """gh replacement: PR list derived from the fake origin, CI runs scripted."""

    def __init__(self, world: "World"):
        self.world = world
        self.calls: list[tuple[str, ...]] = []
        self.ci_conclusion = "success"

    def __call__(self, repo: Path, *args: str, timeout_s: float = 60.0) -> CmdResult:
        self.calls.append(args)
        if args[:2] == ("pr", "list"):
            w = self.world
            prs = []
            if w.pr_number:
                prs.append({"number": w.pr_number, "state": "MERGED" if w.merged else "OPEN", "url": f"https://github.com/o/r/pull/{w.pr_number}", "mergeCommit": {"oid": w.merge_sha} if w.merged else None, "baseRefName": "main"})
            return CmdResult("gh pr list", 0, json.dumps(prs))
        if args[:2] == ("run", "list"):
            if "--commit" in args:
                return CmdResult("gh run list", 0, json.dumps([{"name": "ci", "status": "completed", "conclusion": self.ci_conclusion, "workflowName": "ci"}]))
            return CmdResult("gh run list", 0, "[]")
        if args[:2] == ("auth", "status"):
            return CmdResult("gh auth status", 0, "github.com\n  ✓ Logged in")
        return CmdResult("gh " + " ".join(args), 0, "")


# --------------------------------------------------------------------------------------
# Scripted worker
# --------------------------------------------------------------------------------------

ANALYZE_REPORT = """## Specification Analysis Report

| ID | Category | Severity | Location(s) | Summary | Recommendation |
|----|----------|----------|-------------|---------|----------------|
{rows}

**Metrics:**
- Total Requirements: 3
- Critical Issues Count: {crit}

## Next Actions
Would you like me to suggest concrete remediation edits for the top {n} issues?
"""

TASKS_MD = """# Tasks: Organic filters

## Phase 1: Setup
- [ ] T001 Create module src/filters.py
- [ ] T002 [P] Add test file tests/test_filters.py

## Phase 2: User Story 1 - Filter by origin (Priority: P1)
- [ ] T003 [US1] Implement origin filter in src/filters.py
- [ ] T004 [US1] Write tests for the origin filter in tests/test_filters.py

## Phase 3: Polish
- [ ] T005 Update docs/filters.md
"""

PLAN_MD = """# Implementation Plan: Organic filters

## Summary
Filter products by Organic origin.

## Technical Context
**Language/Version**: Python 3.12
**Testing**: pytest

## Constitution Check
| Principle | Status |
|---|---|
| I. Tests | ✅ PASS |
| II. Simplicity | ✅ PASS |

## Project Structure
src/filters.py
"""


class World:
    """Holds the repo and per-scenario knobs; ``script`` is the FakeSession behaviour."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.repo = make_repo(tmp)
        self.linear = FakeLinear()
        self.gh = FakeGh(self)
        self.sessions: list[FakeSession] = []
        self.pr_number: int | None = None
        self.merged = False
        self.merge_sha: str | None = None
        self.pushes = 0
        self.merges = 0
        # knobs
        self.analyze_rounds: list[list[tuple[str, str]]] = [[]]  # findings per analyze run: (id, severity)
        self.analyze_calls = 0
        self.clarify_questions = 1
        self.clarify_asked = 0
        self.clarify_answers: list[str] = []
        self.implement_ctx_high_after: int | None = None  # context pct >= 90 after this many implement turns
        self.implement_turns = 0
        self.review_blockers_rounds: list[int] = [0]
        self.review_calls = 0
        self.ship_crash_after_push = False
        self.ship_crash_before_push = False  # cc-ship crashes before committing/pushing anything
        self.ship_attempt_push_in = None  # phase name in which the worker tries `git push` (once)
        self.push_attempted = False
        self.pr_creates = 0  # times the worker opened a PR (a second one would be a double ship)
        self.hang_in: dict[str, int] = {}  # phase -> number of worker turns that hang (turn-timeout path)
        self.linear_crash_after_write = False  # close-out worker raises after its Linear writes landed
        self.ask_in_feature_start = False
        self.feature_start_hands_back = True
        self.analyze_trailers: list[str | None] = []  # per analyze run: the ANALYZE_SUMMARY reply (None: counts of the table)
        self.tasks_md = TASKS_MD  # what /speckit-tasks writes
        self.tick_human = False  # a misbehaving implementer that also ticks human-only tasks
        self.checklists: dict[str, str] = {}  # checklists/<name> -> content, written with the plan
        self.spec_front_matter = ""  # e.g. Bravo's "---\nlinear:\n  issueId: ENG-142\n  shared: true\n---\n"
        self.messages: list[tuple[str, str]] = []  # (session label, message)
        self.edits_applied: list[str] = []
        self.repairs_seen: list[tuple[str, str]] = []

    # ---------------------------------------------------------------- helpers
    def git(self, *args: str) -> str:
        return sh(self.repo, "git", *args)

    def feature_dir(self, s: FakeSession) -> Path:
        return self.repo / s.spec.env["SPECIFY_FEATURE_DIRECTORY"]

    def factory(self):
        def make(spec: SessionSpec) -> FakeSession:
            return FakeSession(spec, self.script, registry=self.sessions)

        return make

    def tool_runner(self, results: dict[str, list[Any]] | None = None):
        """Each command pops its next scripted result: an exit code, or (exit code, stdout) for real runner output.
        ``run.on_branch`` records the checked-out branch at every call (the test baseline must run on the base)."""
        results = results or {}
        calls: list[str] = []
        on_branch: list[tuple[str, str]] = []

        def run(cmd: str, cwd: Path, timeout: float) -> CmdResult:
            calls.append(cmd)
            on_branch.append((cmd, self.git("rev-parse", "--abbrev-ref", "HEAD")))
            seq = results.get(cmd)
            item = seq.pop(0) if seq else 0
            rc, out = item if isinstance(item, tuple) else (item, "ok" if item == 0 else "FAILED test_x")
            return CmdResult(cmd, rc, out, "")

        run.calls = calls  # type: ignore[attr-defined]
        run.on_branch = on_branch  # type: ignore[attr-defined]
        return run

    # ---------------------------------------------------------------- the worker
    def script(self, s: FakeSession, msg: str) -> FakeTurn:
        self.messages.append((s.label, msg))
        phase = s.spec.gate.ctx.phase  # shared S1 sessions span several phases
        pipeline_turn = s.spec.phase not in ("linear_read", "halt_notice") and not msg.startswith("AUTOPILOT HANDOFF BRIEF")
        if pipeline_turn and self.hang_in.get(phase, 0) > 0:
            self.hang_in[phase] -= 1
            return FakeTurn("", hang=True)  # does nothing; FakeSession times out and interrupts it
        push = pipeline_turn and self.ship_attempt_push_in == phase and not self.push_attempted
        if push and phase == "implement":
            # a denied tool call blocks the whole turn's action in FakeSession, so the push attempt is a turn of its own
            self.push_attempted = True
            return FakeTurn("Backing up my work first: pushing the branch.", tool_calls=[("Bash", {"command": "git push origin HEAD"})])
        turn = self._script(s, msg, phase)
        if push:
            self.push_attempted = True
            turn.tool_calls = list(turn.tool_calls) + [("Bash", {"command": "git push origin HEAD"})]
        return turn

    def _script(self, s: FakeSession, msg: str, phase: str) -> FakeTurn:
        if msg.startswith("AUTOPILOT HANDOFF BRIEF"):
            return FakeTurn("READY", cost_usd=0.001, context_pct=5)
        if s.spec.phase == "linear_read":
            return FakeTurn("DONE", tool_calls=[(n, {}) for n in self.linear.reader_results()], tool_results=self.linear.reader_results(), cost_usd=0.002)
        if s.spec.phase == "halt_notice":
            return FakeTurn("DONE", tool_calls=[("mcp__linear__save_comment", {"issueId": "ENG-142", "body": "halt"})], action=lambda _s: self.linear.comments.append({"id": "h", "body": "**Autopilot halted**"}))
        if msg.startswith("/cc-feature-start"):
            turn = FakeTurn("Ran feature start. Type `/speckit-specify` to continue.", tool_calls=[("Bash", {"command": f"git checkout -b {s.spec.env['GIT_BRANCH_NAME']}"})], action=lambda _s: self.git("checkout", "-q", "-b", s.spec.env["GIT_BRANCH_NAME"]))
            if self.ask_in_feature_start:
                turn.ask = [{"question": "Which storage should filters use?", "header": "Storage", "multiSelect": False, "options": [{"label": "URL params (Recommended)", "description": "shareable"}, {"label": "Local storage", "description": "sticky"}]}]
            if not self.feature_start_hands_back:
                act = turn.action

                def both(_s):
                    act(_s)
                    self._write_spec(s)

                turn.action = both
                turn.text = "✓ spec initialised"
            return turn
        if msg.startswith("/speckit-specify"):
            return FakeTurn("✓ spec.md written", tool_calls=[("Write", {"file_path": str(self.feature_dir(s) / "spec.md")})], action=lambda _s: self._write_spec(s))
        if msg.startswith("/speckit-clarify") or (phase == "clarify" and not msg.startswith("AUTOPILOT REPAIR")):
            if not msg.startswith("/"):
                self.clarify_answers.append(msg)
            if self.clarify_asked < self.clarify_questions:
                self.clarify_asked += 1
                return FakeTurn(
                    f"**Question:** Which origin field should drive the filter? (FR-001)\n\n**Recommended:** Option A - use the existing `origin_country` column\n\n| Option | Description |\n|---|---|\n| A | origin_country column |\n| B | new brand_origin table |\n",
                    context_pct=12,
                )
            return FakeTurn("Clarification complete: 1 question asked. Sections touched: Requirements.", action=lambda _s: self._write_clarifications(s), tool_calls=[("Edit", {"file_path": str(self.feature_dir(s) / "spec.md")})])
        if msg.startswith("/speckit-plan"):
            return FakeTurn("Plan complete: plan.md, research.md", action=lambda _s: self._write_plan(s), tool_calls=[("Write", {"file_path": str(self.feature_dir(s) / "plan.md")})])
        if msg.startswith("/speckit-tasks"):
            return FakeTurn("tasks.md generated (5 tasks)", action=lambda _s: (self.feature_dir(s) / "tasks.md").write_text(self.tasks_md), tool_calls=[("Write", {"file_path": str(self.feature_dir(s) / "tasks.md")})])
        if msg.startswith("/speckit-analyze"):
            i = min(self.analyze_calls, len(self.analyze_rounds) - 1)
            self.analyze_calls += 1
            findings = self.analyze_rounds[i]
            self._last_analyze = (i, findings or [("L1", "LOW")])
            rows = "\n".join(f"| {fid} | Coverage | {sev} | spec.md | gap {fid} | fix {fid} |" for fid, sev in findings) or "| L1 | Style | LOW | plan.md | wording | optional |"
            crit = sum(1 for _, sev in findings if sev == "CRITICAL")
            return FakeTurn(ANALYZE_REPORT.format(rows=rows, crit=crit, n=len(findings) or 1))
        if msg.startswith("Now append a machine-readable summary of the analysis"):
            i, findings = self._last_analyze
            scripted = self.analyze_trailers[i] if i < len(self.analyze_trailers) else None
            counts = {sev: sum(1 for _, s in findings if s == sev) for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW")}
            return FakeTurn(scripted or "ANALYZE_SUMMARY " + " ".join(f"{k.lower()}={v}" for k, v in counts.items()))
        if msg.startswith("Approved, apply these edits now") or msg.startswith("Approved, apply these fixes now"):
            self.edits_applied.append(msg)
            return FakeTurn("Applied the approved edits.", action=lambda _s: self._apply_edit(s), tool_calls=[("Edit", {"file_path": str(self.feature_dir(s) / "tasks.md")})])
        if msg.startswith("/speckit-implement") or (phase == "implement" and msg.strip().lower() == "continue"):
            self.implement_turns += 1
            pct = 95 if (self.implement_ctx_high_after and self.implement_turns >= self.implement_ctx_high_after) else 20
            return FakeTurn("", context_pct=pct, action=lambda _s: self._implement_one_phase(s), tool_calls=[("Edit", {"file_path": str(self.repo / "src" / "filters.py")})])
        if msg.startswith("AUTOPILOT REPAIR"):
            return FakeTurn("Fixed the failed checks.", action=lambda _s: self._repair(s, msg, phase), tool_calls=[("Edit", {"file_path": str(self.repo / "src" / "filters.py")})])
        if msg.startswith("/cc-review"):
            i = min(self.review_calls, len(self.review_blockers_rounds) - 1)
            self.review_calls += 1
            self._last_review_blockers = self.review_blockers_rounds[i]
            if self._last_review_blockers:
                return FakeTurn("## Blockers\n- **Severity**: blocker\n- **Anchor**: src/filters.py:1\n- **Finding**: missing guard\n\n## Nits\nNone.\n\n## Questions\nNone.")
            return FakeTurn("## Blockers\nNone.\n\n## Nits\nNone.\n\n## Questions\nNone.")
        if msg.startswith("Now append a machine-readable summary"):
            return FakeTurn(f"REVIEW_SUMMARY blockers={self._last_review_blockers} nits=0 questions=0")
        if msg.startswith("/cc-ship") or msg.startswith("AUTOPILOT SHIP RESUME"):
            return self._ship(s, msg)
        if msg.startswith("AUTOPILOT LINEAR CLOSE-OUT"):
            calls = self.linear.closeout(msg, TODAY)

            def close(_s):
                self.linear.apply(calls)
                if self.linear_crash_after_write:
                    self.linear_crash_after_write = False
                    raise SimulatedCrash("simulated crash after the Linear close-out writes")

            return FakeTurn("Close-out done.", tool_calls=calls, action=close)
        return FakeTurn("OK")

    # ---------------------------------------------------------------- worker effects
    def _write_spec(self, s: FakeSession) -> None:
        fd = self.feature_dir(s)
        fd.mkdir(parents=True, exist_ok=True)
        (fd / "spec.md").write_text(self.spec_front_matter + "# Feature Specification: Organic product filters\n\n## Requirements\n- **FR-001**: System MUST filter by origin [NEEDS CLARIFICATION: which field?]\n")

    def _write_clarifications(self, s: FakeSession) -> None:
        p = self.feature_dir(s) / "spec.md"
        t = p.read_text().replace(" [NEEDS CLARIFICATION: which field?]", " using origin_country")
        t += f"\n## Clarifications\n\n### Session {TODAY}\n\n- Q: Which origin field? → A: {self.clarify_answers[-1] if self.clarify_answers else 'A'}\n"
        p.write_text(t)

    def _write_plan(self, s: FakeSession) -> None:
        fd = self.feature_dir(s)
        (fd / "plan.md").write_text(PLAN_MD)
        (fd / "research.md").write_text("# Research\nDecision: reuse origin_country.\n")
        (fd / "data-model.md").write_text("# Data model\n")
        (fd / "quickstart.md").write_text("# Quickstart\n")
        for name, content in self.checklists.items():
            (fd / "checklists").mkdir(exist_ok=True)
            (fd / "checklists" / name).write_text(content)

    def _apply_edit(self, s: FakeSession) -> None:
        p = self.feature_dir(s) / "tasks.md"
        if p.exists():
            p.write_text(p.read_text() + f"\n<!-- edit {len(self.edits_applied)} -->\n")
        if s.spec.gate.ctx.phase == "review":
            (self.repo / "src" / "filters.py").write_text("def origin_filter(x):\n    if x is None:\n        return []\n    return [i for i in x if i.get('origin') == 'KR']\n")

    def _open_for_agent(self, line: str) -> bool:
        """An unchecked task the implementer works on: human-only tasks are skipped unless tick_human is set."""
        if not line.startswith("- [ ]"):
            return False
        tasks = parse_tasks(line)
        return self.tick_human or not (tasks and tasks[0].human)

    def _implement_one_phase(self, s: FakeSession) -> None:
        p = self.feature_dir(s) / "tasks.md"
        lines = p.read_text().splitlines()
        out, in_target, done_one = [], False, False
        for line in lines:
            if line.startswith("## Phase"):
                if in_target:
                    done_one = True
                in_target = not done_one and any(self._open_for_agent(l2) for l2 in self._phase_block(lines, line))
            if in_target and self._open_for_agent(line):
                line = line.replace("- [ ]", "- [X]", 1)
            out.append(line)
        p.write_text("\n".join(out) + "\n")
        (self.repo / "src" / "filters.py").write_text("def origin_filter(x):\n    return [i for i in x if i.get('origin') == 'KR']\n")
        (self.repo / "tests").mkdir(exist_ok=True)
        (self.repo / "tests" / "test_filters.py").write_text("def test_origin():\n    assert True\n")

    @staticmethod
    def _phase_block(lines: list[str], header: str) -> list[str]:
        i = lines.index(header)
        block = []
        for l in lines[i + 1:]:
            if l.startswith("## "):
                break
            block.append(l)
        return block

    def _repair(self, s: FakeSession, msg: str, phase: str) -> None:
        self.repairs_seen.append((phase, msg))
        if "all-tasks-checked" in msg:
            self._implement_one_phase(s)
        if "human-tasks-untouched" in msg:
            # a well-behaved repair: un-tick every human-only task it ticked
            p = self.feature_dir(s) / "tasks.md"
            human = {t.line for t in parse_tasks(p.read_text()) if t.human}
            lines = p.read_text().split("\n")
            p.write_text("\n".join(l.replace("- [X]", "- [ ]", 1) if i in human else l for i, l in enumerate(lines, 1)))
        if phase == "clarify":
            self._write_clarifications(s)
        if phase == "plan":
            self._write_plan(s)
        if phase == "tasks":
            (self.feature_dir(s) / "tasks.md").write_text(TASKS_MD)

    def _ship(self, s: FakeSession, msg: str) -> FakeTurn:
        """cc-ship from scratch commits, pushes and opens a PR every time it is run (so a second
        run would double-ship); the AUTOPILOT SHIP RESUME message is followed literally."""
        branch = s.spec.env.get("GIT_BRANCH_NAME") or self.git("rev-parse", "--abbrev-ref", "HEAD")
        resume = msg.startswith("AUTOPILOT SHIP RESUME")
        resume_open_pr = resume and "is open" in msg  # "Do NOT commit again and do NOT open another PR"
        resume_no_pr = resume and "no PR exists yet" in msg
        calls = [("Bash", {"command": "gh pr merge 1 --merge"}), ("Bash", {"command": "git switch main && git pull --ff-only origin main"})]
        if not resume:
            calls.insert(0, ("Bash", {"command": f"git push -u origin {branch}"}))

        if resume_no_pr and not self.git("ls-remote", "origin", f"refs/heads/{branch}"):
            # followed literally: `gh pr create --head <branch>` fails when the branch was never pushed
            return FakeTurn(f"gh pr create failed: you must first push the current branch to a remote ({branch}). Nothing merged.", tool_calls=[("Bash", {"command": f"gh pr create --base main --head {branch} --fill"})])

        def act(_s):
            cur = self.git("rev-parse", "--abbrev-ref", "HEAD")
            if cur != "main" and not resume and self.ship_crash_before_push:
                self.ship_crash_before_push = False
                raise SimulatedCrash("simulated crash before push")
            if cur != "main" and not resume:
                self.git("add", "-A")
                if self.git("status", "--porcelain"):
                    self.git("commit", "-q", "-m", "feat: organic filters")
                self.git("push", "-q", "-u", "origin", branch)
                self.pushes += 1
            if (not resume or resume_no_pr) and not self.merged:
                self.pr_creates += 1
                self.pr_number = self.pr_number or 1
            if cur != "main" and not resume and self.ship_crash_after_push:
                self.ship_crash_after_push = False
                raise SimulatedCrash("simulated crash after push")
            if resume and not (resume_open_pr or resume_no_pr):
                # "the feature is already merged. Only run `git switch main && git pull`" — done literally
                # (uncommitted work, if any, is carried onto main: that is what the message asks for)
                self.git("checkout", "-q", "main")
            elif not self.merged:
                self.git("checkout", "-q", "main")
                self.git("merge", "-q", "--no-ff", "-m", "Merge pull request #1", branch)
                self.git("push", "-q", "origin", "main")
                self.merges += 1
                self.merged = True
                self.merge_sha = self.git("rev-parse", "HEAD")
            else:
                self.git("checkout", "-q", "main")
            self.git("pull", "-q", "--ff-only", "origin", "main")

        return FakeTurn("Shipped: PR #1 merged, back on main.", tool_calls=calls, action=act)


# --------------------------------------------------------------------------------------
# Scripted judge
# --------------------------------------------------------------------------------------


class JudgeScript:
    """Default judge behaviour + per-test overrides."""

    def __init__(self, world: World):
        self.world = world
        self.clarify_choice = "A"
        self.clarify_follow_recommendation = True
        self.route_action: dict[str, dict[str, Any]] = {}  # after the repair rounds are used up
        self.early_route: dict[str, dict[str, Any]] = {}  # the round-1 root-cause check (default: fix here)
        self.early_requests: list[JudgeRequest] = []
        self.verify_fail: dict[str, list[list[str]]] = {}
        self.verify_next_worker: dict[str, dict[str, Any]] = {}  # model_mode=auto: next_worker on a failed verify
        self.escalate_in: str | None = None
        self.analyze_accept_all = False
        self.analyze_fix_nonblocking = False  # like the real judge: also ask to fix the MEDIUM/LOW rows

    def __call__(self, req: JudgeRequest) -> dict[str, Any] | None:
        m, ph = req.mode, req.phase.split(":")[0]
        text = req.payload.get("worker_text", "")
        if m == "verify":
            seq = self.verify_fail.get(ph)
            if seq:
                out = {"pass": False, "failed_criteria": seq.pop(0), "notes": "gap"}
                if ph in self.verify_next_worker:
                    out["next_worker"] = self.verify_next_worker[ph]
                return out
            return {"pass": True, "failed_criteria": [], "notes": "ok"}
        if m == "route" and req.payload.get("early"):
            if not any(r is req for r in self.early_requests):  # FakeJudge retries invalid output with the same request
                self.early_requests.append(req)
            r = self.early_route.get(ph, {"action": "fix_here", "reason": "the root cause is in this phase"})
            return {"action": r["action"], "target_phase": r.get("target"), "reason": r.get("reason", "scripted")}
        if m == "route":
            r = self.route_action.get(ph, {"action": "halt", "reason": "cannot fix"})
            return {"action": r["action"], "target_phase": r.get("target"), "reason": r.get("reason", "scripted")}
        if m == "answer":
            qs = req.payload.get("questions", [])
            return {"answers": [{"question": q["question"], "labels": [q["options"][1]["label"]]} for q in qs], "decisions": [decision(q["question"], q["options"][1]["label"], recommended=q["options"][0]["label"], followed=False, rationale="local storage fits the evidence")], "escalate": False, "escalate_reason": ""}
        # respond
        esc = self.escalate_in == ph
        if esc:
            self.escalate_in = None
        if ph == "feature_start":
            if "/speckit-specify" in text:
                return respond("needs_reply", "/speckit-specify ENG-142: Add Organic product filters")
            return respond("done", escalate=esc, reason="payments" if esc else "")
        if ph == "clarify":
            if "**Recommended:**" in text:
                follow = self.clarify_follow_recommendation
                return respond("needs_reply", self.clarify_choice, [decision("Which origin field?", "Option " + self.clarify_choice, recommended="Option A", followed=follow, impact="medium", rationale="B is better" if not follow else "A wins")], escalate=esc, reason="pii" if esc else "")
            return respond("done")
        if ph == "analyze":
            rows = re.findall(r"^\| (\w+) \| \w+ \| (CRITICAL|HIGH|MEDIUM|LOW) \|", text, re.M)
            blocking = [r for r in rows if r[1] in ("CRITICAL", "HIGH")]
            if blocking and not self.analyze_accept_all:
                return respond("needs_reply", "Approved, apply these edits now:\n" + "\n".join(f"{i}. fix {r[0]}" for i, r in enumerate(blocking, 1)), [decision(f"finding {r[0]}", "fix", impact="high") for r in blocking], blocking=0)
            if rows and self.analyze_fix_nonblocking:
                return respond("needs_reply", "Approved, apply these edits now:\n" + "\n".join(f"{i}. fix {r[0]}" for i, r in enumerate(rows, 1)), [decision(f"finding {r[0]}", "fix", impact="low") for r in rows], blocking=0)
            return respond("done", decisions=[decision(f"finding {r[0]}", "accept", impact="low") for r in rows if r[1] not in ("CRITICAL", "HIGH")], blocking=0)
        if ph == "implement":
            fd = self.world.repo / "specs"
            remaining = req.payload.get("facts", {}).get("unchecked_remaining", 0)
            return respond("needs_reply", "continue") if remaining else respond("done")
        if ph == "review":
            if "**Severity**: blocker" in text:
                return respond("needs_reply", "Approved, apply these fixes now:\n1. add a None guard in src/filters.py:1", [decision("blocker: missing guard", "fix", impact="medium")], blocking=0)
            return respond("done")
        return respond("done", escalate=esc, reason="legal" if esc else "")


def make_cfg(world: World, **kw: Any) -> Config:
    cfg = Config(repo=world.repo, issue="ENG-142", description="Add Organic product filters")
    cfg.linear = "on"  # the fake world always has a Linear server
    cfg.linear_workspace_url = world.linear.workspace
    cfg.ci_required = False
    cfg.test_cmd, cfg.lint_cmd, cfg.typecheck_cmd, cfg.build_cmd = "pytest -q", "ruff check .", None, None
    cfg.notify_linear_on_halt = True
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def make_runner(world: World, judge_script: JudgeScript | None = None, tool_results: dict[str, list[int]] | None = None, **cfg_kw: Any):
    from speckit_autopilot.runner import Runner

    js = judge_script or JudgeScript(world)
    judge = FakeJudge(js)
    cfg = make_cfg(world, **cfg_kw)
    tr = world.tool_runner(tool_results)
    runner = Runner(cfg, session_factory=world.factory(), judge=judge, tool_runner=tr, gh_runner=world.gh, today=lambda: TODAY, sleep=lambda s: None, install_signal_handlers=False)
    return runner, judge, js, tr
