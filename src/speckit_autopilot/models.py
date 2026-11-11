"""Worker model / effort selection.

``model_mode = "fixed"`` (default): every worker session uses ``worker_model`` / ``worker_effort``, as before.

``model_mode = "auto"``: each phase starts from a per-phase table (built-in defaults below, overridden key by key by
``phase_models`` / ``phase_efforts``), then

* every repair round of the phase climbs one step (``auto_max_escalations`` steps at most), alternating: first the
  effort moves one level up ``auto_efforts``, then the model one place up ``auto_models`` (a ladder already at its
  top hands the step to the other);
* after a failed Definition-of-Done check the judge may recommend the model / effort for the next attempt
  (``next_worker`` in its verify reply). A recommendation inside ``auto_models`` / ``auto_efforts`` replaces the ladder
  for the rest of that phase; anything else is ignored and logged.

The runner opens a fresh worker session (with the usual hand-off brief) whenever the choice for the phase differs from
the live session's, and logs each change as a ``model_switch`` event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import PHASES, Config

EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
MODEL_MODES: tuple[str, ...] = ("fixed", "auto")

# Cheap phases (mechanical commands, structured edits) run on the fast model; design, analysis, code and review on the
# strongest one.
DEFAULT_PHASE_MODELS: dict[str, str] = {
    "feature_start": "sonnet",
    "clarify": "sonnet",
    "plan": "opus",
    "tasks": "sonnet",
    "analyze": "opus",
    "implement": "opus",
    "review": "opus",
    "ship": "sonnet",
    "linear": "sonnet",
}
DEFAULT_PHASE_EFFORTS: dict[str, str] = {
    "feature_start": "medium",
    "clarify": "medium",
    "plan": "high",
    "tasks": "medium",
    "analyze": "high",
    "implement": "high",
    "review": "high",
    "ship": "medium",
    "linear": "low",
}


@dataclass(frozen=True)
class WorkerChoice:
    model: str | None
    effort: str | None
    reason: str

    @property
    def key(self) -> tuple[str | None, str | None]:
        return (self.model, self.effort)

    def label(self) -> str:
        return f"{self.model or 'default'}/{self.effort or 'default'}"


def phase_base(cfg: Config, phase: str) -> tuple[str | None, str | None]:
    model = cfg.phase_models.get(phase, DEFAULT_PHASE_MODELS.get(phase, cfg.worker_model))
    effort = cfg.phase_efforts.get(phase, DEFAULT_PHASE_EFFORTS.get(phase, cfg.worker_effort))
    return model, effort


def _effort_rank(e: str | None) -> int:
    return EFFORT_LEVELS.index(e) if e in EFFORT_LEVELS else -1


def escalate(cfg: Config, model: str | None, effort: str | None, steps: int) -> tuple[str | None, str | None]:
    """Climb ``steps`` rungs, alternating: odd steps raise the effort one level within ``auto_efforts``, even steps
    move the model one place up ``auto_models``; a ladder already at its top hands the step to the other one."""
    efforts = list(cfg.auto_efforts)
    models = list(cfg.auto_models)

    def effort_up(e: str | None) -> str | None:
        higher = [x for x in efforts if _effort_rank(x) > _effort_rank(e)]
        return min(higher, key=_effort_rank) if higher else None

    def model_up(m: str | None) -> str | None:
        return models[models.index(m) + 1] if m in models and models.index(m) < len(models) - 1 else None

    for n in range(1, max(0, steps) + 1):
        order = ("effort", "model") if n % 2 else ("model", "effort")
        for which in order:
            nxt = effort_up(effort) if which == "effort" else model_up(model)
            if nxt is not None:
                if which == "effort":
                    effort = nxt
                else:
                    model = nxt
                break
        else:
            break  # top of both ladders
    return model, effort


def choose(cfg: Config, phase: str, repairs: int = 0, recommendation: dict[str, Any] | None = None) -> WorkerChoice:
    """The worker model / effort for a session of ``phase`` opened in repair round ``repairs``."""
    base = phase.split(":")[0]
    if cfg.model_mode != "auto":
        return WorkerChoice(cfg.worker_model, cfg.worker_effort, "fixed")
    if recommendation:
        return WorkerChoice(recommendation.get("model"), recommendation.get("effort"), "judge: " + str(recommendation.get("reason") or "recommended"))
    model, effort = phase_base(cfg, base)
    steps = min(repairs, cfg.auto_max_escalations)
    if steps:
        m2, e2 = escalate(cfg, model, effort, steps)
        if (m2, e2) != (model, effort):
            return WorkerChoice(m2, e2, f"repair round {repairs}: escalated {steps} step(s) from {model}/{effort}")
    return WorkerChoice(model, effort, f"{base} default")


def accept_recommendation(cfg: Config, current: WorkerChoice, rec: Any) -> tuple[dict[str, Any] | None, str]:
    """Validate the judge's ``next_worker``. Returns (recommendation to store or None, note for the log)."""
    if cfg.model_mode != "auto" or not isinstance(rec, dict):
        return None, ""
    model = rec.get("model") or current.model
    effort = rec.get("effort") or current.effort
    if model not in cfg.auto_models:
        return None, f"ignored: model {model!r} is not in auto_models {list(cfg.auto_models)}"
    if effort not in cfg.auto_efforts:
        return None, f"ignored: effort {effort!r} is not in auto_efforts {list(cfg.auto_efforts)}"
    if (model, effort) == current.key:
        return None, "same as current"
    return {"model": model, "effort": effort, "reason": str(rec.get("reason") or "")[:200]}, "accepted"


def validate_model_config(cfg: Config, phases: tuple[str, ...]) -> list[str]:
    """Problems with the model_mode settings (config.validate turns them into ConfigError)."""
    errs: list[str] = []
    if cfg.model_mode not in MODEL_MODES:
        errs.append(f"model_mode must be one of {', '.join(MODEL_MODES)} (got {cfg.model_mode!r})")
    for name in ("phase_models", "phase_efforts"):
        table = getattr(cfg, name)
        for k, v in table.items():
            if k not in phases:
                errs.append(f"{name}: unknown phase {k!r}")
            if not isinstance(v, str) or not v:
                errs.append(f"{name}.{k} must be a non-empty string")
    for k, v in cfg.phase_efforts.items():
        if isinstance(v, str) and v not in EFFORT_LEVELS:
            errs.append(f"phase_efforts.{k} must be one of {', '.join(EFFORT_LEVELS)} (got {v!r})")
    if not cfg.auto_models:
        errs.append("auto_models may not be empty")
    if not cfg.auto_efforts:
        errs.append("auto_efforts may not be empty")
    bad = [e for e in cfg.auto_efforts if e not in EFFORT_LEVELS]
    if bad:
        errs.append(f"auto_efforts: unknown level(s) {bad} (use {', '.join(EFFORT_LEVELS)})")
    if cfg.auto_max_escalations < 0:
        errs.append("auto_max_escalations must be >= 0")
    return errs
