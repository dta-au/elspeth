# Live inventory and scope reconciliation

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` (restored 0.8.9), measured on 2026-11-09.
Counts below come from importing and calling the live declaration/definition
producers with both source roots on `PYTHONPATH`, not matching source text or
trusting numeric comments. Both `elspeth` and `elspeth_lints` resolved into this
checkout. No model provider was called.

## Measurement

```text
_REGISTERED_TOOLS:                         41
get_tool_definitions():                    43
composer_loop_tool_definitions(strict):    45
composer_loop_tool_definitions(none):      45
planner_tool_definitions(strict):          23
planner_tool_definitions(none):            23
distinct Web Composer tool names:          46
Composer MCP _build_tool_defs():           32
inventory process exit:                    0
```

The stated 46 reconciles exactly: 41 synchronous declarations, two async tools
(`request_advisor_hint`, `request_interpretation_review`), two recovery controls
(`handoff_to_user`, `restore_parked_graph`) and the private planner terminal
`emit_pipeline_proposal`. The freeform and planner sets are intentionally
different. Both dialects have equal name sets; this does not imply equal wire
schemas or live provider behavior. Nothing narrows the planner set at this
baseline: both `PlannerDiscoveryPolicy` constructions pass the full
`PLANNER_DISCOVERY_TOOL_NAMES` tuple
([initial](../../../src/elspeth/web/composer/pipeline_planner.py#L379),
[with_manifest](../../../src/elspeth/web/composer/pipeline_planner.py#L398));
the narrowing API at L1663 has no narrowing caller (corrected by the 2026-11-09
review; [`diff_pipeline`](tools/diff_pipeline.md) §3). Exposure is not usefulness: on the planner
`diff_pipeline`, `get_pipeline_state` and `preview_pipeline` cannot add
information (`diff_pipeline-N1`, `get_pipeline_state-N6`, `preview_pipeline-N2`).

MCP exposes 26 names shared with this Web Composer universe and six local-session
utilities: `new_session`, `save_session`, `load_session`, `list_sessions`,
`delete_session`, `generate_yaml`. Those six live outside `src/elspeth/web` and
are recorded as adjacent scope, not counted as six missing Web Composer sheets.
Relevant MCP route differences for shared tools are covered in their sheets.

The instrument asserted a known-present tool (`set_pipeline`), a known-absent
sentinel, unique names per emitted surface and equality of the union with the
definition map. The final documentation check additionally removes a known
sheet from its input and proves that missing-sheet validation rejects it.

## Tool-by-tool coverage

Every name below has a standalone sheet. “Loop” and “Planner” mean emitted by
the default producer; “MCP” means emitted by the separate local server. Availability
can additionally depend on policy, provider/configuration and per-tool context.
Cache means the synchronous declaration opts into within-turn discovery caching,
not a statement that a cache hit always occurs. Async/control lifecycle appears
explicitly because those tools do not have a synchronous declaration.

| Tool / assessment | Lifecycle kind | Loop | Planner | MCP | Cache opt-in |
|---|---|---|---|---|---|
| [`clear_source`](tools/clear_source.md) | mutation | yes | — | yes | no |
| [`create_blob`](tools/create_blob.md) | blob_mutation | yes | — | — | no |
| [`delete_blob`](tools/delete_blob.md) | blob_mutation | yes | — | — | no |
| [`diff_pipeline`](tools/diff_pipeline.md) | discovery | yes | yes | yes | no |
| [`emit_pipeline_proposal`](tools/emit_pipeline_proposal.md) | planner terminal | — | yes | — | no |
| [`explain_validation_error`](tools/explain_validation_error.md) | discovery | yes | yes | yes | yes |
| [`get_audit_info`](tools/get_audit_info.md) | discovery | yes | yes | yes | yes |
| [`get_blob_content`](tools/get_blob_content.md) | blob_discovery | yes | yes | — | no |
| [`get_blob_metadata`](tools/get_blob_metadata.md) | blob_discovery | yes | yes | — | no |
| [`get_expression_grammar`](tools/get_expression_grammar.md) | discovery | yes | yes | yes | yes |
| [`get_pipeline_state`](tools/get_pipeline_state.md) | discovery | yes | yes | yes | no |
| [`get_plugin_assistance`](tools/get_plugin_assistance.md) | discovery | yes | yes | yes | yes |
| [`get_plugin_schema`](tools/get_plugin_schema.md) | discovery | yes | yes | yes | yes |
| [`handoff_to_user`](tools/handoff_to_user.md) | recovery terminal | yes | yes | — | no |
| [`inspect_source`](tools/inspect_source.md) | blob_discovery | yes | yes | — | no |
| [`list_blobs`](tools/list_blobs.md) | blob_discovery | yes | yes | — | no |
| [`list_composer_blobs`](tools/list_composer_blobs.md) | blob_discovery | yes | yes | — | no |
| [`list_models`](tools/list_models.md) | discovery | yes | yes | yes | yes |
| [`list_secret_refs`](tools/list_secret_refs.md) | secret_discovery | yes | yes | — | no |
| [`list_sinks`](tools/list_sinks.md) | discovery | yes | yes | yes | yes |
| [`list_sources`](tools/list_sources.md) | discovery | yes | yes | yes | yes |
| [`list_transforms`](tools/list_transforms.md) | discovery | yes | yes | yes | yes |
| [`patch_node_options`](tools/patch_node_options.md) | mutation | yes | — | yes | no |
| [`patch_output_options`](tools/patch_output_options.md) | mutation | yes | — | yes | no |
| [`patch_source_options`](tools/patch_source_options.md) | mutation | yes | — | yes | no |
| [`preview_pipeline`](tools/preview_pipeline.md) | discovery | yes | yes | yes | no |
| [`read_attachment`](tools/read_attachment.md) | blob_discovery | yes | yes | — | no |
| [`remove_edge`](tools/remove_edge.md) | mutation | yes | — | yes | no |
| [`remove_node`](tools/remove_node.md) | mutation | yes | — | yes | no |
| [`remove_output`](tools/remove_output.md) | mutation | yes | — | yes | no |
| [`request_advisor_hint`](tools/request_advisor_hint.md) | async advisor | yes | — | — | no |
| [`request_interpretation_review`](tools/request_interpretation_review.md) | async session | yes | — | — | no |
| [`restore_parked_graph`](tools/restore_parked_graph.md) | recovery terminal | yes | yes | — | no |
| [`set_metadata`](tools/set_metadata.md) | mutation | yes | — | yes | no |
| [`set_output`](tools/set_output.md) | mutation | yes | — | yes | no |
| [`set_pipeline`](tools/set_pipeline.md) | mutation | yes | — | yes | no |
| [`set_source`](tools/set_source.md) | mutation | yes | — | yes | no |
| [`set_source_from_blob`](tools/set_source_from_blob.md) | blob_mutation | yes | — | — | no |
| [`set_source_from_blobs`](tools/set_source_from_blobs.md) | blob_mutation | yes | — | — | no |
| [`splice_transform`](tools/splice_transform.md) | mutation | yes | — | yes | no |
| [`update_blob`](tools/update_blob.md) | blob_mutation | yes | — | — | no |
| [`upsert_edge`](tools/upsert_edge.md) | mutation | yes | — | yes | no |
| [`upsert_node`](tools/upsert_node.md) | mutation | yes | — | yes | no |
| [`validate_secret_ref`](tools/validate_secret_ref.md) | secret_discovery | yes | yes | — | no |
| [`wire_blob_inline_ref`](tools/wire_blob_inline_ref.md) | blob_mutation | yes | — | — | no |
| [`wire_secret_ref`](tools/wire_secret_ref.md) | secret_mutation | yes | — | — | no |

## Source authorities and reproduction

- [Synchronous registry](../../../src/elspeth/web/composer/tools/_registry.py#L97).
- [Canonical emitter](../../../src/elspeth/web/composer/tools/_dispatch.py#L311).
- [Loop producer](../../../src/elspeth/web/composer/provider_gateway.py#L391).
- [Planner producer](../../../src/elspeth/web/composer/pipeline_planner.py#L1647).
- [Recovery definitions](../../../src/elspeth/web/composer/recovery_controls.py#L35).
- [MCP filtered surface](../../../src/elspeth/composer_mcp/server.py#L210).

Re-run the producers in a checkout-local frozen environment. For example:

```bash
PYTHONPATH=src:elspeth-lints/src .venv/bin/python - <<'PY'
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.tools import get_tool_definitions
from elspeth.web.composer.tools._registry import _REGISTERED_TOOLS
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.pipeline_planner import planner_tool_definitions
print("registered", len(_REGISTERED_TOOLS))
print("canonical", len(get_tool_definitions()))
universe = {item["name"] for item in get_tool_definitions()}
for dialect in ToolContractDialect:
    for label, emit in (("loop", composer_loop_tool_definitions), ("planner", planner_tool_definitions)):
        names = [item["function"]["name"] for item in emit(dialect=dialect)]
        print(label, dialect.value, len(names), names)
        universe.update(names)
print("web distinct", len(universe))
PY
```

This is a measured baseline, not an invariant that future code must preserve
the number 46. Additions/removals require a renewed inventory and corresponding
assessment, not a hard-coded count adjustment.
