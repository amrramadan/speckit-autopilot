<!-- fixture: synthetic, modelled on a real code-review summary (orchestrator summary excerpt: "## Blockers (2)" count heading with two "**1. …**" / "**2. …**" bold findings, each with plain sub-bullets; long lines truncated with '…') -->
# /cc-review: spec-072 live technician location (`main...7a2e5d0`, 64 commits, 241 files)

All five reviewers have reported. Security was the main reviewer because the branch adds a new real-time channel and handles technician GPS data. On the main security surfaces it found nothing wrong: the socket ticket, orig…

## Blockers (2)

**1. API: locations can be written into a shift that has already closed** (`apps/api/src/modules/tech-location/tech-location.service.ts:64-98`). I checked this in the code.
- The service reads the shift and checks `endedAt` before the write transaction starts.
- The transaction then inserts the fixes without locking or re-reading the shift.
- If End shift or the nightly close commits in between, the closed shift gets extra locations after it was cut down to its last fix. The ops map also receives a location for a technician whose shift is closed.
- Fix: re-read the shift with `findShift(…, tx, { forUpdate: true })` inside the transaction and stop if it has ended, as the close paths already do.

**2. Field app: the iOS Keychain migration does nothing on phones already signed in** (`clients/field/src/services/secure/token.ts:51-58`).
- The migration re-saves the token and technician ref through `setItemAsync`, then writes a "done" marker so it never runs again.
- The reviewer read the secure-store library's iOS code (v14.2.1): saving over an existing item updates only its value, never its access class. I confirmed the JS side. I couldn't find the Swift source on this machine, so the…
- Effect: on any iPhone signed in before this build, the token can't be read while the phone is locked, so no locations are sent until the technician unlocks or opens the app. Only signing out and back in fixes it.
- Fix: delete each item before re-saving it, and write the marker only after both saves succeed, handling a crash between the delete and the save.
- The test (`test/token-accessibility.test.ts:11-22`) uses a `Map` mock that simply overwrites, so it can't catch this. `clients/field/AGENTS.md` also says the token is "migrated on restore", which isn't true yet.
