<!-- fixture: synthetic, modelled on a real code-review summary (orchestrator summary excerpt: bold verdict line, "## Blockers" with a "**1. …**" numbered-bold finding and bold-labelled sub-bullets, then "## Nits" with a bold group label; long lines truncated with '…') -->
# spec-DASHVIZ review (`feature/spec-DASHVIZ-admin-dashboard-charts`)

**Verdict:** one blocker, a handful of nits and four open questions.

I compared `main` (merge-base `3b1e07d2a`) with HEAD `9c4f6a810`: 81 files, 47 commits. Four reviewers ran in parallel: web, api, db and security (the last in an isolated worktree). I skipped the integration reviewer be…

## Blockers

**1. Charts ignore the warehouse read gate (security, confirmed).** The charts only draw, and only fetch their data, when the viewer could open the source page. That is the spec's own rule (R-9). The code checks this w…
- **The gap:** the `/warehouse` entry in `apps/admin/lib/nav-manifest.ts:142` gates on the feature flag only and is marked "session-only". But the warehouse page itself refuses non-warehouse roles once `spec-rbac.wareh…
- **Effect:** on the home, a Support or Marketing admin now gets weeks of stock, which is derived from sell-through rate, on the reorder rows (`dashboard.tsx:198`). On `/reports/ops`, the stock-cover and shelf-life charts use the same…
- **Scope:** the home reorder and shelf-life panels already had this hole on `main`. This branch adds new data behind it. The RBAC test (`dashviz-rbac.int.test.ts`) turns every flag on and never covers a role without war…
- **Fix:** make the `/warehouse` nav entry match what the page enforces (needs `warehouse` authority when the read gate is on), or add an explicit `hasAuthority(role, 'warehouse')` check before the stock reads. Then add…

## Nits

**Security**
- `reports/ops/page.tsx:47`: `getReorderRisk()` runs for every viewer, before `showWarehouse` is known. This was already true on `main`, but it doesn't match the gating used for every other chart read.
- `funnel-vm.ts:18`: `stage` goes into the URL without `encodeURIComponent`. The stage values are a closed set, so there's no real risk; this is tidiness only.
