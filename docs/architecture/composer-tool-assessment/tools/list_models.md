# `list_models` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Discover model identifiers and provider counts. With no provider it returns a provider summary; with a prefix it returns a capped list. The declaration is cacheable discovery. It is a catalog lookup, not a credential-based model access probe.

## 2. Contracts and authority

Optional provider string: omission means summary, empty string means unprefixed identifiers, ordinary strings are prefix filters. Optional positive integer limit defaults to 50; boolean values are rejected by the argument model. OpenRouter prefix normalizes a trailing slash and reads catalog values as unprefixed HTTP model slugs. Other providers use the bundled LiteLLM list. Output shape distinguishes provider summary from list/count/truncated ([src/elspeth/web/composer/tools/generation.py:2539](../../../../src/elspeth/web/composer/tools/generation.py#L2539), [src/elspeth/web/composer/tools/generation.py:2636](../../../../src/elspeth/web/composer/tools/generation.py#L2636)).

## 3. Source trace to the user

Handler reads the bundled list, validates inputs, selects summary/filter branch, substitutes the OpenRouter catalog count/list where appropriate, and returns `LIST_MODELS_RESPONSE_CONTRACT` data ([src/elspeth/web/composer/tools/generation.py:2563](../../../../src/elspeth/web/composer/tools/generation.py#L2563)). It explicitly discards ToolContext and does not test current user's provider credentials.

The freeform exposure is `get_tool_definitions` → wire projection → `composer_loop_tool_definitions`; the planner selects an ordered discovery subset, so exposure can narrow by phase ([src/elspeth/web/composer/tools/_dispatch.py:311](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391), [src/elspeth/web/composer/pipeline_planner.py:1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647)). Public freeform calls pass credential/batch admission, wire decoding and closed semantic-schema admission before `execute_tool`; internal direct calls must explicitly opt into public argument validation ([src/elspeth/web/composer/tool_batch.py:1010](../../../../src/elspeth/web/composer/tool_batch.py#L1010), [src/elspeth/web/composer/tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399), [src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)).

After the handler, `finalize_tool_result` normalizes validation; discovery result admission selects the declared response contract before serialization ([src/elspeth/web/composer/tools/_dispatch.py:752](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752), [src/elspeth/web/composer/tool_batch.py:3052](../../../../src/elspeth/web/composer/tool_batch.py#L3052)). The loop records the invocation, sends a tool result back to the model, and persists its redacted tool-turn cohort; subsequent operation settlement owns user-facing success/failure ([src/elspeth/web/composer/tool_batch.py:3344](../../../../src/elspeth/web/composer/tool_batch.py#L3344), [src/elspeth/web/composer/service.py:1515](../../../../src/elspeth/web/composer/service.py#L1515), [src/elspeth/web/sessions/composer_turn.py:1051](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)). ToolCallCard renders the recorded outcome and explanatory tooltip rather than giving this tool its own direct action button ([src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:90](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L90)). A completed lookup does not imply pipeline readiness.

## 4. Branches, failures and recovery

Omitted, empty, ordinary-prefix, OpenRouter-prefix and no-match calls differ intentionally. Limit truncation is explicit; there is no pagination cursor. Repeating the same limit cannot retrieve the remainder, though a larger limit is accepted. Catalog exceptions are not evidence that no models exist. Argument rejection occurs before the handler. Expected policy/semantic refusal is a failed result on the current state; controlled-code exceptions remain framework/plugin failures, with the executed prefix and audit evidence retained by the common loop. Cancellation, timeout and final persistence failure belong to the request/operation owner, not a handler-local retry. None of these discovery handlers commits graph changes or needs a proposal approval step. Retrying a lookup can recover an argument or infrastructure problem but cannot repair the graph by itself.

**Cache, idempotency, concurrency and retry:** `ToolKind.DISCOVERY`, `cacheable=True`. Identical provider/limit queries reuse successful results for that compose request; different limits or provider strings are distinct queries. Repeated lookup has no graph/store effect but is separately audited. The handler does not lock a credential/account snapshot, make entitlement probes or retry providers. Its catalog freshness follows the imported list/catalog service; request cache reuse can intentionally lag a catalog refresh. Primary-model transport retries and any upstream catalog refresh are not equivalent to model-access verification.

## 5. Defects, restrictions and hypotheses

**GS-03 — confirmed user-facing description defect (model member):** the tooltip claims models available on this deployment's credentials ([src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts:24](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L24)), while the producer ignores context and reads public/bundled catalogs ([src/elspeth/web/composer/tools/generation.py:2563](../../../../src/elspeth/web/composer/tools/generation.py#L2563)). Listing an identifier does not establish authorization, account access or provider availability. **Restriction:** no explicit paging; non-OpenRouter freshness is bundled-catalog freshness. No claim is made that a listed current model actually fails.

## 6. Ownership, remediation and dependencies

Model-catalog owner owns identifier/freshness semantics; frontend owner should say catalog identifiers rather than credential-qualified availability. If credential-scoped reachability is desired, define that separate capability, its cost, evidence expiry and failure semantics before implementing it. Do not silently make a currently local discovery operation perform paid provider probes. Resolve discovery-contract terminology before orchestrator uses the list as an execution guarantee.

**Implementation risk:** Low for tooltip correction; medium for paging/freshness contracts. Introducing live credential probes would change cost and authority and needs separate scope.

## 7. Evidence, regression checks and limits

Read mixed-catalog provider/limit controls and omitted-vs-empty controls ([tests/unit/web/composer/test_tools.py:2894](../../../../tests/unit/web/composer/test_tools.py#L2894)). They inject known matching and foreign providers, avoiding vacuous prefix assertions. Generation response fixtures also exercise summary, capped list, empty prefix and no-match branches and passed in the first selection ([tests/unit/web/composer/test_generation_response_contracts.py:58](../../../../tests/unit/web/composer/test_generation_response_contracts.py#L58)). Confidence: high in GS-03 and local semantics; insufficient for live account reachability. Future regression should include a catalog model unavailable to the configured account and ensure no UI claim implies it was verified.

Source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection and selected local tests do not establish live provider behavior, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

Synchronous kind: `discovery`. Declaration cache opt-in: `true`. Handler: [`elspeth.web.composer.tools.generation._execute_list_models`](../../../../src/elspeth/web/composer/tools/generation.py#L2539).

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
    "provider": {
      "type": "string",
      "description": "Provider prefix to filter by (e.g. 'openrouter/', 'azure/'). Omit to get a provider summary instead of individual models."
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "default": 50,
      "description": "Max models to return (default 50)."
    }
  },
  "required": [],
  "additionalProperties": false
}
```
