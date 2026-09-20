# Composer Phase 2 systems review

Reviewed the concrete Phase 2 specification and the evolving diff against
`ebc4720e0d4f18810353c646a093269a4ba7bc81`. Inspection HEAD was
`688a8bf18bc1ac9150cf7b73d35a93ae7e388fef` with uncommitted implementation.
This is an interim source review, not final-candidate acceptance.

Applied `yzmir-systems-thinking:using-systems-thinking`, its system-patterns
and causal-loop reference sheets, and the SME agent protocol. The relevant
system is the feedback cycle from pending decisions through user action,
provider/API completion, authoritative state, readiness, and visible actions.
Retained history is a separate accumulated record; it must not manufacture a
second pending action. No quantitative latency prediction is made.

## Confidence Assessment

**Overall Confidence: Moderate.** One reproduced projection defect; remaining
observations are source checks on an actively changing tree.

| Finding | Confidence | Basis |
| --- | --- | --- |
| S1: Equal-count replacement of a validation suggestion is not announced | High | Production projection probe and live-region effect dependencies |
| Single pending-action owner is an appropriate intervention | High | ChatPanel mounts DecisionPanel with shared acknowledgement/proposal callbacks; ToolCallCard action controls are removed |
| Inline source fallback remains provider mediated | High | `ChatPanel.tsx:2098` calls existing `sendMessage` with user data |
| Stale advisor advice is withheld when graph ownership changes | High | `completion_gates.py:300` compares fingerprints; line 310 clears stale suggestion |
| Settled guided replay is distinct from pending actions | High | `guided/GuidedDecisionSheet.tsx` renders settled components and replay, with only Close as an action |

### S1 — P2: decision identity loses changed remedies

`src/elspeth/web/frontend/src/components/chat/decisionPanelRows.ts:156`
assigns `suggestion:${i}`. The live region in `DecisionPanel.tsx:109-122`
depends on count-derived text and serialized row ids. Replacing suggestion A
with B at index zero leaves both dependencies identical. After Apply changes
the graph and validation returns a different outstanding remedy, a screen
reader user receives no arrival announcement for that new decision.

A Node probe transpiled and invoked the actual production projection. It
compared the row ids using the live region's arrival predicate. Raw result:

```json
{"same":false,"positive_added":true,"replacement":false}
```

The unchanged-input negative control stayed false; appending a second
suggestion made the positive control true. Distinct code/message/remedy
content at the same index still yielded `suggestion:0`, count 1. This is
source/projection reproduction, not assistive-technology acceptance.

Required repair: derive suggestion identity from its meaningful fields,
handle duplicate rows deliberately, and add a mounted live-region regression
that consumes actual projected rows. Identical rerenders must stay silent;
replacing a remedy at equal count must cause one announcement. Assess the
same transition for blocker advice updates sharing code/component.

## Risk Assessment

**Implementation Risk: Medium. Reversibility: Easy.**

| Risk | Severity | Likelihood | Mitigation |
| --- | --- | --- | --- |
| Changed remedy remains silent, leaving a keyboard/SR user waiting on obsolete feedback | Medium | Confirmed for equal-count replacement | S1 semantic identity and mounted regression |
| A successful API action removes a row before focus restoration | Medium | Requires browser verification | Exercise final and intermediate approval, rejection and fallback dismissal in each mode |
| Delayed refresh temporarily mixes readiness and proposal/interpretation stores | Medium | Not measured here | Verify reload and action completion with real backend responses, including version change |
| Durable advisor-envelope shape rejects older blocked envelopes | Medium | Explicit design choice | Preserve deployment disclosure and strict decoding; do not present this as backward compatible |

The spec improves the information path by placing the blocker and action
together. Its success depends on closing the loop after actions: refresh must
replace outdated advice, remove resolved decisions, and reveal newly arrived
ones. Retaining anchored read-only history preserves the evidence accumulated
by the session without feeding historical decisions back into pending state.
The shared pure projection and existing API handlers are appropriate places
to maintain that distinction.

## Information Gaps

- Final candidate SHA and delta were unavailable at this inspection.
- Browser evidence for focus, narrow scrolling, live-region mutations and
  guided/freeform parity is owned by the parent acceptance lane.
- Live-provider completion after Apply and amendment was not exercised by
  this reviewer. Synthetic state projection does not demonstrate it.
- The advisor decoder and state-reload implementation are still changing;
  this review does not certify their completed regressions or gate results.

## Caveats & Required Follow-ups

Fix S1 and recheck the final delta before treating this review as complete.
Review the final mode transitions as well as each mode in isolation: the
always-mounted announcement region and focused control need to survive the
relevant action lifecycle, including disappearance of the final pending row.

Use the parent lane's terminal frontend/backend, PostgreSQL, browser and
key-free trust-tier evidence for release decisions. This report does not
claim those gates passed and does not authorize clearing package signing.
No product files or broad test runs were changed by this review.
