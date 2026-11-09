# `clear_source` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Remove exactly one named source root from composition state. The orchestrator selects which root to remove and must decide how the remaining graph will be repaired.

Use for intentional source deletion or unbinding before blob replacement. Requires the requested source to exist; source_name omission targets the default source root only.

## 2. Contract

Arguments: optional source_name:string, default source, minimum length1; no other keys. The handler does a named map removal, increments the version once and reports the source component affected. It does not delete the blob, source file, nodes, edges, outputs or their connection strings. Missing root returns source_not_found and preserves exact state/version. Repeated deletion is a failure rather than idempotent success. Successful deletion can make the pipeline invalid, with that invalidity returned in validation. No automatic reconnect/retry or external I/O; public explicit-approve mode stages the deletion for approval.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/sources.py:2338](../../../../src/elspeth/web/composer/tools/sources.py#L2338) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:2310](../../../../src/elspeth/web/composer/tools/sources.py#L2310) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/state.py:8228](../../../../src/elspeth/web/composer/state.py#L8228) — immutable persisted composition model.
- [src/elspeth/web/composer/tools/declarations.py:183](../../../../src/elspeth/web/composer/tools/declarations.py#L183) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

Normal named removal preserves other roots. Deleting a source still used by nodes leaves dangling consumers/visual edges; the tool does not invent substitute dataflow. Public dispatch's post-mutation interpretation gate can refuse retained noncanonical requirements. The handler itself does not perform the review-reconciliation sequence found in replacement/patch/source-binding and graph mutation tools. Whether an inherited downstream resolved review survives this deletion in a way later admission cannot repair needs a specific reviewed-source reproduction; no bypass is established here. The current source removal method is deliberately smaller than graph repair.

## 5. Usability assessment

**SG-01 — source-established graph edit limitation; high confidence.** A source-map removal leaves diagram and runtime dependencies for later repair. This is permissible draft editing, but success is not graph readiness. **SG-06 — unresolved parity hypothesis; moderate confidence.** clear_source omits authoritative review reconciliation while related graph-only mutations invoke it; the review anchor consequences require further verification. Workaround: inspect validation after deletion, remove/reconnect affected paths explicitly, and obtain renewed review before execution.

## 6. Structural remediation

Define deletion as a typed graph operation returning removed identity, broken connection dependants and invalidated review anchors. Use the same review reconciliation/authority admission as other graph edits, while retaining explicit draft-incomplete semantics and leaving reconnection choices to the model. Regression: remove one of several roots and verify untouched roots; reject absent/invalid identities; keep blob bytes; report all dangling dependants; prove resolved source/downstream reviews are reopened or execution refuses with a repairable reason. Compatibility risk medium: changing from failure-on-second-delete or adding automatic cascade would alter operator behaviour and needs a deliberate decision.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_tools_mutations.py:653](../../../../tests/unit/web/composer/test_tools_mutations.py#L653) — test_named_source_patch_and_clear_target_only_selected_source.
- [tests/unit/web/composer/test_tools_mutations.py:4443](../../../../tests/unit/web/composer/test_tools_mutations.py#L4443) — clear_source success/missing tests.
- [tests/unit/web/composer/test_source_rejection_contracts.py:125](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L125) — saved-findings preservation on clear refusal.
- [tests/unit/web/composer/test_source_rejection_contracts.py:662](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L662) — named mutation version sequence.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.sources._handle_clear_source`](../../../../src/elspeth/web/composer/tools/sources.py#L2330).

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
    "source_name": {
      "type": "string",
      "description": "Source root name to clear. Defaults to 'source'.",
      "minLength": 1,
      "default": "source"
    }
  },
  "required": [],
  "additionalProperties": false
}
```
