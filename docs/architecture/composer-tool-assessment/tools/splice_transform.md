# `splice_transform` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Atomically insert one explicitly selected transform between two existing nodes on a direct linear success path. The model selects the insertion and plugin; the server derives mechanical connection and edge identities for that explicit edit.

Use for insert/between/before/after edits only when the predecessor is a source/transform, successor is a transform, exactly one direct success edge exists, endpoint error branches and extra consumers/visual branches are absent, and the resulting entire candidate passes context-aware validation. This is not a general graph planner or automatic repair route.

## 2. Contract

Required predecessor_id,successor_id and node:{id,plugin,options}; optional node.on_error and description. New insertion requires explicit nonempty on_error, while verified identical replay may omit it to retain the prior choice. Node IDs use runtime bounded/reserved-name rules; supplied node_type/input/on_success reject. Distinct endpoints/inserted ID are required. Derived connection tries at most32 bounded collision-free names; derived edge ID is bounded160 characters. New insertion preserves node/edge order and advances version once. Exact replay returns the original state with already_applied:true and IDs/derived_connection, omitting new/replaced edge IDs. Divergent same-ID replay refuses splice_replay_conflict. No external execution/cache/automatic retry; explicit approval applies.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/transforms.py:2214](../../../../src/elspeth/web/composer/tools/transforms.py#L2214) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1219](../../../../src/elspeth/web/composer/tools/transforms.py#L1219) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1117](../../../../src/elspeth/web/composer/tools/transforms.py#L1117) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:2029](../../../../src/elspeth/web/composer/tools/transforms.py#L2029) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:1466](../../../../src/elspeth/web/composer/tools/transforms.py#L1466) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/redaction.py:4478](../../../../src/elspeth/web/composer/redaction.py#L4478) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

Duplicate node/edge IDs, invalid topology, source/output ID collision, name exhaustion, derived-edge collision and inadmissible plugin options reject atomically. Candidate preparation repeats policy/batch/review/config/path rules. On a new insertion, the successor's runtime input and both visual edges are changed as one candidate; reviews are reconciled, then full policy-aware composition validation must pass. It forwards only newly introduced candidate diagnostics in addition to standing saved-state findings. Exact replay checks topology, configured node equivalence and canonical interpretation requirements; it is deliberately idempotent at the version level.

## 5. Usability assessment

**SG-02 — demonstrated undisclosed validity precondition; high confidence.** Known-positive direct splice succeeds. The same direct path with a pre-existing missing output refuses unchanged state with splice_validation_failed even though the requested insertion did not create that old error. The public description lists topology prerequisites but does not disclose the full-validity requirement. In contrast, ordinary incremental edits admit incomplete drafts. Workaround: repair the unrelated graph first or explicitly author the insertion with complete lower-level edits; do not retry the same splice expecting a transient fix. **SG-06:** candidate preparation is another cross-route admission implementation.

## 6. Structural remediation

Declare operation-specific admission semantics explicitly: either retain requires_valid_result and expose that precondition in machine-readable availability/description, or deliberately adopt no_new_errors draft-edit semantics after a separate design decision. Share typed transform-candidate admission while keeping splice's atomic topology/replay proof. Regression: valid insertion and exact replay pass; divergent replay/topology/collisions reject unchanged; unrelated invalid drafts yield an actionable declared precondition failure or follow an explicitly chosen no-new-errors rule; invalid inserted config still rejects; positive and negative outcomes match public contract. Compatibility risk medium/high if acceptance is widened; preserve existing mechanical ID and replay contracts.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_splice_transform_tool.py:215](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L215) — ordered atomic insertion.
- [tests/unit/web/composer/test_splice_transform_tool.py:237](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L237) — source predecessor.
- [tests/unit/web/composer/test_splice_transform_tool.py:260](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L260) — identical reviewed replay same object.
- [tests/unit/web/composer/test_splice_transform_tool.py:452](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L452) — divergent retry atomicity.
- [tests/unit/web/composer/test_splice_transform_tool.py:691](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L691) — unsupported topology rejection.
- [tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py:1](../../../../tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py#L1) — shared transform guards.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance. Controlled positive splice: `True, version5`; pre-existing invalid graph: `False`, same original state, leading splice_validation_failed plus standing missing-sink/dangling-route findings.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.transforms._handle_splice_transform`](../../../../src/elspeth/web/composer/tools/transforms.py#L2184).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | no |
| Composer MCP | yes |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "predecessor_id": {
      "type": "string",
      "description": "Existing source or transform immediately before the insertion point."
    },
    "successor_id": {
      "type": "string",
      "description": "Existing transform immediately after the insertion point."
    },
    "node": {
      "type": "object",
      "description": "The single transform to insert: provide id, plugin, options, and an explicit non-empty on_error choice for a NEW insertion. An exact already-applied replay may omit on_error to preserve its existing choice. Description is optional. Do not supply node_type, input, or on_success: the tool authors the transform kind and derives routing from the insertion point.",
      "properties": {
        "id": {
          "maxLength": 38,
          "minLength": 1,
          "pattern": "^[a-zA-Z][a-zA-Z0-9_-]*$",
          "type": "string",
          "not": {
            "enum": [
              "continue",
              "fork",
              "on_success"
            ]
          },
          "description": "Unique ID for the inserted transform."
        },
        "plugin": {
          "type": "string",
          "description": "Transform plugin name."
        },
        "options": {
          "type": "object",
          "description": "Plugin-specific authored options. Runtime-owned LLM option fields are not settable: approved_prompt_artifact_hash. Omit them; ELSPETH re-derives them during review reconciliation or execution. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
        },
        "on_error": {
          "type": [
            "string",
            "null"
          ],
          "description": "Explicit non-empty failure choice required for a NEW insertion. Omit only on a verified identical replay to preserve its choice. Null/empty is invalid; no default."
        },
        "description": {
          "type": [
            "string",
            "null"
          ],
          "description": "One short sentence of plain prose saying what this step does, shown to reviewers on the Spec tab. Supply it when creating the step and refresh it whenever you change what the step does. Informational only — never affects validation or execution."
        }
      },
      "required": [
        "id",
        "plugin",
        "options"
      ],
      "additionalProperties": false
    }
  },
  "required": [
    "predecessor_id",
    "successor_id",
    "node"
  ],
  "additionalProperties": false
}
```
