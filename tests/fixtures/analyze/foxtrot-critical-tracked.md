<!-- fixture: synthetic, modelled on a real Spec Kit /speckit.analyze report (assistant text; severity cell with a suffix 'CRITICAL (tracked)'; pre-hook and post-hook blocks kept; long lines/cells truncated with '…') -->
No blocking problems in the artifacts themselves. There is one constitution item, and the founder has to approve it before implementation can start. After that come three HIGH inconsistencies, each a small edit.

Every requirement has at least one task. I checked the claims against the code on this branch. The Spec Kit prerequisite script rejects the `feature/013b-…` branch name, so I took the feature directory from `.specify/fe…

## Specification Analysis Report

| ID | Category | Severity | Location(s) | Summary | Recommendation |
|----|----------|----------|-------------|---------|----------------|
| C1 | Constitution | CRITICAL (tracked) | plan.md:56, 137–142; tasks.md T002; spec.md:379 | The founder has not approved widening EXC-003 yet (`exceptions.md:11`). The widening covers the `qa` queue's…| No artifact edit needed. The founder approves the widening (constitution "Conflict Resolution" step 3) befor…|
| I1 | Inconsistency | HIGH | tasks.md T039 vs T041; contracts/r21-audit.md "Closing"; contracts/rule-actions.md "Single-writer tests" | T039's new `test_only_sample_writer.py` allows only `audit_sample.py` and `qa/audit.py` to write `program_au…| Add `close_open(conn, source_id)` to `audit_sample.py` and call it from `store.transition` when `frm == "pr…|
| I2 | Inconsistency | HIGH | spec.md:221–222 vs contracts/dry-run.md step 3 | The spec's edge case says an opted-out or suspended merchant's dry run runs on its archived pages. But `chec…| Pick one. Either amend the edge case ("the dry run is refused for an ineligible merchant") or let `qa/check.…|
| U1 | Underspecification | HIGH | tasks.md T045; quickstart.md §4 step 10 | The e2e test (keep one sample, demote one, demote a third program's from its merchant page) needs at least 3…| In T045 (and in the quickstart), set `qa.r21.sample_every` to 1 in the test database, or seed until there ar…|
| D1 | Duplication | MEDIUM | tasks.md T003; data-model.md "TypeScript mirrors"; plan.md:148 | `RULE_FIELD_VALUES` already exists at `db/schema/_types.ts:468` (spec-009). Its parity with Python is alread…| Have T003 add only `PARSE_PROBLEM_VALUES` and its parity test. Record that spec-007's mirror hand-off was…|
| G1 | Coverage | MEDIUM | spec.md SC-004; tasks.md T010, T050 | No task measures "20 pages within 10 s". T010 doesn't time the run, and T050 is a manual walk. | Add a timing assertion over 20 fixture pages to T010 (or to T020's browser test). |
| G2 | Coverage | MEDIUM | spec.md FR-022; tasks.md T030, T035, T044 | Only T011 (dry run) and T018 (promote/reject) test `worker_unavailable`. The add/edit/revert, deprecate/demo…| Add a `worker_unavailable` case to T030, T035 and T044, and to the coverage table. |
| U2 | Underspecification | MEDIUM | spec.md FR-001, Key Entities:314–316 | `RULE_STATUS_VALUES` includes `shadow` (`_types.ts:107`). The spec lists four statuses and never says where…| Say whether `shadow` rules show (and where), or state that none are ever written. |
| I3 | Inconsistency | MEDIUM | plan.md:105–110, 118, 130–131; tasks.md T017/T029/T034 | The plan says `qa/` gains two modules and its tree has no `qa/rules.py`, but three tasks create and extend i…| Add `qa/rules.py` to the plan tree and the serialize list (T017 → T029 → T034). Change the plan's actions pa…|
| I4 | Inconsistency | LOW | contracts/rule-actions.md "Program demote"; tasks.md T008 | The contract says `demote_in` re-arms the synthesis signal, but no re-arm code exists. Readiness is derived…| Reword the contract, and make T008 assert `readiness.is_ready(...)` instead. |
| I5 | Redundancy | LOW | tasks.md T041; r21-audit.md | "And in `promote_repair`'s deprecation" is redundant: `promote_repair` already goes through `store.transitio…| Keep one closing point, in `transition`. Keep the repair test case. |
| I6 | Terminology | LOW | contracts/rule-actions.md | The new cores are named `*_in`, but spec-013's existing connection-taking core is `propose_rule(conn, …)`. | Note the exception in the contract, or leave it as is. |
| P1 | Ordering | LOW | tasks.md T022 [P] with T023 | T023's test reads the vector file that T022 is still growing, so it can pass against the old 72 cases. | Run T023 against the final file, or put T022 before T023. |
| A1 | Ambiguity | LOW | spec.md FR-014 | "At a rate of 1 in N promotions" reads as exact, but the hash draw gives about 1 in N on average. SC-006's 1…| Change it to "on average 1 in N". |
| S1 | Style | LOW | spec.md:16 | Its **Status** is still "Draft" although tasks are written. | Update it when the next edit lands. |

**Constitution alignment:** C1 only. Its gate is already in tasks.md. Every other principle in the plan's Constitution Check matches the artifacts: §II settings, §IV RTL/locales, §V primitives, §VI real-infrastructure t…

**Unmapped tasks:** none. Setup and polish tasks (T001, T002, T021, T046, T047, T049–T052) are process tasks.

**Metrics**
- Total requirements: 32 (24 FR + 8 SC)
- Total tasks: 52
- Coverage: 100% have at least one task (2 partial: FR-022, SC-004)
- Ambiguity count: 2 (U2, A1)
- Duplication count: 1 (D1)
- Critical issues: 1 (C1, which needs an approval rather than an edit)

## Next Actions

1. **Founder approves the EXC-003 widening (C1).** Implementation can't start without it, and T002 enforces that.
2. **Fix I1, I2 and U1** before `/speckit.implement`. Each is a one-paragraph edit to tasks.md, a contract or the spec. Left alone, I1 makes T039 and T041 contradict each other, and U1 makes T045 depend on the database'…
3. D1, G1, G2, U2 and I3 can be fixed in the same pass by editing tasks.md, data-model.md and plan.md. The LOW items are optional.

Would you like me to suggest concrete remediation edits for the top 8 issues (I1, I2, U1, D1, G1, G2, U2, I3)?

## Extension Hooks

**Optional Pre-Hook**: git
Command: `/speckit.git.commit`
Description: Auto-commit before analysis
Prompt: Commit outstanding changes before analysis?
To execute: `/speckit.git.commit`

**Optional Hook**: git
Command: `/speckit.git.commit`
Description: Auto-commit after analysis
Prompt: Commit analysis results?
