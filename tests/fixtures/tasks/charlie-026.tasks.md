<!-- fixture: synthetic, modelled on a real Spec Kit 0.7.1 tasks.md excerpt (letter-suffixed IDs T003a/T014a/T016a; 'Phase 3: User Story 1 — …' em-dash; '### Tests for User Story 1') -->
---
description: "Task list for 026 Checkout — Digital-Default Payment"
---

# Tasks: 026 Checkout — Digital-Default Payment

**Input**: Design documents from `/specs/026-checkout/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: **Included and mandatory.** The template treats tests as optional; this repo does not.
Constitution §II sets an 85% per-file coverage gate on spec-owned modules, and Protocol §16 states that
for cap/exactly-once surfaces *"the test is the acceptance criterion"* — writing the guard without the
concurrent test is a blocker, not a style preference. SC-003/SC-007/SC-001a are unverifiable without them.

Four guards exist because nothing else in the suite would notice their failure: **T016a** (a 026 schema
change breaking 025's shipped placement is invisible to every test inside `packages/payments`), **T024a**
(SC-010 — cardholder data is verified absent, not assumed absent), **T035a** (FR-038 — three callers each
re-implementing an ownership predicate is three chances to omit it), and **T034a** (FR-007b — the ordering
of the reap is the whole guarantee, so the test asserts it *fails* when the reap moves).

**Organization**: Grouped by user story so each is independently implementable and testable.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Parallelizable (different files, no dependency on incomplete work)
- **[Story]**: US1–US5, mapping to spec.md
- Exact file paths in every description

## Path Conventions

Turborepo monorepo. New package at `packages/payments/`; schema in `packages/db/`; boundary in
`packages/integrations/`; surface in `apps/buyer/`. Tests are colocated `*.test.ts` (this repo's shipped
convention — see `packages/cart/src/`), not a separate `tests/` tree.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Scaffold the package and register the flags so every later task has somewhere to land.

- [X] T001 Create `packages/payments/` scaffold — `package.json` (name `@charlie/payments`, exports `.` and `./testing`, deps per plan.md Technical Context), `tsconfig.json`, `eslint.config.js`, `vitest.config.ts`, mirroring `packages/cart/`
- [X] T002 [P] Register the five `026.*` flags in `packages/flags/src/index.ts` — `026.payment_initiation`, `026.webhook_ingest`, `026.capture_on_delivery`, `026.settlement_split`, `026.stock_return_on_failure`, all default OFF, each with its dark behaviour documented in the registry table
- [X] T003 [P] Write `packages/payments/CLAUDE.md` declaring the canonical lock order `order → payment_attempt → payment_transaction → variant_stock → audit advisory` (research D9) and the package's no-identity/no-cookie posture
- [X] T003a [P] Write `specs/026-checkout/design-baseline.md` **before T042** — what the payment surface reuses from `DESIGN_SYSTEM.md` and `@charlie/ui`, and which interaction/a11y guidance was adopted from the mandatory `ui-ux-pro-max` pass (research D12). Required by root `CLAUDE.md` step 9 and present for every prior UI-touching spec (015/016/023/025/065/066); it does **not** re-decide palette or type scale
- [X] T004 Add `@charlie/payments` to the workspace and to `apps/buyer/next.config.ts` `transpilePackages`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The boundary, the schema, and the one cross-package primitive. No user story can begin
before these land.

**⚠️ CRITICAL**: T018–T020 are a **cross-package change to `@charlie/inventory` requiring the 021 owner's
review** (research D8 — the largest scope risk in this plan). They sit in Foundational rather than in US5
where they are consumed, deliberately: external review has lead time, and discovering the primitive is
unacceptable *after* four stories are built on the assumption it exists would be far more expensive.

## Phase 3: User Story 1 — The shopper pays for a multi-brand order (Priority: P1) 🎯 MVP

**Goal**: One shopper payment action creates one independent authorization per brand segment; each order
leaves `awaiting_payment` for `authorized` and gets a quotable reference.

**Independent Test**: Place a two-brand order, pay with the stub, confirm both orders reach `authorized`
with distinct references, amounts equal to the frozen totals, and no card data anywhere in the system.

### Tests for User Story 1 ⚠️ Write first; verify they FAIL before implementing

- [X] T029 [P] [US1] Refusal-matrix test in `packages/payments/src/initiate.test.ts` — every code in contracts/payments-api.md returns as data, never throws
- [X] T030 [P] [US1] Two-segment happy path in `packages/payments/src/initiate.integration.test.ts` — N segments ⇒ N attempts ⇒ N authorizations
- [X] T031 [P] [US1] **Blocker test** in `packages/payments/src/payout-identity.test.ts` — a segment whose brand has no `psp_submerchant_id` refuses **before any money is reserved** and names the maker. Reachable for every brand at launch (research D5), so this is a dominant path, not an edge case
- [X] T032 [P] [US1] Segment-isolation matrix in `packages/payments/src/isolation.test.ts` — every success/failure combination across a multi-segment session leaves each segment independent (SC-001a)
- [X] T033 [P] [US1] Frozen-total test in `packages/payments/src/initiate.amounts.test.ts` — a price moved after placement does not change what is authorized (FR-002)
- [X] T034 [P] [US1] **Concurrency acceptance test** in `packages/payments/src/initiate.concurrency.test.ts` — two concurrent payment submissions against one order; exactly one attempt survives, the other refuses `attempt_in_flight` (Protocol §16). Verify it fails without the partial unique index
- [X] T034a [P] [US1] **Stalled-attempt test** in `packages/payments/src/initiate.reap.test.ts` (FR-007b) — an attempt left `in_flight` past `in_flight_expires_at` is reaped to `abandoned` by the *next* initiation and the order becomes payable again. Assert the ordering explicitly: with the reap moved after the in-flight pre-check the test must FAIL, because `attempt_in_flight` becomes permanent for that order and FR-031's retry promise is quietly repealed
- [X] T035 [P] [US1] Fail-closed test in `packages/payments/src/fail-closed.test.ts` — `026.payment_initiation` dark returns `payment_disabled`; reads still return
- [X] T035a [P] [US1] **Authorization-boundary test** in `packages/payments/src/authz.test.ts` (FR-038) — shopper A can reach neither shopper B's checkout session (initiation), nor her orders (`readPaymentState`), nor her session summary; every refusal is `not_found`, never `forbidden`. Covers the anonymous-visitor subject too, since 025 re-homes a cart mid-checkout
- [X] T035b [P] [US1] Rate-limit test in `packages/payments/src/ratelimit.test.ts` (FR-039) — initiation over the limit returns `rate_limited`; a limiter **outage** fails **open** and payment proceeds (the 022 posture — a Redis blip must not stop the platform taking money)

### Implementation for User Story 1

- [X] T036 [US1] Leg selection in `packages/payments/src/legs.ts` (FR-011, research **D14**) — the expected fulfilment window is `MAX(listing.dispatch_lead_days_max)` over the segment's `order_line → listing_variant → listing`, read through `@charlie/db` (**no `@charlie/listing` dependency, no new column**); compare it against `payments.fulfillment_window_threshold_days` and the hold window from settings; record the chosen leg on the attempt. **All-NULL ⇒ the default leg** (FR-011a) — and since nothing populates that column until 042, this is the dominant branch, not the fallback
