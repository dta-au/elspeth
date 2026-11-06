# Composer Continuation executable-plan review

**Latest executable-plan verdict: GO — see the final assembled review below.**

The original interim verdict was **NO-GO** while the implementation packet was
incomplete. The earlier findings and bounded actual-caller reviews remain below
as review history. The final GO concerns the assembled implementation plan;
it does not qualify implemented code, integration tests or deployment.

This is the independent Astra review of the executable task decomposition,
not a replacement for the earlier design review. Review scope is document and
immutable source reads only. No implementation, project import, test, hook,
staging, commit, provider call, service, migration or deployment ran. The existing
.9 owner retains actual test/source custody; .10 integration waits for that
owner's actual normal GREEN checkpoint. No test controller or capacity rule is
introduced here.

## Read-time identity

Source inspected with `git show`/`git grep` is commit
`ce4103539fe97f07fc60fb0cd096256c207c8bc5`, tree
`deead153bbabbe1754d3c313235ed349c18274b1`. The planning checkout remains
`d647dd929d2b05be4918f175bc5ed96b18fdd198`. Main, storage and client files changed
during this pass; therefore findings below bind named task/code fragments and
their stated read-time line references, not an assertion that these files are
unchanged. The frames companion had not yet been written when its path was read.

Initial reads returned these SHA-256 values:

| Plan | SHA-256 |
| --- | --- |
| Main | `fae067355038954615d0e7dd78c1e0ebeb73473b25915ceecde5ecb1d784bac4` |
| Storage | `0b686682f02a7c5356fd69afbd30d79a97a9669cbafcdad5e1103024368e7626` |
| Client | `e85db200d1d4b839348d665f6f892b06eb0b37ed1728894534514c2a6841ccb2` |
| Heuristic cleanup | `5423a8fc4091163401c0e2b48495c8c8bd4b37c7524dddbbbf26f68315a18a79` |

A subsequent read at `2026-10-21T03:30:25Z` returned main
`9ab237644ffd2d8eed7b55bebc456f9da0195aa57b14bee17ce081efd31a147b`, storage
`496a2275b7495aabb0dac7d567a6e84f0d66c59ddc19bebcc7a82fd4bbe31aa2`, client
`b9449729a187b7855680187a8c2e321ea3e48f5e8f3544ba385704a5c819d9b1`; heuristic
cleanup was unchanged. These are review observations in this report, not sealed
plan manifests or runtime admission evidence.

Interim close-out read at `2026-10-21T03:39:37Z` returned:

| Plan | SHA-256 |
| --- | --- |
| Main | `743160a5361bde5f4740a94f2086420230cda60061943dd0abf20259afaeec89` |
| Storage | `5dd8dd180c392fc127c901b0bd1239b7e89c747c03d51a49fe1f7c58cdf07241` |
| Client | `b9449729a187b7855680187a8c2e321ea3e48f5e8f3544ba385704a5c819d9b1` |
| Heuristic cleanup | `5423a8fc4091163401c0e2b48495c8c8bd4b37c7524dddbbbf26f68315a18a79` |
| Frames, dated 2026-10-21 | `b43a0e964b9f4ae12b9b60135341f571524afcc6f1fe02d033f3c850ff5ddf3f` |

The frames companion is now present at
`docs/superpowers/plans/2026-10-21-composer-continuation-frames.md`.
Its F1/F2 concrete code and current interface/status were read. It explicitly
retains an unfinished driver/materialization boundary; the displayed
`NotImplementedError` is labelled as an integration target that must not be
inserted into production. F3–F5 were located but not fully reviewed in this pass.
Storage C4a.3/C4b and final recovery integration were still being authored.

## Material findings and smallest closures

### E1 — Cold message restoration must reconstruct producer ownership

Main C1a.3/C1a.4 currently list message/evidence/cache state and a JSON round trip
without the concrete restoration owner. The source contract explicitly says
`PromptGuardTrust` is minted in-process, never decoded from JSON
(`contracts/prompt_guard.py:34`). `detach_prompt_guard_evidence` rejects foreign
markers and validates exact partitions at lines 137–173. A stored enum name,
trusted index, arbitrary reference path or earlier receipt cannot reconstruct
this authority.

This can be closed as an implementation task, without another user design
decision. Retain exact marker-free wire and closed producer descriptors. Invoke
the current owned producer, compare the full generated wire value/shape to the
retained value, then use only the newly minted nominal metadata. A descriptor
selects a producer; it never supplies trust. Concrete producers are
`prompts.build_messages` (504–624, live skill/catalog/state-schema projection),
the fixed recovery and reply instructions in `service.py:1061` and `:1976`, and
`AdvisorCheckpointOwner._call_advisor_with_audit_body` (407–485, live skill,
trigger instructions and owned argument projection). Stored skill text must
not be passed back as trusted `rendered_skill`.

Rebuild untrusted tool-argument partitions through
`json_argument_content_parts`, retaining exact formatting. Native and attachment
projection must use the already selected .10 owner's corrected implementation:
the pinned `provider_thinking.attach_provider_thinking` and
`AdmittedAttachmentResponse.untrusted_content_parts` still window inspection.
Define the exact owner port for current logical/fence/use/context, retained
source descriptors and same-session immutable materialization. It must return
fresh nominal evidence after current custody/policy and full-wire comparison,
or refuse before mutation. Every actual provider composition still receives
fresh validation, including unchanged IDs/context.

Discovery cache restoration also requires a concrete policy: current cache
values contain `AdmittedResponse` (`discovery_cache.py:26–36`), and plugin-schema
admission refuses ordinary dictionaries (`tools/_generation_schema_response.py:
_snapshot`). Rebuild catalog results through the current producer and compare
exact retained content; retain past results as evidence, not clearance.
`RuntimePreflightKey` contains live `SessionOperationContext` and failures contain
live exceptions (`execution/runtime_preflight.py:22–42`). Cold restore must
invalidate those execution-authority cache entries, retain their historical
facts separately, and run a fresh current-fence preflight before use.

### E2 — A resumed tool index does not define a resumable tool batch

Main C1a.3's `for call_index in range(frame.next_tool_index, len(calls))` preserves
neither setup nor effect ownership. Pinned `tool_batch.py` invalidates abandonment
at 1008–1009, checks all call IDs against persisted history at 1055, appends the
assistant message at 1072, and resets accumulated results/counters/maps at
1099–1117 before loop 1120. Re-entry repeats setup, can duplicate history, and can
reject a previously committed prefix as reused provider IDs. The current
`BatchAccumulator` holds only four fields (912–928), not all of these locals.

The smallest concrete extraction needs once-only batch entry, per-call prepared
and reconciliation phases, retained-result application, and P4 publication.
Retain whole-batch admission identity, assistant shelf index, exact batch locals,
closure-captured per-call state/wire facts, and whether each result has already
updated transcript/audit/state. Existing pure/in-memory tool work may rerun only
before its effect owner has committed. Proposal custody (1950), proposals
(2150/2178), Advisor calls (2457), session-aware dispatch (2735), and ordinary
dispatch (3059) need stable effect keys and their real owner receipt/lookup.
P4 must atomically commit its actual mutation/result and next complete snapshot.
Live exception/Task/custody objects are not serializable continuation state.
An explicit owned safe failure/reconciliation state is needed at those edges.

### E3 — V2 proposal authority conflicts with the proposed null parent claim

Pinned `_proposal_composer_binding` requires `row.claim_token is not None`
(`coordination/composer_operation_authority.py:1012`), while storage C2.2's v2
parent CHECK requires it to be null. Both v2 proposal creation paths therefore
fail before useful continuation. The separate `_running_predicates` patch in
C3.10 does not modify this owner. Historical verification at source 1084–1086
also compares a proposal's creator to the mutable latest parent fence/attempt,
which changes on rollover.

Add an explicit v2 current-binding branch using segment, immutable attempt and
live fence, preserving actor/original-user checks. Add a historical branch
resolving the original creator through immutable attempts, never the latest
parent fence. Retain the actual public claim `attempt` in each attempt-history
row because `ComposerOperationBinding` includes it. Tests must create both
proposal kinds in v2, validate segment-1 proposals after segment 2, and reject a
segment-1 fence attempting a new proposal.

### E4 — The admitted full timing policy is not retained

Client C7.0 says the full policy survives cold recovery and flag-off draining.
Current main `ContinuationFrameStack` retains only `policy_hash`. Storage C5.2's
seed passes that hash and initial `actions.session_sql` JSON; it does not retain
the complete segment and role policies. A new process cannot recover the exact
primary/Advisor/title/read/blob/external bounds, especially when the admission
flag constructs the authority with `continuation_policy=None`.

Persist the complete strict `ContinuationAdmissionPolicy` in the root snapshot
or an explicitly typed private chunk, verify its digest on decoding, and use it
for continuation sizing/stage clocks. Retained timing policy does not replace
fresh security policy and actual-use admission. Exercise cold recovery and
draining with new admissions disabled and changed process defaults.

### E5 — Observation-derived rollover flag contradicts equal-revision decoding

Storage `_v2_snapshot_on_connection` derives `rollover_pending` from database
time crossing `cooperative_at`/`deadline_at`, without increasing the semantic
revision. Client C8.2's `semantic()` includes `rollover_pending`; `accept()` throws
when equal revisions differ. An ordinary observation crossing the cooperative
boundary therefore becomes a protocol error.

Treat `rollover_pending` as observation-only beside `observed_at`/`remaining_ms`
in that comparison. Keep sequence, generation, state and fixed deadline bound.
Add a same-revision false-to-true observation test, with unchanged-deadline and
changed-phase negative controls. Persisting a revision for the timer transition
is another design, but is unnecessary for the selected observation contract.

### E6 — Feature-off Stop test expects an absent worker to settle

Client C7.4's feature-off test requires immediate 200, failed/request_cancelled
and terminal replay, but its fixture explicitly starts no worker. Storage's
`request_v2_cancel` writes only the durable Stop marker and deliberately leaves
the v2 worker to reconcile and settle queued work. The actual specified response
is a truthful 202 live snapshot with cancellation recorded.

Assert that marker and live replay first. If the same test must prove terminal
replay, run the real deterministic worker and await actual settlement before
asserting terminal state. Do not fabricate immediate terminal state in the
transport to satisfy the test.

### E7 — PostgreSQL codec width rejects one declared codec

Initial storage C2.1 used `String(32)` for chunk codec while main C1a declared
`composer.attachment-admissions.v1`, a 33-character value. PostgreSQL rejects
the insert even though SQLite does not enforce that width. The author reports
widening it to 64 and adding an actual PostgreSQL round-trip case; closure will
be verified on the frozen packet.

### E8 — Future validation commands still refer to a removed wrapper

The main setup defines ordinary shell variables but no `run_py`; the storage
packet invokes that name throughout and calls it the plan's wrapper. Replace
these with literal ordinary pytest/log/native-exit commands. The main requires
behavioral RED exit 1 after valid collection, while some companion commands
still demand import-error exit 2. Bootstrap absence may be reported as setup
only; the actual behavioral negative control must be specified and measured.

### E9 — Historical prepared receipts permanently block completion

The first C6.2 reducer appends prepared, dispatched and committed receipts but
its whole-request STOP predicate searches every historical receipt for an
unsettled disposition. A completed action therefore retains a historical
prepared receipt that makes STOP impossible. Keep the history, derive the latest
receipt per action key for the unresolved predicate, and enforce monotone owned
transitions. Test prepared/dispatched/committed as settled, dispatched-only as
unsettled, and conflicting terminal receipt/disposition as refusal. A subset
STOP must also leave every structurally open obligation scheduled or explicitly
blocked through the model-owned obligation decision; it cannot end the request
merely by omitting remaining IDs.

### E10 — Proven undispatched provider cancellation needs its actual join

The new C5.5 continuation binding reaches `finish_provider_attempt` and the audit
cohort, but source `ProviderCallCustody._cancel_undispatched` uses the separate
`cancel_undispatched_provider_attempt` owner and then clears its live attempt
(`provider_quota.py:195–216`). That owner writes a real zero-use ledger and audit
event (`quota_authority.py:264` onwards). Forward the binding through this owner
too and atomically retain its verified known-undispatched disposition, remove
the pending settlement ID, and keep the actual receipt. Otherwise cancellation
during quota admission or dispatch-callback refusal leaves a prepared/pending
snapshot even though actual financial settlement completed. Test those actual
paths; an old pending row alone must never be treated as proof of no dispatch.

### E11 — V2 lease adoption loses the existing failure-custody branch

New C4a.2 sets `started=True` after SQL start and calls `SessionOperationLease.adopt`
without the original cancellation observation list or a post-start/adoption
failure branch. Source `composer_async_worker.py:498–555` explicitly owns this
gap with `_adoption_recovery_cancellations`, `started_handoff` and the recovery
handoff. The new code catches only `SessionOperationConflictError` and then
releases the process lock. Specify the concrete v2 durable interruption/recovery
handoff using the same original-outcome and required-work custody; do not call
the v1 terminal path for v2. Exercise cancellation/failure after SQL start and
during lease adoption, ensuring no producer or lease is abandoned and the next
generation observes the truthful durable outcome.

### E12 — Candidate limiting can starve eligible queued requests

C3.4's outer `claim_segment` query applies `LIMIT(limit)` before checking claim
availability, a live exclusive fence, current generation or session archival.
Those conditions appear only in the locked inner query. With limit one, the
earliest already-claimed or otherwise unavailable row can be repeatedly chosen
and skipped, preventing a younger eligible request from ever being considered.
Put the same eligibility predicates into the outer hint query, retaining the
locked recheck, or use bounded pagination that advances past rejected hints.
Test two actual sessions with the oldest temporarily unavailable and the younger
claimable, then make the old request eligible and verify fair selection.

### E13 — Precomputed snapshots cannot acquire their CAS expectation at commit

`prepare_provider_effect_on_connection` builds a complete next snapshot from a
caller-supplied snapshot. `prepare_effect_on_connection` then reads the latest
checkpoint and uses that latest pointer as its expected CAS value.
`append_complete_snapshot` overwrites the supplied payload cursor. Consequently,
a stale caller snapshot can overwrite a newer settlement/frontier update while
passing the CAS. `finish_sql_effect_on_connection(next_snapshot_json=...)` has
the same precomputed-payload pattern.

Carry the caller's selected checkpoint/cursor through these APIs and compare
before ordinal/effect/mutation writes, rolling back on conflict, or derive the
transition directly from the current snapshot inside the owner transaction.
Test an actual settlement advancing the current snapshot before a stale prepare
attempt: the stale attempt must refuse without losing frames, settlement facts
or ordinals; a fresh reload may then succeed.

### E14 — New frame fields and strict frontier semantics need joined fixtures

F1 adds `InitializeFrame.title_response`, but C4a.1's `required_private_chunks`
currently does nothing for `InitializeFrame`. Include its non-null title response
so a cold restore loads the retained value without another provider call.
F2's `reviewed_frontier` fixture leaves root `request` open and adds open `inspect`,
but supplies only `inspect` in `remaining_actionable_ids`. The now-correct C6
exact-open-remainder check rejects this positive fixture before its codec test.
Bind both open IDs, or provide a valid model-owned root status consistent with
the intended scenario. Neither correction is a new product decision.

## Corrections observed during this pass

These observations narrow the findings; they do not certify the assembled
packet or substitute for its future ordinary implementation checks.

- E4: storage now defines a complete strict retained policy and main snapshots
  carry `admission_policy_json`; encode/decode verifies its hash, and segment
  start reads that retained policy. Final driver and flag-off integration remain
  to be checked against the complete packet.
- E5: the client now includes a same-revision rollover observation test. The
  final reducer/test combination remains subject to assembled review.
- E7: `chunks.codec` is now `String(64)`. Actual PostgreSQL test instructions
  remain to be checked when C4b is complete.
- E9: main now derives current action receipts with monotone history checks and
  requires the complete open-obligation remainder. Behavioral regression code
  and the same-transaction actual-owner adapter are still being completed.
- Main C6 and storage must use one stable prepared effect identity across
  receipt phases; the real provider attempt/ledger joins remain separate typed
  facts. A changing phase receipt ID cannot satisfy the reducer's invariant
  owner identity. The coordinator has selected a stable effect key for this
  adapter, with implementation instructions still being authored.
- The heuristic companion H2/H3c specifies the common provider entry and fresh
  complete-original credential inspection on every attachment read. Its H1
  frontend-status row still calls the two files uncommitted, superseded by
  ordinary checkpoint `52543bcc195419561b32412c79f4004e3fa8af62`. Its missing-module
  RED wording also needs alignment with the main behavioral-RED rule.

## What this pass does establish

The packet preserves provider authorship and the tutorial common backend in its
stated design. It explicitly distinguishes logical operation from physical
segment, immutable historical identity from current authority, and exact-key
replay from fresh flag-controlled admission. Its compatibility direction is
coherent: a new store epoch, unchanged refusal of incompatible stores, strict
v1, and compatible v2 readers/workers retained during draining. The identified
problems are concrete implementation/contract gaps inside that direction.

**Interim close-out: NO-GO.** The complete cold driver, producer reconstruction,
effect reconciliation, worker recovery and their joined test packet are not yet
specified in executable form. The coordinator requested this pass close now so
the existing authors can complete those changes. This is not a request for a
new approval chain or product decision. Author reports are not proof of closure.
The same reviewer is standing by to review the frozen assembled packet, recheck
the material corrections, and record the final GO/NO-GO here.

## Actual bounded caller checkpoint — 2026-10-21

**Bounded caller verdict: NO-GO at `9f321eb8da5b5f2214caf163561875626b247954`.**
This is a practical source review of the actual PromptShield caller checkpoint,
not a replacement for the overall Continuation executable-plan NO-GO above.
The two findings below require correction before qualifying this checkpoint.

Reviewed immutable commit `9f321eb8da5b5f2214caf163561875626b247954`, parent
`fcf2d60b94af0f94c20dc19011f31bef6a0e429f`, tree
`5e71fc19be63442aa4623a96294d42e5b39fec0e`. Git's parent-to-commit diff identifies
23 changed paths. All source references in this section bind that commit;
later integration HEAD changes are outside this verdict. Review used Git object
reads and source comparison only: no project imports, tests, hooks, source/index
changes, provider calls or services. The coordinator's reported passing focused
checks are owner evidence, not tests independently executed by this reviewer.

### B1 — Advisor rejects a valid actor-owned environment reference

`advisor_context.py:487–504` now builds the complete graph projection through
`build_composition_content_view`, whose `prompts.py:378` whole-state credential
check uses the supplied `env_ref_names`. All four Advisor fitting call sites
(`advisor_checkpoint.py:365`, `:794`, `:869`, `:1409`) omit that argument, so they
always use the empty default. The owner currently has no actor-scoped secret
inventory input to these calls.

A credential field containing an exact `${NAME}` reference is deliberately
allowed when `NAME` belongs to the caller's inventory
(`contracts/credential_material.py:263–288`;
`web/credential_guard.py:358–402`). The actual compose entry and primary prompt
builder obtain those names from the current actor's secret service
(`service.py:1174–1183`, `:3234–3250`), and the materialization tool does the same
(`tools/_common.py:3823–3833`). Thus the same legitimate current graph can pass
compose/primary validation and then raise `CredentialMaterialRefused` while
constructing EARLY, END or hint input, before Advisor dispatch. The END setup
only catches `AdvisorGraphCoverageUnavailable` at `advisor_checkpoint.py:1377`;
this is a false credential refusal, not the deliberate unresolved-reference
coverage refusal. Structured `{secret_ref, secret_scope}` references follow a
different detector branch and are not the failing example.

Thread the actual actor's current reference-name inventory through all Advisor
construction and retry paths, using the existing service/operation ownership.
Keep reference values unresolved and preserve literal-credential refusal. Add
actual fenced-caller coverage for a listed `${NAME}`, an unlisted reference and
literal credential material; pure renderer tests alone cannot catch this missing
owner-to-caller input.

### B2 — Existing caller and palette tests were not migrated

The new required `content_scope` parameter is absent from unchanged direct
calls, for example `test_prompt_guard_producer_parts.py:92–93`,
`test_prompt_guard_json_boundaries.py:122`,
`test_prompt_guard_reply_context.py:235`,
`test_provider_context_planner_integration.py:53` and
`test_schema_contract_carry_forward.py:174`. Those calls now raise `TypeError`
before their guard, wire or schema assertions. Unchanged private service tests
at `test_service.py:4393–4394` and `:4431` also call `_build_messages` without its
now-required live session authority; `service.py:3228–3232` unconditionally
requires the sessions service and current fence.

Separately, `test_pipeline_planner.py:2387–2408` omits
`get_complete_pipeline_value` from `_DISCOVERY_TEST_ARGUMENTS` while the test at
`:2411–2418` still asserts exact equality with the enlarged planner palette.
Each parameterized case encounters that mismatch. The existing planner JSON
boundary fixture at `test_prompt_guard_json_boundaries.py:109–117` supplies its
interesting graph only through `provider_current_state`, while the new planner
projection reads `current_state` at `pipeline_planner.py:3468–3471`; migrate the
fixture to exercise the actual new graph input rather than dropping its wire
assertion.

Use explicit test-owned scopes for pure renderers and actual fenced sessions
for service-private callers. Update the palette scenario with a real current
reference and meaningful admitted response expectations. Run the affected
existing suites after migration, retaining their guard and disclosure negative
controls. No production fallback, invented session identity or assertion
weakening is needed. These are source-proven incompatible call sites, not a
claim that this reviewer ran the tests or enumerated every affected test.

### Positive checks and bounded limitations

- `content_scope.py:21–37` validates exact owned state/context, COMPOSE kind,
  matching canonical session UUID and the current operation fence before
  deriving the graph hash. The tool-side scope repeats those checks and
  requires the actor (`tools/_common.py:3798–3820`). Neither treats an identity
  hash as detector permission.
- Complete-value materialization rebuilds the current projection and manifest
  on every invocation (`tools/_common.py:3842–3851`), and the registered tool is
  explicitly non-cacheable. The new tests exercise repeated actual configured
  guard requests, audit records and SDK stubs; the caller positive case also
  creates a real user row and a bound publication review. These tests do not
  establish durable recovery or live-provider acceptance.
- The actual Advisor entry points refuse unresolved graph references before
  SDK entry and before any CLEAN/publication binding is accepted. The changed
  tests preserve exact small-graph values and add no-dispatch/no-provider-row
  negative controls for unresolved coverage and unsupported final inspection.
  Removing old optional-summary fitting assertions is consistent with the new
  representation; it does not qualify model-selected Advisor materialization.
- No new public sessionless provider regression was established. Although
  nullable signatures and historical comments remain, the parent already
  required exact COMPOSE authority in `composer_quota_scope`
  (`provider_quota.py:381–385`), and even the planner's legacy branch reaches
  the unchanged `provider_gateway.py:475–476` rejection of absent explicit
  provider custody. Broken mocked/private tests are not evidence of a formerly
  supported real sessionless provider path.

Model-selected Advisor materialization and durable coverage evidence remain
unimplemented, as declared by the owner. The refusal of unseen references is a
safe bounded limitation, not full feature completion. Real END, physical-effect
and cold-driver scenario closure remain outside this checkpoint, and the
overall executable-plan verdict remains **NO-GO**.

## Actual bounded caller repair re-review — 2026-10-21

**GO for the B1/B2 caller repair at immutable tree
`2ef008e5e1d5eaad57116b47cc183904a65f6489`.** No remaining material blocker was
found in this bounded repair. This supersedes the preceding actual-caller
NO-GO at `9f321eb8da5b5f2214caf163561875626b247954`; it does **not** supersede
the overall Continuation executable-plan NO-GO.

The review binds the supplied Git tree, not the moving checkout or a claimed
commit of its contents. Git identifies it as a tree. Compared with `9f321`, the
review covered the two production paths `advisor_checkpoint.py` and `service.py`
and these eight test modules under `tests/unit/web/composer/`:
`test_content_projection_callers.py`, `test_pipeline_planner.py`,
`test_prompt_guard_json_boundaries.py`, `test_prompt_guard_producer_parts.py`,
`test_prompt_guard_reply_context.py`,
`test_provider_context_planner_integration.py`,
`test_schema_contract_carry_forward.py`, and `test_service.py`. Supporting
session, resolver and fixture definitions were read from the same immutable tree.
Engine/checksum changes and other disjoint integration work are outside scope.

**B1 closed.** `service.py:771` passes the existing scoped resolver to the
Advisor owner. Its `_current_env_ref_names` (`advisor_checkpoint.py:267–273`)
loads the actual session record and lists names for `session.user_id`, without
resolving or substituting values. Manual hints, initial EARLY/END construction
and format retries call it after `composition_scope_for_operation` has validated
the current fence. Both branches of `_build_checkpoint_arguments` forward those
names into the existing complete-state credential scan. Missing resolver still
means an empty inventory, and the independent structured-reference contract is
unchanged.

The new tests use a real scoped resolver and user secret store with a real fenced
session. Their positive/negative matrix covers listed `${AZURE_API_KEY}`, an
unknown reference and literal credential material across manual, EARLY, END and
format-retry entry. Value-resolution methods explicitly fail if called; END has
a real user row and publication review. The deletion-after-first-response test
requires two inventory reads, one guard/audit/SDK call and preserved required-work
completion. This directly addresses the missing owner input and stale inventory
on the reconstructed format retry.

**B2 closed.** Pure renderer calls now supply explicit identity fixtures; private
service tests acquire real fenced sessions. Planner boundary fixtures pass the
actual `CompositionState` and assert its `current_state.data` projection and
content hash. Schema carry-forward, exact URL/Unicode/numeric wire, source-path
privacy and authored prompt-part assertions remain. The literal planner palette
and discovery-argument map include the new tool. The map's well-shaped absent
reference exercises its rejection envelope; successful current-reference reads
are exercised separately through the real tool owner in the producer tests.

The large-value test changes preserve the complete saved scalar, exact selected
tool value and untrusted inspection parts. They separately assert that unresolved
Advisor coverage is unavailable and that a selected oversized scalar is still
refused at the final guard before detector/SDK dispatch. The former 1,500-field
fixture now explicitly proves the existing credential traversal-limit refusal
and unchanged graph, with a 500-field complete-inspection positive control. This
does not claim that the previously oversized shape is accepted or that selection
alone supplies Advisor coverage. The fixed scaffolding allowance changes by the
explicit 1,281-byte delta; its exact-ceiling and one-byte-overflow assertions
remain at `test_pipeline_planner.py:1625–1635`. The carried deadline test arms its
clock before the target request coroutine enters `wait_for`, includes delayed
gateway preparation, and retains cancellation, ledger, guard and no-extra-provider
assertions (`:5946–5968`).

This was source/oracle review only. No tests, imports, hooks, provider calls,
services or source/index mutations were performed by the reviewer. The lane's
handoff was read as attributed owner evidence; its reported affected-suite and
frozen structural checks were not independently executed or promoted to combined
release qualification. Normal integration and operator-lint disposition remain
with their existing owners. Model-selected Advisor materialization, durable
coverage evidence, real END/physical-effect/cold-driver scenario closure and the
overall executable-plan decision remain outside this bounded **GO**; the overall
plan remains **NO-GO**.

## Bounded B8/B7.1 and F36–F39 review — 2026-10-21

**NO-GO for these assembled execution paths pending E15–E17 below.** This
bounded review preserves the overall executable-plan NO-GO and the earlier
bounded actual-caller GO. Known unfinished O8/F21/F32 failure integration and
T9 controls are not counted as newly discovered findings. The coordinator has
accepted the three findings for the existing authors to close; their repairs
have not been reviewed here.

Actual owner compatibility was checked using immutable Git tree
`f0844ebe32b061a1fcbea250be6cfe66261bc1a0`, not the moving integration checkout.
The initial and final reads of the three frozen companions had the same
SHA-256 identities:

| Companion under `docs/superpowers/plans/` | Read-time SHA-256 |
| --- | --- |
| `2026-10-21-composer-continuation-batches.md` | `8e3f902a24aa6b143e391708d91aec0c7c7149ec14fa949074fcab5a6e2f208e` |
| `2026-10-21-composer-continuation-frames.md` | `80a9da89cff9711a21eb84805a83158dfa57e7d62f8ac8b0a4d8b6d7b43170f9` |
| `2026-10-21-composer-continuation-driver-tests.md` | `29f3519c101a5837a683c4c647e1301dc32705a986789360a99825df1eb74711` |

Supporting END and storage reads ended at
`dc4f054d37349b3f24572364ddf5622582f19c54d9fe13a795567805279ce97a`
and `5b635b6a893713fe2123b1f81be678f84309e0871d3a7d1cfb2e5f6e42bfda8e`,
respectively. These identify the reviewed text; they are not a plan admission
mechanism. References below use those companions' line numbers.

### E15 — Deferred cancellation can cross preparation into a new business write

In batches B5, `accept_sql_result` records the cancellation objects delivered
while joining actual SQL and then returns its value (`:1539–1553`).
`prepare_owner` adopts the actual prepared head (`:1589–1598`) and returns a
new `ContinuationOwnerCommit` (`:1601–1605`) without delivering those newly
recorded cancellations. The earlier `checkpoint()` at `:1583` cannot deliver a
cancellation that arrives during the later preparation SQL. Proposal callers
then enter their original business writer (`:1703–1713`); interpretation callers
use the same preparation contract. B8's elapsed-time check does not redeliver
an external task cancellation that finish-once has already caught and retained.
This contradicts the stated no-new-dispatch cancellation guarantee at `:1556`.

After projecting and adopting the actual preparation return, call the existing
deferred-cancellation delivery method before returning dispatch authority.
Keep the durable prepared row and exact original cancellation objects. Add a
real held-preparation-SQL test: cancel its waiter, release the actual SQL
producer, require preparation's durable return and complete required-work join,
then assert that no proposal/interpretation business writer ran and that the
original cancellation propagated. This is distinct from cancellation while a
business writer is already issued, whose actual result still must be joined
and projected.

### E16 — Stop cannot close a prepared, uncommitted batch SQL effect

B7.1 correctly proves that Stop cannot erase a call carrying a prepared owner.
The positive reconciliation path is missing. Frames `:10411–10414` excludes
that call from `continuation_batch_stop_ready`; `:10435–10437` excludes it from
`continuation_batch_application_owed`. Consequently `:10482–10489` routes it to
END. END's `prepare_continuation_stop` rejects every remaining `ToolBatchFrame`
at `:1088–1089`, before reaching its actual prepared read/session-SQL refusal
loop (`:1094–1108`). The selected prepared proposal/interpretation/read call
therefore has no successful Stop reconciliation route. Re-entering its ordinary
business writer is not a closure: storage's actual current-authority check
rejects durable cancellation (`:1156–1161`).

Add a batch-specific Stop reconciliation transaction, selected by the current
driver before END: verify the exact CAS, current prepared call/completion/input,
actual non-provider effect row, and absence of a committed result or pending
owner. Under the same session lock, use the existing atomic business-result
receipt contract to establish that the business transaction did not commit;
refuse that exact prepared effect, preserve the completed prefix and audit, and
close its unissued suffix. Do not invent a completed tool outcome or reissue
proposal/interpretation work after Stop. A committed result must instead take
the existing owed application path. Prove both cases with actual SQL cuts,
including a cold owner and unchanged bytes on foreign/stale requests.

### E17 — The owed P4 drain still enters cancellation-forbidden generic writers

Even a Stop at the unissued batch prefix reaches this gap: frames
`:10387–10393` closes the prefix and immediately calls
`finish_continuation_issued_batch`. Batches B6 then sets `p4_pending` and calls
ordinary `execution.checkpoint()` at `:2913`; that selects generic
`commit_transition` (`:1042–1046`), whose storage implementation calls strict
`require_continuation_mutation` (`:4685–4690`). It refuses the already durable
Stop. If P4 preparation already exists, the refresh checkpoint at `:2948` has
the same problem. New P4 effect preparation (`:2922–2924`) also uses strict
`prepare_effect_on_connection` (storage `:2656`), and an actual P4 business
commit remains subject to strict `require_owner_commit` (storage `:2866`).
Thus the B7.1 received-batch Stop positive scenario has no complete literal
path to its claimed terminal result.

Supply a closed P4 owed-work authority covering its preparation checkpoint,
effect preparation, actual persistence and application. It must bind the
SQL-selected received batch, exact completed prefix and actual P4 input, forbid
new handlers/provider calls/counter changes, and retain all current-fence/CAS,
receipt and audit checks. Generic `commit_transition` and generic business
mutation must stay strict. Exercise Stop before P4 preparation, after prepared
P4 but before commit, and after commit before application, with cold recovery
and exactly-once actual rows. This is the concrete missing owner contract, not
a request for a broad cancellation bypass or a new product decision.

### Counterevidence and bounded positive findings

- The proposed old-preparer/current-committer recovery defect is **not** an
  additional finding. Batches B4 `:1369–1412` explicitly replaces storage's
  earlier same-creator restriction with separate immutable preparing-attempt
  and actual committing-attempt coordinates. Its current-context/CAS/input and
  prepared-disposition requirements remain. That amendment must be assembled
  into the final helper; the obsolete restriction cannot be reviewed in
  isolation as the final plan.
- The suspected stale terminal graph-review response is also **not** a
  finding. Although F37 constructs an initial response projection before adding
  the review draft, actual `sessions/service.py:9424–9435` projects the validated
  committed review into the state supplied to `build_response`, and actual
  `sessions/composer_turn.py:460–466` rebuilds the response from that state. The
  preliminary projection supplies the state-identity check, not stale gates.
- B8's literal local clock keeps cumulative stage allowance, pauses the parent
  stage during a different nested stage, shares allowance for same-stage
  nesting, and checks elapsed time after joined SQL. Its controlled joined-SQL
  test preserves the completed producer/projection before surfacing expiry.
  E15 concerns a separate external-cancellation boundary, not a demonstrated
  reset of these clock budgets.
- D6 compares the complete retained original audit call with the cold driver's
  recovered audit call, rather than only usage/token totals. F39 supplies actual
  selected-entry and terminal-CAS refusal controls, retained-title recovery and
  held terminal/successor SQL-return checks. These are meaningful planned
  oracles; their presence is not execution evidence.

This pass performed only source/document reads and this report append. It did
not run project imports, tests, custom clock/codec probes, hooks, provider calls
or services, and did not change source, the index or another owner's gate.
The coordinator's clock/codec probes and running normal gates remain attributed
owner evidence. Final assembled GO/NO-GO remains with this same reviewer after
the accepted closures and known active packets are complete.

## Bounded cancellation, worker and replay re-review — 2026-10-21

**E15 is closed in the reviewed plan text. Overall NO-GO remains pending
assembly and the findings below.** This pass covered frozen B5/E15, D9, W4's
watch change, W8 and T6–T10/A2a against the same immutable actual source tree
`f0844ebe32b061a1fcbea250be6cfe66261bc1a0`. B9/O8/O9 and the newly identified
END prepared-refusal constraint repair were still being assembled; this is
not their final review. The actual combined-source gate is separate and does
not qualify the unimplemented Continuation packet.

Read-time companion hashes were: batches
`cb3dc84180ee5be18a56e2f2d43618918f32455915748eebf22221a7c6fc9a41`,
driver tests `c507befd8961474d6604adab65822b846f2343502e331842b01c61d1a062e470`,
storage `cdb706d32fcdecc964e2ae4b6f2466eb84d5fd807d3a968f7d91fa558df000fe`,
producers `d69128bcd2347e5da34b548f471f90bbc7ad6c1f1078a76e44695a882faace5e`,
and attachment history
`dd66fee4e48e582e8628d1726038f893ce5c4e1d9b90b0d2ae65927b09d4d3a8`.
These identify the concrete review pass, not a claim that actively edited
companion sections were frozen.

### E15 closure and bounded positive findings

Batches `:1604–1618` now performs an actual registered SQL boundary read at
the adopted preparation checkpoint, checks the nominal boundary, delivers
the original deferred cancellations, and checks durable Stop before returning
business authority. D9 (`driver-tests :2176–2318`) holds the actual preparation
SQL return and checks zero writer entries/unchanged business rows, the retained
prepared effect, exact original cancellation objects and the actual worker
recovery receipt. Its corrected snapshot assertion accounts for recovery's
one cursor advancement. This closes the specific missing delivery identified
in E15; it does not turn a prepared effect into a completed result.

W4's watch amendment (`storage :5322–5347`) makes only durable Stop's
`owner.cancel` branch legacy-specific. Immutable actual worker source
`:934–976` confirms that lease loss, terminal observation, shutdown and failed
watch handling are separate retained branches. The actual-reader control at
`storage :6396–6463` observes SQL Stop, proves no task cancellation, then
requires the original shutdown marker and joined recovery. It deliberately
does not claim full driver Stop completion.

W8's schedule comes from actual revoked interrupted-attempt history under the
same session transaction (`storage :1864–1874`), scoped to the same logical
operation and sequence. Its controlled database-clock test (`:6631–6683`)
checks the exact delays, before/at-eligibility claim behavior, immutable history
and a parent older than 300 seconds. No new age cutoff is introduced.

T6–T8 now specify concrete native and attachment reconstruction owners. T7
checks the selected current claim/fence, immutable parent/principal, actual
committed read or settled physical response, complete receipts and original
lineage; fresh reconstruction must equal the complete retained wire. T8
re-reads complete attachment originals under required-work ownership and the
existing provider preparation clock before guard/quota/SDK entry, compares
actual original/version/range data, then rechecks the unchanged selected SQL
authorization. A2a obtains registered reference names from the actual current
session actor without resolving values. Native fields and argument partitions
still pass the existing fresh final provider guard. No historical recipe,
inspection hash or receipt is promoted to reusable clearance. These are
source-consistent plan mechanisms, not executed feature acceptance.

### E18 — T9's suspension oracle expects the wrong parent status

The source-substitution test at producers `:3055–3056` expects parent
`status == "queued"` and `phase == "retry_waiting"`. The actual planned
`suspend_segment_on_connection` (`storage :1896–1902`) requires and preserves
parent status `running`, updating its generation and retry phase. W8's own
positive assertion correctly expects `running` at `:6675–6677`.

Change T9's assertion to the actual running/retry-waiting parent contract and
bind it to the interrupted selected segment/recovery receipt. Preserve zero
additional SDK calls and the no-result assertion. As written, the intended
source-substitution control cannot reach a green final oracle even when the
rejection and recovery work correctly.

### E19 — A fully drained Stop still misses the worker's terminal branch

END `:1362–1365` (also its clock amendment `:1979–1983`) raises
`ComposerOperationCancelledDuringTurn` after its successful Stop preparation.
In immutable actual `sessions/composer_operations.py:449–450`, this is a
`ComposerOperationCancelledBeforeStart` subclass, not a
`ContinuationProtocolError`. W4 `storage :5524–5545` selects terminal failure
only for `ContinuationProtocolError` leaves; its other ordinary errors go to
`_suspend_continuation_owner`. Thus even a fully drained Stop enters another
interrupted generation. The current-cancel priority inside
`_continuation_failure_for` (`:5628–5629`) cannot help because `_settle_failure`
was never selected. This is separate from the accepted B9/P4 drain repairs.

Give successful selected Stop preparation a closed completion signal or
equivalent exact branch, and route it through the existing terminal failure
owner after the real required-work cohort joins. The SQL terminal must still
verify current durable Stop and the fully reconciled frontier. Do not treat an
arbitrary early cancellation or shutdown marker as proof that issued work has
drained. Require an actual-driver test to reach `failed/request_cancelled`
once, without a successor or another SDK call, plus a held-owed-work control
that remains recoverable until its real result is projected.

Both new findings were sent promptly to the coordinator. No project imports,
tests, hooks, SDK calls or product/index edits were executed by this reviewer.
The next overall verdict must review the assembled repairs, not silently
supersede these findings based on author claims.

**E18 repair re-read:** producers companion SHA-256
`0a30514fc08087589f8c9b1d6d021d5dcafa18d0de18681b29042265a51e6191`,
`:3053–3063`, now asserts the actual running/retry-waiting parent, binds its
sequence to the returned `RetiredAttempt`, and requires the selected interrupted
segment's generation to be exactly the retired generation plus one and equal
to the parent's. The existing driver helper verifies the actual recovery
receipt before returning that attempt (`driver-tests :916–922`). **E18 is
closed in the reviewed test plan.** E19 and final assembly remain pending.

## Assembled Stop, failed-provider and original-finding re-review — 2026-10-21

**Overall NO-GO remains pending the explicitly unfinished pre-dispatch
credential-refusal packet.** This is the same reviewer's continued source/doc
review, against immutable actual source tree
`f0844ebe32b061a1fcbea250be6cfe66261bc1a0`. B9, the owed-P4 owner, O8/O9,
E8/W4 and the original findings were reviewed together. The credential packet
was still being authored; it is not silently included in a GO.

Read-time SHA-256 values for this pass are main
`28e71481878f32785bd0f87e1cc98b6628082a78a6846251b870045a183dc8c7`,
storage `1ed965e4481300a62c3e7059bc49fa099533a71761c156eb2008a7db6f813096`,
client `b75b9295f89f0d494ea5655b1520a68e5aef28c8a436fc783db53b41b637431a`,
batches `cb3dc84180ee5be18a56e2f2d43618918f32455915748eebf22221a7c6fc9a41`,
frames `24f379b8d9071b40ca4cb71f7f565dd5860b3a457ba3961a5d125a1be1758039`,
physical outcomes `edc1db1f61c8798812d2bc997dfb817c2d44b34a89b8165210239d9547837c25`,
and END `60fb2982e2c9ee09e842ac25fe10c96f1deded12d491dc934987c869d29d14d1`.
These identify reads of the concrete documents, including the fixes below;
they are not a sealed plan package or executed product evidence.

### E16/E17 closure: prepared Stop and owed P4 have separate exact owners

B9 (`batches :4075–4351`) now validates the actual original response/call,
canonical completion, effect key, input digest, selected index and state. Its
same-transaction refusal requires the current nominal attempt/fence, exact
checkpoint/cursor, durable Stop, immutable preparing history, and absence of
an actual business result. It writes the refusal chunk and receipt together
with the closed batch, preserving the completed prefix. It does not relabel
a committed proposal, interpretation or immutable read as unexecuted.
F32 (`frames :10439–10540`) routes this exact prepared case separately from
retained-result application and ordinary call-ready Stop.

The possible collision with an earlier session's tool-call ID is not an
additional defect: immutable actual `composer/tool_batch.py:346–374` checks
the whole received batch against prior tool, proposal and interpretation IDs
before admitting it. B9's refusal query preserves that existing contract.

Storage's owed-P4 packet (`:8689` onwards at this read) verifies the actual
selected settled prefix and prepared P4 input before granting its private
connection-local scope. The scope is nominal, bound to the exact current SQL
connection/context, and removed in `finally`; ordinary proposal/interpretation
writers retain their cancellation gate. Separate checkpoint, preparation and
actual P4 mutation owners close all three strict-gate failures from E17.
Current generation, principal, lease and fence checks remain in force.

B9.4–B9.6 (`batches :4353–4850`) supplies exact owner refusal/replay controls,
prepared-versus-committed full-driver cuts for proposal, interpretation and
read, and imports the real owner tests into the existing PostgreSQL fixture.
O9 supplies actual zero/one/two-result prefix Stop cases and premature terminal
refusal while real P4 remains owed. These are concrete future test oracles;
none were executed by this reviewer. **E16 and E17 are closed in the plan.**

### E19 closure and two integration corrections found during this pass

E8 (`END :2033–2243`) adds nominal `ComposerContinuationStopReady` with the
actual claim/checkpoint/cursor. W4 (`storage :5525–5535`) recognizes that exact
leaf only after joining the actual cohort. Arbitrary cancellation remains a
suspension. The final terminal transaction requires durable current Stop,
the exact selected locator when supplied, no owed batch/review/owner/financial
work, and agreement between the full SQL effects and selected frontier.
Prepared prescribed reads now have a selected-input proof and a stored refusal
chunk (`END :1074–1151`); the old generic sweep is gone.

Two actionable defects were sent to the coordinator and repaired before the
read-time identity above:

- O9's one-result cut originally watched generic `commit_transition`, but B5
  completes the first read through `commit_owner_application`. Physical outcomes
  `:2424–2461` now observes that actual method's returned snapshot as well, with
  the real transaction forwarded. Its prefix assertion still detects a missed
  cut. No fabricated result is substituted.
- E8 originally forwarded a Stop locator even when the existing failure reducer
  selected a stronger failure. Immutable actual worker `:1029` performs that
  final selection; the amended END `:2083–2089` now forwards the locator only
  for `request_cancelled`. The original signal remains in captured failure
  facts. Its new actual-worker control (`:2093–2134`) groups that signal with
  an actual integrity error and requires the stronger outcome to publish.

**E19 is closed in the reviewed plan.** The existing B9/O9 full-driver terminal
assertions remain necessary implementation tests, rather than evidence of a
runtime pass from this source review.

### Original findings reconciled against the assembled tasks

| Finding | Concrete current closure |
| --- | --- |
| E1 | T6–T10/A2a reconstruct native, attachment and trusted producer evidence from actual owners, compare complete retained wire, and perform fresh current-use validation. The prior bounded producer review remains applicable. |
| E2 | B1–B9 and F32 define once-only admission, prepared/result/application phases, retained batch locals and actual P4 publication. O8 retains and applies the complete actual failed manual invocation before finishing its prefix. |
| E3 | Storage C3.11 `:4463–4545` derives live proposal binding from the current segment/attempt and historical binding from immutable attempts, with actual ingress/actor checks; `:4552–4620` exercises both real writers and old-fence refusal. |
| E4 | Complete strict policy is retained and hash-checked; storage C3.3/C3.4 reads it for admission/start, including flag-off draining, rather than replacing it with current defaults. |
| E5 | Client `:1350–1355` excludes observation-only rollover/time fields from same-revision semantic comparison; `:1326–1333` covers rollover acceptance and phase/deadline refusal. |
| E6 | Client `:799–837` now requires the no-worker 202 queued cancellation marker and live replay; terminal settlement remains a real-worker obligation. |
| E7 | Storage codec is `String(64)` at `:456`; C4b.1 stores and reads the actual attachment codec on SQLite and PostgreSQL, with a deliberate narrow-column negative control. |
| E8 | The affected main/storage/client/heuristic packets use ordinary commands and native exits. The obsolete wrapper search matches this historical report, not those current task documents; bootstrap errors are explicitly separated from behavioral RED. |
| E9 | Main `:1640–1660` derives latest monotone receipts using stable owner identity; `:1706–1724` requires the exact open remainder. `:1863–1916` tests completed history, unsettled history, conflicting terminal/owner facts and omitted open work. |
| E10 | Storage `:4164–4278` forwards the actual continuation binding through undispatched cancellation and joins the real zero-use ledger/audit receipt in the same transaction. Physical outcomes and C5.9 retain the distinction between explicit no-dispatch proof and unknown physical use. |
| E11 | W0/W3/W5 preserve adoption/cancellation/lease/task custody, exact released-owner recovery and process-lock retention when actual completion is unknown; their normal real-owner controls cover the start/adoption seam. |
| E12 | Storage `:1431–1475` includes availability, generation, live-fence and archival predicates before the outer limit and repeats them under lock; C4b.1 `:6839–6854` proves younger eligibility then the older claim's return. |
| E13 | Selected checkpoint/cursor is supplied and checked before precomputed effect writes. C4b.1's actual late-settlement test preserves the new snapshot, effects and ordinals on stale preparation. The additional test-tail correction below closes its assembled oracle mismatch. |
| E14 | Storage `:4655–4659` loads the initializer's retained bootstrap/title response. Frames `:823` includes both open root and child in the reviewed frontier remainder. |

The additional E13 regression mismatch was concrete: its positive tail tried
to prepare another provider effect immediately after undispatched cancellation,
while the selected frame still owned the unapplied prior effect. Production
`prepare_provider_effect_on_connection` correctly refuses that replacement
(`storage :3396–3397`). The repaired test (`:6893–6903`) requires that exact
fresh-head refusal and again compares snapshot/effect/ordinal rows. It does
not clear locators or weaken the production guard. The original successful
preparation remains its positive control; actual result application and later
eligible effects are exercised by the separate driver packet. The same test
remains in the PostgreSQL module.

### Remaining explicit boundary

The coordinator identified a final unfinished no-dispatch case:
`CredentialMaterialRefused` raised by the gateway's exact credential check
before guard/quota/SDK. It must retain an explicit safe no-dispatch outcome
through actual owned custody, preserve zero physical-attempt/audit claims, and
terminate finitely on cold recovery. The same class raised after SDK entry must
remain a failed physical outcome. This known active seam has not been approved
from an author promise. Final GO/NO-GO requires reading its completed literal
packet and confirming the assembled document identity afterwards.

This pass ran only document/immutable-source reads and updated this report.
No imports, tests, custom probes, hooks, provider calls, source/index edits,
commit, service or deployment ran. Actual combined-source qualification
failures remain separate from executable-plan review.

## Final assembled executable-plan verdict — 2026-10-21

**GO to implement the reviewed assembled plan. No material executable-plan
blocker remains from this review.** This supersedes the overall interim
NO-GOs above after their concrete repairs; it leaves the separate bounded
actual-caller verdicts and their source identities intact. The original design
review is unchanged.

The existing authors' edit-complete notices cover the root, frames/batch,
producer and final core packets. The final core notice was read from its
existing completion note. Final normal file reads returned these identities:

| Companion under `docs/superpowers/plans/` | SHA-256 |
| --- | --- |
| `2026-10-20-composer-continuation.md` | `482d166c49539fe62ebed52991c06de0621fc0b15f98c9b3730670d573283381` |
| `2026-10-20-composer-continuation-storage.md` | `1ed965e4481300a62c3e7059bc49fa099533a71761c156eb2008a7db6f813096` |
| `2026-10-20-composer-continuation-client.md` | `b75b9295f89f0d494ea5655b1520a68e5aef28c8a436fc783db53b41b637431a` |
| `2026-10-20-composer-heuristic-cleanup.md` | `f63a70d1c62bdc8811f1f1865e32c07112edf71f9096e9a9f469b31fd640130d` |
| `2026-10-21-composer-continuation-frames.md` | `24f379b8d9071b40ca4cb71f7f565dd5860b3a457ba3961a5d125a1be1758039` |
| `2026-10-21-composer-continuation-batches.md` | `cb3dc84180ee5be18a56e2f2d43618918f32455915748eebf22221a7c6fc9a41` |
| `2026-10-21-composer-continuation-physical-outcomes.md` | `4dd070bc97c82982eeaa946c53f88190959a329ddf22d27b6429fc1738e032ec` |
| `2026-10-21-composer-continuation-end.md` | `81549e556af69e5118f78c934ce2e3868e85542a1bd01faec84b4b9458745651` |
| `2026-10-21-composer-continuation-producers.md` | `0a30514fc08087589f8c9b1d6d021d5dcafa18d0de18681b29042265a51e6191` |
| `2026-10-21-composer-continuation-attachment-history.md` | `dd66fee4e48e582e8628d1726038f893ce5c4e1d9b90b0d2ae65927b09d4d3a8` |
| `2026-10-21-composer-continuation-driver-tests.md` | `c507befd8961474d6604adab65822b846f2343502e331842b01c61d1a062e470` |

These are this report's read-time bindings, not a plan admission mechanism or
an instruction to preserve immutable planning documents. Subsequent ordinary
status edits do not become implementation evidence. Actual source references
in this final pass remain bound to immutable tree
`f0844ebe32b061a1fcbea250be6cfe66261bc1a0`.

### Final O12 closure

Physical outcomes O12 (`:2915–3141` at the final read) now supplies the exact
remaining credential-refusal code and tests. Immutable actual gateway
`provider_gateway.py:475` requires nominal custody before the physical
boundary. O12 wraps only its complete-Advisor and generic credential checks,
after exact prepared-input comparison and before guard, quota or SDK entry.
It does not broadly catch the same exception from response admission.

The strict retained descriptor matches the actual candidate-free
`CredentialMaterialRefused.to_payload()` contract, including its detector
version. Its detail and surface are closed. The actual custody refuses to
classify any admitted/dispatched call as this no-dispatch outcome and retains
the safe receipt through the existing single owned V transaction. No physical
attempt, physical audit call or zero-use ledger is fabricated. Family 1 keeps
the identical original for its actual manual/automatic caller; other families
raise the finite retained-failure protocol. Cold reading uses current selected
authority and the same safe error projection. O8's real manual invocation and
owed P4 path remain responsible for the manual prefix. Post-SDK credential
failure remains a failed physical outcome with its original accounting.

The final test packet controls the actual detector, covers all physical
families, checks original exception identity, actual absence of quota/ledger
rows, checksum-verified retained chunks and fresh-generation outcome reading.
The two test mismatches found on its first read are corrected: the gateway
module import is explicit, and a new prepare at the still-unconsumed selected
effect requires the actual `AuditIntegrityError` with whole-table rollback.
The observational spy wraps the actual class method, which is compatible with
slotted custody, and forwards it unchanged. These are executable future
oracles, not reviewer-run results.

### Scope of the GO

The assembled tasks now connect the complete cold driver, producer
reconstruction, once-only batch phases, current-authorized SQL owners, physical
accounting, bounded clocks, Stop/P4 drain, END publication and v1/v2 client
contracts. They preserve provider-authored graph transitions, the shared
tutorial backend, fresh actual-use validation, historical receipts without
reusable clearance, flag-off draining and store preserve/refuse behavior.
The required future checks use the repository's ordinary runners and real
SQLite/PostgreSQL/worker boundaries with explicit behavioral controls.

This GO is a judgment that the implementation plan is sufficiently concrete
and internally consistent to execute. It is not a claim that its proposed
code imports, typechecks, passes tests, has shipped or is safe to activate.
The actual combined-source gate and its reported failures remain separate and
must be resolved before their corresponding qualification claim. No project
imports, tests, custom probes, hooks, SDK calls, source/index changes, commit,
service, migration or deployment ran in this review. Only this report changed.
The same Astra reviewer is now standing by.

## Narrow post-review documentation delta — 2026-10-21

**Executable-plan GO unchanged.** This follow-up inspected only the reported
status, fixture-comment and end-of-file formatting changes; it did not reopen
the broader review or execute any implementation/checks.

Main now records the assembled plan GO separately from the actual combined
source gate. Its updated status attributes a completed PostgreSQL native exit
0 (1,062 passed, 1 skipped), `frozen=yes`, driver exit 1 and overall
`RESULT=FAIL` to the coordinator's normal gate. The failed default stage remains
visible and prevents combined qualification. This reviewer read the updated
status text, not the gate logs, and makes no additional independent test claim.

The two synthetic SDK fixture credentials in driver tests `:289` and `:291`
now carry the repository-prescribed `# secret-scan: allow-this-line` comments.
Their displayed values remain explicit test-only strings for `sdk.invalid`;
these comments introduce no executable behavior. Normal no-index comparisons
against the staged document bytes also confirmed that the hook's additional
edits to client, heuristic cleanup, attachment history, driver tests and frames
only remove surplus EOF blank lines. The coordinator reported that the first
documentation commit attempt exited 1 without creating a commit. No staging,
hook or commit was performed by this reviewer.

The resulting read-time identities replace only the corresponding entries in
the final table above:

| Companion | SHA-256 |
| --- | --- |
| Main | `1270c899c88bfe521466c896fbed8b9e956e7f04a25302849edddf756e5b6baa` |
| Client | `763d896f57c3c84e695bd7915c0948f782ffd736cf266b036db79bc467eefc1c` |
| Heuristic cleanup | `2a3443ade27d962de8803bf5239fba97afb2b3b67e5a563515e8350cb576390b` |
| Attachment history | `770f57be9136c26be0bedce3ad3c0cbc465c3fb3db38b68387c194b4fd06706e` |
| Driver tests | `e3ff9eff16ad34fd2da48760c314f6035993a9f9408967f22fdd55256f6ea0c3` |
| Frames | `60a7f11f7517fe7597030978bd47ca981120ef87135e8c4887947492419a6a5d` |

The coordinator's reported document syntax/link/text checks remain attributed
document checks, not product qualification. This report is now edit-complete
for the coordinator's normal documentation commit; the reviewer stands by.
