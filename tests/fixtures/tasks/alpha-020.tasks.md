<!-- fixture: synthetic, modelled on a real Spec Kit tasks.md excerpt ('[~]' gated tasks + legend lines '- `[X]` done · `[~]` gated · `[B]` blocked') -->
---
description: "Task list for spec-020 store-credit wallet"
---

# Tasks: Store-Credit Wallet (spec-020)

**Input**: Design documents in `specs/spec-020-store-credit-wallet/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/wallet-interface.md, quickstart.md
**Branch**: `feature/spec-020-store-credit-wallet`

**Tests**: Included — this project's constitution (§II, vitest + ≥85%) and the quickstart scenario→test matrix require integration tests; the cap-atomicity concurrency test is a binding acceptance criterion (Protocol §16).

**Two procedural gates** (do not bypass): **Schema Council ×2** review of migration `0023` (T010) before the schema merges; **ui-ux-pro-max** design pass before each surface's component code (T021, T033, T041).

## Format: `[ID] [P?] [Story] Description`

- **[P]** = parallelizable (different files, no incomplete-task dependency)
- **[USx]** = user story; commit convention `<type>(spec-020): T0NN <subject>`

### Status legend & implementation note (2026-06-26)

- `[X]` done · `[~]` gated (CI / operator / dev-env) · `[B]` **blocked on an open question** · `[ ]` not started.
- **US1 (MVP) + US3 are complete end-to-end; US2 checkout redemption is wired for the COD path.**
- **T027–T032 DONE for the COD checkout path** (migration `0024`): Open Question 4 was resolved by
  `/speckit.clarify` (2026-06-26 — tender on full `grand_total`, FR-015; fully-covered orders settle via a
  captured `store_credit` payment, FR-027). The redemption tender is wired into `acceptCodOrder`/
  `acceptCodForOrder`, `collectCash` expects the residual cash, `cancelOrder` reverses the redemption, and
  the checkout "apply store credit" control + Server Action ship behind `spec-020.wallet_redemption`.
  **The PREPAID/card residual interaction is deliberately deferred** (operator scope decision 2026-06-26 —
  `spec-011.card_iframe` is default-OFF; the residual-via-card-session change is a follow-up). Full
  rationale + insertion points: `docs/post-spec-020-cleanup.md`.
- `[~]` gated: **T005** Unleash registration (operator) · **T010** Schema Council ×2 (PR review) ·
  **T040** ≥85% coverage (CI w/ Docker PG — pure logic unit-covered in-session) · **T041** quickstart
  walkthrough (dev env, both flags ON) · **T044** final verify (typecheck/lint/runnable tests green;
  `*.int.test.ts` + `drizzle-kit status` + HE/EN smoke run in CI / dev env, not the local session).

---

## Phase 1: Setup (Shared Infrastructure)

- [X] T001 Create the `@acme/wallet` package skeleton (`packages/wallet/package.json`, `tsconfig.json`, `eslint.config.mjs`) modeled on `packages/rma`; deps `@acme/audit`, `@acme/core`, `@acme/db`, `@acme/money`, `drizzle-orm`, `zod`; dev `@acme/config`, `fast-check`, `typescript`, `@types/node`; `exports` `"."→./src/index.ts`, `"./constants"→./src/constants.ts`.
- [X] T002 [P] Create `packages/wallet/src/constants.ts` — `WALLET_FLAG='spec-020.store_credit_wallet'`, `WALLET_REDEMPTION_FLAG='spec-020.wallet_redemption'`, key builders (`rmaCreditKey`/`manualCreditKey`/`redemptionKey`/`reversalKey`), settings-key constants (`WALLET_EXPIRY_DAYS_KEY`, `WALLET_MAX_REDEMPTION_PER_ORDER_KEY` — reserved, UNSET).
- [X] T003 [P] Create `packages/wallet/src/errors.ts` — `WalletError` + `WalletErrorCode` (`wallet_not_found`/`insufficient_credit`/`invalid_amount`/`no_redemption_to_reverse`).
- [X] T004 [P] Create the public barrel `packages/wallet/src/index.ts` (re-export constants, errors; stubs for writes/reads filled in later phases).
- [~] T005 Register Unleash flags `spec-020.store_credit_wallet` and `spec-020.wallet_redemption` (default OFF) in the local Unleash instance. *(Operator step — flag constants are declared in `constants.ts` and evaluate default-OFF via the fail-safe Unleash wrapper; the container toggle is Protocol §5 human turn-on. Tests drive flags via the in-memory `setFlagProvider` fake.)*

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: blocks ALL user stories. Includes the Schema Council gate.

- [X] T006 Add `store_credit_wallet` table (id, `customer_id` uuid FK→customers ON DELETE SET NULL UNIQUE, `balance` usdMoney NOT NULL default '0' CHECK ≥0, created_at/updated_at) to `db/schema/payments.ts`.
- [X] T007 Add `wallet_entries` table + `WalletEntryType` union + CHECK (`rma_credit`/`manual_credit`/`manual_adjustment`/`redemption`/`reversal`), signed `amount` CHECK ≠0, `balance_after` CHECK ≥0, FKs (`wallet_id`→store_credit_wallet RESTRICT; `rma_id`→rma_requests, `order_id`→orders, `created_by_admin_id`→admins all ON DELETE SET NULL), UNIQUE `idempotency_key`, indexes (`(wallet_id,created_at)`, partial on `rma_id`) to `db/schema/payments.ts`.
- [X] T008 [P] Add Drizzle Relations for both tables (`storeCreditWalletRelations`, `walletEntriesRelations`) in `db/schema/payments.ts`.
- [X] T009 Generate additive migration `db/migrations/0023_spec_020_store_credit_wallet.sql` via `drizzle-kit generate` (+ `meta/0023_snapshot.json` + journal); verify no ALTER on populated tables.
- [~] T010 **GATE — Schema Council ×2** paired review of migration `0023` + the two table definitions (ENG-ADR-001). Do not merge schema until two reviewers sign off.
- [X] T011 [P] [TEST] Write core tests first (must fail): `applyEntry` idempotency + in-tx audit in `packages/wallet/src/credit.test.ts`; balance-invariant property test (`balance == Σ entries`, never negative) in `packages/wallet/src/wallet.property.test.ts`; invalid (zero/negative) amount rejected.
- [X] T012 Implement `getOrCreateWallet(tx, customerId)` + the `applyEntry(tx, …)` core (`SELECT … FOR UPDATE` on the wallet row → idempotency-keyed `wallet_entries` insert → cached-balance update → `auditLog.write(tx, {action:'wallet.credit'|'wallet.debit', …})`) in `packages/wallet/src/wallet.ts` + `packages/wallet/src/credit.ts` — makes T011 pass.
- [X] T013 [P] Implement reads (`getWalletForCustomer`, `getAvailableCredit`, `listWalletEntries` → DTOs) in `packages/wallet/src/reads.ts`.

**Checkpoint**: ledger primitive + reads exist, balance invariant proven, schema reviewed.

---

## Phase 6: Polish & Cross-Cutting

- [~] T040 [P] Verify ≥85% coverage on `@acme/wallet`; fill unit gaps (`applyEntry` edge cases, DTO mapping).
- [~] T041 [P] Run quickstart.md validation end-to-end with both flags ON (manual walkthrough US1→US2→US3).
- [X] T042 [P] Create `docs/post-spec-020-cleanup.md` — record the 5 deferred open questions (tax-receipt/expiry/guest-merge/totals-VAT/RBAC + per-order cap) + the zero-due path follow-up + the `@acme/wallet` integration touchpoints.
- [X] T043 [P] Update `db/CLAUDE.md` / package `CLAUDE.md` notes for the new tables + `@acme/wallet`.
- [~] T044 Final verification: `pnpm typecheck && pnpm lint && pnpm test` + `pnpm drizzle-kit status` (tip=0023); flag both `spec-020.*` default OFF; note on-device HE/EN smoke-test pending (Protocol §10).

---
