# `request_advisor_hint` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Ask the independent advisor for bounded guidance, including an evidence-based cannot_complete review. Advice never directly mutates graph structure. The tool is exposed in web freeform Composer and intercepted before synchronous dispatch; the CLI MCP surface deliberately excludes it. It is distinct from mandatory EARLY/END publication checkpoints.

## 2. Contracts and authority

Public schema requires an allowed trigger and structured request fields with bounded content; `_validate_advisor_arguments` returns an owned request or an argument rejection. Budget and deadline admission are server-owned. Cannot-complete review must be alone in its batch and carries current request/graph/evidence; outcomes are CONTINUE, CONTINUE_WITH_LIMITATION or STOP_CONFIRMED. Status and outer dispatch success differ: an audited dispatch may successfully report BUDGET_EXHAUSTED or ADVISOR_ERROR without useful advice ([src/elspeth/web/composer/tools/_dispatch.py:137](../../../../src/elspeth/web/composer/tools/_dispatch.py#L137), [src/elspeth/web/composer/tool_batch.py:2262](../../../../src/elspeth/web/composer/tool_batch.py#L2262)).

## 3. Source trace to the user

Batch detects mixed abandonment calls, then advisor interception validates inputs, checks budget and remaining deadline, reserves a call, and invokes AdvisorCheckpointOwner ([src/elspeth/web/composer/tool_batch.py:993](../../../../src/elspeth/web/composer/tool_batch.py#L993), [src/elspeth/web/composer/tool_batch.py:2293](../../../../src/elspeth/web/composer/tool_batch.py#L2293), [src/elspeth/web/composer/tool_batch.py:2430](../../../../src/elspeth/web/composer/tool_batch.py#L2430)). The advisor wrapper adds current graph/transcript and makes an independently audited provider call with SDK retries disabled ([src/elspeth/web/composer/advisor_checkpoint.py:312](../../../../src/elspeth/web/composer/advisor_checkpoint.py#L312), [src/elspeth/web/composer/advisor_request.py:18](../../../../src/elspeth/web/composer/advisor_request.py#L18)). Provider errors and first-party errors have distinct outcomes; successful abandonment advice is bound to request-owned evidence ([src/elspeth/web/composer/tool_batch.py:2542](../../../../src/elspeth/web/composer/tool_batch.py#L2542), [src/elspeth/web/composer/tool_batch.py:2612](../../../../src/elspeth/web/composer/tool_batch.py#L2612), [src/elspeth/web/composer/tool_batch.py:2655](../../../../src/elspeth/web/composer/tool_batch.py#L2655)).

The invocation result retains cost/accounting metadata while model feedback strips budget counters ([src/elspeth/web/composer/advisor_request.py:13](../../../../src/elspeth/web/composer/advisor_request.py#L13)). Invocation and inner LLM-call records flow into turn persistence; ToolCallCard displays the event and the planner may incorporate guidance in its subsequent user reply. No advice payload grants graph publication authority or replaces END review.

## 4. Branches, failures and recovery

Argument error: no outbound advisor call or budget use. Exhausted budget, no remaining time, or deadline too close: explicit statuses without egress. Once admitted, failure still consumes budget. Advisor-specific timeout may allow primary continuation; compose-deadline timeout ends the request path. Provider failure gives bounded feedback; malformed advisor response is not a stop confirmation. Controlled-code faults retain the actual exception and audit; prompt-guard refusal is separate. Cancellation propagates with captured inner-call evidence. Successful advice resets relevant anti-anchor tracking; a limitation preserves useful authorized work ([src/elspeth/web/composer/tool_batch.py:2325](../../../../src/elspeth/web/composer/tool_batch.py#L2325), [src/elspeth/web/composer/tool_batch.py:2369](../../../../src/elspeth/web/composer/tool_batch.py#L2369), [src/elspeth/web/composer/tool_batch.py:2470](../../../../src/elspeth/web/composer/tool_batch.py#L2470), [src/elspeth/web/composer/tool_batch.py:2643](../../../../src/elspeth/web/composer/tool_batch.py#L2643)).

**Cache, idempotency, concurrency and retry:** This intercepted async tool is not a cacheable discovery tool. Every admitted call may perform new provider work and consume budget; the tool has no same-arguments idempotency promise. Anthropic prompt-prefix caching, where supported, is a provider optimization and does not mean result replay. A hint invokes one bounded audited provider call with SDK retries disabled; the separate mandatory checkpoint workflow has its own bounded-attempt policy and must not be confused with hint retries (`advisor_request.py:38–40`, `advisor_checkpoint.py:1348`). Budget is reserved before outbound work. Calls run under request/provider custody and current evidence binding; stale advice never authorizes a graph mutation or terminal publication. Cancellation preserves inner-call evidence, and a new hint after uncertain delivery can incur additional cost.

Advice has no independent graph-mutation proposal approval; server-owned egress/budget admission governs the call. A later mutation applying advice follows its own ordinary approval policy. Completed operation replay must reuse the durable operation outcome, while issuing a fresh advisor tool call may spend again; this lane's mocked advisor tests do not establish the entire operation-replay/process-death matrix.

## 5. Defects, restrictions and hypotheses

No new advisor behavioral defect was demonstrated. **Structural observation:** argument validation, budget, timeout, failure mapping, cost audit, evidence binding and feedback shaping live across batch and advisor owners; a registry entry by itself cannot make this a first-class tool. **Restriction:** advisor suggestions require verified subsequent mutation calls and independent final review. No operational claim about live advisor quality, expense or latency is supported by mocked tests.

## 6. Ownership, remediation and dependencies

Orchestrator should own invocation admission/deadline/cancellation and durable settlement; advisor capability should own request construction, response admission and its effect declaration; provider gateway should own physical-call accounting. Consolidation must preserve outer-versus-inner records and exact request-owned abandonment evidence. Keep planner as graph author. Regression dependencies: nominal effect/result types, provider custody interface, then dispatch migration; do not replace this with server-side fallback recipes.

**Implementation risk:** High for unifying provider custody, budgets or terminal meanings; medium if merely wrapping current owners in descriptors. Retain independent outer invocation and physical provider-call accounting.

## 7. Evidence, regression checks and limits

Read audited happy-path test proving one stubbed provider call plus outer invocation and inner token record ([tests/unit/web/composer/test_advisor_tool.py:448](../../../../tests/unit/web/composer/test_advisor_tool.py#L448)), and exhausted-budget test proving four dispatch records but three provider calls ([tests/unit/web/composer/test_advisor_tool.py:750](../../../../tests/unit/web/composer/test_advisor_tool.py#L750)). Entire advisor_tool file passed in the 370-test selection; provider calls are mocked. Confidence: high on measured local accounting, moderate on all exception/persistence combinations. Required preservation oracles include pre-egress credential refusal, failure consuming budget, first-party fault retaining identity, cancelled provider evidence and cannot_complete mixed-batch rejection.

Source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection and selected local tests do not establish live provider behavior, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

This async/terminal control has no synchronous declaration; its specialized owner is traced above.

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | no |
| Composer MCP | no |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "trigger": {
      "type": "string",
      "enum": [
        "proactive_security_safety",
        "proactive_red_listed_plugin",
        "cannot_complete"
      ],
      "description": "Why this advisor call is allowed. Use cannot_complete for an early-return review of a specific suspected blocker, without requiring a formal impossibility proof. Use proactive_security_safety before set_pipeline for security/safety-sensitive flows. Use proactive_red_listed_plugin before set_pipeline when the plan uses a red-listed plugin such as llm, database, dataverse, Azure safety transforms, RAG retrieval, or Chroma sinks."
    },
    "problem_summary": {
      "type": "string",
      "maxLength": 2000,
      "description": "Your own statement of what you are trying to do and why you are stuck, with secrets and PII removed. One or two sentences. Be specific: 'I cannot get llm transform options to validate against the Azure provider schema' is useful; 'help' is not."
    },
    "recent_errors": {
      "type": "array",
      "maxItems": 5,
      "items": {
        "type": "string",
        "maxLength": 2000
      },
      "description": "Redacted summaries of the last validator errors, most recent first. Include up to 5; remove secrets and PII."
    },
    "attempted_actions": {
      "type": "array",
      "maxItems": 8,
      "items": {
        "type": "string",
        "maxLength": 2000
      },
      "description": "What you have already tried, one item per attempt. Include the tool name and a one-line summary of the argument shape. The advisor uses this to avoid suggesting things you have already ruled out."
    },
    "schema_excerpt": {
      "type": "string",
      "maxLength": 8000,
      "description": "Optional — the relevant, redacted plugin schema snippet you are working against, as returned by `get_plugin_schema`. Including this lets the advisor give field-level guidance grounded in the exact contract."
    }
  },
  "required": [
    "trigger",
    "problem_summary",
    "recent_errors",
    "attempted_actions"
  ],
  "additionalProperties": false
}
```
