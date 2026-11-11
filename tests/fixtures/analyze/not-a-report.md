<!-- fixture: synthetic, modelled on a real Spec Kit /speckit.analyze report (assistant text of the follow-up message after remediation, which is NOT an analysis report; long lines truncated with '…') -->
I've fixed all 10 findings across the spec-042 docs. Nothing is committed, and no code changed; markdownlint passes on every edited file.

**C1, the critical one: I chose one encryption key per delivery.** This was a design choice the analysis left to you, so check it first. Door media are now encrypted under the parcel's own claims key, with the tracking number as th…
- **Returning recipients can always be delivered to.** A later parcel gets its own key, so an earlier key's expiry never gets in the way.
- **No expiry mid-delivery.** A parcel's key has no retention anchor until the delivery completes, and the expiry sweep never destroys a key without one.
- **Each proof has its own claims period**, which is what the spec already said. That also resolves H2 without changing the spec's meaning.
- **Erasure requests still report door evidence as retained.** A recipient's request finds it through a small new counts-only lookup (`CLAIMS_EVIDENCE_INVENTORY`) that the pod module provides to the erasure service.
- **Keys of parcels never delivered are destroyed** by the orphan sweep once the parcel is closed (terminal) and holds no media.

I rejected three alternatives (the full reasoning is in research R4):
- **Re-creating a destroyed recipient key** would reverse spec-012's rule that a destroyed key is never revived.
