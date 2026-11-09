# `get_plugin_schema` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Retrieve the policy-projected configuration contract for one named source, transform or sink. It supports discovery before authoring and repair after rejection; it does not install or enable a plugin. The declaration is synchronous discovery and cacheable within a compose request. Freeform, planner discovery and the restricted MCP surface can invoke this same capability.

## 2. Contracts and authority

Required `plugin_type` is one of source/transform/sink and `name` is a string; unknown root keys are refused. The handler also validates its owned argument model. Output includes JSON and knob schemas, authoring hints, secret requirements and web configuration authority. `operator_profiled` means the author must select a profile rather than invent raw operator options. The exact catalog and availability snapshot must be the same request-owned pair. The handler performs policy admission before schema lookup ([src/elspeth/web/composer/tools/generation.py:283](../../../../src/elspeth/web/composer/tools/generation.py#L283), [src/elspeth/web/composer/tools/generation.py:311](../../../../src/elspeth/web/composer/tools/generation.py#L311)).

## 3. Source trace to the user

The concrete path is `_handle_get_plugin_schema` → `_plugin_selection_violation` → `PolicyCatalogView.get_schema` → `PLUGIN_SCHEMA_RESPONSE_CONTRACT` ([src/elspeth/web/composer/tools/generation.py:311](../../../../src/elspeth/web/composer/tools/generation.py#L311), [src/elspeth/web/catalog/policy_view.py:191](../../../../src/elspeth/web/catalog/policy_view.py#L191)). A successful freeform read also marks that plugin schema disclosed for subsequent prompt context ([src/elspeth/web/composer/tool_batch.py:3310](../../../../src/elspeth/web/composer/tool_batch.py#L3310)).

The freeform exposure is `get_tool_definitions` → wire projection → `composer_loop_tool_definitions`; the planner selects an ordered discovery subset, so exposure can narrow by phase ([src/elspeth/web/composer/tools/_dispatch.py:311](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391), [src/elspeth/web/composer/pipeline_planner.py:1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647)). Public freeform calls pass credential/batch admission, wire decoding and closed semantic-schema admission before `execute_tool`; internal direct calls must explicitly opt into public argument validation ([src/elspeth/web/composer/tool_batch.py:1010](../../../../src/elspeth/web/composer/tool_batch.py#L1010), [src/elspeth/web/composer/tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399), [src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)).

After the handler, `finalize_tool_result` normalizes validation; discovery result admission selects the declared response contract before serialization ([src/elspeth/web/composer/tools/_dispatch.py:752](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752), [src/elspeth/web/composer/tool_batch.py:3052](../../../../src/elspeth/web/composer/tool_batch.py#L3052)). The loop records the invocation, sends a tool result back to the model, and persists its redacted tool-turn cohort; subsequent operation settlement owns user-facing success/failure ([src/elspeth/web/composer/tool_batch.py:3344](../../../../src/elspeth/web/composer/tool_batch.py#L3344), [src/elspeth/web/composer/service.py:1515](../../../../src/elspeth/web/composer/service.py#L1515), [src/elspeth/web/sessions/composer_turn.py:1051](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)). ToolCallCard renders the recorded outcome and explanatory tooltip rather than giving this tool its own direct action button ([src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:90](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L90)). A completed lookup does not imply pipeline readiness.

## 4. Branches, failures and recovery

Available plugin: return the projected schema. Unknown, disabled or unavailable selection: preserve the graph and return the policy owner's refusal. A catalog fault after successful policy selection propagates rather than masquerading as an unavailable plugin. Cache hits reuse admitted data while retaining a fresh call/audit identity. Argument rejection occurs before the handler. Expected policy/semantic refusal is a failed result on the current state; controlled-code exceptions remain framework/plugin failures, with the executed prefix and audit evidence retained by the common loop. Cancellation, timeout and final persistence failure belong to the request/operation owner, not a handler-local retry. None of these discovery handlers commits graph changes or needs a proposal approval step. Retrying a lookup can recover an argument or infrastructure problem but cannot repair the graph by itself.

**Cache, idempotency, concurrency and retry:** `ToolKind.DISCOVERY`, `cacheable=True`; successful same-name/argument lookups are reused within the compose-call discovery cache, with fresh audit invocation and current-state validation. Failed results are not cached. Repeated calls have no graph/store effect, but are not audit-idempotent: each provider decision is recorded. The catalog/snapshot is fixed for the request; this is not a subscription to concurrent plugin-policy changes. The handler has no retry loop or write lock. A later model call may retry a corrected request; primary-provider transport retries are separately audited by the gateway.

## 5. Defects, restrictions and hypotheses

No tool-specific behavioral defect was demonstrated. Deliberate restrictions: deployment availability policy, operator-profile authoring, and secret-reference disclosure. Structural risk: schema emission, policy projection and plugin assistance must continue to describe the same capability; a declaration alone cannot guarantee runtime support.

## 6. Ownership, remediation and dependencies

Catalog/policy owner owns capability truth; tool owner owns argument and response admission; orchestrator owns disclosure/cache lifetime. Retain this separation. Any registry consolidation should preserve policy refusal before lookup and avoid flattening operator-profile schemas into raw authorable configuration. Regression dependency: shared catalog/snapshot contract before tool migration.

**Implementation risk:** Medium if consolidating policy/catalog owners; low for retaining current behavior. Preserve snapshot identity, profile projection and cache scope.

## 7. Evidence, regression checks and limits

Read `test_get_plugin_schema_delegates` and snapshot-refusal tests ([tests/unit/web/composer/test_tools_discovery.py:61](../../../../tests/unit/web/composer/test_tools_discovery.py#L61), [tests/unit/web/composer/test_tools_discovery.py:107](../../../../tests/unit/web/composer/test_tools_discovery.py#L107)). The outer-fault test checks earlier success, expected refusal, final crash and persisted audit order ([tests/unit/web/composer/test_tools_discovery.py:473](../../../../tests/unit/web/composer/test_tools_discovery.py#L473)). These tests are included in the generation/session 370-test local selection, exit 0. Confidence: high in described admission path; moderate in end-to-end completeness because HTTP/browser routes were source traced, not executed. Proposed additional oracle: an availability change between requests must invalidate prior disclosure/cache while preserving the original request snapshot.

Source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection and selected local tests do not establish live provider behavior, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

Synchronous kind: `discovery`. Declaration cache opt-in: `true`. Handler: [`elspeth.web.composer.tools.generation._handle_get_plugin_schema`](../../../../src/elspeth/web/composer/tools/generation.py#L311).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | yes |
| Composer MCP | yes |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "plugin_type": {
      "type": "string",
      "enum": [
        "source",
        "transform",
        "sink"
      ],
      "description": "Plugin type."
    },
    "name": {
      "type": "string",
      "description": "Plugin name (e.g. 'csv')."
    }
  },
  "required": [
    "plugin_type",
    "name"
  ],
  "additionalProperties": false
}
```
