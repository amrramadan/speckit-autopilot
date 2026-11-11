---
name: autopilot-feature-start
description: "Start a Spec Kit feature for an autopilot run: create the feature branch from an up-to-date base, then run /speckit.specify. Generic fallback installed by `speckit-autopilot init`; replace it with your own cc-feature-start skill if you have one."
argument-hint: "SPEC_ID [description]"
disable-model-invocation: true
---

# Start a feature (generic)

Arguments: $ARGUMENTS  (first word = spec id, the rest = optional description)

The autopilot pins the branch and feature directory through environment variables. Use them; never invent numbers.

1. Read `GIT_BRANCH_NAME`, `SPECIFY_FEATURE_DIRECTORY` and `SPECIFY_FEATURE` from the environment
   (`printenv GIT_BRANCH_NAME SPECIFY_FEATURE_DIRECTORY SPECIFY_FEATURE`). If `GIT_BRANCH_NAME` is empty,
   derive `feature/<spec-id>-<slug>` from the arguments.
2. Verify the tree is clean (`git status --porcelain`) and you are on the base branch. If not, stop and say why.
3. `git switch -c "$GIT_BRANCH_NAME"` (if it already exists locally: `git switch "$GIT_BRANCH_NAME"`).
4. Create the feature directory if missing (`mkdir -p "$SPECIFY_FEATURE_DIRECTORY"`).
5. Write the spec by following the repo's Spec Kit specify command (`/speckit.specify` or `/speckit-specify`)
   with the description. If you cannot invoke it yourself, print exactly one line
   `/speckit.specify <description>` and stop — the autopilot runs it next.

Rules when `AUTOPILOT_UNATTENDED=1`: do not wait for confirmation, do not open a PR, do not touch the issue tracker.
