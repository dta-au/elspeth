# `upsert_node` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Add or replace one explicitly selected plugin or structural node. It is the general node authoring operation; the model owns the node kind, routing and complete configuration.

Use for a new node or a complete node replacement. Use patch_node_options for option-only edits and splice_transform for its supported linear insertion. Existing configured transforms/aggregations/collectors require explicit plugin and complete author-owned options even for routing-only replacement. Because `patch_node_options` refuses routing keys and points back here ([transforms.py:1690](../../../../src/elspeth/web/composer/tools/transforms.py#L1690)), `upsert_node` is the only incremental way to change a node's scalar routing (`input`, `on_success`, `on_error`, `routes`, `fork_to`).

## 2. Contract

Required id:string,node_type enum and input:string. Kind-specific optional fields include plugin,on_success,on_error,options(default{}),condition,routes,fork_to,branches,policy,merge,trigger,output_mode,expected_output_count,timeout_seconds,description,scope_name,scope_opener,scope_policy. Unknown keys and non-object options reject. New transform/aggregation failure choice must be explicit nonempty; omission preserves same-kind prior on_error. A new gate may omit on_error for fail-fast; an update that omits it preserves the prior gate choice, and an explicit `null` clears it ([transforms.py:698](../../../../src/elspeth/web/composer/tools/transforms.py#L698)). Collector scope_policy has no default. Coalesce defaults to require_all/union in NodeSpec. Aggregation trigger positive count/timeout and mode/count consistency are admitted. Queue requires id==input and forbids runtime routing/plugin fields; row_union has its own exact contract. The `id` is advertised as a bare string; runtime node-name rules surface only as `node_id_invalid` validation after the mutation is accepted (see upsert_node-N5).

"Supply only fields supported by the chosen kind" is guidance, not a uniform admission rule. Wrong-kind fields are refused atomically for queue and row_union, persisted with a validation error for some kinds, persisted with no finding at all for others, and silently discarded in one queue case (SG-07). The measured matrix is in §5 (upsert_node-N2).

Success replaces at original position or appends and advances version by at least one: `with_node` adds one, and each sink-mirror edge that `_reconcile_node_sink_mirror_edges` retargets or removes adds another (measured 4→6 for a transform→gate replacement that dropped one mirror edge). The result carries `affected_nodes` (the node plus every endpoint of an edge touching it, which can include sink names, e.g. `('after', 'before', 'result')`), the `applied_component` echo, `validation`/`validation_delta`, optional `data.server_owned_metadata_note`, and plugin `post_call_hints` when the plugin has any.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. `validation.is_valid` is Stage-1 composition validity, and it can be `True` for a graph the runtime validator rejects (upsert_node-N1). Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/transforms.py:105](../../../../src/elspeth/web/composer/tools/transforms.py#L105) — `_UpsertNodeArgumentsModel` (`extra="forbid"`, flat union of every kind's fields; no per-kind discrimination).
- [src/elspeth/web/composer/tools/transforms.py:230](../../../../src/elspeth/web/composer/tools/transforms.py#L230) — hand-written advertised JSON schema and per-argument descriptions.
- [src/elspeth/web/composer/tools/transforms.py:391](../../../../src/elspeth/web/composer/tools/transforms.py#L391) and [transforms.py:424](../../../../src/elspeth/web/composer/tools/transforms.py#L424) — registered handler (post-call hint attachment) and `ToolDeclaration` (MUTATION, `augments_on_failure=True`).
- [src/elspeth/web/composer/tools/transforms.py:647](../../../../src/elspeth/web/composer/tools/transforms.py#L647) — `_execute_upsert_node`: aggregation count/mode check, replacement-presence rule (670–685), failure-choice resolution (686–699), interpretation echo/runtime-owned/canonical review checks (700–739), queue early return (740–746), credential, plugin policy, batch placement, inline-prompt, prevalidation, provider policy/path checks (747–827), gate expression/parity and aggregation trigger checks (829–848).
- [src/elspeth/web/composer/tools/transforms.py:589](../../../../src/elspeth/web/composer/tools/transforms.py#L589) — `_execute_upsert_queue_node`: builds the queue `NodeSpec` without the three scope fields (612–631), runs only `queue_node_contract_error`, then commits.
- [src/elspeth/web/composer/tools/transforms.py:858](../../../../src/elspeth/web/composer/tools/transforms.py#L858) through [transforms.py:926](../../../../src/elspeth/web/composer/tools/transforms.py#L926) — general `NodeSpec` construction, row_union intrinsic guard, `with_node` commit (890), sink-mirror reconciliation (894), the selected blocking invariants of `_post_mutation_invariant_error`, review reconciliation and review contract, result construction.
- [src/elspeth/web/composer/tools/_common.py:3905](../../../../src/elspeth/web/composer/tools/_common.py#L3905) — `_MUTATION_BLOCKING_INVARIANT_CODES`: the only `validate()` codes that `_post_mutation_invariant_error` turns into a refusal; every other `validate()` code is returned as draft telemetry (the handler's own explicit checks refuse separately).
- [src/elspeth/web/composer/state.py:724](../../../../src/elspeth/web/composer/state.py#L724) — `NodeSpec` (frozen; `__post_init__` at 784 defaults coalesce policy/merge only) — immutable persisted composition model.
- [src/elspeth/web/composer/state.py:8236](../../../../src/elspeth/web/composer/state.py#L8236) — `with_node` replace-at-position/append and version increment.
- [src/elspeth/web/composer/state.py:2436](../../../../src/elspeth/web/composer/state.py#L2436) — `_runtime_connection_targets`: Stage-1 reachability producers; reads `on_success`, `on_error`, `routes`, `fork_to` from every node regardless of node_type.
- [src/elspeth/web/composer/yaml_generator.py:203](../../../../src/elspeth/web/composer/yaml_generator.py#L203) through [yaml_generator.py:329](../../../../src/elspeth/web/composer/yaml_generator.py#L329) — per-kind lowering; each kind emits only its own fields, so wrong-kind fields stored on a node are dropped before runtime.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces ([redaction.py:3852](../../../../src/elspeth/web/composer/redaction.py#L3852); CI-only directional check [schema_contract.py:575](../../../../src/elspeth/web/composer/tools/schema_contract.py#L575)).
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

### 3.1 Route inventory

| Route | Entry / consumer | Gate or policy | Notes |
|---|---|---|---|
| Canonical declaration | [transforms.py:424](../../../../src/elspeth/web/composer/tools/transforms.py#L424) via [_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) | none | `get_tool_definitions()` emits it; appendix schema is this projection. |
| Freeform compose loop (auto-apply) | `execute_tool` at [tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) | `validate_arguments=True`, `require_data_dir_for_paths=True`, `raise_schema_argument_errors=True`; runtime/structural preflight callbacks supplied | Emitted by `composer_loop_tool_definitions` ([provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391)) in both dialects. |
| Freeform explicit_approve preview + staging | Preview `execute_tool` at [tool_batch.py:2059](../../../../src/elspeth/web/composer/tool_batch.py#L2059) | `trust_mode == "explicit_approve"` and `is_mutation_tool` ([tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728)); preview runs with `runtime_preflight=None` ([tool_batch.py:2075](../../../../src/elspeth/web/composer/tool_batch.py#L2075)) | Two dispatches, not one. If the preview is valid and `wire_required_controls_state` changes it, the proposal is rewritten as a **`set_pipeline`** of the finalised state ([tool_batch.py:2108](../../../../src/elspeth/web/composer/tool_batch.py#L2108)): the user approves a set_pipeline card, not an upsert_node card. A `ToolArgumentError` in preview is swallowed ([tool_batch.py:2114](../../../../src/elspeth/web/composer/tool_batch.py#L2114)) so semantic refusal is deferred to accept. |
| Proposal accept replay (REST) | `POST /{session_id}/proposals/{proposal_id}/accept` ([proposals.py:286](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L286)); `execute_tool` at [proposals.py:492](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L492) | session ownership, compose lock, operation lease; `validate_arguments=True`, `require_data_dir_for_paths=True` | Re-admits against the *current* state; required controls re-wired only if `success`, version advanced and `validation.is_valid` ([proposals.py:519](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L519)). Version-unchanged results get a distinguished 409 ([proposals.py:551](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L551)); an identical upsert_node replacement always advances version, so it never reaches that branch. |
| Planner | not emitted | — | Measured absence: `grep -c upsert_node pipeline_planner.py` = 0 against a control of `set_pipeline` = 19. `inventory.md` records Planner default = no. |
| Recovery controls | not referenced | — | `recovery_controls.py` does not name the tool. |
| Composer MCP | `execute_tool` at [server.py:402](../../../../src/elspeth/composer_mcp/server.py#L402) | trained-operator snapshot; `validate_arguments=True`, `require_data_dir_for_paths=True`, `data_dir=None` | With `data_dir=None` the handler's `provider_config.persist_directory` check returns early ([_common.py:2393](../../../../src/elspeth/web/composer/tools/_common.py#L2393)); `require_data_dir_for_paths` covers source-local paths only. Measured (§7.1 probe 3): MCP `upsert_node` admits and stores `persist_directory="/etc/x"` (`success=True`); the shipped stdio construction's `preview_pipeline` runtime preflight then fails `path_allowlist` ("Path traversal blocked: transform 'rag' persist_directory='/etc/x' …"), and `generate_yaml` omits `persist_directory` from the export. This matches the MCP design that existing tests pin for sink paths (mutation admits, runtime preflight rejects). Whether mutation-time admission is the intended policy for the local operator route remains a maintainer question; no escape was observed. MCP responses carry no `graph_repair_suggestions` ([server.py:104](../../../../src/elspeth/composer_mcp/server.py#L104)). |
| Pre-dispatch credential scan | [credential_guard.py:221](../../../../src/elspeth/web/credential_guard.py#L221) | all routes through `execute_tool` | `_credential_node_projection` of the whole argument object. |
| Dependent-batch refusal | [batch_dependencies.py:39](../../../../src/elspeth/web/composer/batch_dependencies.py#L39), [batch_dependencies.py:68](../../../../src/elspeth/web/composer/batch_dependencies.py#L68), [batch_dependencies.py:106](../../../../src/elspeth/web/composer/batch_dependencies.py#L106) | freeform batches | `_node_produces` counts `on_success`, `routes`, `fork_to` as promised connections regardless of node_type — the same kind-blind reading as upsert_node-N1. This changes which dependents are refused (more aggressively after a failed call; `set_pipeline` subtracts promises from its own references); no defect is claimed for this consumer. |
| Redaction / audit projection | [redaction.py:3852](../../../../src/elspeth/web/composer/redaction.py#L3852), [diagnostic_evidence.py:339](../../../../src/elspeth/web/composer/diagnostic_evidence.py#L339) | transcript/audit | Separate argument models; checked by CI schema-contract tests. |
| Server-constructed upsert_node calls | `_duplicate_consumer_repair_suggestions` ([_common.py:786](../../../../src/elspeth/web/composer/tools/_common.py#L786)) | any mutation whose validation has `duplicate_consumer_connection` | Emits exact `upsert_node` argument objects — patched consumers plus a new fork gate with `condition: "True"` and derived ids — in `validation.graph_repair_suggestions[*].tool_sequence` ([_common.py:900](../../../../src/elspeth/web/composer/tools/_common.py#L900)). The model must still issue them. Recorded as a route; its standing against Composer invariant 1 is a question for the maintainer, not a finding here. |
| Frontend proposal rendering | [toolCallDescriptions.ts:68](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L68), [ProposalDiff.tsx:581](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx#L581) | explicit_approve proposals | Diff "after" is the raw requested arguments, summarised by node_type+plugin only ([ProposalDiff.tsx:76](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx#L76)); any existing id renders as `changed` with no equality check, and server-preserved fields (an omitted prior `on_error`) are not shown. |
| Tutorial | none | — | No tutorial branch in `transforms.py`, `_dispatch.py`, `batch_dependencies.py` or `credential_guard.py` (`grep -i tutorial` = 0 matches; control: the same grep over `web/composer/*.py` matches tutorial modules such as `tutorial_service.py` and `composition_completion.py`). |

## 4. Alternative paths

The handler normalises server metadata echoes, protects runtime-owned LLM fields, stages default model/prompt review, checks credential placement and policy/schema admission, batch-aware placement/required inputs, inline prompt surfaces, profile-aware config, retry budget and path policy. Gates validate expressions and Boolean labels; structural contracts reject before mutation. Candidate sink edges are reconciled against scalar routes, then global invariants and authoritative reviews are checked. Incomplete noncanonical graph readiness can be returned as validation after accepted mutation. Queue admission has a separate early path. Operator-profile public config and private runtime lowering differ deliberately. No plugin processing or provider call occurs in the handler.

The queue early return at [transforms.py:740–746](../../../../src/elspeth/web/composer/tools/transforms.py#L740) bypasses everything after it: sink-mirror edge reconciliation, `_post_mutation_invariant_error`, `reconcile_authoritative_reviews` and `composition_review_contract_error`. The skipped edge reconciliation and the skipped review reconciliation are both demonstrated (upsert_node-N4).

Refusal policy is uneven across required fields. A missing failure choice on a new transform/aggregation refuses atomically (`failure_route_required`), while a missing `plugin` on a new transform is persisted with `transform_missing_plugin` telemetry (measured). Both are documented as required.

## 5. Usability assessment

**SG-07 — demonstrated argument loss defeats the queue invariant; high confidence.** Public `execute_tool(..., validate_arguments=True)` accepts a queue with any of `scope_name`, `scope_opener` or `scope_policy`, then silently stores all three as `None`. The queue-specific builder at lines 612–631 omits those admitted fields before calling `queue_node_contract_error`, despite its docstring saying it constructs the node "from the validated arguments verbatim". That guard explicitly forbids all three at `state.py:1026–1032`, so a direct NodeSpec with the same values correctly fails, while this public route succeeds. Trigger: use `upsert_node(node_type='queue', id==input)` with a collector scope field. Consequence: a forbidden request is reported successful and changed meaning before invariant checking; an invalid-input rejection contract is inconsistent across entry paths. The bulk route does not share the defect: `set_pipeline` with the same queue plus `scope_name` refuses with `queue_config_invalid` (in-memory dispatch run, this review). Workaround: omit all collector scope fields on queues. This does not claim queues execute as collectors or that stored invalid queues bypass runtime validation. SG-07 is one cell of the broader matrix in upsert_node-N2.

**upsert_node-N1 — demonstrated false-green validation for runtime-inert routing fields; high confidence.** Stage-1 reachability treats a node's `on_success`, `routes` and `fork_to` as producers regardless of node_type ([state.py:2467](../../../../src/elspeth/web/composer/state.py#L2467), [state.py:2475](../../../../src/elspeth/web/composer/state.py#L2475)). Its docstring says it "must follow the same routing model as generate_yaml() and DAG build", but gate lowering never emits `on_success` ([yaml_generator.py:227](../../../../src/elspeth/web/composer/yaml_generator.py#L227); runtime `GateSettings` has no such field and is `extra="forbid"`), and transform lowering never emits `fork_to` ([yaml_generator.py:203](../../../../src/elspeth/web/composer/yaml_generator.py#L203)). Trigger: `upsert_node` a gate with `on_success="side"` (or a transform with `fork_to=["side"]`), then a transform consuming `side`. Both calls succeed; the returned `validation.is_valid` is `True` with no errors; `validate_pipeline_for_trained_operator` on the same state fails `graph_structure` with "No producer for connection 'side'". Positive control: the same topology with the gate routing `true → side` is valid in both validators. The aggregation (`fork_to`, `routes`) and coalesce (`routes`, `fork_to`) cases are also demonstrated: in each, the tool result and `state.validate()` are valid while the runtime validator fails "No producer for connection 'side'". Each has a positive control (a lowered producer, valid in both validators) and an isolation control (inert field absent: Stage-1 `node_input_not_reachable` on `node:t1`). See §7.1 probe 1. Consequence: the tool's own validity signal tells the model a graph is complete when it cannot run. This is demonstrated in a scripted freeform compose turn (§7.1 probe 2). Both upsert_node results the model received said `is_valid: true`. The failure reached it only through the loop's pre-finalisation runtime preflight message, and through `preview_pipeline` when the model called it. In both it appears as `1. [?:transform_t1_<hash>] No producer for connection 'side'`. That line has no error code (`?`), names the consuming transform rather than the gate, and never mentions `on_success`. Existing tests encode the Stage-1 reading: producer-resolver fixtures build gates with `on_success` ([test_producer_resolver.py:152](../../../../tests/unit/web/composer/test_producer_resolver.py#L152), [:164](../../../../tests/unit/web/composer/test_producer_resolver.py#L164)), and an AST test pins the body of `_runtime_connection_targets` ([test_producer_resolver.py:473](../../../../tests/unit/web/composer/test_producer_resolver.py#L473)).

**upsert_node-N2 — demonstrated inconsistent wrong-kind field admission; high confidence.** One canonical node per kind, plus each field the kind does not own, through public dispatch with `validate_arguments=True` (in-memory handler/dispatch run):

| Outcome | Kind × field (wrong-kind) |
|---|---|
| Atomic refusal | queue × every non-scope field (`queue_config_invalid`); row_union × plugin/on_error/options/condition/routes/fork_to/policy/merge/trigger/output_mode/expected_output_count (`row_union_config_invalid`); gate, coalesce × plugin (`structural_node_plugin_forbidden`), × options (`gate_config_invalid`/`coalesce_config_invalid`); transform, gate, aggregation, collector × timeout_seconds (`node_timeout_unsupported`) |
| Persisted, with a node error | transform × condition/routes (`transform_unexpected_*`); any non-collector non-queue kind × scope_* (`node_scope_fields_unsupported`); collector × on_error, trigger (`collector_has_*_invalid`), × condition/routes/fork_to/branches/policy/merge/output_mode/expected_output_count (`collector_config_invalid`) |
| Persisted, **no finding** | transform × fork_to/branches/policy/merge/trigger/output_mode/expected_output_count; gate × on_success/branches/policy/merge/trigger/output_mode/expected_output_count; aggregation × condition/routes/fork_to/branches/policy/merge; coalesce × on_error/condition/routes/fork_to/trigger/output_mode/expected_output_count |
| Silently dropped | queue × scope_name/scope_opener/scope_policy (SG-07) |

Every "no finding" field is discarded by its kind's lowering (§3), so the stored state contains authored intent that never runs and that no validator mentions. Where the field is also a Stage-1 producer (gate on_success, transform/aggregation/coalesce fork_to/routes) the silence becomes the false green of N1. Consequence for the agent: the tool contradicts its own description ("Supply only fields supported by the chosen kind"), refusal semantics depend on kind, and an authoring mistake can survive to runtime.

**upsert_node-N3 — demonstrated propagation of inert fields through the advertised authoring payload; high confidence.** A transform stored with inert `fork_to` and `trigger` returns both in `get_pipeline_state(component='set_pipeline_arguments')`, which is the payload upsert_node's own replacement refusal tells the model to read ([transforms.py:679–682](../../../../src/elspeth/web/composer/tools/transforms.py#L679)). Resubmitting that payload through `set_pipeline` succeeds with `validation.is_valid=True` (in-memory dispatch). Once admitted, inert fields are carried forward by the documented read-modify-write loop rather than corrected by it.

**upsert_node-N4 — demonstrated queue-path divergence from shared post-commit work, including stale review evidence that blocks later mutations; high confidence.** Replacing configured transform `after` (sink-mirror edge `after → result`) with a canonical queue `after` leaves the stale edge in state (`edge_not_lowerable` on `edge:after-output`); replacing it with a gate removes the edge through `_reconcile_node_sink_mirror_edges`. The stale edge is reported, not silent. The same early return also skips `_post_mutation_invariant_error`, `reconcile_authoritative_reviews` and `composition_review_contract_error`.

The review consequence is demonstrated (§7.1 probe 4). Review applicability reads the output-stream graph ([interpretation_state.py:3467](../../../../src/elspeth/web/interpretation_state.py#L3467)), and the prompt-shield artifact hash binds every upstream path, including declared queues.
- Fixture: the owned `_reviewed_shield_state()` (`web_scrape → mid → classify`, with a resolved prompt-shield `pipeline_decision` on the LLM).
- Probe: `upsert_node` queue `inbound` (`id == input`) succeeds and changes the review's derived artifact hash. The row stays `resolved` with the old hash, so `pending_execution_interpretation_sites` reports nothing owed, and `materialize_state_for_execution` raises `InterpretationReviewIntegrityError: node 'classify' pipeline-decision review hash drifted`.
- After that, three later mutations are refused with `review_reconciliation_failed`, "resolved interpretation requirement 'prompt_injection_shield_review:classify' hash drifted":
  - an unrelated `upsert_node` (a new passthrough);
  - `remove_node` of the queue itself;
  - `patch_node_options` on `classify`.

  The only tested escape is `remove_node classify`, which succeeds by deleting the reviewed LLM node.
- Controls on the same fixture:
  - a non-queue graph change (`remove_node mid`) re-opens the review cleanly (`pending`, `InterpretationReviewPending`);
  - the same unrelated `upsert_node` without the queue succeeds and keeps the review.

Not shown:
- whether `set_pipeline` can recover the draft (the fixture's hand-built `web_scrape` options fail bulk plugin prevalidation, so that control could not run);
- whether a live session reaches this state through the real resolve route.

Consequence: one admitted queue insertion turns server-owned review evidence into an integrity fault. Among the tested tools, the draft can then only be changed by deleting the reviewed node.

**upsert_node-N5 — demonstrated undisclosed node-name precondition; high confidence.** The emitted `id` schema is `{"type": "string", "description": "Unique node identifier."}`. `splice_transform` ([transforms.py:2203](../../../../src/elspeth/web/composer/tools/transforms.py#L2203)) and `set_pipeline` `nodes[].id` ([schema_contract.py:386](../../../../src/elspeth/web/composer/tools/schema_contract.py#L386)) disclose the runtime node-name pattern, length cap and reserved labels via `_disclose_node_name_constraints`. `upsert_node` with id `""`, a 60-character id or `"bad id!"` each succeeds and persists with `node_id_invalid`. `set_pipeline` also persists a bad id as a draft, so this finding concerns disclosure parity, not a refusal difference.

**upsert_node-N6 — source-established generic refusal code for the replacement-presence rule; high confidence.** The plugin+options presence refusal uses `error_code="argument_error"` ([transforms.py:683](../../../../src/elspeth/web/composer/tools/transforms.py#L683)) and does not name which of `plugin`/`options` was omitted. The message is actionable; the code is not distinguishable from other argument failures by a caller that branches on codes.

**SG-01/SG-06 — source-established structural complexity; high confidence.** Node replacement owns both plugin configuration and web graph/review admission, with kind switches and policy sequences duplicated in bulk, patch and splice routes. Special cases have explicit tests, but extension requires coordinated changes across paths. **SG-03 — no-change lifecycle limitation, demonstrated for this tool.** An identical replacement of `after` (same plugin, input, routing, options) advanced version 4→5 with content otherwise equal, and `tool_result_mutated_composition_state` reported progress `True` (in-memory dispatch, this review). The proposal diff likewise renders any existing id as `changed`. Workaround: read canonical authoring state before replacing; supply plugin/options explicitly and use dedicated patches for narrow edits. No production defect is inferred from the initial optional-Azure test failures: they passed once the declared SDK prerequisite was installed.

### 5.1 Guidance parity

What the model is told, compared with what the handler does. Skills were grepped for `upsert_node` (composer skill 4 hits; capabilities skill 0 hits, with control `set_pipeline` = 31 and 2 respectively) and for the node concepts the tool handles.

- **Kind-field guidance implies enforcement that only exists for some kinds.** Declaration "Omit fields that don't apply to your node_type" ([transforms.py:449](../../../../src/elspeth/web/composer/tools/transforms.py#L449)) and `node_type` "Supply only fields supported by the chosen kind" ([transforms.py:241](../../../../src/elspeth/web/composer/tools/transforms.py#L241)) read as a contract; N2 shows it is enforced only for queue/row_union and partially elsewhere. Nothing tells the model that a stray field may be kept, ignored at runtime and never reported.
- **`on_success` "Gates use routes/fork_to"** ([transforms.py:266](../../../../src/elspeth/web/composer/tools/transforms.py#L266)) is accurate for runtime, but the tool admits, stores and counts a gate `on_success` as a producer (N1). The guidance is right; the tool does not agree with it.
- **Gate `on_error` omission.** Schema: "omit it to preserve fail-fast behavior" ([transforms.py:278](../../../../src/elspeth/web/composer/tools/transforms.py#L278)); capabilities [pipeline_capabilities.md:145–146](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L145) says the same. On an update, omission preserves the prior gate choice ([transforms.py:698](../../../../src/elspeth/web/composer/tools/transforms.py#L698)). The capabilities skill's own topology paragraph has the precise rule ("New gates omit the field for fail-fast; updates preserve an omitted prior choice and can clear it to fail-fast with null", [pipeline_capabilities.md:106–108](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L106)), so the skill contradicts itself, and the tool description carries the less precise version.
- **Collector "rejected" wording.** [pipeline_capabilities.md:193](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L193) says "`trigger` is rejected on a collector" and [:205](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L205) "A collector does not accept `on_error`". Through upsert_node both are *accepted and stored* with `collector_has_trigger_invalid` / `collector_has_on_error_invalid` telemetry. A model expecting an atomic refusal will see `success=True`.
- **Replacement requirement missing from the selection table.** [pipeline_composer.md:178](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L178) routes "Add or rewire a node" to `upsert_node` without saying that rewiring a configured transform/aggregation/collector requires resubmitting plugin and complete options. Only the declaration and the refusal teach it. The capabilities skill never names the tool; its node-kind text ([pipeline_capabilities.md:137–222](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L137)) is tool-agnostic and consistent with the declaration apart from the items above, which is acceptable as long as the declaration carries the tool-specific rules.
- **Node-name constraints undisclosed** (N5). The model learns the id rule only from a post-mutation `node_id_invalid`, while sibling tools advertise it in schema.
- **Queue has two description slots.** Top-level `description` (Spec tab, [transforms.py:365](../../../../src/elspeth/web/composer/tools/transforms.py#L365)) and `options.description` (the only option a queue permits and the only one lowered, [yaml_generator.py:196](../../../../src/elspeth/web/composer/yaml_generator.py#L196)). Neither the declaration nor the skills say which to use; low impact.
- **User-facing tooltip understates the tool (GS-03 class).** [toolCallDescriptions.ts:68](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L68): "Adds a new transform or gate node…", while the tool authors seven kinds.
- No stale references found: the capabilities canonical node field list ([pipeline_capabilities.md:250](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L250)) names exactly the 21 advertised properties. Neither skill pushes the model toward a worse tool for this job.

## 6. Structural remediation

Use kind-specific typed candidates plus one shared candidate-admission pipeline. Plugins own config/capability/placement declarations; graph authority owns routing/queue/barrier contracts; review authority owns review invalidation; the orchestrator chooses and sequences nodes. Return explicit applied/no_change and draft validity separately. Regression: every entry route must accept the same legal plugin config, reject wrong kind fields, mandatory failure/scope omissions, runtime-owned hashes, forged review evidence and provider policy violations, and preserve state/version on rejection. Positive controls must cover each kind and profile. Compatibility risk high across persisted NodeSpec, public args, reviews and lowering; stage behind adapters while preserving existing default/refuse semantics.

For SG-07 specifically, preserve every admitted field until kind-specific validation or reject wrong-kind fields in the argument union before construction. Regression controls: a canonical queue succeeds; each collector scope field and combinations refuse atomically through public dispatch, with bulk authoring kept as the control (it already refuses); the exact requested invalid candidate is what reaches the invariant check. Compatibility impact is a deliberate stricter refusal of previously accepted-but-discarded arguments, with no stored queue migration required by the probe.

For N1/N2, make Stage-1 reachability read connections through one per-kind "what this kind publishes at runtime" authority shared with lowering, instead of reading `on_success`/`routes`/`fork_to` off every node. Pair it with one intrinsic per-kind field contract — the generalisation of `queue_node_contract_error` and the row_union guard — used by upsert_node, set_pipeline and `validate()`. Decide per field whether a wrong-kind value refuses the mutation (recommended for fields lowering discards) or is reported as a node error; never neither. Regression: the N2 matrix as a parametrised test with no "persisted, no finding" cell; the N1 gate-`on_success` and transform-`fork_to` cases must be invalid in Stage 1 and agree with `validate_pipeline`; the positive control (gate route to the same connection) stays valid. Compatibility: persisted sessions holding such fields will begin reporting errors (or refusing re-submission); that is an honest reinspection requirement, not a migration. The AST pin and gate fixtures in `test_producer_resolver.py` must change with the fix.

For N4, run the queue candidate through the same post-commit sequence as other kinds (sink-mirror reconciliation, blocking invariants, review reconciliation and contract), with the intrinsic queue guard as an additional precondition rather than a replacement for the shared sequence. Regression: the §7.1 probe-4 case. A queue inserted on a reviewed LLM's input must re-open the prompt-shield review as `pending`, and must not leave a resolved row whose hash has drifted. A later unrelated mutation must still succeed.

### 6.1 Simple enhancements

1. **Preserve queue scope fields until the guard runs (SG-07).** Where: [transforms.py:612–631](../../../../src/elspeth/web/composer/tools/transforms.py#L612). What: pass `scope_name`, `scope_opener`, `scope_policy` into the queue `NodeSpec`. Why: the guard can then refuse what it already forbids, and the docstring's "verbatim" claim becomes true. Acceptance: canonical queue succeeds; each scope field alone and all three together return `queue_config_invalid` with state and version unchanged.
2. **Disclose the node-name rule on `id` (N5).** Where: the `id` property at [transforms.py:233](../../../../src/elspeth/web/composer/tools/transforms.py#L233). What: apply `_disclose_node_name_constraints`, as splice_transform and set_pipeline do. Why: the precondition becomes visible before a call. Acceptance: emitted schema carries `pattern`/`maxLength`/`not`; `test_tool_schema_contract` (directional check, [schema_contract.py:575](../../../../src/elspeth/web/composer/tools/schema_contract.py#L575)) still passes. Its walker compares requiredness/nullability/enum/typed branches, not `pattern`/`maxLength` ([schema_contract.py:303](../../../../src/elspeth/web/composer/tools/schema_contract.py#L303)). The same walker already tolerates this disclosure for set_pipeline: `canonical_set_pipeline_schema` adds the naming constraints and runs `assert_set_pipeline_schema_compatible` on every call (docstring at [schema_contract.py:575](../../../../src/elspeth/web/composer/tools/schema_contract.py#L575)). Confirm by running the test. Consider the same for `input`/`on_success`/`on_error`/`fork_to` items to match set_pipeline.
3. **Refuse `on_success` on a gate (N1 containment).** Where: the gate branch near [transforms.py:829](../../../../src/elspeth/web/composer/tools/transforms.py#L829), or a new code in `_MUTATION_BLOCKING_INVARIANT_CODES` backed by `validate()`. Why: it is runtime-inert and creates the false green. Acceptance: gate + `on_success` refuses with state/version unchanged; gate routing to the same connection still succeeds; Stage-1 and `validate_pipeline` agree on the N1 probe.
4. **Report the other "no finding" cells (N2 containment).** Where: per-kind arms of `CompositionState.validate()` ([state.py:8667](../../../../src/elspeth/web/composer/state.py#L8667) onward). What: emit a node error for every field that kind's lowering discards (transform fork_to/branches/policy/merge/trigger/output_mode/expected_output_count; gate branches/policy/merge/trigger/output_mode/expected_output_count; aggregation condition/routes/fork_to/branches/policy/merge; coalesce on_error/condition/routes/fork_to/trigger/output_mode/expected_output_count). Acceptance: the N2 matrix has no "persisted, no finding" row; a canonical node of each kind stays free of the new codes.
5. **Specific code for the replacement-presence refusal (N6).** Where: [transforms.py:677–685](../../../../src/elspeth/web/composer/tools/transforms.py#L677). What: a dedicated `error_code` (e.g. `replacement_requires_plugin_and_options`) and the missing field name(s) in the message. Acceptance: omitting only `options` names `options`; omitting both names both; existing tests asserting `argument_error` are updated.
6. **Correct gate `on_error` omission text.** Where: [transforms.py:278](../../../../src/elspeth/web/composer/tools/transforms.py#L278) and [pipeline_capabilities.md:145–146](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L145). What: "New gates omit it for fail-fast; on update omission preserves the prior choice; send null to clear it." Acceptance: text matches [transforms.py:698](../../../../src/elspeth/web/composer/tools/transforms.py#L698) and the topology paragraph; any skill-hash/prompt fixtures are regenerated.
7. **Collector wording.** Where: [pipeline_capabilities.md:193](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L193), [:205](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L205). What: say these fields are reported invalid (`collector_has_trigger_invalid`/`collector_has_on_error_invalid`) unless enhancement 4's policy makes them refuse; then say "refused". Acceptance: wording matches measured behaviour.
8. **Selection-table note.** Where: [pipeline_composer.md:178](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L178). What: add "rewiring a configured plugin node resubmits plugin and complete options; read `set_pipeline_arguments` first". Acceptance: guidance matches the refusal at [transforms.py:679](../../../../src/elspeth/web/composer/tools/transforms.py#L679).
9. **Frontend tooltip.** Where: [toolCallDescriptions.ts:68](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L68). What: describe it as adding or replacing any pipeline node (transform, gate, aggregation, collector, coalesce, row union or queue). Acceptance: `toolCallDescriptions.test.ts` updated.
10. **Missing tests.** Add a public-dispatch test for queue × each scope field (none exists: `node_scope_fields_unsupported` appears only in `test_state_collectors.py`, which builds NodeSpecs directly, and `upsert_node` does not appear in that file). Add a Stage-1/runtime agreement test for the N1 cases next to the route-target agreement tests in `tests/integration/pipeline/test_composer_runtime_agreement.py`.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_tools_mutations.py:1034](../../../../tests/unit/web/composer/test_tools_mutations.py#L1034) — `TestUpsertNode`: new/replacement node, gates and expression rejection.
- [tests/unit/web/composer/test_tools_mutations.py:3924](../../../../tests/unit/web/composer/test_tools_mutations.py#L3924) — profiled Azure AI Search positive control and private-option refusals.
- [tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py:1](../../../../tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py#L1) — upsert/patch/splice/bulk guard parity.
- [tests/unit/web/composer/test_explicit_failure_choices.py:1](../../../../tests/unit/web/composer/test_explicit_failure_choices.py#L1) — preserve/require explicit failure choices.
- [tests/unit/web/composer/test_upsert_node_replacement_presence.py:1](../../../../tests/unit/web/composer/test_upsert_node_replacement_presence.py#L1) — public replacements must not erase omitted plugin configuration (not run by this review).
- [tests/unit/web/composer/test_transform_rejection_contracts.py:154](../../../../tests/unit/web/composer/test_transform_rejection_contracts.py#L154) — queue `id != input` refusal through upsert_node; no scope-field case (run as part of the §7.1 probe-5 baseline).
- [tests/unit/web/composer/test_producer_resolver.py:473](../../../../tests/unit/web/composer/test_producer_resolver.py#L473) — AST pin on `_runtime_connection_targets`, relevant to any N1 fix.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance.

Controlled SG-07 reproduction exited 0 with a canonical queue positive control. For each of `scope_name`, `scope_opener`, `scope_policy`, the owned queue guard reported `Queue 'joined' does not accept field(s): [...]`, while public dispatch returned `success=True` and stored `(None, None, None)`. The comparison exercises actual JSON-schema admission, the production handler and owned invariant; it does not use a mock queue validator.

**Sheet review probes (2026-11-09).** These were in-memory handler/dispatch runs, not end-to-end service runs. Each used `execute_tool(..., validate_arguments=True)` with the `_state()`/`_context()` fixtures from `tests/unit/web/composer/test_splice_transform_tool.py`. `elspeth.__file__` resolved into the assessment worktree. Those in-memory probes used no pytest, provider or network; the pytest probe runs are in §7.1.

```text
N1  stage-1 isolation (identical gate routes {true: result, false: discard}; only on_success differs):
      control_no_on_success  is_valid False [node:t1 node_input_not_reachable]
      gate_on_success=side   is_valid True  []
N1  control_routes_to_side  stage1_is_valid True   runtime_is_valid True
N1  gate_on_success_side    stage1_is_valid True   runtime_is_valid False
      failed: graph_structure, route_target_resolution, ...  "No producer for connection 'side'."
      lowered gate: {name, input, condition, routes}  (no on_success)
N1  transform_fork_to_side  tool_result validation.is_valid True []   runtime_is_valid False ("No producer for connection 'side'")
N2  wrong-kind matrix: see §5 table (each kind's canonical node x each of 17 sample fields it does not already carry)
N3  set_pipeline_arguments carries {'fork_to': ['side'], 'trigger': {'count': 3}}; set_pipeline resubmit success True is_valid True
SG-07 bulk control: set_pipeline queue+scope_name success False ['queue_config_invalid']
N4  gate_replacement_control version 4->6, edge after-output removed
N4  queue_replacement        version 4->5, edge after-output kept; edge:after-output edge_not_lowerable
N5  id '' / 60 chars / 'bad id!'  success True, node_id_invalid (set_pipeline: same draft behaviour)
SG-03 identical replacement version 4->5, content equal, progress True
```

Runtime validation used `validate_pipeline_for_trained_operator` and `SimpleNamespace(data_dir=<lane scratch directory>)` as its settings. Source and sink paths were placed under `managed_blob_directory(<scratch>)/<session_id>` and `<scratch>/outputs/<session_id>`. Instrument control: the first runtime attempt with fixture-relative paths failed `path_allowlist` for both the control and the case, so it said nothing about routing. Only the rerun, in which the control passed every runtime check, is used as evidence.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. The freeform-turn evidence for N1 used a scripted model and a clean-patched advisor checkpoint, not a live provider. Two N4 points are untested: whether `set_pipeline` can recover a draft in the probe-4 state, and whether a live session reaches that state through the real resolve route. Whether MCP mutation-time admission of out-of-root provider paths is intended is a maintainer question (§3.1). Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

### 7.1 Probe runs (2026-11-09)

These probes ran against the 0.8.9 production source in the assessment worktree.
- `$WT` is the worktree root. `$L` is the lane-private probe directory, `.claude/lanes/composer-sheet-review-20261109/probes/upsert_node`. All scripts, logs and `TMPDIR` are under `$L`.
- Every run used the fail-closed prefix `HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 NO_PROXY= LITELLM_LOCAL_MODEL_COST_MAP=True ELSPETH_DISABLE_NETWORK=1 PYTHONDONTWRITEBYTECODE=1`.
- Every run also used the in-process guard `$L/un_guard.py`. It makes `litellm.acompletion`, `litellm.completion`, `provider_gateway._litellm_acompletion`, `pipeline_planner._litellm_acompletion` and IPv4/IPv6 `socket.connect` raise. Its positive control (each entry point raises when called unscripted) passed in every log, and every arm recorded zero guard hits.
- `elspeth.__file__` resolved to `$WT/src/elspeth/__init__.py` in every log.
- Pytest probes ran through `$L/run_pytest.sh`, which uses `-n 0 -p no:cacheprovider --rootdir $WT -c $WT/pyproject.toml`. Conftests were loaded with `-p tests.conftest -p tests.unit.conftest -p tests.unit.web.conftest -p tests.unit.web.composer.conftest -p tests.integration.conftest -p tests.integration.web.conftest`. `--setup-show` confirmed the autouse fixtures `_composer_available_for_phase3` and `_fenced_compose_for_legacy_tests`.

| # | Probe | Finding | Outcome | Exit | Log |
|---|---|---|---|---|---|
| 1 | Aggregation/coalesce Stage-1 vs runtime validity | N1 | **confirmed** | 0 | `$L/probe1.log` |
| 2 | Scripted freeform turn with the gate-`on_success` pair | N1 (turn) | **confirmed** | 0 (4 passed) | `$L/probe2.log` |
| 3 | MCP `persist_directory="/etc/x"` | §3.1 MCP row | **confirmed** admission; caught later by preview | 0 (6 passed) | `$L/probe3.log` |
| 4 | Review status after a queue insertion | N4 | **confirmed**, worse than hypothesised | 0 (1 passed) | `$L/probe4.log` |
| 5 | Existing suites touched by §6.1, before any change | baseline | **inconclusive** (a baseline by construction: it tests no finding; 307 passed) | 0 | `$L/probe5.log` |

**Probe 1** (`cd $WT && <prefix> PYTHONPATH=$L:$WT:$WT/src:$WT/elspeth-lints/src <venv>/bin/python $L/probe1_n1_agg_coalesce.py $L`).
- Base fixture: `_state()` with the source and sink repointed under `managed_blob_directory(<scratch>)/<sid>` and `<scratch>/outputs/<sid>`. Each node was authored with public `execute_tool("upsert_node", validate_arguments=True)`.
- The lane report's aggregation options (`actual_field`, `predicted_field`) had to name two different fields: the plugin refuses identical ones. The source schema gained a `label` column for this.
- Runtime used `validate_pipeline_for_trained_operator(state, SimpleNamespace(data_dir=<scratch>), yaml_generator, session_id=<sid>)`.

```text
                                      (stage1 is_valid, runtime is_valid)
AGG  ctl_pos  on_success=side                 (True,  True)
AGG  ctl_iso  no inert field                  (False, False)  node:t1 node_input_not_reachable
AGG  probe    fork_to=[side]                  (True,  False)  "No producer for connection 'side'"
AGG  probe    routes={true: side}             (True,  False)  same
COAL ctl_pos  merged -> gate g2 true:side     (True,  True)
COAL ctl_iso  no inert field                  (False, False)  node:t1 node_input_not_reachable
COAL probe    routes={true: side}             (True,  False)  same
COAL probe    fork_to=[side]                  (True,  False)  same
lowered aggregation keys: input,name,on_error,on_success,options,plugin   (no fork_to/routes)
lowered coalesce keys:    branches,merge,name,on_success,policy           (no fork_to/routes)
```

**Probe 2** (`$L/run_pytest.sh probe2 $L/test_probe2_n1_turn.py --setup-show`).
- Harness: cloned from the set_source lane. It runs `_run_one_turn_for_test` on an auto-commit session with a real `data_dir`. The primary model is a scripted `_call_llm` (`create_autospec`), the advisor checkpoint is patched with `_clean_advisor_checkpoint`, and every litellm alias is guarded.
- Arm a: the model authors gate `g` (`routes {true: result, false: discard}`, `on_success: side`) and transform `t1` (`input: side`), then says "Done."
- Both tool results said `success: true, is_valid: true` with no errors.
- The loop then injected the following message twice (5 LLM awaits in total):

```text
[composer-system] Pre-finalisation runtime preflight found contract violation(s) …
1. [?:transform_t1_e7eff0f8b3dc] No producer for connection 'side'. Did you mean: middle?
```

The message names `t1` but not `g`, and carries no error code.
- Arm b adds a `preview_pipeline` call. Its `runtime_preflight.errors[0]` is `component_id=transform_t1_…, component_type=null, error_code=null`, with the same message.
- Controls:
  - isolation (no `on_success`): the t1 result is already `is_valid: false` with `node:t1 node_input_not_reachable`;
  - positive (gate `routes true: side`): preview runtime preflight `is_valid: true`.
- Incidental: the persisted `preview_pipeline` chat (`role=tool`) row carries `_unknown_response` and no `runtime_preflight`, so the failure the model saw is not in that transcript row. The private `role=audit` evidence row, which retains the credential-filtered result for other tools (`get_pipeline_state` §7.1 P3, `request_advisor_hint` §7.1), was not read in this probe, so this is not evidence that the audit record loses it.

**Probe 3** (`$L/run_pytest.sh probe3 $L/test_probe3_mcp_persist_directory.py`).

| Arm | Route | `persist_directory` | Result |
|---|---|---|---|
| A | `composer_mcp.server._dispatch_tool` | `/etc/x` | `success: true`, version 1→2, stored `/etc/x` |
| A | `composer_mcp.server._dispatch_tool` | `outputs/chroma` | `success: true`, version 1→2, stored `outputs/chroma` |
| B | shipped `_build_stdio_server` (set_source, upsert_node, set_output, preview_pipeline) | `/etc/x` | upsert `success: true`; preview Stage-1 valid, runtime preflight fails `path_allowlist` ("Path traversal blocked …") |
| B | same | in-root | `path_allowlist` passes; preflight fails later on unrelated `plugin_instantiation` |
| C | web-style `execute_tool` with `data_dir` and `session_id` | `/outside-session/…` | refused `provider_config_path_policy_invalid`, version unchanged |
| C | same | in-root | `success: true` |

In both arm B runs, `generate_yaml` omitted both the key `persist_directory` and the authored value, while the same export did contain `rag_retrieval` and `docs` (positive control). `/etc/x` did not exist after the run.

**Probe 4** (`$L/run_pytest.sh probe4 $L/test_probe4_n4_review.py`).
- Fixture: the owned `_reviewed_shield_state()` from `test_transform_tools_review_and_guard_parity.py`. Every call was public `execute_tool(validate_arguments=True)`.

```text
ctl_baseline              resolved  hash==derived True   pending []                       materialize CompositionState
probe_queue (upsert)      resolved  hash==derived False  pending []                       materialize RAISED InterpretationReviewIntegrityError … hash drifted   success True v1->2
ctl_nonqueue_remove_mid   pending   -                    pending [classify pipeline_decision] materialize InterpretationReviewPending  success True
followup_after_queue      upsert_node new passthrough -> success False review_reconciliation_failed "… 'prompt_injection_shield_review:classify' hash drifted"
recovery_remove_queue     remove_node inbound        -> success False review_reconciliation_failed (same)
patch_classify            patch_node_options classify {temperature: 0.0} -> success False review_reconciliation_failed (same)
remove_classify           remove_node classify       -> success True v2->3 (only tested escape: deletes the reviewed node)
followup_control_no_queue same upsert on the un-queued state -> success True, review kept resolved and coherent
```

On the un-queued state, `patch_node_options` is refused for a different reason (`plugin_options_invalid`: the fixture's hand-built `llm` options lack `schema`), so that call is not a clean control. `remove_node classify` also succeeds there. The `set_pipeline` control could not discriminate. The owned fixture's hand-built `web_scrape` options fail bulk prevalidation (`plugin_options_invalid`), both for an unchanged resubmission and with the queue added. It is recorded as not run.

**Probe 5** (`cd $WT && <prefix> PYTHONPATH=$WT/src:$WT/elspeth-lints/src TMPDIR=$L/tmp <venv>/bin/python -m pytest tests/unit/web/composer/test_tool_schema_contract.py tests/unit/web/composer/test_producer_resolver.py tests/unit/web/composer/test_transform_rejection_contracts.py -n 0 -p no:cacheprovider -o pythonpath="$WT/src $WT/elspeth-lints/src"`): `exit=0`, 307 passed. This is the pre-change baseline for §6.1 items 2 and 3, for the N1 fix (including the `_runtime_connection_targets` AST pin) and for the queue refusal tests. It is not a test of any finding.

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
