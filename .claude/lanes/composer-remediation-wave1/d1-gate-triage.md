# D1 combined-gate initial source triage

Read-only investigation on 2026-11-09. Active tree: `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`; independently read HEAD: `14fef5e5342ccbd79b4fdd69b276c20f77d66dc1`. Base: `5e192342c3fb025fd9a91244bebe437158e9de8e`.

This is a preliminary source/fixture diagnosis, **not a disposition of the observed failures**. The parent-owned backend process has not supplied its terminal trace. No pytest, scan, lint, build, source/test edit, HEAD change, staging, or index operation was run. Only this lane report was written. No subagents were used. SQLite CLI and Chroma model-cache failures are outside this assignment.

Read AGENTS.md, relevant CONTRIBUTING whole-tree/Composer/test-discipline guidance, the systematic-debugging skill, `d1-final-review.md`, D1 state/ blob/ publication-race/ canonical reports and review evidence, the admission brief and approved implementation plan. The final review explicitly approves post-admission proposal counting, direct known refusal/no-change feedback, strict blob provenance, and deferred destructive effects; it expressly leaves integrated-gate disposition open.

## Initial classification

| Reported case | Source evidence | Current classification pending trace |
| --- | --- | --- |
| persistence: `test_explicit_approval_batch_preflights_proposal_cap_before_creation[11]` | Asserts zero proposal/event rows and zero calls for an eleven-change batch. Approved implementation stages first ten admitted changes, then raises the unchanged cap error. | Concrete stale expectation in test; observed failing assertion still must be matched. |
| persistence: `test_proposal_attempt_cap_rejects_mixed_schema_and_semantic_invalid_calls_before_dispatch` | Nine changed metadata calls followed by schema-invalid metadata and invalid canonical call. Asserts cap error before any handler/row. Approved cap excludes both invalid calls after real admission. | Concrete stale attempt-count contract; exact returned error/result and feedback need terminal trace/probe. |
| persistence: `test_proposal_attempt_cap_includes_approval_required_blob_only_mutation` | Ten metadata changes then `delete_blob` with `{}`. This is a schema-invalid deletion, not an admitted external effect. Asserts cap error and zero rows. | Concrete stale expectation and misleading effect fixture; must split invalid-at-cap versus valid admitted effect. |
| service: `test_explicit_approve_intercepts_update_blob_without_mutating_blob` | Calls legacy one-turn helper with no user-message authority; real new admission computes update provenance before publication. Helper explicitly discards `user_message_id`. | Strong fixture-context hypothesis; possible expected `AuditIntegrityError` for missing user-message provenance, not yet observed. |
| endpoint: both exact unset-endpoint kwargs tests | Exact expected dictionaries omit reasoning hint; existing helper always adds `reasoning_effort="low"` for default bare `gpt-5.5`. Those tested source/tests are unchanged from base. | Concrete pre-D1 source/expectation mismatch; need observed extra-key trace or baseline focused reproduction before assigning these reported failures. |

No production regression has been demonstrated by this read-only investigation. That is a statement about evidence obtained, not a claim that no regression exists.

## Persistence cap tests

Relevant source is `tests/unit/web/composer/test_compose_loop_persistence.py:1306-1511`. None of this test file differs between BASE and HEAD (`git diff BASE..HEAD -- <file>` returned no diff). Its `_capture_tool_batch_rejection` at line 456 catches any `BaseException` and returns it, so test names alone do not establish which failure occurred.

The approved brief says: “pending-proposal cap applies only after semantic admission” and “sequential first-N staging is truthful current architecture.” The state report explains cap+1 pure preview is semantically observed but denied before staged/audited dispatch, with no synthetic invocation/outcome for that denied call. Final combined review repeats these rules and confirms unsuccessful publication does not consume a slot.

Current production boundaries:

- `tool_batch.py:2113-2154`: separate non-writing blob admission, caching a real refusal or exception.
- `tool_batch.py:2155-2232`: ordinary pure-state `execute_tool` preview with complete context, first outcome cached; refusal/equality bypass publication.
- `tool_batch.py:2238-2246`: cap guard after semantic admission and before ordinary proposal publication.
- `tool_batch.py:2334-2335`: counter increments only when publication still exists after the narrow resource-race catch.
- `tool_batch.py:417-453`: cap error preserves the real admitted invocation prefix and the carrying completion.
- `tool_batch.py:385-414`: persisted provider tool-call IDs are refused before further effects.

For the eleven-change test, keeping the error evidence `{observed:11, cap:10, cap_kind:pending_proposals}` is correct; replacing only `expected_proposals=0` is insufficient. Its progress expectation also asserts no progress for the eleven case, and its retry repeats IDs now owned by the first ten durable proposals. A truthful repair must pin exact first-ten proposal/event IDs, no eleventh publication, actual invocation-prefix IDs, unchanged graph state, and the denied call's absent outcome. The existing replay must either assert fail-closed ID reuse with no additional effects or use a fresh session/IDs when testing a new turn's cap. It must not pretend the first ten rows never happened.

For the mixed-invalid case, the call count is eleven but the admitted proposal count is nine. The schema-invalid `set_metadata {}` is not admitted. The canonical `set_pipeline` supplies `source=None` and no `sources`; the public model requires exactly one non-null source branch, so this fixture may be an argument failure before it exercises candidate-level semantic validation. Preserve both failure classes explicitly: retain a real schema-invalid example and use a producer-valid, authored-invalid canonical candidate for a genuine semantic-refusal control. Expected behavior is nine staged proposals, one feedback per actual call, original state/version, no invalid proposal, no pending-proposal cap error. Exact handler/cardinality expectations must follow actual canonical versus ordinary admission, not an unconditional `handler_calls == []`.

For the blob-only case, `BlobIdArgumentsModel` requires a string `blob_id`; `delete_blob {}` cannot demonstrate admitted external-effect counting. Keep this input as an invalid-at-cap feedback control. Add/use a real ready, unbound, unretained session blob with actual authority for the admitted-effect boundary: ten metadata proposals followed by a valid deletion should hit cap before deletion proposal or resource effect, retaining the first ten rows. A companion nine-metadata-plus-valid-blob case proves a real blob-only effect consumes the tenth slot and is deferred. Keep exact bytes/metadata/ledger checks and prohibit destructive handler execution during admission. Do not equate graph equality with no blob effect.

Existing useful comparisons (read, not rerun): `test_freeform_proposal_prevalidation.py::test_proposal_cap_counts_only_admitted_changes` covers all-noop/refusal/argument batches, ten changes plus refusal/noop/missing-blob refusal, cap+1 real change, and inline cap+1 without blob creation. `test_proposal_cap_turn_counts.py` pins preceding-turn identity and real invocation prefixes. Publication-race fix-round controls pin a same-batch refusal followed by ten successful publications, detecting accidental failed-publication slot consumption.

## update_blob service fixture

`test_service.py:1727-1795` seeds a real blob using `_create_session_blob_for_test`; that seed helper does have valid verbatim user-message provenance. The later update test creates a fresh two-response fake and invokes `_run_one_turn_for_test` with session/state only. Prior seed provenance does not authorize provenance for newly generated replacement content.

`ComposerServiceImpl._run_one_turn_for_test` at `service.py:852-905` has `user_message_id=None` and explicitly `del user_message_id`, then calls `_compose_loop` without it. The global adapter in `tests/helpers/composer_lease.py:121-179` supplies a real COMPOSE lease and patches the fake LLM; it does not synthesize user-message authority. Current admission passes the loop's user-message ID, message, model/version/provider/skill/argument hash into `ToolContext` (`tool_batch.py:2120-2139`). `admit_blob_mutation` calls `_blob_creation_provenance` (`tools/blobs.py:1760-1764`) before storage observation. `_blob_creation_provenance` at lines 1212-1242 requires a real user-message ID for generated content; missing values raise `AuditIntegrityError`, not an expected user refusal. The new prepublication path exposes a fixture omission previously deferred until acceptance.

Conditional repair if terminal trace confirms this path: drive this test through real `compose(...)` with a persisted update user message and actual user ID/message ID, as the existing D1 integration test does. Merely adding `user_message_id=` to the legacy helper is ineffective because it discards the argument. Keep the pending update proposal/status/affects/summary assertions, exact unchanged blob row and bytes, no intent/receipt/reservation effects, and add complete proposed update provenance/real admission checks. A narrow test-only correction avoids changing a broadly used production test helper without need.

Working comparison: `test_freeform_proposal_prevalidation.py:2234-2271`, `test_durable_blob_mutations_admit_and_stage_without_executing_effect_handler`, seeds a real blob, calls `compose` with real persisted user-message authority, forbids `execute_tool` for durable effects, and asserts update/delete (including same-text update) stages with unchanged bytes. Separate missing/bound/nonready/quota cases return feedback and allow repair.

This remains a hypothesis about the reported service failure until its actual trace is read. If it instead reaches quota, content, fence, readiness or retention code, investigate that actual boundary before changing the fixture.

## Endpoint exact dictionaries

Both expected dictionaries in `test_endpoint_affordance.py:59-91,136-169` omit `reasoning_effort`; the first comment explicitly says bare models remain unhinted. Current `build_composer_loop_request_kwargs` (`provider_gateway.py:419-449`) always calls `apply_reasoning_kwargs` using default discovery effort `low` (`config.py:318`). Current `reasoning.py:42-65` always emits a hint, using `reasoning_effort` except for the OpenRouter native-object carve-out.

BASE already has that same helper behavior and those same stale test dictionaries. Read-only diff BASE..HEAD for `provider_gateway.py`, `config.py`, `reasoning.py`, and `test_endpoint_affordance.py` returned no changes. `git merge-base --is-ancestor c4d04bf4c BASE` exited 0. Commit `c4d04bf4c0321fb82081b6be73561a16e9149af1` states “composer and advisor must be reasoning models”; it removes the old bare-model opt-out and documents loud refusal of unsupported hints. Its reasoning source shows the same unconditional hint behavior. Thus the mismatch predates this D1 patch; no environment explanation is needed or supported.

Conditional repair if the actual two diffs show the single extra key `reasoning_effort: low`: add that exact expected entry to both full dictionaries and correct the stale no-hint comment. Retain exact equality, explicit absence of both endpoint keys, retry pins, empty-tool omission, and all guard keys. Do not weaken to a subset assertion, remove reasoning in production, or infer a generic optional-key exemption. If the trace differs in model, endpoint or additional keys, investigate that actual value rather than applying the guessed dictionary change.

## Minimal next evidence after parent releases execution

1. Read complete terminal traceback and assertion diff for each of these six reported failures. Associate each observed failure with this report's specific source seam; record any discrepancy.
2. After authorization, use a serial focused reproduction of only these named cases on the frozen candidate, with explicit worktree source roots/provenance and lane-private logs. No broad suite, source mutation, or competing PostgreSQL process is needed for diagnosis.
3. For endpoint baseline attribution, a focused base run in an isolated, provenance-verified checkout can establish the identical extra-key failure; source history already establishes the mismatch but is not a claimed test result. Preserve parent-owned capacity/freeze rules.
4. Stage no repair until parent releases the edit freeze. If traces confirm the contracts above, perform narrowly scoped test-only repairs and run their focused cases plus relevant D1 admission/cap/ID-reuse controls. A valid ready-blob cap fixture is the important missing discriminant for the misnamed blob-only test.
5. Mutation controls, if required, occur only after every active gate exits and under a separate explicit release. Their useful targets are pre-count restoration, ignoring admitted blob-only effects, dropping generated update provenance, or omitting the reasoning hint. No mutation was performed here.

There is no basis at this stage to relax production checks, weaken exact assertions, call these environmental failures, or claim the combined gate passes.

## Extension: reviewed reserve-contract family (active log, not terminal)

At the parent's request, read the active `d1-combined-gates/20261109T150428Z-composer-remediation-wave1-68412/pytest.log` for reported IDs only. It reports both `test_reviewed_turn_grammar_matches_live_source_and_refuses_reserve_drift[foreign_receiver-compose]` and `[changed_ordinal-compose]` as failed. Other reported failures include retained-failure transfer controls, reviewed writer-recipe controls, and guarded-title controls. This is not a final failure census, and no assertion trace or final exit has yet been supplied. No test, grammar helper, scanner, source hash recomputation, or pin refresh was executed.

### What the named test actually establishes

`test_web_reserve_reviewed_contracts.py:399-428` first calls `compose.compose_proposal_local_transfers` on the unchanged supplied source and asserts `failures == []`. Only after that precondition does it mutate the AST reservation receiver or ordinal and require the exact changed-TURN diagnostic. Thus the displayed parameter name **does not establish a foreign receiver or ordinal defect in production**: the baseline precondition may fail before that mutation occurs. The parallel route-contract branch inspects a different, narrower source set.

`tests/helpers/web_reserve_compose_proposal_transfers.py:515-563` checks the complete executed grammar for four methods plus `compose_recovery`, before its semantic transfer proof. Their dictionary includes:

| Selected method | D1 source comparison against BASE |
| --- | --- |
| `sessions/composer_turn.py::_run_composer_turn` | No D1 diff. |
| `composer/service.py::ComposerServiceImpl.compose` and `compose_recovery` | No D1 diff. |
| `sessions/routes/composer/proposals.py::accept_composition_proposal` | **D1 changes the selected complete method** with approved legacy terminal refusal and deferred-cancellation handling. |
| `sessions/routes/composer/pipeline_settlement.py::settle_pipeline_proposal_under_compose_lock` | Pinned public wrapper unchanged; D1 changes its private implementation. |

The helper's `_REVIEWED_METHOD_SHAPES[PROPOSALS]` remains `23ef777eef8f72ed82e5943f633d94fdaa6d4f76cab9bd7ef9ec642adc59a8ca`; no D1 helper/pin change accompanies the accept-route change. `git diff BASE..HEAD` proves both facts. An initial coordinator message mistakenly omitted the accept-route change; it was corrected immediately after checking `compose.PROPOSALS`'s exact path. The table above is the corrected finding.

This is a concrete candidate for a legitimate reviewed-grammar delta. The likely precondition diagnostic is `composer complete executed method grammar changed src/elspeth/web/sessions/routes/composer/proposals.py`, **not** the test's expected post-mutant TURN diagnostic. The exact active-gate trace must confirm that; no actual digest or helper result was computed here.

### Authority review of the changed route

The complete D1 accept-route diff is confined to the approved legacy behavior and supporting imports/helper:

- Capture `ToolArgumentError` from actual `execute_tool`, reject the already-loaded proposal with the same `SessionOperationLease.context`, drain deferred cancellation, and return the documented terminal argument diagnosis.
- Retain the existing actual worker execution, complete context/provenance, required-control finalizer, and catalog revalidation. Pass the same cancellation state through those existing awaited operations.
- After that real finalization, compare actual composition content only for non-blob-store effects; reject equality terminally with the same lease context. Blob-store-only effects retain their existing path.
- `_reject_legacy_proposal_refusal` awaits the existing Sessions rejection operation through the existing cancellation-draining helper. It catches only a competing `ProposalStateConflictError`; missing/corrupt loaded authority still faults.

No D1 change to the canonical route's `RequiredWorkCoordinator` construction, `lease.bind_required_work`, canonical settlement call, parent binding, context argument, or public settlement child-issuance wrapper appears in this diff. No new `reserve` call, receiver replacement, transition/semantic ordinal change, detached task, or arbitrary authority forwarding was introduced by this route delta. The final independent D1 review specifically reviewed these legacy cancellation/competing-winner/refusal semantics. These source facts support reviewing an exact new finite body; they do not authorize widening the grammar or bypassing any common-boundary proof.

The guard's semantic layer still requires one exact canonical settlement call, coordinator/binding creation and dominance, nominal owned context, exact paired arguments, and public child issuance (`_source3_local_transfers`, lines 315-380). Any approved pin maintenance must preserve all of those checks and keep both named receiver/ordinal mutants red. A blanket hash refresh without reviewing the whole selected method and its added helper is not sufficient.

### Why related families can cascade

`_reserve_retained_failure_transfer_receipts` (`test_web_landscape_mutation_fencing.py:10682-10787`) independently validates the retained-history/private-failure suppliers, then invokes **the same** complete compose-proposal grammar prerequisite at lines 10751-10755. Therefore a legitimate accept-route grammar mismatch can make unrelated retained-transfer controls fail before their own mutation reaches its intended assertion. The affected tests must retain their original negative-control meaning after prerequisite repair; do not change their expected failure to the unrelated prerequisite error.

The writer-recipe family has another concrete D1 prerequisite delta. `test_reviewed_writer_recipes_match_live_source_and_refuse_recovery_drift` first calls `_reserve_recipe_failures(canonical_units)` and requires no failures before mutating unchanged `composer/protocol.py::ComposerResult`. `_reserve_recipe_failures` first checks whole-source digests for `_reserve_FULL_SOURCE_PATHS`. D1 changes two of those protected files while `reviewed-full-source-guards.json` is unchanged:

- `sessions/protocol.py`: adds the closed `no_change` pipeline rejection literal plus typed `inspect_blob_mutation`, `admit_blob_replacement`, and `admit_blob_deletion` protocol methods and the `BlobMutationObservation` import.
- `sessions/proposal_authority.py`: adds exactly `no_change` to the owned rejection-reason set.

These are legitimate source deltas required by approved D1 behavior, subject to exact review; the full-source prerequisite will refuse changed bytes until their reviewed evidence is maintained. Actual terminal diagnostics must establish which guard fires first. The mutated `ComposerResult` recovery guard itself has no D1 production diff. Do not change its assertions to tolerate an unrelated full-source error, and do not expand protected exports or remove files from the protected set.

`reviewed-writer-recipes.json` and `reviewed-non-dml-recipes.json` likewise have no D1 diff. After the first full-source and exact-body discrepancies are reviewed, later recipe discrepancies may become visible; they must each be compared against the actual approved producer change. This report does not claim to have enumerated or validated those downstream discrepancies.

### Minimal discriminating follow-up after freeze release

1. Obtain terminal traces for these cases and identify whether each failure occurs at the unmodified-source precondition, the in-memory mutation setup, or the post-mutant assertion.
2. Run only the named serial prerequisites/controls once authorized, capturing raw failure diagnostics and import provenance. If the above route precondition is the cause, review a before/after structural diff for the entire selected accept method and new rejection helper against BASE, then maintain only the exact necessary reviewed body evidence.
3. Independently inspect protected full-source deltas and review exact protocol/rejection-authority changes before maintaining any full-source digest. Preserve the path set, protected exports, namespace rules, scanner algorithms, common proof obligations, and semantic transfer predicates.
4. Re-run the original receiver/ordinal mutants, retained-transfer mutants, reviewed recovery writer mutants, and rebound-grammar semantic controls. They must fail for their intended local mutation after an unchanged-source positive control succeeds. Refreshing a positive pin alone cannot prove the authority guard survived.
5. If any post-pin semantic proof reports receiver, binding, coordinator, cancellation, dominance or ordinal disagreement, stop pin maintenance and investigate that actual authority discrepancy. No production authority defect has been demonstrated by the active test names or this static comparison.

This extension preserves the earlier six diagnoses and their trace qualifications. All source/tests/index/HEAD remain frozen; the lane report is the only write.
