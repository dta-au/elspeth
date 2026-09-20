# Composer Phase 2 LLM specialist review

Review date: 2026-09-20. Initial snapshot: evolving worktree at HEAD
`688a8bf18bc1ac9150cf7b73d35a93ae7e388fef`, compared with
`ebc4720e0d4f18810353c646a093269a4ba7bc81`. Uncommitted implementation was
changing during inspection; this is not a frozen final-candidate approval.
Read-only product review; only this report was written.

Delta recheck: `e15a1fc6286d1f84b6ba5664d8bdab7e4ca3349b`, based on
`627b72cf1`. F1 is closed in the inspected source and regression definitions.
`ChatPanel.tsx:2023–2024,2077–2087` now derives `guidedDecisionMode` from the
same active-guided predicate as the rendered workspace, plus completed guided.
Apply is disabled in those modes with visible freeform-editor guidance.
Exited-to-freeform sessions retain the existing provider-backed handler.
`ChatPanel.test.tsx:10640,10667,10682` covers completed guided, freeform after
exit, and active guided; the latter checks disabled Apply and no freeform send.
This is a bounded preservation of the previously supported authoring modes,
not an implementation of guided suggestion application.

Applied the requested `yzmir-llm-specialist:using-llm-specialist` skill,
agentic patterns, safety and evaluation reference sheets, and SME protocol.
The review targets tool authority, prompt approval, suggestion provenance,
graph ownership and guided/freeform behavior. It does not recommend provider
model changes or changes to the agent loop.

## Confidence Assessment

Overall confidence: Moderate. Findings below distinguish source evidence
from runtime evidence; no live provider calls or test suites were run by this
reviewer.

| Finding | Confidence | Basis |
| --- | --- | --- |
| **F1 — resolved: active guided Apply previously dispatched through freeform composition.** The final inspected delta disables this newly exposed action in active/completed guided mode and keeps its advice visible. | High for source and regression closure | Initial dispatch evidence: `ChatPanel.tsx:2072–2085`, `useComposer.ts:49–57`, `sessionStore.ts:2073–2186`. Closure: current `ChatPanel.tsx:2023–2024,2077–2087`; `ChatPanel.test.tsx:10640,10667,10682`. |
| Proposal/interpretation action authority is retained in the inspected code. | High, source only | `ChatPanel.tsx:2332–2347` passes existing accept/reject callbacks and `AcknowledgementStack`; the stack still uses `AcknowledgementCard`, whose resolver dispatches existing `resolveEvent` with accepted/amended choices (`hooks/useInterpretationResolver.ts`). |
| Source fallback and suggestion application still request provider authoring. | High, source only | `ChatPanel.tsx:2081–2085` and `2100–2105` submit conversational prompts; no graph construction was added to the panel. F1 concerns which provider-backed mode receives the request. |
| Advisor suggestion belongs to the graph-bound completion fact and becomes null for a changed graph. | High, source only | `execution/completion_gates.py:175–224`, `272–280`, `298–310` persist/parse suggestion alongside `for_graph`, then clear suggestion on fingerprint mismatch. Clean writer output is an empty gate envelope. |
| Advisor suggestion text uses existing backend wording, not model findings or frontend invention. | High, source only | `composer/service.py:10683–10753` computes fixed suggestion wording; `10783–10811` carries it onto readiness. `DecisionPanel.tsx` displays that field as React text. Tests added in `test_completion_gates.py` exercise raw finding withholding, malformed values, graph change and clean clearing; their execution remains the parent lane's responsibility. |
| Reload advice is recomputed from the current owned composition without provider authoring. | High, source only | `sessions/routes/composer/state.py:613–621` reconstructs and validates the current record; `_helpers.py:819–890` uses the existing response projection. New `test_state_reload_suggestions.py` checks graph replacement and blob path redaction in guided/freeform cases. |
| Existing prompt display and approval resolver are reused rather than replaced. | High for reuse, Moderate for end-to-end behavior | `AcknowledgementStack.tsx` continues passing live `compositionState` to `AcknowledgementCard`; `AcknowledgementCard.tsx:410–420,514` retains view gating and current prompt segment rendering. No resolver, approval-hash, or execution implementation changes were present in the inspected diff. |

Source paths above are relative to `src/elspeth/web/`, except named test files
under `tests/unit/web/`. Line numbers describe the initial evolving snapshot.

## Risk Assessment

Implementation risk: Medium. Reversibility: Easy for frontend routing; Moderate
for the required durable envelope field because old blocked records reject it.

| Risk | Severity | Likelihood | Mitigation |
| --- | --- | --- | --- |
| F1 could dispatch a pipeline-changing instruction through the wrong mode. | High | Closed in the rechecked source | Preserve the active/completed-guided gate and exited-to-freeform regression. Any future guided Apply must use its stage admission and history semantics. |
| A stale suggestion could be attributed to a graph the advisor never reviewed. | High | Guard present in inspected implementation | Execute graph mutation/clean clearing persistence regressions and serial PostgreSQL acceptance. |
| Required suggestion field rejects older blocked completion envelopes. | Medium | Deliberate, documented format change | Keep deployment limitation explicit; no unapproved shared database reset or compatibility fallback. |
| A text-only “retry/review” suggestion can imply guaranteed clearance even though a non-mutating turn may preserve the durable gate. | Medium | Existing wording, now more visible | Keep it advice rather than adding a fabricated one-click review action. Browser evidence must distinguish actual provider mutation/clearance from intercepted success. |

## Information Gaps

- F1 source/regression delta has been rechecked at the SHA above. Browser
  acceptance and terminal backend gates remain in progress.
- This review did not run a live provider or confirm final browser prompt
  approval, amended prompt execution, or guided Apply acceptance.
- Inspected `phase2-rebased-full-frontend.log`: `Test Files 271 passed (271)`
  and `Tests 4866 passed (4866)`. The adjacent `frontend-exits.json` attributes
  Vitest, typecheck, ESLint, Stylelint and build exits zero to the frontend
  agent's terminal outputs at the rechecked SHA. These files are under
  `.claude/lanes/composer-phase2-20260920/logs/` in the primary checkout.
  This reviewer did not rerun those gates. The parent owns backend gates, PostgreSQL validation,
  writer-binding reconciliation and package signing evidence.
- Epoch 62 is being added as the semantic durable-format boundary. The
  inspected working tree changes `_COORDINATION_HARD_CUT_EPOCH` from 61 to 62;
  this review does not certify its pending tests or authorize a database reset.
- The historical-card implementation was still evolving. Final acceptance
  must establish that anchored history remains read-only with event identity,
  node and timestamp information, and only the panel offers pending actions.

## Caveats & Required Follow-ups

F1 is resolved in the rechecked delta; no additional LLM-domain blocker was
identified in that bounded inspection. Complete the outstanding runtime gates
before treating this as release acceptance. Preserve the existing provider/tool loop and
interpretation APIs. Exercise View prompt → Approve and amendment using the
existing attestation path; synthetic UI success is not evidence that an
approved prompt executed. Exercise suggestions across reload, mutation, clean
review and guided/freeform mode changes.

The observed reuse of existing prompt rendering and resolver behavior is a
bounded source assessment, not certification of every inherited attestation
path. No global signing-gate remediation is proposed. The approved operator
choice remains anchored read-only interpretation history.
