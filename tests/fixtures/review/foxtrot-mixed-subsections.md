<!-- fixture: synthetic, modelled on a real code-review summary (orchestrator summary excerpt: "## Blockers" split into "### Security (primary)\nNone." and a "### Pipeline (…)" subsection that holds one bold finding; long lines truncated with '…') -->
# `/cc-review`: `feature/017-catalogue-duplicate-matching`

**Don't merge yet: there's one blocker, a small fix in the precision audit.** I found it reading `audit.py` myself; none of the reviewers reported it. The one blocker they did raise didn't hold up.

Base is `origin/main` (`6d93a20`), HEAD is `b4e1f08`, 52 commits. Nothing was edited, and the working tree is still clean. The security review was treated as primary because the diff adds tables holding operator emails;…

## Blockers

### Security (primary)
None.

### Pipeline (my own read of `audit.py`)
**The launch gate passes at 89.5% precision.** `matching/audit.py:198` rounds precision to two places, and `audit.py:214` then compares that rounded value to the 0.90 threshold. The requirements doc §2.3 requires at least 90%.

I ran `summarize` directly:
