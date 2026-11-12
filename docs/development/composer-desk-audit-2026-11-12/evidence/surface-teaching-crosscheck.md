# Per-surface teaching cross-check

Source texts: `render_system_prompt(None)` (tool loop), plus the captured system messages of a live planner request and a live advisor request.

- Tool-loop system prompt: 135,863 chars, sha256 `2e4c56762a74…`
- Planner system message (live capture): 135,863 chars, sha256 `2e4c56762a74…`, identical to the tool-loop prompt: **True**
- Advisor system message (live capture): 139,987 chars; begins with the full tool-loop prompt: **True**; advisor-specific suffix 4,124 chars

Counts: loop 45 tools, planner 23, escape hatch 3, `execute_tool` registry 43.

## Taught to the planner but not offered (word-boundary mentions)

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

Total: 23 tools, 109 mentions.

Offered to the planner but never named in its system message: `emit_pipeline_proposal`. It is introduced only by a single sentence in the planner's final user message.

Tool loop, taught but not offered: none. Offered but not taught: none.

## Identifier-level check

Of 248 backticked snake_case identifiers in the tool-loop prompt, 7 appear nowhere in `src/elspeth` string constants, identifiers, YAML, the tool schemas or the planner context: `animal_type`, `approval_status`, `branch_one`, `branch_two`, `classify_pcs_lh`, `service_type`, `teeth_count`. All 7 are illustrative example values in worked examples, not phantom features. The instrument was controlled with a known positive (`set_pipeline`) and a known negative (`nonexistent_tool_xyz`).

## Live

- Calls to a tool the surface did not offer: **0**.
- Tool names outside every registry: **0**.
- Planner calls whose reasoning works around the palette ("proposal-only", "no request_interpretation_review", "not available in this palette", …): **66 of 110 (60%)**. The regex was controlled on a positive and a negative sentence.
