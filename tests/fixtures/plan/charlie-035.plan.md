<!-- fixture: synthetic, modelled on a real Spec Kit 0.7.1 plan.md (header through Constitution Check + Complexity Tracking; '**PASS** —' and '**N/A — …**' cells; Complexity Tracking with design rows, no violations; Technical Context carries the project's '**Surfaces**:' field) -->
# Implementation Plan: Order FSM (core)

**Branch**: `035-order-fsm-core` | **Date**: 2026-09-13 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/035-order-fsm-core/spec.md`

## Summary

Define the order lifecycle as a concrete state machine over the `@charlie/fsm` substrate, in a new
Tier-4 package `@charlie/order`, and wire the three things that currently make the cash rail a
no-op. The trunk is `awaiting_payment → preparing → shipped → delivery_asserted → receipt_confirmed
→ completed`, with `receipt_denied`, `rto`, `refused` and `cancelled` off it. Cash collection
becomes a dated fact on the order rather than a status value, so the courier's report and the
maker's assertion cannot erase one another. The feature also lands the ENG-ADR-007 vendor-neutral
rename and deletes the two tripwires that exist to detect it happening unplanned.

Research turned up a third silent hole in the cash rail that was not known when the spec was
written: the signal the collection handler consumes is emitted only by a buyer one-time code or an
admin override, both of which ADR-080 retired as the ordinary path — so for the dominant order it
is never enqueued at all. This feature emits the interim's own trunk signal and re-homes the handler
onto it. See research R2.

## Technical Context

**Language/Version**: TypeScript 5.9.3 strict, Node.js 24.18 (24 Active LTS)
**Primary Dependencies**: `@charlie/fsm` (XState v5, confined there), `@charlie/db` (Drizzle 0.45.2), `@charlie/audit`, `@charlie/outbox`, `@charlie/flags`, `@charlie/cod`, `@charlie/shipment`
**Storage**: PostgreSQL 18 — widened CHECK on `order.status` plus two ENG-ADR-007 column renames; no new table and no new column (history reuses `fsm_transition`, the cash fact reuses `cod_order.collected_at`)
**Testing**: vitest 4.1.9, `--coverage` gate 85% on spec-owned modules; DB-backed suites skip when
no migrated Postgres is reachable
**Target Platform**: Linux server (Node runtime); no browser surface
**Project Type**: Monorepo domain package + migration + three app wiring points
**Surfaces**: none
**Performance Goals**: No new hot path — one CAS, one history insert, one audit write and N outbox enqueues per transition, inside the caller's existing transaction
**Constraints**: Every transition atomic in the caller's transaction; no money value written or computed anywhere; no business number in code
**Scale/Scope**: One new package (~10 modules), three migrations (0041 generated, 0042 hand-authored, 0043 generated in the review pass), ~45 editable files touched by the rename, 3 app wiring points

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| § | Gate | Verdict |
|---|---|---|
| I | Canonical stack; no alternatives | **PASS** — composes shipped packages only; XState stays confined to `@charlie/fsm`, this package imports `defineMachine`, never `xstate` |
| II | TS strict, no `any`, ESLint clean, vitest 85% | **PASS** — planned; no third-party boundary needing a typed adapter |
| III | Stub-and-activation; vendor-neutral boundary naming | **PASS, and this feature closes the outstanding breach** — ENG-ADR-007's rename is the last vendor-named boundary. The notification-receipt port ships declared + stubbed with no adapter, per the pattern |
| IV | RTL-first locales; mandatory design pass | **N/A — `Surfaces: none`.** Renders nothing; the seller console and buyer surface own every pixel. No `design-baseline.md` required |
| V | Drizzle sole migrations owner; varchar+CHECK enums; TIMESTAMPTZ; money `numeric(12,2)` | **PASS** — one generated migration, two reviewers (schema review); status stays varchar + TS union + CHECK; **no new column, no new table, no money column added or written** |
| VI | Flag per spec, default OFF | **PASS** — `035.order_fsm`, default OFF. Note the interaction: `003.fsm_transition` must also be ON, and carve-out 2's exemption for `026.capture_on_delivery` **ends with this feature**, which is the re-homing |
| VII | Docker-for-everything; env-only config | **PASS** — no new service, no new env var |
| VIII | Security; **the platform never holds funds**; callback verification | **PASS** — the strongest gate here and the easiest to fail. This feature writes no money value, computes no commission, instructs no courier, and adds no balance. The five bright lines are untouched. The callback-verification rework assigned to this spec by SECURITY_PATTERNS §4 is **deferred to 078** (spec Out of Scope) because its input — the provider's signing scheme — is an unstarted gate deliverable and no live callback exists |
| IX | No business-rule constants | **PASS** — both window lengths from `policy_setting` through a throwing reader; neither seeded with a value. Presentation defaults (sweep interval, batch size) are correctly *not* settings |
| X | Spec-driven workflow; branch + commit convention | **PASS** — `035-order-fsm-core`; commits `<type>(spec-035): T-NNN <subject>` |

**Cap atomicity**: no cap is enforced by this feature, so no `SELECT … FOR UPDATE` is taken and
`check-cap-atomicity.mjs` is not engaged. The exactly-once guarantee comes from the substrate's
compare-and-swap, which FR-006 requires to be demonstrated by a test **verified to fail when the
guard is removed**.

**Audit-first**: every state change routes through `transition()`, which writes the audit entry in
the same transaction. `@charlie/order` performs no drizzle write of its own outside that primitive, so
`check-audit-first.mjs` — which resolves writes from the schema identifiers a file imports — has
nothing unaudited to find.

**Post-Phase-1 re-check**: unchanged. The design adds no money path, no surface, no external
adapter and no business constant. The one item that moved is the callback rework, deferred to 078
with the owner updated in `SECURITY_PATTERNS §4` and the handover recorded in the cleanup doc.

## Complexity Tracking

> No Constitution Check violations. Two scope facts are recorded here because a reviewer will
> reasonably question them.

| Item | Why it is in this feature | Simpler alternative rejected because |
|---|---|---|
| The ENG-ADR-007 rename rides with the FSM | Assigned here by the constitution's applier's note, ENG-ADR-007, and four cleanup entries; its two tripwires must die in the same commits as the changes they guard | A separate spec leaves a vendor name on a boundary with no vendor, and splits a tripwire from its trigger — the red would then surface in a branch that cannot explain it |
