# `upsert_edge` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Add or replace a diagram relationship and, only for supported sink-target routes, update the corresponding executable producer routing slot. The orchestrator must understand this distinction before selecting the tool.

Select for a visual relationship or direct sink routing change. To connect processing nodes, their input/on_success connection strings must already be or separately be authored consistently; drawing an edge alone does not wire runtime execution.

## 2. Contract

Required id,from_node,to_node strings and edge_type enum on_success/on_error/route_true/route_false/fork. label is optional null|string. The shared lowering matrix admits source/node/output combinations. on_error sink wiring accepts transforms/aggregations, not gate expression errors. A sink-routing scalar slot can have only one edge, with fork uniqueness per destination. Replacing an edge ID clears the former sink route unless another legacy edge still expresses it, then applies the new route. Result returns affected endpoints, applied component echo, validation and version changes from immutable graph helpers; it does not execute a graph or plugin. Same-ID replay is not a no-version-change guarantee. No cache/automatic retry; explicit approval applies.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/transforms.py:464](../../../../src/elspeth/web/composer/tools/transforms.py#L464) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1510](../../../../src/elspeth/web/composer/tools/transforms.py#L1510) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:978](../../../../src/elspeth/web/composer/tools/transforms.py#L978) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1068](../../../../src/elspeth/web/composer/tools/transforms.py#L1068) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/state.py:3464](../../../../src/elspeth/web/composer/state.py#L3464) — shared component-kind and edge-lowering contract.
- [src/elspeth/web/composer/state.py:8259](../../../../src/elspeth/web/composer/state.py#L8259) — immutable persisted composition model.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

Unknown/unlowerable endpoints or illegal source/kind/edge combinations refuse edge_not_lowerable. Conflicting sink route edges refuse edge_route_conflict. Replacement updates diagram and scalar mirrors before invariant/review checks; reconciliation failure returns original state. Edges between processing nodes remain visual, even when their connection strings disagree; validation reports resulting disagreement. Gate expression-error policy must use upsert_node.on_error. Removing/replacing a sink edge can clear node/source routes, unlike visual-only edge changes. A successful edit does not certify runtime-ready configuration.

## 5. Usability assessment

**SG-01 — intended dual authority with a material orchestration cost; high confidence.** One verb means display-only relation for processing edges but executable routing change for sink edges. The description now discloses it, and tests explicitly pin non-sink no-sync behaviour; this is not an undiscovered implementation bug. Nevertheless, the model must reason over two representations and choose a different operation for equivalent-looking edits. Workaround: inspect runtime connection fields, author them explicitly, and use preview rather than infer readiness from the diagram.

## 6. Structural remediation

Make executable connection identity authoritative and derive visual edges, or explicitly type visual annotations separately from executable route edits. Do not silently change existing non-sink edges into runtime wiring. Provide model-selected atomic connect/rewire operations over one graph contract, with typed effects and review invalidation. Regression: two processing nodes are connected only by the declared runtime operation; diagrams and runtime sinks agree after add/replace/remove; wrong-kind routes, duplicate slots and gate error-edge misuse reject atomically; review hashes reopen when upstream dataflow changes. Compatibility risk high because saved edges and scalar routes coexist; migrate by preserve/refuse inspection, not heuristic rewiring.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_tools_mutations.py:1511](../../../../tests/unit/web/composer/test_tools_mutations.py#L1511) — edge creation and sink success/error mirrors.
- [tests/unit/web/composer/test_tools_mutations.py:1612](../../../../tests/unit/web/composer/test_tools_mutations.py#L1612) — gate error edge rejection.
- [tests/unit/web/composer/test_tools_mutations.py:1940](../../../../tests/unit/web/composer/test_tools_mutations.py#L1940) — test_edge_to_node_does_not_sync.
- [tests/unit/web/composer/test_tools_mutations.py:1982](../../../../tests/unit/web/composer/test_tools_mutations.py#L1982) — already-matching sink route avoids an extra helper version bump.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.transforms._handle_upsert_edge`](../../../../src/elspeth/web/composer/tools/transforms.py#L456).

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
    "id": {
      "type": "string",
      "description": "Unique edge identifier."
    },
    "from_node": {
      "type": "string",
      "description": "Source node ID or 'source'."
    },
    "to_node": {
      "type": "string",
      "description": "Destination node ID or sink name."
    },
    "edge_type": {
      "type": "string",
      "enum": [
        "on_success",
        "on_error",
        "route_true",
        "route_false",
        "fork"
      ],
      "description": "Connection kind. Use on_success for a direct success relationship; on_error only from a transform or aggregation to a sink; route_true or route_false for gate routes; fork for a gate fan-out relationship. Gate expression errors use the gate node's on_error field instead. Edges targeting sinks also update the producer's corresponding runtime routing field; other edges are display relationships and do not replace node connection fields."
    },
    "label": {
      "type": [
        "string",
        "null"
      ],
      "description": "Display label."
    }
  },
  "required": [
    "id",
    "from_node",
    "to_node",
    "edge_type"
  ],
  "additionalProperties": false,
  "examples": [
    {
      "id": "e_judge_layers_error",
      "from_node": "judge_layers",
      "to_node": "llm_failures",
      "edge_type": "on_error",
      "label": "LLM failures"
    }
  ]
}
```
