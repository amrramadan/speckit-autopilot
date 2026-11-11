<!-- fixture: synthetic, modelled on a real code-review summary (orchestrator summary excerpt: "## Blockers" opens with the prose "There is one. It's arguable, …" then a "1. **…**" finding with bold-labelled sub-bullets; long lines truncated with '…') -->
# /cc-review: spec-TOGGLES admin settings panel

I reviewed `main...4d2a91e7c`: 88 files, 4 reviewers (security first, then API, web and DB). I didn't run the integration reviewer because no external-service boundary changed. The reviewers made no changes to tracked f…

## Blockers

There is one. It's arguable, so it's yours to call.

1. **The export route writes a log line but no audit row.** In `apps/admin/app/api/admin/settings/export/route.ts:39`, the export calls only `logger.info('settings.exported', { count })`. The project rules list "data export"…
   - **Against blocking:** the export holds no customer data, and the route's own header says "Logged with the count only", so this was a deliberate choice.
   - **Missing:** neither the spec nor the plan settles whether exporting settings needs an audit row.
   - **Possible fix:** write one `auditLog.write` row (`setting.export`, `{ count }`, admin id, client IP) after `buildSettingsSnapshot()`.

## Must do before deploy (governance, not code)
