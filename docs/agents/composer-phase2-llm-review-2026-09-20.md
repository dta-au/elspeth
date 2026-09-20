# Composer Phase 2 LLM specialist review

Review date: 2026-09-20. Initial snapshot: evolving worktree at HEAD
`688a8bf18bc1ac9150cf7b73d35a93ae7e388fef`, compared with
`ebc4720e0d4f18810353c646a093269a4ba7bc81`. Uncommitted implementation was
changing during inspection; this is not a frozen final-candidate approval.
Read-only product review; only this report was written.

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
| **F1 — blocker: active guided Apply dispatches through freeform composition.** The newly mounted guided panel enables Apply while a guided session is active, but the callback always calls `sendMessage`. The existing guided path is `sendGuidedChat` / `chatGuided`. | High for dispatch mismatch; runtime outcome requires regression | `ChatPanel.tsx:2072–2085`, `ChatPanel.tsx:835–848`, new active-guided decision dock; `useComposer.ts:49–57`; `sessionStore.ts:2073–2186`. `sendMessage` calls `api.sendMessage`, updates ordinary messages and composition state, and does not reconcile the guided checkpoint in that success block. |
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
| F1 dispatches a pipeline-changing instruction through the wrong mode and can leave guided history/state inconsistent. | High | Present in inspected source whenever an active guided Apply row is enabled | Add a failing active-guided click regression, route through the existing guided callback/admission rules, and verify guided history and updated state. Preserve completed-guided disabled behavior. |
| A stale suggestion could be attributed to a graph the advisor never reviewed. | High | Guard present in inspected implementation | Execute graph mutation/clean clearing persistence regressions and serial PostgreSQL acceptance. |
| Required suggestion field rejects older blocked completion envelopes. | Medium | Deliberate, documented format change | Keep deployment limitation explicit; no unapproved shared database reset or compatibility fallback. |
| A text-only “retry/review” suggestion can imply guaranteed clearance even though a non-mutating turn may preserve the durable gate. | Medium | Existing wording, now more visible | Keep it advice rather than adding a fabricated one-click review action. Browser evidence must distinguish actual provider mutation/clearance from intercepted success. |

## Information Gaps

- Final candidate SHA/diff and F1 repair/regression evidence have not yet been
  provided. A final delta recheck is required.
- This review did not run a live provider or confirm final browser prompt
  approval, amended prompt execution, or guided Apply acceptance.
- The parent lane owns full frontend/backend gates, PostgreSQL validation,
  writer-binding reconciliation and package signing evidence.
- The historical-card implementation was still evolving. Final acceptance
  must establish that anchored history remains read-only with event identity,
  node and timestamp information, and only the panel offers pending actions.

## Caveats & Required Follow-ups

Before treating this as release review, resolve F1 with a meaningful regression
and recheck the final delta. Preserve the existing provider/tool loop and
interpretation APIs. Exercise View prompt → Approve and amendment using the
existing attestation path; synthetic UI success is not evidence that an
approved prompt executed. Exercise suggestions across reload, mutation, clean
review and guided/freeform mode changes.

The observed reuse of existing prompt rendering and resolver behavior is a
bounded source assessment, not certification of every inherited attestation
path. No global signing-gate remediation is proposed. The approved operator
choice remains anchored read-only interpretation history.
