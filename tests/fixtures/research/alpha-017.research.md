<!-- fixture: synthetic, modelled on a real Spec Kit 0.7.1 research.md (backticked mention of the bare bracketed marker: 'no ... survived the spec') -->
# Phase 0 Research — spec-017 Manual / Draft Orders

All decisions below are resolved (no `[NEEDS CLARIFICATION]` survived the spec). Format: **Decision / Rationale / Alternatives**. Grounded in the live codebase map (signatures verified in `packages/payments`, `packages/orders`, `packages/reconciliation`, `packages/inventory`, `packages/identity`, `apps/admin`).

---

## D-1 — A manual order *is* an `orders` row in `status='draft'`, not a separate entity

**Decision**: Model the draft as a real `orders` row (`status='draft'`) with its `order_items`, reusing the order entity end to end. There is no `manual_order_drafts` table.

**Rationale**: `OrderStatus` already carries `'draft'` upfront — the schema comment literally reserves it for "manual/phone intake (spec-017)". `order_state_history` and every needed column (`is_guest`, `contact_phone`, `address_snapshot`, `gift_message`, money columns, `invoice_status`) already exist. Making the draft the order means confirmation is a single in-place FSM transition (`draft → confirmed`) and "indistinguishable downstream" is true by construction. Drafts hold no stock and no payment obligation (FR-004) because nothing allocates or creates a payment until confirm.
