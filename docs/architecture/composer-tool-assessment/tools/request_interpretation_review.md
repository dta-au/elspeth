# `request_interpretation_review` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Stage a pending interpretation event for the user to accept or amend. This tool creates durable review work without advancing CompositionState; resolution is a later authenticated user action. It is an asynchronous session-aware tool outside synchronous ToolDeclaration dispatch, exposed to the web Composer.

## 2. Contracts and authority

Required public inputs identify affected component, interpretation kind and user term; llm_draft is optional when an exact staged draft exists. The handler requires server-owned session ID, persisted composition state ID, provider tool-call ID, provenance, rate caps and service collaborators. Those are not planner-controlled fields. Content/credential guards and component-kind/draft matching run before any row write. Success data identifies pending or opted-out review status; it does not claim the interpretation is resolved ([src/elspeth/web/composer/tools/sessions.py:3213](../../../../src/elspeth/web/composer/tools/sessions.py#L3213)).

## 3. Source trace to the user

`_dispatch` emits the async schema; freeform batch selects `SessionToolOwner` instead of execute_tool ([src/elspeth/web/composer/tools/_dispatch.py:218](../../../../src/elspeth/web/composer/tools/_dispatch.py#L218), [src/elspeth/web/composer/tool_batch.py:2726](../../../../src/elspeth/web/composer/tool_batch.py#L2726)). The session owner requires a persisted state target, validates the closed schema, and can block review creation on runtime-preflight findings ([src/elspeth/web/composer/session_tool.py:182](../../../../src/elspeth/web/composer/session_tool.py#L182)). `_handle_request_interpretation_review` validates content and exact site, deduplicates, applies vague-term caps and calls the injected session writer ([src/elspeth/web/composer/tools/sessions.py:3242](../../../../src/elspeth/web/composer/tools/sessions.py#L3242)). SessionService creates the checked event under operation authority and session lock ([src/elspeth/web/sessions/service.py:4891](../../../../src/elspeth/web/sessions/service.py#L4891)).

The tool invocation and interpretation row are separate evidence. The frontend interpretation store loads pending/resolved events; ChatPanel associates review affordances with tool-call identity ([src/elspeth/web/frontend/src/stores/interpretationEventsStore.ts:140](../../../../src/elspeth/web/frontend/src/stores/interpretationEventsStore.ts#L140), [src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx:671](../../../../src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx#L671)). User resolve passes authenticated ownership to the service, which computes accepted content and updates state/event atomically; route-level typed errors preserve 404/409/422/500 distinctions ([src/elspeth/web/sessions/routes/interpretation.py:65](../../../../src/elspeth/web/sessions/routes/interpretation.py#L65)).

## 4. Branches, failures and recovery

Missing session is an internal invariant error; missing persisted state is recoverable sequencing feedback. Invalid kind/site, credential-shaped user term or bad draft rejects before writes. Omitted draft resolves from current staged authority; duplicate current review is reused before rate limiting. Only vague_term takes per-term/day caps; server-shaped mandatory kinds remain uncapped but deduplicated. Opt-out returns suppressed status rather than a pending card. A write-lock stale-site/draft race becomes ToolArgumentError and instructs reinspection ([src/elspeth/web/composer/tools/sessions.py:3289](../../../../src/elspeth/web/composer/tools/sessions.py#L3289), [src/elspeth/web/composer/tools/sessions.py:3329](../../../../src/elspeth/web/composer/tools/sessions.py#L3329), [src/elspeth/web/composer/tools/sessions.py:3357](../../../../src/elspeth/web/composer/tools/sessions.py#L3357)). Later accept/amend can fail on stale or resolved events; retry must retain identity and cannot silently create a new approved interpretation.

**Cache, idempotency, concurrency and retry:** This asynchronous session tool is not in the cacheable discovery registry. Its idempotency is domain-specific: within the composition branch, the dedup key is kind/user_term/affected_node_id; a pending match reuses its event ID, a resolved match rejects, and suppression/cap audit rows are not deduplicated (`sessions.py:3035–3124`). Staged draft identity is checked before that lookup and again by the locked writer. No generic handler retry loop exists. A concurrent stale-site/draft change returns typed corrective feedback; operation authority and the service transaction own insertion consistency. A fresh provider call is not itself a new review identity. Do not generalize the read-then-write dedup into a proven cross-process exactly-once guarantee; PostgreSQL contention was not run.

Review staging does not require a separate graph-mutation proposal approval; the resulting interpretation event itself awaits the later user resolve action. Completed operation replay belongs to the operation owner and must reuse its durable outcome rather than issue another tool call. This lane tested current review-row dedup and resolution behavior, not a complete operation-replay/process-death matrix.

## 5. Defects, restrictions and hypotheses

No new interpretation-review defect was demonstrated. **Structural observation:** async exposure, effects and handler registration are maintained outside the declaration paradigm; a future async tool must integrate admission, durable effect and audit policy deliberately. **Restriction:** staging is not approval and does not make a graph executable. Avoid treating event insertion with unchanged graph version as a read-only operation.

## 6. Ownership, remediation and dependencies

Interpretation service owns exact event/site identity and resolution; SessionToolOwner owns context/audit routing; orchestrator owns request authority and terminal handoff; frontend owns pending/resolved affordances. A first-class async tool interface should model durable interpretation effects explicitly while preserving writer-side revalidation. Do not erase rate-limit distinctions or replace exact draft identity with text-only deduplication. Regression depends on authority/operation contracts, not a generic async function wrapper alone.

**Implementation risk:** High for dedup, draft identity, caps or settlement changes. Preserve writer-side checks and pending/resolved UI correlation; no graph version change does not mean no durable effect.

## 7. Evidence, regression checks and limits

Read happy-path test that checks both returned marker and actual DB row ([tests/unit/web/composer/test_request_interpretation_review_tool.py:1484](../../../../tests/unit/web/composer/test_request_interpretation_review_tool.py#L1484)), and production stale-draft handling. Entire interpretation-tool file passed in the 370-test selection. Confidence: high on these local contracts; moderate on full concurrency/browser flow. Needed follow-ups: PostgreSQL simultaneous stage/resolve, stale pending card after graph edit, cancellation after durable row before tool audit, opt-out resolution, and exact artifact identity despite identical draft text. No live PostgreSQL/browser run was performed.

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
  "additionalProperties": false,
  "required": [
    "affected_node_id",
    "kind",
    "user_term"
  ],
  "properties": {
    "affected_node_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 256,
      "description": "Component id. Use 'source' or 'source:<name>' for invented source data and source data contracts; use the LLM node id for vague terms and model choices."
    },
    "kind": {
      "type": "string",
      "enum": [
        "vague_term",
        "invented_source",
        "pipeline_decision",
        "llm_model_choice",
        "source_data_contract"
      ],
      "description": "Class of assumption being surfaced for review. source_data_contract asks the user to acknowledge the data contract for an uploaded/path-bound source the pipeline requires fields from: target 'source' or 'source:<name>', set user_term='source_data_contract', and OMIT llm_draft — the server computes the demanded field set from the graph."
    },
    "user_term": {
      "type": "string",
      "minLength": 1,
      "maxLength": 8192,
      "description": "Stable user-facing label for the assumption being reviewed; for kind='pipeline_decision', copy exactly one of drop_raw_html_fields, prompt_injection_shield_recommendation, or web_scrape_http_identity and never mint a decision term."
    },
    "llm_draft": {
      "type": "string",
      "minLength": 1,
      "maxLength": 8192,
      "description": "OMIT this when the review site already carries a staged interpretation_requirements draft (the normal case): the server resolves the staged draft verbatim, which is more reliable than re-transmitting multi-line text. Provide it only for a site with no staged draft, or to assert an exact match; if provided it must byte-match the staged draft."
    }
  }
}
```
