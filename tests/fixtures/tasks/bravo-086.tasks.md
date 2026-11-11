<!-- fixture: synthetic, modelled on a real Spec Kit tasks.md excerpt (multi-line tasks: the test file is named in an indented sub-bullet, not on the task line) -->
# Tasks: Invoice-Sync Deferred Mode

**Branch**: `feature/086-invoice-sync-deferred-mode`
**Input**: `/home/dev/src/bravo/specs/086-invoice-sync-deferred-mode/`
**Sources**: [spec.md](./spec.md), [plan.md](./plan.md), [research.md](./research.md), [data-model.md](./data-model.md), [API contract](./contracts/api.md), [export format](./contracts/awaiting-invoices-export.md), [quickstart.md](./quickstart.md).

**Tests**: Included. Every story has an Independent Test, and constitution §VII requires explicit
tests for the money ledger: the fee reversal on a cancel and the unwind are money paths, so every
such case asserts that the wallet, `insurance_reserve`, `vat_payable`, `customer_deposit_held` and
`bravo_revenue` net to zero for the booking.

- Write each behavioural test before its implementation, and capture a meaningful red. A missing
  import is not a red: when a test targets a module that does not exist yet, the test task creates
  the module as a **stub** that compiles and returns a wrong value.
- **Every new refusal or rule gets a planted-defect control.** Copy the file, plant the defect,
  watch the named test fail, restore with `cp`, and check with `cmp`. Never restore with git
  (Verification Convention 2). Record each run in `verification.md`: the file, the defect, and the
  failing test names.

**Paths and execution**:
## Phase 1: Setup

- [X] T001 Record the baseline in a new `specs/086-invoice-sync-deferred-mode/verification.md`.
  - Confirm the branch is `feature/086-invoice-sync-deferred-mode` and that `git diff --cached` holds
    only this spec's files.
  - Run, and record the pass counts and durations of:
    - `pnpm --filter @bravo/api test`;
    - `pnpm --filter @bravo/admin test`;
    - `pnpm --filter @bravo/web test`;
    - `pnpm --filter @bravo/shared-types contract:check`;
    - the API e2e suites this spec changes, one at a time on a fresh database:
      `booking-compensation`, `booking-dispatch-gate`, `booking-happy`, `invoice-sync-c2c-clearance`,
      `invoice-sync-clearance`, `invoice-sync-submit`, `invoice-sync-factory`, `delivery`, `tracking-decision`,
      `attempt-booking-window`, `ops-auth`.
  - Note any failure that exists on `main`, and the UTC time of each run, so later runs are not
    blamed for it.

  **Commit:** with T002.

## Phase 2: Foundational prerequisites

- [X] T002 [CAT] The contract delta and the new error code (contracts/api.md).
  - `packages/shared-types/openapi.yaml`:
    - `OpsSurface` gains `awaiting_invoices` (in its `OPS_SURFACES` position, after `settlement`);
    - new `GET /v1/ops/awaiting-invoices` with the strict query (`merchant_ref`, `from`, `to`,
      `page`, `page_size`), `200 {data: OpsAwaitingInvoice[], meta: PageMeta}`;
    - new `GET /v1/ops/awaiting-invoices/summary`, `200 {data: OpsAwaitingInvoiceSummary}`;
    - new `GET /v1/ops/awaiting-invoices/export`, `200 text/csv` (`type: string`), errors as
      `ErrorEnvelope`;
    - schemas `OpsAwaitingInvoice` (six required fields, money as `^\d+$` strings) and
      `OpsAwaitingInvoiceSummary` (`count` integer and three money strings);
    - `PATCH /v1/ops/settings/{key}` documents `409 dispatch_hold_refused_deferred`;
    - `POST /v1/bookings` and `POST /v1/bookings/batch` document `409 dispatch_hold_refused_deferred`;
    - `POST /v1/tracking/{token}/cancel`'s description says the in-window cancel returns the
      merchant's fee once (T57).
  - Error code: add `dispatch_hold_refused_deferred` to the yaml `ErrorCode` enum and to
    `packages/shared-types/src/schemas/errors.ts` `ERROR_CODES`, with a docblock naming spec-086.
  - Catalog: `errors.dispatch_hold_refused_deferred`, en first ("holding parcels until the
    invoice clears is not available while invoice sync is deferred"). Re-measure the pin
    (3489 → 3490).
  - Regenerate with `pnpm --filter @bravo/shared-types contract:generate`, then run
    `contract:check`, and typecheck `api`, `admin` and `web`.

  **Commit:** `feat(shared-types): spec-086 contract for the awaiting-invoice desk and the deferred hold refusal`,
  with T001's `verification.md`.

- [X] T003 [P] The mode gate (research D-01).
  - New `apps/api/src/core/config/invoice-sync-mode.ts`, on the `tracking-base-url.ts` pattern:
    - `type InvoiceSyncMode = 'standard' | 'deferred'`;
    - `loadInvoiceSyncMode(env = process.env): InvoiceSyncMode`: unset or empty → `standard`;
      `deferred` → `deferred`; anything else → throw `InvoiceSyncModeInvalid`; `deferred` with all three
      of `TAX_API_BASE_URL`, `TAX_API_CLIENT_ID`, `TAX_API_CLIENT_SECRET` set → throw `InvoiceSyncModeInvalid`. Each
      message names `INVOICE_SYNC_MODE` and never echoes the value;
    - `currentInvoiceSyncMode()`: the memoized answer for the process, plus a test-only
      `resetInvoiceSyncModeForTests()`;
    - an injection token `INVOICE_SYNC_MODE` with a provider `{ provide: INVOICE_SYNC_MODE, useFactory: currentInvoiceSyncMode }`
      that `BookingModule` registers (T007 consumes it).
  - `apps/api/src/main.ts`: call `loadInvoiceSyncMode()` beside `resolveTrackingBaseUrl()` and log
    `invoice_sync.mode=<mode>` through the bootstrap logger.
  - Test `apps/api/src/core/config/invoice-sync-mode.spec.ts`: unset, empty, `deferred`, an unknown
    value (refused; message contains `INVOICE_SYNC_MODE` and not the value), `deferred` + full tax-API
    config (refused), `deferred` + partial tax-API config (accepted), and that the memo returns the same
    answer until reset.
  - Wiring test (analysis C10): a source-scanning case in the same spec (the
    `route-authorization.spec.ts` approach) reads `apps/api/src/main.ts` and asserts that
    `loadInvoiceSyncMode(` is called before `NestFactory.create(`, so the gate cannot be dropped from
    start-up silently.
  - Planted control: accept an unknown value as `standard`, and watch its case fail.

  **Commit:** `feat(api): spec-086 INVOICE_SYNC_MODE start-up gate`.
