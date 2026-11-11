"""phases.py: phase contracts, session groups and the bounded back-edge graph."""

from __future__ import annotations

import pytest

from speckit_autopilot.config import PHASES
from speckit_autopilot.phases import (
    BACK_EDGE_MEANING,
    PHASE_SPECS,
    SESSION_GROUPS,
    check_back_edge,
    downstream,
    index,
    next_phase,
)

ALLOWED = {
    "analyze": {"clarify", "plan", "tasks"},
    "review": {"implement", "tasks"},
}


# ---------------------------------------------------------------- static structure


def test_every_phase_has_a_spec_and_exactly_one_session_group():
    assert set(PHASE_SPECS) == set(PHASES)
    flat = [p for g in SESSION_GROUPS.values() for p in g]
    assert sorted(flat) == sorted(PHASES)
    assert len(flat) == len(set(flat))
    for g, members in SESSION_GROUPS.items():
        for p in members:
            assert PHASE_SPECS[p].group == g


def test_back_targets_are_upstream_and_documented():
    for src, spec in PHASE_SPECS.items():
        assert set(spec.back_targets) == ALLOWED.get(src, set())
        for t in spec.back_targets:
            assert index(t) < index(src), f"{src} -> {t} is not a backward edge"
            assert (src, t) in BACK_EDGE_MEANING
    for (src, t) in BACK_EDGE_MEANING:
        assert t in PHASE_SPECS[src].back_targets


def test_fresh_verify_only_for_analyze_and_review():
    assert {p for p, s in PHASE_SPECS.items() if s.fresh_verify} == {"analyze", "review"}


def test_ship_has_no_judge_dod_and_linear_has_no_command():
    assert PHASE_SPECS["ship"].dod == ""
    assert PHASE_SPECS["linear"].command_key is None
    for p in PHASES:
        if p not in ("ship",):
            assert PHASE_SPECS[p].dod, p


# ---------------------------------------------------------------- next_phase / downstream


def test_next_phase_walks_the_pipeline():
    walk = ["feature_start"]
    while (n := next_phase(walk[-1])) is not None:
        walk.append(n)
    assert tuple(walk) == PHASES
    assert next_phase("linear") is None


def test_next_phase_unknown_raises():
    with pytest.raises(ValueError):
        next_phase("deploy")


@pytest.mark.parametrize(
    "target,through,expect",
    [
        ("clarify", "analyze", ["clarify", "plan", "tasks", "analyze"]),
        ("plan", "analyze", ["plan", "tasks", "analyze"]),
        ("tasks", "analyze", ["tasks", "analyze"]),
        ("implement", "review", ["implement", "review"]),
        ("tasks", "review", ["tasks", "analyze", "implement", "review"]),
        ("ship", "ship", ["ship"]),
    ],
)
def test_downstream_is_inclusive(target, through, expect):
    assert downstream(target, through) == expect


# ---------------------------------------------------------------- check_back_edge


@pytest.mark.parametrize("src", PHASES)
@pytest.mark.parametrize("target", list(PHASES) + [None, "", "nonsense"])
def test_only_declared_targets_are_allowed(src, target):
    r = check_back_edge(src, target, [], max_back_edges=3, max_per_target=1)
    assert r.allowed is (target in ALLOWED.get(src, set()))
    if not r.allowed:
        assert "may only route back to" in r.reason
        assert repr(target) in r.reason


def test_disallowed_reason_lists_allowed_targets_or_none():
    r = check_back_edge("analyze", "implement", [], 3, 1)
    assert "clarify, plan, tasks" in r.reason
    r = check_back_edge("plan", "clarify", [], 3, 1)
    assert "none" in r.reason


def test_max_back_edges_is_a_global_cap():
    used = [{"from": "analyze", "to": "clarify"}, {"from": "analyze", "to": "plan"}, {"from": "review", "to": "implement"}]
    r = check_back_edge("review", "tasks", used, max_back_edges=3, max_per_target=1)
    assert not r.allowed and "max_back_edges=3" in r.reason
    # one more allowed when the cap is higher
    assert check_back_edge("review", "tasks", used, max_back_edges=4, max_per_target=1).allowed


def test_max_back_edges_zero_disables_routing():
    r = check_back_edge("analyze", "plan", [], max_back_edges=0, max_per_target=1)
    assert not r.allowed


def test_at_most_one_back_edge_per_target():
    used = [{"from": "analyze", "to": "plan", "reason": "gap"}]
    again = check_back_edge("analyze", "plan", used, max_back_edges=3, max_per_target=1)
    assert not again.allowed
    assert "already routed back to plan 1 time(s)" in again.reason
    # a different target is still fine
    assert check_back_edge("analyze", "tasks", used, 3, 1).allowed


def test_per_target_count_spans_sources():
    """tasks reached from analyze counts against tasks reached from review."""
    used = [{"from": "analyze", "to": "tasks"}]
    assert not check_back_edge("review", "tasks", used, 3, 1).allowed
    assert check_back_edge("review", "tasks", used, 3, 2).allowed


def test_allowed_result_reason():
    r = check_back_edge("review", "implement", [], 3, 1)
    assert r.allowed and r.reason == "ok"
