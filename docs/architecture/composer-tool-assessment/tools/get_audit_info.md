# `get_audit_info` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Explain mandatory Landscape auditing and operator ownership of audit backend/export configuration. It returns product policy facts, not session or run records. It is cacheable, argument-free discovery.

## 2. Contracts and authority

The payload is constant: enabled=true, composer_modifiable=false, summary and audit_export_summary. It deliberately exposes no backend URL, database type, key environment name, run IDs or lineage anchors. The handler does not query Landscape or WebSettings ([src/elspeth/web/composer/tools/generation.py:2468](../../../../src/elspeth/web/composer/tools/generation.py#L2468)). Configuration modification is intentionally outside this tool's authority.

## 3. Source trace to the user

`_execute_get_audit_info` validates empty arguments and constructs the constant payload; `AUDIT_INFO_RESPONSE_CONTRACT` validates that bounded policy-fact shape ([src/elspeth/web/composer/tools/generation.py:2438](../../../../src/elspeth/web/composer/tools/generation.py#L2438), [src/elspeth/web/composer/tools/generation.py:2488](../../../../src/elspeth/web/composer/tools/generation.py#L2488)).

The freeform exposure is `get_tool_definitions` → wire projection → `composer_loop_tool_definitions`; the planner selects an ordered discovery subset, so exposure can narrow by phase ([src/elspeth/web/composer/tools/_dispatch.py:311](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391), [src/elspeth/web/composer/pipeline_planner.py:1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647)). Public freeform calls pass credential/batch admission, wire decoding and closed semantic-schema admission before `execute_tool`; internal direct calls must explicitly opt into public argument validation ([src/elspeth/web/composer/tool_batch.py:1010](../../../../src/elspeth/web/composer/tool_batch.py#L1010), [src/elspeth/web/composer/tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399), [src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)).

After the handler, `finalize_tool_result` normalizes validation; discovery result admission selects the declared response contract before serialization ([src/elspeth/web/composer/tools/_dispatch.py:752](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752), [src/elspeth/web/composer/tool_batch.py:3052](../../../../src/elspeth/web/composer/tool_batch.py#L3052)). The loop records the invocation, sends a tool result back to the model, and persists its redacted tool-turn cohort; subsequent operation settlement owns user-facing success/failure ([src/elspeth/web/composer/tool_batch.py:3344](../../../../src/elspeth/web/composer/tool_batch.py#L3344), [src/elspeth/web/composer/service.py:1515](../../../../src/elspeth/web/composer/service.py#L1515), [src/elspeth/web/sessions/composer_turn.py:1051](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)). ToolCallCard renders the recorded outcome and explanatory tooltip rather than giving this tool its own direct action button ([src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:90](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L90)). A completed lookup does not imply pipeline readiness.

The current tooltip diverges from this actual producer, as recorded below.

## 4. Branches, failures and recovery

A valid call returns the same facts independently of session run history. Invalid arguments are rejected. There is no branch that returns audit rows or reconfigures logging. Argument rejection occurs before the handler. Expected policy/semantic refusal is a failed result on the current state; controlled-code exceptions remain framework/plugin failures, with the executed prefix and audit evidence retained by the common loop. Cancellation, timeout and final persistence failure belong to the request/operation owner, not a handler-local retry. None of these discovery handlers commits graph changes or needs a proposal approval step. Retrying a lookup can recover an argument or infrastructure problem but cannot repair the graph by itself.

**Cache, idempotency, concurrency and retry:** `ToolKind.DISCOVERY`, `cacheable=True`. Constant policy facts may be reused within the request; no audit database is queried and no session write lock is needed. Repeated invocation is graph/store-idempotent but still records a tool audit decision. There is no handler retry or resource-probe retry, and a later primary-provider call does not transform these constants into observed session facts.

## 5. Defects, restrictions and hypotheses

**GS-03 — confirmed user-facing description defect (audit member):** `toolCallDescriptions.ts` describes this as looking up current-session run IDs and lineage anchors ([src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts:32](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L32)). The actual handler returns constant policy facts and explicitly does not access operator configuration ([src/elspeth/web/composer/tools/generation.py:2483](../../../../src/elspeth/web/composer/tools/generation.py#L2483)); no run/session lookup exists in this path. ToolCallCard exposes `describeToolCall` in the information bubble ([src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:41](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L41)). This can make the user believe actual audit evidence was inspected. The restriction on changing audit backend is deliberate and should remain.

## 6. Ownership, remediation and dependencies

Frontend/tool-contract owner should make the tooltip describe mandatory audit policy and operator-managed export. Preserve the security boundary; adding DB inspection to justify the old text would change purpose and authority unnecessarily. Group this with list_models under capability-description parity, using one shared source of truthful descriptive metadata where practical.

**Implementation risk:** Low for correcting the tooltip. Adding dynamic audit reads would be a separate medium/high authority and disclosure change, not the proposed fix.

## 7. Evidence, regression checks and limits

Read constant-payload, nonempty text, operator-secret exclusion and unchanged-state assertions ([tests/unit/web/composer/test_tools.py:3046](../../../../tests/unit/web/composer/test_tools.py#L3046)). Backend behavior is source verified; frontend claim is source verified, not browser-rendered. No live audit configuration was inspected. Confidence: high on GS-03. Regression: assert tooltip meaning agrees with constant facts and cannot claim per-session evidence; separately retain DSN/key exclusion checks. The GetAuditInfo class passed in the second focused selection: 195 tests, exit 0.

Source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection and selected local tests do not establish live provider behavior, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

Synchronous kind: `discovery`. Declaration cache opt-in: `true`. Handler: [`elspeth.web.composer.tools.generation._execute_get_audit_info`](../../../../src/elspeth/web/composer/tools/generation.py#L2468).

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
