# `remove_edge` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Remove one explicit diagram edge and clear its executable scalar route only when it represents a supported sink route no longer expressed elsewhere.

Select for intentional relationship removal. A non-sink visual edge deletion does not disconnect processing-node runtime connection strings; the orchestrator must choose the appropriate route edit separately.

## 2. Contract

Required id:string. The handler locates the EdgeSpec or returns edge_not_found unchanged, removes it, conditionally clears sink-route mirrors, reconciles reviews and returns affected endpoints plus validation/new version. Remaining legacy duplicate edges can preserve the route they still express. No blob/plugin/external action occurs. Repeating after successful removal fails; no retry receipt or no-op success is provided. Accepted deletion can leave graph-invalid routes, which remain visible in validation. No cache; public explicit approval applies.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/transforms.py:541](../../../../src/elspeth/web/composer/tools/transforms.py#L541) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1648](../../../../src/elspeth/web/composer/tools/transforms.py#L1648) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:962](../../../../src/elspeth/web/composer/tools/transforms.py#L962) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1068](../../../../src/elspeth/web/composer/tools/transforms.py#L1068) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/state.py:8271](../../../../src/elspeth/web/composer/state.py#L8271) — immutable persisted composition model.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

Removing a source-to-sink success edge clears its source scalar; removing transform/aggregation success/error sink edges clears those slots; gate sink route/fork mirrors use kind-specific rules. Non-sink edges are annotations relative to runtime connection strings. Reconciliation reopens impacted review anchors; failure returns the old state. A removed edge does not itself delete a node or an output. This tool's successful result concerns mutation admission, not proof that the path was runtime-disconnected.

## 5. Usability assessment

**SG-01 — intended split semantics; high confidence.** Visually identical removal gestures can change runtime for a sink edge while merely changing the display for a processing edge. Existing tests prove sink clearing, but that does not establish uniform disconnect semantics. Workaround: inspect surviving producer/consumer connection fields, then author the intended runtime edit and preview it. No claim of a corrupted sink route was demonstrated.

## 6. Structural remediation

Separate annotation removal from executable disconnect in the graph contract, sharing route-slot ownership with upsert_edge and deriving visual state from accepted runtime relationships where possible. Define legacy duplicate resolution explicitly. Regression: disconnect clears only the intended runtime slot; retained duplicate semantics are preserved until migration; processing-edge annotation removal never claims runtime disconnection; missing IDs refuse; review invalidation is consistent; every rejection retains state/version. Compatibility risk high for saved dual representations; use explicit versioning/adapters rather than reinterpret existing edges silently.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_tools_mutations.py:2071](../../../../tests/unit/web/composer/test_tools_mutations.py#L2071) — edge removal/missing cases.
- [tests/unit/web/composer/test_tools_mutations.py:2097](../../../../tests/unit/web/composer/test_tools_mutations.py#L2097) — node runtime sink route clearing.
- [tests/unit/web/composer/test_tools_mutations.py:2144](../../../../tests/unit/web/composer/test_tools_mutations.py#L2144) — source runtime route clearing.
- [tests/unit/web/composer/test_tools_mutations.py:2184](../../../../tests/unit/web/composer/test_tools_mutations.py#L2184) — gate route/fork clearing.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.transforms._handle_remove_edge`](../../../../src/elspeth/web/composer/tools/transforms.py#L533).

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
      "description": "Edge ID to remove."
    }
  },
  "required": [
    "id"
  ],
  "additionalProperties": false
}
```
