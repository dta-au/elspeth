# Composer Phase 2 systems review

Reviewed the concrete Phase 2 specification and the evolving diff against
`ebc4720e0d4f18810353c646a093269a4ba7bc81`. Inspection HEAD was
`688a8bf18bc1ac9150cf7b73d35a93ae7e388fef` with uncommitted implementation.
Final frontend delta recheck inspected candidate
`e15a1fc6286d1f84b6ba5664d8bdab7e4ca3349b` against release base
`627b72cf1`. The frontend had no uncommitted delta during this recheck.
S1 is resolved; no remaining confirmed systems-review blocker was found in
the reviewed delta. This is source and focused-test acceptance, not final
browser/backend or signed-release acceptance.

Applied `yzmir-systems-thinking:using-systems-thinking`, its system-patterns
and causal-loop reference sheets, and the SME agent protocol. The relevant
system is the feedback cycle from pending decisions through user action,
provider/API completion, authoritative state, readiness, and visible actions.
Retained history is a separate accumulated record; it must not manufacture a
second pending action. No quantitative latency prediction is made.

## Confidence Assessment

**Overall Confidence: Moderate.** The reproduced projection defect is repaired
and verified with focused tests. Browser and provider behavior remain outside
this review's direct evidence.

| Finding | Confidence | Basis |
| --- | --- | --- |
| S1 resolved: equal-count replacement is announced and reordering stays silent | High | Semantic identity in the projection and passing mounted live-region regression |
| Single pending-action owner is an appropriate intervention | High | ChatPanel mounts DecisionPanel with shared acknowledgement/proposal callbacks; ToolCallCard action controls are removed |
| Inline source fallback remains provider mediated | High | `ChatPanel.tsx:2104` calls existing `sendMessage` with user data |
| Guided exit restores freeform decision behavior | High | `ChatPanel.tsx:2023` derives actual mode; Apply and interpretation refresh use the same discriminator |
| Stale advisor advice is withheld when graph ownership changes | High | `completion_gates.py:300` compares fingerprints; line 310 clears stale suggestion |
| Settled guided replay is distinct from pending actions | High | `guided/GuidedDecisionSheet.tsx` renders settled components and replay, with only Close as an action |

### S1 — P2, resolved: decision identity lost changed remedies

The initial `src/elspeth/web/frontend/src/components/chat/decisionPanelRows.ts:156`
assigned `suggestion:${i}`. The live region in `DecisionPanel.tsx:109-122`
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

The final projection derives suggestion identities from component, message
and severity, and blocker identities from code, component, detail and advice.
A per-content occurrence index distinguishes duplicates without making the
identity of distinct decisions depend on ordering. The mounted regression
`announces changed projected suggestions at equal count but ignores reordering`
feeds the actual projector into the live region and checks the content
clear/restore transition, then observes zero mutations for reordering.
Another mounted regression checks zero mutations on identical rerenders.

Reviewer rerun from the frontend directory:

```text
npm test -- src/components/chat/DecisionPanel.test.tsx src/components/chat/decisionPanelRows.test.ts
Test Files  2 passed (2)
Tests       35 passed (35)
exit_code: 0
```

Output was captured in `/tmp/composer-phase2-systems-review-final-vitest.log`
and read after process completion. HEAD remained
`e15a1fc6286d1f84b6ba5664d8bdab7e4ca3349b` after the run. These checks resolve
the reproduced S1 defect; they do not measure a physical screen reader.

## Risk Assessment

**Implementation Risk: Medium. Reversibility: Easy.**

| Risk | Severity | Likelihood | Mitigation |
| --- | --- | --- | --- |
| Changed remedy remains silent, leaving a keyboard/SR user waiting on obsolete feedback | Medium | Reproduced defect repaired | S1 semantic identity and mounted regression now pass |
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

- Any subsequent product delta needs proportionate review; planned epoch
  bookkeeping changes were outside this frontend recheck.
- Browser evidence for focus, narrow scrolling, live-region mutations and
  guided/freeform parity is owned by the parent acceptance lane.
- Live-provider completion after Apply and amendment was not exercised by
  this reviewer. Synthetic state projection does not demonstrate it.
- This review does not certify advisor decoder, state-reload backend tests,
  full frontend suite, or terminal backend gate results reported by other lanes.

## Caveats & Required Follow-ups

S1 is closed for this review. The source recheck also confirms that exited
guided sessions use the freeform Apply and interpretation-refresh paths,
while active and completed guided sessions keep Apply gated with a visible
explanation. The corresponding ChatPanel regressions explicitly assert
enabled/disabled Apply and the freeform resolution callback after exit.

Complete browser verification of mode transitions as well as each mode in isolation: the
always-mounted announcement region and focused control need to survive the
relevant action lifecycle, including disappearance of the final pending row.

Use the parent lane's terminal frontend/backend, PostgreSQL, browser and
key-free trust-tier evidence for release decisions. This report does not
claim those gates passed and does not authorize clearing package signing.
No product files or broad test runs were changed by this review.
