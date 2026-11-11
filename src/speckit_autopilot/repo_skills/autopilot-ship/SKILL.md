---
name: autopilot-ship
description: "Commit, push, open one PR, merge it with a merge commit and return to the base branch — only when the first argument line is the autopilot authorisation line. Generic fallback installed by `speckit-autopilot init`."
argument-hint: "AUTOPILOT-AUTHORIZED: commit push pr merge switch-base <instructions>"
disable-model-invocation: true
---

# Ship the feature branch

Arguments:

$ARGUMENTS

## Authorisation

Proceed without asking ONLY if the first line of the arguments is exactly
`AUTOPILOT-AUTHORIZED: commit push pr merge switch-base`. Otherwise show the plan and ask the user to confirm each step.

The authorisation never covers: force pushes, `--admin`, `--no-verify`, deleting the base branch, or issue-tracker
updates.

## Steps

1. Determine the base branch from the arguments (default: `git symbolic-ref --short refs/remotes/origin/HEAD`, minus `origin/`).
2. `git status --porcelain`. Stage files **by path** (never `git add -A` / `git add .`). Do not stage `.autopilot/`,
   secrets, `.env*` or build output. Include the `decisions.md` file the arguments name.
3. Commit with a Conventional Commits message summarising the feature (`feat(<scope>): ...`), referencing the issue key.
4. `git push -u origin HEAD`.
5. If no PR exists for this branch (`gh pr list --head <branch> --state all`), open one:
   `gh pr create --base <base> --title "<commit subject>" --body "<summary of spec + tasks done>"`.
   Never open a second PR.
6. Merge: `gh pr merge <N> --merge` (wait for required checks if the repo requires them: `gh pr checks <N> --watch`).
7. `git switch <base> && git pull --ff-only origin <base>`.
8. Print the PR URL and the merge commit SHA.

If any step fails, stop and report the exact error. Do not retry destructive operations.
