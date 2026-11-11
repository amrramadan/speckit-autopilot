<!-- fixture: synthetic, modelled on a real Spec Kit 1.2.1 tasks.md excerpt ('Phase 0: Founder gate'; FOUNDER GATE tasks; hard-wrapped multi-line task bodies) -->
## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US7 from spec.md; Setup, Foundational and Polish tasks carry none

---

## Phase 0: Founder gate (before `speckit-implement`)

**Purpose**: the decisions an agent may not take (protocol §6; `docs/workflows/feature-start.md` §3a.5).

- [X] T001 FOUNDER GATE: a founder reviews `specs/008-listing-01-create-manage/spec.md` (with the plan's amendments to
  FR-003, FR-030, FR-034, FR-035, FR-063) and ticks the 16 boxes of
  `specs/008-listing-01-create-manage/checklists/requirements.md` (the agent's notes record a pass on all 16). Founder
  decisions F1–F14 (plan.md; F11–F14 from `/speckit-analyze`) are recorded, and the design gaps G1–G5 at the end of this file are answered or accepted.
- [X] T002 FOUNDER GATE: a founder runs quickstart §0 step 3 read-only against any shared database and pastes the
  output: `listing_review_jobs` must not exist and `listings` must hold fixture rows only. Otherwise implementation
  STOPS.

**Not tasks for agents**: the 16 setting values (plan OQ-1), every copy text (OQ-2), the ENG-127 dataset (OQ-3), the map
tiles URL for app builds (OQ-4), `/cc-review`, `/cc-ship`, the PR and its merge.

**Checkpoint**: T001 and T002 done → `speckit-implement` may start.

---

## Phase 1: Setup

- [X] T003 Commit the planning set. Run the four doc scripts and `lychee --offline --include-fragments .` first, and
  quote their output.
  - **Commit 1:** `git add specs/008-listing-01-create-manage/spec.md
    specs/008-listing-01-create-manage/checklists/requirements.md` ⟶ commit
    `docs(LISTING-01): specify and clarify create and manage listings`.
  - **Commit 2:** `git add specs/008-listing-01-create-manage/{plan,research,data-model,quickstart,tasks}.md
    specs/008-listing-01-create-manage/contracts/` ⟶ commit `docs(LISTING-01): plan, contracts and tasks`.
- [X] T004 Baseline. Run the per-workspace checks for `@delta/api`, `@delta/db`, `@delta/shared`, `@delta/web` and
  `@delta/mobile`, plus `pnpm contract:check`. Create `specs/008-listing-01-create-manage/report.md` with a "Baseline"
  section quoting the output (any red is named as pre-existing) and a "Founder gate" section (T001, T002). Verify
  environment health (protocol §8): compose healthy; the migration tip equals the last journal `when`; `db:check`
  passes.
- [X] T005 Run the `db-architect` agent (read-only) on `specs/008-listing-01-create-manage/data-model.md` and research
  R2, R4, R8, R11. Record its findings in report.md; fix the design docs for any accepted finding before T020
  ⟶ commit `docs(LISTING-01): db-architect review of the data model`.
- [X] T006 App dependencies (protocol §12: read each CHANGELOG for deprecations first and record what you read in
  report.md; confirm MapLibre RN's `pmtiles://` source support in the installed version):
  `pnpm --filter @delta/mobile exec expo install expo-image-picker expo-image-manipulator expo-location
  expo-file-system @maplibre/maplibre-react-native @shopify/flash-list`. Add their config plugins and the HE/EN
  permission strings (photos; location "when in use") in `apps/mobile/app.config.ts`; add a mock for each native module
  in `apps/mobile/jest.setup.ts`; rebuild the dev build (`expo run:ios` / `expo run:android`, never editing `ios/` or
  `android/` by hand); run `expo install --check` and the mobile checks
  ⟶ commit `chore(LISTING-01): add picker, location, map and list modules`.

---
