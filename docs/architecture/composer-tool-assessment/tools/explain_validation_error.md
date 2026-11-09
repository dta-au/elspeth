# `explain_validation_error` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Translate a validation message or stable error code into an explanation and repair guidance. The result is advice to the planner; it is not a validation verdict or automatic graph repair. The tool is cacheable discovery. The planner may advertise it selectively when error feedback warrants another discovery turn.

## 2. Contracts and authority

Requires `error_text: string`; closed root arguments. Output requires echoed error_text, explanation and suggested_fix, with optional error_code in the selected response contract. The tool independently validates current graph state for the envelope; that state is distinct from the text being explained ([src/elspeth/web/composer/tools/generation.py:2171](../../../../src/elspeth/web/composer/tools/generation.py#L2171)). Error text is untrusted and its persisted projection has a dedicated summarizer ([src/elspeth/web/composer/redaction.py:2698](../../../../src/elspeth/web/composer/redaction.py#L2698)).

## 3. Source trace to the user

`_execute_explain_validation_error` tries a special runtime union collision reason, exact code catalog, regex message patterns, embedded case-insensitive code lookup, then an explicit unknown-text instruction. It can append a bounded validator expected-value hint ([src/elspeth/web/composer/tools/generation.py:2171](../../../../src/elspeth/web/composer/tools/generation.py#L2171)). `EXPLAIN_VALIDATION_ERROR_RESPONSE_CONTRACT` preserves the result shape.

The freeform exposure is `get_tool_definitions` → wire projection → `composer_loop_tool_definitions`; the planner selects an ordered discovery subset, so exposure can narrow by phase ([src/elspeth/web/composer/tools/_dispatch.py:311](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391), [src/elspeth/web/composer/pipeline_planner.py:1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647)). Public freeform calls pass credential/batch admission, wire decoding and closed semantic-schema admission before `execute_tool`; internal direct calls must explicitly opt into public argument validation ([src/elspeth/web/composer/tool_batch.py:1010](../../../../src/elspeth/web/composer/tool_batch.py#L1010), [src/elspeth/web/composer/tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399), [src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)).

After the handler, `finalize_tool_result` normalizes validation; discovery result admission selects the declared response contract before serialization ([src/elspeth/web/composer/tools/_dispatch.py:752](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752), [src/elspeth/web/composer/tool_batch.py:3052](../../../../src/elspeth/web/composer/tool_batch.py#L3052)). The loop records the invocation, sends a tool result back to the model, and persists its redacted tool-turn cohort; subsequent operation settlement owns user-facing success/failure ([src/elspeth/web/composer/tool_batch.py:3344](../../../../src/elspeth/web/composer/tool_batch.py#L3344), [src/elspeth/web/composer/service.py:1515](../../../../src/elspeth/web/composer/service.py#L1515), [src/elspeth/web/sessions/composer_turn.py:1051](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)). ToolCallCard renders the recorded outcome and explanatory tooltip rather than giving this tool its own direct action button ([src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:90](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L90)). A completed lookup does not imply pipeline readiness.

## 4. Branches, failures and recovery

Exact code and noisy-message branches can produce equivalent guidance through different shapes. Unknown text succeeds with a clear “not known” explanation and instructions to supply a code; that success means the explanation operation completed, not that the graph is valid. Regex/substring matching is advisory only. Argument rejection occurs before the handler. Expected policy/semantic refusal is a failed result on the current state; controlled-code exceptions remain framework/plugin failures, with the executed prefix and audit evidence retained by the common loop. Cancellation, timeout and final persistence failure belong to the request/operation owner, not a handler-local retry. None of these discovery handlers commits graph changes or needs a proposal approval step. Retrying a lookup can recover an argument or infrastructure problem but cannot repair the graph by itself.

**Cache, idempotency, concurrency and retry:** `ToolKind.DISCOVERY`, `cacheable=True`; successful results are cached by tool/arguments within a compose request, including the successful unknown-text explanation. Cache hits are reattached to current state and normalized validation, so cached advice is not a cached graph verdict. Repeated equal inputs are effect-free but still audited. The static guidance lookup has no write lock or internal retry; concurrent graph edits cannot make its advice a current diagnostic. The planner must supply new error evidence after an edit.

## 5. Defects, restrictions and hypotheses

**GS-02 — confirmed low-impact contract discrepancy:** the published description promises an error_code when text matches a closed code ([src/elspeth/web/composer/tools/generation.py:2269](../../../../src/elspeth/web/composer/tools/generation.py#L2269)), but the exact-code return at [src/elspeth/web/composer/tools/generation.py:2194](../../../../src/elspeth/web/composer/tools/generation.py#L2194) omits it; the fuzzy return at [src/elspeth/web/composer/tools/generation.py:2222](../../../../src/elspeth/web/composer/tools/generation.py#L2222) includes it. Controlled calls with `unknown_node_type` versus `LOG UNKNOWN_NODE_TYPE END` reproduce the difference. The explanation remains useful; no graph corruption is alleged. Fuzzy matching can be ambiguous when text contains several codes, so consumers must not treat the chosen code as authoritative diagnosis.

## 6. Ownership, remediation and dependencies

Generation/response-contract owner should decide one canonical identity rule and align producer, declaration and selected-contract fixtures. Prefer carrying the exact matched code consistently; this is an additive output decision that still needs frozen-wire fixture review. Do not move text pattern matching into orchestrator control flow. Dependency: settle result-contract semantics before updating planner consumers or UI.

**Implementation risk:** Low for the matched-code correction, medium for response-schema or frozen-wire compatibility changes. Keep explanation advice separate from authoritative validation.

## 7. Evidence, regression checks and limits

Read exact/noisy/unknown producer fixtures and frozen-wire assertions ([tests/unit/web/composer/test_generation_response_contracts.py:43](../../../../tests/unit/web/composer/test_generation_response_contracts.py#L43), [tests/unit/web/composer/test_generation_response_contracts.py:111](../../../../tests/unit/web/composer/test_generation_response_contracts.py#L111)), plus noisy-code retention test at line 219. Existing tests preserve current bytes and therefore do not refute GS-02. Selected file passed. Controlled raw observations are in the lane's `generation-controls.txt`: exact keys omit error_code; noisy keys include it; unknown control omits it. Confidence: high in discrepancy, low operational severity. Regression should compare exact/noisy variants of the same code and verify unknown input remains honestly unknown.

Source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection and selected local tests do not establish live provider behavior, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

Synchronous kind: `discovery`. Declaration cache opt-in: `true`. Handler: [`elspeth.web.composer.tools.generation._execute_explain_validation_error`](../../../../src/elspeth/web/composer/tools/generation.py#L2171).

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
    "error_text": {
      "type": "string",
      "description": "The validation error message to explain."
    }
  },
  "required": [
    "error_text"
  ],
  "additionalProperties": false
}
```
