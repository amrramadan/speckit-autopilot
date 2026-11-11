---
name: autopilot-review
description: "Report-only code review of the current feature branch against its Spec Kit spec, plan and tasks, ending with a machine-readable REVIEW_SUMMARY line. Generic fallback installed by `speckit-autopilot init`."
argument-hint: "BASE_BRANCH"
disable-model-invocation: true
---

# Review the feature branch (report only)

Base branch: $ARGUMENTS (default `main`)

Do NOT edit files, commit or push. Only report.

1. Find the feature directory: `$SPECIFY_FEATURE_DIRECTORY` if set, else the `specs/*` directory matching the branch.
   Read `spec.md`, `plan.md`, `tasks.md` and `.specify/memory/constitution.md` if present.
2. Diff: `git diff --stat <base>...HEAD`, then read every changed file in full.
3. Check, in order: correctness bugs; requirements in spec.md that the diff does not meet; tasks ticked but not
   done; constitution violations; security issues (injection, authz, secrets); missing or meaningless tests;
   anything that breaks existing behaviour.
4. Classify every finding as **blocker** (must fix before merge), **nit** (optional) or **question**.
   Cite `path:line` and say what to change.

Output exactly this structure:

```
## Blockers
- path:line — problem — fix        (or the single line: None)

## Nits
- ...                              (or: None)

## Questions
- ...                              (or: None)

REVIEW_SUMMARY blockers=<n> nits=<n> questions=<n>
```

The last line is mandatory and its counts must match the lists.
