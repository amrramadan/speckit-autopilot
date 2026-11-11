---
spec: "115"
slug: test-seams-payout-maker-checker
title: "Test Seams That Block Re-testing, and the Payout Maker-Checker Rule"
priority: P1
layer: API + Ops console + Runbooks
linear:
  issueId: ENG-1204
  shared: true
---
<!-- fixture: DERIVED from spec/bravo-115.spec.md (synthetic); edit: a bare bracketed NEEDS-CLARIFICATION marker without a question, outside code, in spec.md (must count) -->

# Feature Specification: Test Seams That Block Re-testing, and the Payout Maker-Checker Rule

**Feature Branch**: `feature/115-test-seams-payout-maker-checker`
**Created**: 2026-11-08
**Status**: Draft
**Input**: `/speckit.specify 115` on Linear ENG-1204 ("Release hardening 3 of 3"), Roadmap Block 12, spec-115. ENG-1204 also holds OBS-101, the OBS-88 and OBS-42 security check, spec-114 and fix waves W4, W5 and W7, so this spec has no Linear issue of its own (three-issue split agreed 2026-11-05). Owner: [redacted]. Milestone: *R1: Build on mocks*.

## Clarifications

### Session 2026-11-08

- Q1: May a transfer be recorded for a payout whose details nobody revealed in the console? → A:
  Yes, as today. Only a person who revealed it is barred; nothing is backfilled for reveals made
  before this ships.
- Q2: If a revealer resubmits a transfer someone else already recorded, what do they get back? → A:
  The refusal. The person check runs before the replay lookup; nothing is written either way.
- Q3: Is the operator who adopts an imported payout also barred from recording its transfer? → A: No.
  Adoption is not a reveal; an adopter who also reveals is barred as a revealer.
- Plan-time correction (2026-11-08, research R-06): the destination control has no "fail-closed"
  kind. R1 flags a payout through "no destination" and "card only", which cover QA-22. User
  Story 3 scenario 2 now matches QA-15: a newer registration is a warning on batch review, not a
  refused transfer.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The admin who revealed a payout cannot record its transfer (Priority: P1)

An admin reveals a prepared payout's bank details in the weekly payout console so the transfer can
be made at the bank. When the transfer is recorded, the system refuses it if the person recording is
someone who revealed that payout's details, and says why. A different finance user or admin records
it instead. Today one admin can do both, and the two-person control is procedural only.

**Why this priority**: it is the one production change in this spec. It closes a money control the
release checklist (RC-14) depends on, and QA-14 is a predicted fail until it lands.

**Independent Test**: prepare a payout, reveal it as admin A, try to record its transfer as admin A
(refused, nothing written), then record it as finance user F (accepted).

**Acceptance Scenarios**:

1. **Given** a payout awaiting transfer whose details admin A revealed, **When** admin A records its
   transfer, **Then** the request is refused as a conflict under its own named code, the console says
   a different person must record it, the payout stays awaiting transfer, and no ledger, audit or
   transfer record is written.
2. **Given** the same payout, **When** finance user F (who did not reveal it) records the transfer
   with the same details, **Then** it is accepted exactly as today.
3. **Given** a payout revealed by admin A and then by admin B, **When** either A or B records the
   transfer, **Then** it is refused; **When** finance user F records it, **Then** it is accepted.
4. **Given** admin A's refused attempt, **When** admin A retries the identical request, **Then** it is
   refused again in the same way: a refusal is never replayed as a success.
5. **Given** a transfer recorded by F, **When** admin A (the revealer) resubmits F's identical
   recording, **Then** it is refused with the same named reason and nothing is written (Q2).

---

## Requirements *(mandatory)*

### Functional Requirements

**Payout maker and checker (DEC-32, production)**

- **FR-001**: The system MUST record, durably and outside the audit chain, every person who
  successfully revealed a payout's bank details, against that payout, at the moment of the reveal and
  inside the same commit as the reveal's audit entry.
- **FR-002**: The record of FR-001 MUST hold only the payout, a pseudonymous reference to the person and
  the time. It MUST hold no bank detail and no personal data, and it MUST NOT be soft-deleted.
- **FR-003**: [NEEDS CLARIFICATION] Recording a payout's transfer MUST be refused when the person recording is anyone who
  revealed that payout (FR-001), whatever their role.
- **FR-004**: The refusal MUST be a conflict with its own named error code, distinct from every other
