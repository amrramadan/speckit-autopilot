<!-- fixture: synthetic, modelled on a real code-review summary (orchestrator summary excerpt: "## Blockers" as a "1. **…**" / "2. **…**" numbered list with indented bold-labelled sub-bullets; long lines truncated with '…') -->
# Spec-014 review: not ready to ship yet

I found **3 blockers, all in the repair path**, and I confirmed each by running code. The sandbox boundary itself held up in review, but its hostile-program tests weren't run here. None of the three reviewers found a bl…

**Reviewed:** `main` (`2f8c4d1`) to `a07e93b`, 196 files. `security-review` was primary because the diff touches `pipeline/prompts/v1/*`; `pipeline-reviewer` and `db-architect` also ran. There's no `apps/web` change, so…

## Blockers

1. **A sandbox outage during a repair condemns the program** (`pipeline/foxtrot_pipeline/synthesis/jobs.py:702`)
   - **Cause:** `_one_attempt` counts `sandbox_unavailable` as a failed attempt, but `contracts/sandbox-runner.md` says `unavailable` means no run happened and the job retries later. Availability is only checked once, w…
   - **Reproduced:** with the sandbox up at the start and `unavailable` on every run, I got `outcome=escalated model_calls=3 status=rejected`.
   - **Impact:** a sandbox restart mid-repair wastes up to 3 paid calls, rejects the program and holds the tenant until an operator releases it.
   - **Test gap:** `test_run_repair.py:214` only covers a sandbox that's already down at the start.
   - **Fix direction:** end the job without counting the attempt or escalating.

2. **A budget deferral restarts the repair attempt count** (`jobs.py:748`)
   - **Cause:** the loop always starts at attempt 1 and never reads earlier attempts for the incident.
   - **Reproduced:** with budget refused on attempt 3 and the job then re-run, I got `model_calls=5 attempt_numbers=[1, 1, 2, 2, 3]`. That breaks FR-027 and SC-004, and FR-027 calls this cap a security control (R19).
   - **Possibly worse (from reading, not run):** `repair-<id>` and `repair-<id>-deferred` have different job IDs, so both could run at once and spend more.
   - **Fix direction:** resume from the incident's logged attempts, and allow only one repair job per incident.
