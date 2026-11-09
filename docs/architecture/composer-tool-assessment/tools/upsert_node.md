# `upsert_node` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Add or replace one explicitly selected plugin or structural node. It is the general node authoring operation; the model owns the node kind, routing and complete configuration.

Use for a new node or a complete node replacement. Use patch_node_options for option-only edits and splice_transform for its supported linear insertion. Existing configured transforms/aggregations/collectors require explicit plugin and complete author-owned options even for routing-only replacement.

## 2. Contract

Required id:string,node_type enum and input:string. Kind-specific optional fields include plugin,on_success,on_error,options(default{}),condition,routes,fork_to,branches,policy,merge,trigger,output_mode,expected_output_count,timeout_seconds,description,scope_name,scope_opener,scope_policy. Unknown keys and non-object options reject. New transform/aggregation failure choice must be explicit nonempty; omission preserves same-kind prior on_error. Gates may remain fail-fast when omitted. Collector scope_policy has no default. Coalesce defaults to require_all/union in NodeSpec. Aggregation trigger positive count/timeout and mode/count consistency are admitted. Queue requires id==input and forbids runtime routing/plugin fields; row_union has its own exact contract. Success replaces at original position or appends, advances version and returns affected neighbours, applied echo, validation and plugin hints.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/transforms.py:105](../../../../src/elspeth/web/composer/tools/transforms.py#L105) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:230](../../../../src/elspeth/web/composer/tools/transforms.py#L230) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:424](../../../../src/elspeth/web/composer/tools/transforms.py#L424) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:647](../../../../src/elspeth/web/composer/tools/transforms.py#L647) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:589](../../../../src/elspeth/web/composer/tools/transforms.py#L589) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/tools/transforms.py:890](../../../../src/elspeth/web/composer/tools/transforms.py#L890) — node/edge-plane declaration, handler and graph effects.
- [src/elspeth/web/composer/state.py:784](../../../../src/elspeth/web/composer/state.py#L784) — immutable persisted composition model.
- [src/elspeth/web/composer/state.py:8236](../../../../src/elspeth/web/composer/state.py#L8236) — immutable persisted composition model.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

The handler normalises server metadata echoes, protects runtime-owned LLM fields, stages default model/prompt review, checks credential placement and policy/schema admission, batch-aware placement/required inputs, inline prompt surfaces, profile-aware config, retry budget and path policy. Gates validate expressions and Boolean labels; structural contracts reject before mutation. Candidate sink edges are reconciled against scalar routes, then global invariants and authoritative reviews are checked. Incomplete noncanonical graph readiness can be returned as validation after accepted mutation. Queue admission has a separate early path. Operator-profile public config and private runtime lowering differ deliberately. No plugin processing or provider call occurs in the handler.

## 5. Usability assessment

**SG-07 — demonstrated argument loss defeats the queue invariant; high confidence.** Public `execute_tool(..., validate_arguments=True)` accepts a queue with any of `scope_name`, `scope_opener` or `scope_policy`, then silently stores all three as `None`. The queue-specific builder at lines612–631 omits those admitted fields before calling `queue_node_contract_error`. That guard explicitly forbids all three at `state.py:1026–1032`, so a direct NodeSpec with the same values correctly fails, while this public route succeeds. Trigger: use `upsert_node(node_type='queue', id==input)` with a collector scope field. Consequence: a forbidden request is reported successful and changed meaning before invariant checking; an invalid-input rejection contract is inconsistent across entry paths. Workaround: omit all collector scope fields on queues. This does not claim queues execute as collectors or that stored invalid queues bypass runtime validation.

**SG-01/SG-06 — source-established structural complexity; high confidence.** Node replacement owns both plugin configuration and web graph/review admission, with kind switches and policy sequences duplicated in bulk, patch and splice routes. Special cases have explicit tests, but extension requires coordinated changes across paths. **SG-03 — no-change lifecycle limitation.** Identical node replacements still advance version, which Composer interprets as progress. Workaround: read canonical authoring state before replacing; supply plugin/options explicitly and use dedicated patches for narrow edits. No production defect is inferred from the initial optional-Azure test failures: they passed once the declared SDK prerequisite was installed.

## 6. Structural remediation

Use kind-specific typed candidates plus one shared candidate-admission pipeline. Plugins own config/capability/placement declarations; graph authority owns routing/queue/barrier contracts; review authority owns review invalidation; the orchestrator chooses and sequences nodes. Return explicit applied/no_change and draft validity separately. Regression: every entry route must accept the same legal plugin config, reject wrong kind fields, mandatory failure/scope omissions, runtime-owned hashes, forged review evidence and provider policy violations, and preserve state/version on rejection. Positive controls must cover each kind and profile. Compatibility risk high across persisted NodeSpec, public args, reviews and lowering; stage behind adapters while preserving existing default/refuse semantics.

For SG-07 specifically, preserve every admitted field until kind-specific validation or reject wrong-kind fields in the argument union before construction. Regression controls: a canonical queue succeeds; each collector scope field and combinations refuse atomically through public dispatch and bulk authoring; the exact requested invalid candidate is what reaches the invariant check. Compatibility impact is a deliberate stricter refusal of previously accepted-but-discarded arguments, with no stored queue migration required by the probe.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_tools_mutations.py:1035](../../../../tests/unit/web/composer/test_tools_mutations.py#L1035) — new/replacement node, gates and expression rejection.
- [tests/unit/web/composer/test_tools_mutations.py:3924](../../../../tests/unit/web/composer/test_tools_mutations.py#L3924) — profiled Azure AI Search positive control and private-option refusals.
- [tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py:1](../../../../tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py#L1) — upsert/patch/splice/bulk guard parity.
- [tests/unit/web/composer/test_explicit_failure_choices.py:1](../../../../tests/unit/web/composer/test_explicit_failure_choices.py#L1) — preserve/require explicit failure choices.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance.

Controlled SG-07 reproduction exited 0 with a canonical queue positive control. For each of `scope_name`, `scope_opener`, `scope_policy`, the owned queue guard reported `Queue 'joined' does not accept field(s): [...]`, while public dispatch returned `success=True` and stored `(None, None, None)`. The comparison exercises actual JSON-schema admission, the production handler and owned invariant; it does not use a mock queue validator.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.transforms._handle_upsert_node`](../../../../src/elspeth/web/composer/tools/transforms.py#L391).

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
    "id": {
      "type": "string",
      "description": "Unique node identifier."
    },
    "node_type": {
      "type": "string",
      "enum": [
        "transform",
        "gate",
        "aggregation",
        "coalesce",
        "row_union",
        "queue",
        "collector"
      ],
      "description": "Node kind. Choose transform for a per-row plugin, aggregation for a batch-aware plugin with optional triggers, collector for a batch-aware plugin closing a declared EXPAND scope, gate for conditional routing or fan-out, queue for explicit fan-in, coalesce to merge branch fields, or row_union to release original branch rows without merging. Supply only fields supported by the chosen kind."
    },
    "plugin": {
      "type": [
        "string",
        "null"
      ],
      "description": "Plugin name. Required for transform/aggregation/collector. Explicitly supply it when replacing an existing configured plugin node, including routing-only edits. Null for gate/coalesce/row_union/queue."
    },
    "input": {
      "type": "string",
      "description": "Connection-name string this node CONSUMES: must equal an upstream's on_success (or routes value or fork_to connection), NOT the upstream node's id — connections match by string, not by graph topology. For queue use its shared connection name; for coalesce and row_union use the first branch connection as the required placeholder, while branches provide the actual consuming bindings."
    },
    "on_success": {
      "type": [
        "string",
        "null"
      ],
      "description": "Output connection, consumed by a downstream input/sink_name (matched by string). Required for transform/collector/row_union. Aggregation may omit it to publish under its own node id. Gates use routes/fork_to; queues omit it. A row_union MUST publish to a processing connection, never directly to a sink. A coalesce normally publishes under its own node id; its optional on_success may name only a sink."
    },
    "on_error": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "description": "Node-level error policy (transform/aggregation/gate): 'discard' or an eligible named sink. New transforms/aggregations require an explicit non-empty choice; omission on an update preserves the same node kind's existing choice. Null/empty is invalid for transforms/aggregations. For a gate it covers row expression-evaluation errors and is authored here, never as an edge; omit it to preserve fail-fast behavior. A failed aggregation batch applies this policy to every buffered row; discard records quarantine outcomes."
    },
    "options": {
      "type": "object",
      "description": "Complete author-owned plugin config for transform, aggregation, and collector nodes. Explicitly supply it when replacing an existing configured plugin node, even for routing-only edits; omitted options are not merged. A queue permits only its optional description. Other structural kinds do not take plugin configuration. The schema: block declares what ARRIVES for consumed fields and the OUTPUT type for fields the node creates. Convert arriving values at the source or with an upstream type_coerce; an output declaration checks values but does not convert them. An int satisfies float. Row-column options use the field name carried by upstream rows, normally a normalized header or source field_mapping target. Runtime-owned LLM option fields are not settable: approved_prompt_artifact_hash. Omit them; ELSPETH re-derives them during review reconciliation or execution. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
    },
    "condition": {
      "type": [
        "string",
        "null"
      ],
      "description": "Boolean expression (gate only). Evaluated per row."
    },
    "routes": {
      "type": [
        "object",
        "null"
      ],
      "additionalProperties": {
        "type": "string"
      },
      "description": "Gate route mapping {true: ..., false: ...}; each value is a sink, a connection, or 'discard' for an audited gate_discarded terminal drop. Mutually exclusive with fork_to."
    },
    "fork_to": {
      "type": [
        "array",
        "null"
      ],
      "items": {
        "type": "string"
      },
      "description": "Fork destinations — row is copied to all listed paths (gate only, mutually exclusive with routes)."
    },
    "branches": {
      "type": [
        "array",
        "object",
        "null"
      ],
      "items": {
        "type": "string"
      },
      "additionalProperties": {
        "type": "string"
      },
      "description": "Branch inputs for coalesce or row_union — list form, or {branch_name: input_connection} when a branch flows through transforms. A row_union consumes EVERY branches value as a real input and releases the original rows without merging fields."
    },
    "policy": {
      "type": [
        "string",
        "null"
      ],
      "description": "Arrival policy (coalesce only). Omitting it means 'require_all', the runtime default — every branch must arrive."
    },
    "merge": {
      "type": [
        "string",
        "null"
      ],
      "description": "Field merge strategy (coalesce only). Omitting it means 'union', the runtime default. A shared union field needs one compatible actual type on every branch; any counts as a type."
    },
    "trigger": {
      "type": [
        "object",
        "null"
      ],
      "description": "Optional early batch trigger config (aggregation only). Omit, null, or {} for end-of-source-only aggregation.",
      "additionalProperties": false,
      "properties": {
        "count": {
          "type": [
            "integer",
            "null"
          ],
          "minimum": 1,
          "description": "Flush after this many accepted rows."
        },
        "timeout_seconds": {
          "type": [
            "number",
            "null"
          ],
          "exclusiveMinimum": 0,
          "description": "Flush after this many seconds since the first accepted row."
        },
        "condition": {
          "type": [
            "string",
            "null"
          ],
          "description": "Boolean expression over row['batch_count'] and row['batch_age_seconds']; do not use end_of_source here."
        }
      }
    },
    "output_mode": {
      "type": [
        "string",
        "null"
      ],
      "enum": [
        "passthrough",
        "transform",
        null
      ],
      "description": "Aggregation output mode (aggregation only). Defaults to 'transform'. Passthrough keeps the same row tokens and admits only a batch plugin that flushes exactly one row per buffered row; check its catalogue aggregation_output_modes. Otherwise use transform."
    },
    "expected_output_count": {
      "type": [
        "integer",
        "null"
      ],
      "description": "Expected aggregation output row count for output_mode='transform' (the default); omit for 'passthrough'. Also omit when output count depends on group_by distinct values."
    },
    "timeout_seconds": {
      "type": [
        "number",
        "null"
      ],
      "exclusiveMinimum": 0,
      "description": "A finite positive structural-barrier timeout in seconds (coalesce/row_union only)."
    },
    "description": {
      "type": [
        "string",
        "null"
      ],
      "description": "One short sentence of plain prose saying what this step does, shown to reviewers on the Spec tab. Supply it when creating the step and refresh it whenever you change what the step does. Informational only — never affects validation or execution."
    },
    "scope_name": {
      "type": [
        "string",
        "null"
      ],
      "description": "Scope identifier for the EXPAND group this collector closes (collector only)."
    },
    "scope_opener": {
      "type": [
        "string",
        "null"
      ],
      "description": "Node id of the multi-row transform that opens the collector's EXPAND group (collector only)."
    },
    "scope_policy": {
      "type": [
        "string",
        "null"
      ],
      "enum": [
        "require_all",
        "best_effort",
        null
      ],
      "description": "Group arrival policy (collector only). REQUIRED for collectors — no default; the author decides whether a lost member fails the group."
    }
  },
  "required": [
    "id",
    "node_type",
    "input"
  ],
  "additionalProperties": false
}
```
