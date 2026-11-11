<!-- fixture: synthetic, modelled on real Spec Kit task lines from five repos (alpha, charlie, bravo, foxtrot, golf):
     each block is one task line plus its indented continuation (blank lines dropped, continuation trimmed to 12
     lines), with the descriptive text, paths and domain nouns rewritten and every label kept as it was. Each block is
     preceded by '<!-- expect: human|agent | <source>:<line> | <why> -->'. Classifier corpus: 15,153 real task lines;
     145 classify as human-only, all reviewed by hand; see tests/test_gates_fixall.py. -->

# Tasks: human-only labels and look-alikes (fixture)

## Phase 1: Real task lines

<!-- expect: human | golf/specs/025-ratings-01-seller-ratings/tasks.md:37 | leading FOUNDER GATE label -->
- [X] T001 FOUNDER GATE: a founder reviews and ticks the 16 boxes in
  `specs/025-ratings-01-seller-ratings/checklists/requirements.md`, and accepts as written: the start override and the
  fixed-price scope (spec "Start decision"), the agent defaults in spec.md's Assumptions and Edge Cases (one counted
  rating per buyer per seller, shared-device ratings kept but not counted, buyer confirmation required, final ratings
  with withdraw), and research.md R9 (chat blocks and BID-01 suspensions don't stop rating), R10 (erasure tombstones)
  and R16 (reviews read by listing). Agents never tick these boxes.

<!-- expect: human | golf/specs/007-pwa-01-app-shell/tasks.md:113 | FOUNDER GATE followed by a dated parenthetical -->
- [X] T001 FOUNDER GATE (answered 2026-10-25: OQ-1 and OQ-4 accepted, OQ-2 follow an English device; checklist left
  unticked, founder chose to proceed): a founder reviews `specs/007-pwa-01-app-shell/spec.md` and ticks the boxes of
  `specs/007-pwa-01-app-shell/checklists/requirements.md`, and answers or accepts the plan's open questions:
  - **OQ-1:** the C9.3 budget is measured on the release variant of the development build.
  - **OQ-2:** the first launch follows an English device language, else Hebrew (answered 2026-10-25).
  - **OQ-4:** the App. D retention row for push tokens.

<!-- expect: human | golf/specs/013-legal-01-privacy-terms-erasure/tasks.md:94 | FOUNDER GATE followed by a full stop -->
- [X] T001 FOUNDER GATE. A founder:
  - reviews `specs/013-legal-01-privacy-terms-erasure/spec.md`, and ticks the 16 boxes of
    `specs/013-legal-01-privacy-terms-erasure/checklists/requirements.md` (the agent's notes record a pass on all 16);
  - confirms the three plan-time spec amendments (research.md, "Spec amendments made at plan time");
  - accepts or answers the design gaps G1–G6 at the end of this file;
  - confirms or replaces SC-002's 1 minute and SC-004's 24 h (engineering targets the BRD leaves open).

<!-- expect: human | golf/specs/001-platform-01-monorepo/tasks.md:42 | FOUNDER DECISION label -->
- [X] T001 FOUNDER DECISION: accept or override the ASSUMED answers in specs/001-platform-01-monorepo/spec.md — reference scaffold (none to port), idempotency enforcement deferred (FR-027), bundle-size budget not wired, d-002 gate built here, CodeQL not wired (FR-084), A-25 not applied to pulled images
  - Founder decision (2026-10-18): ASSUMED 1–6 all accepted — (1) no reference scaffold; (2) A-08 enforcement waits for the first mutating route; (3) no bundle-size gate in this spec, ADMIN-01 sets the budget when a page exists; (4) the d-002 buyer-location CI gate is built here; (5) no CodeQL, Trivy and the project lints wait for their subjects; (6) A-25 binds images this repo builds.

<!-- expect: human | golf/specs/001-platform-01-monorepo/tasks.md:52 | Founder review label + '(agents never tick them)' -->
- [X] T006 Founder review: tick the boxes in specs/001-platform-01-monorepo/checklists/requirements.md (agents never tick them)
  - Founder decision (2026-10-18): the implement prompt is the founder review; all 16 boxes are ticked with its note. Layout: `apps/web` uses `src/` (the init's default, `--src-dir`); for the next sweep, SECURITY_PATTERNS.md §2 and security-privacy-web-logging.md §9–§11 assume no `src/`.
  - Founder decision (2026-10-18), supply chain: at the root, if the installed pnpm supports it, set `minimumReleaseAge` to 4320 (three days) in pnpm-workspace.yaml; verify the option name against the installed pnpm's docs and record it (T010).

<!-- expect: human | golf/specs/001-platform-01-monorepo/tasks.md:244 | FOUNDER ACTION after a [US4] tag -->
- [ ] T110 [US4] FOUNDER ACTION: mark ci.yml's `install`, `lint`, `typecheck`, `test`, `contract`, `mobile-check`, `guards`, `security` jobs and docs-ci's `linkcheck` and `context-guards` as required status checks on `main`'s branch protection (research R14)
  - Founder decision (2026-10-18): deferred until GitHub Actions billing is fixed (founder action, outside the repo); the task stays open.

<!-- expect: human | golf/specs/024-seed-01-supply-playbook/tasks.md:325 | bare FOUNDER: after a [US5] tag -->
- [ ] T023 [US5] FOUNDER: rehearse V8 on a scratch copy of the outreach sheet (never the real one): decline a shop,
  clear the personal fields, keep `do_not_contact` with the shop name. A founder writes the outcome into `report.md`
  without any personal data. Product gaps go to the cleanup doc.

<!-- expect: human | golf/specs/017-analytics-01-signal-metrics/tasks.md:652 | FOUNDER: with the whole task in sub-bullets -->
- [ ] T053 FOUNDER:
  - the quickstart settings runbook (values, ENG-130), before the first roll-up run in a shared environment;
  - the optional read-only dashboard role;
  - a share page opened on a real phone, then `analytics:web-vitals` read.

<!-- expect: human | golf/specs/009-listing-03-bulk-creation/tasks.md:487 | PERSON: label -->
- [ ] T073 PERSON: SC-001 (a person submits 20 listings with photos from a filled template; time it) and the human
  ui-verify pass; the founder records both in report.md.

<!-- expect: human | golf/specs/005-category-01-category-framework/tasks.md:881 | Operator steps (not for agents) -->
- [ ] T099 Operator steps (not for agents): `/cc-review`, then `/cc-ship` (it runs `cc-linear-sync`, which moves ENG-96
  forward), then the PR.

<!-- expect: human | golf/specs/003-auth-01-phone-otp/tasks.md:2076 | mid-line '(operator step: ...)' parenthetical -->
- [ ] T212 Review per docs/workflows/review.md (operator step: `/cc-review` is user-invoked): the read-only
  `api-reviewer`, `db-architect`, `security-review`, `web-reviewer`, `integration-reviewer` and `feature-validator`
  agents on the branch diff; fix blockers in scoped `fix(AUTH-01): …` commits, each with its re-run output

<!-- expect: human | golf/specs/021-referral-01-referral-loop/tasks.md:43 | BRD GATE whose subject is the founder -->
- [X] T003 BRD GATE (2026-11-08: the founder chose "Ratify as drafted"; BRD v1.7 C14.1–C14.3, rows A66–A68, commit 5d1c0b7e): a founder ratifies the perk, the qualifying rule and the device-claim retention row in
  `docs/product/brd/07-fast-follows.md` and `docs/product/brd/appendix-d-compliance-privacy.md`, with a new row in
  `docs/product/brd/appendix-a-decision-log.md`, per `docs/product/brd/README.md`.

<!-- expect: human | foxtrot/specs/002-minimum-viable-hosted-pipeline/tasks.md:41 | foxtrot [H] tag -->
- [ ] T005 [H] Provision a 2-vCPU cloud VM (Ubuntu 24.04 LTS, operator SSH key); record IPv4 + IPv6 (quickstart §0)

<!-- expect: human | foxtrot/specs/002-minimum-viable-hosted-pipeline/tasks.md:144 | [H] before a [US5] tag -->
- [ ] T041 [H] [US5] Key ceremony per contract: `age-keygen` on an operator machine (offline), identity → password-manager item, public recipient only → host `/etc/foxtrot-backup/age-recipients.txt` (0644); `/etc/foxtrot-backup/env` (0600 root): object-storage credentials, bucket, heartbeat URL (depends on T006)

<!-- expect: human | foxtrot/specs/003-web-tier-production-infrastructure/tasks.md:31 | [H] **Gate 0** -->
- [ ] T003 [H] **Gate 0** — confirm spec-002 provisioned: ENG-893 → Done; reboot + deploy + restore drills green; `docs/sequencing-hold.md` deleted (quickstart Gate 0). **Blocks every Live task below.**

<!-- expect: human | foxtrot/specs/018-llm-fallback-extraction/tasks.md:667 | leading '*(Optional, human-gated, ...)*' parenthetical -->
- [ ] T042 *(Optional, human-gated, spends money; not part of the local definition of done.)*
  The operator runs quickstart §5 (`run-eval.py --record --by <name>`) with the password-manager key,
  commits the `source: "live"` recordings in a separate PR, and re-runs the gate. This is the
  first real model-quality measurement and the first live structured-output smoke test on
  `claude-haiku-4-5`. **Every later prompt version needs this step** before its PR can merge
  (gate rule 3, analysis C1), so the live-recording procedure is part of the prompt-change
  workflow, not a one-off. `--record` writes the provenance fields that `check_golden.py`
  requires, copied from the real response.

<!-- expect: human | foxtrot/specs/023-qa-review-console/tasks.md:99 | **Gate: the founder approves ...** -->
- [X] T002 **Gate: the founder approves EXC-003 before Phase 2 starts.** *Approved 2026-11-09 and
  entered in `.specify/memory/exceptions.md`; T001 records the approval in the cleanup doc.* Present plan.md's Complexity
  Tracking row and research R2's drafted amendment (the `qa` queue for decisions, page renders and rule
  suggestions; the pipeline → web Redis caches and liveness key; the widened pipeline writes). On
  approval, add EXC-003 to `.specify/memory/exceptions.md` in EXC-002's layout and record the approval in
  the cleanup doc. Without approval, stop and surface (Protocol §6): no Phase 2 task may start. Don't edit
  the constitution.

<!-- expect: human | alpha/specs/spec-020-store-credit-wallet/tasks.md:45 | '*(Operator step — ...)*' parenthetical on a [~] gated task -->
- [~] T005 Register Unleash flags `spec-020.store_credit_wallet` and `spec-020.wallet_redemption` (default OFF) in the local Unleash instance. *(Operator step — flag constants are declared in `constants.ts` and evaluate default-OFF via the fail-safe Unleash wrapper; the container toggle is Protocol §5 human turn-on. Tests drive flags via the in-memory `setFlagProvider` fake.)*

<!-- expect: human | alpha/specs/spec-CHAT-rag-support-chatbot/tasks.md:576 | Operator act, not autonomous -->
- [ ] T083 Operator act, not autonomous (Protocol §5): with the key and models set on a non-production environment, run `pnpm --filter @acme/assistant eval --real`. The resulting report in `evidence/` is the SC-001, SC-002 and SC-009 evidence. A miss on SC-001 recall opens the R-2 hybrid-search follow-up spec, not a prompt tweak without a re-run.

<!-- expect: human | alpha/specs/spec-SEO-search-visibility/tasks.md:85 | Operator — after a [US3] tag -->
- [ ] T040 [US3] Operator — obtain actual complete pre-activation 28-day exports and prepare the branded/non-brand baseline using `specs/spec-SEO-search-visibility/operations/report-template.md`; keep raw exports access-controlled outside committed evidence and record filters/coverage/date. Requires T039 and approved cohort T075; missing data remains unavailable. (FR-021, FR-022, FR-023, FR-024, FR-027, SC-007, SC-010, SC-012).

<!-- expect: human | charlie/specs/072-courier-remittance-ingest/tasks.md:57 | HUMAN GATE — MUST NOT be ticked by any agent (mid-line) -->
- [ ] T011 Schema Council paired review of `0037_courier_remittance.sql` — two reviewers, ENG-ADR-001. **HUMAN GATE — MUST NOT be ticked by any agent, and cannot be self-certified.** Constitution §II and §V make two reviewers a MUST for every migration without exception, and this is the **only open task in the feature**: T001–T010 and T012–T064 are all done, so an unchecked box here is the whole of what stands between this branch and its schema having been reviewed by nobody. The migration, its ten planted-and-watched constraint failures and the eleven-assertion `courier_remittance.integrity.test.ts` are the review packet; the two reviewers are not. **Phase 9 changes nothing this gate is waiting on** — the post-analysis remediation deliberately introduces no schema change and no migration, so the packet under review is the one it was opened against. **Phase 10 changes nothing here either, and it does supersede one sentence above**: the post-review repair round opens further boxes, so this is no longer the only UNCHECKED task in the file. It remains the only **human** gate, the only task no agent may close, and the only one whose subject is the migration — and Phase 10, like Phase 9, introduces no schema change and no migration, so the packet is still the one this gate was opened against. **Phase 11 is the third pass to say exactly this, and it changes nothing here either**: it opens two high-severity code boxes and a documentation tail, touches no file under `packages/db/`, and leaves `0037_courier_remittance.sql` byte-for-byte what the two reviewers were handed. Nothing in Phases 9, 10 or 11 is to be read as satisfying it. **Phase 12 is the first phase that does NOT leave this packet alone, and it says so rather than burying it.** A seven-dimension adversarial review of `0037` — the review this gate has been waiting on, which no one had performed — found silent money misattribution: nothing constrained one verified maker per `destination_handle`, and the sweep resolves by handle with an unordered `LIMIT 1`. The fix is **`0038_destination_handle_unique.sql`**, so **the packet under review is now `0037` + `0038`**. `0037` is NOT amended and is still byte-for-byte what the reviewers were handed. That review was also PARTIAL — 27 of 66 agents died on a rate limit, both closing agents included — so seven findings are UNVERIFIED rather than cleared, and T094–T100 carry the rest. **None of this is sign-off.** An agent's review is not two reviewers, and this box is still not one any agent may tick.

<!-- expect: human | bravo/specs/078-driver-app-sdk57-test-runner/tasks.md:1053 | **Operator-run**: /cc-review -->
- [x] T062 **Operator-run**: `/cc-review`. Its reviewers are `security-review` (permissions, token
  clearing), `web-reviewer` (no web change; skip if the operator agrees), and a courier-focused
  review. This skill is user-invoked: the implementing agent does **not** replicate it by hand. Ask
  the operator to type it, then verify each finding against the code and record the outcome as a
  C-entry in `docs/post-spec-078-cleanup.md`.
  **Done on 2026-10-06** on `1a2b3c4`: one blocker (a rejected Keychain read at session restore
  left the app blank) and six nits fixed in `5d6e7f8` to `9a0b1c2`; the never-settling Keychain
  call recorded as C-030; every finding and its outcome in C-031.

<!-- expect: human | bravo/specs/038-friends-family-test/tasks.md:124 | Bravo [RUN] tag ('never ticked by an implementation session') -->
- [ ] T026 [RUN] [US2] Execute the money loop with real participants: handover against a real delivered code, value collected across every rail reached, exact courier debit, and a finance operator's intake restoring the ledger to the cent without double-restoring (SC-012, SC-013, SC-013a)

<!-- expect: human | bravo/specs/052-capacity-model-min-time-volume/tasks.md:314 | backticked `[RUN]` tag -->
- [ ] T136 `[RUN]` **SC-020 operator-journey proof** — walk a complete weekly refit with **no engineering involvement**: read the evidence, decide new values, set them through the audited path, confirm the new capacity, all from the operator surface. This is the criterion that fails if the desk is merely present rather than usable, and nothing else in the plan exercises it end to end. **AIP §10: the implementer must not claim this without observing it** — and the marker matters more here than anywhere else in the list, because this is the one criterion whose own wording excludes the implementer from performing it. Left unmarked beside `T132`, the single task an engineer may not run was the single task an engineer could freely tick

<!-- expect: human | bravo/specs/053-four-phase-day-same-day-relay/tasks.md:336 | bold **[RUN] ...** tag -->
- [ ] T095 **[RUN] On-device dual-locale verification** of the courier kerbside screen and the admin route-review screen, in HE and EN, on a real device and browser — **the implementer must not tick this** (AIP §10); any deviation from the LOCKED design doc is recorded in its changelog (AIP §14)

<!-- expect: human | bravo/specs/056-failure-reason-codes-disposition/tasks.md:181 | [NOT CLAIMABLE BY THE IMPLEMENTER — AIP §10] tag -->
- [X] T070 **[NOT CLAIMABLE BY THE IMPLEMENTER — AIP §10]** On-device dual-locale run of the reason Sheet (he + en, both themes) against the locked doc's §8 verification standard. **RUN 2026-09-02** on `bravo_emulator` (API 36, 412dp) against a live API and four real seeded stops, all four combinations; **ticked on the reviewer's authority after reviewing the evidence, NOT claimed by the implementer.** Evidence in `evidence/T070/`; observation recorded in the locked doc's §9 changelog and as post-spec-056 C-035. C-014 answered: the five Hebrew labels do not wrap at 412dp. The run found and fixed one FR-009 defect (the confirm path did not clear the selection, so it leaked across parcels) and recorded one unfixed deviation (the success notice renders in the danger-red slot)

<!-- expect: human | bravo/specs/058-per-vehicle-insured-aggregate/tasks.md:172 | [MANUAL] tag + 'The implementer must NOT tick this' -->
- [ ] T073 **[MANUAL]** On-device dual-locale verification of the admin screen (HE/RTL + EN, both themes). **The implementer must NOT tick this** — AIP §10 requires a human observer, and a claim without one is the false-completion this rule exists to prevent

<!-- expect: human | bravo/specs/048-courier-role-separation/tasks.md:188 | 'Do **not** tick this on the implementer's behalf' -->
- [ ] T069 **Report the AIP §10 obligation as OUTSTANDING**: on-device dual-locale (HE/RTL + EN) verification of the accounts screen. Do **not** tick this on the implementer's behalf — it requires a real browser run, and claiming it unperformed is the precise false-completion AIP §10 exists to prevent

<!-- expect: agent | golf/specs/003-auth-01-phone-otp/tasks.md:129 | 'founder-ticked' in prose -->
- [X] T002 Commit the founder-ticked checklist after T001 (tasks.md is already committed on the branch):
  `git add specs/003-auth-01-phone-otp/checklists/requirements.md` ⟶ commit `docs(AUTH-01): record the founder gate`
  (already committed: 2fbede2)

<!-- expect: agent | golf/specs/014-notify-01-notifications-push/tasks.md:516 | 'the founder gate' mid-sentence -->
- [X] T086 report.md: the founder gate, red/green per test task, stub status (protocol §9: the push stub and SMS
  stub are exercised; Expo and the SMS provider are not activated), new copy for review, and the person checks of
  quickstart §3 left for founders. ⟶ commit `docs(NOTIFY-01): report and documentation sync`.

<!-- expect: agent | golf/specs/003-auth-01-phone-otp/tasks.md:2079 | 'Stop for the founders' (an agent hand-over) -->
- [X] T213 Stop for the founders: `/cc-ship` and the PR are founder-invoked (docs/workflows/ship.md); hand over
  report.md, including the delegated decisions F1–F9, the deviations (Hebrew-only captures; the region picker
  deferral; CI not run — local outputs in report.md; size-limit unwired, inherited from PLATFORM-01) and the PR-body
  text

<!-- expect: agent | golf/specs/008-listing-01-create-manage/tasks.md:108 | body mentions a 'Founder gate' section -->
- [X] T004 Baseline. Run the per-workspace checks for `@delta/api`, `@delta/db`, `@delta/shared`, `@delta/web` and
  `@delta/mobile`, plus `pnpm contract:check`. Create `specs/008-listing-01-create-manage/report.md` with a "Baseline"
  section quoting the output (any red is named as pre-existing) and a "Founder gate" section (T001, T002). Verify
  environment health (protocol §8): compose healthy; the migration tip equals the last journal `when`; `db:check`
  passes.

<!-- expect: agent | golf/specs/024-seed-01-supply-playbook/tasks.md:154 | 'Ask a founder to read the Hebrew' in a wrapped line -->
- [X] T008 [US1] Add the "shop pitch" and "visit follow-up" blocks to `docs/operations/outreach-messages.md` (create
  the file with a short header: colloquial Hebrew, a one-line English gloss per block, no promise of sales volume or
  price). Ask a founder to read the Hebrew before T009 is marked done.

<!-- expect: agent | golf/specs/006-listing-02-listing-detail/tasks.md:572 | 'then the operator steps for the second PR' mid-sentence -->
- [X] T093 Reviewers (`security-review`, `integration-reviewer`), then the operator steps for the second PR (`/cc-review`,
  `/cc-ship`).

<!-- expect: agent | alpha/specs/spec-CMP-compliance/tasks.md:572 | 'Operator script' (code for operators) -->
- [ ] T116 [US7] Operator script `db/scripts/backfill-price-history.ts`. It builds history from the `variant.price_change` audit rows, has dry-run and apply modes, ends with `process.exit`, and comes with a test. Leaving a variant with no history is fail-closed.

<!-- expect: agent | alpha/specs/spec-CHKUX-checkout-payment-redesign/tasks.md:174 | 'owner-only' is an authorization rule -->
- [X] T077 [P] [US3] Write `packages/payments/tests/latest-open-order.int.test.ts` for `getLatestOpenOrderForShopper`: owner-only (customer and guest tokens), status set, `confirmed` only with a fee/transfer outstanding, window bound, most-recent wins, another shopper's order never returned (FR-023) — DONE: also covers the fee redirect lookup and the fee-aware fallback (9 cases)

<!-- expect: agent | alpha/specs/spec-A3-3-cart-checkout-clean-pastel/tasks.md:109 | '(Protocol §10, human gate)' qualifier: Alpha agents flag and tick these (see spec-003 T037) -->
- [ ] T031 Real-browser RTL smoke (Protocol §10, human gate): drive HE checkout at 375px + EN at 1440px + RTL cart overflow + the COD/card selected-ring + the result success/failure states; flag any device-only breakage. Record as "implementation complete; on-device validation pending" in the ship notes.

<!-- expect: agent | alpha/specs/spec-003-taxonomy/tasks.md:139 | 'Human RTL/visual smoke' ticked by the agent after flagging -->
- [X] T037 Human RTL/visual smoke (Protocol §10): HE admin tree + tables at 1440px (RTL, logical spacing), EN admin LTR, HE storefront nav at 375px — flag explicitly as on-device validation

<!-- expect: agent | alpha/specs/spec-A2-6-advanced-bi-warehouse/tasks.md:120 | 'Manual "refresh now"' is a feature -->
- [X] T038 [POLISH] Manual "refresh now" admin Server Action (`'use server'`, Zod-validated, re-verify admin + `analytics_ops`) invoking `refreshBiMarts`; surface last-run status + "data as of" on each BI view. CSRF handled by Next Server Actions (SECURITY §16).

<!-- expect: agent | alpha/specs/spec-020-store-credit-wallet/tasks.md:58 | '**GATE — Schema Council ×2**' names no human label (open box halts safely) -->
- [X] T011 [P] [TEST] Write core tests first (must fail): `applyEntry` idempotency + in-tx audit in `packages/wallet/src/credit.test.ts`; balance-invariant property test (`balance == Σ entries`, never negative) in `packages/wallet/src/wallet.property.test.ts`; invalid (zero/negative) amount rejected.

<!-- expect: agent | charlie/specs/021-stock-soft-reserve/tasks.md:82 | 'owner-only via resolveVariantOwner' -->
- [X] T018 [US2] `setVariantStock(tx, {variantId, mode, total?}, actor)` in `packages/inventory/src/stock.ts` — owner-only via `resolveVariantOwner` (`not_owner` — the SC-009 gate). **Upsert via `SELECT … FOR UPDATE` first**: existing row → evaluate guards against the **locked** counters then `UPDATE`; no row → INSERT, and a concurrent first-set loser catches the unique-violation (23505) and **retries down the update path** (clean `Result`, never a raw 23505). `finite` requires non-negative integer `total` (`invalid_quantity`) and refuses `total < committed + reserved` (`below_obligated`, computed under the lock so it can't race into a raw `no_oversell` CHECK — D9). A `finite ↔ made_to_order` flip is refused (`mode_change_blocked`) while **any active `stock_reserve` exists for the variant** (existence, not `reserved > 0` — D5); mode + `total` change in one `UPDATE`. `made_to_order` sets `total=NULL, reserved=0`. **Audit-first with who/old/new** (FR-010 reconciliation distinguishes an owner edit from a reserve-driven change); emit `changeKind='adjust'` (`quantity=0` for a pure mode flip). Behind `021.stock_manage`. (depends: T010, T011, T012)

<!-- expect: agent | charlie/specs/015-brand-storefront/tasks.md:102 | 'Owner "copy share link"' is a feature -->
- [X] T031 [P] [US3] Owner "copy share link" affordance in the brand editor (`apps/brand/src/app/storefront/page.tsx`) surfacing `buildShareLink` output (FR-018).

<!-- expect: agent | charlie/specs/035-order-fsm-core/tasks.md:115 | 'Operator-visible count' -->
- [X] T034 [US3] Operator-visible count of orders waiting at the receipt window, so the expected zero-auto-confirm period is visible rather than discovered (FR-021a), in `packages/order/src/receipt-window.ts`

<!-- expect: agent | bravo/specs/023-sender-portal/tasks.md:94 | 'human-gated' describes a secret, mid-line -->
- [X] T037 [US2] Map-provider abstraction + `PinPicker` component (map tiles behind the abstraction; key from config, human-gated) in `apps/web/src/components/PinPicker.tsx`.

<!-- expect: agent | bravo/specs/002-cicd-pipeline/tasks.md:97 | agent surfaces a **human-gated** step to the operator -->
- [~] T018 Run the full quickstart.md acceptance matrix end-to-end (all rows incl. fork-PR behavior SC-008) and report results; surface the **human-gated** branch-protection apply step (require `ci-success`, no force-push) to the operator per FR-019 / Protocol §11 (do not apply repo settings autonomously).

<!-- expect: agent | bravo/specs/049-van-fleet-size-tiers/tasks.md:330 | quotes 'the implementer must not claim it' about other work -->
- [X] T074 [P] Record findings in `docs/post-spec-049-cleanup.md` via `/cc-cleanup-add 049`, including at minimum: the T002 pre-migration courier count; the half-provable capacity obligation (spec-052); the two `@Global` modules now adapting `CourierRosterService` and the consolidation trigger if a third appears; the intake size-mismatch tolerance now being reachable in the under-charging direction; the **single vehicle-change refusal message T048e settles on and what it costs the operator**, if the two `state_transition_rejected` causes proved indistinguishable on the wire; and the **outstanding AIP §10 obligation, now narrowed to one thing**: the **on-device** execution of T048j's committed dual-locale spec, covering both new accounts-desk controls — the create form's vehicle picker and the vehicle-change control. Be exact about which half is outstanding, because the two are no longer the same claim: the spec is authored here and **executed against a live api+admin stack by T071**, whose recorded output is what meets SC-017's console half. What is left is the run on a device, observed by a human, and the implementer must not claim it as done

<!-- expect: agent | bravo/specs/112-merchant-data-isolation/tasks.md:233 | 'Stop and ask the operator to run /cc-review' (agent stops) -->
- [X] T074 **Stop and ask the operator to run `/cc-review`, then `/cc-ship`.** Both are user-only. T073 must have landed before `/cc-ship`. State plainly what stays unverified: on-device and at-360-px checks not run, and everything that closes only in the fifteenth run (AIP §10).

<!-- expect: agent | bravo/specs/106-sim-findings-hardening/tasks.md:61 | accepted miss: '**Owner decisions O-1 … O-13**' carries no separator -->
- [X] T002 **Owner decisions O-1 … O-13** (plan § Owner decisions). **Answered 2026-10-27** ("fix all": every recommendation taken). The answers are already under `## Clarifications` in `specs/106-sim-findings-hardening/spec.md`, and plan § Spec amendments is applied:
  - the pay-week Assumption (O-3);
  - FR-022 wording (R-05);
  - FR-023 "cancelled" (O-13);
  - the FR-037 and US6 AS-6 example (O-7);
  - FR-029's unusable default (O-8);
  - the four new Out-of-scope lines;
  - the analysis amendments: US3 AS-6, FR-027, SC-001 and the Key Entities.
  What is left:
  - Commit the spec, plan, research, data-model, quickstart and tasks as they stand.
  - Refresh Linear ENG-1345 with `/cc-linear-sync 106`.
  - If the owner later overrides an answer, update the affected `(O-n)` tasks in the same commit as the override.
  Commit: `docs(spec-106): record the owner decisions`.

<!-- expect: agent | foxtrot/specs/002-minimum-viable-hosted-pipeline/tasks.md:145 | '**[H]** sub-step' mid-line, not a leading tag -->
- [ ] T042 [US5] Install script+units on host, `systemctl enable --now foxtrot-pg-backup.timer`; manual first run → verify dated `.dump.age` object in the bucket, size check passed, uptime heartbeat green; **[H]** sub-step (storage-provider account action — analyze C9): configure the bucket lifecycle rule — expire `nightly/` after 30 days + abort incomplete multipart after 2 days (depends on T040, T041, T022)

<!-- expect: agent | foxtrot/specs/007-operations-authentication/tasks.md:128 | 'Owner-notify abstraction (... human-gated)' -->
- [X] T042 [US4] Owner-notify abstraction `apps/web/lib/auth/notify.ts` (logs in dev; email provider behind a prod flag — external/human-gated) (depends: T041)

<!-- expect: agent | foxtrot/specs/022-ops-console-foundation/tasks.md:52 | 'Commit the founder-applied constitution change' -->
- [X] T001 Commit the founder-applied constitution change as `docs(constitution): lock TanStack Table v9 (1.6.4)`, staging only `.specify/memory/constitution.md`. Verify with `node scripts/check-doc-index.mjs` and `node scripts/check-agents-md.mjs`.
