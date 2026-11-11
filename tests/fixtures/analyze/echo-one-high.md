<!-- fixture: synthetic, modelled on a real Spec Kit /speckit.analyze report (assistant text; one HIGH finding; metrics written inline as '· 0 critical issues'; offer is 'suggest concrete edits'; long lines/cells truncated with '…'; coverage rows trimmed) -->
## Specification Analysis Report

`/speckit.analyze` is read-only, so no files were changed. The spec, plan and tasks agree with each other well. I found one high-severity gap, a security point in the API contract, plus a few medium and low ones.

| ID | Category | Severity | Location(s) | Summary | Recommendation |
|----|----------|----------|-------------|---------|----------------|
| C1 | Constitution §IV / security | HIGH | contracts/api.md:8; tasks.md T022; `apps/api/src/modules/portal/controllers/auth.controller.ts:37-45` | `POST /v1/portal/session/credential-change` creates a session, but the contract doesn't exempt it from the r…| Mark the route `@SkipIdempotency` with the sign-in rationale, and say so in contracts/api.md and T022. Keep…|
| I1 | Inconsistency | MEDIUM | spec.md:386-388 vs FR-026 (C6), research D-08 | An edge case says requests made "with an existing session after the change are refused until acceptance". Th…| Reword the edge case: "bookings are refused, and the portal redirects every page to acceptance". |
| A1 | Ambiguity | MEDIUM | spec.md:488 (FR-031); research D-09 | "Meet the policy" doesn't say which one. The one-time password uses the strong staff policy (12 characters,…| Name the sign-up rule in FR-031, and the strong policy for the one-time password in FR-028. |
| G1 | Coverage gap | MEDIUM | tasks.md T028; data-model.md Settings | Two new settings stop things working until someone sets them. `merchant_terms_version` empty blocks every si…| Add a T028 bullet: list the four keys in the release checklist's settings table and the soft-launch record, with the ter…|
| G2 | Coverage gap | LOW | spec.md:379, 391 | Two edge cases have no test: a suspended merchant can still print labels, and two admins resetting at once m…| Add one case each to T006's route test and T021's e2e, or relax the edge-case wording. |
| G3 | Coverage gap | LOW | spec.md:213 (US4 sc. 2); tasks.md T014 | "Makes no request for that desk's data" is proven only by a unit test of `requireDesk`, not by any page. | In T014, add one page-level test showing `apiFetch` is not called when the desk is hidden. |
| G4 | Coverage gap | LOW | quickstart.md US6 | The new public `GET /v1/portal/terms` has no quickstart step. | Add a curl step: unset gives `null`, set gives the version. |
| I2 | Inconsistency | LOW | plan.md:141 | The source tree lists a `portal-support(new).service.ts`, but T011 reads the setting in the controller. | Drop it from the tree. |
| I3 | Inconsistency | LOW | research.md:187 | The route-census note says "one handler removed, one added". The spec actually adds six routes, three of the…| Restate it as "check the census floors once all routes land". T011 and T029 already re-run the spec. |

**Coverage summary**

| Requirement | Has task? | Task IDs | Notes |
|---|---|---|---|
| FR-001–004 | Yes | T006, T007 | C1 correction applied |
| FR-005–009 | Yes | T008–T010 | FR-009: settings are already admin-only; no new test |

**Constitution alignment:** C1 above is the only issue. The rest is clean:
- §II: every new value is a setting.
- §IV: contract first.
- §V: display only.
- §VII: explicit tests.
- §IX: personal data stays in the encrypted store, audit entries carry none, and the privacy register moves with each migration.

**Unmapped tasks:** none. T001, T028 and T029 are setup and wrap-up by design.

**Metrics:** 36 requirements · 29 tasks · 100% coverage · 1 ambiguity · 0 duplicates · 0 critical issues.

### Next Actions

- No critical issues, so `/speckit.implement` can start. Fix **C1** first, or at the latest inside T002/T022. It's a one-line contract change, and leaving it means a replayed request returns a stored session token.
- I1, A1 and G1 are small edits to spec.md and tasks.md. G2–G4 and I2–I3 are optional tidying.

Would you like me to suggest concrete edits for the top 4 (C1, I1, A1, G1)?

## Extension Hooks

**Optional Hook**: git
Command: `/speckit.git.commit`
Description: Auto-commit after analysis

Prompt: Commit analysis results?
To execute: `/speckit.git.commit`
