"""Phase contracts, session groups and the bounded routing graph (docs/DESIGN.md §3, §6)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import PHASES


@dataclass(frozen=True)
class PhaseSpec:
    name: str
    group: str  # session group: S1..S6
    command_key: str | None  # key into RepoProfile.commands
    dod: str  # judge Definition of Done (empty -> no judge verify)
    back_targets: tuple[str, ...] = ()
    fresh_verify: bool = False  # gate re-runs the command in a brand-new session (analyze, review)
    preconditions: tuple[str, ...] = field(default_factory=tuple)  # human-readable, enforced in runner


PHASE_SPECS: dict[str, PhaseSpec] = {
    "feature_start": PhaseSpec(
        "feature_start",
        "S1",
        "feature_start",
        "The spec (spec.md) covers the issue's intent (the Linear title + description when Linear is on, else the description) and invents no scope beyond it.",
        preconditions=("clean tree", "on base branch", "base up to date with origin", "constitution exists and is filled in", "run lock held"),
    ),
    "clarify": PhaseSpec(
        "clarify",
        "S1",
        "clarify",
        "Every clarification answer is written into the right spec section (not only the Clarifications log), and no two sections of spec.md contradict each other.",
        preconditions=("spec.md exists",),
    ),
    "plan": PhaseSpec(
        "plan",
        "S1",
        "plan",
        "plan.md satisfies every functional requirement in spec.md, and its technical choices fit the repository's existing stack (no new frameworks/languages without a recorded justification).",
        preconditions=("clarify passed",),
    ),
    "tasks": PhaseSpec(
        "tasks",
        "S1",
        "tasks",
        "tasks.md covers every functional requirement and user story in spec.md, and the dependency order of phases/tasks makes sense.",
        preconditions=("plan passed",),
    ),
    "analyze": PhaseSpec(
        "analyze",
        "S2",
        "analyze",
        "Every remaining (non-CRITICAL, non-HIGH) analyze finding was consciously accepted with a reason, or fixed.",
        back_targets=("clarify", "plan", "tasks"),
        fresh_verify=True,
        preconditions=("tasks passed",),
    ),
    "implement": PhaseSpec(
        "implement",
        "S3",
        "implement",
        "The code implements the behaviour described in spec.md (not just the task titles), and there are no TODO stubs or placeholder implementations for required behaviour.",
        preconditions=("analyze passed",),
    ),
    "review": PhaseSpec(
        "review",
        "S4",
        "review",
        "Every review finding was either fixed or consciously accepted with a reason; no must-fix finding remains.",
        back_targets=("implement", "tasks"),
        fresh_verify=True,
        preconditions=("implement passed",),
    ),
    "ship": PhaseSpec("ship", "S5", "ship", "", preconditions=("ship gate open",)),
    "linear": PhaseSpec(
        "linear",
        "S6",
        None,
        "The Linear close-out comment and the project status block accurately describe what actually shipped (merge commit / PR, the three summary bullets) and mention every high-impact or escalated decision.",
        preconditions=("ship passed",),
    ),
}

SESSION_GROUPS: dict[str, tuple[str, ...]] = {
    "S1": ("feature_start", "clarify", "plan", "tasks"),
    "S2": ("analyze",),
    "S3": ("implement",),
    "S4": ("review",),
    "S5": ("ship",),
    "S6": ("linear",),
}

# Reasons the judge may give for a backward move, used in prompts.
BACK_EDGE_MEANING: dict[tuple[str, str], str] = {
    ("analyze", "clarify"): "an ambiguity in spec.md that needs a clarification answer",
    ("analyze", "plan"): "an architecture / design gap in plan.md",
    ("analyze", "tasks"): "a coverage gap in tasks.md",
    ("review", "implement"): "a code defect",
    ("review", "tasks"): "missing work that no task covers",
}


def index(phase: str) -> int:
    return PHASES.index(phase)


def next_phase(phase: str) -> str | None:
    i = index(phase)
    return PHASES[i + 1] if i + 1 < len(PHASES) else None


def downstream(target: str, through: str) -> list[str]:
    """Phases to re-run after routing back to ``target`` from ``through`` (inclusive)."""
    return list(PHASES[index(target): index(through) + 1])


@dataclass
class RouteCheck:
    allowed: bool
    reason: str


def check_back_edge(source: str, target: str | None, back_edges: list[dict], max_back_edges: int, max_per_target: int) -> RouteCheck:
    spec = PHASE_SPECS[source]
    if not target or target not in spec.back_targets:
        allowed = ", ".join(spec.back_targets) or "none"
        return RouteCheck(False, f"{source} may only route back to: {allowed} (judge asked for {target!r})")
    if len(back_edges) >= max_back_edges:
        return RouteCheck(False, f"max_back_edges={max_back_edges} already used")
    same = sum(1 for b in back_edges if b.get("to") == target)
    if same >= max_per_target:
        return RouteCheck(False, f"already routed back to {target} {same} time(s) (limit {max_per_target})")
    return RouteCheck(True, "ok")
