---
description: "Task list for SEED-01 — Pre-launch Supply Playbook"
---

# Tasks: Pre-launch Supply Playbook (SEED-01)

**Input**: Design documents from `/specs/024-seed-01-supply-playbook/`

**Prerequisites**:
- [plan.md](plan.md): structure, constitution check. [spec.md](spec.md): FR-001–FR-021, SC-001–SC-007, US1–US5.
- [research.md](research.md) (R1–R9, founder gate items G1–G5), [data-model.md](data-model.md) (five records),
  [quickstart.md](quickstart.md) (docs guards, rehearsals V1–V9).
- [contracts/playbook-outline.md](contracts/playbook-outline.md) (the playbook's §0–§7),
  [contracts/product-dependencies.md](contracts/product-dependencies.md) (P1–P9).

**Tests**: no unit tests. No business logic changes (plan R9, constitution §VI). Verification:
- The docs guards and the phone-number grep in `quickstart.md`, run after every playbook file change.
- Rehearsals V1–V9, each recorded with its outcome in `specs/024-seed-01-supply-playbook/report.md`.
- Rehearsals run on a local environment only (R7). An agent may drive the local stack and the iOS simulator. Items
  marked **FOUNDER** need a person.

**Writing rules for every playbook task**:
- No figure anywhere. A value the founders own is written `— (founder value)` (FR-002, A-19).
- No phone number, owner name or sheet URL in the repository (FR-016, R3).
- Lines in `AGENTS.md` and `docs/README.md` stay under 240 characters.

**Organization**: Phase 0 gates; Phase 1 setup; Phase 2 foundational; Phases 3–4 the P1 stories (US1, US2); Phases 5–6
the P2 stories (US3, US4); Phase 7 the P3 story (US5); Phase 8 polish and verification.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US5 from spec.md; Setup, Foundational and Polish tasks carry none

---

## Phase 0: Gates (before `speckit-implement`)

- [X] T001 FOUNDER GATE: a founder ticks the 16 boxes of `specs/024-seed-01-supply-playbook/checklists/requirements.md`,
  and answers G1–G5 in `specs/024-seed-01-supply-playbook/research.md`:
  - G1: the outreach sheet's host and sharing.
  - G2: whether the retention period is set now or left blank.
  - G3: records as commits.
  - G4: the waive switch stays a gate call.
  - G5: the App. D row is recorded as owed.

  Record the answers in `research.md` under the gate table, dated.
  Done 2026-11-09: the founder said "tick the 16 boxes, G1–G5 accepted as recommended" (answers in `research.md`).

**Not tasks for agents**:
- Creating the outreach sheet or putting anyone's details in it.
- Choosing any figure (lines, caps, retention, supply target).
- Changing a setting in a shared environment.
- Running a real burst or contacting a real shop.
- Editing the App. D shard (a BRD version).
- Accepting a DPA.

**Checkpoint**: T001 done → `speckit-implement` may start. Stop and report if it is open.

---

## Phase 1: Setup

- [X] T002 Rebase `feature/024-seed-01-supply-playbook` on `main`. Re-check the four facts the plan rests on, and start
  `specs/024-seed-01-supply-playbook/report.md` with the command output:
  - the ad-click list (`specs/017-analytics-01-signal-metrics/runbook-settings.sql:47` contains `golf_ad`, not
    `sclid`);
  - the seller-activity filter (`apps/api/src/analytics/rollup/seller-stats.sql.ts`, `actor = 'seller'`);
  - the assisted free reason (`packages/shared/src/credits/charge.ts`);
  - the admin bulk route (`apps/web/src/app/[locale]/admin/bulk/[sellerId]/page.tsx`).

  If one changed, stop and report (protocol §17).
- [X] T003 Create `docs/operations/records/README.md`. It says what each record is, where it lives, and the rules:
  - Ratified lines: rows are only ever added, by commit. Gate records: never edited once committed. Burst log: an
    entry's fields are filled in as the burst runs and never changed; a correction is a dated note under the entry.
  - Dates are local `YYYY-MM-DD`.
  - Money is integer cents with its period; an amount billed in another currency also keeps the billed currency,
    the billed amount and the founder-entered exchange rate with its source and date (`data-model.md`).
  - "No personal data in this folder: no phone numbers, no owner names, no sheet links."
  - The outreach record lives outside the repository (R3).

  Include a gate-record template for `gate-YYYY-MM.md` with every GateRecord field from `data-model.md`:
  `metric_active_in_segment`, `listing_to_contact_rate` (with the "burst month" mark), `seller_return_self_service` /
  `seller_return_assisted` ("never summed"), `contacts_per_week`, `views_excluded_ad_click`, `spend_cents`
  (a spanning burst split by the platform report's date range; prorate by days only when it can't split, and say so),
  `cost_per_active_listing` ("`spend_cents ÷ metric-active denominator`"), `runway_months`, `lines_in_force`,
  `shops` ("counts only, one per status"), `deletions`, `founder_contact_check`, `samples`, `decision`
  (continue / change / stop), `founders`.

---

## Phase 2: Foundational (blocks every story)

- [X] T004 Create `docs/operations/supply-playbook.md` with the eight section headings of
  `contracts/playbook-outline.md` in order (§0–§7), and write §0 "How to use this playbook" in full:
  - who runs it (the founders);
  - the gates it waits on: ENG-129 (first CAT build) and ENG-119 (production SMS sender). Outreach does not wait;
    ENG-118 is the execution;
  - rehearsal vs real (R7: local only, no demo items in production, FR-007);
  - the records table: file or sheet, what it holds, who writes it, links to `records/README.md`;
  - the "never" list from `contracts/playbook-outline.md`, verbatim;
  - what may start now: shop outreach with business details only. Owner names and phones wait until the sheet's
    host has an accepted DPA and is on the regulator's transfer register (FR-016, R3). Group posts and creator posts wait for
    live share pages and real listings; onboarding waits for ENG-129 and ENG-119;
  - how to change the playbook: a commit, and a gate record line saying why.
- [X] T005 [P] Add one line for `docs/operations/` to `docs/README.md` "Other folders" ("founder-run procedures and their
  records; no personal data"), and one line to the `AGENTS.md` Layout block ("founder playbooks (SEED-01) and their
  append-only records"). Keep `scripts/check-context-budget.sh` passing.
- [X] T006 [P] Create `docs/post-SEED-01-cleanup.md` in the format of `docs/post-WALLET-01-cleanup.md`, with entries for:
  - G5: an App. D retention row owed at the next BRD version, "owner contact details for supply outreach: deleted on
    request, on decline, and at the founder-set period after onboarding closes";
  - G1: the outreach sheet's host added to App. D's processor list and the regulator's transfer register (the founders' counsel
    track);
  - BRD §6 says to write the ratified figures into its thresholds table; at the next BRD version, point that table at
    `docs/operations/records/ratified-lines.md` instead (spec Assumptions, analyze A1);
  - a heading "Product gaps found in rehearsal" (filled by T010, T012, T016, T019, T023), opening with one known
    gap: no admin screen shows contacts by buyer account, so SC-002's "no founder contact" check rests on each
    founder's own statement (research R8, analyze C2).

**Checkpoint**: the playbook skeleton, the records README and the map lines exist; the docs guards pass.

---

## Phase 3: User Story 1 — Onboard a shop so its listings stay live and counted (P1) 🎯 MVP

**Goal**: a founder can take one shop from first contact to `onboarded`, with listings that stay metric-active.

**Independent test**: rehearsals V2–V4 pass on the local stack, following §2 only.

- [X] T007 [US1] Write §2 "Onboard a shop" in `docs/operations/supply-playbook.md`:
  - Find shops (phones/electronics, the two launch districts; FR-009).
  - The pitch: what golf is and what it is not (discovery, not escrow, §IX).
  - Fees: assisted listings are free. Self-serve listings follow the category price unless the waive switch is on;
    check it at the gate (R6, G4).
  - The shop signs up on the owner's own phone with its own phone number before anything is listed. The founder
    guides; the owner taps. A founder never receives, reads or types the owner's code (FR-004, Clarifications U3).
  - Choose the path: self-serve bulk flow, or the assisted flow at `/admin/bulk/[sellerId]` (FR-006).
  - Real items only, at the shop's price, with its agreement (FR-007).
  - The owner signs in on their own device; show where buyer chats arrive; no founder test chat (FR-005, R5).
  - Follow-up for shops with no owner activity in the activity window, keyed on the owner's own last activity,
    never on founder edits (FR-008).
  - Out-of-segment shops may list but don't count.
  - Held or rejected assisted listings are fixed like any seller's, never overridden (P9).
  - Items sold in the shop are marked sold the same day, by the owner or by the founder if the owner asks; a founder
    mark is not seller activity (spec Edge Cases).
  - A shop that answers buyers by phone: the tap still counts as a contact, but the reply is not activity, so ask
    the owner to open the app regularly (spec Edge Cases).
  - Demo items, stock the shop doesn't have yet, and founders' or friends' items are refused (FR-007, Edge Cases).

  Quote the OutreachEntry status rules from `data-model.md` verbatim: "`listed` requires the shop's own verified
  account (FR-004)"; "`owner-active` requires the owner's own sign-in (FR-005)"; "`onboarded` requires `owner-active`
  and live in-segment listings (FR-009)"; "An `onboarded` shop whose owner has no activity inside the activity window
  goes back to `paused`, with a follow-up (FR-008)". End with the §2 checklist.
- [X] T008 [US1] Add the "shop pitch" and "visit follow-up" blocks to `docs/operations/outreach-messages.md` (create
  the file with a short header: colloquial Hebrew, a one-line English gloss per block, no promise of sales volume or
  price). Ask a founder to read the Hebrew before T009 is marked done.
- [ ] T009 [US1] Rehearse V2, V3 and V4 from `specs/024-seed-01-supply-playbook/quickstart.md` on the local stack,
  following §2 only:
  - V2: one OTP-stub account plays the shop, signed up on its own device; the admin lists 3 items through the
    assisted flow.
  - V3: a next-day `pnpm --filter @golf/api analytics:signal`.
  - V4: note the shop's `seller_stats.last_activity_at`, make a founder edit through the assisted flow with no owner
    activity, rerun the seller-stats roll-up, and show `last_activity_at` unchanged. Don't expect the shop to read
    inactive: the V2 sign-up is an OTP sign-in and keeps it active for the whole window.

  Record each command and its output in `report.md`, then fix §2 where the rehearsal disagreed with it.
- [X] T010 [US1] Product gaps from T009 (if any) go to "Product gaps found in rehearsal" in
  `docs/post-SEED-01-cleanup.md`, as open questions for the founders. Do not change code (FR-020).

**Checkpoint**: US1 is usable on its own. A founder can rehearse onboarding end to end.

---

## Phase 4: User Story 2 — Pre-register the lines before any spend (P1)

**Goal**: a dated, append-only table of the six lines exists, blank for the founders, and nothing can spend before
it is full.

**Independent test**: `records/ratified-lines.md` lists all six lines with `— (founder value)`. §1 says how to ratify
and how to supersede.

- [X] T011 [P] [US2] Create `docs/operations/records/ratified-lines.md` with one row per line, every figure
  `— (founder value)`. Fields from `data-model.md`: `line` ("one of: `cost_per_active_listing`,
  `listing_to_contact_rate`, `seller_return_self_service`, `contacts_per_week`, `runway_months`,
  `supply_target_metric_active_in_segment`"), `continue_if` / `stop_if` ("founder text, quoting BRD §6's row shape;
  the supply target has a target figure only"), `check_by` (founder value), `ratified_on`, `founders`, `supersedes`,
  `reason` ("required when `supersedes` is set (FR-011)"). Above the table: "Rows are only ever added." and the
  in-force rule from `data-model.md` verbatim (the latest entry on or before the month's first day; a line's first
  entry is in force from its `ratified_on` date; a superseding entry only from the next month's first day).
- [X] T012 [US2] Write §1 "Draw the lines first" in `docs/operations/supply-playbook.md`:
  - why (BRD §6: a gate without pre-committed numbers is theatre);
  - how to fill each row together, citing §6's definitions (Q4 contact and view, Q6 seller return on self-service
    only, Q7 metric-active);
  - how to supersede (FR-011);
  - "A burst is blocked while any of the six lines has no entry (FR-010)".

  End with the §1 checklist. Note any product gap in the cleanup doc.

**Checkpoint**: US1 and US2 both work; MVP complete (supply can start; spend cannot until the founders fill §1).

---

## Phase 5: User Story 3 — Run a deliberate paid-social burst (P2)

**Goal**: every burst is approved, logged, marked as ad traffic and costed from the platform report.

**Independent test**: rehearsals V1, V5 and V6 pass; `records/burst-log.md` has the template and its state rules.

- [X] T013 [P] [US3] Create `docs/operations/records/burst-log.md` with the Burst fields from `data-model.md`:
  - `burst_id`: "`B<NN>`, sequential, also the `golf_ad` value on its links (R4)".
  - `approved_at_gate`.
  - `start` / `end`: "`end` ≥ `start`".
  - `cap_cents`: "founder value, fixed before spend".
  - `channel`, `audience`, `test_click_seen`.
  - `actual_spend_cents`: "from the platform report, entered after `end` (FR-014)".
  - `billed_currency` / `billed_amount` / `exchange_rate`, and `spend_by_month` for a burst that spans months.

  Above the table, the rule from `data-model.md`: fields are filled in as the burst runs and never changed; a
  correction is a dated note under the entry. Add the state line "`planned → running → ended → spend-recorded`" and
  both rules verbatim. "A burst cannot be
  `running` without `test_click_seen`, or `planned` while a RatifiedLine is missing." and "Paid spend outside every
  `start..end` range must be zero (FR-013)." No entries.
- [X] T014 [US3] Write §4 "Run a burst" in `docs/operations/supply-playbook.md`:
  1. The first check, the lines are complete (FR-010).
  2. Approval at a gate (FR-013).
  3. The log entry before spend.
  4. Confirm the environment's `analytics.views.ad_click_markers` contains `golf_ad` in the admin settings editor
     (P5).
  5. Every paid link carries `golf_ad=<burst_id>`: the ad's URL parameters field on each social ad platform, the link
     given to a paid creator (R4). Say plainly that `sclid` is not a marker and organic social clicks stay counted.
  6. The test click before spend (FR-012).
  7. Run, then end; record actual spend from the platform report (FR-014), in the billed currency too if it isn't
     USD, and split by month if the burst spans months. If the cap ran out early or the platform overspent, the
     report's figure is what's recorded, and the overspend is noted at the gate (spec Edge Cases).
  8. Zero spend between bursts.
  9. The stop/pivot rule: "flat or rising after two consecutive ad bursts", per the ratified line.
  10. A paid link shared onward loses its marker and its views count as organic; accepted, and noted at the gate if
     the excluded count looks low (spec Edge Cases). The marker removes only the directly opened view; contacts are
     not filtered (R4).

  End with the §4 checklist.
- [X] T015 [US3] Rehearse V1, V5 and V6 on the local stack:
  - First set `analytics.views.ad_click_markers` to the runbook value in the local admin settings editor; the local
    seed carries `['test_ad_click_id']` (quickstart Prerequisites).
  - V1: §4 stops on a missing line.
  - V5: `?golf_ad=B01` is an excluded ad-click view.
  - V6: `?sclid=x` is a counted view.

  Record output in `report.md` and fix §4 where it disagreed.
- [X] T016 [US3] Product gaps from T015 go to the cleanup doc (FR-020).

**Checkpoint**: US3 works on its own on top of US2's lines.

---

## Phase 6: User Story 4 — Read the monthly cash + time gate (P2)

**Goal**: a founder writes one month's gate record from the dashboard and the burst log, unaided.

**Independent test**: rehearsal V7 produces a complete gate record from the local readout.

- [X] T017 [US4] Write §5 "Hold the monthly gate" in `docs/operations/supply-playbook.md`:
  - Where each figure comes from: the ADMIN-01 signal dashboard narrowed to phones/electronics × the launch districts (P6), the
    monthly denominator (P7), the burst log.
  - How to compute cost per active listing (R8).
  - Seller-return cohorts read apart, with the line judged on self-service only (US4 scenario 2).
  - Runway, and comparing each figure to the line in force (`data-model.md` in-force rule).
  - In a month with a burst, the contact rate is marked "burst month": paid views are excluded but their contacts
    are not (FR-012, R4).
  - The cash and time questions out loud, and the decision.
  - The SC-002, SC-003 and SC-007 samples written into the record. For SC-002's founder-contact half, each founder
    checks their own chat inbox and states it in `founder_contact_check`; no admin screen shows contacts by account.
  - Burst overspend and a low excluded-view count are noted here (§4 steps 7 and 10).
  - How the waive switch is reviewed (G4).

  End with the §5 checklist pointing at the template in `records/README.md`.
- [X] T018 [US4] Rehearse V7: write `docs/operations/records/gate-REHEARSAL.md` from the local `analytics:signal`
  readout and one made-up spend row marked REHEARSAL. Check every FR-015 field is filled and the cohorts are apart.
  Record the result in `report.md`, then delete the file (it must not be committed).
- [X] T019 [US4] Product gaps from T018 go to the cleanup doc (FR-020).

**Checkpoint**: US4 works with or without a burst (spend zero).

---

## Phase 7: User Story 5 — Digital outreach to shops and buyers (P3)

**Goal**: outreach runs from approved messages, honours every "no", and keeps personal data out of the repository.

**Independent test**: V8 passes on a scratch sheet; §3 and §6 cover every FR-016–FR-019 rule.

- [X] T020 [US5] Write §3 "Outreach" in `docs/operations/supply-playbook.md`:
  - The channels: visit, public business messaging, business page, inbound, community groups, creators.
  - The first-contact rule: public business channels, one by one, from a founder's own account (FR-017,
    Clarifications Q2).
  - No bulk or automated messages, no scraped or bought lists, no personal numbers.
  - A "no" ends contact for good, and the do-not-contact mark stays.
  - Never from the OTP sender (FR-018).
  - Groups: follow each group's rules, from a founder's own identified account, linking only to real live listings
    (US5 scenario 1). Creators: disclosed as paid, links with `golf_ad`, cost recorded like a burst (FR-019).
  - Timing: shop outreach starts now with business details only; group and creator posts wait for live share pages
    and real listings (spec Gates).
  - No fake accounts, reviews or buyer activity.

  End with the §3 checklist.
- [X] T021 [US5] Write §6 "Privacy" in `docs/operations/supply-playbook.md`:
  - The OutreachEntry fields from `data-model.md`, as the sheet's only columns.
  - A blocking first check: `owner_name` and `owner_phone` stay empty until the sheet's host has an accepted DPA and
    is on the regulator's transfer register (FR-016, R3, the G1 entry in the cleanup doc). Until then, business details only.
  - The forbidden list, verbatim: "ID documents, home address, notes about the person, message screenshots, anything
    copied from the product's admin screens beyond the account's existence".
  - Who can open the sheet (G1 answer), deletion including the sheet's version history (R3), `retention_end` (G2).
  - Logging deletions in the gate record as counts only.
  - The host's place on the DPA and transfer lists (cleanup doc).
  - Requests by a shop owner go to the `privacy@` address (App. D manual fulfilment).
- [X] T022 [US5] Add the remaining blocks to `docs/operations/outreach-messages.md`:
  - business messaging first message;
  - business page first message;
  - group post;
  - creator brief, which requires disclosure and the `golf_ad` link;
  - "thanks, we won't contact you again".

  Each block is colloquial Hebrew with a one-line English gloss, and none promises sales volume, a price or a feature the
  product lacks (§IX). A founder reads the Hebrew.
- [ ] T023 [US5] FOUNDER: rehearse V8 on a scratch copy of the outreach sheet (never the real one): decline a shop,
  clear the personal fields, keep `do_not_contact` with the shop name. A founder writes the outcome into `report.md`
  without any personal data. Product gaps go to the cleanup doc.

**Checkpoint**: all five stories are written and rehearsed.

---

## Phase 8: Polish and verification

- [X] T024 Write §7 "Close at launch" in `docs/operations/supply-playbook.md`:
  - The final gate record: metric-active in-segment listings against the ratified supply target, assisted and
    self-service apart (FR-021, SC-005).
  - The hand-over: the playbook stays the running pilot procedure, and SEED-01's spec close names it.
  - If launch slips, the playbook keeps running and the supply target is re-read, not re-drawn (spec Edge Cases).
- [X] T025 [P] Read `docs/operations/supply-playbook.md` against `contracts/playbook-outline.md` and the spec:
  - every FR-001–FR-021 is covered by a named section;
  - every spec edge case is covered by a named section;
  - every section ends with its checklist;
  - no figure appears: grep for ASCII digits (`[0-9]`) and full-width digits (`[０-９]`) outside dates, IDs, FR
    and § numbers, in both playbook files, and review each hit.

  Record the FR → section map in `report.md`.
- [X] T026 [P] Run the quickstart docs guards: `scripts/check-references.sh`, `scripts/check-context-budget.sh`,
  `scripts/check-coverage.sh`, `lychee --offline --include-fragments .`, and both phone-number greps over
  `docs/operations/` (expect no output). Prove the greps catch what they should: put fake numbers in a scratch file
  under `docs/operations/`, in ASCII, in full-width digits and with spaces or dashes, see each one reported, then
  delete the file. Record all output in `report.md`.
- [X] T027 Run the `security-review` agent on the branch diff (privacy-law posture of the records and messages) and the
  `feature-validator` agent on the spec against the BRD shards. Fix findings in the playbook or record them in
  `docs/post-SEED-01-cleanup.md`.
- [ ] T028 FOUNDER: V9. A founder who did not write the playbook does V2–V3 and V7 from it alone (SC-006); the outcome
  goes into `report.md`.

---

## Dependencies & execution order

- Phase 0 (T001) blocks everything. Phase 1 → Phase 2 → stories.
- US1 (Phase 3) and US2 (Phase 4) depend only on Phase 2 and can run in parallel. They touch different sections of
  one file (`supply-playbook.md`), so the §-writing tasks T007 and T012 are serialized; T011 is [P].
- US3 needs US2's `ratified-lines.md` (the burst check, V1). US4 needs US3's `burst-log.md` (spend source), but its
  rehearsal works with zero spend.
- US5 is independent of US2–US4. It shares `outreach-messages.md` with T008, so T022 follows T008.
- Polish follows all stories. T028 needs the finished playbook.

## Parallel examples

- After T004: T005 and T006 together.
- US2 + US3 setup: T011 (`ratified-lines.md`) and T013 (`burst-log.md`) together.
- Polish: T025 and T026 together.

## Implementation strategy

- **MVP = Phase 0–4 (US1 + US2).** Founders can rehearse onboarding now and fill the lines. Real onboarding starts when
  ENG-129 and ENG-119 are green, with no further work here.
- Then US3 and US4 before the first burst, and US5 for outreach (which can start immediately after the MVP, since it
  does not wait on the gates).
- Every phase ends with the docs guards (T026's commands) passing.
- Commits: `docs(SEED-01): …`, one concern each.

<!-- fixture: synthetic, modelled on a real Spec Kit 1.2.1 tasks.md (full file; provenance comment at the end, not on line 1).
     Human-only: T001 FOUNDER GATE [X], T023 [US5] FOUNDER: [ ], T028 FOUNDER: [ ]. Agent tasks: 25, one open (T009 rehearsal). -->
