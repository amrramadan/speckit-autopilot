<!-- fixture: synthetic, modelled on a real Spec Kit /speckit.analyze report (assistant text of a second pass after fixes; optional pre-hook and post-hook blocks kept; long lines/cells truncated with '…'; coverage rows trimmed) -->
## Extension Hooks

**Optional Pre-Hook**: git
Command: `/speckit.git.commit`
Description: Auto-commit before analysis

Prompt: Commit outstanding changes before analysis?
To execute: `/speckit.git.commit`

---

## Specification Analysis Report: spec-OFFERS (second pass)

The spec is clean of CRITICAL and HIGH issues, and all 26 findings from the first pass are resolved. This pass found 6 new issues: 3 MEDIUM, 3 LOW.
- Two come from my own fixes: the "place the order again" link, and the missing source for the "counted since" date.
- One I missed the first time: the IP limit under carrier-shared IPs.

| ID | Category | Severity | Location(s) | Summary | Recommendation |
|----|----------|----------|-------------|---------|----------------|
| S1 | Security / availability | MEDIUM | research R-11; storefront-codes contract (limiter, share route); data-model §8 | The wrong-code limit counts per IP at 10 attempts per 15 minutes. Mobile carriers often share one public…| Give the IP count its own, much higher limit in a new setting (`promo_codes.attempt_limit_ip`), and keep the…|
| U1 | Underspecification | MEDIUM | spec FR-008; admin-console contract; tasks T028 | The promotions list must say usage is "counted since <date>", but nothing records when `console_v2` was swit…| Use the earliest order with a non-null `auto_promotion_id`, and show "not yet used" when there is none. |
| I1 | Underspecification | MEDIUM | spec US3 scenario 3; T066 | My H2 fix added a "Place the order again" link that rebuilds the cart. No rebuild-cart-from-order function e…| Either add a task for a cart rebuild from the order's lines (re-checking stock and prices, with tests), or l…|
| D1 | Drift | LOW | spec.md:554 (Assumptions) | "This feature adds a promotion type…". Pay-online is not a new type (plan, Key Entities). Missed in the firs…| Reword to "adds payment-method discount controls, personal coupon ownership and popups". |
| D2 | Drift | LOW | root `CLAUDE.md`, Active Technologies (spec-OFFERS line) | It lists the `orders` and `coupons` changes without `orders.auto_promotion_id` or `idx_popups_live`. | Regenerate with `update-agent-context.sh`. `CLAUDE.md` already has unrelated uncommitted edits, so it was le…|
| U2 | Ambiguity | LOW | T093a | "Confirm from the route's query log that `/promo/[code]` does one indexed coupon read" doesn't say how the q…| Name the method: the ORM's query logger in a test harness, or `EXPLAIN` on the coupon lookup. |

**First-pass findings re-checked**

| Finding | Status |
|---|---|
| H1 | e2e paths now point under `apps/*/e2e/`; no root `e2e/` reference remains. |
| H2 | US3 scenario 3, FR-015, the plan, and T064 and T066 all say the order stays locked to its method. |
| H3 | FR-032, data-model, research, the offers-popups contract, and T071 and T081 all null `coupon_id`. |
| H4 | `auto_promotion_id` (T021a and T024a) feeds `usageCount`. The placement result already carries `promotionId`…|
| H5 | The owner-check cases moved to T072, the route branch to T074, and the dependency is noted. |
| H6 | The welcome code is limited to one use per phone; the first-order-ever rule is out of scope. |
| M1–M12, L1–L8 | Applied. The only remaining gap is the D1 sentence I missed. |

**Coverage summary**

| Requirement | Has task? | Task IDs | Notes |
|---|---|---|---|
| FR-001–FR-007 | ✓ | T011–T029, T033–T057 | |
| FR-008 | ✓ | T013, T014, T021, T021a, T024, T024a, T028, T044, T092 | U1 |

**Constitution alignment:** no MUST violations.
- §VIII is covered in two places: the runbook now checks that the shared Redis limiter is in place, and the privacy tombstone is a keyed hash.
- §VI (flags) holds: `auto_promotion_id` is written behind `console_v2`, and the three unflagged changes are named and limited.

**Unmapped tasks:** none.

**Metrics**
- Total requirements: 50 (41 FR and 9 buildable SC)
- Total tasks: 110 (104 plus T008a, T008b, T021a, T024a, T032a and T093a)
- Coverage: 100%
- Ambiguities: 2 (U1, U2)
- Duplications: 0
- Critical issues: 0

## Next Actions

- With only MEDIUM and LOW issues, you can go ahead to `/speckit-implement`. I'd still fix S1 first: it decides whether real shoppers on mobile data can use codes at all.
- I1 needs your call: build a cart rebuild, or keep the resume screen simple.
- U1, D1 and U2 are one-line edits. Regenerate D2 at commit time.

Would you like me to suggest concrete remediation edits for these 6 issues?

## Extension Hooks

**Optional Hook**: git
Command: `/speckit.git.commit`
Description: Auto-commit after analysis

Prompt: Commit analysis results?
To execute: `/speckit.git.commit`
