<!-- fixture: synthetic, modelled on a real Spec Kit /speckit.analyze report (assistant text; '**CRITICAL**' bold severity cell; metrics as a table row '| Critical issues | 1 (C1) |'; long lines/cells truncated with '…'; coverage rows trimmed) -->
## Specification Analysis Report

The analysis found **1 critical issue**: a returning recipient could become impossible to deliver to. It should be fixed in the plan before `/speckit.implement`. The rest is two spec-wording conflicts the plan raised bu…

| ID | Category | Severity | Location(s) | Summary | Recommendation |
|----|----------|----------|-------------|---------|----------------|
| C1 | Coverage / design gap | **CRITICAL** | research.md R4; plan.md Summary "Retention"; data-model.md "Vault anchor"; tasks T021, T025, T035 | Door media are encrypted under the recipient's `claims_retention` key. `mintOrGetDek` **refuses on a destroy…| Fix in `/speckit.plan` (R4) before implementing. Options: give `claims_retention` an active-shipment deferra…|
| H1 | Inconsistency | HIGH | spec.md FR-019, US4 scenario 3, SC-006; plan.md "Surfaced" item 1 | The spec still says door media are **deleted** when the claims period ends. The plan implements destroying t…| Reword FR-019, US4-3 and SC-006 to "made unrecoverable (key destroyed), the proof remains", or run `/speckit…|
| H2 | Inconsistency | HIGH | spec.md US4 Independent Test ("a proof's claims period"), SC-006; research R4 "Consequence"; plan.md "Surfac…| The spec talks about each proof's own claims period. The design runs one clock per recipient, reset by their…| Amend the spec to per-recipient wording, or resolve as part of C1 (option b would restore a per-proof clock)…|
| M1 | Underspecification / conflict | MEDIUM | spec.md FR-012a, FR-011; research R8 and R13 | FR-012a requires a **distinct** "missing" refusal code. The plan uses the generic `invalid_input` from the i…| Add `pod_evidence_missing` (a 14th code), or amend FR-012a and FR-011 to accept `invalid_input` with a field…|
| U1 | Underspecification | MEDIUM | quickstart.md row 14; tasks T024 | "Bad evidence" in the payment-gate-first row is undefined. If it means a missing or non-ULID ref, the input…| Say a well-formed ULID that doesn't exist in quickstart row 14 and T024. |
| M2 | Inconsistency | MEDIUM | tasks T003; quickstart.md "Migration proof" + closeout; research R5; plan.md Project Structure (`0036_pod_me…| T003 records the 0036/0037 split in plan.md and data-model.md only. Quickstart still expects the CHECK at `0…| Add quickstart.md, research R5 and the plan.md file-list line to T003. |
| L1 | Constitution (noted) | LOW | plan.md gate IV; tasks T031 | Export is a POST that writes an audit row but skips `Idempotency-Key` (§IV says every mutating endpoint need…| Call it out in the PR description; no change needed. |
| L2 | Terminology | LOW | spec.md FR-012a ("wrong driver"); research R13 (`pod_evidence_wrong_courier`) | The spec says "driver"; the codes and routes say "courier". This is the house convention. | None. Optionally add a glossary line in the contract. |
| L3 | Task precision | LOW | tasks T026 | The hedge "(or new `scans.occurred-at.spec.ts` if none exists)" isn't needed: `scans.service.spec.ts` exists…| Drop the hedge. |
| L4 | Pre-existing drift | LOW | constitution §I (Next 16.2) vs packages (16.3) | Already recorded by specs 040 and 041, and by plan.md. | None here. |

### Coverage Summary

| Requirement | Has task? | Task IDs | Notes |
|---|---|---|---|
| FR-001 | ✓ | T019, T021 | |
| FR-002 | ✓ | T006, T007, T019, T021 | |
| FR-003 | ✓ | T019, T021 | |

### Constitution alignment

Apart from the low-severity idempotency note (L1), every design element passes against §§I–IX. C1 is not a constitution violation: crypto-shredding is the correct way to erase under §IX. It is a gap in what happens afte…

**Unmapped tasks:** none. T001–T003 are setup and T041–T044 are polish and closeout, as they should be.

### Metrics

| Metric | Value |
|---|---|
| Total requirements | 33 (26 FRs, 7 buildable SCs; SC-008 is deferred to spec-043) |
| Total tasks | 44 |
| Coverage | 100% have at least one task; 4 are partial or conflicted (FR-012a, FR-019, SC-006, plus FR-008 and FR-011 de…|
| Ambiguity count | 1 (U1) |
| Duplication count | 0 |
| Critical issues | 1 (C1) |

## Next Actions

1. **Fix C1 before `/speckit.implement`.** Rerun `/speckit.plan` (or edit research R4, data-model.md and plan.md by hand) to choose the returning-recipient and mid-delivery-expiry behaviour. Then regenerate or patch the…
2. **H1 and H2:** reword FR-019, US4-3 and SC-006 in `spec.md`, or run `/speckit.clarify`. They are cheap to settle at the same time as C1, since option (b) in C1 changes the answer to H2.
3. **M1, U1, M2, L3:** small edits to `spec.md`, `quickstart.md` and `tasks.md`.

Would you like me to suggest concrete remediation edits for the top 6 issues (C1, H1, H2, M1, U1, M2)? For C1 I'd lay out the options with their trade-offs, since choosing one is your call.

## Extension Hooks

**Optional Hook**: git
Command: `/speckit.git.commit`
Description: Auto-commit after analysis

Prompt: Commit analysis results?
To execute: `/speckit.git.commit`
