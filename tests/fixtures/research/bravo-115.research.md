<!-- fixture: synthetic, modelled on a real Spec Kit 1.2.3 research.md (Decision / Rationale / Alternatives considered) -->
# Research: spec-115

**Date**: 2026-11-08 · **Spec**: [spec.md](./spec.md) · **Base**: `main` at `4f2e9a1c0`

Each entry records a decision, why, and what else was weighed. Code references are to `main`.

## R-01 Where "who revealed" is recorded (DEC-32)

**Decision**: a new append-only table, `payout_destination_reveals`: one row per successful reveal,
holding the payout id, the revealer's pseudonymous ops ref and the time. It is written inside the
reveal's existing transaction (`PayoutRevealService.revealPayoutBankDetails`), after the vault read
and before the audit append, so the reveal, its record and its audit entry commit or roll back
together.

**Rationale**: today the revealer exists only in the `payouts.payout_destination_revealed` audit
event. The audit chain is the tamper-evident record, not a query surface for business rules, and no
module reads it to decide anything. A row the transfer check can read under the payout's lock is the
plain answer. Several reveals of one payout are possible (two admins, or one admin twice), so the
row is not unique per payout. Append-only follows `manual_payout_transfers`: the shared
`app_forbid_mutation()` trigger, hand-appended to the migration (migration 0042's pattern).

**Alternatives considered**:

- Read the audit chain for `payouts.payout_destination_revealed` with the payout id: couples a money
  rule to the audit payload's shape and to a table no other rule reads. Rejected.
- A `revealed_by_ref` column on `payouts`: holds one revealer only, and the payout row is
  mutable, so a later reveal would overwrite the earlier one. Rejected.

## R-02 Where the person check runs (DEC-32, Q2)

**Decision**: in `ManualTransferService.confirmManualTransfer`, inside the transaction, right after
`getPayoutForUpdate` and before `findByPayout`. If the caller's ops ref has a row in
`payout_destination_reveals` for this payout, throw the new error, which rolls the transaction back.

**Rationale**: the `findByPayout` branch is the replay path. It commits a
`payouts.payout_manual_transfer_repeated` event before it answers, so checking before it means a
refused revealer writes nothing at all (FR-004) and never gets a stored success back (Q2). Holding
the payout's `FOR UPDATE` lock while reading the reveal rows closes the race with a reveal: the reveal
takes the payout `FOR SHARE`, so the two serialise, and a reveal after a recorded transfer is already
refused as `payout_not_awaiting_transfer`.
