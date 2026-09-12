# Presence-preserving argument redaction and honest replacement diffs

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.
> The combined presence/errors charter governs the epoch cutover and all five semantic comparisons. Historical line references and source observations below must be refreshed.

Read-only preparation against worktree `.claude/worktrees/composer-wires-campaign`, HEAD `1278c5c215fc8f749d53fd16d57413e416b93a00`. No repository edits or test-suite runs. User has approved option (a), preserving omitted versus explicit null, and prioritizes transparency. **The newer `combined-presence-errors-charter.md` controls implementation and supersedes this report wherever they differ, including epoch cutover and all five semantic hash comparisons.**

## Bounded contract

Validate arguments using the existing manifest model, then preserve recursively which model fields were supplied while applying the existing sensitive-field substitutions. Omitted non-sensitive fields remain absent; explicitly supplied null remains null. Sensitive fields still become their prescribed safe summary, so this does not promise exposure of a sensitive null or nested sensitive key. Responses retain their existing default-inclusive redaction. This changes display/audit argument projection, not handler defaults, mutation semantics, or historical authority hashes.

The before/after UI compares supplied arguments; it must not imply that an omitted field survives a replacement. Replacement cards always disclose that omitted settings may reset to defaults. They must not assert that the actual pipeline is unchanged merely because supplied comparable arguments match.

## Minimal production changes

1. `src/elspeth/web/composer/redaction.py::_redact_via_schema` (currently line 2297): add keyword-only `exclude_unset: bool = False`; change the local deep-copy expression to `copy.deepcopy(validated.model_dump(exclude_unset=exclude_unset))`. Retain the existing Sensitive schema walk, error behavior, and telemetry unchanged.
2. `redact_tool_call_arguments` type-driven branch (line 2579): pass `exclude_unset=True`. Declarative redaction already walks provided arguments and needs no equivalent conversion. Response callers at lines 4031 and 4116 retain default `False`.
3. The argument function's current return at line 2580 invokes `normalize_set_pipeline_redacted_arguments`, which strips even an explicitly supplied `source.inline_blob: null`. Return the sparse redacted mapping directly for new argument display; do not delete or globally change the normalizer.
4. **Necessary narrow publication seam beyond the requested files:** `src/elspeth/web/sessions/service.py`, canonical pipeline proposal creation at lines 7490-7498, recomputes the expected projection, then normalizes supplied redacted arguments and persists the normalized copy. After step 3, that rejects explicit null and destroys presence. New proposal creation should compare the supplied deep-thawed projection exactly against the freshly generated sparse projection and persist that exact representation. Remove normalization from this new-publication path; do not compensate by normalizing both sides, which would erase the approved distinction. Retain all other authority comparisons and transaction behavior.
5. `src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx`: extend the local `ProposalDiffResult` with a replacement scope such as `replacementScope: "pipeline" | "component" | null`, populate it from the projection dispatch's own arm/registry metadata, and render a notice for every replacement projection. `set_pipeline` is pipeline replacement; `set_source`, `set_output`, `upsert_node`, and `upsert_edge` replace a component. Avoid another independently maintained name list. `clear_source`, removals, option patches, and metadata patches do not acquire this replacement notice by default.
6. For replacement cards, empty-state text: `No difference in the supplied arguments this view can compare.` Notice: `This view compares supplied arguments. Omitted settings may be reset to defaults when the pipeline is replaced.` Use `component` for component replacement. Show the notice whether entries are empty or nonempty and whether an option summary was skipped. Retain the separate existing option-values-not-compared caveat. Update the module honesty-contract comments and `ProposalChanges` documentation so they match runtime behavior.

Do not implement client-side effective-default simulation. Do not remove the redaction blind-spot ledger. Do not interpret explicit null as universally meaning clear: some handlers default it and metadata currently ignores it.

## Custody normalization callers

* **Retain normalization semantics, share its helper:** persisted dispatch restoration in `src/elspeth/web/composer/pipeline_commit.py` normalizes inline_blob null only for redacted semantic comparison. The controlling charter calls this constructor `PipelineDispatchAuditBinding.from_persisted_envelope` and requires the same narrowly named helper as all five service sites. Preserve exact validation before that helper. Old-epoch row compatibility is not added.
* **Retain helper and helper tests:** `redaction.py:2479`; `tests/unit/web/composer/test_redact_set_source.py::test_normalize_set_pipeline_redacted_arguments_membership_shapes`, `::test_normalize_set_pipeline_redacted_arguments_reads_the_frozen_authority_form`, and `::test_normalize_set_pipeline_redacted_arguments_leaves_a_frozen_redacted_blob_alone`.
* **Change new-publication caller as above:** `src/elspeth/web/sessions/service.py:7495`. Contrary to the first exploratory recommendation, this caller cannot remain unchanged: it explicitly overwrites the new display with a normalized copy. Historical authority and newly published display must have separate responsibilities here.

## Measured serialization behavior and traps

Runtime inspection of the live redaction MANIFEST recursively visited 21 argument model classes. No registered custom model/field serializers, direct `model_dump` overrides, or `serialize_by_alias=True` were present. Runtime `SetPipelineArgumentsModel.model_validate(replay_case).model_dump(exclude_unset=True)` exactly equalled the fixture's raw argument mapping, retained explicit node `on_error: null`, and kept only `name` in a provided `metadata: {name: ...}` object.

The substitution walker already skips missing intermediate fields (`redaction.py:797`) and missing leaves (`:813`), so no new getattr/default/error-catching path is needed. It traverses present nested models, lists, and named-source mappings using the same declared paths. Sparse dump must happen BEFORE sensitive summarization; pruning a completed summary against raw object structure is wrong and can also reveal sensitive names.

Nested sparsity also corrects metadata summary identity: supplying only metadata name should produce `<metadata-patch:name>`, not claim a supplied description inserted by model defaults. An explicit empty metadata object should produce `<metadata-patch:empty>`. Do not preserve prior incorrect sentinel values just to reduce fixture churn.

Avoid asserting that every schema required field always appears on sparse output without checking validators: Pydantic model_fields_set reflects validator assignments too. Current replay runtime probe passed, but regression cases should use the real argument entry point. A future custom serializer moving a sensitive field would violate the walker contract; any guard should traverse live models using owned model metadata, not grep source or add dynamic-attribute hacks.

## Actual execution semantics behind the notice

`set_pipeline` creates fresh specs and a fresh state (`tools/sessions.py:1646-1734`). Omitted description/timeout fields become None; omitted or null transform/aggregation on_error becomes discard (:1652); omitted or null output on_write_failure becomes discard (:1710). Source description comes directly from the validated new source (:1106).

`upsert_node` similarly constructs a fresh NodeSpec (`tools/transforms.py:714-735`) and calls with_node; `set_source` constructs SourceSpec and calls with_named_source (`tools/sources.py:1153-1160`); `set_output` constructs OutputSpec and calls with_output (`tools/outputs.py:182-189`). These component replacements do not preserve omitted optional fields from the current component. Option patch tools merge and do preserve omitted option keys. `set_metadata` currently uses model_dump(exclude_none=True) (`tools/transforms.py:1399`), so explicit null does not clear metadata.

## Exact regression placement and cases

### Python producer and safety

Add tests in `tests/unit/web/composer/test_redact_set_source.py` or `test_redact_tool_call_arguments.py`, using the real `redact_tool_call_arguments` producer:

1. Parameterized omitted versus explicit-null source description, node description/timeout, output description under set_pipeline. Assert absence with key membership, null with membership AND `is None`.
2. Omitted versus explicit-null `source.inline_blob`, asserting exact display presence; include a non-null inline blob whose content is still summarized and never appears in serialized output.
3. Named-source mapping and node-list sensitive options remain summarized when surrounding optional fields are absent. A present nested trigger retains only supplied fields, without dropping required structure.
4. Metadata absent, empty object, name-only, and description-only produce corresponding absence/summary identities; authored metadata values do not appear.
5. Incremental set_source/upsert_node/set_output arguments preserve optional-key presence. Required-field omission still raises ValidationError, malformed values still fail, and caller input remains unchanged.
6. Keep `tests/unit/web/composer/test_redact_tool_call_response.py` outputs unchanged and add one direct check that the shared helper default still includes schema defaults for a response model if existing assertions do not cover it.

### New proposal persistence and historical authority

Extend `tests/unit/web/sessions/test_composer_proposals.py::test_create_pipeline_proposal_writes_closed_bound_creation_event_and_restores` with an explicit inline_blob-null case using producer-generated redacted arguments. Assert creation, event/row projection equality, and restored display preserve null. Add omitted counterpart and retain tampered binding rejection tests.

Keep the existing frozen normalizer tests unchanged. Run `tests/integration/web/composer/test_pipeline_proposal_lifecycle.py` (including `test_pipeline_dispatch_binding_restores_core_domain_normalized_reserved_mapping`) and `tests/unit/web/composer/test_pipeline_commit_operation_authority.py`; add a restored historical invocation carrying null inline_blob only if no existing test covers this exact semantic-hash normalization. No migration or rewriting of old records.

### Shared fixtures and frontend

Add cases to `scripts/cicd/bootstrap_proposal_diff_fixture.py`, regenerate `src/elspeth/web/frontend/src/test/fixtures/redacted-tool-arguments.json`, and retain `tests/unit/web/composer/test_proposal_diff_redaction_fixture.py` producer equality checks. Keep insertion order; do not sort nested keys.

Use those real payloads in `src/elspeth/web/frontend/src/components/chat/ProposalDiff.test.tsx`:

1. Existing `set_pipeline_replaying_current_state` remains no comparable entries, but replacement notice is mandatory. The current makeState omits most optional keys, so this case alone cannot prove absence behavior.
2. Start from current state with non-null source description/node timeout/output failure policy. Omit corresponding raw proposed fields; do not emit a fabricated provided-null difference. Still show replacement notice and qualified empty-state text.
3. Explicit-null counterpart: comparison must not skip the explicitly supplied value. Use a non-sensitive field with an existing current key; assert a row when values differ. Do not label it as a guaranteed runtime clear.
4. Replacement notice with nonempty entries, with redacted options, and with empty options; notice is independent of the option ledger.
5. Component replacement notice for each replacement dispatch arm; no replacement notice for genuine option patch. Retain all existing option-redaction caveat and iteration-order tests.
6. Update direct ProposalDiffResult literals in renderer tests for the added local field; no API/generated TypeScript wire type change is necessary.

## Reconciliation: durable dispatch and recovery hashes

The earlier publication recipe is necessary but not sufficient. The controlling combined charter identifies `PipelineDispatchAuditBinding.from_persisted_envelope` as the persisted semantic binding constructor. It normalizes inline_blob null before calculating the semantic dispatch binding. An exact sparse proposal display with an explicitly supplied null therefore cannot be compared by directly hashing its raw redacted row. Earlier function naming and the three-site inventory in this scratch report were incomplete; use the combined charter's verified five-site inventory.

Add one named semantic hash helper for **redacted pipeline argument comparison**, computing composer_authority_hash over normalize_set_pipeline_redacted_arguments of that mapping. It must neither mutate nor replace the row's display. Use it in `PipelineDispatchAuditBinding.from_persisted_envelope` after exact canonical/hash/projection validation and at all FIVE existing comparisons in `src/elspeth/web/sessions/service.py`:

* `settle_pipeline_composition_proposal`, currently line 7725;
* `reject_pipeline_composition_proposal`, currently line 8026;
* `_pipeline_dispatch_recovery_on_connection`, currently line 12676.
* The later durable dispatch comparison at line 12837.
* The later recovery comparison at line 12952.

Keep the exact publication/event/row display comparison and exact displayed bytes; normalization is exclusively a semantic comparison here. Do not apply this helper to private arguments, invocation arguments_canonical hashes, authority_arguments_canonical hashes, result hashes, `_pipeline_private_arguments_hash`, `_pipeline_audit_payload_hash`, proposal draft hashes, or existing live/private pipeline authority checks. In particular leave `PipelineDispatchAuditBinding.from_invocation`, `pipeline_commit.py:571`, and the private comparison immediately before the new semantic call at `sessions/service.py:12835` untouched. The controlling charter requires a shared helper in the persisted constructor as well as all five row comparisons; do not retain the earlier suggestion to leave that constructor's inline calculation separate.

Required additional tests before implementation is considered complete: create a pipeline proposal with explicit inline_blob null, persist the canonical invocation and its redacted record through the real audit adapter, restore the dispatch binding, then exercise FIRST durable guided dispatch recording, settle, reject, retry and recovery separately. All five semantic comparisons must accept the corresponding exact display while retaining explicit null in the row/event. The omitted counterpart must also work. Change a non-normalized argument, tool call id, or result hash and assert rejection. Assert private canonical hashes still differ when raw private arguments differ; no global null/absence equivalence is introduced. Old default-inclusive pending rows are excluded by the combined charter's session epoch cutover, not read through a compatibility path. Do not introduce old-shape readers, migrations, backfills, or actual store resets. This reconciliation follows the controlling audit report and claims no passing probe.

## Gates and validation boundary

Run proportional Python producer/fixture/session/authority tests and focused Vitest, then frontend typecheck and lint. Inspect generated redaction snapshot drift through `scripts/cicd/bootstrap_redaction_snapshot.py`; `tests/unit/web/composer/redaction_policy_snapshot.json` is the existing artifact. Schema sensitive-path inventory should not change: no Sensitive annotations are added or removed. Summarizer output/fixture bytes can legitimately change with corrected presence.

Whole-tree AST contracts for getattr/hasattr, masquerade, test mocks and fingerprints remain relevant. This design requires no new dynamic attribute access, broad catch, suppression, or fake exception types. The CONTRIBUTING wire-shape-template gate specifically pins no_tool_policy wrapped diagnostics; a frontend-authored JSX caveat is not such a backend diagnostic and should not be added to that registry. Do not invent extra backend wire fields to carry the local replacement notice.

Before merging run the required whole-tree gate. Because the necessary sessions publication seam is changed, parent must assess and run required PostgreSQL testcontainer checks under repository policy. Full-suite and trust-tier corpus evidence remains the parent's integrated validation obligation; this handoff claims no suite passed.
