<!-- fixture: synthetic, modelled on a real Spec Kit plan.md (header through Constitution Check + Complexity Tracking; 'FAIL' mid-cell in a passing row; '### Post-Design Re-Check (Constitution)') -->
# Implementation Plan: Observability & Pre-Launch Performance Validation (spec-OBS)

**Branch**: `feature/spec-OBS-perf-observability` | **Date**: 2026-07-19 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `specs/spec-OBS-perf-observability/spec.md`

## Summary

Deliver the **Bucket-3/4 observability + pre-launch-validation delta** that spec-026 (NFR hardening) did not ship, without restating spec-026's substrate (OTel traces/metrics/logs, health endpoints, blocking Lighthouse CI, backups/restore). Four independently-testable slices, each behind a default-OFF `spec-obs.*` feature flag, all **strictly verify-only** (report READY/NOT-READY, never auto-flip a flag or activate a vendor — mirrors spec-LAUNCH FR-012):

1. **Field RUM incl. INP** — a consent-gated, PII-free `web-vitals` client beacon (`POST /api/vitals`) that feeds a **new** custom OTel metric instrument in `@acme/observability` (the substrate today emits only auto-instrumentation server histograms — there is no client RUM); plus an INP assertion added to the lab Lighthouse config.
2. **Pre-launch load test** — a k6 scenario (browse → PDP → search → cart) with config-driven thresholds + environment labelling, producing a PASS/FAIL artifact; run on-demand, never in the merge path.
3. **Percentile dashboards, SLOs & alerting** — p95/p99 Grafana views + SLO + alert rules over the **existing** auto-emitted OTel HTTP-duration histogram (`http.server.request.duration`); a memory-pressure health check + `Alerter`-boundary wiring for uptime/OOM; and a verify-only error-tracking readiness gate that reports NOT-READY while stub-only.
4. **Bundle analyzer + per-chunk budget** — `@next/bundle-analyzer` wired into the three `next.config.ts`, plus a CI per-chunk budget check extending the spec-026 Lighthouse resource-size totals.

A **new verify-only package `@acme/perf-readiness`** (parallel to `@acme/launch`) aggregates the six gates into a single READY/NOT-READY verdict with a CLI (`pnpm perf:check`) and an Admin-only dashboard, gated on a new `performance_readiness` RBAC area. Production observability **vendor / OTLP export-target** selection stays deferred to ENG-ADR-009 / spec-HOST; this spec builds the collection, budgets, dashboards, SLOs, and readiness gates that sit on top.

## Technical Context

**Language/Version**: TypeScript 5.x (strict) on Node.js 24 LTS. k6 scripts are plain ES (run by the k6 binary, not Node). Grafana dashboard/alert provisioning is JSON/YAML config.
**Primary Dependencies**:
- **New third-party (Protocol §12 currency-check at install):** `web-vitals` (runtime — `apps/storefront`, browser bundle; v4+ API — `onLCP`/`onCLS`/`onINP`, `onINP` supersedes the removed `onFID`); `@next/bundle-analyzer` (devDependency — the three apps). **k6** is an external binary invoked on-demand, **not** an npm dependency (no lockfile entry).
- **Reused:** Next.js 16.2 (App Router — one Route Handler for the beacon, RSC + client island for the admin dashboard, three `next.config.ts` wrappers), React 19.2, `@opentelemetry/{api,sdk-metrics}` (already deps of `@acme/observability` — add a `Meter` + histogram instruments), Zod v4 (beacon boundary), `@acme/observability` (extend: `recordWebVital` sink), `@acme/core` (`isFlagEnabled`/`FLAG_CATALOG`, `getSetting`, `getIntegrations` → `rateLimiter`/`alerter`, `logger`/PII-scrub, `middleware/security-headers`, extend `health` with a memory check, `cookies` consent read), `@acme/launch` (the ratified verify-gate pattern this mirrors — **reference, not import**), `@acme/rbac` (new `performance_readiness` area), `@acme/identity` (`getAuthenticatedAdmin`), `@acme/integrations` (`Alerter` boundary + stub — reused, not new), `@acme/ui` (`AppShell`, `bi/charts.tsx` server-SVG primitives, i18n). **New internal package `@acme/perf-readiness`.**
**Storage**: **No PostgreSQL schema change, no migration, no schema review.** RUM samples are aggregate OTel metrics (not a Postgres entity — Clarifications 2026-07-13). Thresholds/budgets/sampling/freshness are `store_settings` / `policy_setting` rows (several UNSET at launch, §IX). Lab budgets (INP/LCP/per-chunk) live in CI config files (`lighthouserc.json`, `budget.json`, `bundle-budget.json`) — config values, not code constants (FR-014).
**Testing**: vitest (unit — consent-gate, beacon Zod schema, same-origin guard, `recordWebVital` no-op-when-OTel-off, memory-pressure check, each gate evaluator, verdict aggregation, RBAC parity); Playwright (existing e2e harness — a consent-gated-beacon smoke); `lhci` (INP lab assertion); k6 `thresholds` (load PASS/FAIL). **No new Pact contract** (no new external boundary).
**Target Platform**: Storefront browser (client beacon) + Node.js route handlers + Admin RSC + CI + the dev observability stack (OTLP collector → Grafana, Jaeger).
**Project Type**: Web monorepo (`apps/*` + `packages/*` + root `scripts/` + `.github/workflows/`).
**Performance Goals**: LCP ~2.5s (NFR-01) proven under concurrency (US2); INP goes from unmeasured → measured in both lab and field (US1); p95/p99 visible with SLOs (US3).
**Constraints**: **Verify-only** — no gate flips a production flag or activates a vendor (FR-011/012). **Consent-gated, PII-free** RUM (privacy policy; equal-prominence with the spec-025 analytics pixels). **Flags default OFF** in production (merge ≠ turn-on). **Delta-only** — no restatement of spec-026. Production vendor/OTLP endpoint **deferred** (ENG-ADR-009 / spec-HOST).
**Scale/Scope**: Pre-launch validation for the single-seller storefront; four slices, one new package, ~one new route, one admin page, one k6 harness, CI extensions.

## Constitution Check

*GATE: evaluated pre-Phase-0 and re-checked post-Phase-1 (both PASS — see "Post-Design Re-Check").*

| Principle | Applies? | Disposition |
|---|---|---|
| **§I Tech stack** | Yes | Next 16.2 / TS strict / Node 24 / pnpm. Two new npm deps (`web-vitals` runtime, `@next/bundle-analyzer` dev) → **Protocol §12 currency check fires at install**. k6 = external binary, not a dep. **PASS.** |
| **§III External Boundary Contract** | Yes | **No new boundary.** The error-tracking readiness gate *introspects* the existing `setLogSink` seam + the absence of a direct error-tracking SDK dep (identity/absence checks only — like `@acme/launch`'s adapters gate) and reports NOT-READY **without activating a vendor**. Uptime/OOM alerting uses the **existing** `Alerter` interface + stub (`packages/integrations`). No vendor SDK in business logic. **PASS.** |
| **§IV UI & CSS Discipline** | Yes | The Admin performance-readiness page + client island are a UI surface ⇒ **UI design pass → `specs/spec-OBS-perf-observability/design-baseline.md` REQUIRED before any component code** (ENG-ADR-007; enforced by the `design-baseline` gate + `require-design-baseline` hook). Logical CSS only; p95/p99 charts reuse the token-only server-SVG `bi/charts.tsx`. **PASS (gated as a task).** |
| **§V Database Discipline** | No | No schema/migration/money columns. **N/A.** |
| **§VI Feature Flags** | Yes | Five flags `spec-obs.{field_rum,load_test,slo_alerting,bundle_budget,perf_readiness_dashboard}` (the 5th gates the FR-012 verdict surface — the Admin readiness page + `perf:check` CLI), all `production:false`. Catalog completeness invariant honored (each key has a referencing constant). **PASS.** |
| **§VIII Security & Compliance** | Yes | Public beacon route: Zod `safeParse` at the boundary, same-origin guard, PII-free payload (test-enforced), rate-limit, full `securityHeaders`, `Cache-Control: no-store`, PII-scrubbed logging, no `console.*`. Consent-gating reuses spec-025 (privacy policy, equal-prominence). **PASS.** |
| **§IX No Business-Rule Constants** | Yes | Sampling rate, beacon flood-cap limit/window, memory soft-ceiling, load-test freshness-window → `store_settings`/`policy_setting` (resolver-fallback; several UNSET). CI lab budgets are config files, not code constants. **PASS.** |
| **§X Workflow** | Yes | Feature branch cut; atomic commits `<type>(spec-OBS): T-NNN …`; issue-tracker mirror on ship. **PASS.** |
| **Verify-only invariant (FR-011/012, spec-LAUNCH parity)** | Yes | Every gate is read-only w.r.t. launch/production state; verdict is advisory; UNKNOWN (evidence unreachable) distinct from FAIL; READY only when all required gates PASS. **PASS.** |

**No violations → Complexity Tracking is empty.** The one net-new package (`@acme/perf-readiness`) is a standard internal package parallel to `@acme/launch`, not a complexity exception (rationale in Structure Decision).

### Post-Design Re-Check (Constitution)

Re-evaluated after the contracts + data-model: **still PASS on every gate.** No new external boundary was introduced by the design (error-tracking + OTLP export stay deferred; Alerter is reused). No business-rule constant landed in code (all numbers are settings/CI-config). The only UI surface (admin dashboard) is gated behind the mandatory design-baseline task. No schema change surfaced. Verify-only invariant holds across all six gates (each is read-only; UNKNOWN≠FAIL; READY only when all required PASS).

## Complexity Tracking

*No constitution violations — table intentionally empty.*
