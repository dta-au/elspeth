# `get_pipeline_state` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Inspect the current authored graph, one component, or a recoverable authoring document for a full replacement. This is state-sensitive, noncacheable discovery. The result is diagnostic unless the caller explicitly asks for set_pipeline_arguments and that serializer succeeds.

## 2. Contracts and authority

Optional component string: omitted/full-state aliases select the document; source selects the named sources map; a node ID or output name selects that component; set_pipeline_arguments selects the flat authoring document. Ordinary state output and set_pipeline wire input are different contracts. The web mutation needs the flat document wrapped in `{"pipeline": ...}`. Exact argument export can refuse with round_trip_unavailable. Credential checking runs over state using accessible secret reference names, then storage paths are redacted ([src/elspeth/web/composer/tools/sessions.py:2451](../../../../src/elspeth/web/composer/tools/sessions.py#L2451), [src/elspeth/web/composer/tools/sessions.py:2519](../../../../src/elspeth/web/composer/tools/sessions.py#L2519)).

## 3. Source trace to the user

`_execute_get_pipeline_state` validates the selector, reads reference names when secret context exists, rejects raw credential material, selects `_serialize_set_pipeline_arguments` or source/node/output/full serializers, and applies `redact_source_storage_path`. `PIPELINE_STATE_RESPONSE_CONTRACT` admits the selected response variant ([src/elspeth/web/composer/tools/sessions.py:2462](../../../../src/elspeth/web/composer/tools/sessions.py#L2462), [src/elspeth/web/composer/tools/sessions.py:2483](../../../../src/elspeth/web/composer/tools/sessions.py#L2483)).

The freeform exposure is `get_tool_definitions` → wire projection → `composer_loop_tool_definitions`; the planner selects an ordered discovery subset, so exposure can narrow by phase ([src/elspeth/web/composer/tools/_dispatch.py:311](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391), [src/elspeth/web/composer/pipeline_planner.py:1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647)). Public freeform calls pass credential/batch admission, wire decoding and closed semantic-schema admission before `execute_tool`; internal direct calls must explicitly opt into public argument validation ([src/elspeth/web/composer/tool_batch.py:1010](../../../../src/elspeth/web/composer/tool_batch.py#L1010), [src/elspeth/web/composer/tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399), [src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)).

After the handler, `finalize_tool_result` normalizes validation; discovery result admission selects the declared response contract before serialization ([src/elspeth/web/composer/tools/_dispatch.py:752](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752), [src/elspeth/web/composer/tool_batch.py:3052](../../../../src/elspeth/web/composer/tool_batch.py#L3052)). The loop records the invocation, sends a tool result back to the model, and persists its redacted tool-turn cohort; subsequent operation settlement owns user-facing success/failure ([src/elspeth/web/composer/tool_batch.py:3344](../../../../src/elspeth/web/composer/tool_batch.py#L3344), [src/elspeth/web/composer/service.py:1515](../../../../src/elspeth/web/composer/service.py#L1515), [src/elspeth/web/sessions/composer_turn.py:1051](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)). ToolCallCard renders the recorded outcome and explanatory tooltip rather than giving this tool its own direct action button ([src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:90](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L90)). A completed lookup does not imply pipeline readiness.

The model can use the exact-document mode before set_pipeline; the UI receives an inspection event rather than an applied-state version.

## 4. Branches, failures and recovery

Credential refusal happens before any component disclosure. Exact reconstruction failure is explicit; it must not become a guessed document. Component selection checks node, then output, then full-state alias; ordinary IDs can therefore take precedence over aliases. Unknown components fail without state mutation. Source mode returns all named sources, not one compatibility source. Argument rejection occurs before the handler. Expected policy/semantic refusal is a failed result on the current state; controlled-code exceptions remain framework/plugin failures, with the executed prefix and audit evidence retained by the common loop. Cancellation, timeout and final persistence failure belong to the request/operation owner, not a handler-local retry. None of these discovery handlers commits graph changes or needs a proposal approval step. Retrying a lookup can recover an argument or infrastructure problem but cannot repair the graph by itself.

**Cache, idempotency, concurrency and retry:** `ToolKind.DISCOVERY`, `cacheable=False`. Each call serializes its supplied immutable state; accessible secret-reference names may be read from the scoped secret service. Repetition does not mutate graph/storage but is audited again and may observe a newer caller-supplied state. The serializer does not acquire a write lock or reread the session head atomically with references; operation/request ownership supplies that context. Exact export is not a compare-and-swap token. No handler retry occurs; on round_trip_unavailable the correct action is preserve/refuse or a supported narrow edit, not repeated guessing.

## 5. Defects, restrictions and hypotheses

No tool-specific behavioral defect was demonstrated. Deliberate restriction: historical or structurally unsupported states may be inspectable but not losslessly reconstructable as current public set_pipeline arguments. Secret/storage redaction means diagnostic output is not automatically authoring authority. This should remain a preserve/refuse boundary, especially during 0.8.10-lite reinspection.

## 6. Ownership, remediation and dependencies

State-serialization owner owns exact round-trip availability; credential owner owns sensitive-value refusal; orchestrator owns requesting the appropriate variant. A first-class tool contract should declare the returned variant and its authority explicitly so consumers cannot mistake a display document for an approved replacement. Avoid adding generic coercion or a fallback serializer that invents missing fields.

**Implementation risk:** Medium for serializer/authority changes, particularly blob/review round trips. Preserve the diagnostic-versus-authoring distinction and explicit refusal.

## 7. Evidence, regression checks and limits

Read functional full-state and alias tests ([tests/unit/web/composer/test_tools.py:1306](../../../../tests/unit/web/composer/test_tools.py#L1306)), which construct a graph through tools then assert sources/nodes/outputs/edges and the single envelope version. This class passed in the second focused selection: 195 tests, exit 0. Confidence: high on selector/admission behavior; moderate on all historical graph round trips. Required regression: exact export→public wire decode→candidate build across named sources, blob binding and resolved reviews, with unsupported shape controls refusing without mutations.

Source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection and selected local tests do not establish live provider behavior, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

Synchronous kind: `discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.sessions._execute_get_pipeline_state`](../../../../src/elspeth/web/composer/tools/sessions.py#L2444).

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
    "component": {
      "type": "string",
      "description": "Optional: return only one component — 'source', a node ID, or an output name. Accepted full-state aliases: omit component, pass 'full', 'all', 'pipeline', or pass the empty string. Use 'set_pipeline_arguments' for the flat authoring document; the web set_pipeline call requires {\"pipeline\": <document>}. Ordinary inspection output is diagnostic only."
    }
  },
  "required": [],
  "additionalProperties": false
}
```
