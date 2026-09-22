# Held back from the GitHub import

These issues were written to the same standard as the rest, then held, because
verification against the tree found the work they describe already present.

Publishing an issue that asserts a defect a contributor would immediately find fixed
is worse than publishing nothing: it wastes the first hour of whoever picks it up, and
it makes the whole set less trustworthy.

They are not deleted, because in each case the ticket is broader than what was verified
and someone closer to the work has to make the call.

| File | Verified present at `release/0.8.1`, 2026-09-23 | Why it is held, not closed |
|---|---|---|
| `advisor-end-gate-judged-a-dead-prompt-multi-query-llm-quer.md` | `_advisor_query_option_values` (`service.py:10283`), the `prompt_template_in_use` marker (`service.py:8477`), and `_ADVISOR_SUMMARY_VALUE_KEYS` admitting `queries` | The ticket names five surfaces — evidence, injection pre-scan, degeneracy signal, review card and anchor, planner teaching. Only the evidence half is confirmed. |
| `composer-blocking-readiness-state-and-its-fix-affordance-a.md` | `DecisionPanel` mounted at `ChatPanel.tsx:2324`; `readiness.blockers` read at `workspaceStatus.ts:73` | The tracker row is already in a verifying state with the work recorded as pushed. The close belongs to whoever is verifying it. |

Both carry a `wait:decision` label and a comment recording this evidence in the tracker.

**To publish one:** confirm the remaining scope is genuinely outstanding, rewrite it to
describe only that, and move the file back into `issues/`.

**To drop one:** close the tracker row and delete the file.

## Held for a different reason — the publication review, 2026-09-23

The two below were not held because the work is already done. They were held by the
pre-publication review in `docs/reviews/2026-09-23-aps-publication-review.md`, which read
all 46 files against the question of whether anything in them reflects badly on the
Australian Public Service. It returned 34 PASS, 10 FIX and these 2 HOLD.

**Read this before assuming a hold contains anything.** Every file in this directory is
tracked and has been on `origin/release/0.8.1` — a public repository — since it was
written. Withholding the import withholds *amplification*: a GitHub issue is indexed,
notified, cross-linked and not deletable by the accounts available here. It does not
withhold the text. For anything where the content itself is the concern, holding the
import alone is theatre and the tracked file has to be dealt with too.

| File | Why it is held | The decision needed |
|---|---|---|
| `sentinel-custody-projection-fails-open-when-live-source-op.md` | The one file of eight security-adjacent candidates where the reviewer could not answer "what does an attacker do differently after reading this?" with *nothing*. An unfixed control that fails open and emits an affirmative false assurance, in a product whose purpose is a trustworthy audit trail. | Does it meet this project's own bar in `../README.md` for a **private security advisory**? If yes, the remedy is the advisory *plus* redacting or removing the tracked file — not skipping the import. If the fix lands first, publish it in full and do not water it down. |
| `release-0-8-1-pre-merge-findings-code-review-suite-state-2.md` | Not a security matter. A dated snapshot of a branch under review rather than an issue — it says of itself that "none of the numbers above are current", its per-finding evidence lives in child items outside this set, and it carries the set's most quotable process lines. No mechanical edit repairs "this file is a snapshot". | The reviewer offered publish / re-scope to the wrapper defect / drop, conditional on which wrapper misreported the exit code. **Settled since:** the source item says "the harness task notification reported exit code 0", so it is the agent harness, not `scripts/full-suite-gate.sh` — the project's own log recorded `SUITE exit=1` correctly. Agent tooling is excluded from this migration, so re-scoping is unavailable and dropping is the recommendation. The tracker row still has open children, so the close is not automatic. |

Neither file was watered down and no technical claim in either was softened. The review's
standing instruction applies to both: a project that finds its own fail-open and describes
it this precisely is a project working correctly.

Found by the technical-writing pass, not by the triage that preceded it — writing an
issue for a reader who will actually pick it up is a stronger check on whether the
defect still exists than reading the ticket was.
