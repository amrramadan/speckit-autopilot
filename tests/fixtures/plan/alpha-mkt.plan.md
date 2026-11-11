<!-- fixture: synthetic, modelled on a real Spec Kit plan.md (header + Technical Context, Constitution Check and Complexity Tracking blocks; 'PASS, see Complexity Tracking'; design-complexity rows) -->
# Implementation Plan: Per-person admin permissions and a Marketing role

**Branch**: `feature/spec-MKT-admin-permissions-marketing` | **Date**: 2026-11-07 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `specs/spec-MKT-admin-permissions-marketing/spec.md`


## Technical Context

**Language/Version**: TypeScript 5.7 (strict), Node.js 24 LTS
**Primary Dependencies**: Next.js 16.4.3 (App Router: RSC, Server Actions), React 19.2, Drizzle ORM 0.45 +
`postgres` (PostgreSQL 18), Zod v4, `@acme/ui` (Dialog, ConfirmDialog, Tabs, Badge, Table, Textarea, Input, Button,
Label), lucide-react. Extends `@acme/rbac` (levels, metadata, `AdminAccess`), `@acme/identity` (`admin-access.ts`,
`current-admin.ts`, `admin-jwt.ts`, `admin-rbac-mutations.ts`), `@acme/core` (flag catalog), `@acme/config`
(`AUDITED_ENTITIES`), and the package actors / 25 predicates that take a role today: the 19 packages that import
`@acme/rbac` (`analytics`, `assistant`, `campaigns`, `catalog`, `content`, `crm`, `inventory`, `loyalty`,
`marketplace`, `notifications`, `orders`, `payments`, `popups`, `promotions`, `reconciliation`, `returns`, `shipping`,
`support`, `wallet`) plus `procurement`, which carries a role only to build the `@acme/inventory` actor
(`receive.ts`). `campaign-tool` and `reviews` have no role check of their own (their admin pages gate). **No new package, no new third-party dependency ⇒
Protocol §12 not triggered.**
**Storage**: PostgreSQL 18 — one additive migration `0146_spec_mkt_admin_access.sql` (schema review ×2): new table
`admin_access_overrides`, `admins.access_version`, `admins_role_check` widened for `marketing`. History reuses
`admin_audit_log`. No settings rows.
**Testing**: vitest — rbac pure tests (matrix 6 × 38, frozen parity snapshot, resolution order, guards),
`@acme/identity` integration on the test DB (writes, guards, stale version, role-change clear, fail closed), admin
nav-manifest drift + home byte-identity, dictionary coverage, the **View-actor write sweep** (SC-003), the
**Marketing exposure sweep** over RSC payloads (SC-006); Playwright smoke of the editor at 360 px and 1280 px, both locales.
**Target Platform**: the single pilot server (systemd units, `APP_ENV=staging`, env-list flag provider + spec-FLAGS
overlay) and local Docker dev.
**Project Type**: monorepo web application (three Next.js apps + `@acme/*` packages + workers).
**Performance Goals**: one extra primary-key-prefix query per authenticated admin request (≤ 38 rows), skipped for
Admin-role accounts and when the per-person flag is OFF; menu-badge cache hit rate unchanged for people on role
defaults (same key per role).
**Constraints**: every flag OFF ⇒ byte-identical access and rendering for the five existing roles (SC-004); fail
closed; server-side enforcement independent of the View-mode flag; RTL + LTR locales; usable at 360 px with no
page-level horizontal scroll.
**Scale/Scope**: ≤ 20 admin accounts; 38 areas; 6 roles; ~94 files change signature; 10 pages gain a View mode;
1 new page.

## Constitution Check

*GATE: passed before Phase 0; re-checked after Phase 1 (below).*

| Principle | Verdict | Evidence |
|---|---|---|
| §I Stack | PASS | Next.js + Drizzle + PostgreSQL only; no new package or dependency. |
| §II Code quality | PASS | Strict TS, no `any`; the role-string overload is removed so the compiler, not review, finds every check; vitest ≥ 85 % on `@acme/rbac` and `admin-access.ts`; ESLint `--max-warnings=0`. |
| §III External boundary | PASS (N/A) | No external service. |
| §IV UI & CSS | PASS | `design-baseline.md` produced by the UI design pass before any component code; native radios in a fieldset (no new primitive); logical CSS and tokens only; copy in both locales with fixed level vocabulary. |
| §V Database | PASS, see Complexity Tracking | `drizzle-kit generate --name`, schema review ×2, `timestamptz`, varchar + CHECK, metadata-only ADD COLUMN with a constant default, no money, no customer data. One recorded deviation: `area` has a format CHECK, not a value-list CHECK. |
| §VI Feature flags | PASS | Three `spec-mkt.*` flags, production default OFF; turn-on order in the runbook; enabling in production is an operator act (Protocol §5). |
| §VII DevOps | PASS | Permissions are business state in the DB with an audit trail, like roles today; no config moves out of env. |
| §VIII Security | PASS | Server-side checks at every page and action (SECURITY_PATTERNS §6/§7); actor from the verified session only; Zod at the boundary; CSRF on the actions; fail closed; locked areas refused in the domain, not just the UI; audit row in the same transaction (RBAC-02, §13); 2FA unchanged; data minimisation at View (unit cost absent from payloads); no PII or confirmation text in logs or audit. |
| §IX No business-rule constants | PASS | No tunable numbers (R-14). Locked / sensitive / kind are security mechanism beside the matrix, as the current Admin-only lists are. |
| §X Workflow | PASS | Branch `feature/spec-MKT-…`; atomic commits `<type>(spec-MKT): T-NNN …`; BRD RBAC-01 delta and a Roadmap row tracked as docs tasks. |

**Post-design re-check (after Phase 1)**: PASS. The design added no dependency, no external boundary, no business
constant and no customer data. The spec's FR-032 setting was removed rather than kept unused (§IX in reverse).

## Complexity Tracking

| Item | Why needed | Simpler alternative rejected because |
|---|---|---|
| Signature change across ~94 files | Per-person access must reach every check; leaving the role-string overload callable makes every unmigrated site a silent bypass of a per-person downgrade | AsyncLocalStorage principal (smaller diff) is invisible to the type system and silently falls back to role defaults outside the request scope. |
| `admin_access_overrides.area` with a format CHECK, not a value-list CHECK (§V "varchar + CHECK") | Areas are added in code roughly monthly; rows for a removed area must be ignorable, not block its removal | A value-list CHECK needs a schema-review migration for every new area and a DROP/ADD to remove one; the TS union + R-6 step 7 already reject unknown values on read and write. |
| Third flag `spec-mkt.view_modes` | Lets the read-only rendering ship and be checked before anyone can be given View, and keeps flag-OFF rendering byte-identical for existing roles on the formerly session-only pages | Folding it into `person_permissions` would change those pages for existing roles at the same moment per-person access first goes live. |
