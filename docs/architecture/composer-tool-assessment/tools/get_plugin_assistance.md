# `get_plugin_assistance` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Retrieve plugin-owned guidance and examples, either discovery guidance or issue-specific repair advice. Plugins remain the author of these facts; the orchestrator consumes them. It is a cacheable discovery tool, separate from generic validation-code explanation.

## 2. Contracts and authority

Requires plugin_type and plugin_name; optional issue_code may be omitted or null. The response names plugin identity and issue code, with summary, suggested fixes, examples and composer hints. No published assistance is a successful empty-guidance result. Plugin selection is admitted against the request policy snapshot first ([src/elspeth/web/composer/tools/generation.py:2306](../../../../src/elspeth/web/composer/tools/generation.py#L2306)).

## 3. Source trace to the user

Handler validates arguments → checks policy → verifies the projected schema → resolves the concrete plugin class from the shared plugin manager → calls `get_agent_assistance(issue_code=...)` → applies `PolicyCatalogView.project_agent_assistance` → encodes examples → admits selected response contract ([src/elspeth/web/composer/tools/generation.py:2335](../../../../src/elspeth/web/composer/tools/generation.py#L2335), [src/elspeth/web/catalog/policy_view.py:203](../../../../src/elspeth/web/catalog/policy_view.py#L203)).

The freeform exposure is `get_tool_definitions` → wire projection → `composer_loop_tool_definitions`; the planner selects an ordered discovery subset, so exposure can narrow by phase ([src/elspeth/web/composer/tools/_dispatch.py:311](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391), [src/elspeth/web/composer/pipeline_planner.py:1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647)). Public freeform calls pass credential/batch admission, wire decoding and closed semantic-schema admission before `execute_tool`; internal direct calls must explicitly opt into public argument validation ([src/elspeth/web/composer/tool_batch.py:1010](../../../../src/elspeth/web/composer/tool_batch.py#L1010), [src/elspeth/web/composer/tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399), [src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)).

After the handler, `finalize_tool_result` normalizes validation; discovery result admission selects the declared response contract before serialization ([src/elspeth/web/composer/tools/_dispatch.py:752](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752), [src/elspeth/web/composer/tool_batch.py:3052](../../../../src/elspeth/web/composer/tool_batch.py#L3052)). The loop records the invocation, sends a tool result back to the model, and persists its redacted tool-turn cohort; subsequent operation settlement owns user-facing success/failure ([src/elspeth/web/composer/tool_batch.py:3344](../../../../src/elspeth/web/composer/tool_batch.py#L3344), [src/elspeth/web/composer/service.py:1515](../../../../src/elspeth/web/composer/service.py#L1515), [src/elspeth/web/sessions/composer_turn.py:1051](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)). ToolCallCard renders the recorded outcome and explanatory tooltip rather than giving this tool its own direct action button ([src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:90](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L90)). A completed lookup does not imply pipeline readiness.

## 4. Branches, failures and recovery

Discovery and issue-code modes share the plugin-owned hook. Missing guidance returns null summary and empty lists. Policy refusal must not leak blocked configuration through examples. A manager/catalog inconsistency after admission is a controlled-code error, not a fabricated no-assistance result. Argument rejection occurs before the handler. Expected policy/semantic refusal is a failed result on the current state; controlled-code exceptions remain framework/plugin failures, with the executed prefix and audit evidence retained by the common loop. Cancellation, timeout and final persistence failure belong to the request/operation owner, not a handler-local retry. None of these discovery handlers commits graph changes or needs a proposal approval step. Retrying a lookup can recover an argument or infrastructure problem but cannot repair the graph by itself.

**Cache, idempotency, concurrency and retry:** `ToolKind.DISCOVERY`, `cacheable=True`. Successful no-assistance responses are cached too; they mean this plugin published no guidance for that request, not that help can never exist. Cache keys include issue_code and plugin identity. Same-input repetition has no graph/store effect but creates another audit record. The handler owns no lock or retry loop; request policy projection remains snapshot-bound, while plugin code is process-loaded. Future hot reload would require explicit cache/version invalidation rather than assuming this cache observes it.

## 5. Defects, restrictions and hypotheses

No tool-specific defect was demonstrated. Structural limitation: helpfulness and breadth depend on each plugin publishing meaningful assistance; empty guidance is truthful but does not solve an authoring problem. The shared plugin manager and policy catalog are distinct collaborators, so their identity agreement remains a required invariant. This is a review lens rather than a proven divergence.

## 6. Ownership, remediation and dependencies

Plugin maintainers own assistance content; catalog policy owns safe projection; tool owner owns shape. First-class plugin work should standardize usable capabilities/examples alongside runtime contracts and preserve the ability to report no assistance honestly. Test examples by validating their after-configurations through plugin configuration admission, not just checking they are dictionaries.

**Implementation risk:** Medium for shared assistance/policy changes because examples can expose forbidden configuration. Preserve policy projection before disclosure.

## 7. Evidence, regression checks and limits

Read the `web_scrape.content.compact_text` assistance test that asserts identity, fixes and examples ([tests/unit/web/composer/test_tools.py:3130](../../../../tests/unit/web/composer/test_tools.py#L3130)); response-contract tests admit owned examples and reject non-finite/unowned leaves ([tests/unit/web/composer/test_generation_response_contracts.py:224](../../../../tests/unit/web/composer/test_generation_response_contracts.py#L224)). Policy-refusal and outer-fault tests in test_tools_discovery passed in the 370-test selection. Confidence: high on dispatch and projections; moderate on semantic usefulness of every plugin's examples. The whole plugin corpus was not evaluated for repair quality.

Source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection and selected local tests do not establish live provider behavior, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

Synchronous kind: `discovery`. Declaration cache opt-in: `true`. Handler: [`elspeth.web.composer.tools.generation._execute_get_plugin_assistance`](../../../../src/elspeth/web/composer/tools/generation.py#L2306).

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
      "description": "Plugin family. 'source', 'transform', or 'sink'."
    },
    "plugin_name": {
      "type": "string",
      "description": "Plugin name (e.g. 'csv', 'web_scrape', 'database')."
    },
    "issue_code": {
      "type": [
        "string",
        "null"
      ],
      "description": "Optional. Stable issue identifier owned by the plugin for failure-time guidance. Omit or pass null for discovery-time guidance."
    }
  },
  "required": [
    "plugin_type",
    "plugin_name"
  ],
  "additionalProperties": false
}
```
