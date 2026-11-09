# `set_metadata` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Apply a partial pipeline name/description update. This affects presentation, not plugin configuration or executable connections.

Select when the user changes pipeline naming or explanatory metadata. Requires no plugin, blob, secret or runtime dependency beyond the standard composition context.

## 2. Contract

Required patch:object; only name:string and description:string are allowed and both are optional within it. Included strings replace values; omitted fields preserve values. Null is not a declared field value, and this is not null-deletes merge patch. Empty object is admitted. Result reports no affected nodes, carries current graph validation and always constructs a state with version+1. No plugin call or external write occurs inside the handler; durable save/approval belongs to the enclosing route. No cache or automatic retry; exact repetition is content-idempotent but still advances version.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/transforms.py:147](../../../../src/elspeth/web/composer/tools/transforms.py#L147) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:565](../../../../src/elspeth/web/composer/tools/transforms.py#L565) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1676](../../../../src/elspeth/web/composer/tools/transforms.py#L1676) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/state.py:8297](../../../../src/elspeth/web/composer/state.py#L8297) — immutable persisted composition model.
- [src/elspeth/web/composer/discovery_cache.py:38](../../../../src/elspeth/web/composer/discovery_cache.py#L38) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/tool_batch.py:3333](../../../../src/elspeth/web/composer/tool_batch.py#L3333) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

A partial name update preserves description and vice versa. Empty patch preserves all content while advancing version; an unchanged string does the same. Unknown metadata keys and wrong types reject at schema/model admission. Existing graph invalidity remains in the response even though it did not prevent this informational edit. Under explicit_approve this mutation is proposed for approval like graph mutations; under immediate mode it becomes a saved composition edit through normal turn persistence.

## 5. Usability assessment

**SG-03 — demonstrated progress misclassification; high confidence.** The controlled empty-patch probe preserved every state field except version but moved4→5, and tool_result_mutated_composition_state returned True. tool_batch uses that result to mark mutation progress and clear anti-anchor state. Empty patch_source_options retained version4 and returned False under the same probe. This is a cross-cutting progress/effect contract problem, not evidence of wrong metadata values. Workaround: avoid empty/identical metadata calls and do not treat a metadata version advance as proof that pipeline construction progressed.

## 6. Structural remediation

Distinguish attempted, applied, no_change and graph-progress effects in tool results. Compare accepted metadata content before bumping revision; let the orchestrator count meaningful work by explicit goal/effect rather than any version increment. EffectDomain currently defaults all MUTATION tools to graph+validation+YAML, so metadata deserves an explicit projection rather than accidental broad classification. Regression: empty/same-value patch returns no_change and preserves version; actual name change persists once; metadata alone cannot reset graph-repair progress; invalid keys/null reject atomically. Compatibility risk medium: revisions/approval receipts may currently depend on attempt counting; preserve receipt history while improving progress semantics.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_tools_mutations.py:2247](../../../../tests/unit/web/composer/test_tools_mutations.py#L2247) — partial metadata update and missing-patch argument rejection.
- [src/elspeth/web/composer/tools/declarations.py:183](../../../../src/elspeth/web/composer/tools/declarations.py#L183) — default effects inspected.
- [src/elspeth/web/composer/tool_batch.py:3333](../../../../src/elspeth/web/composer/tool_batch.py#L3333) — progress consumer inspected.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance. Controlled no-change probes compared full serialized state excluding version: metadata and node empty patches had identical content, versions4→5 and progress=True; source empty patch had identical content, versions4→4 and progress=False.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.transforms._handle_set_metadata`](../../../../src/elspeth/web/composer/tools/transforms.py#L557).

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
    "patch": {
      "type": "object",
      "description": "Partial metadata update. Only included fields are changed.",
      "additionalProperties": false,
      "properties": {
        "name": {
          "type": "string"
        },
        "description": {
          "type": "string"
        }
      }
    }
  },
  "required": [
    "patch"
  ],
  "additionalProperties": false
}
```
