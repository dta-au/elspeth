# Composer+ Continuation implementation plan

Status: **executable specification; planning only; base UNQUALIFIED**. Prepared 2026-10-19 by the sole
follow-on owner, task `01a153ef-eb1f-7441-8a4d-6c4a920af71c`, on
`codex/composer-continuation-plan-20261019`. No numbered release is assigned.
This document replaces the Stage 1 plan **in this isolated branch only**.
The owner's original planning documents remain untouched.

The outcome is one authorized user request that keeps working automatically
across execution segments and recoverable worker loss, with exact private
context, fenced effects, cumulative accounting and truthful observation.
The provider continues to choose tools and author graph structure. The user
does not submit another message, press Continue or approve internal scheduling.

## Authority, order and resumption of this work

The release owner `01a151cf-06d4-78b1-a2e2-28913836ca69` retains the current
release checkout, service, PR, CI and main custody. This task owns only this
branch and this plan now, and is the prospective Composer+ implementation
owner. It neither replaces the release owner nor creates another successor.

Latest direct instruction: finish iterating this implementation plan, then
**stand by**. That is the current action boundary even if earlier conditional
early-start requirements become true while planning. Do not start code, tests,
runtime, commit hooks, services or a watcher from this planning turn. A later
execution instruction must first satisfy P0 below; no new generic approval
loop is needed for planning or source-text review.

The required order is:

1. Audited rigid user-text heuristic removal/replacement, retaining actual
   credential, provenance, review, execution, quota and custody boundaries.
2. Composer Continuation.
3. Selected existing .7 changes, with semantic conflict review of each hunk.
4. Combined source checks and complete ordinary deployed B/C **assemblies**,
   eligible Gate2, protected review/CI/main.

ST01, ST02, request-selected terminal removal and the bounded whole-reader are
already represented according to the completed audit. Do not implement them
again. Optional engine .9 is excluded. Engine work resumes only when Composer+
reaches PR. One-shot is suspended. Latest qualification is B1–B3 and C1–C6;
older two-shot B instructions in the design inputs are superseded. Counts do
not identify the private cases: the release custodian supplies the exact
approved artifact identities and expectations later, outside tracked docs.

**Early isolated implementation is conditionally authorized.** The latest
instruction supersedes the unconditional first-release-PR start hold. Start
only if observed current-release stability, clear patch ownership/dependencies
and noncompeting resource needs support it. A committed SHA alone proves none
of these. Current release priority, primary checkout/service and release
PR/CI/main custody stay with the existing owner. On supported handoff, before
any implementation:

- Record a source-grounded readiness assessment of release state, owned paths,
  dependency changes and resource impact. Early work must remain in this
  isolated branch; do not consume competing release resources or old grants.
- When the first release PR exists, independently read its URL, repository,
  branch and full head SHA from GitHub. Attach it only if this task actually
  continues work on that PR, not merely because it is a dependency.
- Compare the immutable current candidate/PR head with this base and review every durable repair
  affecting the seams below. Update the plan and its independent review for
  material differences; a historical source hash is not current permission.
- Record scope acceptance with the current custodian: this task owns the
  isolated Composer+ work; the release owner retains their PR/CI/main lane.
  Do not switch or edit their checkout or wake/retire prior owners.
- Reconfirm the release target before versioned docs/changelog edits. Take
  fresh resource admission for any checks; no capacity has been reserved.
- Before canonical pre-PR qualification in the primary checkout, obtain the
  later exclusive checkout/service-custody handoff required by the supplied
  .7 merge-set. Preserve dirty installation/configuration and local documents.

Until the conditional readiness gate is satisfied, work remains this branch's
documents and one independent read-only review: no production/test/config
edits, project imports, tests, heavy scans, installs, services,
browser/provider/graph execution or capacity reservation. No current-release
checkout/PR/CI/main changes, push, takeover or watcher/automation. Return
plan-ready and wait for supported readiness/handoff evidence; do not poll.

### Actual early-start assessment, 2026-10-19T11:42Z

**HOLD: implementation has not started.** Supported read-only release-owner
status shows active release qualification/diagnosis and a newly assigned
Advisor complete-request/transcript change overlapping H3/C1/C6 paths. Its
current head is still d647/tree216b on this check, but that is source identity,
not release stability or completion of that assigned repair. Release-owned
patches must settle before this task can select a stable, nonduplicated delta.
The owner remains active. This task has neither release checkout/service
custody nor a fresh runtime/test allocation. Local `ps` exposes only the
sandbox PID namespace; it cannot establish host-wide spare capacity. Host
uptime/load is not an admission grant. Planning/review uses text reads and
document writes only, with no product runtime or test capacity reservation.
These facts support continuing planning and review, and do not support early
implementation now. Reassess on a supported owner/handoff update, not a timer.

## Source and attributed inputs

Verified at `2026-10-19T11:33:25Z` on `nyx.foundryside.dev`:

| Item | Observed value |
| --- | --- |
| Own HEAD and committed `codex/composer-fixes` | `d647dd929d2b05be4918f175bc5ed96b18fdd198` |
| Own tree and candidate tree | `216b426c28e7db13f2601ed56aa3ec5990118ee3` |
| Initial worktree status | Empty, detached before the named planning branch was created |
| Release checkout status | Modified `.gitignore`; the four design inputs below untracked |
| Baseline qualification | UNQUALIFIED; no execution or test result inferred |

### Committed-candidate refresh during the executability pass

The isolated branch remains rooted at d647/tree216b; no product rebase or copy
was made. A final read observed `codex/composer-fixes` advance to
`f4c2f88497f7105afc114715e5072aef66d3d37c`, tree
`f628b6d63d87d7e9f419243cc8ee950265aaaad8`. Its actual committed diff was read
against d647. This is source reconciliation, not a qualification/pass claim.
P0 must include that committed repair (or its exact landed successor) before
implementation, then refresh again for later changes. Do not implement directly
on old d647 and replay release files afterward.

| Relevant committed repair | Incorporated implementation consequence |
| --- | --- |
| `advisor_checkpoint.py`, `advisor_context.py`, `turn_effects.py` | Current request is already complete once: owned `AdvisorPublicationReview.current_request_text()`, separate `provider_review_context_json()` with current_request_ref, and `untrusted_content_parts()` define exact rendered/inspection ownership. Preserve these methods and snapshot binding in H3a/C1; do not remove an excerpt again or duplicate current request. |
| `advisor_context.py:advisor_user_content_parts` | The committed producer partitions the complete current-request section into ordered 8000-character untrusted parts. H3b preserves that existing request-specific contract and its full-request-once tests; it does not infer classifier equivalence or use it as blanket admission for every long scalar. |
| `prompts.py:STATE_CONTEXT_PREFIX` | Initial state is an as-of context-construction snapshot; newer successful tool/mutation results take precedence. C1 exact replay/timing retains that prefix and actual newer results; never rewrite history to update state text. |
| `interpretation_state.py` | Required prompt-shield recommendation for untrusted/operator data is separate from optional installation, and pending review is not execution approval. H1/H2/H3 and continuation preserve the decision-card authority boundary. |
| `frontend/.../toolCallDescriptions.ts` | read_attachment already has truthful chat copy. H3c reuses it, without a duplicate UI feature. |
| Census/catalog, plugin and affected test repairs in this commit | Carry the committed base intact through P0. Reconcile current contract/wire expectations, never replay old golden/count values. Source presence is not a passing test claim, and no optional engine import is added. |

New regression consumers read in the committed delta include
`test_advisor_checkpoint.py::test_actual_advisor_receives_whole_current_request_once`
and updated `test_turn_effects.py`. They are required future H3a/C1 packet inputs,
not tests executed during planning.

Exact persistent worktree, Git directory, common directory and object-store
paths are recorded privately in
`.claude/lanes/composer-continuation-plan-20261019/startup.md`. Managed linked
metadata remains intact. No owner dirty source or configuration was copied.
The following are untracked **owner-authored planning inputs**, separately
identified from the committed base. These hashes identify what was read; they
are not seals, approval gates or a plan manifest.

| Owner input under `docs/design/` | SHA256 |
| --- | --- |
| `composer-continuation-plan.md` (Stage 1) | `a4886a6d0a43ce3efeb949379dce3b67c49b4cc16bb8b8e144a9e9398fe5c095` |
| `composer-continuation-architecture.md` (revised Stage 2) | `7544546c83ab6e93af34e6eb7d5f7ba2e1ee61b07ddca6fd3a085ea1e8ddb29e` |
| `composer-continuation-systems-review.md` (B1–B4 closed at design level) | `1b5f2880dabfc37ae3f92de4f5a65e959229630abc6141669c985b1454ce3dd0` |
| `composer-remediation-plan.md` (historical release sequencing) | `fc29f7306e5047792b35edcc45296c47e7a19dc5e3a6ebb9888569cfd42b78cc` |

Also read, without altering or importing:

- Completed rigid-text audit lane
  `composer-user-text-audit-20261019-01a153a2`: `audit-report.md`, `coverage.md`
  and the source/scope manifest `final-source-manifest.json`. Its final base
  is this same d647/tree216b. Object membership is not semantic coverage.
- `.7` lane `composer07-consolidation-20261019/merge-set.md`, a scoped
  planning reference only. Its reported staged work is not a committed input.
- Current `AGENTS.md`, `CONTRIBUTING.md` whole-tree conventions and maintainer
  toolchain; implementation-planning and plan-review skill guidance.

This plan uses public source and metadata only. No private user request,
CSV, provider transcript, recovery archive, credential or graph payload is
included in this document or sent to the reviewer.

## Current implementation: verified seams

Paths and line anchors here are at d647. These are observations, not proposed
APIs or claims about the active service.

| Source anchor | Observed contract and required change |
| --- | --- |
| `src/elspeth/web/coordination/composer_operation_authority.py:300` | Admission verifies request identity and reserves the parent plus quota once under the session transaction. Keep this authority and reservation. |
| Same file `:480`, `:863`, `:929` | Claim/start/mutation predicates use the parent's queued/running status and deadline; the mutation lookup is bound to a session operation epoch. V2 must cover these SQL predicates, not just a Python token. |
| `src/elspeth/web/coordination/repository.py:4816`, `:5273` | Start advances the exclusive session fence and mutate calls the durable Composer predicate. Segment starts and every real write must use these owners. |
| `src/elspeth/web/sessions/composer_async_worker.py:326`, `:563`, `:615`, `:731`, `:884`, `:1422` | Existing scan, owned lease, terminal verification, outer timeout, loss watcher and reaper assume terminal turns. Add a distinct verified nonterminal yield and recovery path. |
| `src/elspeth/web/sessions/composer_turn.py:247`, `:931`, `:2012` | Database-to-monotonic budget anchoring exists. Fresh ingress inserts/binds one user row and rejects non-fresh ingress. Resume below this preamble. |
| `src/elspeth/web/composer/service.py:1221`, `:2362`, `:2502`, `:2581` | English intent selects the empty-graph planner; ordinary compose has mutable loop-local messages, counters, caches, state and review debt. Refactor actual state, not a graph-only restart. |
| `src/elspeth/web/composer/turn_effects.py:307`, `:340`, `:779`, `:824` | Evidence identity and review scope currently include the transient session fence, and committed witnesses require its creator ID. V2 needs separate stable logical scope and current execution authority, with verified historical effects. |
| `src/elspeth/web/composer/tool_batch.py:837`, `:912` | Tool context includes mutable discovery/preflight caches and live custody. BatchAccumulator covers only four live inputs; the remaining intra-batch locals need an explicit cursor/carrier. |
| `src/elspeth/web/composer/pipeline_planner.py:713`, `:3448`, `:3506`, `:3563`, `:3724` | Planner has request-bound reservations/cost/publication state, discovery/repair history, nested closure state and remainder-based deadlines. Serialize their data and reconstruct live owners. |
| `src/elspeth/web/composer/provider_quota.py:37` | Provider-family ordinals start at zero in an in-memory mint. Use persistent request-family allocation for v2. |
| `src/elspeth/web/composer/provider_gateway.py:481` | Exact input provenance is detached, credential-scanned, guarded, admitted and dispatched. Timing must enter before freeze/guard; later checks cannot edit that input. |
| `src/elspeth/web/composer/provider_context.py:229` | Optional diagnostic prepared-context capture can return unavailable. It is not a mandatory exact-resume store. |
| `src/elspeth/web/composer/service.py:1088`, `:2600`; `pipeline_planner.py:4320` | Recovery tools return a ComposerResult or PlannerHandoff before ordinary completion review. Close provider-originated early-return adjudication before enabling continuation. |
| `src/elspeth/web/composer/advisor_output.py:62` | Current abandonment schema has decision/reason/free-text next_action/question, no typed requirement scope or executable action binding. Add a closed v2 decision for v2 frames. |
| `src/elspeth/web/sessions/models.py:68`, `:3564`; `schema.py:156` | Session epoch 77, strict status bundles, one nonterminal parent/session, preserve/refuse initializer; no existing session migration path. |
| `src/elspeth/web/sessions/routes/composer/state.py:784` | Revert uses receipt reservation and expected-current-head checks. Add cancellation/fence revocation at its authority admission, before waiting for the active provider. |
| `src/elspeth/web/frontend/src/api/composerOperationDecoder.ts:178`; `composerOperationObserver.ts:87`, `:195` | Closed v1 decoder; observer only tightens one deadline, SSE rotates independently and GET retains custody. V2 needs a separate closed decoder and segment timers. |

## Concrete lifecycle and ownership contracts

Keep `composer_async_operations` as the logical public operation. Each v2
operation has one current segment and immutable checkpoints; queued segments
are the durable work queue. Use existing SQL/session lock and worker scan,
without a broker or new service.

The state transition is `initialize -> action_ready -> awaiting_response ->
response_retained -> tool_ready -> effect_reconciling/result_retained ->
action_ready`, with nested planner/advisor/END frames. Only an inert boundary
can yield. The public parent stays queued/running while continuation is queued,
waiting or reconciling. Normal completion, a reviewed whole-request blocker,
explicit Stop, or an actual unrecoverable contract/policy failure settles it.

### Persistence shape (proposed)

Implement closed nominal dataclasses/Pydantic wire models; use tagged JSON
codecs, no pickle, SDK objects, callables, Tasks, leases or required-work
tickets in storage. All child keys include `(session_id, operation_id)` with
composite foreign keys to prevent cross-session substitution.

| Record | Fields and invariants |
| --- | --- |
| Parent additions in `models.py` | `lifecycle_version` (1/2), `phase`, `status_revision`, `current_segment_sequence`, `ownership_generation`, `checkpoint_id`, `ingress_phase`, `reserved_user_message_id`; original request/user-message/hash/base/cancel fields retained. V1 bundles/hash bytes unchanged. V2 reserves the message UUID before ingress but keeps actual `user_message_id` null until that row exists. For v2 the parent `deadline_at` is null; segment owns hard time. Rewrite CHECK constraints by lifecycle rather than invent a far-future sentinel. Keep one nonterminal parent per session. |
| `composer_operation_segments` | PK parent+sequence; unique predecessor per parent; queued/running/yielded/interrupted/settled status; generation, claim token/owner/expiry, session fence binding, enqueue/eligibility/claim/start/cooperative/deadline times, retained action descriptor and policy hash, checkpoint reference and rollover reason. Queued rows have no running deadline. Index eligibility and stable tie-break keys on both dialects. |
| `composer_segment_attempts` | PK parent+segment sequence+ownership generation; immutable creator fence ID/epoch, owner, start/expiry and outcome/revocation. Retain every attempt, including takeovers of one segment. Segment current-owner fields never replace this history; old-fence mutation lookup consults it after the parent moves. |
| `composer_continuation_chunks` | Immutable exact private content once per request/content key, codec/type/digest/byte length; store complete native fields and ordered manifests. Digest does not replace retained bytes. Collision or binding mismatch is integrity failure. |
| `composer_continuation_events` | Monotone request event sequence, typed tag/version, exact chunk references and frame/effect/accounting/review bindings. Allocate sequence under session lock. Record input prepared, physical dispatch, complete response, tool result, review and checkpoint transitions separately. |
| `composer_continuation_checkpoints` | Immutable ID, kind (`transition` or `handoff`), event cursor+binding, frame-stack version, graph head/version/content or uncommitted proposal, original-request/user-message binding, effects frontier, review/guard/capability/tool/provider-policy bindings, cumulative counters, semantic frontier and optional notes. Every committed transition has a complete snapshot. A transition snapshot may record an unknown in-flight physical attempt; a handoff checkpoint cannot hide live or unsettled work. |
| `composer_continuation_effects` | Stable key from parent+retained response/tool index; arguments/input hash, effect domain/owner, prepared/dispatched/committed/refused/unknown disposition, authoritative receipt locator and exact replay result. Unique effect key; new physical retries have new provider attempt IDs. |
| `composer_continuation_provider_ordinals` | Parent+provider family next ordinal, allocated transactionally before custody creation. Gaps are allowed; duplicate ordinals are not. Link attempts to original segment/generation and existing quota/audit owners. |

Session service owns private retention; existing quota and audit services keep
financial/physical-attempt authority. Extend session archive/deletion and
quarantine code for child tables/chunks, with dependency/cascade tests. Do not
delete active/reconciling state because an optional diagnostic retention window
expires. Do not promise an export, encryption mechanism or retention duration
that does not exist. Keys, decrypted secrets and auth headers never become
checkpoint content. Required fields that fail complete credential admission
cause an explicit persistence refusal, not truncation.

### Authority APIs and atomic transitions (proposed signatures)

Add these methods to the existing Composer/session authorities and private
session service protocol; actual SQL connection stays behind mutation facades:

```python
claim_segment(*, limit: int) -> tuple[ComposerSegmentClaim, ...]
start_segment(claim: ComposerSegmentClaim) -> ComposerSegmentRunning
commit_transition(running, *, expected_head, expected_cursor, event_batch,
                  next_frame_stack, effect_transition=None) -> TransitionReceipt
prepare_effect(running, *, expected_cursor, effect,
               next_frame_stack) -> EffectReceipt
commit_checkpoint_and_successor(running, *, expected_head, expected_cursor,
                                checkpoint, next_action) -> ComposerSegmentYieldReceipt
recover_segment(*, session_id, operation_id, expected_generation) -> RecoveryDisposition
resume_composer_operation(services, *, running, checkpoint, lease,
                          observation) -> ComposerSegmentOutcome
```

`ComposerSegmentOutcome` is the closed sum of a verified terminal receipt and
a verified yield receipt. Never return a fabricated ComposerResult to release
a worker. Yield transaction: validate current actor/policy, cancellation, live
session lease/parent generation, expected graph head and journal cursor; retain
all completed results; insert checkpoint; mark predecessor yielded; revoke its
generation; insert the unique successor; advance parent pointer/revision.
Commit together under the session transaction, then notify the worker. Read
back the exact yield before closing/sealing the required-work cohort and
releasing the session lease. Lost notification is recovered by scanning SQL.

The parent admission reservation remains held between segments. The transient
session writer lease is released after the yield; a successor obtains a new
one. Ordinary fresh-message admission still returns busy and preserves the
new draft. Audit settlement may finish for an old generation, but no old
generation can mutate a graph, publish a reply or create another successor.
Old COMPOSE fence lookup must not fail open when the parent pointer changes:
retain predecessor segment/fence history and consult it in the SQL predicate.

### Complete transition snapshots and recovery algorithm

Choose **snapshot at every durable transition**, not an event-suffix reducer.
`commit_transition` atomically writes its event batch, immutable complete typed
snapshot and parent snapshot/cursor/revision pointer. Events are attribution;
they are never replayed as commands. Standalone append is private to that same
transaction, not an API that may advance events without resumable state.
Large unchanged messages/native values use immutable chunk references, so a
snapshot does not copy an entire conversation prefix again.

| Transition | Same-transaction persistence and restart behavior |
| --- | --- |
| V2 admit | Validate actor, body/version and base; check admission quota once; insert parent, reserved message UUID, `initialize` snapshot and sequence-1 queued segment atomically. No user row exists yet. Replayed POST returns that parent without another quota charge/segment. |
| Ingress | `add_message_with_transcript` receives the authority-reserved ID, validates it against this parent, and inserts user row, transcript, parent actual user-message binding and initialized snapshot in one transaction. Crash before commit repeats initialization; after commit restores that receipt. V1 caller behavior stays unchanged. |
| Prepare provider/tool | Persist exact input/arguments, stable effect key, next frame, allocated provider ordinal where relevant and disposition `prepared` before admitting a physical attempt. No dispatch occurs before this snapshot is durable. |
| Physical admission | Extend the existing session/quota transaction to insert its actual `ProviderAttempt` and associate it with the prepared effect/snapshot atomically. Do not invent an attempt ID or infer quota admission from a preallocated ordinal. |
| Pre-SDK | Persist dispatch intent with actual attempt/input binding and current authority. Crash after this marker is conservatively unknown even if the process may not have reached the SDK. Never label the provider resumed. |
| Complete response | Persist exact admitted response/native fields and a `response_retained` snapshot before executing any tool. Retain pending audit settlement in the frame. Crash restores the response and settles its actual attempt before tool dispatch. |
| Session SQL effect | Mutation, real owner receipt, exact tool result, event and next snapshot commit in the same owner transaction. Post-commit projection failure restores the receipt/result; it cannot reexecute the mutation. |
| Cross-store effect | Snapshot records prepared/dispatched/unknown and the owning receipt key. The external owner commits separately. After loss, resolve its receipt before persisting a known result/next snapshot; no guessed distributed atomicity. |
| Yield | Drain owned work, verify safe frame, then checkpoint+predecessor yielded+generation revoke+unique queued successor+parent pointer/revision atomically. This snapshot is `kind=handoff`. |
| Final/Stop | Current authority, cancellation and head checks plus durable final result/error and terminal pointer/state commit together. Existing private request cleanup can run only after resume is no longer needed. |

Reserve initial user message ID as UUID5 over a fixed product namespace and
`(session_id, public operation_id, "composer.v2.user-message")`; persist the
value. Recompose uses its already verified original user-message ID instead.
No client can supply or change this reserved ID. Add the reserved-ID parameter
to the actual ingress service/protocol/SQL facade, not a second message writer.
`LogicalRequestBinding` can therefore be stable before the user row is inserted;
the new reserved field is not a foreign key to an absent row. Actual binding
retains the existing same-session FK once ingress commits.

On restart: lock parent/session; validate current actor/schema/policy; locate
the parent-selected snapshot and exact cursor; revoke expired ownership;
recover the **same segment sequence with a new generation** if it did not yield;
insert a new immutable attempt/fence history entry. A yielded predecessor is
never recovered; only its already queued successor is eligible. Restore typed
state, reconcile all prepared/dispatched/unknown effects and unsettled actual
attempts, then enter its recorded next transition. A committed event beyond
the selected snapshot cursor is an integrity error, not a suffix to guess how
to execute. If a transaction failed, none of its event/snapshot/pointer state
is acknowledged. If a required owner is unavailable, persist `reconciling` or
`retry_waiting` eligibility and release worker capacity; timeout alone does not
make it terminal. Cancellation remains on the same parent through every phase.

Takeover/failure transitions use the same snapshot transaction and generation
predicates as normal execution. Parent CHECK bundles explicitly permit v2
running/waiting without a currently live session lease, backed by exactly one
queued/reconciling current segment. Legacy v1 running bundles remain strict.
No request is stranded between an old lease release and new scheduling intent.

### Stable logical evidence versus current execution authority

V2 introduces `LogicalRequestBinding` with session, public parent operation ID,
original user-message ID and request hash, distinct from
`SegmentExecutionBinding` with segment sequence, generation, session-fence
operation ID/epoch and lease. The logical binding remains stable; execution
binding changes on takeover/resume. No stored lease is revived.

In `turn_effects.py`, add closed `ContinuationEvidenceState` and a nominal
`TurnEvidence.for_continuation(state, current_execution_binding)` factory.
It restores all evidence data under a freshly validated current execution
binding. Keep existing `operation_id` semantics for live witness checks and
the v1 adapter; never relabel that fence ID as the public parent ID. Version
v2 identity/abandonment/publication/semantic-key construction explicitly around
the stable logical scope, exact graph/candidate, evidence coverage, immutable
physical-call references and relevant policy. Do not include the *new current*
fence merely because custody changed. Stored physical-call/creator facts still
retain their actual original fence and attempt. V1 identity hashes remain
byte-compatible. Update `service.py` evidence-owner checks, planner continuation
validation, advisor review scope models and session publication checks together.

New `CommittedCheckpointWitness` values still come only from actual current-
fence commits (`sessions/_persist_payload.py:244`); their creator must match
the current live authority. To restore earlier effects, use a separate owned
`ValidatedHistoricalEffectReference`, constructed only by the session authority
after checking the immutable effect receipt, original creator fence/segment
lineage, parent logical binding, graph content and saved revision IDs. Do not
pass an old witness through the live creator-equality check, change its creator,
mint a new witness for an old commit, or count a restored revision again.
Forged/cross-request/unknown history refuses before graph mutation.

An unchanged restored review retains its exact input/call/candidate/coverage
binding after current authorization and compatible policy validation. A new
physical authoring candidate, graph/request/evidence/tool/provider-policy change
invalidates affected review/publication state into explicit review debt. A
custody change alone does not erase semantic progress or trigger duplicate
advice. Publication still requires current SQL execution authority at commit;
historical semantic evidence is not a live mutation permit.

### Exact resume state and effect handling

The first implementation work unit makes the following source state explicit.
This is a seed map, not a claimed exhaustive locals inventory; the equivalence
tests below decide completeness before v2 admission can be enabled.

| Frame / source owner | Retained data consumed by the next transition |
| --- | --- |
| Turn initialization / `composer_turn.py` | Original admission, compose base/head, user-row/transcript receipt, ingress provenance, title phase/attempt/outcome, policy/plugin snapshot bindings, pending post-compose settlement. Crash after user insert resumes the same ingress receipt. |
| Ordinary / `service.py:2483` | Exact messages/tools, initial version, composition/discovery slice and cumulative counts, mutation-success fact, discovery/preflight cache contents and freshness, anti-anchor history, validation/preflight results, advisor calls/repair/checkpoint counts, advisor review state, persisted assistant ID/content/tool-turn flag, failed turn, current state ID, advisor-repair-context flag and pending elision indices. |
| Tool batch / `tool_batch.py:912` | State/last validation/preflight/advisor count, complete admitted response, next tool index, already settled outcomes, rejection/proposal/blob custody references, publication debt and exact result envelopes. Refactor intra-batch locals; no rerun of the earlier tools on resume. |
| Planner / `pipeline_planner.py:3448` | Exact messages/tools, pending/supplied information manifest and discovery policy, seen-discovery rounds/no-gain state, total calls/cost/cost-known, repair/prose/decline counters, argument retry/history, selected schema contracts, rejected candidates/fingerprints, pending delivery, trail cursor, escape-hatch phase, original candidate/proposal and publication bindings. |
| Advisor / completion | Exact input/coverage and request/graph/policy binding, native context if used, pending physical attempt, parsed decision and its scope, pending repair/action/publication obligation. A graph CLEAN verdict is separate from stop confirmation and message publication. |
| Provider / audit | Exact ordered request/response/native bytes, complete tool-use/result pairing, immutable guard provenance, attempt/family/ordinal, physical-dispatch/usage/cost disposition and durable audit cursor. Rebuild runtime owners under the new lease. |
| Evidence / `turn_effects.py:269` | Persisted effects and saved-revision frontier, abandonment review/limitations, page/quoted inspection registries, conflicts, registration revision, delivery generations, current/historical pages and call bindings, retained authoritative history, stable logical scope and original physical creator references. Restore without resetting these collections. |
| Audit buffer / `audit.py:287` | Invocations, LLM calls, planner attempts, withheld replies, private candidates/authorings and their staged-versus-durably-committed channel cursors. Reconstruct a lock, not the old lock object; avoid redraining already committed channels. |
| Disclosure / `schema_disclosure.py:13` | Successful schema-disclosure set under session/catalog/policy bindings. Reconstruct for a cold service, or invalidate explicitly and record new evidence acquisition; do not silently treat restart as first disclosure. |

After H2 the ordinary compose loop is the common live entry. At d647, the
source call chain is `service.py:1264 -> PlanningApplication._plan_and_stage_empty_pipeline
(:996/:1093) -> plan_pipeline`; `sessions/routes/_helpers.py:3151` is a docstring
reference. H2 removes that sole production selector and retires the now
unreachable private facade, its facade-specific helpers/tests and stale comment.
Keep the direct planner library and its component tests; they do not obtain a
new restart-capable public entry. V2's baseline frame stack is ordinary compose,
tool batch, advisor and END. Planner-specific continuation edits below are
conditional delta work only if P0 discovers a genuinely new retained production
caller in release repairs; then update/review this dependency before executing.
That caller would also need its private response namespace/candidate IDs
(`pipeline_planner.py:3507`); do not invent a new selector to justify that work.

Cache invalidation depends on content, settings, catalog and policy bindings;
version-only cache keys are insufficient. Missing/stale data is revalidated
explicitly; never silently reconstruct a private conversation from chat or
notes. Append timing observations and frame transitions; do not rewrite a
signed/native prefix. Context-window exhaustion is a precise retained host
limitation if no supported provider transition exists. Compaction, approximate
fresh-context recovery and independently durable provider activities are
excluded from this first implementation.

Every exposed tool needs an effect-owner classification using the **live
registry** at implementation time, not a name regex. Use existing
`tools/_registry.py:resolve_tool_effects` and `ToolDeclaration` metadata as the
starting authority, then review actual handlers and async session carve-outs.
The continuation tool is an orchestration control in the same schema palette
layer as existing async/recovery controls, not a fake synchronous mutation
handler. Classify reads, session SQL mutations, blob/filesystem writes,
proposal/interpretation operations, preview/runtime effects, secret operations
and provider calls. Include aliases and nested advisor/planner effects.

Session SQL mutation + authoritative effect receipt + journal outcome share
one transaction wherever they share an owner. Cross-store effects use their
existing receipt/finish-once protocol and stable effect ID. Known completed
effects replay their **result**, not the action. Provably undispatched effects
may dispatch. A dispatched effect without a retained result is unknown: query
the owning service before retry. Missing receipt alone is not proof of absence.
An unresolved owner remains reconciling; a proven irreconcilable non-idempotent
effect becomes a specific host failure. No universal exactly-once claim.

### Independent time, fairness and accounting

Keep legacy `composer_timeout_seconds` and synchronous transport semantics.
Add these exact fields in `web/config.py`:
`composer_continuation_enabled=False`,
`composer_continuation_segment_seconds=300`,
`composer_continuation_watermark_seconds=120`,
`composer_continuation_quantum_seconds=180`,
`composer_continuation_settlement_reserve_seconds=None` and
`composer_continuation_action_bounds=None`. The last is a closed
`ContinuationActionBounds` model with independent primary/advisor/title/read/
session_sql/blob/external action policies and declared applicable stages.
Provider policies use P/G/Q/H/V where those stages actually run; non-provider
policies use preparation/execution/retention. An inapplicable guard/quota/
physical-provider stage is explicitly absent/zero, never a fictitious wait or
duplicate reserve. Every executed stage has a positive finite bound, every
action has a positive finite total C, and R remains separate. Reject missing,
unknown, unbounded or inconsistent policies when enabled. R must be explicitly
positive when enabled; no live reserve default is invented. With the flag off,
legacy settings/constructors remain valid and no v2 admission occurs. Starting
numeric values are engineering values requiring qualification, not live facts.
Add settings validation and constructor wiring in `web/app.py:2301`
and `:2322` and the typed service bundle in
`sessions/composer_app_services.py`. No implicit environment rebuilding.

For each retained action define independently enforced `C = P + G + Q + H + V`
for preparation, guard, quota admission, physical response and complete-response
retention. Distinct `R` closes remaining audit/quota/checkpoint settlement.
Nested advisor/planner work is a frame, not one unbounded tool allowance.
Use role-specific physical `H`; the guard's own call is inside `G` and remains
physically accounted. Do not derive C or H from segment remainder.

At segment start use database time and
`duration = max(nominal, next_action_allowance + R + watermark)`.
Before each action require `hard_remaining >= C + R`, valid policy and no
pending cooperative yield/cancel. Retain the exact next action and yield if it
does not fit. A fresh properly sized segment that still cannot fit refuses an
admission invariant instead of creating empty successors; bounded resume
preparation must be included in the descriptor/headroom. At the watermark or
quantum, finish an already admitted healthy action and then yield. Do not cancel
it solely to meet the watermark. A long action can exceed 300 seconds with an
adequately sized segment and transport. An immutable smaller worker limit is
a deployment incompatibility, not proof of task incapability.

Freeze the model-visible timing observation **before** immutable input/guard:
segment sequence/generation, observed_at, hard remaining, cooperative remaining,
watermark and yield-pending. Recheck cancellation/action allowance/fence before
SDK entry without editing the guarded input or renewing any clock. Refreshing
that input requires full rebinding under the original action deadline.
Monotonic anchors are process-local; persist database times and re-anchor on
claim. Audit allowance stages and enforce each actual wait once.

Order eligible segments by `(next_eligible_at, enqueued_at, session_id,
operation_id, sequence)`. New successors get fresh eligibility/queue times,
not original parent age. Preserve one parent admission charge, request-wide
provider attempts/planner cost/reservations/advisor policy counters and unknown
cost. Technical composition/discovery/repair **slices** can yield; explicit
operator policy caps remain request-wide unless separately changed. Trace
every exhaustion branch in service/planner/advisor/tool helpers to its actual
scope, and reject an unclassified reset. No new total segment/node/task-time cap.

| Existing control | V2 disposition selected here |
| --- | --- |
| composer_max_composition_turns / composer_max_discovery_turns | Technical slice thresholds. Retain request totals separately; reset only slice counters after a committed yield, with pending action/review/history intact. Reaching a threshold does not settle the request. V1 remains unchanged. |
| Internal composition_completion._MAX_REPAIR_TURNS | Technical repair slice. Before another repair at its boundary, retain exact failed action/evidence and enter the continuation/Advisor action controller; never clear unchanged action history to manufacture another repair budget. |
| composer_advisor_max_calls_per_compose / composer_advisor_checkpoint_max_passes | Existing explicit request-wide policy limits; count all matching real attempts/passes across segments. If actually exhausted, retain the specific policy refusal; do not reset at rollover or call it task incapability. |
| composer_max_tool_calls_per_turn / provider request/completion/context bounds | Per complete response/batch/physical call; mixed control batches still reject before mutation. |
| Admission rate quota, token/storage quota and actual guard policy | Existing owner and scope retained. One new-user admission only, real attempts remain chargeable across all generations, unknown usage is not zero. |
| Planner call/cost/repair controls | Retained direct library/v1 semantics; no baseline v2 public planner entry after H2. If P0 establishes a new production caller, revise the plan before changing those scopes. |

Execution's exhaustion-branch trace verifies that all actual callers consume
these scopes; it is a completeness check against the selected behavior, not
permission to make new cap decisions during implementation.

### Progress, stop scope and human controls

Use durable evidence/action history. The host checks receipt identity, action
recurrence and graph/cursor facts; **the Advisor judges semantic relevance,
completeness and whether a negative investigation is useful**. Do not implement
an English classifier or host relevance score. Graph changes, notes, timers,
leases, renamed labels and scheduling receipts never certify progress.
Discovery remains actual evidence for a bound model assessment. The schema
below replaces any implied host semantic judgment in the high-level design.

The executor owns `authoring -> advisor_needed -> action_required ->
action_outcome`. A repeated continuation-only/no-progress response gets its
exact normal tool result and does not generate a fresh empty segment. Where
no productive action remains, invoke the existing advisor owner directly.
A v2 CONTINUE decision must retain a concrete action, target obligations,
role/tool/capability and premise/outcome to inspect. Route model-prescribed
arguments through the normal role owner; the host must not invent graph
structure. Mark attempted only from an execution receipt. Reject unchanged,
already exhausted advice at the same frontier as a control-protocol error;
keep the request/state and identify the failed contract. No arbitrary N-tries
cap substitutes for this transition. Recoverable infrastructure waiting may
still move to another segment without claiming semantic advancement.

V2 advisor schema adds requirement IDs, decision scope (subset/whole_request),
still-actionable obligations and a closed prescribed-action binding. Preserve
existing v1 decision decoding for legacy jobs. A subset blocker cannot stop
other authorized work; STOP_CONFIRMED whole-request needs actual whole-request
scope. Neither verdict grants graph readiness or END/publication clearance.
Provider terminal recovery in ordinary, restricted and planner paths must go
through this review owner. Direct authenticated human Stop/Revert bypass that
model adjudication and retain their authority.

Implement the closed v2 decision in `advisor_output.py`, its input producer in
`advisor_abandonment.py`, and the durable controller in a new
`composer/continuation_progress.py` module:

| Data | Exact fields / authority |
| --- | --- |
| Root obligation | ID `request` in the stable logical-request namespace; covers the whole original request without host interpretation. |
| Child obligation | Host-assigned immutable ID, Advisor-authored description, exact original human message ID plus code-point `[start,end)` references and parent ID. Host validates source identity/ranges and never deletes/renames active IDs to create novelty. Description/ID additions alone are not progress. |
| Evidence reference | Actual retained tool/provider/validation receipt ID, content/range hash, original attempt and coverage. A model-supplied nonexistent ID is a protocol error. |
| Frontier | Ordered obligation states (`open`, `blocked`, `satisfied`), accepted evidence references, pending review debt, normalized action/outcome history and graph hash. Status changes require a bound Advisor decision; they are not derived from prose. |
| V2 Advisor decision | `schema_version="composer.continuation-decision.v1"`, decision ID from the actual Advisor call, decision enum (`CONTINUE`, `CONTINUE_WITH_LIMITATION`, `STOP_CONFIRMED`), scope enum (`subset`, `whole_request`), obligation IDs, evidence refs, reason, remaining-actionable IDs and nullable prescribed action. Strict extra-field refusal. |
| Prescribed action | Kind enum (`primary_turn`, `read_tool`, `review`), target obligation IDs, input/evidence binding and policy/tool-manifest binding. `primary_turn` carries a model-authored action instruction; `read_tool` carries a currently permitted read-only tool name and exact model-authored JSON arguments; `review` carries the exact candidate/evidence reference. Graph/mutation arguments always go through normal primary authoring/provider and tool admission. No arbitrary wake time or user approval. |

Normalize an action as `(kind, target IDs, tool name, canonical arguments/input
hash, evidence-premise IDs, graph hash, policy hash)`. Advice wording cannot
discharge an unattempted action. Dispositions are
`prepared|dispatched|committed|refused|unknown`; only real owner receipts mark
attempted. If the same action/premises recur or a pure continuation proposal
has no pending executable action, schedule Advisor directly with actual new
evidence for its relevance assessment. Do not infer inability or add an
attempt-count threshold. Technical rollover cannot reset this history.

CONTINUE/CONTINUE_WITH_LIMITATION require an action and no question for the
user. STOP_CONFIRMED has no action; subset scope cannot settle the root while
other work is actionable. Whole-request stop requires explicit complete request
scope/coverage, no unattempted action, and separate final-message admission.
The host validates structural conditions and source bindings; the provider
owns the semantic verdict. An inconsistent shape enters its owning protocol-
error recovery path with the exact rejection; unchanged rejected state cannot
mint another segment or advice call. Raw decisions are private until the
normal reply/redaction owner admits user-visible text.

Persist Stop on the parent; check it at claim, resume, action admission,
pre-SDK dispatch, effect commit, yield and final completion. Stop versus commit
uses session transaction order. Revert admission cancels the logical request
and revokes its generation in the same exclusion domain before obtaining its
normal revert receipt/lease. Old late responses settle audit/cost only. If
Revert then fails, report stopped build plus failed revert; do not resurrect
it. Revert does not undo external effects.

Database/dependency outage uses durable eligibility/backoff (1,2,4,8,16,30s as
engineering retry schedule), releasing worker capacity while waiting. Five
minutes may change waiting/escalation information, never recovery eligibility.
Equivalent state before/after that time resumes identically. If Stop cannot
persist, expose it as unconfirmed and keep client cancellation intent until
acknowledged. No new dispatch while physical/effect custody is unknown.

## Dependency-ordered implementation checkpoints

Each work unit below is future work. First add a failing behavioral test and
prove it fails for the intended reason, implement the bounded change, run its
focused checks and relevant whole-tree gates, then commit only owned paths
through ordinary branch-safety/hooks. Keep lane logs and exit codes private.
Do not run any command in this section before the conditional readiness/handoff gate and
fresh resource admission. One owner executes serially at the integration
boundaries; these are component responsibilities, not another owner fleet.

### Execution order and checkpoint discipline

The concrete order is:

`P0 -> H1 -> H2 -> H3a -> H3b -> H3c -> C1a -> C2 -> C3 -> C1b ->
C4a -> C5 -> C4b -> C6 -> C7 -> C8 -> S7 -> Q`.

Q means combined integration, deployed acceptance and protected delivery in the
acceptance table; it is not an extra implementation lane. V2 admission stays
disabled through C8 and S7. Each unit can be committed with disabled/unreachable
v2 wiring and green affected legacy behavior. Do not enable a half-built writer
merely to test the next unit. C1b/C4b are explicit integration gates, not cycles
back into an earlier task's definition of done.

Source path shorthand in work units resolves as follows: unprefixed production
`.py` names mean `src/elspeth/web/composer/`; `sessions/`, `coordination/`,
`frontend/` and `web/` refer to their explicit trees under `src/elspeth/web/`
(drop the `web/` qualifier when joining); `contracts/` is
`src/elspeth/contracts/`. Exact test command paths below disambiguate test
filenames. Newly named files are deliverables to create, not existing-source
claims. Retained planner library edits are not required in the baseline after
H2; P0 material production-caller drift requires plan revision first.

For each unit: add its RED behavioral cases; run the named focused packet and
confirm assertion failure for the intended missing behavior, not collection/
import/prerequisite errors; implement; rerun GREEN and affected whole-tree gates;
review the owned diff; run branch-safety and ordinary hooks; commit the coherent
unit. If a gate fails, remain at that unit, classify actual base counterevidence
where relevant and repair or report the exact blocker. No skip/no-verify,
suppression or later ticket substitutes for that exit contract.

After any interruption, use actual branch commits/status and the lane's latest
completed native exits to determine the next unit. Preserve uncommitted owned
work; do not reset/stash a dirty tree or infer completion from a planned
checkbox. A failed or unfinished check is not GREEN. Lane notes record current
unit, source SHA/tree, owned dirty paths, latest real exit/log, unresolved failure
and next command. No sealed plan package or additional approval chain.

### P0 — Rebind and admit execution

Apply the conditional readiness gate and compare the actual current candidate
with d647; when the first-release PR exists independently bind its identity/head
as well. Refresh every changed seam above and the final audit/.7 state. Confirm no duplicate owner,
current source/index, target version, environment/import paths and resource
capacity. Preserve this branch's documents across any agreed rebase. No
primary checkout change until its later exclusive handoff. Deliver an updated
plan section and scoped source diff; material changes return to the same
independent reviewer, not another review fleet.

**Exit:** conditional readiness and orderly custody are evidenced, changed dependencies resolved,
all future work has an immutable current source binding. This step cannot be
satisfied by reusing the startup receipt.

### H1 — Remove hidden retry and guessed progress

Modify `composition_completion.py` to delete the **entire** pre-state
interpretation-review retry at d647:1603–1619; remove its now-unused helper
and classifier imports in `no_tool_policy.py` and consumers. Preserve original
request, exact tool error, interpretation requirements and genuine surfacing
repair below that branch. Do not remove only the classifier conjunct: that
would broaden the injected sequence. Recovery copy is factual, for example
an invitation to ask Composer to retry, without hidden instructions or a new
approval flow.

Remove keyword fallback selection from
`frontend/src/components/chat/ComposingIndicator.tsx`; use backend progress or
neutral waiting text. Preserve Stop, terminal/error state and accessibility.

Tests: modify `test_rootless_no_tool_recovery.py`,
`test_request_intent_policy.py` only where the selected behavior is retired,
`test_no_tool_finalize_disclosure.py`, and `ComposingIndicator.test.tsx`.
Add constant-state ARG_ERROR tests across paraphrases, quotations, >4096
characters and no prior failure; assert **no** injected stage/wait/review
sequence, genuine review refusal still works, and Stop remains available.
Use synthetic public data only. A deletion assertion alone is not the oracle.

**Exit:** provider chooses recovery from the retained request/error; the host
does not choose a tool sequence or invent completed work from words.

### H2 — Replace text-based routing, history and discovery debt

Modify `service.py:1221` to remove the **whole English-selected empty-graph
planner shortcut** and use the existing ordinary `_compose_loop` as the common
ordinary/recompose authoring entry, independent of graph emptiness or wording.
It already supports provider tool choice, `set_pipeline`, and a no-tool
informational reply through the normal END/publication owner. Delete the unused
surface classifier/telemetry consumers; do not introduce a new planner-selection
tool or route an answer through decline/park/reset. The provider authors graph
structure and chooses tools. Read-only questions, negations and quoted examples
cannot become implicit mutation authorization. Retain actual tool/review/approval
prerequisites. Compare complete-build capability/accounting on the common path;
if it cannot do the requested work, repair that path before this checkpoint exits.

Replace `_freeform_planner_conversation_context` in
`planning_application.py:193` with the common owned authoritative request-history
projection used by the ordinary entry and any actually retained planner caller.
Provide the complete earlier user-request chronology, including middle
amendments, with authorship markers. Remove referential-phrase selection and
first-plus-last-seven loss; remove unreachable facade helpers after tracing
actual callers rather than creating another speculative surface.
History is data, not a new user instruction. Preserve explicit current-request
and source identities. A real complete-input/provider bound returns a precise
refusal, never a silently truncated success. Apply the same owned requirement
projection to ordinary compose and later advisor frames; do not summarize
amendments by host inference.

Remove `_intent_selected_schema_keys` and its caller additions from planner
pending-information/declaration/export logic. Keep actual catalog omissions,
unavailable entries and evidence from discovery results. Quoted or negated
plugin-name tokens create no host-owned discovery obligation.

Tests: extend `test_service.py`, `test_pipeline_planner.py`,
`test_compose_loop_anti_anchor.py`, `test_request_intent_policy.py` and
`test_provider_context_planner_integration.py`; update only obsolete oracles.
Compare one state across paraphrases/length/quotes/negation, ordinary UI and
tutorial input; check per-transition provider calls, a question that authors
no graph, a negated build and quoted example that neither propose/park/reset
nor mutate, a genuine build reaching provider-authored construction, full middle
amendment and real omitted-schema discovery. Exercise the actual common provider
boundary with session/user-message custody and END owner; inspect prepared
instructions, tool sets and final result disposition, not a mocked application
return or route log string.

**Exit:** routing/history/discovery no longer depends on English grammar or
plugin-token spelling. Neutral replies and complete builds both work through
the common reviewed provider path; no capability blocker or graph-recovery
action is manufactured to answer an informational question.

### H3 — Three bounded evidence/guard/reader patches

Rebind each patch against the release owner's committed request/transcript
repair at P0. Keep a behavior already supplied there and verify it rather than
reimplementing it. H3a -> H3b -> H3c are separate focused checkpoints before C1.
No new approximate long-text classifier or global unbounded scanner is selected.
The f4c2 committed-candidate refresh above is incorporated before these patches;
the whole-current-request-once repair is carried, not reimplemented.

**H3a: complete required review evidence.** Modify `advisor_context.py`,
`advisor_checkpoint.py`, `advisor_abandonment.py`, `advisor_request.py` and
`turn_effects.py`. Add an owned `AdvisorEvidenceCoverage` value with schema
version, request evidence binding (current `TurnEvidence.identity`; C1 adds the
v2 logical binding adapter), graph hash, required semantic-unit IDs/hashes,
included IDs, redacted/name-only IDs with reasons, optional omissions and
`complete_required` flag. Unit IDs are owned graph path/query identity plus
content hash, not model-authored coverage claims. Complete required units are:
the full redacted current request once, chronological authoritative amendments,
structural graph/contracts, full currently value-visible prompts/system prompts/
query templates/expressions/field mappings and actionable findings. Render each
required unit once, without the old per-value 120/1000 prefix cuts. Existing
secret/storage-name exclusions remain explicit redacted/name-only units, not
falsely claimed inspected values. No all-plugin metadata redesign is implied.

`fit_advisor_checkpoint_summary` reserves those complete required units first;
the optional allowance can only omit duplicate/presentation prose. Share the
same producer for admission, exact guarded provider input and retained coverage
binding. Remove duplicate current-request excerpts where the release repair has
not already done so. Whole-scope CLEAN/STOP_CONFIRMED/publication require complete
required coverage; a missing required unit or actual provider/guard resource
refusal raises the owned evidence-unavailable outcome, retaining saved work.
Do not convert that into confirmed user-task incapability or silently certify a
subset. Optional diagnostic capture may fail independently. Tests:
`test_advisor_mandatory_evidence_budget.py`, `test_advisor_graph_visibility.py`,
`test_advisor_request.py` and `test_advisor_terminal_publication.py`; inject a
material late prompt tail, a middle amendment, duplicate request, excluded
secret marker and one omitted required unit, asserting actual prepared input
and denied whole-scope verdict where appropriate.

**H3b: separate logical admission from physical guard limits.** Modify
`prompt_guard.py`, `prompt_guard_rendering.py`, `prompt_guard_clients.py` and
`provider_gateway.py`. Split `_request` validity into exact structural/provenance/
Unicode/credential validity and physical service-fit. Always perform complete
credential/structure checks first. With no configured mount and existing
`debug-when-unconfigured` policy, audit that policy's bypass for structurally
valid input independently of a nonexistent service's 10K physical envelope.
Required mode without a mount remains unavailable. A configured mount retains
the actual per-prompt/document/batch limit and whole semantic-unit batching in
`prompt_guard_clients.py:38`; require every physical batch to allow. An
indivisible complete value exceeding that real service bound stays a precise
unsupported-input refusal. Preserve f4c2's producer-owned current-request
partitioning and exact untrusted coverage; do not add generic raw slicing as
a new size fallback. No padding, dropped tail, trusted-label upgrade or claim
that window coverage proves classifier equivalence. This
plan delivers no new long-prompt support for that configured physical adapter.
Fingerprint validity policy and physical adapter policy separately, bind both
to the exact frozen request, and audit no authoring dispatch on guard failure.
Tests: `test_prompt_guard_producer_parts.py`,
`test_prompt_guard_advisor_projection.py`, `test_provider_context.py` and
existing prompt-guard client tests: long valid debug input, long required input,
malformed/credential input in debug, full-value credential at a would-be seam,
multi-batch failure and immutable guard/journal/SDK equality.

**H3c: inspect the complete immutable attachment before paging.** Add
`composer/attachment_admission.py`; modify `tools/blobs.py`, `tools/_common.py`,
`tool_batch.py` and `web/config.py`. Add explicit operator field
`composer_attachment_inspection_max_bytes`, defaulting to the deployment's
`max_upload_bytes`, as a real bounded inspection resource policy. Pass it through
ToolContext. Under `_locked_read_ready_blob`, verify current same-session ready
custody/hash/actual byte length, apply that policy before decode/scan, and scan
the complete UTF-8 original with `require_no_credential_material` using its
existing explicit `max_string_bytes` argument. Keep the global scanner default
and detector grammar unchanged. Scan metadata separately with the normal policy.
This removes the generic 256KiB string default as an attachment-size limit
without weakening whole-input recognition or pretending inspection is unbounded.

Return a nominal private `AttachmentReadAdmission` containing session/blob ID,
verified content hash/length, detector version and full policy fingerprint
(byte/depth/node/width bounds, env-reference names, additional credential fields
and original content interpretation). Cache only successful receipts in the
request-local tool context, keyed by those fields. Each later read still verifies
current custody/hash; changed identity/policy/detector, failed scan or a cold
uncached process requires complete rescan. C1 snapshots its receipt data; resume
revalidates the binding before reuse. Never persist a receipt for a partial scan.

Keep existing character cursors, hash checks and 4096-byte **serialized page**
limit; do not promise 4096 payload bytes. Reuse existing `read_attachment` and
retain its complete serialized-response bound, now explicitly distinct from
whole-original inspection. If complete response cannot fit, the model can read
the admitted original through existing pages. Validate/scan each output envelope
normally after slicing; no original scan is replaced by page scans. Tests:
`test_tools_blobs.py`, `test_blob_response_contracts.py`, plus new
`tests/unit/web/composer/test_attachment_admission.py`: original above 256KiB
within operator bound, late/seam credentials, scan-cache identity invalidation,
over-policy refusal, non-UTF8, actual serializer escape expansion and exact
cursor reconstruction. Tests instrument the full-original scanner to prove one
successful scan for unchanged repeated pages, with a changed hash/policy as a
negative control. Hash/custody verification itself is never skipped.

**Exit:** all three behavioral contracts and real resource refusals have focused
evidence. Required-evidence omissions cannot receive whole-scope approval;
unconfigured debug mode imposes no phantom physical-service limit; attachments
within explicit inspection policy remain completely scanned and pageable.

### C1a — Extract pure typed frames with v2 admission disabled

Create `src/elspeth/web/composer/continuation.py` (closed domain types) and
`continuation_codec.py` (versioned private JSON); modify `service.py`,
`_compose_loop_carriers.py`, `tool_batch.py`,
`planning_application.py`, `composition_completion.py`, `turn_effects.py`,
`audit.py`, `schema_disclosure.py`, `sessions/_persist_payload.py` and
`sessions/composer_turn.py`. Implement the stable-logical/current-execution
evidence contract above, with v1 adapter hashes unchanged. Extract the state table above into explicit
phase transitions, preserving existing behavior on the legacy adapter.

Create `tests/unit/web/composer/test_continuation_frames.py` and
`test_continuation_codec.py`. With deterministic public provider and in-memory
receipt-store doubles, compare uninterrupted versus serialize/new-owner/restore
at every pure phase: exact next prepared messages/tools/native fields, graph
content, result pairs, counters, pending reviews and final outcome. Discard all
old in-memory owners. Include partial batch, advisor/END repair, initialization
and any actually retained planner adapter. This step implements no SQL claim,
historical-effect validation or live v2 admission. Represent those dependencies
as typed ports and refuse production entry until C1b/C3/C4 are complete.
Unknown codec/tag/field, missing chunk/result and binding mismatch refuse before
any write. Mutate one retained counter, native block order, tool index and
publication binding to prove the equivalence instrument goes red. Preserve
H3c attachment-admission policy/receipt state in the same frame contract.

**Exit:** all pure next-transition state is explicit; no Task/SDK/runtime owner is
serialized; missing fields are caught behaviorally. V1 affected tests remain
green and no live v2 admission exists. Cold SQL proof is C1b after C3, not a
prerequisite that depends on code not yet built.

### C2 — Session schema, journal and private repository

Modify `sessions/models.py`, `sessions/schema.py`, `sessions/protocol.py` and
`sessions/service.py`; create `sessions/continuation.py` for SQL/private record
access behind session service. Add the persistence shape above, exact CHECK/FK
contracts and dialect-symmetric indexes. Allocate the then-current next free
epoch; do not assume 78. Keep the initializer preserve/refuse: a nonempty
predecessor or future schema is unchanged and rejected. No startup migration,
reset, checkpoint fabrication or downgrade.

Implement atomic append/cursor compare-and-swap, immutable chunk reuse,
complete record validation, effect prepare/result linking and checkpoint
load under current authorization. Keep keys/headers out; journals are private
and bypass public DTO serializers. Wire archive/deletion/quarantine ownership
after inspecting those current callers. Size admission must be explicit and
nontruncating; measure actual retained bytes during qualification.

Tests: create `tests/unit/web/sessions/test_continuation_repository.py`,
`test_continuation_schema.py`; extend `test_schema.py`,
`test_composer_operation_codec.py`, `test_composer_operation_dtos.py` and relevant
archive lifecycle tests discovered through actual service calls. Check complete
metadata and bytes before/after predecessor/future/corrupt refusal; use a valid
current schema as positive control and changed epoch/constraint as negatives.
Test cross-session chunk/effect substitution, CAS conflict, corrupt digest,
credential refusal, journal exhaustion and no public leakage markers.

**Compatibility decision:** the baseline implementation preserves the current
preserve/refuse policy. It does **not** include an offline upgrader merely
because Stage 2 proposed one. An existing installation cannot enable v2 on its
old store. Before rollout the operator must select an explicitly new session
store, retaining the old one/access with a compatible binary, or separately
authorize an exact-predecessor offline upgrade implementation and run. Such an
upgrade must drain active v1 jobs, preserve request/result/transcript bytes and
prove refusal without mutation on any mismatch. This is a rollout dependency,
not authority to reset or abandon existing requests. No migration is run by
this plan or inferred from early implementation readiness or a PR.

**Exit:** fresh v1/v2-capable schema works; old stores are honestly preserved
and refused. No historical-row readability/backfill is promised in a new
store. If an upgrader is selected later, add its explicit work unit and review
before claiming existing-store continuity.

### C3 — Transactional segments, fencing and effects

Extend `coordination/composer_operation_authority.py`,
`coordination/repository.py`, `coordination/contracts.py`,
`contracts/session_operation.py`, `sessions/composer_operations.py`,
`sessions/mutation_capabilities.py`, `sessions/service.py`, `sessions/protocol.py`,
`sessions/_persist_payload.py`, `composer/turn_effects.py` and
`composer/session_tool.py`. Add segment claim/start/yield/recover APIs,
typed claim/running/yield receipts and private mutation facade methods. Preserve
parent uniqueness and v1 SQL branches. Keep fence-history lookup so old
generation commits cannot become unbound synchronous COMPOSE writes.
Implement authority-only validation of historical effect references and live
witness creator checks. Preserve original creator/attempt receipts and prevent
counting their saved revisions again on resume. Semantic review reuse never
substitutes for a fresh live SQL mutation permit.

Produce the live tool/effect-owner classification in lane state during this
work unit, then implement journal/receipt joins for every exposed path in
`tool_batch.py` and actual handler owners. Inspect proposal finish-once,
interpretation, blob custody, preview and secret effects before enabling them
for restart. Unsupported unknown outcomes preserve state and reconcile/refuse;
never assume all tools are idempotent. Do not serialize secret values.

Tests: extend `tests/unit/web/coordination/test_composer_operation_authority.py`,
`test_session_mutation_facets.py`, `test_session_operation_fence.py` and
`tests/unit/web/sessions/test_composer_operation_lifecycle.py`. Add
`tests/testcontainer/web/test_composer_continuation_postgres.py` and extend
`test_composer_operations_postgres.py`, `test_session_mutation_fencing_postgres.py`.
Prove duplicate yields/claims, cancellation between each write, stale expected
head/cursor, late predecessor mutation, crash after effect commit before result
projection, lost notification and independent replicas. Assertions read
authoritative SQL and owning receipt, not an in-memory token. Deliberately
remove one generation predicate in a disposable test mutation to prove stale
write controls catch it; never repin a negative control to pass.

**Exit:** checkpoint+successor are atomic, one eligible successor, no revoked
graph write, one parent admission and honest effect reconciliation on both SQL
dialects. V1 behavior remains separately tested.

### C1b — Cold SQL restoration gate after C3

Depends on C1a, C2 and C3. Add
`tests/unit/web/sessions/test_continuation_cold_restore.py` and the corresponding
cases in `tests/testcontainer/web/test_composer_continuation_postgres.py`.
Commit under a real segment-1 fence; retain inspection, attachment admission,
buffer channel cursors and a bound review; discard the service and in-memory
owners; restore the latest complete snapshot under a genuinely new fence;
commit in segment 2. Read authoritative SQL to prove cumulative revisions,
coverage, semantic frontier, no double drain and the specified review reuse/
invalidation. A forged historical witness, wrong parent, removed event/snapshot
atomicity and late predecessor write must fail. C1b does not need live providers
or the final worker; use the actual session/authority APIs with public fixtures.

**Exit:** the pure codec/frame promise survives actual SQL identity/custody
change. Do not enable the worker on production v2 input before this gate.

### C4a — Worker yield, internal resume and restart recovery

Modify `sessions/composer_async_worker.py`, `composer_turn.py`,
`composer_operation_preconditions.py`, `composer_operations.py`,
`coordination/composer_operation_authority.py`, `coordination/lifecycle.py`,
`web/composer_watch_reads.py`, `web/required_work.py` and
`sessions/composer_app_services.py`. Preserve the required-work joining rules:
drain completed actions before checkpoint, verify yield receipt while live,
seal and release the old cohort, reconstruct a fresh cohort on successor.
Change `_verify_terminal_before_release` into an outcome verifier that accepts
only a nominal verified terminal or durable yield receipt. Normal yield must
not invoke failure settlement, publish a terminal assistant row or clear the
parent request. Shutdown/loss/cancel retain distinct dispositions.

Claim v2 only from segment rows; exclude it from legacy `claim_next`, expired
queued/running queries, timeout watcher and reaper settlement. Resume initial
ingress by its phase receipt without another user row/title charge. Restore
frames and reconcile dispatched/unknown attempts before more work. Unwritable
database keeps original intent discoverable; do not fabricate successful Stop.

Tests: extend `test_composer_async_worker.py`, `test_run_composer_turn.py`,
`test_composer_operation_clock.py`, `test_composer_operation_lifecycle.py` and
create `tests/unit/web/sessions/test_composer_continuation_recovery.py`.
Crash at: before/after user-row commit, response retain, tool prepare, effect
commit, checkpoint transaction, notification, predecessor release, successor
claim, title/post-compose settlement. Compare exact request/result/effect
identities and doubled accounting receipts with the uninterrupted oracle. Lost notification
must recover by scan; an actual absent result remains unknown. Test retry
waiting at 299/301/360 seconds under identical custody and Stop while SQL fails.

**Exit:** one request resumes through the real worker/SQL path with provider
and quota doubles, without a second human row, repeated committed effect,
lost required-work audit or false final result. Real physical-attempt/cost
reconciliation is C4b after C5; it is not claimed by these doubles.

### C5 — Independent action clocks and provider accounting

Modify `web/config.py`, `web/app.py:2301`, `:2322`,
`sessions/composer_app_services.py`,
`service.py:_call_llm_before_deadline`,
`provider_gateway.py`, `provider_quota.py`, `provider_context.py`,
`sessions/service.py`, `sessions/protocol.py`,
`coordination/quota_authority.py`,
`advisor_checkpoint.py` and tool/advisor timeout helpers. Add action descriptor
and scoped clock types to `composer/continuation.py`; remove remainder-based
timeouts only in v2. Freeze timing before exact input/guard/journal binding.
Persist ordinals before constructing custody; new physical retry is a new ID,
known result replay is no new call. Include title/guard/advisor/planner families
and quota refusal/dispatch-unknown/late settlement. Keep retries under the
existing explicit owner, with SDK retries disabled as configured.

Tests: create `test_continuation_action_admission.py`; extend
`test_provider_quota.py`, `test_provider_quota_end_to_end.py`,
`test_capture_llm_completion_boundary.py`, `test_provider_context.py`,
`test_composer_operation_settings.py`. Fake clocks distinguish queue, action,
segment, observer and lease time. Counterexamples: C incorrectly equal to
remaining; valid >300s call; unused long tool bound; slow restore/guard/admission;
fresh-fit failure; post-guard timing edit; hard deadline after dispatch; unknown
cost; duplicate audit/charge; ordinal restart. Record fit operands and assert
guard/journal/SDK input identity. One-worker scheduling proves productive
requests interleave and backed-off work does not retain its original queue age.

**Exit:** no undersized segment repeatedly truncates the same valid call, no
clock refresh extends one action, no accounting policy resets, and no timer
becomes a model-facing incapability claim. Production reserve adequacy remains
a measured deployment gate.

### C4b — Physical-accounting recovery gate after C5

Use the real `ProviderCallCustody`, SQL quota owner, gateway admission and
settlement with a deterministic network transport double, not quota/custody
doubles. Extend `test_composer_continuation_recovery.py`,
`test_provider_quota_end_to_end.py` and serial PostgreSQL continuation cases.
Crash after ordinal reservation, actual quota admission, pre-SDK may-dispatch
marker, response retain, quota/audit settlement and local projection. Assert
the cut-specific expected ledger attempts/cost-known flags/charges against
uninterrupted controls: retained-result replay adds no attempt/charge, whereas
a permitted retry after an unknown dispatch may add a real physical attempt
and uncertain cost. Never demand equal attempt totals by treating uncertainty
as zero or erasing the predecessor's attempt.
Unknown call custody must block redispatch until its owner resolves it. A safe
new physical retry receives a new attempt and ordinal; replay of a retained
response gets neither. C4a remains the worker control-flow proof; this closes
its separately identified physical-accounting dependency.

**Exit:** actual accounting owners agree across rollover/restart; no paid call
is needed for this deterministic integration gate. Live-provider transport and
measured reserves remain later deployment acceptance.

### C6 — Continuation control and advisor follow-through

Modify `provider_gateway.py:composer_loop_tool_definitions`, `tool_batch.py`,
`recovery_controls.py`, `service.py`, `composition_completion.py`,
`advisor_output.py`, `advisor_context.py`, `advisor_checkpoint.py`,
`advisor_abandonment.py` and `turn_effects.py`. Add the closed
`checkpoint_and_continue` scheduling schema to the appropriate control palettes
and wire contract declarations; accepts optional concise notes only. It is the
sole tool in a batch; reject mixed batches before mutation. Host-only safe
rollover does not require the model tool. Tool result confirms queued same
request, never claims successor execution; checkpoint contains exact pair.
Update the admitted control classification in `provider_gateway.py:825–831`
so recovery-only and continuation-only palettes stay distinct. Register the
scheduling schema through the existing gateway palette/strict dialect owner;
do not insert an async scheduling handler into the synchronous tool registry.

Implement durable semantic frontier and advisor/action transitions above with
v2 scoped decision validation. Route all reachable model terminal proposals,
including restricted recovery, through advisor owner and separate
publication gate. Direct human Stop/Revert remains immediate. No server graph
arguments or tutorial branch. Keep explicit resource failure separate from
advisor capability judgment. Model notes never replace stored facts.

Tests: create `test_continuation_control.py`, `test_continuation_progress.py`;
extend `test_recovery_control_palettes.py`, `test_graph_recovery.py`,
`test_planner_graph_recovery.py`, `test_advisor_output.py`,
`test_advisor_terminal_publication.py`, `test_pending_review_advisor_terminal.py`
and `test_wire_fidelity_matrix.py`. Test mixed batch, duplicate tool request,
pure automatic rollover, graph oscillation, useful negative discovery, no-op
notes, prescribed action not executed, unsupported/repeated advice, subset
blocker with actionable remainder, full blocker and incomplete review.
Assert every graph-authoring transition has its actual provider/role owner.
The retained direct planner library keeps its legacy component contract; if P0
finds a new production planner caller, extend its palette and PlannerHandoff
review adapter in the separately reviewed delta before admitting v2 there.

**Exit:** no self-parking bypass, empty continuation chain, false progress or
unreviewed final publication; valid further work keeps progressing without a
new user action.

### C7 — Closed v2 HTTP/SSE contract and cancellation/Revert

Modify `sessions/schemas.py`, `composer_operations.py`,
`sessions/routes/composer/operations.py`, `sessions/routes/composer/state.py`,
operation receipt authority/service facets and `web/composer_stream.py`.
Negotiate `Composer-Operation-Version: 2`, echo it and bind it to immutable
admission hash. Missing header means v1. V2 admissions remain disabled until
all lifecycle proofs pass. Preserve old hash bytes and v1 active-job deadline.

Use the existing closed SendMessageRequest/RecomposeRequest bodies unchanged.
Select lifecycle from the header before hashing. V1 hash algorithm/bytes stay
unchanged; v2 request hash is `stable_hash({"schema":"composer-operation-request.v2",
"lifecycle_version":2,"v1_request_hash":old_hash})`. The same operation key
with another body/version returns existing `composer_operation_conflict` 409;
there is no automatic retry under another lifecycle after ambiguous admission.

Define one closed `ComposerOperationSnapshotV2` for POST acknowledgement, GET,
cancel and SSE status/terminal projections. POST always returns HTTP202, including
idempotent replay of a terminal parent; it creates no new segment on replay.
The selected response header is `Composer-Operation-Version: 2`.

| Field | Closed type and bundle rules |
| --- | --- |
| `protocol_version`, `operation_id`, `kind` | Literal 2, canonical parent UUID, existing compose_message/compose_recompose kind. |
| `logical_status` | queued/running/completed/failed; cancelled uses failed plus existing request_cancelled error. |
| `phase` | queued/executing/continuation_queued/reconciling/retry_waiting/settled. Initial queued parent uses queued; running uses one of the four active phases; both terminal statuses require settled. |
| `status_revision` | Nonnegative integer advanced by durable parent transitions, never by a GET or heartbeat. |
| `poll_after_ms` | Existing 100..60000 bound. |
| `cancel_requested` | Boolean from the durable parent; completed cannot be cancelled, failed may report acknowledged cancellation. An unpersisted Stop never changes it. |
| `segment` | Required nonnull for live parents; null on terminal projection. Fields: positive sequence/generation, state queued/running/interrupted, aware observed_at, nullable deadline_at/remaining_ms, Boolean rollover_pending. Queued/interrupted has no running deadline; running has its fixed aware hard deadline and nonnegative remaining_ms. No claim/lease secret. |
| `result`, `error` | Both null while live. Completed has exactly existing MessageWithStateResponse; failed has exactly existing ComposerOperationError. Existing failure_code vocabulary remains, with precise host/protocol detail in its admitted body. |

V2 SSE frame is a closed union with common `schema_version=
"composer-operation-stream.v2"`, session_id, operation_id, per-subscription
sequence, status_revision and `event`. `status` and `terminal` payloads are the
same complete snapshot above, with event matching its live/terminal state.
`progress` payload is the existing public progress projection plus segment
sequence/generation; `heartbeat` has no payload. Heartbeats/progress do not
invent durable revision changes. No checkpoint/tool/native/prompt bytes.

Across streams/GET accept a larger durable revision; equal revision may refresh
derived timing only when identity/phase/generation/deadline remain identical.
An equal-revision semantic disagreement is a protocol error. Within one running
generation, time cannot extend its fixed deadline. New generation/segment with
a larger revision replaces segment timers; stale callbacks cannot detach the
logical request. Stream sequence only orders frames within its subscription.

| Request/client condition | Response/side effect |
| --- | --- |
| Missing header or 1, v1 record/admission | Existing exact v1 response and deadlines; echo 1. |
| Header 2, v2 enabled new admission | Atomic v2 admission and snapshot ACK; echo 2. |
| Header 2, v2 admission disabled | 409 detail.error_type=composer_continuation_disabled; no row/quota effect. Keep user draft. No fallback POST. |
| Unsupported header value | 406 detail.error_type=composer_operation_version_unsupported; no mutation. |
| Header 2 GET/SSE/cancel for existing v1 row | Select/echo 1 and return the existing v1 contract, so the new reader dispatches to its v1 decoder. Do not convert that job. |
| Header 1/missing GET or SSE for any v2 row | 409 detail.error_type=composer_operation_version_required with required_version=2 and operation_id; before opening SSE, no mutation. |
| Header 1/missing cancel for v2 row | Authenticate, persist Stop through parent authority, then return a v1-compatible snapshot acknowledgement (HTTP202 live/200 terminal), echo 1. For live ACK use cancel transaction time in deadline_at and **deadline_remaining_ms=0** strictly as a cancellation projection. The old closed v1 decoder requires this exact field name; v2 alone uses segment.remaining_ms. Follow-up GET/SSE still returns the version-required 409. |
| Wrong principal/session | Existing authenticated 403/404 behavior before exposing version/operation facts; Cache-Control no-store. |

The old observer ignores the cancellation response deadline
(`composerOperationObserver.ts:127–140`); zero alone does not detach it. Its
subsequent version-required **409** is a definitive refusal under
`client.ts:isDefinitiveComposerRefusal` and ends observation after Stop has
been persisted. Keep pending custody for a compatible reader to settle from
the actual terminal state; never pretend cancellation ACK itself is terminal.
Test that exact old-client sequence rather than expecting a zero timer to fire.
New readers can read v1 records only in supported stores; no implicit migration.
Revoked/auth-switched reconnects cannot observe another principal.

Implement Stop/Revert authority transitions described above at the real SQL
admission boundary. Do not block human Stop behind advisor/provider waits.

Tests: extend `test_composer_operation_dtos.py`,
`test_composer_operation_error_parity.py`,
`tests/unit/web/sessions/routes/composer/test_operation_outcomes.py`,
`test_operation_receipts.py` and PostgreSQL receipt/fencing modules. Test
version/key conflicts, v1 cancel reaching v2 before detach, exact status/error
projection, auth changes, Stop/yield/claim/final/Revert races and Revert failure
after cancellation. Marker controls detect private leakage in GET/SSE/errors.

**Exit:** truthful version semantics, direct controls serialized through SQL,
and no deadline overloading. Every old/new client-server combination has an
explicit supported/refused outcome.

### C8 — Observer custody and quiet continuous-thinking UX

Modify `frontend/src/types/composerOperations.ts`, `api/client.ts`,
`api/composerOperationDecoder.ts`, `api/composerOperationObserver.ts`,
`stores/composerOperationCustody.ts`, `stores/sessionStore.ts`,
`hooks/useComposer.ts`, `components/chat/ChatPanel.tsx` and
`components/chat/ComposingIndicator.tsx`. Update generated API types through
the existing production schema-generation path after services are authorized;
do not start a service now to generate them. Also modify `web/app.py`'s existing
`/api/system/status` response and `frontend/src/types/index.ts:SystemStatus` to
expose `composer_operation_admission_version: 1|2` from the server feature gate.
Missing field defaults to v1 in new browsers. It selects only a new immutable
submission; persisted custody retains its admitted/requested version across
reconnect. Use the existing `npm run generate-types` only against the later
owned localhost:8000 service expected by that script, not an unrelated service.

Persist selected protocol version with immutable submission custody. Decode
v1/v2 separately. Accept matching identity/auth generation and increasing
status revisions; ignore stale frames and callbacks. Segment time replaces
only a newer segment's timer. Expiry triggers GET/reconnect, never aborts
logical observation or resubmits POST. Preserve ambiguous-submission and Stop
intent; refresh resumes the same durable operation. New message keeps current
busy/draft behavior. Observer detach alone does not cancel authoring.

On first newly observed successor start, show “I'm still working on this” in
the existing activity/live region for 5,000ms, keyed by operation+segment.
No transcript row/card/focus shift/provider call. Baseline attachment shows no
historical notices; reconnect/refresh cannot repeat the same notice. Clear it
locally on expiry (next event-loop turn after background throttling), Stop or
terminal result. Continue showing actual working/queued/reconciling state;
do not say work is saved unless the checkpoint receipt supports it.

Tests: extend `api/composerOperationDecoder.test.ts`,
`composerOperationObserver.test.ts`, `.deadlines.test.ts`, `.recovery.test.ts`,
`stores/composerOperationCustody.test.ts`,
`stores/sessionStore.composerOperations.test.ts` and
`components/chat/ComposingIndicator.test.tsx`. Fake timers cover old deadline,
new segment, stale generation/status, SSE idle/lifetime fallback, reload,
transport loss, pending Stop, auth switch and five-second local clearing.
Existing tutorial integration tests must use this same observer path.

**Exit:** uninterrupted one-request experience, truthful activity and effective
Stop across reconnect; no new confirmation/Continue workflow.

### S7 — Selected .7 semantic integration after Continuation

The existing .7 owner remains the source custodian; do not wake or replace it.
At actual integration re-read its immutable committed input and current staged
disposition. Planning input reports shared-auth commit
`92901baf2b9930bcc238561f41068b17e5c4591e` and three staged SMK10 paths, complete
source tree `4d03666cf67991aadd123113d588ebc120351ac1`, on its separate fixed
base. These are attributed reports, not a newly verified merge grant.

Select only shared-auth protocol classification and source-option semantic
no-op/strict serialization once available through an orderly committed handoff.
SMK10 paths: `composer/tools/sources.py`,
`tests/unit/web/composer/test_patch_source_options_noop.py` and
`test_source_rejection_contracts.py`. Shared-auth's verified seven-path commit
changes `src/elspeth/web/auth/middleware.py`,
`frontend/src/api/shareableReviews.ts`, `frontend/src/api/shareableReviews.test.ts`,
`frontend/src/components/shared/SharedInspectView.tsx`,
`frontend/src/components/shared/SharedInspectView.test.tsx`,
`tests/integration/web/test_shareable_reviews_routes.py` and
`tests/unit/web/auth/test_middleware.py`. This list was read from the actual
commit in its existing source store during planning, without import. Inspect
the current committed handoff again rather than copy/replay whole files.
Preserve current native-thinking,
Stop, evidence and continuation ownership. No whole-file replay, old packet
replay or restored legacy serialization fallback. Real malformed owned state
continues to raise; historical graphs may need reinspection/reconstruction,
without promising every graph is reconstructable.

**Checks:** repeat its affected auth/shared-route/source-option tests on the
combined production tree, including numeric type distinctions, semantic no-op
preserving review/progress and TypeError/ValueError propagation. Rebind any
inherited red gates rather than relabel old results green. No import of the
other sixteen already represented changes.

**Exit:** two complementary fixes represented semantically on Composer+ with
current focused evidence; staged/uncommitted owner work never silently imported.

## Verification commands and staged acceptance

Commands below are future examples, run from the then-owned worktree. Do not
install or rebuild an environment automatically. Before tests, verify both
`elspeth.__file__` and `elspeth_lints.__file__` resolve inside that tree using an
already admitted interpreter, with `PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"`.
No linked main venv mutation. Keep `TMPDIR`, caches and logs inside the project.

After execution is instructed and P0 admits environment/resources, initialize
these task-specific variables **from the intended worktree**, not a shared
checkout. The interpreter must already exist; a missing executable stops setup.
The code block is a future command recipe, not a script created or run here.

```bash
export COMPOSER_WORKTREE="$(git rev-parse --show-toplevel)"
export COMPOSER_PYTHON="$COMPOSER_WORKTREE/.venv/bin/python"
export COMPOSER_LANE="$COMPOSER_WORKTREE/.claude/lanes/composer-continuation-execution"
test -x "$COMPOSER_PYTHON" || exit 1
mkdir -p "$COMPOSER_LANE/tmp"
export TMPDIR="$COMPOSER_LANE/tmp"
export PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src"

run_check() (
    cd "$COMPOSER_WORKTREE" || exit 1
    COMPOSER_CHECK_LABEL="$1"
    shift
    COMPOSER_CHECK_DIR="$(mktemp -d "$COMPOSER_LANE/$COMPOSER_CHECK_LABEL.XXXXXX")" || exit 1
    if "$@" > "$COMPOSER_CHECK_DIR/output.log" 2>&1; then
        COMPOSER_CHECK_EXIT=0
    else
        COMPOSER_CHECK_EXIT=$?
    fi
    printf '%s\n' "$COMPOSER_CHECK_EXIT" > "$COMPOSER_CHECK_DIR/exit-code"
    printf 'exit=%s log=%s\n' "$COMPOSER_CHECK_EXIT" "$COMPOSER_CHECK_DIR/output.log"
    exit "$COMPOSER_CHECK_EXIT"
)
run_py() {
    COMPOSER_PACKET_LABEL="$1"
    shift
    run_check "$COMPOSER_PACKET_LABEL" "$COMPOSER_PYTHON" -m pytest -n 0 "$@"
}
```

Before the first check run this actual provenance check, with exit0 required:

```bash
run_check provenance "$COMPOSER_PYTHON" -c 'import os; from pathlib import Path; import elspeth; import elspeth_lints; root = Path(os.environ["COMPOSER_WORKTREE"]).resolve(); modules = (elspeth, elspeth_lints); paths = [Path(module.__file__).resolve() for module in modules]; print(*paths, sep="\n"); assert all(root in path.parents for path in paths), "package import escaped the worktree"'
```

No uv run, pip or package install is part of this recipe. Read each native
exit file and full log; RED must be the intended
assertion failure (normally exit1), GREEN must exit0. A collection exit2 or
missing prerequisite does not satisfy either oracle. Repeating a command gets
a new project-local log directory and preserves its preceding evidence.

These are executable minimum packets, with each unit's named counterexamples
added in the indicated modules before its RED invocation. Use label `*-red`
before implementation and `*-green` after; table labels illustrate GREEN.
New continuation/attachment test files do not exist until their work unit.

| Unit | Exact focused command using the helper |
| --- | --- |
| H1 backend | `run_py h1-green tests/unit/web/composer/test_rootless_no_tool_recovery.py tests/unit/web/composer/test_no_tool_finalize_disclosure.py tests/unit/web/composer/test_request_intent_policy.py` |
| H1 activity | `run_check h1-ui npm --prefix "$COMPOSER_WORKTREE/src/elspeth/web/frontend" test -- src/components/chat/ComposingIndicator.test.tsx` |
| H2 | `run_py h2-green tests/unit/web/composer/test_service.py tests/unit/web/composer/test_pipeline_planner.py tests/unit/web/composer/test_provider_context_planner_integration.py` |
| H3a | `run_py h3a-green tests/unit/web/composer/test_advisor_mandatory_evidence_budget.py tests/unit/web/composer/test_advisor_graph_visibility.py tests/unit/web/composer/test_advisor_request.py tests/unit/web/composer/test_advisor_terminal_publication.py tests/unit/web/composer/test_advisor_checkpoint.py tests/unit/web/composer/test_turn_effects.py` |
| H3b | `run_py h3b-green tests/unit/web/composer/test_prompt_guard_producer_parts.py tests/unit/web/composer/test_prompt_guard_clients.py tests/unit/web/composer/test_prompt_guard_advisor_projection.py tests/unit/web/composer/test_provider_context.py` |
| H3c | `run_py h3c-green tests/unit/web/composer/test_attachment_admission.py tests/unit/web/composer/test_tools_blobs.py tests/unit/web/composer/test_blob_response_contracts.py` |
| C1a | `run_py c1a-green tests/unit/web/composer/test_continuation_frames.py tests/unit/web/composer/test_continuation_codec.py` |
| C2 | `run_py c2-green tests/unit/web/sessions/test_continuation_repository.py tests/unit/web/sessions/test_continuation_schema.py tests/unit/web/sessions/test_schema.py` |
| C3 | `run_py c3-green tests/unit/web/coordination/test_composer_operation_authority.py tests/unit/web/coordination/test_session_mutation_facets.py tests/unit/web/sessions/test_composer_operation_lifecycle.py` |
| C3 PostgreSQL | `run_py c3-postgres -m testcontainer tests/testcontainer/web/test_composer_continuation_postgres.py tests/testcontainer/web/test_session_mutation_fencing_postgres.py` |
| C1b | `run_py c1b-green tests/unit/web/sessions/test_continuation_cold_restore.py` plus the C3 PostgreSQL packet after adding its cold-restore cases. |
| C4a | `run_py c4a-green tests/unit/web/sessions/test_composer_async_worker.py tests/unit/web/sessions/test_composer_continuation_recovery.py tests/unit/web/sessions/test_run_composer_turn.py` |
| C5 | `run_py c5-green tests/unit/web/composer/test_continuation_action_admission.py tests/unit/web/composer/test_provider_quota.py tests/unit/web/composer/test_capture_llm_completion_boundary.py tests/unit/web/sessions/test_composer_operation_settings.py` |
| C4b | `run_py c4b-green tests/unit/web/sessions/test_composer_continuation_recovery.py tests/unit/web/composer/test_provider_quota_end_to_end.py` plus the C3 PostgreSQL packet with actual accounting cases. |
| C6 | `run_py c6-green tests/unit/web/composer/test_continuation_control.py tests/unit/web/composer/test_continuation_progress.py tests/unit/web/composer/test_advisor_output.py tests/unit/web/composer/test_wire_fidelity_matrix.py tests/unit/web/composer/test_advisor_terminal_publication.py` |
| C7 | `run_py c7-green tests/unit/web/sessions/test_composer_operation_dtos.py tests/unit/web/sessions/test_composer_operation_error_parity.py tests/unit/web/sessions/routes/composer/test_operation_outcomes.py tests/unit/web/sessions/test_operation_receipts.py` |
| C8 | `run_check c8-ui npm --prefix "$COMPOSER_WORKTREE/src/elspeth/web/frontend" test -- src/api/composerOperationObserver.test.ts src/api/composerOperationObserver.deadlines.test.ts src/api/composerOperationObserver.recovery.test.ts src/api/composerOperationDecoder.test.ts src/stores/composerOperationCustody.test.ts src/stores/sessionStore.composerOperations.test.ts src/components/chat/ComposingIndicator.test.tsx` |
| S7 backend | `run_py s7-green tests/unit/web/auth/test_middleware.py tests/integration/web/test_shareable_reviews_routes.py tests/unit/web/composer/test_patch_source_options_noop.py tests/unit/web/composer/test_source_rejection_contracts.py` |
| S7 browser logic | `run_check s7-ui npm --prefix "$COMPOSER_WORKTREE/src/elspeth/web/frontend" test -- src/api/shareableReviews.test.ts src/components/shared/SharedInspectView.test.tsx src/hooks/useSharedRoute.test.ts` |

Each affected Python unit additionally runs
`run_check contracts "$COMPOSER_PYTHON" -m scripts.check_contracts` and
`run_py attributes tests/unit/web/test_sessions_composer_attribute_contracts.py`
when its scanned inputs change, and Ruff check/format on its actual owned
pathspecs before commit. Wire/tool schema and plugin source binding gates follow
the affected scanned paths in CONTRIBUTING. Regenerate approved declarations
with canonical tools; never hand-pin counts or suppressions. Do not run these
project gates during planning. At C8/S7 also run
`run_check frontend-types npm --prefix "$COMPOSER_WORKTREE/src/elspeth/web/frontend" run typecheck`.

At the combined integration checkpoint, obtain fresh shared-host/CI capacity
and exclusive testing-checkout custody. Runtime/persistence/locks justify the
default full suite and serial PostgreSQL selection, in addition to relevant
contracts/lints/Ruff/mypy. Use the canonical script's frozen-tree summaries:

```bash
cd "$COMPOSER_WORKTREE" && scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,lints,pytest,testcontainer --root "$PWD" --log-dir "$COMPOSER_LANE/integration"
```

Read all stage exit codes and frozen-tree status. Default pytest excludes
PostgreSQL; `testcontainer` must execute serially (`-n 0`) with Docker.
Classify named flaky failures with focused serial/base counterevidence; no
green claim from piped/tail output. Keyless findings are a before/after corpus
comparison; they are not operator signature clearance. Stage a signing bundle
only through the approved key-free workflow if required; operator holds key.

| Acceptance gate | Required observable evidence |
| --- | --- |
| Local lifecycle | Same operation and original user row across multiple segments; exact next input/tool result pairing and final result versus uninterrupted control; provider authors each graph transition. |
| Crash/SQL contention | Atomic successor, revoked-generation refusal, head/cursor CAS, Stop/Revert ordering, unknown-effect reconciliation and correct cumulative attempt/cost/admission on SQLite and PostgreSQL. |
| Time/progress | Long valid action fits; fresh-fit refusal cannot spin; cooperative fairness; timing input unchanged after guard; real discovery advances frontier; cycles/advice-only bookkeeping does not. |
| Private storage | Full exact native replay where supported; corrupt/missing/incompatible state refused without mutation; no secret/private marker in public transport; actual bytes/retention and archive ownership measured. |
| Client compatibility | Strict v1/v2 negotiation, old Stop, stale frame/timer protection, same-request reconnect, local five-second notice and auth isolation. |
| Deployed Continuation acceptance | Ordinary production authoring request spans several actual segments and a supported restart/reconnect; inspect saved graph, effect/accounting, review coverage and publication—not only successful HTTP or CLEAN. Tutorial uses identical backend. No raw thinking exposed. |
| Combined B/C Gate1 | Complete approved B1–B3 and C1–C6 ordinary deployed graph assemblies with exact private case/expected-state binding and actual saved configuration evidence. No private graph execution or interpretation approval inferred. One-shot remains suspended. |
| Eligible Gate2 | Repository examples selected by then-current release criteria, excluding external-service/endurance cases, under fresh execution/resource authority. Private assemblies remain zero-execution. |
| Delivery | Protected independent review, ordinary hooks, required CI and main read-back on the final combined head; release owner coordinates primary/service custody. Composer+ PR is engine-resume trigger, not engine import. |

## Deployment, rollback and actual blockers

Deploy compatible readers/worker/browser with new v2 admission disabled. Test
v1 semantics and explicit v2 refusal first. Enable v2 only on a supported store
after all lifecycle and required evidence checks; measure provider/guard/tool/
SQL settlement tails and checkpoint size to qualify the actual action bounds,
reserve and installation storage. No topology or paid-provider capacity was
measured here. Longer provider calls may require supported internal streaming;
qualify it if the selected transport needs it. Streaming is not a substitute
for complete response retention and never executes partial tools.

Rollback disables **new** v2 admission while compatible readers drain/settle
existing requests. Keep queued/reconciling checkpoints and cancellation state.
An old binary is expected to refuse the new epoch; do not downgrade that store
or erase continuation data. A new-store rollout retains the prior store and
appropriate compatible access. An approved offline upgrade, if later selected,
needs its own compatible-binary recovery/drain plan. No schema downgrade or
external-effect undo is promised.

Known execution/rollout dependencies, distinct from plan completeness:

- Conditional early-start readiness is not met on the observed facts above;
  implementation is held. Moving first-release repairs require a source refresh,
  and the actual first PR identity/head must be independently checked when it exists.
- Existing-store strategy: current preserve/refuse cannot provide in-place
  continuation. Operator new-store selection or separately approved upgrader
  is required before enabling on that installation. No data action is granted.
- Full mutable-state and effect-owner completeness must be proved in C1/C3;
  this plan does not label unsupported handlers restart-safe in advance.
- Independent action/settlement enforcement and target reserve/storage values
  require measurements after resource admission. No live value is guessed.
- Required semantic review/guard coverage and terminal bypass closure are
  launch dependencies. Continuation cannot repair evidence already omitted.
- Selected .7 SMK10 input remains reported staged; obtain its actual committed
  handoff and recheck shared-auth source before integration.

No implementation, tests, imports, service/provider/browser/graph execution,
capacity reservation, migration or release acceptance occurred in preparing
this document. Branch-local documentation is not a qualified product candidate.
Independent review and its disposition are retained in
`docs/design/composer-continuation-review.md`.
