# Composer state and feedback review — 2026-09-22

Targeted systems-thinking investigation following the approval conflict fix
`e26a1dd5927b2838066482d790777763911ca85e`. Release HEAD advanced during
review to `29f9af13b44e3c7a3abefb5043cae74bd4261727`; the reviewed approval,
session, interpretation, and authentication files were unchanged between
those commits. No application changes were made by this investigation.

Eight P2 findings are listed below. They are demonstrated with controlled
response schedules or exact-source executable probes, with working controls.
They are not captures of the original production incident. In particular,
finding 1 can produce a repeated approval card without any duplicate model
request, but the original production cause remains unconfirmed.

## Findings

### 1. Delayed snapshots resurrect retired approval cards or hide new ones

`src/elspeth/web/frontend/src/stores/interpretationEventsStore.ts:291-360`
preserves accepted/amended/opted-out history but discards superseded and
abandoned events. It publishes pending snapshots in response-arrival order.

Reproduction: delay refresh A containing pending event E; complete refresh B
containing superseded E; then deliver A. E becomes pending in the browser
again. The acknowledgement selector displays it; clicking Approve reaches
`src/elspeth/web/sessions/service.py`'s non-pending-event rejection and gets
`interpretation_already_resolved`. Alternatively, deliver an old empty
snapshot after one containing a newly created event: the new card disappears.

Refreshes are independently launched by session selection, composition and
proposal completion, and validation completion. A selection or validation
read can overlap a later composition and its refresh. Backend
`src/elspeth/web/sessions/dead_site_supersession.py:134-142` supplies the real
superseded transition when a reviewed site is removed. Abandoned is an
additional store-level case, not a separately established continuing-session
production path.

Actual-store tests: three expected-invariant failures, one passing control
(accepted events remain resolved). Repair needs ordered per-session
publication and complete terminal-state reconciliation.

### 2. Late approval replaces the newly selected session's displayed pipeline

`src/elspeth/web/frontend/src/hooks/useInterpretationResolver.ts:299-310`
invokes its callback after the request resolves, even after unmount.
`components/chat/ChatPanel.tsx:2368-2373` publishes the returned composition
without checking its originating session. Freeform
`stores/sessionStore.ts:4536-4562` uses the currently selected session for
subsequent validation.

Reproduction with the actual hook and stores: approve in A, unmount the card,
select B, then deliver A's successful response. Observed:

```json
{"active":"session-b","composition":"state-a","validationTargets":[["session-b"]]}
```

The no-switch control passes. Measured impact is wrong-session display and
validation dispatch; backend persistence corruption was not established.
Publish only when request origin and activation generation still match.

### 3. Temporary contention hides an authoritative pending proposal as stale

`stores/sessionStore.ts:2372-2379,2452-2459` classifies every accept/reject
409 as stale. The proposal endpoints acquire a session lease before mutation
(`src/elspeth/web/sessions/routes/composer/proposals.py:287,694`), and busy
leases produce an uncoded 409 with `Session operation is already active`.

Even when the subsequent GET returns the same pending proposal, the store
adds its ID to `staleProposalIds`. `components/chat/actionableProposals.ts:9`
excludes it; historical cards ask the user to rebase or revise it. Both accept
and reject probes reproduce this with no error surfaced. Preserve pending
actionability on contention and classify only explicit lifecycle conflicts.

### 4. Failed refresh discards a successful proposal decision receipt

`stores/sessionStore.ts:2346-2365,2437-2449` receives successful accept/reject
POST results, then awaits GET hydration before publishing them. A failed GET
enters the mutation failure catch and invites retry while the old pending
proposal remains actionable.

Actual-store probes reproduce this for both actions; the successful reject
and successful refresh control passes. Retain the terminal receipt before
hydration and distinguish refresh failure from decision failure. Accept also
needs an explicit incomplete composition refresh state; its proposal receipt
alone cannot reconstruct the composition.

### 5. Execute claims a run exists when the session is merely busy

`stores/executionStore.ts:629-639` maps remaining 409 responses to
`A run is already in progress for this pipeline.` However,
`src/elspeth/web/execution/routes.py:1031-1037` acquires the operation lease
before execution. Another tab composing can cause that 409 without a run.

An executable probe of the current method and parser reproduces the false
claim; a recognized `interpretation_review_drift` control preserves its real
detail. Restrict active-run copy to the actual active-run discriminator.

### 6. File deletion attributes unrelated blockers to an active run

`stores/blobStore.ts:199-211` maps every 409 to
`Cannot delete — file is linked to an active run.` Real producers include
operation contention (`src/elspeth/web/blobs/routes.py:519-524`) and pending
proposal retention (`src/elspeth/web/coordination/repository.py:3110-3118`).
Neither requires an active run.

Exact-source probes reproduce both false claims, with a non-409 control.
Preserve the actual retention or contention reason so users can resolve the
right dependency.

### 7. YAML view labels contention as pipeline validation failure

`components/inspector/YamlView.tsx:34-45` assigns every 409 the title
`YAML export is blocked by validation errors.` YAML export takes a COMPOSE
lease (`src/elspeth/web/sessions/routes/composer/state.py:1295-1304`), so a
concurrent operation yields that title above the contradictory busy detail.
Preflight infrastructure failures at `state.py:1195-1210` also use 409 without
establishing pipeline invalidity.

Exact-source probes reproduce the busy case and verify actual-validation and
non-409 controls. Classify the failure reason explicitly and provide a retry
for transient export failure.

### 8. Delayed 401 logs out a successfully replaced credential

`src/elspeth/web/frontend/src/api/client.ts:245-262` checks only whether a
token currently exists before logging out on 401. It does not check whether
that token authorized the failed request.

Actual-client/store reproduction: start an auth request using synthetic
credential A; perform successful `loginWithToken(B)` including its profile
request; deliver A's delayed 401. Both B's stored and in-memory token become
null. Expected preservation fails; the control where A is still current
correctly logs out. Bind unauthorized-response handling to the credential
generation that originated the request.

## Shared structure and repair priorities

The browser conflates four distinct facts: operation admission, durable
decision state, projection freshness, and the current navigation or credential
identity. HTTP status alone cannot establish decision state; a failed read
cannot undo an acknowledged write; response arrival order is not state order.

Prioritize findings 1–4 because they directly affect approval fidelity. Use
semantic error discriminators, retain decision receipts, and guard publication
by request/session generation. Apply the same conflict taxonomy to Execute,
Files, and YAML. Apply credential-generation custody to unauthorized responses.
Blanket retries or prompt changes do not repair these boundaries.

A plausible reinforcing loop is busy response → false stale/invalid feedback
→ unnecessary user revision → more operation contention. Another is committed
decision → failed refresh → false failure → repeated decision request. These
are causal hypotheses about user behavior, not measured production rates.

Controls matter: accepted interpretation history already resists replay;
some execution conflicts already use explicit discriminators; shareable review
preserves server detail; guided retry custody differs from ordinary proposal
hydration. Do not replace these working distinctions with a universal retry.

## Verification scope

Scratch probes run against production imports or unchanged AST-extracted
bodies. They intentionally test defective behavior, not fixes. Captured exits:

| Probe | Result | Meaning |
| --- | --- | --- |
| `/tmp/elspeth-stale-review/stale.test.ts` | exit 1; 3 failed, 1 passed | Snapshot invariants fail; accepted-history control passes |
| `/tmp/elspeth-stale-review/late.test.ts` | exit 1; 1 failed, 1 passed | Session-switch invariant fails; no-switch control passes |
| `/tmp/approval-receipts-probe/receipts.test.ts` | exit 0; 5 passed | Four assertions reproduce defects, one successful-operation control |
| `/tmp/elspeth-similar-error-proof/prove.cjs` | exit 0 | Three classifiers reproduce false claims; parser and discriminator controls pass |
| `/tmp/similar-approval-auth/auth.test.ts` | exit 1; 1 failed, 1 passed | Replacement-token invariant fails; current-token logout control passes |

Temporary probes are local investigation artifacts, not permanent regression
coverage. No full suite, deployed-production replay, or exhaustive bug census
is claimed. Source traces and the schedules above specify regression cases
for implementation. No application code was changed or committed by this
review.
