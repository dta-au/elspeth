# R04. A decision-only state save is invisible to the frontend readiness refresh

| | |
|---|---|
| Severity | medium |
| Status | confirmed |
| Area | — |
| Review line | seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-02-advisor#1 |

## Finding

- **Status and severity:** confirmed, **medium** (raised as high).
- **Location:**
  - `src/elspeth/web/sessions/routes/messages.py:842-872` and `compose.py:588-618`, the backend save.
  - `src/elspeth/web/frontend/src/stores/subscriptions.ts:515-531`, which skips `/validate` for a content-equal bump.
  - `subscriptions.ts:293-316` together with `auditReadinessStore.ts:341-372`, which carry the snapshot forward and re-key it.
  - `useAuditReadinessSync.ts:79-85`.
  - `lib/compositionContent.ts:7-15`.
- **What is wrong:** 41aeaeac0 persists version+1 rows whose content is byte-identical when only the advisor decision changed. The frontend assumes a content-equal version bump "wrote nothing" that could change readiness. It therefore skips `/validate` and carries the previous version's audit-readiness snapshot forward, including the old completion-gate verdict.
- **Failure scenario:**
  - (a) v5 carries an UNAVAILABLE block. The user says "retry", the gate now returns CLEAN, and the route saves v6. The UI keeps showing the v5 blocker, and Save for review stays withheld until a reload or Ctrl+Shift+V. This defeats the user-visible purpose of 41aeaeac0.
  - (b) v5 is clean, and a question-only turn is flagged GRAPH_REJECTED. The UI keeps the clean verdict, and the blocker and reviewer note never render.
- **Evidence:**
  - Plan `docs/plans/2026-09-22-advisor-transient-failure-recovery.md` step 7 requires "the client sees the new readiness immediately".
  - The `DecisionPanel.tsx:36-42` header comment still says a turn that mutates nothing saves no row.
- **Suggested fix:** Have the compose and recompose response signal a review-status change, or carry a completion-gate identity on the state response. On that signal, force `requestValidate` and skip `carrySnapshotForward`. Do not widen `compositionContentEqual`'s projection, because it must stay in parity with `composition_content_hash`. Add a frontend test for a content-equal bump that changes the blocker.
- **Sources:** seam-02-advisor#1.
- **Verifier notes:** The server stays authoritative: execution re-merges the durable gates at run (`execution/service.py:1547`). The "validationResult is null" branch in the original finding does not happen. With no v5 snapshot, the hook fetches v6 correctly.


## Source findings and verification

### seam-02-advisor#1: Decision-only state save is invisible to the frontend readiness refresh (content-equal carry-forward)

- **Reported at:** `src/elspeth/web/sessions/routes/messages.py:842`; reviewer severity high; category half-wired-seam; diff-anchored True.
- **Summary:** 41aeaeac0 persists a new composition state row (version+1, byte-identical sources/nodes/edges/outputs/metadata) whenever completion_gate_decision_changes is true on an unchanged graph (messages.py:842-872, compose.py:588-618). The frontend assumes a content-equal version bump has identical readiness. subscriptions.ts:515-531 skips /validate, subscriptions.ts:293-316 carries the audit-readiness snapshot forward (auditReadinessStore.ts:341-372 re-keys it), and useAuditReadinessSync then projects the stale validation_result back. sessionStore.ts:2221/2802 has already called clearValidation on versionChanged, so the result is either the stale verdict or null.
- **Failure scenario:** (a) Turn 1 mutates the graph and the advisor is unavailable, so an UNAVAILABLE block is persisted on v5 and the frontend shows the blocker. Turn 2: the user says 'retry', the END gate re-reviews and gets CLEAN, and the route saves v6 with identical content and no gate. The frontend carries the v5 blocked verdict forward, so the DecisionPanel still shows 'advisory review could not be obtained' and Save for review stays withheld. If no snapshot existed, validationResult is null and Run shows 'This pipeline hasn't been validated yet'. Either way this lasts until a manual Ctrl+Shift+V or a reload. (b) v5 is clean; a question-only turn is FLAGGED on its final pass (GRAPH_REJECTED), so v6 is saved with the blocked fact. The frontend keeps the clean v5 verdict, and the advisor blocker and the new reviewer's note never render.
- **Evidence:** messages.py:842-846 `elif result.state.version != state.version or completion_gate_decision_changes(...)`; sessions/service.py save_composition_state 'Version is max(existing versions for session) + 1'; lib/compositionContent.ts compares only sources/nodes/edges/outputs/metadata; subscriptions.ts:515 `const carriedVerdict = ... isContentOnlyVersionBump(...)` then `lastValidatedVersionBySession.set(sessionId, version); return;`; auditReadinessStore.ts:367 `[sessionId]: { ...cached, composition_version: toVersion }`; useAuditReadinessSync.ts:79-85 loadSnapshot early-return then projectMatchingSnapshotToExecution; audit_readiness/service.py:564 merges completion_gates into that snapshot; ExecuteButton.tsx:598 validationNotRun disables Run.
- **Suggested fix:** Have the compose/recompose response signal a review-status change, or carry a completion-gate identity in the state response. sendMessage/recompose should then force requestValidate and skip carrySnapshotForward for that bump. Do not widen compositionContentEqual's content projection, because that breaks its stated parity with composition_content_hash. Add a frontend test where a content-equal version bump changes the advisor blocker.
- **Verifier (impact):** upheld, confidence high, severity medium. I could not refute this finding. I traced the failure path myself at the pinned commit 74c0ce0db. The window's own plan requires what the frontend now defeats. docs/plans/2026-09-22-advisor-transient-failure-recovery.md step 7 says "Return a state response for advisor-only changes so the client sees the new readiness immediately", and the Done criterion requires "GET state/readiness and revalidation agree with the just-returned result". No recorded ruling covers the frontend. The content-equal carry-forward (elspeth-986801d218, lib/compositionContent.ts:7-15) rests on a premise: a content-equal version bump "wrote nothing" that changes readiness. That premise held for guided settlement rows. 41aeaeac0 broke it by adding decision-only saves. The DecisionPanel.tsx:36-42 header still says "A compose turn that mutates nothing saves no composition-state row, so the durable completion gate cannot be rewritten by it". That comment is now stale and shows the frontend was never updated for the new save.

I lowered the severity from high to medium. The server stays authoritative: lib/compositionContent.ts:28-30 notes that execute re-runs the full preflight. So no audit, integrity or authz consequence follows. Nothing is lost, and the wrong display lasts only until a reload, a manual revalidate or the next real edit. The practical effect is still real. The user-visible purpose of 41aeaeac0 is that retrying an unchanged turn clears a transient advisor block. On the client that purpose is defeated: the stale blocker keeps Save for review withheld. In scenario (b) a new GRAPH_REJECTED block and the reviewer's note do not render.

One minor correction to the finding: the "validationResult is null" branch does not happen. When no v5 snapshot exists, carrySnapshotForward is a no-op, so useAuditReadinessSync fetches a fresh v6 snapshot and projects the correct result. The ambient hook (ArtifactWorkspaceSurface) normally caches a v5 snapshot, and in that case the stale verdict is what gets projected.
- **Verifier (trace):** upheld, confidence high, severity medium. I traced the whole path at 74c0ce0db and could not refute it. 41aeaeac0 now saves a new composition_states row when only the advisor decision changes and the graph does not. The frontend decides whether to re-validate by comparing authored content only (sources, nodes, edges, outputs, metadata). So it treats that bump as bookkeeping: it skips /validate and carries the previous version's audit-readiness snapshot forward. That snapshot holds the old completion-gate verdict. Scenario (a) is the exact recovery path the commit adds. advisor_block_covers_unchanged_graph now skips the END gate only when the cause is GRAPH_REJECTED, so an UNAVAILABLE or MALFORMED block gets a fresh review on the unchanged graph. A CLEAN result returns AdvisorGatePassed, completion_gate_decision_changes returns true, and the route saves v6 with the same content. The UI then shows the stale v5 blocker. I lowered the severity to medium for three reasons. It is a stale-UI defect, not a hole in enforcement: the execution service re-merges the durable gates at Run (execution/service.py:1547). A reload or Ctrl+Shift+V recovers. Nothing already in the window fixes it (41aeaeac0..74c0ce0db after it is docs only).
