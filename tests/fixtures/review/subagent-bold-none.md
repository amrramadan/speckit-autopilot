<!-- fixture: synthetic, modelled on a real code-review subagent report (reviewer-agent format excerpt, non-contiguous: "### Blockers\n\n**None.**", then "### Nits" with a "**1. …**" bold finding and "### Questions" with a "**Q1. …**" bold question; long lines truncated with '…') -->
Based on my review of all the key security-sensitive surfaces, here is my assessment:

---

## Security Review — spec-058 (branch `feature/058-operator-scope-reassign-safety`)

### Blockers

**None.**

---

### Nits

**1. `reassignZoneDay` passes `allowedZoneIds: null` to the scheduling service — no in-transaction scope re-check**

`apps/api/src/modules/ops/services/schedule-board.service.ts` (line ~540) calls `this.scheduling.reassignZoneDay(zoneId, serviceDate, technicianId, { expectedTechnicianId, actor: opsRef, allowedZoneIds: null })`. The c…
### Questions

**Q1. Is the badge code format schema synchronized between `packages/shared-types` and the API?**

`packages/shared-types/src/schemas/technician-badge-code.ts` is the canonical source; `apps/api/src/modules/scheduling/technician-badge-code.schema.ts` holds a byte-identical copy, enforced by a parity spec. The prior review conf…
