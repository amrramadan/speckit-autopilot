"""speckit_autopilot.live: the status-line rows and the --watch view, built from throwaway run files."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from speckit_autopilot import live


def make_run(root: Path, name: str, *, status: str = "running", phase: str = "implement", alive: bool = True,
             until: str | None = "ship", halt: str = "", age_s: float = 0.0) -> Path:
    repo = root / name
    ap = repo / ".autopilot"
    ap.mkdir(parents=True)
    phases = {}
    for p in live.PHASES:
        if live.PHASES.index(p) < live.PHASES.index(phase):
            phases[p] = {"status": "passed", "attempts": 1, "repairs": 0, "cost_usd": 12.5}
        elif p == phase:
            phases[p] = {"status": "running", "attempts": 2, "repairs": 1, "cost_usd": 3.0}
    updated = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - age_s))
    updated = updated[:-2] + ":" + updated[-2:]
    (ap / "state.json").write_text(json.dumps({
        "issue": "ENG-9", "description": "SPEC-01 A feature", "status": status, "current_phase": phase,
        "phases": phases, "flags": {"spec_id": "SPEC-01", "until": until}, "elapsed_s": 600.0, "updated_at": updated,
        "halt": {"reason": halt} if halt else None, "escalations": [], "back_edges": [], "total_cost_usd": 130.91,
        "linear": {"title": "SPEC-01 — A feature"},
    }))
    (ap / "lock").write_text(json.dumps({"pid": os.getpid() if alive else 999999}))
    (ap / "run.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"ts": updated, "event": "phase_start", "phase": phase, "summary": f"{phase} attempt 2"},
        {"ts": updated, "event": "repair", "phase": phase, "summary": "applying approved edits (round 1)"},
        {"ts": updated, "event": "session_close", "phase": phase, "cost_usd": 4.2},
    ]) + "\n")
    return repo


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("SPECKIT_AUTOPILOT_ROOTS", str(tmp_path))
    monkeypatch.setenv("SPECKIT_AUTOPILOT_STATE", str(tmp_path / ".state"))
    return tmp_path


def plain(rows):
    return [live._ANSI.sub("", r) for r in rows]


def test_status_row_for_this_repo_and_the_others(ws):
    a = make_run(ws, "alpha")
    make_run(ws, "beta", phase="plan")
    rows = plain(live.statusline({"workspace": {"project_dir": str(a)}}, 200))
    assert rows[0].startswith("autopilot ENG-9 SPEC-01 │ implement (try 2, fix 1)")
    assert "5/7" in rows[0] and "● running" in rows[0] and "applying approved edits" in rows[0]
    assert rows[1].startswith("also: ") and "beta ENG-9 plan" in rows[1]
    assert not any("$" in r or "cost" in r.lower() for r in rows)


def test_workspace_root_lists_every_live_run(ws):
    make_run(ws, "alpha")
    make_run(ws, "beta", phase="plan")
    rows = plain(live.statusline({"workspace": {"project_dir": str(ws)}}, 200))
    assert len(rows) == 2 and all(r.startswith("autopilot ENG-9") for r in rows)


def test_dead_running_run_is_not_running_and_old_runs_are_hidden(ws):
    a = make_run(ws, "alpha", alive=False)
    make_run(ws, "old", status="completed", alive=False, age_s=2 * 24 * 3600)
    run = live.load_run(a)
    assert run.status == "stale" and not run.alive
    assert [r.repo.name for r in live.all_runs()] == ["alpha"]


def test_halted_run_shows_the_reason(ws):
    a = make_run(ws, "alpha", status="halted", alive=False, halt="analyze failed after 2 repairs")
    row = plain(live.statusline({"workspace": {"project_dir": str(a)}}, 200))[0]
    assert "✖ halted" in row and "analyze failed after 2 repairs" in row


def test_rows_are_clipped_to_the_width(ws):
    a = make_run(ws, "alpha")
    for row in live.statusline({"workspace": {"project_dir": str(a)}}, 50):
        assert live.visible_len(row) <= 50


def test_no_run_no_rows(ws):
    assert live.statusline({"workspace": {"project_dir": str(ws / "nothing")}}, 200) == []


def test_watch_view_has_the_phase_strip_and_no_costs(ws):
    make_run(ws, "alpha")
    frame = live._ANSI.sub("", live.watch_view(live.all_runs(), 200))
    assert "✔ plan" in frame and "▶ implement ×2" in frame and "· ship" in frame and "5/7 phases" in frame
    assert "$" not in frame and "130.91" not in frame


def test_statusline_main_never_fails(ws, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert live.main(["--statusline"]) == 0
    assert capsys.readouterr().out == ""
