<!-- fixture: synthetic, modelled on a real code-review summary (orchestrator summary excerpt, non-contiguous: one "## <Lens> (`agent`)" section per reviewer, each opening with "**Blockers:** none." followed by a "**Nits**" label; long lines truncated with '…') -->
# MSG-01 review: in-app messaging branch

**Verdict: no blockers.** Nothing needs fixing before ship. There are 12 nits, 4 questions for you or the founders, and 3 questions that already have answers.

Five reviewers ran in parallel against `main` (common ancestor `5a1d9e3`, HEAD `f02b7c6`, 167 files). There was no web reviewer because nothing under `apps/web` changed. Security is listed first because the gateway hand…

## Security (`security-review`)
**Blockers:** none.

**Nits**
- `apps/api/src/messaging/handshake.ts:57` — when a request has no `Origin` header (native app clients), the check passes. That is correct, but there's no comment saying so, and someone may later "fix" it by requiring an `Or…
- `apps/api/src/messaging/message-limits.ts:28` — the Redis key for duplicate-message detection contains a keyed hash of the message body. It can't be reversed, but a Redis dump would show how many distinct messages each user s…
## API and contract (`api-reviewer`)
**Blockers:** none.

**Nits**
- `apps/api/src/messaging/event-router.ts:104` — there's an `as never` cast. It's justified, but list it in `docs/post-MSG-01-cleanup.md`.
## Database (`db-architect`)
**Blockers:** none. The two migrations are split correctly between generated and custom, and the custom one holds only the three triggers. Every new guard has a test that checks the constraint name, so the drill would c…

**Nits**
## Spec drift (`feature-validator`)
**Blockers:** none. There's no drift from the requirements doc §4.2, §4.6, App. A, App. D or the constitution, and all four requirement sections that cover messaging list MSG-01 in their `specs:` frontmatter.

**Nits**
