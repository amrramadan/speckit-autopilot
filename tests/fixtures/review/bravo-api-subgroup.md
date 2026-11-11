<!-- fixture: synthetic, modelled on a real code-review summary (orchestrator summary excerpt: "### Blockers" with a "**API**" group label, then a "- **…**" finding with nested sub-bullets and an indented "**Fix:**" paragraph; long lines truncated with '…') -->
## Review: `fix/p9-api-polish` vs `main` (HEAD `b71c3e05`)

I found one thing to fix before shipping: a crash at the wrong moment can leave a booking stuck for good. Everything else is stale comments or questions for you. No database migrations or pipeline files changed, so only…

### Blockers

**API**
- **A booking can get stuck if the process dies when the deposit charge runs out of attempts** (OBS-212) — `apps/api/src/modules/rental/rental-saga.worker.ts:388-392`. The step is marked `failed` in one transaction, and…
  - The restarted job refuses to run, because `reserveStep` only runs a `pending` step (`rental-saga.repository.ts:204`).
  - The recovery sweep only picks up an `in_progress` step (`rental-recovery.worker.ts:131`).

  So the booking stays `active` at `charge_deposit`, unpaid, and nobody is told. Before this change the step stayed `in_progress`, and the spec-074 recovery restarted it and sent it to compensation.

  **Fix:** write the `failed` status and the compensating flag in one transaction, which `setCompensating` already opens with the saga row locked.

  The reviewer said the receipt and handover steps already do this in one transaction. They don't: `:912-922` and `:954-959` have the same two-transaction shape, so those steps already had this gap. The difference is…

### Nits
