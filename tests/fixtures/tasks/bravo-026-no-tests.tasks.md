<!-- fixture: synthetic, modelled on a real Spec Kit tasks.md (whole file; '**Tests**: NOT requested. This spec writes no code') -->
---
description: "Task list for spec-026 — Payments/Fintech Counsel: Technical-Aggregator Structure"
---

# Tasks: Payments/Fintech Counsel — Technical-Aggregator Structure

**Input**: Design documents from `/specs/026-payments-aggregator-structure/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md (no contracts/ — purely a documentary deliverable)

**Tests**: NOT requested. This spec writes no code (Constitution §I/§VII); verification is a documentary cross-reference review (research.md R5), captured as a polish task — there are no automated test tasks.

**Organization**: Tasks are grouped by user story. NOTE: the deliverable is a **single file** (`docs/payments/aggregator-structure.md`), so the three story-authoring tasks edit the same file and run **sequentially** (no `[P]`). Only the two cross-link edits (different files) are parallelizable.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Exact file paths are included in every task.

## Path Conventions

Documentation-only spec. The deliverable lives at `docs/payments/aggregator-structure.md`. No `apps/`, `packages/`, or `pipeline/` paths are touched.

---

## Phase 1: Setup

**Purpose**: Create the deliverable scaffold.

- [X] T001 Create the `docs/payments/` directory and scaffold `docs/payments/aggregator-structure.md` with: H1 title, a source-citation header (links to `specs/026-payments-aggregator-structure/spec.md`, BRD §9/§13, constitution §V/§IX, ADR-C-024/C-025/C-039), and the three empty part headings — "Part A — Structural assertions", "Part B — Receiving-account model", "Part C — No-custody integration boundary".

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared conventions every part relies on. **⚠️ Same file as all story tasks — must complete before Part A/B/C authoring.**

- [X] T002 In `docs/payments/aggregator-structure.md`, add the shared legend block: the counsel-status enum (`Pending` default · `Confirmed` · `Conditional` · `Rejected`), the `SA-`/`NCB-` stable-ID conventions, the "blocked-pending-counsel" gate convention (FR-012), and a note that counsel engagement is client-pending so all statuses ship as `Pending`.

**Checkpoint**: Scaffold + legend ready — part authoring can begin (sequentially, same file).

---

## Phase 3: User Story 1 - Counsel-confirm the technical-aggregator structure (Priority: P1) 🎯 MVP

**Goal**: Part A — the launch-gate structural assertions counsel must confirm.

**Independent Test**: Part A renders a table where every `SA-` row has a statement, a resolvable citation, and a `counsel_status` (= `Pending`); reviewable on its own without Parts B/C.

- [X] T003 [US1] Author "Part A — Structural assertions" table in `docs/payments/aggregator-structure.md` with rows `SA-01`…`SA-06` per data-model.md (columns: id · statement · citation · counsel_status=`Pending` · condition/reason · gates). Covers FR-002, FR-003, FR-004 (SA-01), FR-005 (SA-02), FR-006 (SA-03), and the fee/credit/handover assertions SA-04/05/06.

**Checkpoint**: Part A complete — the launch-gate assertion set is auditable (SC-001 shape).

---

## Phase 4: User Story 2 - Resolve the at-door receiving-account model + AML/KYC (Priority: P2)

**Goal**: Part B — the receiving-account model decision frame + AML/KYC + per-rail cap design.

**Independent Test**: Part B shows exactly one `decision` field (value `Pending`), an AML/KYC obligations placeholder, and a per-rail cap design basis that names the cap as DB-tunable; reviewable on its own.

- [X] T004 [US2] Author "Part B — Receiving-account model" in `docs/payments/aggregator-structure.md`: candidate models (courier-personal vs Bravo-controlled), a single `decision` field (= `Pending`), AML/KYC obligations placeholder, and the per-rail collection-cap **design basis** — explicitly framing the cap value as a DB-tunable owned by spec-007's registry and consumed by spec-015 (constitution §II) and citing the external rail limits (instant-payment rail 10k/tx·25k/day·100k/month; e-wallets ~5–10k/day) as facts. Covers FR-007, FR-008, FR-009.

**Checkpoint**: Part B complete — the open ADR-C-039 receiving-account question is framed for counsel (SC-002 shape).

---

## Phase 5: User Story 3 - Publish the no-custody integration boundary (Priority: P3)

**Goal**: Part C — the binding boundary rules + Gate Map, plus making them discoverable from the ADR and the security conventions doc.

**Independent Test**: Part C lists `NCB-01`…`NCB-06` each with ≥1 named downstream consumer, and a Gate Map mapping 015/016/018/022/027 to their gating items; the ADR + SECURITY_PATTERNS pointers resolve to this document.

- [X] T005 [US3] Author "Part C — No-custody integration boundary" rule list in `docs/payments/aggregator-structure.md` with `NCB-01`…`NCB-06` per data-model.md (columns: id · rule · citation · consumers). Covers FR-010 (a)–(f).
- [X] T006 [US3] Add the Gate Map table in `docs/payments/aggregator-structure.md` mapping spec-015/016/018/022/027 → their gating `SA-`/`NCB-`/decision items → blocked-until condition. Covers FR-011, FR-012, SC-004.
- [X] T007 [P] [US3] Update `ADR-COLLECTION.md` ADR-C-039 "New open items" line so the receiving-account-model / AML/KYC item points to `docs/payments/aggregator-structure.md` (Part B).
- [X] T008 [P] [US3] Add a pointer in `docs/conventions/SECURITY_PATTERNS.md` §14 ("Payments/central-bank aggregator structure …") to `docs/payments/aggregator-structure.md` as the citable no-custody boundary.

**Checkpoint**: All three parts published; the boundary is discoverable from the ADR and the security conventions doc.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Acceptance verification + capture an out-of-scope finding.

- [X] T009 Run the `quickstart.md` acceptance cross-reference review against `docs/payments/aggregator-structure.md`: every `SA-`/`NCB-` row has a resolvable citation; every `SA-` has a `counsel_status`; exactly one receiving-account `decision` field; every `NCB-` names ≥1 consumer; Gate Map lists 015/016/018/022/027; no PII; no hardcoded Bravo cap value. Record pass/fail (SC-001…005).
- [X] T010 [P] File the out-of-scope finding via `/cc-cleanup-add 026`: `update-agent-context.sh` mis-parses a documentation-only plan's `Technical Context: N/A — …` fields and injects junk rows into root `CLAUDE.md` "Active Technologies" (observed this spec; manually reverted). Suggest the script skip `N/A`-prefixed fields.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (T001)**: no dependencies — start immediately.
- **Foundational (T002)**: depends on T001 — BLOCKS all story tasks (shared legend, same file).
- **User Stories (T003 → T004 → T005/T006)**: all depend on T002. Because they edit the **same file**, they run sequentially in priority order (US1 → US2 → US3); they are NOT parallel, but each part is an independently reviewable increment.
- **Cross-links (T007, T008)**: depend on the document existing (T001) and ideally Part C drafted (T005/T006) so the pointer target is meaningful; T007 and T008 are mutually `[P]` (different files).
- **Polish (T009, T010)**: T009 depends on all authoring (T003–T008); T010 is independent `[P]`.

### Within Each User Story

- No tests (documentary deliverable).
- Each story = one or two same-file section edits; commit after each task (atomic, Protocol §1).
- Story section complete before moving to next priority.

### Parallel Opportunities

- T007 and T008 can run in parallel (different files).
- T010 can run any time (different file — the cleanup doc).
- T003/T004/T005/T006 CANNOT be parallel — same file (`docs/payments/aggregator-structure.md`).

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. T001 Setup → T002 Foundational → T003 Part A (the launch-gate assertion set).
2. **STOP and VALIDATE**: Part A is the minimum usable artifact — counsel can review the structural assertions even before Parts B/C exist.

### Incremental Delivery

1. Setup + Foundational → scaffold ready.
2. US1 (Part A) → the launch-gate assertions (MVP).
3. US2 (Part B) → receiving-account model frame.
4. US3 (Part C + cross-links) → published boundary + discoverability.
5. Polish → acceptance review + cleanup-doc finding.

### Notes

- `[P]` = different files, no dependencies. The bulk of this spec is one file, so most tasks are sequential by design.
- Counsel statuses ship as `Pending`; when counsel responds, statuses/decision are updated and Linear re-synced (`/cc-linear-sync 026`) — that update is outside this tasks list (it is the client-pending follow-up, quickstart.md "Capturing counsel's response").
- `/cc-ship` commits the staged spec+plan+tasks bundle on the first phase transition and advances ENG-1088.

## Modernization note (2026-07-10 — recovery as spec-026)

This task list and all deliverables were **authored 2026-06-19 as spec-023** and completed (T001–T010) on the unmerged `feature/023-payments-aggregator-structure` branch. They were recovered onto `feature/026-payments-aggregator-structure` off current `main`, renumbered 023 → 026, and their downstream references refreshed to the current Roadmap (ledger 012 → 015, payments/COD handover → 016, booking 014 → 017, settlement 013 → 018, courier client → 022, card-PSP + cash-voucher contracts 024 → 027). The boundary doc was reconciled against the now-shipped ledger (spec-015/016/018/021). **T007/T008 cross-links** (ADR-C-039 "New open items" + SECURITY_PATTERNS §14 pointer) were made on the orphan branch and never reached `main`, so they are **re-applied to current `main`** during this recovery to keep the deliverable discoverable. See `docs/post-spec-026-cleanup.md`.
