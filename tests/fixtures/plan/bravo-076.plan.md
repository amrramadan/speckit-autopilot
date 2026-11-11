<!-- fixture: synthetic, modelled on a real Spec Kit plan.md (header + '## Technical Context' + '## Constitution Check' blocks; principle cell 'Violations block merge until amended' in a PASS row: a real false positive of the old gate) -->
# Implementation Plan: R1 Scope Records — Constitution, BRD and ADR Amendments

**Branch**: `feature/076-r1-scope-records` | **Date**: 2026-10-01 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/076-r1-scope-records/spec.md`


## Technical Context

**Language/Version**: Markdown (records). One TypeScript 5.7 strict test file, `apps/api`, Node.js 24
LTS, Jest e2e config.

**Primary Dependencies**: None added. Existing tooling only:
- `scripts/check-doc-index.mjs`
- `scripts/check-agents-md.mjs`
- markdownlint-cli2 0.23.3 (CI-pinned)
- Jest

**Storage**: N/A. No schema or migration change. `ropa.md` must still agree with the schema at the
latest migration (`0065` today, re-verified at implement time).

**Testing**:
- `pnpm test`: the unit tests, including the ROPA and secret-inventory doc tests.
- The guard e2e test, which needs the dev Postgres for its `globalSetup`.
- The doc gates: `check:doc-index` and `check:agents-md`.
- markdownlint.
- `scripts/ci-local.sh` before the PR. Results are recorded LOCAL per T56.

**Target Platform**: The repository's governing documents.

**Project Type**: A documentation/records change inside the existing monorepo.

**Performance Goals**: N/A.

**Constraints**:
- Every commit is green: the doc gates and `pnpm test`, plus the guard test from the amendment
  commit on.
- Root `AGENTS.md` must not grow past its current 221 lines.
- No ADR heading may change.
- No business value is copied into the BRD (constitution §II).
- The PR cannot merge before IN-23.

**Scale/Scope**:
- 6 user stories and 26 FRs;
- 1 constitution amendment, 17 BRD files (the §0 changelog, the §0.1 decision log and 15
  sections) and 16 ADR notes;
- 6 new ADRs;
- 56 register entries re-derived, plus a new §K;
- 26 ROPA tables;
- 6 spec status marks and about 8 stale-text sites.

## Constitution Check

*This spec **is** the amendment. So the check has two layers. First: does the work follow the
constitution's own Amendment Procedure and Governance? Second: does any principle the amendment does
**not** change get violated along the way?*

| Principle / rule | Gate | Verdict |
| --- | --- | --- |
| **Governance — Amendment Procedure** | Amend via PR + matching BRD update/ADR; semver bump; history entry | **PASS**. One PR carries the constitution 3.0.0, BRD 3.1.0, 16 ADR notes and C-055…060; the history entry follows the existing shape (D-01, D-13, D-20). |
| **Governance — Conflict Resolution** | Constitution > BRD > ADRs > Roadmap | **PASS**. The commit order follows the citation order (D-20); no lower document is left contradicting a higher one at merge. |
| **Governance — Compliance Review** | Violations block merge until amended | **PASS**. This is the mechanism. Each R1 contradiction is amended, not waived. |
| **I. Stack** | A locked-layer major bump needs amendment + ADR | **PASS**. Expo SDK 56→57 gets ADR-C-055. §I line 48 is amended: the Payments part gets the R1 qualifier (the two payout providers are kept but unused), and the Comms tail becomes SMS only. |
| **II. No business-rule constants** | Values live in the DB | **PASS**. §20 records the decisions and points at the IN-8 settings; no numbers are copied (D-12). The day rate's refuse-when-unset obligation is kept. |
| **III / V / IX** | — | **Amended by this spec**. The new wording is in `contracts/constitution-amendment.md`. |
| **IV. Contract** | OpenAPI unchanged | **PASS**. No contract touched. |
| **VI. Geodata** | — | **PASS**. The pin rule is unchanged. IX-E2 time-boxes the interim exact-location deletion position and does not alter the pin principle. |
| **VII. Testing** | Empirical verification; explicit tests on ledger paths | **PASS**. The guard test is re-pointed and run; every SC has a command (quickstart). No ledger code is changed. |
| **VIII. DevOps** | Drizzle sole migrations owner | **PASS**. No migration. |
| **IX. Privacy** | ROPA current; DPO; no new PII flow without an entry | **PASS**. The 26 tables are mapped and RN-18/RN-05 corrected. The DPO is named before merge (IN-23, D-19). The same-commit rule is added for later specs. |
| **Dev workflow — Branch, atomic commits, tracker mirror** | `feature/<NNN>-<slug>`; one concern per commit; one issue | **PASS**. `feature/076-r1-scope-records`; the commit list is below; ENG-1320. |

**Gate result: PASS. No Complexity Tracking entry.**

**Post-design re-check (after Phase 1):** still PASS. The design adds one convention, the spec
"superseded" status mark (data-model §8). It reuses the ADR precedent rather than inventing a shape.
It adds two register namespaces through method.md's own write-back procedure.
