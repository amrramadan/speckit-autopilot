<!-- fixture: synthetic, modelled on a real code-review subagent report (reviewer-agent format excerpt: bold pre-amble sections, then "## Blocker" with "**Severity**: blocker" / "**Anchor**" / "**Finding**" / "**Suggested action**" lines, then "## Questions" with "**Severity**: question"; long lines truncated with '…') -->
Based on reading the full diff and the relevant source files, here are my findings.

---

**AGENTS.md change — intentional, not the `next dev` corruption**

The diff touches only one table row (the `<EditForm>` description gains the `secondaryAction` clause). It does NOT contain the multi-paragraph "agent-rules" block that `next dev` appends. Safe to merge as written.

**Returns page and `bidi.ts` — in scope for spec-066**

The returns page change removes a local `isolate()` helper and imports the shared one from `@/lib/bidi.ts`. Spec-066 extracted the function because `CarrierSummaryLine` needed the same utility. This is a legitimate in-sco…

---

## Blocker

**Severity**: blocker
**Anchor**: `apps/admin/src/app/[locale]/(ops)/shipping/CarrierSummaryLine.tsx:1`

**Finding**: `CarrierSummaryLine.tsx` calls `useTranslations()` and `useLocale()` — both React hooks that `next-intl` requires a Client Component context to run — but the file has no `'use client'` directive. Without it,…

**Suggested action**: Add `'use client';` as the first line of `CarrierSummaryLine.tsx`, OR rewrite the component to use `getTranslations()` / `getLocale()` as an `async` Server Component and move the `formatCount` call i…

---

## Questions

**Severity**: question
**Anchor**: `apps/admin/src/app/[locale]/(ops)/shipping/actions.ts` (new `saveShipmentDraftAction`, lines ~310–351 in the diff)

**Finding**: `saveShipmentDraftAction` verifies the CSRF token (`verifyCsrfToken`) and validates input with Zod, but contains no explicit session or role check (`getOpsSession()` / `requireSurface('shipping')`) before issu…
