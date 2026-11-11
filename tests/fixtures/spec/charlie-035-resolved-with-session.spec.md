---
spec: 035
slug: order-fsm-core
title: "Order FSM (core) — the pay-on-delivery interim order lifecycle"
priority: P0
linear:
  issueId: ENG-731
  lastSync: 2026-09-14T16:50:00Z
---
<!-- fixture: DERIVED from spec/charlie-035-resolved.spec.md (synthetic); edit: adds a suffixed '### Session 2026-11-10 — …' under '## Resolved Clarifications' (1.2.x corpus heading variants) -->

# Feature Specification: Order FSM (core) — the pay-on-delivery interim order lifecycle

**Feature Branch**: `035-order-fsm-core`
**Created**: 2026-09-13
**Status**: Draft
**Input**: User description: "035 — Order FSM (core): placed/awaiting_payment → packing → shipped → delivery-asserted → 48h receipt window → 30-day return-window timer → completed; + return-to-sender"
## Resolved Clarifications

### Session 2026-11-10 — resolved at the pre-specify gate

- Q: Is auto-confirm in scope? → A: Yes, built inert.

**C1 — Auto-confirm ships behind an unfilled notification dependency.** The transition is built and
correct, and it never fires until the notifications feature supplies evidence the buyer was reached.
Until then every order stops at the buyer's window and waits, which is the intended reading of the
rule rather than a defect. The alternative — starting the clock when the platform *sent* a
notification rather than when the buyer *received* one — was rejected: that rule is simultaneously a
fairness obligation and, under this interim, a revenue control, because auto-confirm is what makes
a platform fee chargeable. Relaxing it would let the platform bill a seller on that seller's own
statement plus a buyer who was never reached.

**C2 — Paid is a recorded fact, not a state the order passes through.** The lifecycle carries the
trunk; payment collection is recorded as its own dated fact against the order. Two different actors
report the two facts — a carrier's confirmed delivery and the seller's own statement — on independent
timing, and a single state column would let whichever arrived second silently erase the first. As a
separate fact, both are always true and always readable, and the buyer-facing surfaces show the order
as paid from that fact. The prepaid-card paid state therefore joins the retained-unreachable set,
consistent with how the rest of the deferred payment work is treated.

**C3 — Cancellation is defined here and surfaced by the seller console.** This feature adds the
cancelled state and declares exactly which states admit it and who may cause it; the seller-facing
flow belongs to the console that owns seller actions. This is the same division as every other story
here: the lifecycle is defined once, and surfaces compose it.

## Out of Scope
