<!-- fixture: synthetic, modelled on a real Spec Kit 1.2.3 constitution.md (filled; excerpts of preamble, a principle tail, Testing Discipline, DevOps, Governance; 'require explicit tests before merge') -->
# Bravo Constitution

This constitution encodes the non-negotiable engineering principles for Bravo — an independent, asset-light, **market-rate** local delivery service (reliability-led, priced at parity ~1.0×, not a premium). It is binding for every session that touches code. Where it conflicts with a ticket, a chat thread, or convenience, the constitution wins. Amendments follow the Governance section.

> **Pivot note (v2.0).** Bravo was reconciled from a multi-tenant delivery-platform thesis to an **independent delivery service, not a platform**. Principles that the pivot reversed are flagged in the amendment history, `.specify/memory/constitution-history.md`.

## Amendment history

Every amendment is recorded in `.specify/memory/constitution-history.md` (the Sync Impact
Report: newest first, what changed, why, and why that semver tier). The Amendment Procedure
below still requires an entry for every amendment; it no longer requires the entry to load
with this file.
- The canonical location entity is `(geo-pin + verified phone)`, **not** a street string (**ADR-C-005**).
- Capture a map pin at booking; the **driver GPS-verifies on delivery**, so the canonical pin self-corrects — the **self-improving pin DB is the moat** (BRD §2.1). Drivers run a **consistent roster on consistent PostGIS cluster zones**.
- The pin DB carries a **stated lawful basis + retention policy** (applicable data-protection law, ADR-C-015), not indefinite retention. No LLM at MVP; freeform-address→pin is a later option.

### VII. Testing Discipline
- Empirical verification is mandatory (AGENTS.md §2): no claim without a test run, curl, or log line.
- The **money ledger, the driver collected-funds ledger + payment-gating, the SLA/refund logic, and the insurance-reserve accounting** require explicit tests before merge.
- Per-workspace gate before commit: typecheck + lint + affected tests green. The ship script automates it.

### VIII. DevOps & Infrastructure
- **Host-for-apps + Docker-for-infra in dev.** `infra/docker-compose.dev.yml` runs Postgres 18 + PostGIS + Redis; apps run on host. Lint hooks invoke `eslint`/`ruff` directly on host. *(No workflow engine at MVP — durability is outbox + `pg-boss`, ADR-C-036.)*

### Compliance Review
- `/speckit.analyze` and code review check work against this constitution. Violations block merge until resolved or explicitly amended.

### Conflict Resolution
- Order of authority: **Constitution > BRD > ADRs > Roadmap > tickets/threads.** A conflict surfaced mid-implementation is a stop-and-surface event, not a judgment call.

**Version:** 4.1.0 | **Ratified:** 2026-05-31 | **Last amended:** 2026-11-05
