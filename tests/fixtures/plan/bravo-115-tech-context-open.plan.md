<!-- fixture: DERIVED from plan/bravo-115.plan.md (synthetic); edit: a Technical Context field value still carries NEEDS CLARIFICATION -->
# Implementation Plan: Test Seams That Block Re-testing, and the Payout Maker-Checker Rule

**Branch**: `feature/115-test-seams-payout-maker-checker` | **Date**: 2026-11-08 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/115-test-seams-payout-maker-checker/spec.md`

## Summary

Four parts from plan package P4. **DEC-21** (production): record who revealed each payout in a new
append-only table, written in the reveal's own transaction, and refuse a transfer recorded by any
revealer with a new 409 `payout_revealer_cannot_record`. The check runs under the payout's lock and
before the replay path, so a refusal writes nothing (R-01 to R-03). **DEC-08**: the SMS stub
keeps each sent message's locale and rendered body, and its existing dev controller gains a read
(R-04). **OBS-112**: a new dev controller drives the payout-destination stub per seller
(`missing`, `wallet_only`, `usable` at a revision), registered only in development and test (R-05).
Planning found the spec's "fail-closed" kind targets a method v1 never calls, so it is dropped
(R-06). **DEC-03**: the device-check runbook funds a wallet through the local gateway mock, which
signs the provider's webhook format, and the launch plan's stale sentence is corrected (R-07).

## Technical Context

**Language/Version**: TypeScript (strict), Node.js 24 LTS; Next.js 16.4 for the console.

**Primary Dependencies**: NestJS 11, Drizzle ORM and drizzle-kit, Zod 4 via `nestjs-zod`,
next-intl. No new dependency.

**Storage**: NEEDS CLARIFICATION (one new table or a column on `settlement_payouts`?). PostgreSQL 18. One new table, `payout_destination_reveals` (migration `0115`). The two
stub changes are in memory.

**Testing**: Jest unit and e2e (`apps/api`, real Postgres), Vitest (`apps/admin`), and a run of the
rewritten runbook step.

**Target Platform**: the API server and the ops console. The dev routes exist only on development
and test servers.

**Project Type**: web service + web console (monorepo).

**Performance Goals**: none new. The transfer check adds one indexed lookup inside a transaction
that already locks the payout.

**Constraints**: a refusal writes nothing (FR-004); the person check precedes the replay (Q2); no
account identifier enters a non-production stack (FR-016); no body or bank detail in any log
(SC-005); the dev routes never exist in production (FR-011, FR-019).

**Scale/Scope**: one table, one error code, one service change in each of reveal and transfer, one
changed stub, two dev controllers (one new), console vocabulary and copy, three documents, and the
register and catalogue rows.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Check | Result |
| --- | --- | --- |
| §I stack | No new dependency or layer; Drizzle owns the migration | Pass |
| §II no business constants | No tunable value involved; the rule is mechanism | Pass |
| §II no floats for money | No money arithmetic added | Pass |
| §IV contract | The new error code is added to `openapi.yaml` and the shared list. The dev routes stay out of the contract, as the existing stub controls do | Pass |
| §IV idempotency | The confirm route keeps its keys. A refusal is decided, so it is replayed as the same refusal (FR-005). Dev routes use `@SkipIdempotency()`, as the stub's do | Pass |
| §V money, ledger | The refusal precedes every ledger, vault and outbox write. No posting changes | Pass |
| §V audit | The reveal's audit event is unchanged and still written in the same transaction as the new row | Pass |
| §VII tests | DEC-21 touches a money action, so it gets explicit e2e tests for the refusal and the writes it prevents | Pass |
| §VIII migrations | Drizzle-generated, with the append-only triggers hand-appended (MIGRATION_PATTERNS §10) | Pass |
| §IX privacy | The new table holds pseudonymous refs only. Bodies are kept only on development and test stacks, which hold synthetic data, and are never logged | Pass |
## Complexity Tracking

No constitution violations.
