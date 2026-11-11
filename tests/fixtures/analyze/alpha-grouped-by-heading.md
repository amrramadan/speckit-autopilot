<!-- fixture: synthetic, modelled on a real Spec Kit /speckit.analyze report (assistant text; findings grouped under ### High / ### Medium / ### Low headings with no Severity column; long lines/cells truncated with '…'; coverage rows trimmed) -->
No critical issues and no constitution violations. Every one of the 613 audit IDs is closed by exactly one task, in the right story. The real problems are six high-severity gaps, mostly in how the acceptance checks are…

## Specification Analysis Report — spec-CONSOLE

### High

| ID | Category | Location(s) | Summary | Recommendation |
|---|---|---|---|---|
| F1 | Inconsistency | tasks.md:52 (T002), :97 (T015), :827 (T299); spec.md:909 (SC-012) | The flags-OFF baseline can't stay zero-diff. `/feature-flags` renders every catalog key (`feature-flags/page…| Capture the baseline after T002/T008, or add an "expected OFF diffs" list to the sweep config. Then say in S…|
| F2 | Inconsistency | spec.md:581 (FR-013); contracts/sweep.md ("excluding `(auth)`") | FR-013 requires the sweep to "reach the sign-in steps", but the contract leaves `(auth)` out. The 5 sign-in…| Bring `(auth)` into the sweep (no session needed), or change FR-013 and point SC-002 to SC-005 at T297's aut…|
| E1 | Coverage | spec.md:635 (FR-204); research R-4; tasks T057, T071, T148 | FR-204 says search covers all records on every paginated list. R-4 moves search to US5, but T148/T149 add it…| Add a task per server-mode list: add a `q` parameter, or remove the search box under `tables`. Re-point CORE…|
| E2 | Coverage | spec.md:893 (SC-007); tasks T022/T023; ORD-101 recommendation | SC-007 says restocking returned stock needs a confirmation naming what changes. T023 only removes the defaul…| Add the summary confirm to T023 and an assertion to T022. |
| F3 | Inconsistency | spec.md:554 (FR-003), :907 (SC-011); tasks.md:331 (T079) | CORE-003 (High, "no route has a loading state") is closed by T079. T079 is a 404 test that already passes to…| Keep T079 as a guard. Add a Playwright check with a delayed read that asserts the skeleton or progress bar a…|
| F4 | Conflict | spec.md:578 (FR-012); plan.md:94-95; data-model §5, §7 | FR-012 says every limit this feature introduces must be a setting. The plan hard-codes several: bulk orders…| Narrow FR-012 to business thresholds and list mechanism caps as allowed constants. This is a spec edit. |

### Medium

| ID | Category | Location(s) | Summary | Recommendation |
|---|---|---|---|---|
| F5 | Inconsistency | data-model.md:63; spec FR-005 (:559) and US1 independent test; contracts/server-actions.md (US7 table) | The plan makes the Meadow theme flag a prerequisite for every story, including US1's UI; the spec doesn't s…| Add Meadow to FR-005. Make the reason requirement follow the same flag as the dialog that collects it. Pick…|
| F6 | Inconsistency | spec.md:881 (SC-001); data-model.md:215; tasks T005, T300 | SC-001 and data-model allow High rows to end only as "fixed" or "rejected". T005 and T300 also allow "fixed_…| Align all three. Most likely change SC-001 to allow fixed-by-pattern, since FR-001 defines it as an outcome. |
| E3 | Coverage | spec.md:562 (FR-006); contracts/sweep.md | FR-006 requires 360, 390, 768 and 1440 px. The sweep runs EN 1440, RTL 1440 and RTL 390 only. 768 px is exactl…| Add RTL 768, EN 390 and RTL 360 layouts, or limit FR-006's MUST to what is swept. |
| E4 | Coverage | contracts/sweep.md (routes); tasks T166, T168–T170, T178–T180, T209–T210, T122–T125 | The sweep visits each `page.tsx` once with one fixture ID. The audit's 86 routes include tabs and states the…| Add a variant list (route plus query or fixture) to `fixture-ids.ts`. |
| E5 | Coverage | spec SC-004 (:887); contracts/sweep.md | SC-004 needs RTL-locale body text at 13 px or more. The sweep checks only RTL table headers. | Add an RTL body-text check. |
| E6 | Coverage | spec SC-009 (:904) | No sweep check or task measures "no page taller than 10 screens at 1440×900". | Add a `scrollHeight / 900 ≤ 10` check (default state). |
| E7 | Coverage | plan.md:80-85 | Three performance goals have no measuring task: search p95 ≤ 300 ms, orders list ≤ 1.2× today's read time, 5…| Add the measurements to T104/T142 and record them in `evidence/flows.md`. |
| E8 | Coverage | constitution §II; plan.md:112 | The 85% coverage gate on new modules is claimed in the plan, but no close-out or polish task runs coverage. | Add `vitest --coverage` for the listed modules to each story's close-out task. |
| E9 | Coverage | spec FR-009 (:573), FR-011, edge case "No permission" | Nothing verifies that View-level or Marketing users never see bulk bars, pickers or build buttons. The sweep…| Add a sweep pass (or component tests) as a View-level Marketing admin. |
| E10 | Coverage | spec Edge Cases (:525, :532) | Two edge cases have no task: retrying a bulk action touches only the failed orders, and each home or report…| Add a "retry failed" control to T108/T109 and per-section error boundaries to T097/T258. |
| A1 | Duplication | tasks T068 [US2], T093 [US3] → `C/empty-state.tsx` | The `ux` branch of `EmptyState` is built twice under different flags. Which flag selects it is undefined. | Give it one owner, say whether it follows `tables` or `ui_kit`, and drop the other task. |
| A2 | Duplication | tasks T147 [US5] (HOME-014), T295 [US11] (SHELL-005) → `C/topbar.tsx` | The keyboard-driven command palette is built twice, under `search` and under `shell`. | One task builds it; the other closes its row as fixed by pattern. |
| F7 | Inconsistency | spec US2 scenario 3 (:202), FR-203; tasks T073 [US2], T155 [US6], T271 [US10] | The US2 story test relies on CAT-040 and RPT-035, but those are fixed under the `catalogue` and `reports` fl…| Move CAT-040 and RPT-035 to US2, or reword the US2 scenario. |
| F8 | Underspecified | FR-005 (US9 needs only `ui_kit`); tasks T076 [US2], T258 [US9], T269 [US10] | `dashboard.tsx` ends up with an unspecified `tables` × `home` flag matrix. No task creates the shared period…| Name an owner task for the period control. State whether the home table follows `tables` or `home`. |
| F9 | Ordering | tasks T094, T098, T099 [P] vs T097; T100 [P] vs T087/T092/T093 | These are marked parallel ([P]) but edit the same files: every page under `app/[locale]`, and the shared com…| Remove the [P] markers, or split the work by route group. |
| B1 | Ambiguity | spec FR-012 "staleness windows"; data-model §2 | No staleness setting exists anywhere. FR-1107 defines "stale" as "a refresh failed", not a time window. | Drop "staleness windows" from FR-012, or define the window and its settings key. |
| F10 | Inconsistency | spec SC-012; contracts/sweep.md ("main-landmark signature") | The flags-OFF signature covers only `<main>`. The sidebar, top bar and `<title>` (which US1, US3 and US11 ch…| Add `<title>` and the shell landmarks to the signature, or narrow SC-012's wording. |
| D1 | Convention | tasks T004 `scripts/check-tasks-coverage.py` | This would be the first Python file in the repo's own tooling; T005 already validates the ledger in TypeScri…| Write T004 as a vitest test next to T005. |

### Low

| ID | Category | Location | Summary |
|---|---|---|---|
| F11 | Drift | research.md:353 | The R-15 heading still says "owner confirmation needed"; its status line says decided. |
| F12 | Inconsistency | spec Out of scope ("BI mart calculations…") | R-15 Option A changes the BI finance refresher. Add a carve-out for RPT-006. |
| B2 | Ambiguity | spec Assumptions (:936-939) | "Clarify may revisit this" is left open for search-term masking and the IP column. The contract stores no te…|
| B3 | Ambiguity | spec Assumptions (:928) vs FR-001 | Whether Medium-severity issues can be deferred is unstated. |
| A3 | Duplication | FR-110 vs FR-503 | The wallet phone formats (FR-110) are a subset of FR-503. The staging is intended; cross-reference them. |
| F13 | Coverage | contracts/sweep.md | Title uniqueness is checked in English only; FR-312 requires the page's language. |
| C1 | Granularity | T097 | One task restructures about 86 pages. Split it by route group. |
| C2 | Underspecified | T078, T102 | Both refer to rows "moved into US2/US3", but all 613 ledger rows still carry their original story. There is…|

### Coverage summary

| Requirement group | Has task? | Main tasks | Notes |
|---|---|---|---|
| SC-009, SC-011 | **No** | — | E6, F3 |

**Audit rows:** 613 of 613 closed by exactly one task, with no duplicates, no unknown IDs, and no story mismatches. The 30 US1 rows match the spec's list exactly. Two High rows (CAT-052, PRC-005) are closed by generic p…

**Unmapped tasks:** none. Setup, foundation and polish tasks map to FR-001, FR-004, FR-013 and FR-015.

### Metrics

- **Requirements:** 134 functional requirements and 14 success criteria (148), plus 613 audit rows
- **Tasks:** 305 (T001–T305, contiguous)
- **Coverage:** 146 of 148 requirements have at least one task (98.6%); 7 of those are only partly covered
- **Ambiguities:** 3 · **Duplications:** 3 · **Critical issues:** 0 · **High:** 6

### Next actions

- Resolve **F1** and **F2** before Phase 2: they change T013 (the sweep build) and T015 (the baseline capture). **E1, E2, F3 and F4** need fixing before their stories start.
- The medium items can be fixed inside each story's tasks.
- How to apply them: F4, F5, F6, B1 and F12 are spec edits (run `/speckit.specify` to refine). F2, E3 to E6 and F10 change the sweep contract (`/speckit.plan`). E1, E2, F3, A1, A2, F8 and F9 are direct edits to `tasks.m…

Would you like me to suggest concrete remediation edits for the top 10 issues?

## Extension Hooks

**Optional Hook**: git
Command: `/speckit.git.commit`
Description: Auto-commit after analysis

Prompt: Commit analysis results?
