<!-- fixture: DERIVED from tasks/bravo-115.tasks.md (synthetic); edit: re-uses T008 (duplicate ID) and adds one open task -->
---
description: "Tasks for spec-115: test seams that block re-testing, and the payout maker-checker rule"
---

# Tasks: Test Seams That Block Re-testing, and the Payout Maker-Checker Rule

**Input**: Design documents from `specs/115-test-seams-payout-maker-checker/`
**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/api-changes.md](contracts/api-changes.md), [quickstart.md](quickstart.md)

**Tests**: Required. DEC-32 touches a money action (constitution §VII), and quickstart lists a proof
for every part. Write each story's tests first and watch them fail.

**Organization**: One phase per user story, each able to ship alone. US1 (DEC-32) comes first
because it is the only production change. US2, US3 and US4 do not depend on US1 or on each other.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an unfinished task)
- **[Story]**: The user story in spec.md this task belongs to

## Rules for every task

- **Pin the directory.** Pass `SPECIFY_FEATURE_DIRECTORY="$PWD/specs/115-test-seams-payout-maker-checker"` to any Spec-Kit script.
- **Atomic commits**, one concern each, with typecheck and the affected tests green first (root `AGENTS.md`). Check `git diff --cached` before each commit.
- **Watch it fail.** Every new test is shown failing on the old behaviour first. Copy the file before planting, and `cmp` against the copy after restoring (root `AGENTS.md` convention 2).
- **Contract first.** A wire change edits `packages/shared-types/openapi.yaml`, then runs `pnpm --filter @bravo/shared-types contract:generate`, in the same commit.
- **A catalogue key added** needs the web and full admin Vitest runs in the same commit, because both pin key counts.
- **Read `Test Suites:` as well as `Tests:`.** A suite that fails to load leaves `Tests:` green.
- **User-only skills are never replicated by hand:** `/cc-review`, `/cc-ship`, `/cc-cleanup-add`, `/cc-business-context`. Stop and ask the operator.
- **Out-of-scope findings** go to `docs/post-spec-115-cleanup.md` (AIP §5).
- **Linear:** ENG-1368 is shared (`linear.shared: true`). Comments and ticked tasks only; `/cc-linear-sync` skips it.
- **No pushes** without the operator's go-ahead.

---

## Phase 1: Setup

**Purpose**: Confirm the environment and record the defects on unfixed code. No product code changes.

- [X] T001 Confirm the environment per AIP §8 and record it in `specs/115-test-seams-payout-maker-checker/verification/environment.md`: the Bravo Postgres on 55432 and Redis on 56379 healthy (the sibling project's containers stay stopped), migrations at tip `0114`, Node 24, `pnpm install --frozen-lockfile` clean, and no other `0115` migration on `origin` (`git fetch && git ls-tree -r --name-only origin/main -- apps/api/drizzle | tail -3`, plus `git branch -r` checked for one).
- [X] T002 Record the four defects on unfixed code in `specs/115-test-seams-payout-maker-checker/verification/pre-fix.md`. (a) DEC-32: one admin reveals and then records a payout's transfer, and it is accepted. (b) DEC-13: `GET /v1/dev/sms-stub/messages` answers 404. (c) OBS-406: `POST /v1/dev/payout-destination-stub/reset` answers 404. (d) DEC-05: step 4 of `docs/runbooks/courier-device-checks.md`, run as written, is refused by the receiver. Keep the status and code of each.

---

## Phase 2: Foundational

**Purpose**: None. The four stories share no new code. The reveal table (T003 to T006) belongs to US1 alone.

---

## Phase 3: User Story 1 - The admin who revealed a payout cannot record its transfer (Priority: P1) MVP

**Goal**: record every successful reveal, and refuse a transfer recorded by any revealer with a 409 `payout_revealer_cannot_record`, before anything is written (FR-001 to FR-008).

**Independent Test**: prepare a payout, reveal it as admin A, record its transfer as A (refused, nothing written), then record it as finance user F (accepted). See quickstart "DEC-32".

### Schema

- [X] T003 [US1] Add `payoutDestinationReveals` to `apps/api/src/db/schema/settlement.ts`, beside `manualPayoutTransfers`. Columns, from data-model.md: `id` "`char(26)` ULID, primary key" via `ulidPk()`; `payout_id` "`char(26)` NOT NULL; FK to `settlement_payouts.id`, `ON DELETE RESTRICT`"; `revealed_by_ref` "`subjectRef` NOT NULL; the admin's pseudonymous ops ref (`actor.ref`)"; `revealed_at` "`timestamptz` NOT NULL, default `now()`". Index `ix_payout_destination_reveals_payout_actor` on `(payout_id, revealed_by_ref)`, **not unique**. No `relations()` block and no soft-delete column. The header comment says it holds no bank detail and no personal data (FR-002).
- [X] T004 [US1] Run `pnpm --filter @bravo/api db:generate --name payout_destination_reveals` and rename the output to `apps/api/drizzle/0115_payout_destination_reveals.sql` if needed (journal entry matching). Hand-append the two triggers after the generated DDL, copying migration `0068`'s form: `trg_payout_destination_reveals_no_mutate` (BEFORE UPDATE OR DELETE, FOR EACH ROW) and `trg_payout_destination_reveals_no_truncate` (BEFORE TRUNCATE, FOR EACH STATEMENT), both `EXECUTE FUNCTION app_forbid_mutation()`. Run `pnpm --filter @bravo/api db:migrate` and `pnpm --filter @bravo/api db:check`, and `pnpm db:drift` from the root.
- [X] T005 [US1] Register the table with the e2e resets, so a CASCADE from `settlement_payouts` does not hit the new TRUNCATE trigger: add `'payout_destination_reveals'` to `APPEND_ONLY_TABLES` in `apps/api/test/helpers/reset-append-only.ts` (with a spec-115 comment like the `manual_payout_transfers` one) and to `SETTLEMENT_RESET_TABLES` in `apps/api/test/helpers/settlement.ts`. Then check every suite that names `manual_payout_transfers` in its own reset list (`git grep -ln manual_payout_transfers -- apps/api/test`: `c2c-completion-carveout`, `settlement-manual-confirm`, `settlement-vault-retention`, `topup-migration` and others) and add the new table where the list is explicit. Commit T003 to T005 together, as one schema concern.
- [X] T006 [P] [US1] Add a schema-guard e2e, `apps/api/test/payout-reveal-schema-guards.e2e-spec.ts`, following `manual-payouts-migration.e2e-spec.ts`: an INSERT works, a second row for the same payout and person works (not unique), UPDATE, DELETE and TRUNCATE are each refused by `app_forbid_mutation`, and the FK refuses a `payout_id` with no payout. Show the UPDATE case failing first by applying the migration without its triggers.

### Tests first (watch them fail)

- [X] T007 [US1] Write the e2e suite `apps/api/test/settlement-maker-checker.e2e-spec.ts`, reusing the setup of `settlement-manual-confirm.e2e-spec.ts` and `settlement-reveal.e2e-spec.ts` (two admins A and B, finance user F, one prepared payout awaiting transfer). Cases, from quickstart: (1) A reveals, A records: 409 `payout_revealer_cannot_record`, `category: conflict`, no `details`; zero new rows in `manual_payout_transfers`, `ledger_postings`, `ledger_lines`, `outbox` and `audit_logs`, and the payout still `pending`. (2) The same, then F records: 200, exactly as today. (3) A and B both reveal: A and B each refused, F accepted. (4) A retries the refused request with the same idempotency key: refused again the same way, never a stored success (FR-005). (5) F records, then A resubmits F's identical body: 409, and no `settlement.payout_manual_transfer_repeated` audit event is written (Q2). (6) F resubmits their own recording: the stored result is replayed, as today (FR-008). (7) No reveal, A records: 200 (Q1). (8) A adopts a legacy payout (`prepareLegacyManualPayout`) and records it: 200 (Q3). (9) A rate-limited or failed reveal writes no reveal row. Run it and confirm cases 1, 3, 4 and 5 fail on the old code.
- [X] T008 [P] [US1] Add a reveal-row assertion to `apps/api/test/settlement-reveal.e2e-spec.ts`: a successful reveal writes exactly one `payout_destination_reveals` row with the payout id and `actor.ref`, in the same commit as its `settlement.payout_destination_revealed` audit event; a refused reveal (wrong role, not pending, rate-limited) writes none.

### Implementation

- [ ] T008 [US1] Duplicate of an existing ID
- [X] T009 [US1] Add `insertReveal(tx, { payoutId, revealedByRef })` and `isRevealedBy(tx, payoutId, actorRef): Promise<boolean>` to `apps/api/src/modules/settlement/settlement.repository.ts`. `isRevealedBy` is one indexed `EXISTS` over `(payout_id, revealed_by_ref)`.
- [X] T010 [US1] In `apps/api/src/modules/settlement/payout-reveal.service.ts` (`revealPayoutBankDetails`), call `insertReveal` inside the existing transaction, after the vault read and before the audit append, so the reveal, its row and its audit entry commit together (FR-001). Nothing on a refused, rate-limited or failed path writes a row. T008 passes; commit.
- [X] T011 [US1] Add `PayoutRevealerCannotRecord` to `apps/api/src/modules/settlement/settlement.errors.ts`: `DomainError('conflict', 'payout_revealer_cannot_record', HttpStatus.CONFLICT, "The person who revealed this payout's bank details cannot record its transfer.")`, no `details`, modelled on `DisputeSelfAdjudication`. Amend the file's header ("adds NO new wire code") to name this one exception and why (research R-03).
- [X] T012 [US1] Add the code to the contract: `payout_revealer_cannot_record` in the `ErrorCode` enum and a 409 description on `/v1/ops/settlement/payouts/{payoutId}/confirm-manual-transfer` in `packages/shared-types/openapi.yaml` (as contracts/api-changes.md says), in the hand-kept list in `packages/shared-types/src/schemas/errors.ts`, then `pnpm --filter @bravo/shared-types contract:generate`. Add `errors.payout_revealer_cannot_record` to `packages/shared-types/locales/en.json` and `he.json` with the contract's copy, the Hebrew marked as a draft for the owner in the commit body. Run the shared-types tests and the web and full admin Vitest runs (key counts).
- [X] T013 [US1] In `apps/api/src/modules/settlement/manual-transfer.service.ts` (`confirmManualTransfer`), right after `getPayoutForUpdate` and **before** `findByPayout` (the replay path), throw `PayoutRevealerCannotRecord` when `isRevealedBy(tx, payoutId, actor.ref)` is true. Do not touch `prepareLegacyManualPayout` (Q3). T007 passes in full, and `settlement-manual-confirm`, `settlement-manual-payout` and `settlement-reveal` stay green (FR-008). Prove the order by moving the check after `findByPayout` once: case 5 must fail. Restore and `cmp`. Commit T011 to T013 as one concern if the contract commit cannot stand alone, otherwise T012 first.

### Console

- [X] T014 [US1] Add `payout_revealer_cannot_record` to `MANUAL_GENERAL_ERRORS` and `MANUAL_PAYOUT_ERROR_LABEL` in `apps/admin/src/lib/settlement-vocabulary.ts`, and `ops.settlement.manualError.payout_revealer_cannot_record` to both locale files with the contract's copy (FR-006). Extend `apps/admin/src/app/[locale]/(ops)/settlement/manual-transfer.test.ts`: `toManualPayoutError` maps the code to its own label, never `internal_error`; show it falling through to `internal_error` before the vocabulary change. Run the full admin Vitest (catalogue parity and orphans) and the web Vitest.
- [X] T015 [US1] Update `docs/runbooks/manual-payouts.md`: the system now refuses a transfer recorded by anyone who revealed the payout, with the refusal's code and what the desk shows; remove the sentence saying nothing stops one admin doing both; say a payout revealed before this shipped is not barred (Q1) and adoption is not a reveal (Q3) (FR-007).

**Checkpoint**: J44-14's steps pass on a development stack (quickstart "DEC-32" walk). US1 can ship alone.

---
