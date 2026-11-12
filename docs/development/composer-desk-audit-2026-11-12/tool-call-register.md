# Composer tool-call register (snapshot 2026-11-12, `main` @ `1a7ff0e6`)

Every tool any composer surface can call, generated from the live
registries, joined with live provider captures from the desk-audit battery
(about 290 provider calls over 74 turns, DeepSeek v4.1 Flash composer and
GLM-5.3 advisor).

Surfaces:

- **Loop**: the tool loop on a non-empty session, built by
  `provider_gateway.composer_loop_tool_definitions`.
- **Planner**: the empty-session proposal planner, built by
  `pipeline_planner.planner_tool_definitions`.
- **Hatch**: the planner's escape-hatch turn (recovery controls plus the
  terminal).
- The advisor is offered no tools.

How this is generated:

```bash
.venv/bin/python evals/composer-live-audit/build_register.py out.json <capture.jsonl ...>
.venv/bin/python evals/composer-live-audit/render_register.py out.json out.md
```

How each column is sourced:

- **Required / optional arguments** come from the non-strict registry
  definitions, because the strict wire marks every key required.
- **Skill mentions** are word-boundary counts in `render_system_prompt()`,
  the text all three roles receive today. The matcher was controlled against
  a known positive and a known negative.
- **Live columns** are observational. Tools with no live calls were simply
  not exercised by the scenarios.

The plan to make this a generated, gated artifact is in
[remediation-plan.md](remediation-plan.md#tool-call-register-plan). Raw data
is in [evidence/tool-call-register.json](evidence/tool-call-register.json).

Live tool counts: tool loop **45**, planner **23**, escape hatch **3**, execute_tool registry **43**. Rendered skill: 135,863 characters.

## Register

| Tool | Kind | Loop | Planner | Hatch | Required args | Optional args | Skill mentions | Live calls (surface:n) | Results seen ok/fail* |
|---|---|---|---|---|---|---|---|---|---|
| `diff_pipeline` | discovery | ✓ | ✓ | — | — | — | 1 | tool_loop:4 | 0/4 |
| `explain_validation_error` | discovery | ✓ | ✓ | — | `error_text` | — | 5 | planner:1 | — |
| `get_audit_info` | discovery | ✓ | ✓ | — | — | — | 2 | planner:2 | 2/0 |
| `get_blob_content` | discovery | ✓ | ✓ | — | `blob_id` | `offset_chars`, `expected_content_hash` | 7 | planner:4, tool_loop:1 | 2/1 |
| `get_blob_metadata` | discovery | ✓ | ✓ | — | `blob_id` | — | 3 | planner:5 | 7/0 |
| `get_expression_grammar` | discovery | ✓ | ✓ | — | — | — | 4 | — | — |
| `get_pipeline_state` | discovery | ✓ | ✓ | — | — | `component` | 15 | tool_loop:11 | 11/0 |
| `get_plugin_assistance` | discovery | ✓ | ✓ | — | `plugin_type`, `plugin_name` | `issue_code` | 7 | planner:13, tool_loop:2 | 14/1 |
| `get_plugin_schema` | discovery | ✓ | ✓ | — | `plugin_type`, `name` | — | 7 | planner:99, tool_loop:18 | 113/4 |
| `inspect_source` | discovery | ✓ | ✓ | — | `blob_id` | `encoding` | 6 | planner:13 | 11/2 |
| `list_blobs` | discovery | ✓ | ✓ | — | — | — | 5 | planner:9 | 9/0 |
| `list_composer_blobs` | discovery | ✓ | ✓ | — | — | — | 2 | planner:8 | 8/0 |
| `list_models` | discovery | ✓ | ✓ | — | — | `provider`, `limit` | 3 | planner:2, tool_loop:5 | 7/0 |
| `list_secret_refs` | discovery | ✓ | ✓ | — | — | — | 2 | — | — |
| `list_sinks` | discovery | ✓ | ✓ | — | — | — | 4 | planner:2 | 2/0 |
| `list_sources` | discovery | ✓ | ✓ | — | — | — | 4 | planner:3 | 4/0 |
| `list_transforms` | discovery | ✓ | ✓ | — | — | — | 4 | planner:3 | 3/0 |
| `preview_pipeline` | discovery | ✓ | ✓ | — | — | — | 5 | tool_loop:16 | 16/0 |
| `read_attachment` | discovery | ✓ | ✓ | — | `blob_id` | `expected_content_hash` | 16 | — | — |
| `validate_secret_ref` | discovery | ✓ | ✓ | — | `name` | — | 1 | — | — |
| `clear_source` | mutation | ✓ | — ⚠ | — | — | `source_name` | 1 | — | — |
| `create_blob` | mutation | ✓ | — ⚠ | — | `filename`, `mime_type`, `content` | `description` | 8 | — | — |
| `delete_blob` | mutation | ✓ | — ⚠ | — | `blob_id` | — | 1 | — | — |
| `patch_node_options` | mutation | ✓ | — ⚠ | — | `node_id`, `patch` | — | 5 | tool_loop:6 | 5/1 |
| `patch_output_options` | mutation | ✓ | — ⚠ | — | `sink_name`, `patch` | — | 2 | tool_loop:6 | 4/2 |
| `patch_source_options` | mutation | ✓ | — ⚠ | — | `patch` | `source_name` | 2 | — | — |
| `remove_edge` | mutation | ✓ | — ⚠ | — | `id` | — | 1 | — | — |
| `remove_node` | mutation | ✓ | — ⚠ | — | `id` | — | 2 | tool_loop:1 | 1/0 |
| `remove_output` | mutation | ✓ | — ⚠ | — | `sink_name` | — | 1 | tool_loop:1 | 1/0 |
| `set_metadata` | mutation | ✓ | — ⚠ | — | `patch` | — | 1 | tool_loop:4 | 4/0 |
| `set_output` | mutation | ✓ | — ⚠ | — | `sink_name`, `plugin`, `options` | `on_write_failure`, `description` | 2 | tool_loop:6 | 6/0 |
| `set_pipeline` | mutation | ✓ | — ⚠ | — | `nodes`, `edges`, `outputs` | `source`, `sources`, `metadata` | 34 | tool_loop:6 | 5/1 |
| `set_source` | mutation | ✓ | — ⚠ | — | `plugin`, `on_success`, `options` | `source_name`, `on_validation_failure`, `description` | 4 | — | — |
| `set_source_from_blob` | mutation | ✓ | — ⚠ | — | `blob_id`, `on_success` | `source_name`, `plugin`, `on_validation_failure`, `options` | 5 | — | — |
| `set_source_from_blobs` | mutation | ✓ | — ⚠ | — | `blob_ids`, `on_success` | `source_name`, `on_validation_failure`, `options` | 3 | — | — |
| `splice_transform` | mutation | ✓ | — ⚠ | — | `predecessor_id`, `successor_id`, `node` | — | 2 | — | — |
| `update_blob` | mutation | ✓ | — ⚠ | — | `blob_id`, `content` | — | 1 | — | — |
| `upsert_edge` | mutation | ✓ | — ⚠ | — | `id`, `from_node`, `to_node`, `edge_type` | `label` | 5 | — | — |
| `upsert_node` | mutation | ✓ | — ⚠ | — | `id`, `node_type`, `input` | `plugin`, `on_success`, `on_error`, `options`, `condition`, `routes`, `fork_to`, `branches`, `policy`, `merge`, `trigger`, `output_mode`, `expected_output_count`, `timeout_seconds`, `description`, `scope_name`, `scope_opener`, `scope_policy` | 4 | tool_loop:17 | 17/0 |
| `wire_blob_inline_ref` | mutation | ✓ | — ⚠ | — | `field_path`, `blob_id` | `encoding` | 2 | — | — |
| `wire_secret_ref` | mutation | ✓ | — ⚠ | — | `name`, `target`, `option_key` | `target_id` | 1 | — | — |
| `request_interpretation_review` | session-aware | ✓ | — ⚠ | — | `affected_node_id`, `kind`, `user_term` | `llm_draft` | 19 | tool_loop:11 | 1/1 |
| `request_advisor_hint` | advisor | ✓ | — ⚠ | — | `trigger`, `problem_summary`, `recent_errors`, `attempted_actions` | `schema_excerpt` | 3 | tool_loop:1 | 0/0 |
| `emit_pipeline_proposal` | planner-terminal | — | ✓ | ✓ | `pipeline` | — | 0 | planner:53 | 0/16 |
| `handoff_to_user` | recovery-control | ✓ | ✓ | ✓ | `action`, `message` | — | 7 | — | — |
| `restore_parked_graph` | recovery-control | ✓ | ✓ | ✓ | `park_id`, `message` | — | 3 | — | — |

*Results are tool results observed in captured provider requests; a retried request can count a result twice.

⚠ = the tool is taught in the system prompt the planner receives, but the planner is not offered it (the planner is sent the full tool-loop skill).

## Flags per tool

| Tool | Strict schema | Cacheable discovery | Approval-required blob mutation | Redaction policy | Description (first sentence) |
|---|---|---|---|---|---|
| `diff_pipeline` | ✓ | — | — | ToolRedaction | Compare the current pipeline with the baseline supplied by this surface, which may be the request-start state |
| `explain_validation_error` | ✓ | ✓ | — | ToolRedaction | Get a human-readable explanation of a validation error with suggested fixes |
| `get_audit_info` | ✓ | ✓ | — | ToolRedaction | Return facts about ELSPETH's Landscape audit trail |
| `get_blob_content` | ✓ | — | — | ToolRedaction | Retrieve a blob for inspection: `blob_id`, `filename`, `mime_type`, and UTF-8 decoded `content` |
| `get_blob_metadata` | ✓ | — | — | ToolRedaction | Get metadata for a specific blob (file) by ID: `id`, `filename`, `mime_type`, `size_bytes`, `content_hash`, and `status`. |
| `get_expression_grammar` | ✓ | ✓ | — | ToolRedaction | Get the gate expression syntax reference |
| `get_pipeline_state` | ✓ | — | — | ToolRedaction | Inspect the full current pipeline state including all options for source, nodes, and outputs |
| `get_plugin_assistance` | ✓ | ✓ | — | ToolRedaction | Retrieve plugin-owned guidance for a source, transform, or sink |
| `get_plugin_schema` | ✓ | ✓ | — | ToolRedaction | Get the full configuration schema for a plugin |
| `inspect_source` | ✓ | — | — | ToolRedaction | Return bounded structural facts about a blob-backed source: `source_kind`, `observed_headers`, `runtime_headers`, `field_name_mapping` (raw CSV labels to runtime row keys), `sample_row_count`, `inferred_types` (lexical s |
| `list_blobs` | ✓ | — | — | ToolRedaction | List uploaded/created files (blobs) in this session with metadata: each entry carries `id`, `filename`, `mime_type`, `size_bytes`, `status`, `created_by`, and `creation_modality` |
| `list_composer_blobs` | ✓ | — | — | ToolRedaction | List ready blobs available for audited inline-content authoring |
| `list_models` | ✓ | ✓ | — | ToolRedaction | List available LLM model identifiers |
| `list_secret_refs` | ✓ | — | — | ToolRedaction | List available secret references (API keys, credentials) |
| `list_sinks` | ✓ | ✓ | — | ToolRedaction | List available sink plugins |
| `list_sources` | ✓ | ✓ | — | ToolRedaction | List available source plugins |
| `list_transforms` | ✓ | ✓ | — | ToolRedaction | List available transform plugins |
| `preview_pipeline` | ✓ | — | — | ToolRedaction | Preview the current pipeline without executing it |
| `read_attachment` | ✓ | — | — | ToolRedaction | Read ONE authorized session attachment's complete UTF-8 text in ONE tool call |
| `validate_secret_ref` | ✓ | — | — | ToolRedaction | Check if a secret reference exists and is accessible to the current user |
| `clear_source` | ✓ | — | — | ToolRedaction | Remove a named source from the pipeline composition state. |
| `create_blob` | ✓ | — | — | ToolRedaction | Create a new file (blob) from inline content |
| `delete_blob` | ✓ | — | ✓ | ToolRedaction | Delete a blob (file) and its storage |
| `patch_node_options` | — | — | — | ToolRedaction | Apply a shallow merge-patch to a node's options |
| `patch_output_options` | — | — | — | ToolRedaction | Apply a shallow merge-patch to an output's options |
| `patch_source_options` | — | — | — | ToolRedaction | Apply a shallow merge-patch to a named source's options |
| `remove_edge` | ✓ | — | — | ToolRedaction | Remove an edge by ID. |
| `remove_node` | ✓ | — | — | ToolRedaction | Remove a node and all its edges. |
| `remove_output` | ✓ | — | — | ToolRedaction | Remove a pipeline output (sink) by name. |
| `set_metadata` | ✓ | — | — | ToolRedaction | Update pipeline metadata (name and description only). |
| `set_output` | — | — | — | ToolRedaction | Add or replace a pipeline output (sink). |
| `set_pipeline` | — | — | — | ToolRedaction | Atomically create or fully rebuild a pipeline |
| `set_source` | — | — | — | ToolRedaction | Set or replace a named pipeline source. |
| `set_source_from_blob` | — | — | — | ToolRedaction | Wire a blob as the pipeline source |
| `set_source_from_blobs` | — | — | — | ToolRedaction | Wire one or more READY session blobs as a blob_rows source: one blob becomes one row carrying its payload hash and bounded metadata, in the given order |
| `splice_transform` | — | — | — | ToolRedaction | Insert one transform between a predecessor and successor on an existing direct linear on_success path |
| `update_blob` | ✓ | — | ✓ | ToolRedaction | Update the content of an existing blob (file) |
| `upsert_edge` | ✓ | — | — | ToolRedaction | Add or update a connection between nodes |
| `upsert_node` | — | — | — | ToolRedaction | Add or update a pipeline node |
| `wire_blob_inline_ref` | ✓ | — | — | ToolRedaction | Author a widened blob_ref inline_content marker at a canonical field_path |
| `wire_secret_ref` | ✓ | — | — | ToolRedaction | Place a secret reference marker in the pipeline config |
| `request_interpretation_review` | ✓ | — | — | ToolRedaction | Ask the user to review an LLM-authored assumption before it is finalised into the pipeline |
| `request_advisor_hint` | ✓ | — | — | ToolRedaction | ESCAPE HATCH — use cannot_complete as soon as missing or contradictory information, an unavailable capability, or an unreasonable requirement prevents a faithful result |
| `emit_pipeline_proposal` | — | — | — | — | Return one complete canonical pipeline proposal for server validation, alone in its tool-call turn |
| `handoff_to_user` | ✓ | — | — | — | End authoring immediately and talk to the user |
| `restore_parked_graph` | ✓ | — | — | — | Restore the exact advertised parked graph and end this turn with your message |

## Teaching mismatches

Tools named in the planner's system prompt that the planner cannot call (name: mentions):

- `set_pipeline`: 34
- `request_interpretation_review`: 19
- `create_blob`: 8
- `patch_node_options`: 5
- `set_source_from_blob`: 5
- `upsert_edge`: 5
- `set_source`: 4
- `upsert_node`: 4
- `request_advisor_hint`: 3
- `set_source_from_blobs`: 3
- `patch_output_options`: 2
- `patch_source_options`: 2
- `remove_node`: 2
- `set_output`: 2
- `splice_transform`: 2
- `wire_blob_inline_ref`: 2
- `clear_source`: 1
- `delete_blob`: 1
- `remove_edge`: 1
- `remove_output`: 1
- `set_metadata`: 1
- `update_blob`: 1
- `wire_secret_ref`: 1

Tools offered to the planner but never named in its system prompt: `emit_pipeline_proposal`

Tools named in the tool-loop system prompt that the loop cannot call: none

Tools offered to the tool loop but never named in its system prompt: none

Live calls to a tool the surface did not offer: 0

Live tool names not in any registry: none

## Live failure samples


### `diff_pipeline`

```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "rejected_mutation", "message": "No baseline available. Load or create a session first.", "severity": "high", "error_code": "diff_baseline_unavailable"}], "warnings": [{"component": "output:orders_with_gst", "message": "Output 'orders_with_gst' uses plugin 'json' but filename extension suggests a different format.", "severity": "low"}], "suggestions": [], "semantic_contracts": [], "graph_repair_suggestions": []}, "affected_nodes": [], "version": 16}
```
```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "rejected_mutation", "message": "No baseline available. Load or create a session first.", "severity": "high", "error_code": "diff_baseline_unavailable"}], "warnings": [{"component": "node:classify_comments", "message": "LLM node 'classify_comments' has no authorized prompt-injection shield in front of it. This pipeline has no declared untrusted-content producer upstream. Operator-supplied or uploaded data is not necessarily trusted. If this LLM consumes untrusted or externally controlled text, stage the required prom…
```
```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "rejected_mutation", "message": "No baseline available. Load or create a session first.", "severity": "high", "error_code": "diff_baseline_unavailable"}], "warnings": [{"component": "node:classify_feedback", "message": "LLM node 'classify_feedback' has no authorized prompt-injection shield in front of it. This pipeline has no declared untrusted-content producer upstream. Operator-supplied or uploaded data is not necessarily trusted. If this LLM consumes untrusted or externally controlled text, stage the required prom…
```

### `get_blob_content`

```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "source", "message": "No source configured.", "severity": "high", "error_code": "no_source_configured"}, {"component": "pipeline", "message": "No sinks configured.", "severity": "high", "error_code": "no_sinks_configured"}], "warnings": [], "suggestions": [], "semantic_contracts": [], "graph_repair_suggestions": []}, "affected_nodes": [], "version": 1, "data": {"argument_error": {"component": "tool arguments", "severity": "high", "error_code": "SCHEMA_VALIDATION", "error_class": "ToolArgumentError", "error_category":…
```

### `get_plugin_assistance`

```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "source", "message": "No source configured.", "severity": "high", "error_code": "no_source_configured"}, {"component": "pipeline", "message": "No sinks configured.", "severity": "high", "error_code": "no_sinks_configured"}], "warnings": [], "suggestions": [], "semantic_contracts": [], "graph_repair_suggestions": []}, "affected_nodes": [], "version": 1, "data": {"argument_error": {"component": "tool arguments", "severity": "high", "error_code": "SCHEMA_VALIDATION", "error_class": "ToolArgumentError", "error_category":…
```

### `get_plugin_schema`

```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "rejected_mutation", "message": "transform plugin selection is unavailable (plugin_not_enabled): the plugin is installed but not turned on in this deployment's plugin policy; an operator must enable it", "severity": "high", "error_code": "plugin_not_enabled"}, {"component": "source", "message": "No source configured.", "severity": "high", "error_code": "no_source_configured"}, {"component": "pipeline", "message": "No sinks configured.", "severity": "high", "error_code": "no_sinks_configured"}], "warnings": [], "sugge…
```
```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "rejected_mutation", "message": "transform plugin selection is unavailable (plugin_not_enabled): the plugin is installed but not turned on in this deployment's plugin policy; an operator must enable it", "severity": "high", "error_code": "plugin_not_enabled"}, {"component": "source", "message": "No source configured.", "severity": "high", "error_code": "no_source_configured"}, {"component": "pipeline", "message": "No sinks configured.", "severity": "high", "error_code": "no_sinks_configured"}], "warnings": [], "sugge…
```
```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "rejected_mutation", "message": "transform plugin selection is unavailable (plugin_not_enabled): the plugin is installed but not turned on in this deployment's plugin policy; an operator must enable it", "severity": "high", "error_code": "plugin_not_enabled"}, {"component": "source", "message": "No source configured.", "severity": "high", "error_code": "no_source_configured"}, {"component": "pipeline", "message": "No sinks configured.", "severity": "high", "error_code": "no_sinks_configured"}], "warnings": [], "sugge…
```

### `inspect_source`

```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "rejected_mutation", "message": "CSV inspection requires a supported text encoding.", "severity": "high"}, {"component": "source", "message": "No source configured.", "severity": "high", "error_code": "no_source_configured"}, {"component": "pipeline", "message": "No sinks configured.", "severity": "high", "error_code": "no_sinks_configured"}], "warnings": [], "suggestions": [], "semantic_contracts": [], "graph_repair_suggestions": []}, "affected_nodes": [], "version": 1}
```
```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "rejected_mutation", "message": "CSV inspection requires a supported text encoding.", "severity": "high"}, {"component": "source", "message": "No source configured.", "severity": "high", "error_code": "no_source_configured"}, {"component": "pipeline", "message": "No sinks configured.", "severity": "high", "error_code": "no_sinks_configured"}], "warnings": [], "suggestions": [], "semantic_contracts": [], "graph_repair_suggestions": []}, "affected_nodes": [], "version": 1}
```

### `patch_node_options`

```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "rejected_mutation", "message": "Authoritative interpretation-review reconciliation failed: prompt part references unknown interpretation requirement 'angry or urgent:flag_comment_tone'; valid requirement IDs: ['prompt_injection_shield_recommendation:flag_comment_tone', 'prompt_template_review:flag_comment_tone', 'urgency:flag_comment_tone']. Repair the rejected candidate's prompt_template_parts using one of its exact valid requirement IDs, or add the intended authorable requirement shell to that candidate. Node requ…
```

### `patch_output_options`

```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "rejected_mutation", "message": "Invalid options for sink 'csv': description: Extra inputs are not permitted", "severity": "high", "error_code": "plugin_options_invalid", "rejected_component": "output:large_expenses"}], "warnings": [], "suggestions": [], "semantic_contracts": [], "graph_repair_suggestions": []}, "affected_nodes": [], "version": 3, "plugin_schemas": {"sink/csv": {"name": "csv", "plugin_type": "sink", "description": "Write rows to a CSV file.\n\nReturns ArtifactDescriptor with SHA-256 content hash for …
```
```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "rejected_mutation", "message": "Invalid options for sink 'csv': description: Extra inputs are not permitted", "severity": "high", "error_code": "plugin_options_invalid", "rejected_component": "output:routine_expenses"}], "warnings": [], "suggestions": [], "semantic_contracts": [], "graph_repair_suggestions": []}, "affected_nodes": [], "version": 3, "plugin_schemas": {"sink/csv": {"name": "csv", "plugin_type": "sink", "description": "Write rows to a CSV file.\n\nReturns ArtifactDescriptor with SHA-256 content hash fo…
```

### `set_pipeline`

```json
{"success": false, "validation": {"is_valid": false, "errors": [{"component": "rejected_mutation", "message": "Source 'source': Invalid options for source 'csv': path: Field required\ninline_blob: Extra inputs are not permitted", "severity": "high", "error_code": "plugin_options_invalid", "rejected_component": "source"}], "warnings": [], "suggestions": [], "semantic_contracts": [], "graph_repair_suggestions": []}, "affected_nodes": [], "version": 2, "plugin_schemas": {"source/csv": {"name": "csv", "plugin_type": "source", "description": "Load rows from a CSV file.\n\nConfig options:\n    path:…
```

### `request_interpretation_review`

```json
{"error": "Tool 'request_interpretation_review' failed: 'affected_node_id' must be a pending pipeline_decision interpretation requirement, got missing pending pipeline_decision review site"}
```

### `emit_pipeline_proposal`

```json
{"success":false,"validation":{"errors":[{"component":"source:colours_in","detail":"Source 'colours_in': Invalid options for source 'json': path: Field required","error_class":"ValidationError","error_code":"plugin_options_invalid","explanation":"One of the component's options failed its plugin schema. This code alone does not identify WHICH option: the rejection's 'detail' field carries the validator's own message naming the exact option and requirement — read it before hypothesising a cause.","plugin_contracts":[{"aggregation_output_modes":[],"composer_hints":["Format auto-detects from exten…
```
```json
{"success":false,"validation":{"errors":[{"component":"pipeline","error_class":"SchemaValidationError","error_code":"canonical_schema","schema_violations":[{"path":"sources/item","rule":"additionalProperties"}],"severity":"high"}],"is_valid":false}}
```
```json
{"success":false,"validation":{"errors":[{"component":"source","detail":"Source 'source': Invalid options for source 'json': schema: Value error, Observed schemas (mode: observed) cannot have explicit field definitions. Use guaranteed_fields/required_fields for contracts instead.","error_class":"ValidationError","error_code":"plugin_options_invalid","explanation":"One of the component's options failed its plugin schema. This code alone does not identify WHICH option: the rejection's 'detail' field carries the validator's own message naming the exact option and requirement — read it before hypo…
```
