# `remove_node` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Remove one node and every explicit edge touching it. It is deletion, not a model-independent bypass or reconnect operation.

Select when a requested component should disappear. The orchestrator must plan the resulting runtime connection changes and downstream review consequences.

## 2. Contract

Required id:string; unknown keys reject. Missing node returns node_not_found and original state/version. Existing removal drops that NodeSpec and its incident EdgeSpecs, increments version once, reports the removed node and visual neighbours, and reconciles reviews. It does not rewrite surviving input/on_success/on_error/routes/fork/branch/scope references or create a replacement edge. It never calls a plugin or deletes source/blob/output artifacts. A repeated removal is a failed call, not idempotent success. Validation can report an incomplete graph after successful deletion; public explicit approval applies.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/transforms.py:517](../../../../src/elspeth/web/composer/tools/transforms.py#L517) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1619](../../../../src/elspeth/web/composer/tools/transforms.py#L1619) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1594](../../../../src/elspeth/web/composer/tools/transforms.py#L1594) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/state.py:8250](../../../../src/elspeth/web/composer/state.py#L8250) — immutable persisted composition model.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

Normal deletion preserves all unrelated nodes and outputs. Runtime consumers that read the removed node's connection may dangle; removed scope openers or barriers can leave structural obligations for validation. Review reconciliation reopens stale downstream evidence or rejects malformed retained authority. If reconciliation fails, the tool returns the original immutable state. Affected_nodes is collected from visual edges, so it is an edit/projection aid rather than a complete semantic dependency inventory. Missing node rejection must not be confused with already-removed receipt recovery.

## 5. Usability assessment

**SG-01 — source-established deletion limitation; high confidence.** Removing all incident diagram edges does not repair runtime connection fields. That is a legitimate draft operation, but an orchestrator cannot interpret the response's visual affected_nodes as all runtime dependants. **SG-06 — shared graph/review contract need.** Direct graph deletion and bulk replacement should compute the same affected review anchors. Workaround: inspect validation plus full runtime connections after deletion; explicitly reconnect or remove downstream consumers using model-authored edits.

## 6. Structural remediation

Define a graph deletion candidate with separate deleted components, dangling runtime dependants, visual changes and invalidated reviews. A model-selected bypass/rewire command can be added as a distinct operation with explicit preconditions, rather than make remove_node invent intent. Regression: deletion removes only intended node/incident edges, reports semantic dependants even without visual edges, reopens upstream-bound reviews, refuses malformed authority without partial state, and distinguishes missing-node rejection from completed operation receipt. Compatibility risk medium/high if affected_nodes or deletion cascades change; retain current deletion semantics during introduction.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_tools_mutations.py:2026](../../../../tests/unit/web/composer/test_tools_mutations.py#L2026) — test_removes_node_and_edges.
- [tests/unit/web/composer/test_tools_mutations.py:2062](../../../../tests/unit/web/composer/test_tools_mutations.py#L2062) — missing-node refusal.
- [tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py:1](../../../../tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py#L1) — graph mutation review reconciliation.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.transforms._handle_remove_node`](../../../../src/elspeth/web/composer/tools/transforms.py#L509).

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
      "description": "Node ID to remove."
    }
  },
  "required": [
    "id"
  ],
  "additionalProperties": false
}
```
