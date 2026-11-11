"""Deterministic exit gates: pure functions over the repo (docs/DESIGN.md §5–§6).

Every regex here is cited in docs/DESIGN.md §"Spec Kit markers" and is tolerant
of the Spec Kit 0.7.1 / 1.0.6 / 1.2.1 / 1.2.3 variants found in the workspace repos.
The judge adds semantic checks on top and can never overrule a failed check here.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .repo import CmdResult, Git, gh

# --------------------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    warn_only: bool = False


@dataclass
class GateResult:
    phase: str
    checks: list[Check] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, passed: bool, detail: str = "", warn_only: bool = False) -> "GateResult":
        self.checks.append(Check(name, bool(passed), detail, warn_only))
        return self

    @property
    def passed(self) -> bool:
        return all(c.passed or c.warn_only for c in self.checks)

    def failed(self) -> list[str]:
        return [f"[{c.name}] {c.detail}".rstrip() for c in self.checks if not c.passed and not c.warn_only]

    def warnings(self) -> list[str]:
        return [f"[{c.name}] {c.detail}".rstrip() for c in self.checks if not c.passed and c.warn_only]

    def summary(self) -> str:
        if self.passed:
            return f"{self.phase}: PASS ({len(self.checks)} checks)"
        return f"{self.phase}: FAIL — " + "; ".join(self.failed())

    def to_dict(self) -> dict[str, Any]:
        return {"phase": self.phase, "passed": self.passed, "checks": [c.__dict__ for c in self.checks], "facts": self.facts}


# --------------------------------------------------------------------------------------
# Markdown helpers
# --------------------------------------------------------------------------------------

FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def strip_front_matter(text: str) -> str:
    return FRONT_MATTER.sub("", text, count=1)


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
        return ""


def section(text: str, heading_re: str) -> str | None:
    """Body of the first heading matching ``heading_re`` up to the next heading of the same or higher level."""
    m = re.search(heading_re, text, re.M | re.I)
    if not m:
        return None
    line = text[m.start(): text.find("\n", m.start()) if "\n" in text[m.start():] else len(text)]
    level = len(line) - len(line.lstrip("#"))
    rest = text[m.end():]
    nxt = re.search(rf"(?m)^#{{1,{max(level, 1)}}}\s", rest)
    return rest[: nxt.start()] if nxt else rest


def sections(text: str, heading_re: str) -> list[str]:
    """Bodies of EVERY heading matching ``heading_re`` (e.g. a Constitution Check and its post-design re-check)."""
    return [section(text[m.start():], heading_re) or "" for m in re.finditer(heading_re, text, re.M | re.I)]


def without_code_fences(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.S)


INLINE_CODE = re.compile(r"`[^`\n]*`")


# --------------------------------------------------------------------------------------
# Markers (tolerant across Spec Kit versions; see DESIGN.md)
# --------------------------------------------------------------------------------------

CLARIFICATIONS_H2 = r"^##\s+(?:Resolved\s+)?Clarifications\s*$"
SESSION_H3 = re.compile(r"^###\s+Session\s+(\d{4}-\d{2}-\d{2})\b[^\n]*$", re.M)
NC_BRACKETED = re.compile(r"\[NEEDS CLARIFICATION\b[^\]]*\]")  # any bracketed mention (kept for callers; not a gate)
# The unresolved marker specify writes is "[NEEDS CLARIFICATION: <question>]" (template + skill). Real research.md /
# plan.md prose routinely MENTIONS the bare form ("No `[NEEDS CLARIFICATION]` remain", "(resolves spec
# [NEEDS CLARIFICATION] #1)") — 25 of 277 real plans failed a bare-bracket gate. See nc_markers().
NC_MARKER = re.compile(r"\[NEEDS\s+CLARIFICATION\s*:")
NC_BARE = re.compile(r"\[NEEDS\s+CLARIFICATION\s*\]")
NC_TECH_FIELD = re.compile(r"^\*\*[^*\n]+\*\*:\s*[^\n]*\bNEEDS CLARIFICATION\b", re.M)
NO_AMBIGUITY = re.compile(r"No critical ambiguities detected worth formal clarification", re.I)

CONST_CHECK_H = r"^#{2,3}\s+(?:Post-?\s*design\s+)?Constitution\s+(?:Check|Re-?\s*check)\b[^\n]*$"
COMPLEXITY_H = r"^#{2,3}\s+Complexity Tracking\b[^\n]*$"
# A failing Constitution Check row = a table cell that STARTS with a fail status. Upper-case tokens and marks always
# count; title/lower-case words only when they stand alone as a status ("Fail", "Violation — justified below"), never
# as the first word of a principle/description cell ("Violations block merge until amended"; "Fails closed").
CONST_FAIL_CELL = re.compile(
    r"^\|(?:[^|\n]*\|)*?[ \t]*(?:\*\*|__)?[ \t]*(?:"
    r"[❌✗✘]"
    r"|(?:FAIL(?:ED|S|ING)?|VIOLATION|VIOLATED|NON-?COMPLIANT|ERROR)(?![\w-])"
    r"|(?:[Ff]ail(?:ed|s)?|[Vv]iolat(?:ion|ed)|[Nn]on-?compliant)(?=[ \t]*(?:\*\*|__)?[ \t]*(?:\||$|[—–:;,.(/]|-\s))"
    r")",
    re.M,
)
COMPLEXITY_PLACEHOLDER_ROW = re.compile(r"\[e\.g\.[^\]]*\]|\[[A-Z][A-Z _]*\]|^\|\s*\[[^\]\n]*\]\s*\||N/A|^\|\s*\|")
PLAN_LEFTOVERS = re.compile(
    r"\[###-feature-name\]|\[FEATURE\]|\[DATE\]|\[REMOVE IF UNUSED\]|ACTION REQUIRED|\[Gates determined based on constitution file\]|\[e\.g\.,"
)
SPEC_LEFTOVERS = re.compile(r"\[FEATURE NAME\]|\[###-feature-name\]|\[DATE\]|\$ARGUMENTS")

TASK_LINE = re.compile(r"^\s*-\s+\[(?P<mark>[ xX~B])\]\s+(?:\*\*)?(?P<id>T\d{3,}[a-z]?)(?:\*\*)?(?=[\s:.,)\]]|$)(?P<rest>.*)$", re.M)
CHECKBOX_LINE = re.compile(r"^\s*-\s+\[[ xX~B]\]\s+(?P<rest>.*)$", re.M)
TASK_LEFTOVERS = re.compile(r"\bTXXX\b|SAMPLE TASKS|\[Title\]")
PHASE_H2 = re.compile(r"^##\s+Phase\s+(?P<n>\d+[a-z]?|N)\b\s*[:—–-]?\s*(?P<title>.*)$", re.M)
TEST_HINT = re.compile(r"(?i)\btests?\b|\.test\.|\.spec\.|_test\.|\btest_|\bpytest\b|\bvitest\b|\bjest\b|\bplaywright\b|\be2e\b")

ANALYZE_H2 = re.compile(r"^#{1,3}\s+Specification Analysis Report\b", re.M | re.I)
ANALYZE_TABLE_HDR = re.compile(r"^\|\s*ID\s*\|\s*Category\s*\|\s*Severity\s*\|", re.M | re.I)
# Severity cell may carry a suffix in real reports ("CRITICAL (tracked)") — never drop such a row.
ANALYZE_ROW = re.compile(
    r"^\|\s*\**(?P<id>[A-Za-z]{1,4}-?\d+)\**\s*\|\s*(?P<cat>[^|\n]*)\|\s*\**\s*(?P<sev>CRITICAL|HIGH|MEDIUM|LOW)\b[^|\n]*\|(?P<rest>[^\n]*)$",
    re.M | re.I,
)
ANALYZE_ID_CELL = re.compile(r"^\**\s*([A-Za-z]{1,4}-?\d+[a-z]?)\s*\**$")
ANALYZE_SEV_CELL = re.compile(r"^[\W_]*?(CRITICAL|HIGH|MEDIUM|LOW)\b", re.I)
# Real reports also group findings under "### High" / "### Medium" headings with NO Severity column.
ANALYZE_SEV_HEADING = re.compile(r"^#{2,5}\s+[*_]*\s*(CRITICAL|HIGH|MEDIUM|LOW)(?![\w-])", re.I)
# Template says "Critical Issues Count"; real reports write "Critical issues: 0", "| Critical issues | 1 (C1) |",
# "**Critical issues:** 0", "| Critical findings | 0 |" or "· 0 critical issues". Anchored to list/table/metric context.
ANALYZE_CRIT_METRIC = re.compile(
    r"(?im)(?:^|[\-*|·;,.(]|\*\*)[ \t]*[*_]*[ \t]*Critical[ \t]+(?:Issues?|Findings?)(?:[ \t]+Count)?[ \t]*[*_]*[ \t]*[:=|\-–]?[ \t]*[*_]*[ \t]*(\d+)"
)
ANALYZE_CRIT_METRIC_PRE = re.compile(r"(?im)(?:^|[\-*|·;,(]|\*\*)[ \t]*[*_]*(\d+)[*_]*[ \t]+critical[ \t]+(?:issues?|findings?)\b")
# No HIGH metric exists in the template, but real reports often add one ("High: 4", "· **High:** 6", "| High issues | 4 |").
ANALYZE_HIGH_METRIC = re.compile(
    r"(?im)(?:^|[\-*|·;,.(]|\*\*)[ \t]*[*_]*[ \t]*High(?:[ \t]+(?:Issues?|Findings?|severity))?(?:[ \t]+Count)?[ \t]*[*_]*[ \t]*[:=|][ \t]*[*_]*[ \t]*(\d+)"
)
ANALYZE_ZERO = re.compile(r"(?i)no (?:issues|findings|inconsistencies|problems) (?:were )?(?:found|detected|identified)|zero (?:issues|findings)|\b0 (?:issues|findings)(?: found)?\b|total (?:issues|findings)\**\s*[:=|]\s*\**\s*0\b|no CRITICAL or HIGH (?:issues|findings)")
ANALYZE_ANY_TABLE = re.compile(r"(?mi)^\|\s*\**ID\**\s*\|")
ANALYZE_OFFER = re.compile(r"(?i)would you like me to suggest (?:concrete )?(?:remediation )?edits")

REVIEW_TRAILER = re.compile(r"(?m)^\s*[`*]*REVIEW[_-]SUMMARY[`*]*:?[`*]*\s+blockers\s*=\s*(\d+)\b[^\n]*$", re.I)
REVIEW_BLOCKERS_NONE = re.compile(
    r"(?im)^\s*(?:#{1,4}[ \t]+[*_]*Blockers?[*_]*[ \t]*(?:\(\s*0\s*\))?[ \t]*:?[ \t]*\n\s*|[*_]*Blockers?(?:[ \t]*/[ \t]*[a-z][a-z ]{0,20}?)?[*_]*\s*:?[*_]*\s*)[*_]*(?:none|0|no blockers?)\b"
)
# "- **Severity**: blocker" (agent format) and the bare "**Severity**: blocker" line real subagents print.
REVIEW_BLOCKER_ITEM = re.compile(r"(?im)^\s*(?:[-*]\s+)?\*\*Severity\*\*\s*:\s*[*_]*blocker\b|^\s*[-*]\s*\*\*(?:Blocker|B\d+)\b")
REVIEW_BLOCKERS_LIST = re.compile(r"(?im)^#{1,4}\s+Blockers?\s*:?\s*\n+\s*(?:\d+\.|[-*]\s)")
REVIEW_BLOCKERS_H = re.compile(r"(?im)^(#{1,4})[ \t]+[*_]*Blockers?[*_]*[ \t]*(?:\((\d+)\))?[ \t]*:?[ \t]*$")
REVIEW_BLOCKERS_LABEL = re.compile(r"(?im)^[ \t]*\*\*Blockers?:?\*\*[ \t]*:?[ \t]*$")
REVIEW_NONE_START = re.compile(r"(?i)^[*_]*\s*(?:none|no blockers?|0)\b")
REVIEW_GROUP_LABEL = re.compile(r"^(?:#{1,6}\s+.*|\*\*[^*\n]+\*\*:?)\s*$")

EXT_HOOKS_H2 = re.compile(r"^##\s+Extension Hooks\s*$", re.M)
IMPLEMENT_CHECKLIST_Q = re.compile(r"(?i)Some checklists have unchecked items\.\s*Do you want to proceed with implementation anyway\?")
SLASH_IN_TEXT = re.compile(r"(?m)(?:^|`)\s*(/[a-z][\w.\-:]*)(?:\s+([^`\n]*))?")

CONSTITUTION_PLACEHOLDER = re.compile(r"\[(?:PROJECT_NAME|PRINCIPLE_\d+_NAME|PRINCIPLE_\d+_DESCRIPTION|SECTION_\d+_NAME|GOVERNANCE_RULES|CONSTITUTION_VERSION|RATIFICATION_DATE|LAST_AMENDED_DATE)\]")
TESTS_REQUIRED = re.compile(
    r"(?i)test[- ]first|\bTDD\b|tests? (?:MUST|are required|is required)|(?:MUST|must) (?:include|have|add) (?:test|tests)|require[sd]? (?:explicit |named )?tests|No PR without tests|explicit named test|NON-NEGOTIABLE[^\n]*test"
)


# --------------------------------------------------------------------------------------
# Fingerprints (idempotency + read-only proofs)
# --------------------------------------------------------------------------------------


def fingerprint(paths: list[Path], root: Path | None = None) -> str:
    h = hashlib.sha256()
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(x for x in p.rglob("*") if x.is_file() and ".git" not in x.parts))
        elif p.is_file():
            files.append(p)
    for f in sorted(set(files)):
        if f.name == "decisions.md":
            continue  # supervisor-owned
        rel = f.relative_to(root).as_posix() if root and f.is_relative_to(root) else str(f)
        h.update(rel.encode())
        try:
            h.update(f.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------------------
# feature_start
# --------------------------------------------------------------------------------------


def constitution_status(repo: Path) -> tuple[bool, str]:
    p = repo / ".specify" / "memory" / "constitution.md"
    text = read(p)
    if not text:
        return False, f"{p.relative_to(repo)} is missing"
    if CONSTITUTION_PLACEHOLDER.search(text):
        return False, f"{p.relative_to(repo)} still contains template placeholders (e.g. {CONSTITUTION_PLACEHOLDER.search(text).group(0)})"
    if len(strip_front_matter(text).strip()) < 400:
        return False, f"{p.relative_to(repo)} looks empty ({len(text)} bytes)"
    return True, "filled in"


def preconditions_feature_start(repo: Path, base: str, *, fetch: bool = True, allow_detached_at_base: bool = False) -> GateResult:
    g = Git(repo)
    r = GateResult("feature_start:pre")
    dirty = g.status_porcelain()
    r.add("clean-tree", not dirty, "working tree is clean" if not dirty else f"{len(dirty)} uncommitted/untracked entries, e.g. {dirty[:3]}")
    branch = g.current_branch()
    on_base = branch == base or (allow_detached_at_base and branch == "HEAD" and g.head() == g.head(f"origin/{base}"))
    r.add("on-base", on_base, f"current branch is {branch!r}, expected {base!r}")
    if fetch:
        f = g.fetch("origin", base)
        r.add("fetch", f.ok, f.stderr.strip()[:200] if not f.ok else "fetched origin")
    ab = g.ahead_behind(base if not allow_detached_at_base else "HEAD", f"origin/{base}")
    r.add("base-up-to-date", ab == (0, 0), f"local {base} vs origin/{base}: ahead/behind = {ab}")
    ok, detail = constitution_status(repo)
    r.add("constitution", ok, detail)
    return r


def gate_feature_start(repo: Path, base: str, feature_dir: Path, expected_branch: str | None) -> GateResult:
    g = Git(repo)
    r = GateResult("feature_start")
    branch = g.current_branch()
    r.facts["branch"] = branch
    r.add("feature-branch", branch not in (base, "HEAD", ""), f"current branch is {branch!r}")
    if expected_branch:
        r.add("branch-name", branch == expected_branch, f"expected branch {expected_branch!r}, found {branch!r}")
    r.add("feature-dir", feature_dir.is_dir(), f"{_rel(feature_dir, repo)} {'exists' if feature_dir.is_dir() else 'does not exist'}")
    spec = feature_dir / "spec.md"
    text = read(spec)
    r.add("spec-exists", bool(text), f"{_rel(spec, repo)} {'exists' if text else 'is missing or empty'}")
    if text:
        template = read(repo / ".specify" / "templates" / "spec-template.md")
        same = template and _norm_md(text) == _norm_md(template)
        r.add("spec-not-template", not same, "spec.md is identical to the template" if same else "spec.md differs from the template")
        left = SPEC_LEFTOVERS.findall(text)
        r.add("spec-no-placeholders", not left, f"template placeholders left: {sorted(set(left))[:5]}" if left else "no template placeholders")
    return r


def _norm_md(s: str) -> str:
    return re.sub(r"\s+", " ", strip_front_matter(s)).strip()


def _rel(p: Path, repo: Path) -> str:
    try:
        return p.relative_to(repo).as_posix()
    except ValueError:
        return str(p)


# --------------------------------------------------------------------------------------
# clarify
# --------------------------------------------------------------------------------------


def clarify_reported_nothing(worker_text: str) -> bool:
    return bool(NO_AMBIGUITY.search(worker_text or ""))


def nc_markers(text: str, *, bare: bool) -> list[str]:
    """Unresolved NEEDS CLARIFICATION markers outside fenced and inline code.

    Always counts the marker form "[NEEDS CLARIFICATION: …" (opening bracket + colon, so a question hard-wrapped
    across lines or missing its closing bracket still counts). ``bare=True`` (spec.md) also counts
    "[NEEDS CLARIFICATION]" without a question; plan/research prose uses that form to *mention* markers.
    """
    body = INLINE_CODE.sub("", without_code_fences(text or ""))
    found = [m.group(0) for m in NC_MARKER.finditer(body)]
    if bare:
        found += [m.group(0) for m in NC_BARE.finditer(body)]
    return found


def gate_clarify(feature_dir: Path, today: str, *, nothing_to_clarify: bool = False, since: str | None = None) -> GateResult:
    """``since`` (ISO date) widens the accepted session date to since <= date <= today, for runs that cross midnight or
    resume on a later day; None keeps "today only"."""
    r = GateResult("clarify")
    text = strip_front_matter(read(feature_dir / "spec.md"))
    r.add("spec-exists", bool(text), "spec.md present" if text else "spec.md missing")
    sessions = SESSION_H3.findall(section(text, CLARIFICATIONS_H2) or "") if re.search(CLARIFICATIONS_H2, text, re.M) else []
    if since is None:
        has_today = today in sessions
        wanted = today
    else:
        has_today = any(since <= s <= today for s in sessions)  # ISO dates compare correctly as strings
        wanted = f"<date> ({since} … {today})"
    if nothing_to_clarify and not has_today:
        r.add("clarifications-session", True, "clarify reported 'No critical ambiguities detected worth formal clarification'; no session needed")
    else:
        r.add("clarifications-session", has_today, f"'### Session {wanted}' under '## Clarifications' {'found' if has_today else 'not found'} (sessions: {sessions[-3:]})")
    nc = nc_markers(text, bare=True)
    r.add("no-needs-clarification", not nc, f"{len(nc)} [NEEDS CLARIFICATION] marker(s) remain in spec.md" if nc else "0 [NEEDS CLARIFICATION] markers")
    return r


# --------------------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------------------


def gate_plan(feature_dir: Path) -> GateResult:
    r = GateResult("plan")
    plan = strip_front_matter(read(feature_dir / "plan.md"))
    r.add("plan-exists", bool(plan), "plan.md present" if plan else "plan.md missing")
    research = read(feature_dir / "research.md")
    r.add("research-exists", bool(research), "research.md present" if research else "research.md missing (plan Phase 0 output)")
    if not plan:
        return r
    nc = nc_markers(plan, bare=False) + nc_markers(research, bare=False)
    tech = section(plan, r"^##\s+Technical Context\b") or ""
    nc_fields = NC_TECH_FIELD.findall(without_code_fences(tech))
    r.add("no-needs-clarification", not nc and not nc_fields, f"{len(nc)} bracketed + {len(nc_fields)} Technical Context NEEDS CLARIFICATION remain" if (nc or nc_fields) else "none")
    cc = section(plan, CONST_CHECK_H)
    r.add("constitution-check", cc is not None, "Constitution Check section present" if cc is not None else "no '## Constitution Check' section in plan.md")
    if cc is not None:
        # every Constitution Check / post-design re-check section, not just the first one
        fails = list(dict.fromkeys(m.group(0).strip()[:120] for body in sections(plan, CONST_CHECK_H) for m in CONST_FAIL_CELL.finditer(without_code_fences(body))))
        justified = False
        ct = section(plan, COMPLEXITY_H)
        if ct:
            rows = [l.strip() for l in ct.splitlines() if l.strip().startswith("|") and not re.match(r"^\|\s*[-:| ]+\|?\s*$", l.strip())]
            body_rows = rows[1:] if rows else []
            justified = any(not COMPLEXITY_PLACEHOLDER_ROW.search(row) for row in body_rows)
        ok = not fails or justified
        detail = "no violations" if not fails else (f"{len(fails)} violation row(s), justified in Complexity Tracking" if justified else f"unjustified violation(s): {fails[:3]}")
        r.add("constitution-no-unjustified-violation", ok, detail)
    left = PLAN_LEFTOVERS.findall(plan)
    r.add("plan-no-template-leftovers", not left, f"template text left in plan.md: {sorted(set(left))[:4]}" if left else "none")
    for name in ("data-model.md", "quickstart.md"):
        r.add(f"artifact-{name}", (feature_dir / name).exists(), f"{name} {'present' if (feature_dir / name).exists() else 'missing'} (warn-only: conditional in the template)", warn_only=True)
    if re.search(r"(?i)\bcontracts/", plan) and re.search(r"(?i)(external interfaces?|API|endpoint)", plan):
        has = (feature_dir / "contracts").is_dir() and any((feature_dir / "contracts").iterdir())
        r.add("artifact-contracts", has, "contracts/ referenced by plan.md " + ("and present" if has else "but missing"), warn_only=True)
    return r


# --------------------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------------------


@dataclass
class Task:
    id: str
    done: bool
    mark: str
    text: str
    line: int
    body: str = ""  # indented continuation lines (real tasks.md often put the test file in a sub-bullet)
    human: bool = False  # reserved for a person (FOUNDER GATE, Operator steps, PERSON:, "[H]", ...)


# Human-only tasks. Measured on real task lines from several Spec Kit repos (tests/test_gates_fixall.py,
# test_gates_review3.py). Both errors cost: a false positive drops real work
# from the implement gate, a miss lets an agent do and tick a person's task (all-tasks-checked then passes). So a task is
# human-only when it NAMES itself so: a label at the start of its description (after "[P] [US1]"-style tags), a
# parenthetical that starts with the label, a human tag ("[H]", "[RUN]", or one the file's own legend reserves for a person:
# "`[OPERATOR]` marks a task that is executed by a person"), or an explicit prohibition ("not for agents", "the implementer
# must not tick this"). Prose that merely mentions a founder, operator or owner is agent work ("Operator script …",
# "owner-only via resolveVariantOwner", "report.md: the founder gate, red/green …", "Commit the founder-ticked checklist").
TASK_LEAD_TAGS = re.compile(r"^(?:[ \t]*(?:\*\*|`)?\[[^\]\n]{1,60}\](?:\*\*|`)?)*[ \t]*")
TASK_TAG = re.compile(r"\[([^\]\n]{1,60})\]")
# "[H] = human-gated operator action"; "A `[RUN]` task is never ticked by an implementation session",
# "[MANUAL]", "[NOT CLAIMABLE BY THE IMPLEMENTER — AIP §10]" (only among the leading tags, never mid-sentence).
HUMAN_TAG = re.compile(r"^(?:H|RUN|MANUAL|NOT CLAIMABLE\b.*)$")
# What a legend (or a tag's own text) says about a person's task, with markdown emphasis removed: "executed by a person …
# never run by an agent" (`[OPERATOR]`), "operator act (never autonomous)" (`[op]`),
# "never ticked by the implementer" / "deliberately left unticked" (`[REVIEW]`), "human-gated … Claude Code never
# executes these autonomously" (`[H]`), "[AIP §10 — performed by the operator, not by the implementer]".
HUMAN_LEGEND = re.compile(
    r"\b(?:executed|performed|done|carried\s+out)\s+by\s+(?:a|the)\s+(?:person|human|operator|founder|owner)s?\b"
    r"|\bnever\s+(?:run|executed|performed|ticked|claimed)\s+by\s+(?:an?\s+|the\s+|any\s+)?(?:AI\s+)?(?:agents?|implementers?|implementation\s+sessions?)\b"
    r"|\bnever\s+autonomous(?:ly)?\b|\bnever\s+executes?\s+(?:these|this|them|it)\s+autonomously\b"
    r"|\bnot\s+by\s+the\s+(?:implementer|agent)s?\b|\bhuman[- ](?:gated|only|work)\b|\boperator\s+acts?\b"
    r"|\bdeliberately\s+(?:left\s+)?(?:unticked|not\s+ticked)\b",
    re.I,
)
# a tag spelled out in a legend line: "`[OPERATOR]`", "**[REVIEW]**", "**`[H]`" (its definition runs to the next tag)
LEGEND_TAG = re.compile(r"(?:`|\*\*)\[(?P<tag>[^\]\n]{1,40})\]")
_HUMAN_COMPOUND = (
    r"founders?(?:'s)?[ -](?:gate|action|decision|review|task|step|sign-?off|approval)"
    r"|human[ -](?:gate|gated|action|task|step|sign-?off|approval)"
    r"|operators?[ -](?:steps?|action|act|task|run|gate)"
    r"|owner[ -](?:action|decisions?|gate|task|step|sign-?off|approval)"
)
# "-only" compounds are also how specs state an authorisation rule ("Owner-only (`requireOwner`) guard",
# "Operator-only (RBAC ops:write) endpoints"), so they count as a label only before ":" or a dash.
_HUMAN_ONLY_COMPOUND = r"(?:founders?|human|operators?|owner)[ -]only"
# Compound labels may be followed by "(", "," or "." ("FOUNDER GATE (answered …)", "Operator act, not autonomous"); a bare
# "Founder"/"Operator"/"Person" only by a dash, or by ":" when written in capitals or bold ("FOUNDER:", "PERSON:",
# "**Operator**:"), so "Operator (admin) dashboard", "Operator, owner roles", "Operator: add the /ops page" and
# "Person: add the person table" stay agent work.
HUMAN_LABEL = re.compile(
    rf"^[*_ \t]*(?:(?:{_HUMAN_COMPOUND})[*_]*[ \t]*(?:[:.,(—–]|-[ \t]|$)|{_HUMAN_ONLY_COMPOUND}[*_]*[ \t]*(?:[:—–]|-[ \t]|$)"
    r"|(?:founders?|operators?|person)[*_]*[ \t]*(?:[—–]|-[ \t]))",
    re.I,
)
HUMAN_BARE_COLON = re.compile(r"^[ \t]*(?:(?:\*\*|__)(?i:founders?|operators?|person)(?:\*\*|__)|FOUNDERS?|OPERATORS?|PERSON)[ \t]*:")
# what follows a label that names an endpoint, not a person's task ("Founder review: GET /admin/reviews …")
LABEL_ENDPOINT = re.compile(r"^[*_`\s]*(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/")
HUMAN_LEAD_PAREN = re.compile(r"^[*_ \t]*\((?P<body>[^()\n]{1,200})\)")  # "*(Optional, human-gated, spends money; …)*"
HUMAN_PAREN_ITEM = re.compile(r"^(?:human[ -]gated?|human[ -]only|founders?[ -](?:gate|only|action)|operators?[ -]steps?|not for agents?)$", re.I)
# the phrase must end the parenthetical or be followed by a separator: "(Operator step — …)", "(operator step: …)",
# "(not for agents)"; "(operator steps for the cutover …)" describes a document's content
HUMAN_PAREN_ANYWHERE = re.compile(
    r"\(\s*[*_]*\s*(?:operators?[ -]steps?|founders?[ -](?:gate|action|only)|human[ -](?:gate|only)|not\s+for\s+agents?)[*_]*\s*(?:[:—–),;]|-\s)",
    re.I,
)
# "BRD GATE (2026-11-08: the founder chose …): a founder ratifies …", "**Gate: the founder approves EXC-003 …**"
HUMAN_GATE_SUBJECT = re.compile(r"^[*_ \t]*(?:[A-Za-z][\w-]*[ \t]+)?gate\b[^:—–\n]{0,60}[:—–][ \t]*[*_]*[ \t]*(?:a|the)[ \t]+founders?\b(?![-'’])", re.I)
# Explicit prohibitions whose object is the task itself, matched with markdown emphasis removed ("Do **not** tick this on
# the implementer's behalf"). Not "the implementer must not claim …": agent tasks quote that about other work.
HUMAN_ANYWHERE = re.compile(
    r"\bnot\s+for\s+(?:the\s+)?(?:AI\s+)?agents?\b"
    r"|\b(?:must\s+not|mustn't|must\s+never|never|cannot|can't|do\s+not|don't|may\s+not|should\s+not)\s+be\s+"
    r"(?:ticked|checked|marked|completed|claimed|self-certified)\s+by\s+(?:an?|any|the)\s+(?:AI\s+)?(?:agents?|implementers?|implementation\s+sessions?)\b"
    r"|\b(?:agents?|implementers?)\s+(?:must|may|should)\s+(?:not|never)\s+tick\s+(?:this|it)\b"
    r"|\bdo\s+not\s+tick\s+this\b",
    re.I,
)
LIST_ITEM_START = re.compile(r"^(?:[-*+]|\d+[.)])\s")


def legend_human_tags(lines: list[str]) -> frozenset[str]:
    """Tags a tasks.md legend reserves for a person: each tag spelled out in a non-task line, read with the text up to
    the next tag on that line ("**`[H]` = human-gated** (…). **`[code]`** tasks are CI-verifiable" marks [H], not [code])."""
    out: set[str] = set()
    for line in lines:
        ms = list(LEGEND_TAG.finditer(line))
        for i, m in enumerate(ms):
            definition = line[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(line)]
            if HUMAN_LEGEND.search(re.sub(r"[*`_]", "", definition)):
                out.add(m.group("tag").strip())
    return frozenset(out)


def _label_match(lead: str) -> re.Match[str] | None:
    """The human-only label at the start of a task description, unless what follows it names an HTTP endpoint."""
    m = HUMAN_LABEL.match(lead) or HUMAN_BARE_COLON.match(lead)
    if m and LABEL_ENDPOINT.match(lead[m.end():]):
        return None
    return m


def is_human_task(text: str, body: str = "", legend_tags: frozenset[str] = frozenset()) -> bool:
    """True when a task line (``Task.text`` + its continuation ``body``) reserves the task for a person. ``legend_tags``
    are the tags its tasks.md legend reserves for a person (legend_human_tags)."""
    text = text or ""
    tags_end = TASK_LEAD_TAGS.match(text).end()
    for tag in TASK_TAG.findall(text[:tags_end]):
        tag = tag.strip()
        if HUMAN_TAG.match(tag) or tag in legend_tags or HUMAN_LEGEND.search(tag):
            return True
    lead = text[tags_end:]
    if _label_match(lead) or HUMAN_GATE_SUBJECT.match(lead):
        return True
    pm = HUMAN_LEAD_PAREN.match(lead)
    if pm and any(HUMAN_PAREN_ITEM.match(item.strip(" \t*_")) for item in re.split(r"[,;]", pm.group("body"))):
        return True
    # the task's own hard-wrapped first paragraph only (never its sub-bullets, which often quote other tasks)
    wrapped: list[str] = []
    for line in (body or "").splitlines()[:3]:
        if LIST_ITEM_START.match(line):
            break
        wrapped.append(line)
    para = " ".join([text, *wrapped])
    return bool(HUMAN_PAREN_ANYWHERE.search(para) or HUMAN_ANYWHERE.search(re.sub(r"[*`]", "", para)))


def pipeline_step(task: Task, commands: tuple[str, ...]) -> str | None:
    """The pipeline command (one of ``commands``, e.g. "/cc-review", "/cc-ship") that a human-only task names as its own
    action, or None: "Operator steps (not for agents): `/cc-review`, then `/cc-ship` …", "**Operator-run**:
    `/cc-ship` for …", "Review per … (operator step: `/cc-review` is user-invoked)". A run performs those
    itself, so they do not hold the ship pause. A task that does something else first ("FOUNDER GATE: approve the
    release notes, then `/cc-ship`") is not one."""
    cmds = [c if c.startswith("/") else "/" + c for c in commands if c]
    if not task.human or not cmds:
        return None
    alt = "|".join(re.escape(c) for c in cmds)
    text = task.text[TASK_LEAD_TAGS.match(task.text).end():]
    m = re.match(rf"^[^/`:—–\n]{{0,80}}?(?:[:—–)]|-\s)[*_ \t]*`?(?P<cmd>{alt})`?(?![\w-])", text)
    m = m or re.search(rf"\(\s*[*_]*\s*operators?[ -]steps?[*_]*\s*[:—–]\s*`?(?P<cmd>{alt})`?(?![\w-])", text, re.I)
    return m.group("cmd") if m else None


def parse_tasks(text: str) -> list[Task]:
    out: list[Task] = []
    cur: Task | None = None
    legend: list[str] = []
    for line_no, line in enumerate((text or "").split("\n"), 1):
        m = TASK_LINE.match(line)
        if m:
            cur = Task(m.group("id"), m.group("mark") in "xX", m.group("mark"), m.group("rest").strip(), line_no)
            out.append(cur)
        elif cur is not None and (not line.strip() or line[:1] in (" ", "\t")):
            if line.strip():
                cur.body += line.strip() + "\n"
        else:
            cur = None
            legend.append(line)
    tags = legend_human_tags(legend)
    for t in out:
        t.human = is_human_task(t.text, t.body, tags)
    return out


def requires_tests(constitution_text: str, plan_text: str) -> bool:
    return bool(TESTS_REQUIRED.search(constitution_text or "") or TESTS_REQUIRED.search(plan_text or ""))


def gate_tasks(feature_dir: Path, repo: Path) -> GateResult:
    r = GateResult("tasks")
    text = strip_front_matter(read(feature_dir / "tasks.md"))
    r.add("tasks-exists", bool(text), "tasks.md present" if text else "tasks.md missing")
    if not text:
        return r
    body = without_code_fences(text)
    tasks = parse_tasks(body)
    r.facts["task_count"] = len(tasks)
    r.facts["human_tasks"] = [t.id for t in tasks if t.human]
    r.add("at-least-one-task", len(tasks) >= 1, f"{len(tasks)} task(s)")
    ids = [t.id for t in tasks]
    dups = sorted({i for i in ids if ids.count(i) > 1})
    r.add("unique-ids", not dups, f"duplicate task IDs: {dups[:10]}" if dups else "all IDs unique")
    # every checkbox under a Phase heading must be a well-formed task line
    malformed = []
    in_phase = False
    for line in body.splitlines():
        if PHASE_H2.match(line):
            in_phase = True
            continue
        if re.match(r"^##\s", line):
            in_phase = bool(PHASE_H2.match(line))
            continue
        if in_phase and CHECKBOX_LINE.match(line) and not TASK_LINE.match(line):
            malformed.append(line.strip()[:80])
    r.add("task-format", not malformed, f"{len(malformed)} checkbox line(s) without a T### ID under a phase: {malformed[:3]}" if malformed else "all task lines match '- [ ] T### …'")
    left = TASK_LEFTOVERS.findall(text)
    r.add("no-template-leftovers", not left, f"template leftovers: {sorted(set(left))}" if left else "none")
    need = requires_tests(read(repo / ".specify" / "memory" / "constitution.md"), read(feature_dir / "plan.md"))
    r.facts["tests_required"] = need
    if need:
        has = any(TEST_HINT.search(t.text) or TEST_HINT.search(t.body) for t in tasks)
        r.add("test-tasks", has, "constitution/plan require tests; " + ("test tasks present" if has else "no task references tests"))
    return r


def feature_tasks(feature_dir: Path) -> list[Task]:
    return parse_tasks(without_code_fences(strip_front_matter(read(feature_dir / "tasks.md"))))


def task_progress(feature_dir: Path) -> tuple[int, int]:
    """(done, total) over AGENT tasks only; human-only tasks are reported by human_tasks()."""
    tasks = [t for t in feature_tasks(feature_dir) if not t.human]
    return sum(1 for t in tasks if t.done), len(tasks)


def human_tasks(feature_dir: Path) -> list[Task]:
    """Human-only tasks of tasks.md in file order, with their done flags."""
    return [t for t in feature_tasks(feature_dir) if t.human]


def task_key(text: str) -> str:
    """A task's description without its leading tags and human-only label, as lower-case words: the same task
    before and after an agent rewrites its label away ("FOUNDER GATE: approve X" and "Approve X" share a key)."""
    t = text or ""
    t = t[TASK_LEAD_TAGS.match(t).end():]
    m = _label_match(t) or HUMAN_LEAD_PAREN.match(t)
    if m:
        t = t[m.end():]
    return " ".join(re.findall(r"[a-z0-9]+", t.lower()))


# --------------------------------------------------------------------------------------
# analyze
# --------------------------------------------------------------------------------------


@dataclass
class Finding:
    id: str
    category: str
    severity: str
    rest: str


SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
# Supervisor-requested trailer: "ANALYZE_SUMMARY critical=<n> high=<n> medium=<n> low=<n>" (keys in any order, any case;
# backticks/bold around it allowed). Every complete line counts and the largest count per severity wins.
ANALYZE_TRAILER = re.compile(r"(?im)^[ \t>]*[`*_]*ANALYZE[_-]SUMMARY[`*_]*:?[`*_]*[ \t]+(?P<body>[^\n]*)$")
ANALYZE_TRAILER_PAIR = re.compile(r"(?i)\b(critical|high|medium|low)[`*_]*[ \t]*[=:][ \t]*[`*_]*(\d+)\b")


def parse_analyze_trailer(text: str) -> dict[str, int] | None:
    """{"critical", "high", "medium", "low"}: per severity, the largest count over every complete ANALYZE_SUMMARY line,
    or None when there is none. Fail closed: a later "all zero" line (the trailer turn) never erases a count the
    report itself stated. A line that misses any of the four keys is not a trailer (it cannot vouch for the counts it
    leaves out)."""
    found: dict[str, int] | None = None
    for m in ANALYZE_TRAILER.finditer(text or ""):
        pairs = {k.lower(): int(v) for k, v in ANALYZE_TRAILER_PAIR.findall(m.group("body"))}
        if all(k in pairs for k in ("critical", "high", "medium", "low")):
            line = {k: pairs[k] for k in ("critical", "high", "medium", "low")}
            found = line if found is None else {k: max(found[k], line[k]) for k in line}
    return found


@dataclass
class AnalyzeReport:
    found: bool
    findings: list[Finding]
    critical_metric: int | None
    offered_remediation: bool
    high_metric: int | None = None
    has_table: bool = True
    explicit_zero: bool = False
    trailer: dict[str, int] | None = None  # parse_analyze_trailer(); lower-case keys

    @property
    def counts(self) -> dict[str, int]:
        """Per-severity counts of the parsed findings table (trailer not included)."""
        c = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for f in self.findings:
            c[f.severity] = c.get(f.severity, 0) + 1
        return c

    @property
    def effective_counts(self) -> dict[str, int]:
        """Per-severity maximum of the table and the trailer (fail closed: the larger stated count wins)."""
        c = self.counts
        if self.trailer is None:
            return c
        return {s: max(c[s], self.trailer[s.lower()]) for s in SEVERITIES}

    @property
    def blocking(self) -> int:
        c = self.effective_counts
        return c["CRITICAL"] + c["HIGH"]


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def parse_analyze(text: str, trailer_text: str = "") -> AnalyzeReport:
    """Findings from the analyze report (assistant text). Table-aware: the Severity column is located by its header;
    tables WITHOUT a Severity column take the severity of the enclosing "### High"-style heading (real variant).
    ``trailer_text`` is an optional follow-up reply carrying the ANALYZE_SUMMARY trailer; summary lines in ``text`` and
    in it all count (the larger stated count per severity wins)."""
    text = text or ""
    trailer = parse_analyze_trailer(text + "\n" + (trailer_text or ""))
    found = bool(ANALYZE_H2.search(text) or ANALYZE_TABLE_HDR.search(text))
    findings: dict[str, Finding] = {}
    finding_lines: set[int] = set()
    heading_sev: str | None = None
    header: list[str] | None = None
    lines = text.split("\n")

    def is_sep(row: str) -> bool:
        return row.startswith("|") and "-" in row and re.fullmatch(r"[|\s:\-]+", row) is not None

    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#"):
            hm = ANALYZE_SEV_HEADING.match(s)
            heading_sev = hm.group(1).upper() if hm else None
            header = None
            continue
        if not s.startswith("|"):
            if s:
                header = None  # prose ends the table
            continue
        if is_sep(s):
            continue
        cells = _cells(s)
        if i + 1 < len(lines) and is_sep(lines[i + 1].strip()):
            header = [c.strip("*_ ").lower() for c in cells]  # a row followed by |---| is a header
            continue
        legacy = ANALYZE_ROW.match(s)
        idm = ANALYZE_ID_CELL.match(cells[0]) if cells else None
        if not idm:
            continue
        sev: str | None = None
        rest_cells = cells[2:]
        sev_col = next((j for j, h in enumerate(header or []) if h.startswith("sev")), None)
        if sev_col is not None and sev_col < len(cells):
            sm = ANALYZE_SEV_CELL.match(cells[sev_col])
            sev = sm.group(1).upper() if sm else None
            rest_cells = cells[sev_col + 1:]
        elif header and header[0] == "id" and heading_sev:
            sev = heading_sev
        elif legacy:
            sev = legacy.group("sev").upper()
            rest_cells = [legacy.group("rest").strip().strip("|").strip()]
        if sev is None:
            continue
        fid = idm.group(1).upper()
        finding_lines.add(i)
        findings.setdefault(fid, Finding(fid, cells[1] if len(cells) > 1 else "", sev, " | ".join(c for c in rest_cells if c)))
    metric_text = "\n".join(l for i, l in enumerate(lines) if i not in finding_lines)
    crit = [int(m.group(1)) for m in ANALYZE_CRIT_METRIC.finditer(metric_text)] + [int(m.group(1)) for m in ANALYZE_CRIT_METRIC_PRE.finditer(metric_text)]
    high = [int(m.group(1)) for m in ANALYZE_HIGH_METRIC.finditer(metric_text)]
    return AnalyzeReport(
        found,
        list(findings.values()),
        max(crit) if crit else None,  # fail closed: the largest stated count wins
        bool(ANALYZE_OFFER.search(text)),
        max(high) if high else None,
        has_table=bool(ANALYZE_ANY_TABLE.search(text)),
        explicit_zero=bool(ANALYZE_ZERO.search(text)),
        trailer=trailer,
    )


def _fmt_counts(c: dict[str, int]) -> str:
    return " ".join(f"{s.lower()}={c[s]}" for s in SEVERITIES)


def gate_analyze(report: AnalyzeReport, *, read_only_ok: bool = True) -> GateResult:
    r = GateResult("analyze")
    table, trailer, c = report.counts, report.trailer, report.effective_counts
    r.facts.update({"counts": table, "critical_metric": report.critical_metric, "trailer": trailer, "effective_counts": c})
    r.add("report-present", report.found, "analysis report found" if report.found else "no 'Specification Analysis Report' / findings table in the analyze output (fail closed)")
    table_parsed = bool(report.findings) or report.has_table
    metric_says_blocking = bool(report.critical_metric) or bool(report.high_metric)
    if report.found and not table_parsed and not report.explicit_zero:
        if trailer is None:
            r.add("findings-parsed", False, "the report has no findings table, no explicit 'no issues found' statement and no ANALYZE_SUMMARY trailer, so the CRITICAL/HIGH count cannot be verified (fail closed)")
        elif not any(trailer.values()):
            r.add("findings-parsed", not metric_says_blocking,
                  "no findings table; the ANALYZE_SUMMARY trailer reports zero findings (explicit zero)" if not metric_says_blocking
                  else f"the ANALYZE_SUMMARY trailer reports zero findings but the report's metrics say critical={report.critical_metric} high={report.high_metric} (fail closed)")
        else:
            r.add("findings-parsed", True, f"no findings table; severity counts taken from the ANALYZE_SUMMARY trailer ({_fmt_counts(c)})")
    if trailer is not None and table_parsed and any(trailer[s.lower()] != table[s] for s in SEVERITIES):
        r.add("trailer-matches-table", False, f"ANALYZE_SUMMARY says {_fmt_counts({s: trailer[s.lower()] for s in SEVERITIES})} but the findings table has {_fmt_counts(table)}; using the per-severity maximum ({_fmt_counts(c)})", warn_only=True)

    def detail(sev: str) -> str:
        ids = [f.id for f in report.findings if f.severity == sev][:8]
        extra = f" (the ANALYZE_SUMMARY trailer says {trailer[sev.lower()]}, the table lists {table[sev]})" if trailer is not None and trailer[sev.lower()] > table[sev] else ""
        return f"{c[sev]} {sev} finding(s): {ids}{extra}"

    r.add("zero-critical", c["CRITICAL"] == 0, detail("CRITICAL"))
    r.add("zero-high", c["HIGH"] == 0, detail("HIGH"))
    if report.critical_metric is not None:
        r.add("critical-metric-agrees", report.critical_metric <= c["CRITICAL"] or c["CRITICAL"] > 0,
              f"'Critical Issues Count' says {report.critical_metric}, table has {c['CRITICAL']} (fail closed when the metric reports more)")
    if report.high_metric is not None:
        r.add("high-metric-agrees", report.high_metric <= c["HIGH"] or c["HIGH"] > 0,
              f"the report's HIGH count says {report.high_metric}, table has {c['HIGH']} (fail closed when the metric reports more)")
    r.add("analyze-read-only", read_only_ok, "analyze did not modify the feature directory" if read_only_ok else "the analyze run modified files in the feature directory (it must be read-only)")
    return r


# --------------------------------------------------------------------------------------
# review (cc-review is report-only; labels blocker / nit / question)
# --------------------------------------------------------------------------------------


@dataclass
class ReviewReport:
    trailer_blockers: int | None
    explicit_none: bool
    blocker_items: int
    blockers_listed: bool

    @property
    def blockers(self) -> int | None:
        """Best estimate; None when unknown (fail closed)."""
        if self.trailer_blockers is not None:
            return max(self.trailer_blockers, self.blocker_items, 1 if self.blockers_listed else 0)
        if self.blocker_items or self.blockers_listed:
            return max(self.blocker_items, 1)
        if self.explicit_none:
            return 0
        return None


def _blocker_section_verdicts(text: str) -> tuple[bool, bool, int]:
    """Read every "## Blockers" heading section and every stand-alone "**Blockers**" label block.

    Returns (some section says none, some section lists something, largest "(N)" count in a heading).
    A section is "none" only when each of its sub-groups ("### API", "**Web/admin**") starts with None / No blockers / 0;
    anything else under the heading — "**1. …**", "**B1** …", "There is one.", a sub-heading with findings — counts as
    listed. An empty section is neither (unknown).
    """
    none = listed = False
    count = 0
    lines = text.split("\n")
    for m in REVIEW_BLOCKERS_H.finditer(text):
        if m.group(2):
            count = max(count, int(m.group(2)))
        level = len(m.group(1))
        start = text.count("\n", 0, m.start()) + 1
        body: list[str] = []
        for l in lines[start:]:
            hm = re.match(r"^(#{1,6})\s", l)
            if hm and len(hm.group(1)) <= level:
                break
            body.append(l)
        verdict = _blocker_body_verdict(body)
        none |= verdict == "none"
        listed |= verdict == "listed"
    for m in REVIEW_BLOCKERS_LABEL.finditer(text):
        start = text.count("\n", 0, m.start()) + 1
        body = []
        for l in lines[start:]:
            if re.match(r"^#{1,6}\s", l) or (REVIEW_GROUP_LABEL.match(l.strip()) and body and any(b.strip() for b in body)):
                break
            body.append(l)
        verdict = _blocker_body_verdict(body)
        none |= verdict == "none"
        listed |= verdict == "listed"
    return none, listed, count


def _blocker_body_verdict(body: list[str]) -> str:
    groups: list[tuple[str | None, list[str]]] = [(None, [])]
    for l in body:
        s = l.strip()
        if REVIEW_GROUP_LABEL.match(s) and not REVIEW_NONE_START.match(s):  # "**None.**" is content, not a label
            groups.append((s, []))
        elif s:
            groups[-1][1].append(s)
    verdicts = []
    for label, content in groups:
        if content:
            verdicts.append("none" if REVIEW_NONE_START.match(content[0]) else "listed")
        elif label is not None:
            verdicts.append("listed")  # a sub-group label with nothing under it is not a "none" (fail closed)
    if not verdicts:
        return "unknown"
    return "listed" if "listed" in verdicts else "none"


def parse_review(report_text: str, trailer_text: str = "") -> ReviewReport:
    report_text = report_text or ""
    both = report_text + "\n" + (trailer_text or "")
    t = REVIEW_TRAILER.findall(both)
    sec_none, sec_listed, heading_count = _blocker_section_verdicts(report_text)
    return ReviewReport(
        trailer_blockers=int(t[-1]) if t else None,
        explicit_none=bool(REVIEW_BLOCKERS_NONE.search(report_text)) or sec_none,
        blocker_items=max(len(REVIEW_BLOCKER_ITEM.findall(report_text)), heading_count),
        blockers_listed=bool(REVIEW_BLOCKERS_LIST.search(report_text)) or sec_listed,
    )


def gate_review(report: ReviewReport, tooling: "ToolingResult | None") -> GateResult:
    r = GateResult("review")
    b = report.blockers
    r.facts["blockers"] = b
    r.add("review-summary", report.trailer_blockers is not None or report.explicit_none, "REVIEW_SUMMARY trailer or explicit 'Blockers: none' found" if (report.trailer_blockers is not None or report.explicit_none) else "could not determine the blocker count (no REVIEW_SUMMARY trailer, no explicit 'Blockers: none')")
    r.add("zero-must-fix", b == 0, f"must-fix (blocker) findings: {b if b is not None else 'unknown'}")
    if report.trailer_blockers is not None and report.blocker_items and report.trailer_blockers < report.blocker_items:
        r.add("trailer-agrees", False, f"trailer says blockers={report.trailer_blockers} but {report.blocker_items} blocker item(s) are listed")
    elif report.trailer_blockers == 0 and report.blockers_listed:
        r.add("trailer-agrees", False, "trailer says blockers=0 but the report's Blockers section lists findings")
    if tooling is not None:
        for c in tooling.checks():
            r.checks.append(c)
    return r


# --------------------------------------------------------------------------------------
# implement + tooling
# --------------------------------------------------------------------------------------

ToolRunner = Callable[[str, Path, float], CmdResult]


# ---- test-failure baseline ("no new failures vs base") --------------------------------------------------------------
# Formats captured from the real runners (tests/fixtures/runners/MANIFEST.json): vitest 4.1, jest 30.4,
# pytest 9 (+ pytest-cov 7.1), node --test (spec + TAP, Node 24), playwright 1.61, pnpm -r and turbo 2.9 prefixes;
# mocha and go test from their documented reporters (not installed here). Anything that cannot be pinned to individual
# tests returns None, so a failing run is only ever excused test by test.

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# Whole-run markers: the failure (or part of the run) is not attributable to individual tests.
UNATTRIBUTABLE = (
    (re.compile(r"Failed to load|Failed Suites \d+|Test suite failed to run|Suite failed to run|ERROR collecting|Interrupted: \d+ error"), "a test file failed to load or collect"),
    (re.compile(r"^[#\t ]*SyntaxError\b|ERR_MODULE_NOT_FOUND|ModuleNotFoundError|ImportError while importing|Cannot find module|error TS\d{4}:|: command not found|ERR_PNPM_NO_SCRIPT", re.M), "an import, syntax or command error outside any test"),
    (re.compile(r"(?i)No test files found(?!, exiting with code 0)|No tests found(?!, exiting with code 0)|no tests ran"), "no tests ran"),
    (re.compile(r"Required test coverage of|Coverage failure:|coverage threshold|does not meet (?:global )?threshold"), "a coverage threshold failed"),
    (re.compile(r"\btimed out (?:in|after) \d|Exceeded timeout of \d|Test timeout of \d|Timeout of \d+\s*ms exceeded|Timed out \d+\s*ms waiting|Timed out waiting \d|\+{3,} Timeout \+{3,}|Failed: Timeout >|testTimeoutFailure"), "a timeout"),
    (re.compile(r"Unhandled Errors|Unhandled Rejection|Vitest caught \d+ unhandled|^\s*Errors\s{2,}\d+ errors?\b", re.M), "unhandled errors outside tests"),
    (re.compile(r"stopping after \d+ failures|KeyboardInterrupt|ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL|\[build failed\]|\[setup failed\]|^panic: |JavaScript heap out of memory|Segmentation fault", re.M), "the run stopped early (bail, build failure or crash), so later tests never ran"),
    # failures after (or around) the tests that a runner's failed-test count does not include: vitest "error during
    # close" (a globalSetup teardown threw), jest "Got error running globalTeardown", playwright global setup/teardown
    (re.compile(r"error during close\b|Got error running global(?:Setup|Teardown)|Error in global (?:setup|teardown)\b", re.I), "a global setup/teardown failed outside any test"),
    # jest under --ci / CI=1 fails the run on an obsolete snapshot even when every test passed
    (re.compile(r"^\s*Snapshots:\s+.*\b\d+ obsolete\b|\b\d+ snapshots? obsolete\b", re.M), "obsolete snapshots fail the run outside any test"),
    # a conftest/plugin hook (pytest_unconfigure, pytest_sessionfinish, ...) or pytest itself raised
    (re.compile(r"^INTERNALERROR>|File \"[^\"\n]*[/\\](?:_pytest|pluggy)[/\\]", re.M), "pytest failed outside any test (a hook, plugin or internal error)"),
)
# Workspace prefixes: turbo "@acme/web:test: " / "@acme/web#test: ", pnpm -r "packages/web test: " (announced by its
# "packages/web test$ <cmd>" echo line, so only real package prefixes are stripped).
TURBO_PREFIX = re.compile(r"^(?P<pkg>(?:@[\w.-]+/)?[\w.-]+)[:#](?P<task>[\w-]+(?::[\w-]+)*):(?: |$)")
PNPM_ECHO = re.compile(r"^(?P<pkg>[\w@.][^\s$]*) (?P<script>[\w-]+(?::[\w-]+)*)\$ ")
TURBO_TASKS = re.compile(r"(?m)^\s*Tasks:\s+(\d+) successful, (\d+) total")
TURBO_FAILED = re.compile(r"(?m)^\s*Failed:\s+(\S+#\S+(?:,\s*\S+#\S+)*)\s*$")
WRAPPER_FAILURE = re.compile(r"ERR_PNPM_|ELIFECYCLE|npm ERR!|Command failed|run failed|exited \(\d+\)|^\s*(?:FAIL|FAILED|ERROR)\b", re.M)
# vitest
VITEST_TESTS = re.compile(r"^\s*Tests\s{2,}(?P<body>[^()\n]*?)\s*\((?P<total>\d+)\)\s*$")
VITEST_FAIL = re.compile(r"^\s*FAIL\s+(?P<id>.+?\s>\s.+?)\s*$")
# jest
JEST_TESTS = re.compile(r"^\s*Tests:\s+(?P<body>.*\btotal)\s*$")
JEST_FILE = re.compile(r"^\s*(?P<st>FAIL|PASS)\s+(?P<file>\S.*?)\s*(?:\(\s*\d+(?:\.\d+)?\s*m?s\s*\))?$")
JEST_TEST = re.compile(r"^\s*●\s+(?P<title>.+?)\s*$")
# pytest
PYTEST_SUMMARY = re.compile(r"^=*\s*(?P<body>\d+ (?:failed|passed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun)(?:, \d+ \w+)*) in [\d.]+s(?: \([^)]*\))?\s*=*$")
PYTEST_ITEM = re.compile(r"^(?P<kind>FAILED|ERROR) (?P<id>(?:[^\s\[]|\[[^\]\n]*\])+)(?: - .*)?$")
# node --test
NODE_SPEC_FAIL = re.compile(r"^ℹ fail (\d+)$")
NODE_SPEC_HDR = re.compile(r"^✖ failing tests:\s*$")
NODE_SPEC_AT = re.compile(r"^test at (?P<file>.+?):\d+:\d+$")
NODE_SPEC_X = re.compile(r"^✖ (?P<name>.+?)(?: \([\d.]+m?s\))?$")
NODE_TAP_FAIL = re.compile(r"^# fail (\d+)$")
TAP_SUBTEST = re.compile(r"^(?P<ind>[ \t]*)# Subtest: (?P<name>.+)$")
TAP_RESULT = re.compile(r"^(?P<ind>[ \t]*)(?P<ok>not ok|ok) \d+(?: - (?P<name>.+?))?(?: # (?:SKIP|TODO)\b.*)?$")
TAP_YAML_KEY = re.compile(r"^[ \t]*(?P<k>type|location|failureType):[ \t]*'?(?P<v>[^'\n]*)'?\s*$")
# playwright
PW_RUNNING = re.compile(r"(?m)^Running \d+ tests? using \d+ workers?")
PW_COUNT = re.compile(r"^(?P<ind>\s+)(?P<n>\d+) (?P<kind>failed|flaky|passed|skipped|did not run|interrupted)(?: \(.*\))?\s*$")
# mocha
MOCHA_PASSING = re.compile(r"^\s+(\d+) passing \(")
MOCHA_FAILING = re.compile(r"^\s+(\d+) failing\s*$")
MOCHA_ITEM = re.compile(r"^(?P<ind>\s+)\d+\) (?P<title>.*)$")
MOCHA_HOOK = re.compile(r'"(?:before|after) (?:all|each)" hook')
# go test
GO_FAIL_TEST = re.compile(r"^\s*--- FAIL: (?P<name>\S+) \(")
GO_PKG = re.compile(r"^(?P<st>ok  |FAIL)\t(?P<pkg>[^\t]+)(?:\t(?P<rest>.*))?$")
TEST_FILE_NAME = re.compile(r"\.(?:[cm]?[jt]sx?|py)$")


class _Unattributable(Exception):
    pass


@dataclass
class _Parsed:
    ids: set[str] = field(default_factory=set)
    summaries: int = 0  # runner summaries recognised
    failed: int = 0  # failures those summaries report


def _clean_lines(output: str) -> list[str]:
    out = []
    for line in ANSI.sub("", (output or "").replace("\r\n", "\n")).split("\n"):
        parts = [p for p in line.split("\r") if p.strip()]  # progress redraws: keep the last frame
        out.append((parts[-1] if parts else "").rstrip())
    return out


def _norm_test_id(s: str) -> str:
    s = re.sub(r"^(?:[×✗✕✖✘]|x(?=\s))\s+", "", s.strip())
    s = re.sub(r"(\.[cm]?[jt]sx?):\d+:\d+(?=\s|$)", r"\1", s)  # playwright "a.spec.ts:4:5" moves with every edit
    s = s.replace(" › ", " > ")
    s = re.sub(r"\s*[(\[](?:retry|attempt)\s*#?x?\d+[)\]]", "", s, flags=re.I)
    s = re.sub(r"\s*\(\s*\d+(?:\.\d+)?\s*(?:ms|s|m)\s*\)\s*$", "", s)  # "✕ name (3 ms)"
    s = re.sub(r"[\s─━]+$", "", s)
    s = re.sub(r"^\./", "", s)
    return re.sub(r"\s+", " ", s)


def _split_workspace(lines: list[str]) -> tuple[dict[str, list[str]], set[str]]:
    """Group lines by workspace package (key "" = unprefixed) and name the packages the wrapper reports as failed."""
    text = "\n".join(lines)
    pnpm = {f"{m.group('pkg')} {m.group('script')}": m.group("pkg") for m in map(PNPM_ECHO.match, lines) if m}
    turbo_counts: dict[str, int] = {}
    for line in lines:
        m = TURBO_PREFIX.match(line)
        if m:
            turbo_counts[m.group("pkg")] = turbo_counts.get(m.group("pkg"), 0) + 1
    turbo = {p for p, n in turbo_counts.items() if n >= 2}
    segments: dict[str, list[str]] = {}
    for line in lines:
        key, content = "", line
        for prefix, pkg in pnpm.items():
            if line == prefix + ":" or line.startswith(prefix + ": "):
                key, content = pkg, line[len(prefix) + 2:]
                break
        else:
            m = TURBO_PREFIX.match(line)
            if m and m.group("pkg") in turbo:
                key, content = m.group("pkg"), line[m.end():]
        segments.setdefault(key, []).append(content)
    failed = {pkg for prefix, pkg in pnpm.items() if f"{prefix}: Failed" in lines}
    tf = TURBO_FAILED.search(text)
    if tf:
        failed |= {item.strip().split("#", 1)[0] for item in tf.group(1).split(",")}
        tt = TURBO_TASKS.search(text)
        if tt and int(tt.group(1)) + len(tf.group(1).split(",")) < int(tt.group(2)):
            raise _Unattributable("turbo stopped after the first failing task, so other packages never ran (use --continue)")
    return segments, failed


def _parse_vitest(lines: list[str]) -> _Parsed:
    p = _Parsed()
    for line in lines:
        m = VITEST_TESTS.match(line)
        if m:
            p.summaries += 1
            f = re.search(r"(\d+) failed", m.group("body"))
            p.failed += int(f.group(1)) if f else 0
        m = VITEST_FAIL.match(line)
        if m:
            p.ids.add(_norm_test_id(m.group("id")))
    return p


def _parse_jest(lines: list[str]) -> _Parsed:
    p = _Parsed()
    cur: str | None = None
    for line in lines:
        m = JEST_TESTS.match(line)
        if m:
            p.summaries += 1
            f = re.search(r"(\d+) failed", m.group("body"))
            p.failed += int(f.group(1)) if f else 0
            cur = None
            continue
        m = JEST_FILE.match(line)
        if m and " > " not in line:
            cur = _norm_test_id(m.group("file")) if m.group("st") == "FAIL" else None
            continue
        m = JEST_TEST.match(line)
        if m and cur is not None and m.group("title") not in ("Console",):
            p.ids.add(f"{cur} > {_norm_test_id(m.group('title'))}")
    return p


def _parse_pytest(lines: list[str]) -> _Parsed:
    p = _Parsed()
    for line in lines:
        m = PYTEST_SUMMARY.match(line)
        if m:
            p.summaries += 1
            for n, kind in re.findall(r"(\d+) (failed|errors?)\b", m.group("body")):
                p.failed += int(n)
        m = PYTEST_ITEM.match(line)
        if m:
            if m.group("kind") == "ERROR" and "::" not in m.group("id"):
                raise _Unattributable(f"pytest could not collect {m.group('id')}")
            p.ids.add(m.group("id"))
    return p


def _parse_node_spec(lines: list[str]) -> _Parsed:
    p = _Parsed()
    in_failing = False
    file = ""
    for line in lines:
        m = NODE_SPEC_FAIL.match(line)
        if m:
            p.summaries += 1
            p.failed += int(m.group(1))
        if NODE_SPEC_HDR.match(line):
            in_failing = True
            continue
        if not in_failing:
            continue
        m = NODE_SPEC_AT.match(line)
        if m:
            file = m.group("file")
            continue
        m = NODE_SPEC_X.match(line)
        if m:
            name = m.group("name")
            if TEST_FILE_NAME.search(name) and Path(name).name == Path(file).name:
                raise _Unattributable(f"node --test could not load {name}")
            p.ids.add(f"{_norm_test_id(file)} > {_norm_test_id(name)}" if file else _norm_test_id(name))
    return p


def _parse_node_tap(lines: list[str]) -> _Parsed:
    p = _Parsed()
    stack: list[tuple[int, str]] = []
    failed_tests = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        m = NODE_TAP_FAIL.match(line)
        if m:
            p.summaries += 1
            p.failed += int(m.group(1))
        m = TAP_SUBTEST.match(line)
        if m:
            ind = len(m.group("ind").expandtabs())
            stack = [s for s in stack if s[0] < ind] + [(ind, m.group("name").strip())]
            i += 1
            continue
        m = TAP_RESULT.match(line)
        if not m:
            i += 1
            continue
        ind = len(m.group("ind").expandtabs())
        name = (m.group("name") or "").strip()
        chain = [n for d, n in stack if d < ind] + [name]
        stack = [s for s in stack if s[0] < ind]
        meta: dict[str, str] = {}
        j = i + 1
        if j < len(lines) and lines[j].strip() == "---":
            j += 1
            while j < len(lines) and lines[j].strip() != "...":
                km = TAP_YAML_KEY.match(lines[j])
                if km and len(lines[j]) - len(lines[j].lstrip()) == ind + 2:
                    meta[km.group("k")] = km.group("v")
                j += 1
        if m.group("ok") == "not ok" and meta.get("type", "test") != "suite":
            failed_tests += 1
            if meta.get("failureType") != "subtestsFailed":
                loc = re.sub(r":\d+:\d+$", "", meta.get("location", ""))
                if TEST_FILE_NAME.search(name) and loc and Path(name).name == Path(loc).name:
                    raise _Unattributable(f"node --test could not load {name}")
                # TAP locations are absolute and differ between checkouts; the file name keeps the id stable
                prefix = f"{Path(loc).name} > " if loc else ""
                p.ids.add(prefix + " > ".join(_norm_test_id(n) for n in chain))
        i = j if j > i + 1 else i + 1
    if p.summaries and failed_tests != p.failed:
        raise _Unattributable(f"node --test reported {p.failed} failure(s) but {failed_tests} failing test line(s) were found")
    return p


def _parse_playwright(lines: list[str]) -> _Parsed:
    p = _Parsed()
    if not PW_RUNNING.search("\n".join(lines)):
        return p
    listing: int | None = None
    for line in lines:
        m = PW_COUNT.match(line)
        if m:
            p.summaries += 1
            n, kind = int(m.group("n")), m.group("kind")
            if kind in ("interrupted", "did not run") and n:
                raise _Unattributable(f"playwright: {n} test(s) {kind}")
            if kind == "failed":
                p.failed += n
                listing = len(m.group("ind"))
            else:
                listing = None
            continue
        if listing is not None:
            if line.strip() and len(line) - len(line.lstrip()) > listing:
                p.ids.add(_norm_test_id(line))
            else:
                listing = None
    return p


def _parse_mocha(lines: list[str]) -> _Parsed:
    p = _Parsed()
    for k, line in enumerate(lines):
        if MOCHA_PASSING.match(line):
            p.summaries += 1
        m = MOCHA_FAILING.match(line)
        if not m:
            continue
        p.failed += int(m.group(1))
        j = k + 1
        while j < len(lines):
            im = MOCHA_ITEM.match(lines[j])
            if not im:
                j += 1
                continue
            parts, ind = [im.group("title").strip()], len(im.group("ind"))
            j += 1
            while not parts[-1].endswith(":") and j < len(lines) and lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) > ind:
                parts.append(lines[j].strip())
                j += 1
            title = " > ".join(parts).rstrip(":")
            if MOCHA_HOOK.search(title):
                raise _Unattributable(f"mocha hook failure: {title}")
            p.ids.add(_norm_test_id(title))
    return p


def _parse_go(lines: list[str]) -> _Parsed:
    p = _Parsed()
    pending: list[str] = []
    for line in lines:
        m = GO_FAIL_TEST.match(line)
        if m:
            pending.append(m.group("name"))
            continue
        m = GO_PKG.match(line)
        if not m:
            continue
        p.summaries += 1
        if m.group("st") == "FAIL":
            if not pending:
                raise _Unattributable(f"go test: package {m.group('pkg')} failed outside any test")
            leaves = [n for n in pending if not any(o.startswith(n + "/") for o in pending)]
            p.failed += len(leaves)
            p.ids |= {f"{m.group('pkg')} > {n}" for n in leaves}
        pending = []
    if pending:
        raise _Unattributable("go test: failing tests without their package result line")
    return p


def _parse_segment(lines: list[str]) -> _Parsed:
    total = _Parsed()
    for parse in (_parse_vitest, _parse_jest, _parse_pytest, _parse_playwright, _parse_mocha, _parse_go):
        p = parse(lines)
        if p.ids and not p.summaries:
            raise _Unattributable(f"{parse.__name__[7:]} failures without the runner's summary line (truncated output?)")
        if p.summaries and len(p.ids) != p.failed:
            raise _Unattributable(f"{parse.__name__[7:]} reports {p.failed} failure(s) but {len(p.ids)} failing test(s) could be named")
        total.ids |= p.ids
        total.summaries += p.summaries
        total.failed += p.failed
    # node lists only leaf failures, while its "fail" count also includes parents failing through a subtest
    for parse_node in (_parse_node_spec, _parse_node_tap):
        p = parse_node(lines)
        if p.ids and not p.summaries:
            raise _Unattributable("node --test failures without the runner's summary line (truncated output?)")
        if p.summaries and (len(p.ids) > p.failed or (p.failed and not p.ids)):
            raise _Unattributable(f"node --test reports {p.failed} failure(s) but {len(p.ids)} failing test(s) could be named")
        total.ids |= p.ids
        total.summaries += p.summaries
        total.failed += p.failed
    return total


def explain_test_failures(output: str) -> tuple[set[str] | None, str]:
    """parse_test_failures() plus a one-line reason (why None, or what was found)."""
    lines = _clean_lines(output)
    text = "\n".join(lines)
    for rx, why in UNATTRIBUTABLE:
        m = rx.search(text)
        if m:
            return None, f"{why} ({m.group(0).strip()[:60]!r})"
    try:
        segments, failed_pkgs = _split_workspace(lines)
        ids: set[str] = set()
        summaries = 0
        for pkg, seg in segments.items():
            p = _parse_segment(seg)
            summaries += p.summaries
            ids |= {f"[{pkg}] {i}" if pkg else i for i in p.ids}
            if pkg in failed_pkgs and not p.ids:
                raise _Unattributable(f"package {pkg} failed but no failing test could be named in its output")
        for pkg in failed_pkgs - set(segments):
            raise _Unattributable(f"package {pkg} failed but printed no output")
    except _Unattributable as e:
        return None, str(e)
    if not summaries:
        return None, "no test-runner summary recognised"
    if ids:
        return ids, f"{len(ids)} failing test(s)"
    if WRAPPER_FAILURE.search(text):
        return None, "the runners report no failing test but the output shows a failure"
    return set(), "clearly passing"


def parse_test_failures(output: str) -> set[str] | None:
    """Identifiers of the failing tests in a test run's output (stdout + stderr), or None when the failure cannot be
    attributed to individual tests with confidence. An empty set means clearly passing output.

    Ids are normalised so the same test gets the same id on base and branch: ANSI codes, durations, retry markers and
    result glyphs are dropped, playwright line:col is dropped, jest "›" becomes ">", and a turbo / pnpm -r workspace
    prefix ("@acme/web:test: ", "packages/web test: ") becomes a "[<package>] " qualifier."""
    return explain_test_failures(output)[0]


def baseline_from_result(res: CmdResult) -> set[str] | None:
    """Failing tests of a test run on the base branch: set() when it passed, None when unknown."""
    if res.ok:
        return set()
    if res.timed_out:
        return None
    return parse_test_failures((res.stdout or "") + "\n" + (res.stderr or ""))


# A pre-existing failure early in "a && b" (or a runner-level bail) stops later suites, hiding their regressions. Quotes
# are not stripped on purpose: "sh -c 'vitest run && pytest'" chains just the same.
CHAINED_COMMAND = re.compile(r"&&|\|\||;")
BAIL_FLAG = re.compile(r"(?:^|\s)(?:--bail\b|-x\b|--exitfirst\b|--maxfail\b|--max-failures\b|-failfast\b)")


def baseline_verdict(res: CmdResult, base_failures: set[str] | None) -> tuple[bool, str]:
    """(excused, detail) for a failing test run against the base branch's failing set."""
    if res.timed_out:
        return False, "baseline not applied: the run timed out"
    if base_failures is None:
        return False, "baseline not applied: the base branch's failing tests are unknown"
    if CHAINED_COMMAND.search(res.cmd):
        return False, "baseline not applied: the test command chains commands (&&, ||, ;), so an early pre-existing failure can stop later suites"
    if BAIL_FLAG.search(res.cmd):
        return False, "baseline not applied: the test command stops at the first failure, so later tests never ran"
    now, why = explain_test_failures((res.stdout or "") + "\n" + (res.stderr or ""))
    if now is None:
        return False, f"baseline not applied: {why}"
    if not now:
        return False, "baseline not applied: no failing test could be named although the command failed"
    new = sorted(now - base_failures)
    if new:
        old = len(now) - len(new)
        return False, f"{len(new)} new failing test(s) not failing on base{f' ({old} pre-existing)' if old else ''}: " + "; ".join(new[:10]) + (" …" if len(new) > 10 else "")
    names = sorted(now)
    return True, f"{len(names)} failing test(s), all already failing on base: " + "; ".join(names[:10]) + (" …" if len(names) > 10 else "")


@dataclass
class ToolingResult:
    results: dict[str, CmdResult] = field(default_factory=dict)
    retried: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    baseline_ok: dict[str, str] = field(default_factory=dict)  # kind -> why its failure is excused (pre-existing only)
    baseline_notes: dict[str, str] = field(default_factory=dict)  # kind -> why a baseline did NOT excuse the failure

    def passed(self, kind: str) -> bool:
        res = self.results.get(kind)
        return res is not None and (res.ok or kind in self.baseline_ok)

    def checks(self) -> list[Check]:
        out = []
        for kind, res in self.results.items():
            detail = f"`{res.cmd}` exit {res.returncode}{' (timed out)' if res.timed_out else ''} in {res.duration_s:.0f}s"
            if kind in self.retried:
                detail += " (re-run once to rule out flakiness)"
            if kind in self.baseline_ok:
                detail += "; " + self.baseline_ok[kind]
            elif not res.ok:
                if kind in self.baseline_notes:
                    detail += "\n" + self.baseline_notes[kind]
                detail += "\n" + res.tail(25)
            out.append(Check(f"{kind}-passes", self.passed(kind), detail))
            if kind in self.baseline_ok:
                # surfaced as a warning so a tolerated red suite is never silent
                out.append(Check(f"{kind}-preexisting-failures", False, self.baseline_ok[kind], warn_only=True))
        for kind in self.skipped:
            out.append(Check(f"{kind}-passes", True, f"no {kind} command configured (skipped)", warn_only=True))
        return out

    @property
    def ok(self) -> bool:
        return all(self.passed(k) for k in self.results)


def run_tooling(
    commands: dict[str, str | None],
    cwd: Path,
    runner: ToolRunner,
    timeout_s: float,
    env_note: Callable[[str], Any] | None = None,
    *,
    baseline: dict[str, set[str]] | None = None,
) -> ToolingResult:
    """Run typecheck/lint/test/build in that order. A failing test command is re-run once (flakiness), unless the
    first run already fails with only tests that fail on base: re-running a suite that is red on base gains nothing and
    doubles the longest stage. When ``baseline`` has a "test" entry (failing test ids on the base branch, see
    baseline_from_result) and the (re-)run fails with only tests that already fail on base, the test check passes and
    ``baseline_ok["test"]`` says why."""
    tr = ToolingResult()
    for kind in ("typecheck", "lint", "test", "build"):
        cmd = commands.get(kind)
        if not cmd:
            tr.skipped.append(kind)
            continue
        res = runner(cmd, cwd, timeout_s)
        if not res.ok and kind == "test" and not res.timed_out:
            excused = baseline is not None and kind in baseline and baseline_verdict(res, baseline[kind])[0]
            if not excused:
                tr.retried.append(kind)
                res = runner(cmd, cwd, timeout_s)
        tr.results[kind] = res
        if not res.ok and kind == "test" and baseline is not None and kind in baseline:
            excused, detail = baseline_verdict(res, baseline[kind])
            (tr.baseline_ok if excused else tr.baseline_notes)[kind] = detail
    return tr


def gate_implement(repo: Path, base: str, feature_dir: Path, tooling: ToolingResult | None, *, human_before: dict[str, bool] | None = None) -> GateResult:
    """``human_before`` maps each HUMAN-only task id to its done flag before implement started (e.g.
    ``{t.id: t.done for t in human_tasks(fd)}``); a task open then and ticked (or deleted) now fails the gate."""
    r = GateResult("implement")
    tasks = feature_tasks(feature_dir)
    agent = [t for t in tasks if not t.human]
    human = [t for t in tasks if t.human]
    done, total = sum(1 for t in agent if t.done), len(agent)
    r.facts["tasks_done"], r.facts["tasks_total"] = done, total
    human_open = [f"{t.id} {t.text[:80]}" for t in human if not t.done]
    r.facts["human_open"] = human_open
    excluded = f" ({len(human)} human-only task(s) not counted)" if human else ""
    r.add("all-tasks-checked", total > 0 and done == total, f"{done}/{total} agent tasks checked off in tasks.md{excluded}")
    if human:
        r.add("human-only-tasks", not human_open, f"{len(human_open)} human-only task(s) left for a person: {human_open[:8]}" if human_open else "every human-only task is ticked", warn_only=True)
    if human_before is not None:
        now = {t.id: t for t in tasks}
        ticked = [f"{i} {now[i].text[:80]}" for i, was_done in human_before.items() if not was_done and i in now and now[i].human and now[i].done]
        removed = [i for i, was_done in human_before.items() if not was_done and i not in now]
        bad = ticked + [f"{i} (removed from tasks.md)" for i in removed]
        r.add("human-tasks-untouched", not bad, f"agents must never tick human-only tasks; ticked or removed during implement: {bad[:8]}" if bad else "no human-only task was ticked or removed during implement")
    if tooling is not None:
        r.checks.extend(tooling.checks())
    g = Git(repo)
    committed = g.diff_names(f"origin/{base}" if g.ref_exists(f"refs/remotes/origin/{base}") else base)
    pending = g.uncommitted_names()
    code_changes = [p for p in committed + pending if not p.startswith("specs/") and not p.startswith(".autopilot/")]
    r.facts["changed_files"] = len(set(committed + pending))
    r.add("diff-not-empty", bool(code_changes), f"{len(set(code_changes))} changed file(s) outside specs/ vs {base}" if code_changes else f"no code changes vs {base} (only spec artifacts)")
    return r


# --------------------------------------------------------------------------------------
# ship
# --------------------------------------------------------------------------------------


@dataclass
class ShipFacts:
    branch: str = ""
    on_base: bool = False
    clean: bool = False
    base_synced: bool = False
    remote_branch: bool = False
    pr_number: int | None = None
    pr_state: str | None = None  # OPEN / MERGED / CLOSED
    pr_url: str | None = None
    merge_commit: str | None = None
    merged: bool = False
    detail: list[str] = field(default_factory=list)


def ship_facts(repo: Path, base: str, feature_branch: str, *, fetch: bool = True, gh_runner: Callable[..., CmdResult] = gh, start_sha: str | None = None) -> ShipFacts:
    """Ship state from git + gh. ``start_sha`` is the base commit the feature branch was cut from (recorded at
    feature_start): a tip that has not moved past it has nothing of its own, so ancestry must not count as merged."""
    g = Git(repo)
    f = ShipFacts()
    if fetch:
        g.fetch("origin")
    f.branch = g.current_branch()
    f.on_base = f.branch == base
    f.clean = g.is_clean()
    f.remote_branch = g.branch_exists_remote(feature_branch)
    ab = g.ahead_behind(base, f"origin/{base}") if g.ref_exists(base) else None
    f.base_synced = ab == (0, 0)
    pr = gh_runner(repo, "pr", "list", "--head", feature_branch, "--state", "all", "--json", "number,state,url,mergeCommit,baseRefName", "--limit", "5")
    if pr.ok and pr.stdout.strip():
        try:
            items = [p for p in json.loads(pr.stdout) if p.get("baseRefName") in (base, None)]
        except json.JSONDecodeError:
            items = []
        merged = [p for p in items if p.get("state") == "MERGED"]
        chosen = merged[0] if merged else (items[0] if items else None)
        if chosen:
            f.pr_number = chosen.get("number")
            f.pr_state = chosen.get("state")
            f.pr_url = chosen.get("url")
            mc = chosen.get("mergeCommit") or {}
            f.merge_commit = mc.get("oid") if isinstance(mc, dict) else None
    elif not pr.ok:
        f.detail.append(f"gh pr list failed: {pr.stderr.strip()[:200]}")
    # ancestry is the ground truth for merge commits / fast-forwards
    tip = g.head(f"refs/remotes/origin/{feature_branch}") if f.remote_branch else g.head(feature_branch) if g.ref_exists(feature_branch) else ""
    own_commits = True
    if tip and start_sha:
        own_commits = tip != start_sha and not g.is_ancestor(tip, start_sha)
    elif tip:
        # no recorded fork point: only trust ancestry if the tip entered base through a merge commit
        merges = g.out("rev-list", "--merges", "--parents", "-n", "2000", f"origin/{base}").splitlines() if g.ref_exists(f"refs/remotes/origin/{base}") else []
        own_commits = any(tip in line.split()[2:] for line in merges)
    if tip and own_commits and g.ref_exists(f"refs/remotes/origin/{base}"):
        if g.is_ancestor(tip, f"origin/{base}"):
            f.merged = True
    if f.pr_state == "MERGED":
        f.merged = True
    return f


def ci_state(repo: Path, sha: str, gh_runner: Callable[..., CmdResult] = gh) -> tuple[str, str]:
    """Return (state, detail): state in success|failure|pending|none."""
    r = gh_runner(repo, "run", "list", "--commit", sha, "--json", "name,status,conclusion,workflowName,databaseId", "--limit", "50")
    if not r.ok:
        return "pending", f"gh run list failed: {r.stderr.strip()[:200]}"
    try:
        runs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return "pending", "unparseable gh output"
    if not runs:
        return "none", "no workflow runs for this commit yet"
    pending = [x for x in runs if x.get("status") != "completed"]
    if pending:
        return "pending", f"{len(pending)} run(s) still in progress"
    bad = [x for x in runs if x.get("conclusion") not in ("success", "skipped", "neutral")]
    if bad:
        return "failure", "; ".join(f"{x.get('workflowName') or x.get('name')}: {x.get('conclusion')}" for x in bad[:5])
    return "success", f"{len(runs)} run(s) green"


def wait_for_ci(repo: Path, sha: str, timeout_s: float, poll_s: float = 30.0, gh_runner: Callable[..., CmdResult] = gh, sleep: Callable[[float], Any] = time.sleep) -> tuple[str, str]:
    deadline = time.monotonic() + timeout_s
    state, detail = ci_state(repo, sha, gh_runner)
    grace_none = 3  # runs may take a moment to register
    while state in ("pending", "none") and time.monotonic() < deadline:
        if state == "none":
            grace_none -= 1
            if grace_none < 0:
                break
        sleep(poll_s)
        state, detail = ci_state(repo, sha, gh_runner)
    if state == "pending":
        return "timeout", f"CI still running after {timeout_s / 60:.0f} min: {detail}"
    return state, detail


def gate_ship(facts: ShipFacts, base: str, *, ci: tuple[str, str] | None, ci_required: bool) -> GateResult:
    r = GateResult("ship")
    r.facts.update(facts.__dict__)
    r.add("on-base", facts.on_base, f"current branch {facts.branch!r} (expected {base!r})")
    r.add("clean-tree", facts.clean, "working tree clean" if facts.clean else "working tree has uncommitted changes")
    r.add("base-synced", facts.base_synced, f"local {base} == origin/{base}" if facts.base_synced else f"local {base} differs from origin/{base}")
    r.add("merged", facts.merged, f"feature branch merged into origin/{base}" + (f" via PR #{facts.pr_number}" if facts.pr_number else "") if facts.merged else "feature branch is NOT merged into origin/" + base)
    if ci_required:
        state, detail = ci if ci else ("unknown", "CI not checked")
        r.add("ci-green", state == "success", f"CI on merge commit: {state} — {detail}")
    else:
        r.add("ci-green", True, "CI not required for this repo (ci_required=false)", warn_only=True)
    return r


def ci_health(repo: Path, base: str, gh_runner: Callable[..., CmdResult] = gh) -> tuple[bool | None, str]:
    """Heuristic: is hosted CI functional? (False when every recent run failed within seconds, e.g. billing blocks)."""
    r = gh_runner(repo, "run", "list", "--branch", base, "--limit", "10", "--json", "conclusion,createdAt,updatedAt,status")
    if not r.ok:
        return None, f"gh run list failed: {r.stderr.strip()[:160]}"
    try:
        runs = [x for x in json.loads(r.stdout or "[]") if x.get("status") == "completed"]
    except json.JSONDecodeError:
        return None, "unparseable"
    if not runs:
        return None, "no completed runs on base"
    import datetime as _dt

    def dur(x: dict[str, Any]) -> float:
        try:
            a = _dt.datetime.fromisoformat(x["createdAt"].replace("Z", "+00:00"))
            b = _dt.datetime.fromisoformat(x["updatedAt"].replace("Z", "+00:00"))
            return (b - a).total_seconds()
        except Exception:
            return 999.0

    if any(x.get("conclusion") == "success" for x in runs):
        return True, f"{sum(1 for x in runs if x.get('conclusion') == 'success')}/{len(runs)} recent base runs succeeded"
    if all(x.get("conclusion") == "failure" and dur(x) < 60 for x in runs):
        return False, f"all {len(runs)} recent base runs failed within 60s (looks like Actions is not running jobs, e.g. a billing block)"
    return True, f"0/{len(runs)} recent base runs succeeded (CI runs but is red)"


# --------------------------------------------------------------------------------------
# Linear close-out (read back through MCP; data comes from raw tool results)
# --------------------------------------------------------------------------------------

STATUS_START = "<!-- autopilot:status -->"
STATUS_END = "<!-- /autopilot:status -->"
VISIBLE_START = "[autopilot:status]"
VISIBLE_END = "[/autopilot:status]"
COMMENT_MARKER = "Autopilot close-out"


def status_block(description: str) -> tuple[str | None, str]:
    """Return (block text or None, marker style)."""
    for a, b, style in ((STATUS_START, STATUS_END, "html"), (VISIBLE_START, VISIBLE_END, "visible")):
        i = description.find(a)
        j = description.find(b, i + len(a)) if i >= 0 else -1
        if i >= 0 and j > i:
            return description[i + len(a): j], style
    return None, ""


def gate_linear(issue: dict[str, Any] | None, comments: list[dict[str, Any]] | None, project: dict[str, Any] | None, today: str, issue_key: str, *, require_project: bool = False) -> GateResult:
    r = GateResult("linear")
    if issue is None:
        r.add("issue-read", False, f"could not read issue {issue_key} back through MCP")
        return r
    status = issue.get("status")
    st = issue.get("statusType") or (status.get("type") if isinstance(status, dict) else None)
    r.facts["issue_status"] = issue.get("status")
    r.add("issue-completed", st == "completed", f"{issue_key} state type is {st!r} (status {issue.get('status')!r})")
    ours = [c for c in (comments or []) if COMMENT_MARKER.lower() in str(c.get("body", "")).lower() and issue_key.lower() in str(c.get("body", "")).lower()]
    r.facts["autopilot_comments"] = len(ours)
    r.add("one-autopilot-comment", len(ours) == 1, f"{len(ours)} autopilot close-out comment(s) on {issue_key} (expected exactly 1)")
    if project is None:
        has_project = bool(issue.get("projectId") or issue.get("project"))
        if has_project or require_project:
            r.add("project-read", False, "the issue's project could not be read back through MCP")
        else:
            r.add("project-read", False, f"{issue_key} has no project, so there is no project overview to update (skipped)", warn_only=True)
        return r
    desc = str(project.get("description") or project.get("content") or "")
    block, style = status_block(desc)
    r.facts["status_marker_style"] = style
    r.add("status-block", block is not None, f"status block between {STATUS_START} and {STATUS_END} {'found' if block is not None else 'not found'} in the project overview" + (f" ({style} markers)" if style else ""))
    if block is not None:
        r.add("status-block-dated-today", today in block, f"status block {'is' if today in block else 'is NOT'} dated {today}")
    return r


def parse_mcp_json(payload: Any) -> Any:
    """MCP tool results arrive as text (JSON) or content blocks; return parsed JSON or None."""
    if payload is None:
        return None
    if isinstance(payload, (dict, list)) and not (isinstance(payload, list) and payload and isinstance(payload[0], dict) and payload[0].get("type") == "text"):
        return payload
    text = payload
    if isinstance(payload, list):
        text = "\n".join(str(b.get("text", "")) if isinstance(b, dict) else str(getattr(b, "text", b)) for b in payload)
    text = str(text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(text[s: e + 1])
            except json.JSONDecodeError:
                return None
    return None
