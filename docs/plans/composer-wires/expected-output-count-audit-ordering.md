# Audit-preserving expected_output_count admission ordering

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.
> The final count-applicability charter governs implementation. New guidance must use its prerequisite direct diagnostic-code catalogue, not grow the legacy regex inventories.

Preparation only, based on source inspection at campaign HEAD 1278c5c215fc8f749d53fd16d57413e416b93a00. No repository edits, application imports, tests, provider calls or database actions were performed for this recommendation.

This constrains the implementation of [Transform-only expected_output_count alignment](count-applicability-implementation-charter.md). It supersedes any interpretation of that report that would enforce domain applicability inside the Pydantic transport/redaction models.

## Required separation

Keep structural admission and domain applicability separate. Tightening the shared redaction model would lose attempted arguments: `src/elspeth/web/composer/audit_storage.py:119-128` reduces `ARG_ERROR` evidence to `_redaction_status`, `error_class` and `field_count`. The normal failed-result path at `:137-141` uses the manifest redactor and can preserve inspectable mode/count safely. A stricter redaction failure is not an acceptable substitute for transparent semantic rejection.

Use one shared pure owned domain check. Reject only an aggregation whose effective output mode is passthrough and whose expected_output_count is non-null. Composer omitted/null mode retains the existing transform default. Do not add numeric range/coercion restrictions, modify values, silently discard a count, or broaden runtime null-mode admission. Return an owned diagnostic or raise a specific domain error that each adapter wraps explicitly.

Suggested diagnostic code: `aggregation_expected_output_count_mode_invalid`.
Suggested message: `expected_output_count requires output_mode='transform' (the default); omit expected_output_count for 'passthrough'.`

## Exact Composer ordering

### Incremental upsert_node

`src/elspeth/web/composer/tools/transforms.py:605` structurally admits `_UpsertNodeArgumentsModel`. Invoke the pure applicability check on its owned fields immediately afterward, before option normalization and plugin prevalidation. Return an ordinary semantic `_failure_result` with actionable code/component; do not raise `ToolArgumentError` for this structurally valid pair.

Evidence for mutation ordering: option processing starts at `:610-615`, plugin prevalidation occurs at `:685`, aggregation trigger checks at `:717-720`, NodeSpec construction at `:730-752`, and `state.with_node` at `:762`. Rejection must preserve the original state and version.

### Full set_pipeline

`src/elspeth/web/composer/tools/sessions.py:810-830` performs structural model admission and source-shape checks. Immediately afterward, before source processing begins at `:832`, sweep admitted nodes for this pure applicability defect. Collect the intrinsic node failures in input order through the existing bounded collector (`_record_component_rejection`, `:703-718`) and return the collected failure before custody. Keep accurate node component references and existing bounded truncation reporting. Do not stop at the first invalid aggregation node.

The existing node-plugin check at `:1397` is too late to satisfy pre-custody admission. `_legacy_source_rejection` resolves source blobs at `:939` and prepares inline blobs at `:978`; that helper is invoked at `:1295`, before the node loop at `:1302`. Final inline persistence happens later at `:1829`, but merely rejecting before that final write does not establish that custody preparation was avoided.

This is a new pure preflight stage. Leave the subsequent source/node/output processing order unchanged. A preflight rejection cannot claim that later source/plugin/output checks ran; it reports the intrinsic defects actually examined, without creating or resolving a blob to discover unrelated diagnostics.

## Evidence projection constraints

Keep `_PipelineNodeModel` and its nested `SetPipelineArgumentsModel` use structurally accepting the semantically invalid pair. Keep incremental transport/redaction models equally capable of representing the attempt. Do not put this cross-field semantic constraint in a shared model validator, including a future common node argument model inherited by the manifest redactor.

Ordinary semantic failed-result audit redaction must retain the supplied `output_mode` and `expected_output_count` while preserving existing sensitive options/blob-content substitutions. Do not introduce new invalid-argument fallbacks, raw argument disclosure, historical rewriting, provider routing or tutorial-special handling.

Existing NodeSpec hydration and ordinary serialization remain readable. An invalid state can be inspected and repaired; readable state is not permission for a new mutation, run or export to accept the pair.

## Runtime, import, state validation and lowering parity

- `src/elspeth/core/config.py:662-709`, AggregationSettings: invoke the same rule in after-model domain validation. OutputMode.TRANSFORM remains the default; explicit runtime null mode remains outside the existing enum contract.
- `src/elspeth/web/composer/yaml_importer.py:718-721`: after scalar admission, invoke the rule before appending NodeSpec. Wrap rejection as RuntimeYamlImportError with aggregation/field context. This path bypasses AggregationSettings construction.
- `src/elspeth/web/composer/yaml_generator.py:278-301`: invoke the rule before emitting the aggregation document; raise PipelineLoweringError on invalid state. Preserve conditional omission of None, including valid default-transform count with mode omitted. The check is intrinsic to this node, not a new whole-pipeline completeness requirement.
- `src/elspeth/web/composer/state.py`, aggregation validation branch identified in the alignment report: report the named intrinsic diagnostic without changing hydration or stored values.
- `src/elspeth/web/composer/tools/generation.py`: register actionable advice/code in the existing inventories. Present removing the count or deliberately selecting transform as user choices, not equivalent automatic repairs.
- Keep engine passthrough cardinality/identity behavior unchanged; this work closes admission of an ignored configuration, not executor semantics.

## Test obligations

1. Real invalid upsert_node and set_pipeline dispatch produce ordinary semantic failure, retain exact prior state/version, and serialize inspectable redacted mode/count through the actual audit path. Include sensitive options and inline content; those values must stay summarized.
2. Invalid full-pipeline aggregation plus inline blob never reaches blob preparation, resolution or persistence. Verify the call boundary, not merely a final zero-blob count.
3. Multiple invalid aggregations retain input-order, bounded per-component diagnostics. Check truncation reporting when the existing collector limit is exceeded.
4. Explicit/omitted/null Composer transform mode with a count remains accepted as applicable. Passthrough with omitted/null count remains accepted. Do not add numerical restrictions in these tests.
5. Runtime settings and actual YAML import reject passthrough plus non-null count; valid default-transform/count and passthrough/no-count round-trip. Test the public lowering/export entry path as well as ordinary generation.
6. Legacy/internal invalid NodeSpec stays readable, emits the new state validation code and fails lowering without rewriting its fields. Repairing the pair works normally.
7. Existing executor regression obligations remain: transform count mismatch fails, passthrough preserves N-row identity and accepts its established zero-row result behavior. No provider calls are needed for these checks.

These are required implementation checks, not claims that tests have run or passed.
