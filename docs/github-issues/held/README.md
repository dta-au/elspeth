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

Found by the technical-writing pass, not by the triage that preceded it — writing an
issue for a reader who will actually pick it up is a stronger check on whether the
defect still exists than reading the ticket was.
