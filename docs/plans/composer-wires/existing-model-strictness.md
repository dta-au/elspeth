# Existing argument model strictness: bounded audit

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.
> This is supporting historical evidence, not fresh verification. The old strict-Python-integer preference below was corrected: finite integral JSON numbers remain valid, including list_models.limit.

Read-only audit of current composer-wires-campaign source and production model classes. Ran scratch admission probes with `LITELLM_LOCAL_MODEL_COST_MAP=True`, bytecode disabled, and both campaign source roots on PYTHONPATH. Imported production models and `get_tool_definitions`; did not import census/gate modules, invoke handlers, call a provider, or modify production files. No test suite result is claimed.

For each case below, first validated the COMPLETE baseline object with both the production Pydantic model and the shipped Draft202012 schema, then changed one field. Results are direct whole-model admission evidence, not conclusions from isolated TypeAdapter fields. Main machine-readable results: `existing-model-probes.json`. Additional trigger timeout probes are recorded below. This is a bounded audit, not full-42 or all-field proof.

## Proven actionable mismatches

| Field | Mutation admitted by whole model but rejected by shipped schema | Authority / repair |
|---|---|---|
| `upsert_node.expected_output_count` | `true`, `"3"` | `_UpsertNodeArgumentsModel`, transforms.py:96. Plain `int | None`, no strict field metadata. Reject boolean/string coercion at argument admission. |
| `set_pipeline.nodes[0].expected_output_count` | `true`, `"3"` | `_PipelineNodeModel`, redaction.py:1959. Same plain integer hole on full-state path; use the same honest JSON-number admission policy as incremental path. |
| `set_pipeline.nodes[0].trigger.count` | `true`, `"3"` | Nested trigger model, redaction.py:1877. Root extra='forbid' does not make numeric fields strict. |
| `set_pipeline.nodes[0].trigger.timeout_seconds` | `true` becomes `1.0`; `"3"` becomes `3.0` | redaction.py:1878 plain float. Reject these without rejecting JSON integer numbers: `3` is schema-valid and model accepts it as `3.0`. `None` is advertised nullable and must remain valid. |
| `set_metadata.patch.name` | explicit `null` | `_SetMetadataPatchModel`, transforms.py:122. Omission is valid. Distinguish omitted value from supplied null instead of widening the public schema accidentally. Check sibling description in implementation, but this audit did not mutate that sibling. |
| `create_blob.description` | explicit `null` | `CreateBlobArgumentsModel`, redaction.py:1699. Same omission/supplied-null disagreement. |
| `set_source_from_blob.plugin` | explicit `null` | `SetSourceFromBlobArgumentsModel`. Published optional string differs from model's optional nullable field. Preserve omitted inference behavior while rejecting supplied null. |
| `upsert_node.trigger` | `{"invented": 1}` | `_UpsertNodeArgumentsModel.trigger` is `dict[str, Any] | None`, while advertised trigger is a closed count/timeout_seconds/condition record. This is owned record structure, not arbitrary plugin options. Use a typed trigger admission record; preserve the existing argument-error channel and distinguish it from downstream semantic trigger validation. |

The last row proves argument-model admission only. `_execute_upsert_node` later calls `_validate_aggregation_trigger` for aggregations. Do not claim that this payload necessarily executes successfully or escapes every current guard. Moving shape enforcement into the input model closes an existing split contract; any changed repair text should remain safe and consistent with current tool errors.

## Counterevidence and controls

- `upsert_node.timeout_seconds=true` and `"3"` are already rejected by both schema and model. `_StrictTimeoutSeconds` has `_reject_coerced_timeout_seconds` BeforeValidator and finite/positive constraints. This existing admission code is a useful pattern, but do not blindly copy its positivity constraint onto a different field without checking that field's advertised semantics.
- `upsert_node.expected_output_count=3.0` passes the shipped JSON Schema integer check and the model. JSON Schema treats mathematically integral JSON numbers as integers. Do not call this a schema mismatch. The user explicitly requested a strict Python integer for `list_models.limit`; extending that extra narrowing to every integer field is a distinct implementation choice, not proven parity repair.
- `upsert_node.plugin=null`, `upsert_edge.label=null`, `set_pipeline.metadata=null`, and trigger timeout null are accepted by BOTH sides. Preserve these intentional nullable fields.
- `create_blob.content=42` is rejected by both; default Pydantic strings are not universally coercive in the same way as numbers.
- `set_pipeline.nodes[0].trigger={"invented":1}` is already rejected by both sides: its nested trigger model is closed.
- `set_metadata.patch.invented=1` and `set_pipeline.source.invented=1` are rejected by models but ACCEPTED by their shipped nested schemas. These are producer schema openness gaps in owned records, not model laxness. Add nested `additionalProperties:false` where the owned model is already closed and verify intended schema disclosure compatibility.

## Deliberate workaround for malformed nested output: eligible for strict repair

Proven mismatches also occur for `set_source_from_blob.options='{"x":1}'` and `set_pipeline.source.options='{"x":1}'`: the schema rejects strings, but the model parses them into objects. This is NOT unexplained Pydantic coercion. `_LlmJsonObject` includes `BeforeValidator(_coerce_stringified_json_object)` in redaction.py:1434.

The helper's source documentation explicitly records a provider behavior workaround: openrouter/openai/gpt-5.4-mini intermittently stringified object-valued fields in staging sessions fd551d98 and 71d57b4f. Dedicated tests preserve narrow decoding and depth/length failure behavior. Those historical incident claims were read from current source; their underlying provider transcripts were not independently reverified in this audit.

The documented trigger is malformed model output: a nested value violates the object type ELSPETH actually advertises. No inspected source or supplied evidence identifies a provider/SDK contract requiring object-valued nested fields to be strings. That is distinct from the legitimate top-level tool `arguments` JSON string that the transport decodes before these fields are considered. The historical source calls the conversion meaning-preserving, but that is a local compatibility policy, not evidence that the external protocol imposes it.

Under the parent's supplied user ruling, this workaround is eligible for strict advertised-object admission and actionable argument-error feedback; it is not a mandatory exception. Retire nested object-string acceptance deliberately while preserving legitimate top-level JSON decoding, safe error envelopes, and resource bounds on decoding that remains. Simply setting ConfigDict(strict=True) is insufficient because BeforeValidator still parses the string before strict dict validation. If the parent instead retains this intentional compatibility behavior, the advertised field schema must truthfully include the supported string encoding with bounded parse/rejection coverage; hidden coercion must not be reported as exact type parity. No provider call or additional broad investigation is required for this distinction.

Do not close the keys of the resulting plugin options/patch maps. Their plugin/data-owned key spaces remain intentionally dynamic even if string encodings are rejected. This differs from the fixed-shape trigger and metadata records above.

## Baselines used

```python
upsert_node = {'id': 'a', 'node_type': 'transform', 'input': 'source'}
upsert_edge = {'id': 'a', 'from_node': 'a', 'to_node': 'b', 'edge_type': 'on_success'}
set_metadata = {'patch': {}}
create_blob = {'filename': 'a.txt', 'mime_type': 'text/plain', 'content': 'a'}
set_source_from_blob = {'blob_id': 'a', 'on_success': 'next'}
set_pipeline = {
    'source': {'plugin': 'csv', 'on_success': 'next'},
    'nodes': [{'id': 'n', 'node_type': 'aggregation', 'input': 'next'}],
    'edges': [], 'outputs': [],
}
```

For trigger timeout probes, the pipeline node additionally began with `trigger={'timeout_seconds':1}`. Both full baseline validators passed before mutation. These baselines establish argument-schema/model admission; they do not assert a runnable graph, an existing blob ID, or plugin-catalog validity.

## Tests to extend

- `tests/unit/web/composer/test_redaction_trust_boundaries.py`: existing direct timeout wrong-type controls; add full outer-model count/trigger timeout admission controls alongside the helper tests so a disconnected validator cannot look green.
- `tests/unit/web/composer/test_tool_schema_contract.py` and `test_tool_dispatch_boundary.py`: compare explicit omission/null and numeric-type cases against shipped definitions and actual handler-admission models; keep the existing public error envelope checks.
- `tests/unit/web/composer/test_tools.py`: extend actual upsert aggregation/expected-output behavior tests with rejected bool/string inputs and preserved valid numeric values. Mock operational boundaries as current fixtures do.
- `tests/unit/web/composer/test_redact_tool_call_arguments.py` / `test_redact_set_source.py`: preserve sensitivity markers and admission/redaction model agreement after changes to shared redaction-bearing models.
- `tests/unit/web/composer/test_coerce_stringified_json_object_args.py`: existing intentional compatibility tests, especially `test_set_source_from_blob_options_stringified_object_coerces`, `test_set_pipeline_source_options_stringified_object_coerces`, and bounded rejection cases. Any normalization policy change must address these explicitly.

No blanket strict configuration is recommended. Apply small shared strict scalar types/validators where semantics agree, model owned nested records precisely, preserve schema-valid arrays and genuinely dynamic plugin maps, and verify each changed model with a valid whole-object baseline plus single-field mutations.
