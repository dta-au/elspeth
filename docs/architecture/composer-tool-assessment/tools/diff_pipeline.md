# `diff_pipeline` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Compare current pipeline with an explicitly supplied baseline. The baseline may be request-start or MCP session baseline, so this tool does not inherently mean “last saved state.” It is noncacheable discovery and cannot change either input state.

## 2. Contracts and authority

Empty semantic arguments. Caller-owned ToolContext.baseline is required for useful behavior, although optional at the generic dispatcher API. Success carries from_version, metadata/source changes, collection additions/removals/modifications and warning deltas. Missing baseline produces a failed result with diff_baseline_unavailable, retaining the current state ([src/elspeth/web/composer/tools/generation.py:4642](../../../../src/elspeth/web/composer/tools/generation.py#L4642)).

## 3. Source trace to the user

`_execute_diff_pipeline` reads context.baseline/current_validation, validates the baseline, and calls `diff_states` ([src/elspeth/web/composer/tools/generation.py:4659](../../../../src/elspeth/web/composer/tools/generation.py#L4659), [src/elspeth/web/composer/tools/_common.py:1136](../../../../src/elspeth/web/composer/tools/_common.py#L1136)). Planner context supplies current_state as baseline ([src/elspeth/web/composer/pipeline_planner.py:3434](../../../../src/elspeth/web/composer/pipeline_planner.py#L3434)); MCP supplies its held baseline ([src/elspeth/composer_mcp/server.py:409](../../../../src/elspeth/composer_mcp/server.py#L409)). Freeform callsite omits it ([src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)).

The freeform exposure is `get_tool_definitions` → wire projection → `composer_loop_tool_definitions`; the planner selects an ordered discovery subset, so exposure can narrow by phase ([src/elspeth/web/composer/tools/_dispatch.py:311](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391), [src/elspeth/web/composer/pipeline_planner.py:1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647)). Public freeform calls pass credential/batch admission, wire decoding and closed semantic-schema admission before `execute_tool`; internal direct calls must explicitly opt into public argument validation ([src/elspeth/web/composer/tool_batch.py:1010](../../../../src/elspeth/web/composer/tool_batch.py#L1010), [src/elspeth/web/composer/tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399), [src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)).

After the handler, `finalize_tool_result` normalizes validation; discovery result admission selects the declared response contract before serialization ([src/elspeth/web/composer/tools/_dispatch.py:752](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752), [src/elspeth/web/composer/tool_batch.py:3052](../../../../src/elspeth/web/composer/tool_batch.py#L3052)). The loop records the invocation, sends a tool result back to the model, and persists its redacted tool-turn cohort; subsequent operation settlement owns user-facing success/failure ([src/elspeth/web/composer/tool_batch.py:3344](../../../../src/elspeth/web/composer/tool_batch.py#L3344), [src/elspeth/web/composer/service.py:1515](../../../../src/elspeth/web/composer/service.py#L1515), [src/elspeth/web/sessions/composer_turn.py:1051](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)). ToolCallCard renders the recorded outcome and explanatory tooltip rather than giving this tool its own direct action button ([src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:90](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L90)). A completed lookup does not imply pipeline readiness.

## 4. Branches, failures and recovery

No baseline → explicit failure; identical baseline/current → success and zero changes; changed metadata → success with a changed flag and count; structural collections use identity-based diffs and warnings compare validation summaries. A malformed baseline/catalog failure must not become zero changes. Argument rejection occurs before the handler. Expected policy/semantic refusal is a failed result on the current state; controlled-code exceptions remain framework/plugin failures, with the executed prefix and audit evidence retained by the common loop. Cancellation, timeout and final persistence failure belong to the request/operation owner, not a handler-local retry. None of these discovery handlers commits graph changes or needs a proposal approval step. Retrying a lookup can recover an argument or infrastructure problem but cannot repair the graph by itself.

**Cache, idempotency, concurrency and retry:** `ToolKind.DISCOVERY`, `cacheable=False`. A repeated call over the same immutable baseline/current pair has the same semantic delta and no graph/storage effect, but a distinct audit invocation. Caller-supplied current state, prior validation and baseline define the observation; the handler acquires no session write lock and does not refresh them from storage. A stale baseline can still produce a mathematically valid but wrong-scope comparison, hence R1's explicit context requirement. No automatic handler retry exists; repeated freeform calls cannot remedy the missing baseline. Provider transport retries are separate.

## 5. Defects, restrictions and hypotheses

**GS-01 — confirmed freeform route wiring defect:** the advertised freeform capability cannot compare against a baseline because the production `execute_tool` call supplies none, the dispatcher defaults baseline to None ([src/elspeth/web/composer/tools/_dispatch.py:810](../../../../src/elspeth/web/composer/tools/_dispatch.py#L810)), and the handler rejects that condition. Direct controls reproduce failure with omitted context, one metadata change with supplied baseline, and zero changes with identical baseline. The user receives “Load or create a session first,” which does not repair this route's omission. Planner/MCP wiring is counterevidence against a broken diff algorithm, not against the freeform integration defect.

## 6. Ownership, remediation and dependencies

Orchestrator context owner should define baseline semantics for each invocation and supply an immutable request-start baseline to freeform dispatch, or stop advertising the capability where intentionally unsupported. The former matches the stated tool purpose but must specify baseline lifetime across multi-turn authoring and retries. Do not inject a fabricated baseline equal to current state, which would always report zero. Regression depends on explicit request context before any larger dispatch consolidation.

**Implementation risk:** Medium: choosing the wrong baseline lifetime creates plausible false comparisons. Require changed/unchanged and inter-turn controls before wiring it.

## 7. Evidence, regression checks and limits

The controlled probe uses changed metadata and same-state positive/negative controls; raw results are retained in `generation-controls.txt`. Existing contract fixtures explicitly pass baseline and therefore cannot catch the production omission ([tests/unit/web/composer/test_generation_response_contracts.py:31](../../../../tests/unit/web/composer/test_generation_response_contracts.py#L31)); no-baseline rejection is pinned at line 206. These tests passed in the 370-test selection. Confidence: high in caller/default/handler chain. A service-level scripted probe is tracked separately in the lane report; direct-control success alone is not described as an HTTP test. Required regression: fake provider calls diff after a real earlier mutation in the same request, and assert accurate result plus persisted tool audit across fresh-session, existing-session and retry paths.

Source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection and selected local tests do not establish live provider behavior, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

Synchronous kind: `discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.generation._execute_diff_pipeline`](../../../../src/elspeth/web/composer/tools/generation.py#L4642).

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
