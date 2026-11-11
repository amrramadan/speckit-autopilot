<!-- fixture: synthetic, modelled on a real Spec Kit 0.7.1 research.md (prose headings that mention the bare bracketed marker, e.g. 'resolves spec ... #1': mentions, not markers) -->
# Phase 0 Research — spec-013 Promotions

**Date**: 2026-06-17 · **Branch**: `feature/spec-013-promotions`

This resolves the spec's open questions and records the substrate decisions the plan builds on. Every "Decision" below is grounded in code read on `main` (spec-010/011/012 merged) or in the binding docs (Constitution, DB_PATTERNS, SECURITY_PATTERNS, Implementation Protocol).

---

## R-1. Stacking policy *(resolves spec [NEEDS CLARIFICATION] #1)*

- **Decision**: At most **one manual coupon per order**; **automatic promotions (free gift, bundle/BOGO) may stack on top of a coupon**. The "max coupons per order" is itself an admin-tunable setting (`promotions.max_coupons_per_order`, default `1`) so the policy can change without a release (§IX).
- **Rationale**: Matches the documented spec default (Option A) and standard e-commerce behaviour; keeps the coupon path single-discount (simple to reason about for margin) while still allowing supplier-funded gifts/bundles to run during a coupon campaign. Making the cap a setting avoids hardcoding the rule.
- **Operator-confirmable**: A change to "single promotion of any kind per cart" (Option B) would alter `applyPromotions` composition logic and the storefront messaging only — no schema change. Flagged for `/speckit-clarify`; the plan proceeds on Option A.
- **Alternatives**: (B) one promotion of any kind — rejected as default (too restrictive for funded campaigns); (C) coupon+gift stack but bundle exclusive — deferred, reachable by setting per-type exclusivity later.

## R-2. Total-composition order *(resolves spec [NEEDS CLARIFICATION] #2)*

- **Decision**: `grand_total = max(0, subtotal − discount_total) + shipping_total + handling_fee`. The coupon discount applies to the **merchandise subtotal**; the free-shipping threshold (spec-014, SHIP-03) is evaluated against the **post-discount** subtotal; a free-shipping coupon sets `shipping_total = 0`. The payable is **floored at zero** (a fixed-amount coupon can never produce a negative total — preserves the existing `orders_total_nonneg_check`).
- **Handling fee note**: the handling fee is a **flat configured Money value** (`store_settings.handling.fee`, read by `getHandlingFee`), not a percentage — so "fee on pre- vs post-discount payable" is moot: it is added as a constant after the discounted subtotal. Confirmed by reading `packages/payments/src/handling.ts` + `handling-settings.ts`.
- **Rationale**: Conservative for margin (a coupon can drop a cart below the free-ship line), and it keeps the existing invariant in `db/schema/orders.ts` (`grand_total = subtotal + shipping_total + handling_fee`) intact by introducing `discount_total` as a subtraction term. Mirrors the spec's documented default (Option A).
- **Operator-confirmable**: Option B (free-ship threshold on the *pre-discount* subtotal) changes one comparison in the free-ship evaluation (spec-014 consumes it) — no schema change. Flagged for `/speckit-clarify`.

## R-3. Total-composition seam — extend `computeOrderTotals`
