<!-- fixture: synthetic, modelled on a real Spec Kit 1.2.1 constitution.md (filled; starts mid-way through a leading HTML comment; excerpts of §I, §VI, Governance; §VI 'No PR without tests') -->
Already stated in a BRD shard (pointer left, sentence not copied): docs/_restructure/constitution-moves.md.
Check: scripts/check-constitution-coverage.sh. Spec Kit loads this file from the same path.
-->

# Delta Constitution

**Version**: 2.13.2 | **Ratified**: 2026-05-19 | **Last Amended**: 2026-10-29

## Core Principles

### I. Technology Stack

The platform is a **pnpm monorepo** with three deployables (mobile app, web, API) and shared contract packages. Every service MUST use the
canonical technology for its domain — no alternatives without a constitutional amendment.

- Hand-rolled endpoint contracts — and any second artifact that could independently claim contract truth — are FORBIDDEN.
- **D-13 is permanent and non-negotiable:** **No LLMs, no generative AI, no cloud CV, no model inference of any kind,
  anywhere in the platform.** The lean MVP has no AI at all — every classification, suggestion, and ranking decision
  (category auto-suggest, search) is deterministic. If you find yourself reaching for any LLM/AI SDK, STOP — you are
  violating a locked product decision a session instruction cannot override.
- **Rules:** `docs/conventions/platform-stack.md`, `docs/conventions/drizzle-queries-migrations.md` (database, search),
  `docs/conventions/dev-environment.md` (OS, local secrets), `docs/conventions/git-workflow.md` (issue tracking).
- **BRD:** clients §2; stack, contract, storage, maps, payments, OTA §5; OTP §3.1; search §3.3; chat §3.5; moderation
  §3.7, App. B; analytics §3.11; categories and payments §3.12; residency and transfers §5, App. D.

### VI. Testing Discipline

- **Every business rule from the BRD has an explicit named test.**
- **No PR without tests**: any PR introducing or modifying business logic MUST include test updates.
- **Rules:** `docs/conventions/testing.md` (stack, coverage gate, E2E split and flows, named gates).

## Governance

- **Amendment Procedure**:
  1. Propose the amendment via PR to `.specify/memory/constitution.md`. The file is **no longer edit-locked** (founder
     decision, 2026-10-17): it is kept in step as decisions land, provided every amendment carries what point 2 requires.
  2. The amendment MUST include: rationale, impact analysis, version-bump justification, and a sync-impact-report header (see the top of this file).
  3. Version increment: **MAJOR** (principle removed/redefined), **MINOR** (new principle / material expansion), **PATCH** (wording clarifications).
- **Compliance Review**:
  - Every code review MUST verify compliance with this constitution.
  - Monthly, the **cash + time gate** (BRD §6) governs whether building continues at all — it sits above this constitution.
  - Violations = high-priority technical debt recorded in the spec cleanup-doc.
