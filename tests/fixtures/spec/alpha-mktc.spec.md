<!-- fixture: synthetic, modelled on a real Spec Kit 0.7.1 spec.md (header lines + Clarifications excerpt; '### Session 2026-09-27 (operator answers)' suffix; bold '- **Q: …** → **A: …**'; a non-session '### Derived …' h3) -->
# Feature Specification: Marketplace tab ⇄ Alpha catalogue — one list, shared weight, bulk edit

**Feature Branch**: `feature/spec-MKTC-marketplace-catalogue-link` (stacked on the unmerged `feature/spec-MKT-marketplace-inventory`)
**Created**: 2026-09-27
**Status**: Draft
## Clarifications

### Session 2026-09-27 (operator answers)

- **Q: Which catalogue variants appear on the Marketplace tab?** → **A: Every variant, whatever its product's or its own
  status** (draft, active or archived), as the Stock tab lists them (FR-002). Draft and archived variants appear with
  their status shown (FR-007).
- **Q: Who may see landed costs and margins, now that they sit next to every catalogue product?** → **A: As today:
  inventory authority**, meaning the admin, catalogue and orders roles (FR-003). This answers spec-MKT's open
  orders-role question (`docs/post-spec-MKT-cleanup.md` §2) with "yes", and it needs no change to authority.
- **Q: Can weight be set in bulk from the Marketplace tab?** → **A: Yes, on every selected row of the default view**,
  including "not listed" (store-only) and "delisted" variants. It sets the catalogue weight on variant rows
  and the item's own weight on not-linked rows (FR-026, FR-029). Clearing is not a bulk value, and the confirmation says
  that store catalogue data will change (FR-028).

### Derived from the operator brief and the match ledger (2026-09-27) — spec author's reading, not operator answers

- **Which way does the reversal of D-6 run?** → **One way only.** The Marketplace tab reads the catalogue (name, SKU,
  photo, weight, status). It may change a variant's weight, through the catalogue's own recorded change path, and nothing
  else. No store function reads marketplace data. D-6's other half, no link into orders, fulfilment, stock lots,
  payments, receipts or analytics, stands.

  The operator's 2026-09-26 decisions (`docs/marketplace-inventory-plan.md` §5) kept D-1/D-6's isolation: "no foreign
  keys into the order or stock tables; no Alpha code reads them". The same section's table design carried an optional,
  display-only catalogue variant pointer, used for the catalogue photo (plan §6; spec-MKT Clarification and User
  Story 7). spec-MKT research R-13 dropped even that. This feature restores the link and widens it to name, photo and a
  weight write path.
- **How are links applied on an instance other than dev?** → **By SKU on the catalogue side and by source row on the
  marketplace side.** The map's record identifiers exist only on dev. The map is extended with each item's source row
  before it is applied, and names alone never link (spec-MKT edge case "Duplicate names").
- **Which marketplace item keeps the KESTREL variant?** → **The canonical one** (brand line "KESTREL", source row 103).
  The duplicate (source row 104) stays not linked. Retiring or correcting it is the operator's call (match ledger).
- **May one variant have several marketplace items?** → **No: one to one.** The duplicate KESTREL line is the only live
  case, and it is a sheet error, not a second listing.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - One weight per variant (Priority: P1)

A catalogue variant gains a **weight per unit**. The operator sets it in the product editor, and the Marketplace tab's
weight cell shows and edits the same value (User Story 3). A change in one place shows in the other on the next view.
