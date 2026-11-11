<!-- fixture: synthetic, modelled on a real Spec Kit tasks.md excerpt (lower-case '[x]' + bold IDs '**T001**') -->
---
description: "Task list — spec-CONTENT Compliance Content CMS"
---

# Tasks: Compliance Content CMS (spec-CONTENT)

**Input**: Design documents from `specs/spec-CONTENT-content-cms/` (plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md, design-baseline.md)
**Tests**: INCLUDED — required by Constitution §II (85% coverage) + §VIII (sanitizer red-team, audit pairing) and the spec's acceptance scenarios.
**Organization**: by user story (US1 public reading P1 · US2 admin authoring P2 · US3 settings readiness P3) over a shared Foundational phase.

## Format: `[ID] [P?] [Story] Description`

- **[P]** = different files, no dependency → parallelizable.
- Commit convention: `<type>(spec-CONTENT): T-NNN <subject>`; one concern per commit; typecheck+affected tests green before each commit.

> **Pre-task (already done, separate commit):** the `.specify/scripts/bash/common.sh` branch-regex fix that unblocked the speckit scripts is a **tooling** change — commit it on its own (`chore(tooling): accept non-numeric spec keys in speckit branch check`), NOT bundled with spec-CONTENT code.

---

## Phase 1: Setup (shared infrastructure)

- [x] **T001** Scaffold `@acme/content` package (`packages/content/{package.json,tsconfig.json,CLAUDE.md}` + `src/index.ts` barrel), mirroring `@acme/crm`. Third-party deps `react-markdown@^10.1.0` + `remark-gfm@^4.0.1` (**run the Protocol §12 currency check at install**); workspace deps `@acme/db`/`@acme/core`/`@acme/audit`/`@acme/rbac` + `drizzle-orm` + `zod`; `react`/`react-dom` as peers. JIT package — `exports` → `.ts` source, no build; extensionless relative imports (ENG-ADR-006).
- [x] **T002** [P] Wire `@acme/content` into both apps: add to `apps/storefront` + `apps/admin` `package.json` deps and `transpilePackages` (next config). Confirm `pnpm-workspace.yaml` already globs `packages/*`.

---

## Phase 2: Foundational (BLOCKS all user stories)

**⚠️ No user-story work starts until this phase is complete.**

- [x] **T003** Add `content_pages` table to `db/schema/compliance.ts` per [data-model.md](./data-model.md): columns (`slug` unique, `status` `$type<ContentPageStatus>` default `'draft'`, `title_he`/`body_he` NOT NULL, `title_en`/`body_en` nullable, `created_at`/`updated_at` TIMESTAMPTZ), + `ContentPageStatus` union + `$inferSelect`/`$inferInsert` types. No relations.
- [x] **T004** [P] Add `'contentPages'` to `AUDITED_ENTITIES` (`packages/config/audited-entities.mjs`) with a spec-CONTENT/FR-011 comment.
- [x] **T005** Add `content_publish` `AuthorityArea` to `@acme/rbac` — extend the union in `packages/rbac/src/types.ts` + add the `content_publish: ['catalogue']` row in `packages/rbac/src/matrix.ts`; update the matrix/union-parity tests (admin superuser passes automatically).
- [x] **T006** `@acme/content` `src/constants.ts` (`CONTENT_CMS_FLAG='spec-content.compliance_cms'`, `CONTENT_CACHE_TAG='content-pages'`, `RESERVED_SLUGS`) + `src/errors.ts` (`ContentError` + `ContentErrorCode` union).
- [x] **T007** Register flag `spec-content.compliance_cms` in `packages/core/lib/flag-catalog.ts` (`development:true, staging:false, production:false`, non-empty description). **Same commit as T006** (flag-catalog bidirectional lockstep — the `CONTENT_CMS_FLAG` reference must exist in source when the entry lands). Verify `packages/core` flag-catalog test green.
- [x] **T008** Generate migration `0028_spec_content_content_pages.sql` (`drizzle-kit generate`) — must include: `uq_content_pages_slug`, `content_pages_status_check`, `content_pages_locale_complete_check` (published ⇒ both locales complete), partial `idx_content_pages_published`. **Schema Council ×2** review; apply via `db:migrate`. Additive only — no ALTER on populated data (Protocol §6).

**Checkpoint:** schema + migration + RBAC area + flag + package constants ready.

---
