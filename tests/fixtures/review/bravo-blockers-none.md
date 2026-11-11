<!-- fixture: synthetic, modelled on a real code-review summary (orchestrator summary excerpt, non-contiguous: "## Blockers\nNone." directly under the heading, then "## Should fix" and "## Questions"; long lines truncated with '…') -->
# /cc-review: `feature/061-pick-priority-and-queue-trimming` vs `main`

This branch has no blockers. I found 4 small fixes worth making, a handful of optional ones, and 2 questions for you.

The branch is 39 commits ahead of `main`. 8 of them come from the unmerged `docs/e2e-catalogue-decisions` branch (DEC-12, DEC-03, DEC-27, DEC-31, DEC-08); the other 31 are spec-061. Five reviewers looked at it: securit…

## Blockers
None.

## Should fix

**A missing priority or order time puts an item at the front instead of the back.** These code paths can't be reached today, but each one does the opposite of what it intends:
- `apps/api/src/modules/picking/pick-queue.service.ts:1186`: the sort uses `ranks.get(a)?.rank ?? 0`. Ranks start at 1, so an item with no rank would sort ahead of rank 1. It should fall back to `MAX_SAFE_INTEG…
## Questions
- **Database:** the hold table's two single-column foreign keys overlap with its composite one. Are they there on purpose, for navigation?
- **Database:** nobody ran `drizzle-kit check` on the new migration snapshot. The DB reviewer only spot-checked it. If CI's schema-drift step passes on this branch, that settles it.

I haven't changed any code. I deleted the security reviewer's temporary worktree, which had no changes in it.
