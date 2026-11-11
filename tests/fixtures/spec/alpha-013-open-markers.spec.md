<!-- fixture: synthetic, modelled on a real Spec Kit 0.7.1 spec.md (front matter, Edge Cases and Assumptions excerpts; two unresolved NEEDS-CLARIFICATION question markers in Edge Cases and one bare bracketed reference in Assumptions) -->
---
spec: spec-013
slug: promotions
title: "Promotions — Coupons, Free Gifts & Bundles"
linear:
  issueId: ENG-412

---

### Edge Cases

- **Stacking**: What happens when a shopper applies a coupon to a cart that already qualifies for an automatic free gift or a bundle/BOGO? See [NEEDS CLARIFICATION: stacking policy — is at most one manual coupon allowed per order, and may automatic promotions (free gift, bundle/BOGO) stack on top of a coupon, or is the cart limited to a single promotion of any kind?].
- **Total-composition order vs free-shipping threshold and handling fee**: In what order are merchandise discount, free-shipping-threshold evaluation, shipping fee, and handling fee composed? See [NEEDS CLARIFICATION: is the free-shipping threshold (spec-014, SHIP-03) evaluated against the merchandise subtotal *before* or *after* a coupon discount, and is the handling fee (spec-012, PAY-07) computed on the pre- or post-discount payable?].
- A percentage coupon that would discount more than the merchandise subtotal must floor the payable at zero, never negative.
- **SC-007**: The composed order total — discount, shipping (incl. free-ship threshold), and handling fee — matches the defined order of operations in 100% of priced orders, with the displayed total equal to the amount charged.

## Assumptions

- **Single manual coupon per order (pending confirmation)**: As a working default, at most one coupon code may be applied per order; automatic promotions (free gift, bundle/BOGO) may coexist with it. The definitive stacking policy is a [NEEDS CLARIFICATION] item in Edge Cases and is the subject of `/speckit.clarify`.
- **Total-composition default (pending confirmation)**: As a working default, a coupon discount applies to the merchandise subtotal; the free-shipping threshold (spec-014, SHIP-03) is evaluated against the *post-discount* merchandise subtotal; the handling fee (spec-012, PAY-07) is computed on the resulting payable and added last. This default is flagged for confirmation in Edge Cases.
- **Validity timezone**: Coupon validity windows are evaluated in a single store timezone (America/Chicago) and stored/compared in UTC, not the shopper's device clock.
