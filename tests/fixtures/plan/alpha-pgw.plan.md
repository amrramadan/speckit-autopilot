<!-- fixture: synthetic, modelled on a real Spec Kit plan.md (header + '## Technical Context' + '## Constitution Check' blocks; '### NEEDS CLARIFICATION — resolved in Phase 0' inside Technical Context) -->
# Implementation Plan: Card gateway activation — the real payment adapter behind the spec-011 seam

**Branch**: `feature/spec-PGW-gateway-activation` | **Date**: 2026-09-03 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/spec-PGW-gateway-activation/spec.md`

## Technical Context

**Language/Version**: TypeScript 5.x (strict), Node.js 24 LTS
**Primary Dependencies**: **None new** (Protocol §12 does not fire — SC-011 satisfied by its first
branch). Platform `fetch` + `node:crypto`. Reuses `@acme/money` (`toMinorUnits`/`fromMinorUnits`),
`@acme/payments` (`settlePayment`/`createOrderFromCart`/`refundPayment`, unchanged), `@acme/core`
(`isFlagEnabled`, `getSetting`, `getIntegrations`, `logger`/PII-scrub), `@acme/integrations`
(`GatewayClient` contract + stub), `@acme/audit`, `@acme/db`, `@acme/rbac`, `@acme/ui`.
**Storage**: PostgreSQL 18 — **one additive nullable column** + **one additive defaulted column**;
migration `0094_spec_pgw_merchant_reference.sql` (tip `0093`). Schema review ×2.
**Testing**: vitest; `fast-check` (already a dev dep) for the SC-004 minor-unit property test; a
**golden captured sandbox callback** fixture for FR-007/SC-002. The suite stays offline (Protocol §14).
**Target Platform**: Next.js 16.3 App Router — one existing route handler (`api/payment/gateway/callback`),
one new admin RSC surface (FR-032), Server Actions for the staff status check (FR-033).
**Project Type**: Monorepo — `packages/integrations/adapters/` + `packages/core` binding + `apps/admin`.
**Performance Goals**: bounded outbound timeout on every provider call, fail-closed (FR-006).
**Constraints**: PCI **SAQ-A** — no card data, no `card_*` column, masked PAN read-then-discarded
(FR-019, research R-10). Settlement exactly-once under concurrency (FR-010).
**Scale/Scope**: 4 user-story slices; US1+US2 ship together as one indivisible slice.

### NEEDS CLARIFICATION — resolved in Phase 0

| Spec Open Question | Resolution | Where |
|---|---|---|
| Q1 — merchant order reference | **`merchant_reference`**, persisted in a new column; `checkout_session_id` → existing `gateway_order_ref` | research R-5 |
| Q2 — one refund method or two | **Two.** `void()` joins `refund()`; `@acme/payments` chooses from our own settlement state | research R-6 |
| Q3 — hosted page vs embedded fields | **Hosted page (redirect)** — matches the shipped `hostedUrl` seam; zero storefront change | research R-7 |
| Q4 — methods in first activation | **Card alone.** Every other method's integration id is an account fact gated on S0-4 | research R-4 |
| Q5 — reconciliation scheduled or on-request | **On request** at launch volume; no scheduler surface added | spec Q5, unchanged |
| Q6 — bank-transfer method through the gateway | **Still open — operator/ADR-010.** Does not block US1–US3 | gate table |

### Residual unknowns — sandbox-gated, NOT blocking the plan

U-1…U-7 in `research.md`: exact endpoint paths, the checkout URL template, the golden payload, the
`merchant_reference` collision behaviour, the sandbox/live key discriminator, the error vocabulary.
All are **values behind the adapter**, reached only with the flag on and credentials present.
**Binding rule**: an `[UNCONFIRMED]` value may enter the adapter only in the same commit as the
sandbox capture confirming it.

## Constitution Check

*GATE: evaluated before Phase 0 and re-evaluated after Phase 1 design.*

| § | Principle | Verdict | Evidence |
|---|---|---|---|
| **I** | Technology stack | ✅ PASS | No stack change. |
| **II** | Code quality / TS strict | ✅ PASS | No `any`; provider JSON parsed through Zod at the boundary. |
| **III** | **External Boundary Contract** | ✅ PASS | The adapter lands at `packages/integrations/adapters/`, business logic keeps importing the interface. This spec **is** §III's activation half. The one interface change (`void()`) is recorded as a contract delta, not made quietly — as the spec's own Assumptions required. |
| **IV** | UI & CSS discipline | ⚠️ **GATED** | FR-032's stale-payment queue is a new admin surface ⇒ UI design pass **mandatory**, output at `design-baseline.md`, enforced by the `require-design-baseline` hook. Logical CSS only. |
| **V** | Database discipline | ✅ PASS | Drizzle-only; two additive columns; schema review ×2; no NOT-NULL ALTER on un-defaulted populated data (Protocol §6). |
| **VI** | Feature flags | ✅ PASS | `spec-pgw.real_gateway_adapter`, default OFF everywhere. Relationship to the pre-existing `spec-011.gateway_iframe` / `spec-011.refunds` pinned in research R-12. |
| **VII** | DevOps | ✅ PASS | No infra change; sandbox needs a tunnel, the suite needs nothing. |
| **VIII** | **Security & compliance** | ✅ PASS **with named controls** | `timingSafeEqual` (never `===`); idempotent webhooks; **PCI SAQ-A** — no `card_*` column, masked PAN read-then-discarded; three credentials from env, scrubbed from logs; Zod at the boundary; rate-limited endpoint (FR-034). SECURITY_PATTERNS §4/§5/§9/§19/§20 all load. |
| **IX** | **No business-rule constants** | ✅ PASS | Integration ids, staleness window, rotation overlap, base URL ⇒ `store_settings`; launch-gate values **UNSET**, unset fails closed. Timeouts/TTLs are protocol timers, explicitly out of §IX's scope. |
| **X** | Development workflow | ✅ PASS | Feature branch created; atomic commits; tracker issue ENG-1300. |

### Protocol checks

| § | Rule | Verdict |
|---|---|---|
| §5 | Scope discipline — adapter **activation** is never autonomous | ✅ Honoured. US1–US3 build + sandbox-verify. **US4/US5 are operator acts**; the flag stays OFF; live keys are never bound by this session. |
| §6 | No NOT-NULL ALTER on populated data | ✅ Column 1 nullable; column 2 `NOT NULL DEFAULT 'refund'` where the default is the correct historical value. |
| §12 | Currency awareness | ✅ **Does not fire** — no dependency added. |
| §13 | No business-rule constants | ✅ See §IX. |
| §14 | Stub-and-activation | ✅ The adapter is the pattern's second half. Stub stays the suite's only dependency. |
| §15 | Feature flag discipline | ✅ Registered in `FLAG_CATALOG`, OFF in production. |
| §16 | Cap atomicity | ✅ Settlement keeps `SELECT … FOR UPDATE` + the unique-txn ledger; SC-003 proves it **under concurrency**, not sequentially. |

**Gate result: PASS.** One conditional (§IV) with a named, enforced remedy. No violation requiring
justification ⇒ Complexity Tracking is empty.
