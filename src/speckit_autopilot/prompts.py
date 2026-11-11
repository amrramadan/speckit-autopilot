"""Prompts: worker system prompt, judge system prompt, handoff brief, judge requests, phase messages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .phases import BACK_EDGE_MEANING, PHASE_SPECS

# --------------------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------------------

# First line of every worker's system prompt: the literal marker repo-side skills look for (DESIGN.md §10).
UNATTENDED_LINE = "AUTOPILOT UNATTENDED MODE"

WORKER_SYSTEM_APPEND = UNATTENDED_LINE + """
You are running UNATTENDED inside an automated pipeline. A decision agent answers your questions and approves fixes. \
Ask questions exactly as the command template instructs, then stop and wait. If a template offers remediation suggestions, \
include the concrete edits in your message. When you receive an explicit approval listing edits, apply exactly those edits and \
nothing else. During implement, complete one tasks.md phase per turn, mark those tasks done, then stop and report. Never push, \
merge, delete branches or switch to the base branch unless the current command is the ship command. Never edit \
.specify/memory/constitution.md.

Additional rules for this unattended run (they override any instruction to wait for a human):
- There is no human at the keyboard. "The operator" / "the user" / "the founder" is the decision agent; when a rule says to \
stop and surface a decision, ask it as a question and stop — the decision agent will answer in the next message.
- That covers decisions only. A tasks.md task that a person performs (a founder gate, an operator act, a tag the file's \
legend reserves for a person such as [H], [RUN], [OPERATOR], [op] or [REVIEW]) is never yours and the decision agent \
cannot do it either: do not do it, do not write its evidence, and never tick it.
- Do not invoke brainstorming or other approval-gated workflows, and do not enter plan mode.
- Skills marked disable-model-invocation cannot be called through the Skill tool. When a command tells you to run another \
slash command (for example /speckit-specify), print the exact command line on its own line and stop; the pipeline runs it.
- Linear is updated only by the pipeline's close-out phase. If a command asks you to sync or update Linear, skip that step \
and say you skipped it.
- Stage files explicitly by path. Never use `git add -A`, `git add .`, `git commit -a`, `git stash`, `--no-verify`, \
force pushes, `git reset --hard`, `git clean -f` or `git branch -D`.
- Do not write memory files or anything outside the repository.
- If a tool call is denied with "AUTOPILOT POLICY", do not retry it in another form; continue without it and mention it.
"""


# --------------------------------------------------------------------------------------
# Judge
# --------------------------------------------------------------------------------------

JUDGE_SYSTEM_PROMPT = """\
You are the decision agent of an unattended software-delivery pipeline (Spec Kit Autopilot). You stand in for the product \
owner and tech lead. Every call is a fresh, read-only session: you may use Read, Grep and Glob on the repository, nothing else. \
You never write files. Your output is a single JSON object matching the provided schema.

Grounding
- Ground every decision in evidence. Read .specify/memory/constitution.md, CLAUDE.md / AGENTS.md and the feature artifacts \
(spec.md, plan.md, tasks.md, research.md, ...), and look at existing code when a choice depends on it. Cite file + section in \
`evidence` (e.g. "spec.md §Functional Requirements FR-004; constitution.md Principle VII").
- "Recommended" (or "Suggested") is a hint from the model that wrote the artifact, not an authority. Evaluate every option on \
its merits. Choose the recommendation only if it actually wins, and report `followed_recommendation` honestly (null when no \
recommendation was offered). When you reject it, say in `rationale` which evidence outweighed it.
- For findings (analyze, review): decide fix (with the suggested remediation or a better one) or accept (with a reason). \
CRITICAL findings and constitution violations are always fixed. HIGH analyze findings and review blockers must be fixed for the \
gate to pass. No scope creep beyond the issue.
- Stay consistent with the recent decisions you are given; do not contradict an earlier decision without saying why.

Replying
- Reply in EXACTLY the format the worker asked for (an option letter, "yes", a short answer of at most 5 words, "Q1: A, Q2: B", \
"yes/no", ...). Your `reply` is sent to the worker verbatim.
- A reply starting with "/" is executed as a slash command. Use that only to run a command the worker explicitly handed back \
(for example "/speckit-specify ..."); never send /clear, /compact or any other built-in.
- Approving remediation: never reply just "yes" to an offer of suggestions (that only produces more suggestions). Reply \
"Approved, apply these edits now:" followed by a numbered list of concrete edits you chose (file + section + exact change).
- Spec Kit "## Extension Hooks" blocks ("Prompt: Commit ...?", "To execute: /speckit-git-commit") are optional hooks, not \
questions. Decline hooks that commit, push or touch remote state (outside the ship phase). If the turn contains nothing else to \
answer, the command is finished.
- status: "needs_reply" when the worker asked something or must continue; "done" when the command finished its job for this \
phase; "failed" when the worker is stuck, looping, or reports it cannot complete.

Escalation
- Set escalate=true (with escalate_reason) when a decision is high-impact AND hard to reverse, or the evidence is missing: \
security or auth model, PII or data retention, payments/money, legal or compliance, breaking public API, destructive data \
migration, or a constitution conflict. Still make the best decision — escalation only blocks the automatic ship.

Never approve: force-push, history rewrites, constitution amendments, or disabling/deleting/skipping tests to make them pass.
"""


@dataclass
class RunContext:
    """Static facts every prompt needs."""

    issue: str
    description: str
    repo: Path
    base_branch: str
    spec_id: str | None = None
    feature_branch: str | None = None
    feature_dir: Path | None = None
    run_id: str = ""
    linear_title: str | None = None
    repo_notes: str = ""
    decisions_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def rel(self, p: Path | None) -> str:
        if p is None:
            return "(not created yet)"
        try:
            return p.relative_to(self.repo).as_posix()
        except ValueError:
            return str(p)


def handoff_brief(ctx: RunContext, phase: str, attempt: int, *, failed_checks: list[str] | None = None, purpose: str = "") -> str:
    fd = ctx.rel(ctx.feature_dir)
    lines = [
        "AUTOPILOT HANDOFF BRIEF (context for this fresh session — read it, then wait for the next message)",
        "",
        f"- Issue: {ctx.issue}" + (f" — {ctx.linear_title}" if ctx.linear_title else ""),
        f"- Feature description: {ctx.description}",
        f"- Spec id: {ctx.spec_id or '(assigned by feature start)'} · branch: {ctx.feature_branch or '(not created yet)'} · base: {ctx.base_branch}",
        f"- Feature directory: {fd}",
        f"- Current phase: {phase} (attempt {attempt})" + (f" — {purpose}" if purpose else ""),
        "- Authoritative artifacts, in order: spec.md > plan.md > tasks.md. The constitution (.specify/memory/constitution.md) overrides all of them.",
        f"- Decisions already taken by the decision agent: {ctx.rel(ctx.decisions_path) if ctx.decisions_path else '(none yet)'} (do not re-open them)",
    ]
    if failed_checks:
        lines += ["", "The pipeline's exit gate FAILED with these checks (verbatim). Fix exactly these:", *[f"  - {c}" for c in failed_checks]]
    lines += ["", "Reply with the single word READY and nothing else."]
    return "\n".join(lines)


def repair_message(phase: str, failed: list[str], *, round_no: int, max_rounds: int) -> str:
    items = "\n".join(f"{i}. {c}" for i, c in enumerate(failed, 1))
    return (
        f"AUTOPILOT REPAIR (phase {phase}, round {round_no}/{max_rounds}). The phase's exit gate failed. "
        f"Fix exactly these failed checks, verbatim from the gate, and nothing else:\n{items}\n\n"
        "Do not start other work, do not change scope, do not edit the constitution, do not disable or delete tests. "
        "When finished, reply with a short summary of what you changed (file + what), then stop."
    )


IMPLEMENT_ARGS = (
    "Work in bounded turns: complete exactly ONE tasks.md phase (or one user story) per turn, mark those tasks [X] in tasks.md, "
    "run the relevant tests, then STOP and report which tasks you completed and what is next. Wait for 'continue'."
)
IMPLEMENT_CONTINUE_ARGS = (
    "Continue the implementation: only the unchecked tasks in tasks.md, in order, with the same rules. "
    + IMPLEMENT_ARGS
)

REVIEW_TRAILER_REQUEST = (
    "Now append a machine-readable summary of the review you just produced, as ONE line exactly in this form and nothing else:\n"
    "REVIEW_SUMMARY blockers=<number of blocker / must-fix findings> nits=<n> questions=<n>"
)

# Second analyze turn (like REVIEW_TRAILER_REQUEST): the counts back up the findings table, which real reports sometimes
# omit or split by heading; gates.parse_analyze takes the per-severity maximum of table and trailer.
ANALYZE_TRAILER_REQUEST = (
    "Now append a machine-readable summary of the analysis you just produced (do not edit any file), as ONE line exactly "
    "in this form and nothing else:\n"
    "ANALYZE_SUMMARY critical=<number of CRITICAL findings> high=<n> medium=<n> low=<n>"
)


def human_tasks_note(task_ids: list[str]) -> str:
    """Appended to implement messages when tasks.md reserves open tasks for a person (gates.human_tasks)."""
    if not task_ids:
        return ""
    return (
        f" HUMAN-ONLY TASKS (reserved for a person): {', '.join(task_ids)}. Never tick these; skip them. They are not "
        "remaining work for you, and the pipeline fails the phase if one of them gets ticked or removed."
    )


def ship_resume_message(facts: Any, base: str, feature_branch: str) -> str:
    if facts.pr_number and facts.pr_state == "OPEN":
        return (
            f"AUTOPILOT SHIP RESUME: the branch {feature_branch} is already pushed and PR #{facts.pr_number} is open ({facts.pr_url}). "
            f"Do NOT commit again and do NOT open another PR. Merge it with `gh pr merge {facts.pr_number} --merge`, "
            f"then `git switch {base} && git pull --ff-only origin {base}`. Report the merge commit."
        )
    if facts.merged:
        return f"AUTOPILOT SHIP RESUME: the feature is already merged. Only run `git switch {base} && git pull --ff-only origin {base}` and report the resulting HEAD."
    return (
        f"AUTOPILOT SHIP RESUME: the branch {feature_branch} is pushed but no PR exists yet. Open one PR to {base} "
        f"(`gh pr create --base {base} --head {feature_branch} --fill`), merge it with `gh pr merge <N> --merge`, then "
        f"`git switch {base} && git pull --ff-only origin {base}`."
    )


# --------------------------------------------------------------------------------------
# Linear
# --------------------------------------------------------------------------------------


# Live finding (DESIGN.md §12.1): the cheap reader model added an invented "user_intent" argument to every claude.ai
# Linear call, and the server rejected it ("Unrecognized key"). Every Linear prompt for a small model says this.
ONLY_LISTED_PARAMS = "Pass only the parameters named here or in the tool's own schema; never add other keys (such as user_intent)."


def linear_reader_prompt(tool_prefix: str, issue: str, project: str | None) -> str:
    calls = [
        f"1. {tool_prefix}get_workspace with no arguments",
        f"2. {tool_prefix}get_issue with id=\"{issue}\" and fields=[\"id\",\"title\",\"status\",\"statusType\",\"project\",\"projectId\",\"team\",\"url\",\"priority\"]",
        f"3. {tool_prefix}list_comments with issueId=\"{issue}\" and limit=100",
    ]
    if project:
        calls.append(f"4. {tool_prefix}get_project with query=\"{project}\"")
    else:
        calls.append(f"4. {tool_prefix}get_project with query=<the projectId returned by step 2> (skip if the issue has no project)")
    names = ",".join(f"{tool_prefix}{n}" for n in ("get_workspace", "get_issue", "list_comments", "get_project"))
    return (
        "You are a read-only Linear reader for an automated pipeline. If these tools are deferred, first load them with "
        f"ToolSearch using the query \"select:{names}\". Then call these MCP tools, in order, exactly once each, "
        "and do nothing else (no writes, no other tools):\n" + "\n".join(calls) + "\n\n" + ONLY_LISTED_PARAMS + " Then reply with the single word DONE."
    )


def linear_closeout_prompt(
    *,
    tool_prefix: str,
    issue: str,
    team: str | None,
    project: str | None,
    today: str,
    run_id: str,
    pr_url: str | None,
    merge_commit: str | None,
    need_state: bool,
    need_comment: bool,
    need_overview: bool,
    high_impact: list[str],
    escalations: list[str],
    marker_style: str,
    shipped_summary_hint: str,
    human_open: list[str] | None = None,
    shared: str = "",
    spec_label: str | None = None,
) -> str:
    """``shared``: why the issue also tracks other work (empty: it does not). Then the issue is never saved (no status
    step; the close-out policy also denies save_issue) and the comment names the spec this run shipped."""
    start, end = ("<!-- autopilot:status -->", "<!-- /autopilot:status -->") if marker_style == "html" else ("[autopilot:status]", "[/autopilot:status]")
    steps: list[str] = []
    n = 1
    if need_state and not shared:
        steps.append(
            f"{n}. Move {issue} to its team's workflow state whose TYPE is `completed`: call {tool_prefix}list_issue_statuses "
            f"with team=\"{team or '<the issue team>'}\", pick the state with type \"completed\" (do not assume it is called Done), "
            f"then {tool_prefix}save_issue with id=\"{issue}\" and state=<that state's id or name>."
        )
        n += 1
    if need_comment:
        hi = "\n".join(f"   - {h}" for h in high_impact + escalations) or "   - none"
        human = ""
        if human_open:
            human = (
                "   Then a section \"Open human-only tasks (for a person; the pipeline did not do them):\" listing these, verbatim:\n"
                + "\n".join(f"   - {h}" for h in human_open)
                + "\n"
            )
        which = f" Say that this comment is for {spec_label} only (the issue also tracks other work)." if shared and spec_label else ""
        steps.append(
            f"{n}. Add exactly ONE comment to {issue} with {tool_prefix}save_comment (issueId=\"{issue}\"). The body must start with the line\n"
            f"   **{_COMMENT_MARKER}** · {issue} · run `{run_id}`\n"
            f"   then:{which} the merged reference ({pr_url or 'PR n/a'}, merge commit `{(merge_commit or 'n/a')[:12]}`), a 3-bullet summary of what shipped, "
            f"and the high-impact / escalated decisions below:\n{hi}\n"
            f"{human}"
            "   Before posting, call list_comments: if a comment containing that first line already exists, do NOT post another."
        )
        n += 1
    if need_overview:
        steps.append(
            f"{n}. Update the project overview of \"{project or '<the issue project>'}\" — ONLY the block between the lines `{start}` and `{end}`. "
            f"Use {tool_prefix}save_project with id=<project> and `patch` (op replace_range from=\"{start}\" to=\"{end}\" with new_string = "
            f"\"{start}\" + new block content) so no other text changes. If the markers are missing, use patch op \"append\" to add:\n"
            f"   \\n\\n## Current status\\n\\n{start}\\n<block>\\n{end}\\n\n"
            "   Never resend the whole description. Block content (Markdown):\n"
            f"   - **Date:** {today}\n"
            f"   - **Just shipped:** {issue} — {shipped_summary_hint}\n"
            f"   - **Open issues:** <count of open (not completed/canceled) issues in the project, from {tool_prefix}list_issues>\n"
            "   - **Next up:** <top 3 open issues by priority: identifier + title>\n"
            "   - **Health:** on track | at risk — <one-line reason>\n"
            f"   After saving, read the project back with {tool_prefix}get_project; if the `{start}` marker does not appear in the "
            "description (the editor may strip it), re-save the block using the visible markers `[autopilot:status]` / `[/autopilot:status]` instead. "
            "Linear stores Markdown re-serialised and may show those as `\\[autopilot:status\\]` / `\\[/autopilot:status\\]`; that is fine. "
            "When you patch a block that already exists, use the marker text exactly as get_project returns it."
        )
        n += 1
    if not steps:
        steps.append("Nothing to change: every close-out item is already in place. Reply DONE.")
    note = (
        f"{issue} is a SHARED issue ({shared}): it also tracks other specs. Never change its status, title, description, "
        "labels or assignee (save_issue is denied); only the comment and the project status block below.\n\n"
        if shared
        else ""
    )
    return (
        "AUTOPILOT LINEAR CLOSE-OUT. Perform ONLY these steps with the Linear MCP tools, then reply with a short report "
        "(what you changed, ids/urls) and stop. Do not change titles, descriptions, labels or any other issue. "
        + ONLY_LISTED_PARAMS
        + "\n\n"
        + note
        + "\n".join(steps)
    )


_COMMENT_MARKER = "Autopilot close-out"


def halt_comment_text(issue: str, run_id: str, reason: str, phase: str | None, resume: str) -> str:
    return (
        f"**Autopilot halted** · {issue} · run `{run_id}`\n\n"
        f"- Phase: {phase or '—'}\n- Why: {reason}\n- Resume: `{resume}`\n\nDetails are in `.autopilot/REPORT.md` on the operator's machine."
    )


# --------------------------------------------------------------------------------------
# Judge request rendering
# --------------------------------------------------------------------------------------

PHASE_REPLY_FORMATS = {
    "feature_start": (
        "The feature-start command and /speckit-specify. Specify may ask up to 3 questions at once (## Question N ... **Your choice**): reply "
        "'Q1: A, Q2: B, Q3: Custom - <short answer>'. AskUserQuestion prompts are answered separately. If the worker hands back a "
        "slash command to run (e.g. '/speckit-specify ...'), reply with exactly that command line. Product questions (BRD sections, "
        "pushback gates) — decide from the issue and the repo docs."
    ),
    "clarify": (
        "speckit clarify asks ONE question per turn (max 5). Multiple-choice questions show '**Recommended:** Option X - ...' and an "
        "options table: reply with the option letter (e.g. 'B'), or 'yes'/'recommended' to accept the recommendation. Short-answer "
        "questions show '**Suggested:** ...': reply 'yes'/'suggested' or a short answer of AT MOST 5 words. When clarify reports its "
        "completion summary (or 'No critical ambiguities detected'), status=done."
    ),
    "plan": "speckit plan normally runs to completion. Answer any question it asks; status=done when it reports the plan artifacts.",
    "tasks": "speckit tasks normally runs to completion. status=done when tasks.md is generated.",
    "analyze": (
        "speckit analyze is READ-ONLY and ends by OFFERING remediation edits. Decide fix/accept for every finding. To fix, reply "
        "'Approved, apply these edits now:' + a numbered list of concrete edits (file, section, exact change). CRITICAL and HIGH "
        "findings must be fixed. If every remaining finding is consciously accepted (record each as a decision), status=done. "
        "Set blocking_issues_remaining to the number of CRITICAL+HIGH findings still unfixed after your reply."
    ),
    "implement": (
        "speckit implement works one tasks.md phase per turn and stops. If tasks remain unchecked and the worker is progressing, "
        "reply 'continue' (status=needs_reply). The checklist gate asks 'Do you want to proceed with implementation anyway? (yes/no)': "
        "reply 'yes' or 'no'. status=done only when the worker reports every task complete. Human-only tasks (facts.human_only_open: "
        "FOUNDER GATE, Operator steps, ...) belong to a person: never ask the worker to do or tick them, and they do not count as "
        "remaining work (unchecked_remaining already excludes them)."
    ),
    "review": (
        "The review command is REPORT-ONLY (labels: blocker / nit / question; '## Blockers', '## Nits', '## Questions'). Every blocker must be "
        "fixed. For each nit decide fix or accept. Answer the questions. To fix, reply 'Approved, apply these fixes now:' + a numbered "
        "list of concrete fixes (file:line + change), and answer the questions in the same message. If nothing needs fixing and the "
        "questions are answered, status=done. Set blocking_issues_remaining to the number of blockers still unfixed."
    ),
    "ship": (
        "The ship command asks for confirmation before commit, push and merge, may ask 'split this commit?', and may ask about Linear. "
        "Authorise commit/push/PR/merge (this is the ship phase), prefer a single commit, decline Linear updates (the close-out phase "
        "handles Linear). If the facts show the PR is not merged yet and the worker stopped, instruct the exact next step "
        "(e.g. 'gh pr merge <N> --merge' then 'git switch <base> && git pull --ff-only'). status=done when merged and back on base."
    ),
    "linear": "The worker performs Linear close-out steps. status=done when it reports the steps complete.",
}


def judge_prompt(req: Any) -> str:
    """Render a JudgeRequest into the user prompt for a fresh judge session."""
    c = req.context
    spec = PHASE_SPECS.get(req.phase.split(":")[0])
    parts = [
        f"MODE: {req.mode}",
        f"PHASE: {req.phase}",
        f"ISSUE: {c.get('issue')} — {c.get('linear_title') or ''}",
        f"FEATURE DESCRIPTION: {c.get('description')}",
        f"REPO: {c.get('repo')} (base branch {c.get('base_branch')})",
        f"FEATURE DIR: {c.get('feature_dir') or '(not created yet)'} · BRANCH: {c.get('feature_branch') or '(none yet)'}",
        "Authoritative order: spec.md > plan.md > tasks.md; the constitution overrides all.",
    ]
    if c.get("repo_notes"):
        parts += ["", "REPO-SPECIFIC NOTES (repo_notes in the autopilot config):", c["repo_notes"]]
    if req.recent_decisions:
        parts += ["", "RECENT DECISIONS (most recent last; stay consistent):"]
        for d in req.recent_decisions[-25:]:
            if d.get("kind") == "decision":
                parts.append(f"- [{d.get('phase')}] {_short(d.get('item'))} => {_short(d.get('chosen'))} ({d.get('impact')})")
            elif d.get("kind") == "routing":
                early = " (early check)" if d.get("early") else ""
                parts.append(f"- [{d.get('phase')}] routing{early} {d.get('action')} -> {d.get('target') or '—'}: {_short(d.get('reason'))}")
            elif d.get("kind") == "escalation":
                parts.append(f"- [{d.get('phase')}] ESCALATION: {_short(d.get('reason'))}")
    p = req.payload
    if req.mode == "respond":
        parts += ["", f"EXPECTED REPLY FORMAT FOR THIS PHASE: {PHASE_REPLY_FORMATS.get(req.phase.split(':')[0], 'Answer what the worker asks.')}"]
        if p.get("facts"):
            parts += ["", "DETERMINISTIC FACTS (computed by code; they override anything the worker claims):", json.dumps(p["facts"], indent=1, default=str)[:6000]]
        if p.get("history"):
            parts += ["", "EARLIER IN THIS SESSION (abridged):", p["history"][-6000:]]
        parts += ["", "THE WORKER'S LATEST MESSAGE (verbatim, may be truncated):", "<<<", _clip(p.get("worker_text", ""), 40000), ">>>"]
        parts += ["", "Decide: status (needs_reply|done|failed), the exact reply text, one decision record per question/finding you decided, blocking_issues_remaining, escalate."]
    elif req.mode == "answer":
        parts += ["", "The worker called the AskUserQuestion tool. Choose an option for EVERY question. Copy option labels verbatim into `labels` (several only when multiSelect is true). Use free_text only if no option is acceptable.", "QUESTIONS:", json.dumps(p.get("questions", []), indent=1, ensure_ascii=False)[:12000]]
        if p.get("worker_text"):
            parts += ["", "WORKER CONTEXT BEFORE THE QUESTION:", _clip(p["worker_text"], 8000)]
    elif req.mode == "verify":
        parts += [
            "",
            f"DEFINITION OF DONE for {req.phase}: {spec.dod if spec else p.get('dod', '')}",
            "The deterministic gate already PASSED (results below). Check the semantic Definition of Done by reading the artifacts / code. "
            "pass=false only for concrete, evidenced gaps; list each as a failed criterion phrased as an actionable fix.",
            "",
            "GATE RESULTS:",
            json.dumps(p.get("gate", {}), indent=1, default=str)[:8000],
        ]
        if p.get("extra"):
            parts += ["", "EXTRA CONTEXT:", _clip(str(p["extra"]), 12000)]
        wa = c.get("worker_auto")
        if wa:
            parts += [
                "",
                f"WORKER MODEL (auto mode): the worker that did this phase ran {wa.get('current') or 'the default model'}. "
                "Only when pass=false, you may set next_worker to the model and/or effort for the repair attempt: a stronger "
                "one when the gaps show the worker could not reason through the problem, a cheaper one when the remaining "
                "fixes are mechanical. Leave next_worker null to keep the automatic escalation. "
                f"Allowed models: {', '.join(wa.get('models') or [])}. Allowed efforts: {', '.join(wa.get('efforts') or [])}.",
            ]
    elif req.mode == "route":
        src = req.phase.split(":")[0]
        allowed = PHASE_SPECS[src].back_targets if src in PHASE_SPECS else ()
        meanings = "; ".join(f"{t}: {BACK_EDGE_MEANING.get((src, t), '')}" for t in allowed) or "none"
        if p.get("early"):
            # SPEC §7: route back "when a finding's root cause is upstream" — asked once, before any fix is applied
            parts += [
                "",
                f"Phase {src} just reported blocking findings in its first round; no fix has been applied yet. Decide where they must be fixed.",
                f"- back (target_phase in [{', '.join(allowed) or '—'}]) when the ROOT CAUSE is upstream: {meanings}. "
                f"The pipeline then re-runs that phase and every phase after it, with your reason and these findings.",
                f"- fix_here when the findings can be fixed in {src} itself (the normal repair loop applies the fixes). "
                "Choose this unless the evidence shows the upstream artifact itself is wrong; patching a symptom here while the "
                "upstream artifact stays wrong is the case to route back.",
                f"Back-edges used so far: {p.get('back_edges_used', 0)} / {p.get('max_back_edges', 3)}; already routed back to: {p.get('targets_used', [])}",
                "",
                "BLOCKING FINDINGS / FAILED CHECKS:",
                *[f"- {x}" for x in p.get("failed", [])],
            ]
            if p.get("worker_text"):
                parts += ["", "THE REPORT:", _clip(p["worker_text"], 12000)]
            return "\n".join(parts)
        parts += [
            "",
            f"Phase {src} has used all its repair rounds and still fails. Decide where to route.",
            f"- back (target_phase in [{', '.join(allowed) or '—'}]) when the ROOT CAUSE is upstream: {meanings}",
            "- forward is only valid if the remaining failures are not real (the code will re-check the gate; a failed deterministic gate can never be overruled)",
            "- halt otherwise (fix_here is not an option any more: the repair rounds are used up).",
            f"Back-edges used so far: {p.get('back_edges_used', 0)} / {p.get('max_back_edges', 3)}; already routed back to: {p.get('targets_used', [])}",
            "",
            "FAILED CHECKS:",
            *[f"- {x}" for x in p.get("failed", [])],
        ]
        if p.get("worker_text"):
            parts += ["", "LAST WORKER OUTPUT:", _clip(p["worker_text"], 12000)]
    return "\n".join(parts)


def _short(s: Any, n: int = 140) -> str:
    t = " ".join(str(s or "").split())
    return t[:n] + ("…" if len(t) > n else "")


def _clip(s: str, n: int) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    head = s[: n // 3]
    tail = s[-(2 * n // 3):]
    return head + f"\n\n[... {len(s) - n} characters omitted ...]\n\n" + tail


# --------------------------------------------------------------------------------------
# Reply sanitising
# --------------------------------------------------------------------------------------

_BUILTIN_DANGEROUS = {"clear", "compact", "reset", "logout", "login", "exit", "quit", "config", "permissions", "model", "resume", "rewind", "init", "install-github-app", "mcp", "hooks", "plugin", "plugins"}


def sanitize_reply(reply: str, allowed_commands: list[str]) -> tuple[str, str | None]:
    """A judge reply starting with '/' is dispatched as a slash command. Only allow the handed-back commands.

    Returns (safe_reply, warning).
    """
    r = (reply or "").strip()
    if not r.startswith("/"):
        return reply, None
    name = re.match(r"^/([\w.:\-]+)", r)
    cmd = name.group(1) if name else ""
    allowed = {a.lstrip("/") for a in allowed_commands}
    if cmd in allowed and cmd not in _BUILTIN_DANGEROUS:
        return r, None
    return "Decision: " + r, f"judge reply started with a non-allowed slash command /{cmd}; sent as plain text"
