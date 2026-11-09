# `get_expression_grammar` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Return the static gate-expression language reference. It supplies syntax knowledge; it neither parses a user's expression nor executes a sample. It is cacheable discovery exposed to the ordinary loop and planner discovery palette.

## 2. Contracts and authority

No semantic arguments are accepted. Strict transport may carry a wire marker that is removed before the empty argument model is validated. The response is exactly `{"grammar": <string>}` inside the discovery envelope, with a nominal selected contract. Source: [src/elspeth/web/composer/tools/generation.py:363](../../../../src/elspeth/web/composer/tools/generation.py#L363) and [src/elspeth/web/composer/tools/generation.py:382](../../../../src/elspeth/web/composer/tools/generation.py#L382). The handler makes no graph, configuration or data-store mutation; the enclosing loop still persists invocation audit/history.

## 3. Source trace to the user

`_handle_get_expression_grammar` validates empty arguments, reads `get_expression_grammar()` and constructs the one-key payload. `_admit_expression_grammar` rejects wrong carriers/keys/types rather than accepting arbitrary serializable objects ([src/elspeth/web/composer/tools/generation.py:278](../../../../src/elspeth/web/composer/tools/generation.py#L278), [src/elspeth/web/composer/tools/generation.py:363](../../../../src/elspeth/web/composer/tools/generation.py#L363)).

The freeform exposure is `get_tool_definitions` → wire projection → `composer_loop_tool_definitions`; the planner selects an ordered discovery subset, so exposure can narrow by phase ([src/elspeth/web/composer/tools/_dispatch.py:311](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391), [src/elspeth/web/composer/pipeline_planner.py:1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647)). Public freeform calls pass credential/batch admission, wire decoding and closed semantic-schema admission before `execute_tool`; internal direct calls must explicitly opt into public argument validation ([src/elspeth/web/composer/tool_batch.py:1010](../../../../src/elspeth/web/composer/tool_batch.py#L1010), [src/elspeth/web/composer/tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399), [src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)).

After the handler, `finalize_tool_result` normalizes validation; discovery result admission selects the declared response contract before serialization ([src/elspeth/web/composer/tools/_dispatch.py:752](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752), [src/elspeth/web/composer/tool_batch.py:3052](../../../../src/elspeth/web/composer/tool_batch.py#L3052)). The loop records the invocation, sends a tool result back to the model, and persists its redacted tool-turn cohort; subsequent operation settlement owns user-facing success/failure ([src/elspeth/web/composer/tool_batch.py:3344](../../../../src/elspeth/web/composer/tool_batch.py#L3344), [src/elspeth/web/composer/service.py:1515](../../../../src/elspeth/web/composer/service.py#L1515), [src/elspeth/web/sessions/composer_turn.py:1051](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)). ToolCallCard renders the recorded outcome and explanatory tooltip rather than giving this tool its own direct action button ([src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:90](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L90)). A completed lookup does not imply pipeline readiness.

## 4. Branches, failures and recovery

The successful branch always returns the shipped reference. Unexpected arguments fail admission. A corrupt response is a framework fault. Cache reuse is appropriate only while the process's grammar reference remains fixed. Argument rejection occurs before the handler. Expected policy/semantic refusal is a failed result on the current state; controlled-code exceptions remain framework/plugin failures, with the executed prefix and audit evidence retained by the common loop. Cancellation, timeout and final persistence failure belong to the request/operation owner, not a handler-local retry. None of these discovery handlers commits graph changes or needs a proposal approval step. Retrying a lookup can recover an argument or infrastructure problem but cannot repair the graph by itself.

**Cache, idempotency, concurrency and retry:** `ToolKind.DISCOVERY`, `cacheable=True`. Repeated successful calls return the same shipped grammar and do not change graph/storage; each call still has its own audit identity, including cache hits. This static reference has no session concurrency or lock requirement. Failure is not cached and the handler never retries. Any retry of the primary provider is separate from replaying this local handler. Cache scope is the compose request, not a durable result cache.

## 5. Defects, restrictions and hypotheses

No parser/grammar behavioral mismatch was demonstrated. **Presentation restriction:** the frontend tooltip says routing and templating, while this producer explicitly calls the reference gate-expression syntax ([src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts:35](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L35), [src/elspeth/web/composer/tools/generation.py:278](../../../../src/elspeth/web/composer/tools/generation.py#L278)); whether template expression consumers share precisely this grammar should be checked before widening that claim. This is a hypothesis, not a demonstrated language bug.

## 6. Ownership, remediation and dependencies

Expression parser owner owns the accepted language; tool owner owns its published reference; frontend owner owns the scope description. Keep operator-set parity tests when evolving the grammar. Do not make the orchestrator synthesize syntax documentation or accept new expression forms based on prose alone.

**Implementation risk:** Low for grammar documentation updates; medium if changing language semantics or merging grammar consumers. Parser/reference parity is the safeguard.

## 7. Evidence, regression checks and limits

Read static-reference and parser-operator parity test bodies beginning at [tests/unit/web/composer/test_tools_discovery.py:149](../../../../tests/unit/web/composer/test_tools_discovery.py#L149). The parity test imports actual parser operator registries before comparing text; the file passed in the 370-test local selection. Confidence: high on the static payload and cache semantics; moderate on all consumer-language parity. Future checks should execute representative supported and prohibited expressions through the actual parser, alongside disclosure tests.

Source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection and selected local tests do not establish live provider behavior, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

Synchronous kind: `discovery`. Declaration cache opt-in: `true`. Handler: [`elspeth.web.composer.tools.generation._handle_get_expression_grammar`](../../../../src/elspeth/web/composer/tools/generation.py#L382).

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
  "properties": {},
  "required": [],
  "additionalProperties": false
}
```
