# Composer chat-card lifecycle review

Reviewed release HEAD `7d981d6cf7c26c434085c084790db9fbc1da5d79` on 2026-09-22.
Read-only application review using the systems-thinking skill, five independent
card-family investigations, and parent reruns of every reproduction. Seven
confirmed chat-card defects and one adjacent recovery-dialog defect. All are P2.
No application changes, deployment, or production-incident causation claim.

## Findings

### 1. A partial prompt preview satisfies full-prompt approval

During session loading, cached interpretation cards can render before composition
state arrives. Their fallback is the bounded event draft. Backend
`interpretation_state.py:2835-2872` deliberately truncates that draft at 8,000
characters, while approval covers the complete prompt.

Opening this fallback sets the same `promptViewed` boolean as opening the complete
prompt (`AcknowledgementCard.tsx:410-413,563-567`). Approval becomes available.
If the user collapses the fallback before the complete composition arrives, the
boolean remains true without the newly available content ever being displayed.
The server approves an unchanged artifact, but the UI's required review step did
not make that complete artifact available.

Actual Stack/Card/store probes used a backend-generated 9,026-character prompt
with a tail absent from the fallback. Two desired-invariant assertions fail:
approval dispatch occurs after only fallback viewing, and eligibility remains
unlocked after full content loads while collapsed. A fully loaded prompt control
passes. Relevant loading path: `sessionStore.ts:1896-1930`,
`ChatPanel.tsx:2048-2070,2364`, `promptTemplateDisplay.ts:302-332`.

Repair direction: distinguish complete authoritative content from fallback
preview; bind review eligibility to the complete review artifact. Preserve the
intentional separate attestation of vague-term substitutions.

### 2. Guided column edits change claimed observations, not source headers

In **Edit columns**, rename CSV `id` to `record_id` and apply.
`InspectAndConfirmTurn.tsx:146-182` submits the new name.
`guided/stage_transitions.py:1057-1085` replaces `observed_columns` but retains the
original source options. `guided/planning.py:1127,1373,2739-2744` presents the new
name to the planner and eventually binds the original source options.

Actual transition, planner projection, and CSV source execution produced:

```text
reviewed id,name        -> planner id,name        -> runtime id,name -> agreement True
reviewed record_id,name -> planner record_id,name -> runtime id,name -> agreement False
```

This can direct subsequent field selection toward nonexistent fields. A specific
downstream provider failure was not tested or claimed.

Repair direction: preserve observations as facts. Express requested renames as
intent for the provider to author, or offer a clearly defined, validated factual
correction. Do not synthesize pipeline transformations server-side.

### 3. Genuinely stale proposals still offer Accept

The recent repair introduced a regression: `sessionStore.ts:583-609` treats
authoritative `status=pending` as proof of actionability. Backend
`sessions/routes/composer/proposals.py:365-375` rejects a mismatched
`base_state_id` with 409 while retaining pending status. The card therefore keeps
offering the same impossible Accept action after the authoritative refusal.

Actual-store reproduction uses current state2 and a pending proposal based on
state1. After the exact stale-base rejection and pending-list refresh,
`actionableProposals` still includes it. Matching-base lease-contention control
correctly remains retryable. The displayed error already advises rebase; the
contradictory action is the defect.

Repair direction: distinguish typed stale-base refusal or trusted base identity
from lifecycle status. Preserve transient-contention retry and the ability to
reject/discard an obsolete proposal. Do not revert to classifying every 409 stale.

### 4. A delayed proposal refresh removes a newer pending card

`sessionStore.ts:1077-1084` preserves existing terminal receipts when applying a
snapshot, but drops pending proposals absent from that snapshot.

Reproduced with actual `rejectProposal` and `sendMessage`: rejection succeeds;
its list GET is delayed; a subsequent compose response publishes pending p2;
the older rejection GET arrives and erases p2. The backend proposal remains
pending but loses its chat/decision-panel approval affordance until reloaded.
Navigation generation guards do not address ordering within one session.

Three proposal invariant probes fail in total (stale base, overlapping reads,
and concrete reject/compose overlap). Busy-retry and terminal-receipt controls
pass. Transport and unrelated background polling were isolated in the probes.

Repair direction: fence snapshots and retain arrivals occurring after dispatch
across every proposal-list writer, as well as retaining terminal receipts.

### 5. Failed source replacement hydration leaves an obsolete card editable

`ChatPanel.tsx:1667-1672` selects the source summary by session alone. When the
composition replaces blob A with B, its effect fetches B but preserves A's
summary if that fetch fails (`1842-1889`). Rendering and Edit do not check the
summary's identity against the active composition (`2157-2163,3498-3501`).

Actual ChatPanel/store/card test: replacement metadata returns 503; A remains
visible; clicking Edit sends A's obsolete contents despite the pipeline now
referencing B. A no-source transition correctly clears the card as a control.

Repair direction: associate projection and actions with source/blob identity;
detach outdated active cards and expose a retryable loading error. Historical
cards need explicit historical action semantics.

### 6. An uploaded first source hides later generated-source cards

`ChatPanel.tsx:405-410` selects only the first blob among sorted named sources.
If it is uploaded, the effect clears the summary and returns (`1854-1858`),
without inspecting later assistant-generated sources. The store holds only one
summary per session (`inlineSourceStore.ts:161-168`).

Real ChatPanel reproduction with `a_upload` and `b_generated`: generated blob
metadata is never fetched and its source-created card is absent. Reversing the
order renders the generated card, using authentic hash-matching CSV content.
This omits preview/provenance/Edit in chat; no backend approval bypass or absence
from the graph is claimed.

Repair direction: project a collection of assistant-created sources, each with
its own identity and bound actions.

### 7. Guided JSON fields submit malformed text and wrong container types

`SchemaFormTurn.tsx:727-732` stores parsed JSON and invalid raw draft text in the
same variable. Validation (`438-458`) cannot distinguish a legal scalar string
from a parse failure for `json-value`; it also admits parsed wrong-container
values for `json-object` / `json-array`.

Actual component submits incomplete required Schema text `{"mode":` and an array
for an object field, with no local validation error. The live catalog confirms
CSV Schema uses `json-value` and database Effect Ledger uses `json-object`.
Valid observed-schema submission is a passing control. Server validation is not
claimed bypassed: this is an avoidable refusal/repair loop in the card.

Repair direction: keep raw text, parse status, and typed values separate; enforce
the declared container while preserving legal scalar strings and null semantics.

### 8. Adjacent recovery dialog offers an impossible Apply and hides its refusal

This is a modal, not an inline chat card. A compose failure whose partial draft
could not be persisted carries `partial_state_save_failed=true`. Nevertheless,
`RecoveryPanel.tsx:198-205` offers enabled **Apply partial draft**.

Actual store correctly refuses (`sessionStore.ts:3082-3094`), leaves the current
composition unchanged, and sets an explanation in global chat error state.
The modal ignores that reason and remains open with its enabled button. Its
focus trap excludes the underlying chat error banner. Actual component/store
probe fails the in-dialog explanation assertion; a saved-draft control applies.

Repair direction: show the unsaved distinction and refusal inside the modal,
remove/disable impossible Apply, and retain discard/retry guidance.

## System-level interpretation

The recurring boundary is between authoritative state, delayed observations,
local editor/view state, and actions authorized by the card. Several surfaces
collapse different facts into one value: pending versus applicable; displayed
preview versus fully reviewed artifact; observed headers versus requested edits;
raw JSON versus parsed value; session cache versus source identity.

Highest-value interventions are explicit identity and information flow at those
boundaries, followed by visible actionable feedback. More retries or broader
error-string matching would not repair these distinctions. No production
frequency, behavioral feedback-loop strength, or leverage multiplier was measured.

## Verification and limits

Parent reran each actual-import probe against the reviewed HEAD. Raw summaries:

| Probe | Process exit | Test summary |
| --- | --- | --- |
| Interpretation cards | 1 | 2 failed, 1 passed |
| Proposal lifecycle | 1 | 3 failed, 2 passed |
| Source cards | 0 | 4 passed |
| Guided forms | 1 | 2 failed, 2 passed |
| Recovery dialog | 1 | 1 failed, 1 passed |
| Guided source/planner/runtime | 0 | agreement control true; edited-name agreement false |

Failed tests assert desired invariants and fail at the described behavior, not
setup. Source-card tests instead assert the existing defects and controls, so
their green result is reproduction evidence, not fix verification. Parent logs
are `/tmp/chat-card-parent-{interpretations,proposals,sources,guided,completion}.log`
and `/tmp/chat-card-parent-source-runtime.log`. Scratch probes and detailed lane
notes remain in `/tmp/cards-*`, `/tmp/guided-card-probe`, and
`.claude/lanes/chat-card-review/`; those are ephemeral support artifacts.

No new validated defect in the inspected completion/readiness labels. Rejected
leads include metadata-only recovery diffs and intentionally separate prompt
slot attestations. Historical attribution and modal-state candidates without a
material reachable reproduction were not counted. This is a bounded source and
deterministic interleaving review, not exhaustive browser/live-production coverage
or a broad frontend/Python suite. It does not establish the cause of the original
production approval incident.
