<!-- fixture: synthetic, modelled on a real Spec Kit 0.7.1 constitution.md (filled; tests only as 'Tests via vitest. Coverage gate: 85%' — no explicit tests-required phrase) -->
# Alpha Constitution

**Version:** 1.3.1
**Date:** 2026-10-11
**Status:** Active

> **SYNC IMPACT REPORT — v1.3.1 (2026-10-11), PATCH**
> - **Version change:** 1.3.0 → 1.3.1 (PATCH — record the existing framework baseline; no platform principle added, removed or weakened).
> - **§I. Technology Stack:** Next.js 16.3.8, matching the current storefront/admin/auth dependency pins. The existing App Router/RSC/Turbopack, React, strict TypeScript and crawlable SSR/ISR requirements remain authoritative.
> - **Rationale:** the root agent guide documents the 16.3 upgrade and subsequent patching. The spec-SEO analysis identified that the constitutional version wording had not followed those upgrades. The owner requested the correction on 2026-10-11; this amendment changes documentation, not dependencies.
> - **Synchronization:** update the current stack/version references in `README.md`, the root agent guide and `docs/conventions/NEXTJS_PATTERNS.md` as a separate documentation step. Templates read this constitution at runtime and require no changes.
>
> **Engine note (ADR-002, Accepted):** the commerce core is the **custom Next.js + Drizzle** stack (this §I), confirmed by ADR-002 — superseding the "hosted commerce engine" wording in BRD D-03/PLAT-01/02. Switching to a hosted engine later would be a constitutional **MAJOR** amendment to §I/§V.

### II. Code Quality

- TypeScript strict mode everywhere. No `any` except typed adapters at third-party boundaries.
- No `@ts-ignore` / `@ts-expect-error` without inline rationale + cleanup-doc entry.
- ESLint `--max-warnings=0` on every commit. No `eslint-disable` without inline reason.
- Tests via vitest. Coverage gate: 85% on all spec-owned modules.
- Code reviews required for all PRs. Drizzle migrations require two reviewers (Schema Council rotation, see ENG-ADR-001).

### III. External Boundary Contract (ENG-ADR-002: Stub-and-Activation)
