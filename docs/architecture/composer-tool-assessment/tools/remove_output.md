# `remove_output` — delete one authored sink instance

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only. **Overall confidence: High for source and focused mutation tests.**

## 1. Purpose and usage

Select this tool when a named sink instance should be removed from the authored pipeline. It is a graph edit, not deletion of produced files, blobs, remote records, or audit evidence. It is appropriate during route restructuring, after inspecting references to the sink's connection name. Available through web Composer and common Composer MCP dispatch. The sink must exist in the current composition; the handler has no external service prerequisites.

## 2. Contract

The only argument is required `sink_name: string`; extra arguments are forbidden. Existing output removal returns a successful mutation with incremented composition version and `affected_nodes=(sink_name,)`. The applied component may be absent because the target no longer exists; current validation/delta describes the surviving graph. Missing output returns `output_not_found`, with the untouched prior state and a rejected component identity.

Removal does not cascade into source/node success routes, error routes, or sink-diversion references. It can therefore succeed as an editor action while leaving invalid graph validation. No automatic replacement sink or guessed route is generated. It does not inspect a sink plugin schema or instantiate the plugin; deletion of a configuration must remain possible even when the former implementation is unavailable.

This is uncached `MUTATION` with graph/validation/YAML effects. Web explicit-approval mode proposes the edit; auto-commit applies an admitted edit. A successful call changes version, but a second identical call fails missing-output: no idempotent delete guarantee exists at the semantic tool level. Calls are serialized by the enclosing session operation authority, not by local mutable global state. The orchestrator must preserve returned state/outcome across later failures and must not infer external artifact deletion.

## 3. Implementation trace

The [argument model, declaration, and wrapper](../../../../src/elspeth/web/composer/tools/outputs.py#L58) register through [_registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111) and [get_tool_definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311). [`_execute_remove_output`](../../../../src/elspeth/web/composer/tools/outputs.py#L225) admits the model, calls [`CompositionState.without_output`](../../../../src/elspeth/web/composer/state.py#L8290), and returns either typed saved-graph rejection or [`_mutation_result`](../../../../src/elspeth/web/composer/tools/_common.py#L1712).

[Public dispatch](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) checks closed schema, credentials, catalog snapshot, and interpretation invariants; [finalization](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752) preserves prior validation and normalizes surviving graph diagnostics. [Web tool-batch execution](../../../../src/elspeth/web/composer/tool_batch.py#L3008) and approval orchestration own persistence/publication; [audit](../../../../src/elspeth/web/composer/audit.py#L812) records semantic and dispatch outcomes; [frontend cards](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L75) present pending/applied/failed outcomes. [MCP dispatch](../../../../src/elspeth/composer_mcp/server.py#L363) uses the same mutation with its own session checkout/persistence authority.

## 4. Alternative paths

Removing an existing terminal output changes only the outputs tuple. Existing producer routes remain available for the model to inspect and repair. A missing sink fails cleanly rather than silently claiming a deletion. Whole-graph or interpretation constraints can still reject publication through the shared dispatcher. Under explicit approval, the proposed action is not applied until accepted against current session state. Subsequent validation failure does not roll back previously accepted unrelated edits; it reports the draft's actual state.

## 5. Usability assessment

No confirmed defect was demonstrated in the scoped review. **Intended restriction/editor behaviour:** removal does not repair consumers/producers automatically, and successful authoring does not imply a runnable graph. The model-facing description is terse and does not explicitly mention dangling routes or repeated-call failure. Those are useful guidance improvements, but automatic rewiring would violate planner ownership and should not be proposed as the fix.

Workaround: inspect routes and validation before/after removing, then author explicit model-chosen repairs. If a completion/result was lost, inspect current state before replaying; missing-output on retry can describe an already applied prior operation rather than a new impossible request.

## 6. Structural remediation

Make edit outcome, readiness, and retry semantics explicit in a common mutation contract. Provide model-visible reference impact information before destructive graph edits, derived from the graph authority, while leaving repair decisions with the model. A shared state/effect receipt should distinguish never-applied, applied, and replayed removal without making missing targets silently successful by default.

Acceptance: valid removal deletes exactly the named output and leaves other components byte-equivalent; upstream references are reported accurately and never rewritten server-side; absent target and malformed arguments reject; explicit approval protects the exact current candidate; replay and concurrent edit scenarios have truthful outcomes; graph-invalid drafts cannot execute. Compatibility risk is Low for description/impact additions and Medium for changed missing-target/idempotency semantics. Preserve existing graph and artifact retention boundaries.

## 7. Evidence, gaps, and caveats

Read [mutation success/version sequence](../../../../tests/unit/web/composer/test_output_rejection_contracts.py#L322), [saved-findings rejection tests](../../../../tests/unit/web/composer/test_output_rejection_contracts.py#L91), and state/dispatch code. Included in the seven-file focused selection: exit 0, **448 passed, 43 warnings, 66.95s**. No external artifact deletion, browser, live provider, PostgreSQL, or concurrency experiment ran for this tool. The impact-query/receipt design remains proposed; current noncascading deletion is not classified as a defect.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.outputs._handle_remove_output`](../../../../src/elspeth/web/composer/tools/outputs.py#L113).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | no |
| Composer MCP | yes |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "sink_name": {
      "type": "string",
      "description": "Sink name to remove."
    }
  },
  "required": [
    "sink_name"
  ],
  "additionalProperties": false
}
```
