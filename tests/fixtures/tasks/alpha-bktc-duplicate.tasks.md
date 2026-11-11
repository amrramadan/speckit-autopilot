<!-- fixture: synthetic, modelled on a real Spec Kit tasks.md excerpt (a duplicate ID kept from the real file: T129 [US5] and T129 [US1]) -->
# Tasks: Bank-transfer collection as a live checkout payment method

**Input**: Design documents from `/specs/spec-BKTC-transfer-checkout/`
**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md),
[data-model.md](./data-model.md), [contracts/](./contracts/), [design-baseline.md](./design-baseline.md)

**Tests**: included. The spec's success criteria are behavioural (SC-006 "zero occurrences", SC-007 "no
order dispatched unpaid"), and research found several existing paths that write money state with **no
history and no audit** — those need a regression net, not a review comment.

## Format: `[ID] [P?] [Story] Description`

## Phase 10: Filed follow-ups (2026-08-22 sweep)

Found by a four-source sweep — the cleanup doc, every implementation record in this file,
- [X] T126 [US5] Build the ambiguous-credit attribution dialog. design-baseline §5 reserves a dialog for exactly one case — "attributing an ambiguous credit, where the operator is choosing between candidates and needs them side by side" — having rejected a Sheet-per-row for everything else. T094 covers settle / close-as-unpaid / extend as inline row actions; **nothing covers the candidate-comparison surface**, which is the one place FR-039's one-credit-one-collection guarantee is exercised by a human under real ambiguity
- [X] T127 Verify the **waiting page, the T130 Payment-methods page, and both US5 admin surfaces** at HE/EN × 375/768/1024/1440. **The Payment-methods page was added to this task by T130** — it shipped with a design pass (baseline §8) and no browser verification, and its flag notice + switch rows + confirm dialog are exactly the kind of thing a static render cannot check. T030 discharged this for the checkout picker only, and it found two defects no suite could see — including one that reproduced in EN and never in HE. The surfaces added since (`transfer-waiting.tsx`, `transfer-instructions.tsx`, and T092/T097 when they land) have had no equivalent pass, and the copyable trio is the highest-risk of all of them: it is `dir="ltr"` content inside an RTL page, wrapping long values with `break-all`
- [X] T129 [US5] **A reversed bank-transfer sale files no tax credit note, and cannot.** Found while discharging T055's second half. `refundPayment` (`packages/payments/src/refund.ts:171-177`) enqueues `tax.file_credit_note` keyed off a **`payments` row**; a bank-transfer order has none — `acceptTransferOrder` writes no payment at all, and `db/schema/payments.ts`'s provider CHECK cannot express `'bank_transfer'` (migration `0075` declined the widen deliberately). So an order that settles through this rail, files its receipt, and is then cancelled or refunded leaves a filed receipt standing against a reversed sale, with **no error and nothing in any queue**. This is statutory, not cosmetic. It belongs with **T095** (the paid-after-cancellation exception), which is the only other task that reasons about money arriving on a cancelled bank-transfer order — decide there whether the reversal files a credit note against `receiptDedupeKey(orderId)` directly (no refund row needed) or whether this rail gets a `payments` row after all, which is a migration plus a re-reading of `uq_payments_order` and every consumer that assumes one payment means one gateway. **Do not close T095 without answering this**; the receipt is already filed by then
> fill.

- [X] T128 Tell an operator that a **held** collection does not block dispatch. T044 made that choice deliberately — held means a credit was observed and a person is reviewing it, so freezing fulfilment would turn a two-minute check into a delivery delay — but it exists only in a code comment and a task record. The parcel can leave while the money is unresolved, and the person who has to decide whether to let it needs to know that the system will not decide for them. Belongs in T109's runbook and beside the held state on the T092 queue

### 10e — The method-availability settings substrate

- [X] T129 [US1] Write a **lockstep test** at `packages/payments/tests/method-availability-lockstep.int.test.ts` asserting bidirectional agreement between `OFFERABLE_PREPAID_METHODS.map(methodAvailabilityKey)` and the seeded `checkout.method_available.%` rows in `db/seed/checkout.ts`, that every one is seeded BLANK, and that the seeded set is non-empty (an unseeded DB must not pass vacuously). **This is the exact hazard T012 was written for, applied to the wrong key family**: T012 guards `transfer.*` only, and `apps/storefront/tests/checkout/method-availability.test.ts` stubs `@acme/core/settings` with an in-memory Map, so it can never see the drift. Rename the key prefix or add a sixth offerable method and `getPrepaidMethodAvailability` reads a key with no row, fails closed, and **card and wallet go permanently inert with nothing thrown and nothing logged** (FR-005/FR-003). Home it in `@acme/payments`, not the `@acme/collections` T012 suite — the key family belongs to payments
