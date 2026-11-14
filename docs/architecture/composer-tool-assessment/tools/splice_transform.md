# `splice_transform` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Atomically insert one explicitly selected transform between two existing nodes on a direct linear success path. The model selects the insertion and plugin; the server derives mechanical connection and edge identities for that explicit edit.

Use for insert/between/before/after edits only when the predecessor is a source/transform, successor is a transform, exactly one direct success edge exists, endpoint error branches and extra consumers/visual branches are absent, and the resulting entire candidate passes context-aware validation. This is not a general graph planner or automatic repair route.

The successor must be an existing **transform node**. Inserting immediately before a sink (the predecessor's `on_success` names an output) or before/after a gate, fork, queue or coalesce is outside the tool's contract. An endpoint whose `on_error` routes to a sink (rather than `discard`) is also refused. For those edits the model must use `upsert_node` / `upsert_edge`.

## 2. Contract

Required predecessor_id,successor_id and node:{id,plugin,options}; optional node.on_error and description. New insertion requires explicit nonempty on_error, while verified identical replay may omit it to retain the prior choice. Node IDs use runtime bounded/reserved-name rules; supplied node_type/input/on_success reject. Distinct endpoints/inserted ID are required. Derived connection tries at most32 bounded collision-free names; derived edge ID is bounded160 characters. New insertion preserves node/edge order and advances version once. Exact replay returns the original state with already_applied:true and IDs/derived_connection, omitting new/replaced edge IDs. Divergent same-ID replay refuses splice_replay_conflict. No external execution/cache/automatic retry; explicit approval applies.

Precise scope of the replay and validity rules (re-verified for this review):

- "Exact replay" compares only `id`, `plugin`, `on_error` and authoring-serialised `options` ([`_normalized_splice_node_projection`, transforms.py:953](../../../../src/elspeth/web/composer/tools/transforms.py#L953)). `description` is not compared, so a replay that changes only `description` returns `already_applied: true` and keeps the stored description (splice_transform-N5).
- Any existing node whose ID equals `node.id` is treated as a replay candidate ([transforms.py:1254](../../../../src/elspeth/web/composer/tools/transforms.py#L1254)), including an unrelated node elsewhere in the graph (splice_transform-N2).
- The full-validity precondition (SG-02) applies only to a **new** insertion ([transforms.py:1428](../../../../src/elspeth/web/composer/tools/transforms.py#L1428)). The replay branch never calls `validate_composition_state` ([transforms.py:1254–1318](../../../../src/elspeth/web/composer/tools/transforms.py#L1254)). In-memory handler run: an exact replay on a state whose outputs were removed afterwards returned `success=True, already_applied=True` with `is_valid=False`.
- `derived_connection` is the **newly minted** connection name. It becomes the inserted node's `on_success` and the successor's new `input`. The predecessor's `on_success` stays unchanged and becomes the inserted node's `input`. In-memory run: `derived_connection='inserted_out'`; predecessor `on_success='middle'` retained; inserted `input='middle'`.
- `affected_nodes` is `(predecessor_id, inserted_id, successor_id)` on both a new insertion and a replay.
- On success only, `_attach_post_call_hints` may add plugin `post_call_hints` ([transforms.py:2184](../../../../src/elspeth/web/composer/tools/transforms.py#L2184)).

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

In explicit_approve mode the staging runs a preview `execute_tool` first ([tool_batch.py:2057](../../../../src/elspeth/web/composer/tool_batch.py#L2057)). Its outcome only matters when the preview succeeds, advances the version and is valid. In that case required controls are wired and, if they change the state, the proposal becomes a full-state `set_pipeline` proposal ([tool_batch.py:2108](../../../../src/elspeth/web/composer/tool_batch.py#L2108)). A refused preview, or an exact-replay no-op preview, is still staged as an `APPROVAL_REQUIRED` splice proposal (splice_transform-N8; demonstrated by a compose turn and the accept route, §7.1).

## 3. Implementation trace

- [src/elspeth/web/composer/tools/transforms.py:2214](../../../../src/elspeth/web/composer/tools/transforms.py#L2214) — `_SPLICE_TRANSFORM_DECLARATION`: public description, JSON schema, `kind=MUTATION`, `augments_on_failure=True`.
- [src/elspeth/web/composer/tools/transforms.py:1219](../../../../src/elspeth/web/composer/tools/transforms.py#L1219) — `_execute_splice_transform`: argument admission, ID-distinctness and duplicate guards, replay branch, new-insertion branch, review reconciliation and the full-validity gate.
- [src/elspeth/web/composer/tools/transforms.py:1117](../../../../src/elspeth/web/composer/tools/transforms.py#L1117) — `_splice_topology_error`: endpoint-kind, routed-error, direct-edge, consumer and canonical-replay-topology checks (helpers `_splice_connection_name`/`_splice_edge_id` at [:929](../../../../src/elspeth/web/composer/tools/transforms.py#L929)/[:945](../../../../src/elspeth/web/composer/tools/transforms.py#L945); bounds at [:2024](../../../../src/elspeth/web/composer/tools/transforms.py#L2024)).
- [src/elspeth/web/composer/tools/transforms.py:2029](../../../../src/elspeth/web/composer/tools/transforms.py#L2029) — `_prepare_transform_candidate`: shared candidate admission (failure-route choice, runtime-owned LLM fields, review canonicalisation, credential wiring, plugin policy, batch placement, inline-prompt surface, prevalidation, provider policy/path).
- [src/elspeth/web/composer/tools/transforms.py:1466](../../../../src/elspeth/web/composer/tools/transforms.py#L1466) — `_splice_validation_rejection`: leading `splice_validation_failed`, then candidate errors not in the prior state, re-filed as `rejected_mutation`, then standing findings. Novelty is keyed by `(component, error_code, message)`.
- [src/elspeth/web/composer/redaction.py:4478](../../../../src/elspeth/web/composer/redaction.py#L4478) — `_SpliceTransformNodeModel`; `SpliceTransformArgumentsModel` at [:4491](../../../../src/elspeth/web/composer/redaction.py#L4491); manifest entry at [:4504](../../../../src/elspeth/web/composer/redaction.py#L4504).
- [src/elspeth/web/composer/tools/_registry.py:99](../../../../src/elspeth/web/composer/tools/_registry.py#L99) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback. [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:147](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L147) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

### 3.1 Route inventory

Exposure was measured by importing the live producers (`elspeth.__file__` resolved into the review worktree). Results: canonical `get_tool_definitions()` includes it; `composer_loop_tool_definitions` includes it on both `openai_strict` and `none`; `planner_tool_definitions` excludes it on both (control `list_transforms` present); MCP `_build_tool_defs()` includes it. It is in `_MUTATION_TOOLS` and `_AUGMENTS_ON_FAILURE_TOOL_NAMES`. The `openai_strict` wire entry carries `strict: False`.

| # | Route | Where | Gate / policy | Notes |
|---|---|---|---|---|
| R1 | Canonical declaration/emitter | [transforms.py:2214](../../../../src/elspeth/web/composer/tools/transforms.py#L2214), [_registry.py:99](../../../../src/elspeth/web/composer/tools/_registry.py#L99), [_registry.py:148](../../../../src/elspeth/web/composer/tools/_registry.py#L148) | always registered | `kind=MUTATION` drives approval, MCP admission and proposal preview membership |
| R2 | Canonical dispatch | [_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) | credential guard; schema admission when `validate_arguments`/`raise_schema_argument_errors` | credential projection treats `node` as a transform even without `node_type` ([credential_guard.py:221](../../../../src/elspeth/web/credential_guard.py#L221)); failure augmentation with plugin schemas ([_dispatch.py:683](../../../../src/elspeth/web/composer/tools/_dispatch.py#L683)) |
| R3 | Freeform loop, auto-apply | [provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391), [tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008) | trust mode not `explicit_approve` | after a successful valid version-advancing result, `_finalize_completed_incremental_mutation` runs `wire_required_controls_state` ([tool_batch.py:3046](../../../../src/elspeth/web/composer/tool_batch.py#L3046), [:774](../../../../src/elspeth/web/composer/tool_batch.py#L774)), which can interpose controls around the inserted node |
| R4 | Freeform loop, explicit approval staging | [tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [:2057](../../../../src/elspeth/web/composer/tool_batch.py#L2057), [:2202](../../../../src/elspeth/web/composer/tool_batch.py#L2202) | `trust_mode == "explicit_approve"`, per-turn proposal cap | preview executes; a valid preview with required controls becomes a `set_pipeline` proposal; a failed or no-op preview is still staged as a `splice_transform` proposal (N8, demonstrated §7.1) |
| R5 | REST proposal acceptance | [sessions/routes/composer/proposals.py:493](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L493), [:522](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L522) | user accepts; session lease and stale-state check | re-executes `execute_tool(proposal.tool_name, …)` against the **current** state with `validate_arguments=True, require_data_dir_for_paths=True` (no `raise_schema_argument_errors`), then wires required controls |
| R6 | Batch dependency projection | [batch_dependencies.py:79](../../../../src/elspeth/web/composer/batch_dependencies.py#L79), [:127](../../../../src/elspeth/web/composer/batch_dependencies.py#L127) | within one provider batch | promises the inserted node's produced resources; references `predecessor_id`/`successor_id` as components. Malformed-field test at [test_dependent_batch_refusal.py:292](../../../../tests/unit/web/composer/test_dependent_batch_refusal.py#L292) |
| R7 | Diagnostic evidence / redaction | [diagnostic_evidence.py:340](../../../../src/elspeth/web/composer/diagnostic_evidence.py#L340), [redaction.py:4504](../../../../src/elspeth/web/composer/redaction.py#L4504), [protocol.py:971](../../../../src/elspeth/web/composer/protocol.py#L971) | always | `node` path treated as a transform component for credential/field-mapper projection |
| R8 | Validation-guidance catalogue (`explain_validation_error`) and closed error-code registry | [generation.py:1653–1690](../../../../src/elspeth/web/composer/tools/generation.py#L1653), [error_codes.py:207–211](../../../../src/elspeth/web/composer/error_codes.py#L207) | via `explain_validation_error` and planner repair feedback | five `splice_*` codes plus a splice sentence inside the generic `batch_dependency_refused` entry |
| R9 | Cross-tool advertisement | [sessions.py:2120](../../../../src/elspeth/web/composer/tools/sessions.py#L2120) | `set_pipeline` description | points narrow insert edits at `splice_transform` |
| R10 | Composer MCP | [composer_mcp/server.py:117](../../../../src/elspeth/composer_mcp/server.py#L117), [:390](../../../../src/elspeth/composer_mcp/server.py#L390) | trained-operator snapshot; `data_dir=None`, `require_data_dir_for_paths=True`, `raise_schema_argument_errors=True` | no approval, no required-control finalisation, local session persistence only |
| R11 | Frontend | [toolCallDescriptions.ts:91](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L91), [ToolCallCard.tsx:147](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L147) | rendering only | "Inserts a transform between two connected steps." No splice-specific replay or rendering |
| — | Planner (one-shot) | [capability_skill.py:16](../../../../src/elspeth/web/composer/capability_skill.py#L16) | not in `PLANNER_DISCOVERY_TOOL_NAMES` | not reachable; the planner authors a whole proposal instead |
| — | Recovery controls | `recovery_controls.py` | — | no reference: `grep -c splice` = 0. Control: `grep -c "handoff_to_user\|restore_parked_graph"` = 6 on the same file |
| — | Async operation path | message route 202 + poll | operation fence | no splice-specific path. The splice runs inside the turn's ordinary tool batch (exercised by [test_composer_splice_concurrency.py:224](../../../../tests/testcontainer/web/test_composer_splice_concurrency.py#L224)) |

No tutorial-specific branch was found. `grep -rn splice` over `src/` also hit sites that are not invocation routes:

- `required_controls.py` and `planner_authoring_aids.py:504/527/610`: server-owned control interposition and its brief text, not callers of this tool.
- `interpretation_state.py:90/649/2067`, `_common.py:1455`, `sessions.py:1607`, `blobs.py:877/942/965/1080`, `secrets.py:295`, `tool_batch.py:1743`: docstring/comment references to shared guards.
- `credential_guard.py:180/188`: the `splice=True` mode of the node projection used by R2.
- The frontend `Array.splice` calls.

## 4. Alternative paths

Duplicate node/edge IDs, invalid topology, source/output ID collision, name exhaustion, derived-edge collision and inadmissible plugin options reject atomically. Candidate preparation repeats policy/batch/review/config/path rules. On a new insertion, the successor's runtime input and both visual edges are changed as one candidate; reviews are reconciled, then full policy-aware composition validation must pass. It forwards only newly introduced candidate diagnostics in addition to standing saved-state findings. Exact replay checks topology, configured node equivalence and canonical interpretation requirements; it is deliberately idempotent at the version level.

Refusal codes and the cause each one actually covers (in-memory handler runs against the unit fixture `_state()`; not end-to-end service runs):

| Cause | `error_code` | `rejected_component` | Message |
|---|---|---|---|
| predecessor ID absent | `splice_topology_unsupported` | `None` | "Splice predecessor 'missing' not found." |
| successor ID absent, or names a source/sink | `node_not_found` | `node:<id>` | "Splice successor '…' not found or is not a transform." |
| successor is an existing gate | `splice_topology_unsupported` | `None` | "splice_transform requires a transform successor on a direct linear path." |
| endpoint `on_error` routed to a sink | `splice_topology_unsupported` | `None` | "…does not support predecessors or successors with routed error branches." |
| inserted ID equals an endpoint | `splice_ids_not_distinct` | `None` | — |
| inserted ID equals an **unrelated** existing transform | `splice_topology_unsupported` | `None` | "Existing splice topology does not match the server-derived direct path." |
| pre-existing invalid draft (no sinks) | `splice_validation_failed` + 0 forwarded entries + standing findings | `None` | "Spliced pipeline failed context-aware validation." |

The "novelty" filter is message-keyed. Validation messages embed producer IDs ("'before' -> 'after'"), so a standing successor finding is re-filed as a new `rejected_mutation` entry once the producer becomes the inserted node. In-memory run: with a pre-existing `schema_contract_violation` on `node:after`, the refusal carried a forwarded `'inserted' -> 'after'` entry plus the standing `'before' -> 'after'` entry. The splice does re-cause that violation (the new producer also lacks the field), so the attribution is not false. The filter cannot tell "still broken" from "newly broken" (splice_transform-N7).

Forwarded entries have `plugin_identity=None` by design ([transforms.py:1494](../../../../src/elspeth/web/composer/tools/transforms.py#L1494)). `augments_on_failure` therefore attaches `plugin_schemas` only to prevalidation refusals from `_prepare_transform_candidate`, never to `splice_validation_failed` refusals.

## 5. Usability assessment

**SG-02 — demonstrated undisclosed validity precondition; high confidence.** Known-positive direct splice succeeds. The same direct path with a pre-existing missing output refuses unchanged state with splice_validation_failed even though the requested insertion did not create that old error. The public description lists topology prerequisites but does not disclose the full-validity requirement. In contrast, ordinary incremental edits admit incomplete drafts. Workaround: repair the unrelated graph first or explicitly author the insertion with complete lower-level edits; do not retry the same splice expecting a transient fix. **SG-06:** candidate preparation is another cross-route admission implementation.

SG-02 refinement (this review): in that refusal, **zero** `rejected_mutation` entries follow the leading entry. The `splice_validation_failed` guidance still tells the model that following entries "name each new error" and to "Repair the inserted node's options". Nothing in the result distinguishes "precondition not met" from "your insertion broke something" (splice_transform-N1). The precondition binds only new insertions (§2).

New findings (provisional IDs; the coordinator renumbers):

| ID | Classification | Finding | Where |
|---|---|---|---|
| splice_transform-N1 | Demonstrated (in-memory handler + `execute_tool` dispatch) | SG-02 refusal carries no forwarded entry and no distinguishing field. Leading guidance directs repair of the inserted node, which is not at fault. Dead end for the agent | [transforms.py:1428–1434](../../../../src/elspeth/web/composer/tools/transforms.py#L1428), [:1489–1507](../../../../src/elspeth/web/composer/tools/transforms.py#L1489), [generation.py:1675](../../../../src/elspeth/web/composer/tools/generation.py#L1675) |
| splice_transform-N2 | Demonstrated (in-memory handler) | An inserted ID that collides with an unrelated existing transform is treated as a replay and refused as `splice_topology_unsupported` ("Existing splice topology does not match…"). The real fix is a new ID; the guidance sends the model to choose a different path | [transforms.py:1254–1263](../../../../src/elspeth/web/composer/tools/transforms.py#L1254), [:1178](../../../../src/elspeth/web/composer/tools/transforms.py#L1178), [generation.py:1658](../../../../src/elspeth/web/composer/tools/generation.py#L1658) |
| splice_transform-N3 | Demonstrated (in-memory handler) | Endpoint-not-found asymmetry: missing predecessor gives `splice_topology_unsupported` with no component; missing successor gives `node_not_found` with `node:<id>` | [transforms.py:1124–1126](../../../../src/elspeth/web/composer/tools/transforms.py#L1124), [:1245–1252](../../../../src/elspeth/web/composer/tools/transforms.py#L1245) |
| splice_transform-N4 | Demonstrated (in-memory handler) | "Insert before the output" (successor = an existing sink) and successor = source yield `node_not_found`. That guidance says "If a new node is intended, author it explicitly with upsert_node before referring to it", which invites inventing a node named after the sink. No pointer to the `upsert_node`/`upsert_edge` rewire that actually serves the intent | [transforms.py:1245–1252](../../../../src/elspeth/web/composer/tools/transforms.py#L1245), [generation.py:1643–1647](../../../../src/elspeth/web/composer/tools/generation.py#L1643) |
| splice_transform-N5 | Demonstrated (in-memory handler) | Description-only change on an applied splice returns `success`, `already_applied: true`, same state object; the new description is silently not stored. Contradicts the argument text "refresh it whenever you change what the step does" | [transforms.py:953–959](../../../../src/elspeth/web/composer/tools/transforms.py#L953), [:1281–1318](../../../../src/elspeth/web/composer/tools/transforms.py#L1281) |
| splice_transform-N6 | Source-established; value demonstrated | The declaration calls `derived_connection` "the on_success carried over". It is the newly derived connection name; the carried-over connection stays on the predecessor | [transforms.py:2223](../../../../src/elspeth/web/composer/tools/transforms.py#L2223), [:1444](../../../../src/elspeth/web/composer/tools/transforms.py#L1444) |
| splice_transform-N7 | Demonstrated fragility (in-memory handler); not misattribution | Novelty of forwarded diagnostics is keyed on message text that embeds producer IDs, so standing endpoint findings reappear as "new" after rewiring | [transforms.py:1489–1498](../../../../src/elspeth/web/composer/tools/transforms.py#L1489) |
| splice_transform-N8 | Demonstrated (compose turn under `explicit_approve` + REST accept route, §7.1) | In explicit_approve mode a preview that refuses (SG-02, topology) or is an exact-replay no-op is still staged as an `APPROVAL_REQUIRED` proposal returned with `success=True`. The preview's diagnostics are discarded, so the user is asked to approve a change the server already knows will refuse or do nothing. On accept, a refused preview becomes 422 `proposal_validation_failed` with auto-reject. The no-op replay instead returns 409 "Accepted proposal did not change composition state." and the proposal stays `pending`, so it can neither commit nor be cleared by the server. The comment documents this for `ToolArgumentError` only. Affects every incremental mutation tool. A replay-conflict preview was not run | [tool_batch.py:2057–2121](../../../../src/elspeth/web/composer/tool_batch.py#L2057), [:2202–2227](../../../../src/elspeth/web/composer/tool_batch.py#L2202) |
| splice_transform-N9 | Demonstrated (in-memory composition of the R3 chain; deployment-conditional; not a compose turn, §7.1) | When required controls are wired around a freshly spliced LLM node, `_splice_input_control` rewires the inserted node's `input` ([required_controls.py:501–557](../../../../src/elspeth/web/composer/required_controls.py#L501)), and the output control rewires its `on_success`. An identical replay then refuses as `splice_topology_unsupported` ("Existing splice topology does not match the server-derived direct path.") instead of returning `already_applied`. Consequence: an auto-applied LLM splice in a control-required deployment cannot be replayed, and the refusal's guidance sends the model to "select an existing direct linear path" | [tool_batch.py:3046](../../../../src/elspeth/web/composer/tool_batch.py#L3046), [transforms.py:1178](../../../../src/elspeth/web/composer/tools/transforms.py#L1178) |
| splice_transform-N10 | Intended restriction, under-disclosed | The tool cannot be used for insertion before a sink, before/after a gate, or around any endpoint whose `on_error` routes to an error sink. Common real pipelines (errors routed to a sink; the "add a step before output" edit) are outside it. The declaration says "insert/between/before/after edits" without these caveats | [transforms.py:1127–1141](../../../../src/elspeth/web/composer/tools/transforms.py#L1127), [:1161–1162](../../../../src/elspeth/web/composer/tools/transforms.py#L1161) |
| splice_transform-N11 | Source-established test gap | The 15-case topology rejection test asserts only `not success` and atomicity, not `error_code`/`rejected_component`, so N2–N4 code choices are unpinned. No test pins the SG-02 zero-forward shape or description-on-replay | [test_splice_transform_tool.py:691–701](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L691) |
| splice_transform-N12 | Source-established guidance oddity | The generic `batch_dependency_refused` fix text ends with "Use splice_transform only for its supported direct-transform-edge topology." That is unrelated to batch dependency repair | [generation.py:1685–1690](../../../../src/elspeth/web/composer/tools/generation.py#L1685) |

### 5.1 Guidance parity

Instrument: `grep -n -i splice` over both skill files, `prompts.py` and `pipeline_planner.py`. Hits: `pipeline_composer.md:176` and `:342` (autogen inventory); `pipeline_planner.py:1011` (comment about required controls); none in `pipeline_capabilities.md` or `prompts.py`. Control: `grep -c "set_pipeline\|transform" pipeline_capabilities.md` = 22 and `grep -c upsert_node pipeline_composer.md` = 4, so the greps match known mentions. Concept grep `insert|between|before .*transform` found the repair-table rows cited below.

| # | Surface | Says | Handler does | Finding |
|---|---|---|---|---|
| G1 | [pipeline_composer.md:180–181](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L180) | "The narrower tools also support staged construction of a new pipeline." | A new splice refuses any draft that is not fully valid. The SG-02 control is exactly a staged draft without a sink | Contradiction. The skill pushes the model to splice mid-construction, where it deterministically refuses |
| G2 | [pipeline_composer.md:176](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L176) | "one-transform insertion between existing nodes on a direct linear path → `splice_transform`" | Also requires a transform successor, a non-gate predecessor, endpoints without routed errors, and a fully valid result | Missing guidance; the fallback row at :178 exists but nothing tells the model when the narrower row does not fit |
| G3 | [pipeline_composer.md:1424–1428](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L1424), [:1444](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L1444) | repair rows: "insert a `type_coerce` before the gate"; "insert the … cleanup/projection transform … immediately before the sink" | splice refuses gate successors and sink-terminated connections | Guidance pushes toward an "insertion", which the edit table routes to `splice_transform`, for exactly the topologies it refuses. Whether models follow that route is unmeasured |
| G4 | Declaration description [transforms.py:2218](../../../../src/elspeth/web/composer/tools/transforms.py#L2218) | "Use this for insert/between/before/after edits" | before-sink, gate-adjacent and error-routed endpoints refuse; full validity required | Overclaim plus undisclosed precondition (SG-02 confirmed) |
| G5 | Declaration description [transforms.py:2223](../../../../src/elspeth/web/composer/tools/transforms.py#L2223) | `derived_connection` "(the on_success carried over)" | newly derived connection | Wrong result-field description (N6) |
| G6 | `description` argument (`_STEP_DESCRIPTION_DESCRIPTION`, [_common.py:1899](../../../../src/elspeth/web/composer/tools/_common.py#L1899)) | "refresh it whenever you change what the step does" | replay ignores a changed description | The splice cannot honour the instruction and reports success (N5); the refresh must go through `upsert_node` |
| G7 | [generation.py:1675–1683](../../../../src/elspeth/web/composer/tools/generation.py#L1675) `splice_validation_failed` | "rejected_mutation entries that follow … name each new error … Repair the inserted node's options" | may forward zero entries (SG-02) | Wrong guidance for the precondition case (N1) |
| G8 | [generation.py:1658–1662](../../../../src/elspeth/web/composer/tools/generation.py#L1658) `splice_topology_unsupported` | "Select an existing direct linear path" | also emitted for an ID collision and a missing predecessor | Misdirects (N2, N3) |
| G9 | [generation.py:1643–1647](../../../../src/elspeth/web/composer/tools/generation.py#L1643) `node_not_found` | "If a new node is intended, author it explicitly with upsert_node" | emitted for an existing sink/source successor | Misdirects (N4) |
| G10 | `pipeline_capabilities.md` | no mention | — | Acceptable: that skill carries plugin/routing contracts, not edit-tool selection. The edit table in `pipeline_composer.md` is the right home for the caveats |

No stale reference to nonexistent splice arguments was found: the skill names only the tool, and the declaration's argument names match `SpliceTransformArgumentsModel`.

## 6. Structural remediation

Declare operation-specific admission semantics explicitly: either retain requires_valid_result and expose that precondition in machine-readable availability/description, or deliberately adopt no_new_errors draft-edit semantics after a separate design decision. Share typed transform-candidate admission while keeping splice's atomic topology/replay proof. Regression: valid insertion and exact replay pass; divergent replay/topology/collisions reject unchanged; unrelated invalid drafts yield an actionable declared precondition failure or follow an explicitly chosen no-new-errors rule; invalid inserted config still rejects; positive and negative outcomes match public contract. Compatibility risk medium/high if acceptance is widened; preserve existing mechanical ID and replay contracts.

Additional structural items from this review: (a) key diagnostic novelty on a structured identity (component, code, subject/field set) rather than rendered message text before any no-new-errors rule relies on it (N7). (b) Decide how replay identity interacts with server-owned required-control interposition (N9, now demonstrated in memory): either define replay over the authored projection or document that controls end replayability. (c) For explicit approval (N8, cross-cutting, demonstrated), decide whether a deterministically refused or no-op preview should return its refusal to the model instead of staging a proposal. The no-op case also needs a terminal accept outcome: today it leaves a `pending` proposal behind a 409. That is a staging-contract decision for all incremental mutations, not a splice-only patch.

### 6.1 Simple enhancements

| # | What | Where | Why | Acceptance check |
|---|---|---|---|---|
| E1 | When `_splice_validation_rejection` forwards zero entries, add a distinct signal: `data={"precondition": "requires_valid_result", "introduced_errors": 0}` or a separate code such as `splice_requires_valid_pipeline`, with guidance "repair the standing errors listed, then retry; or use upsert_node/upsert_edge" | [transforms.py:1489–1507](../../../../src/elspeth/web/composer/tools/transforms.py#L1489), [generation.py:1675](../../../../src/elspeth/web/composer/tools/generation.py#L1675) | N1/SG-02 dead end | `replace(_state(), outputs=())` refusal carries the signal; the ghost-field case still forwards `schema_contract_violation` without it |
| E2 | Disclose the full-validity precondition and the unsupported endpoints (sink successor, gate, routed `on_error`) in the declaration; replace "insert/between/before/after" with the bounded wording; fix the `derived_connection` text | [transforms.py:2218–2226](../../../../src/elspeth/web/composer/tools/transforms.py#L2218) | G4, G5, N6, N10 | Description-pinning test asserts the new phrases; wire-parity tests still pass |
| E3 | Before the replay branch, refuse an existing node that is not on the requested path with an ID-collision code (`duplicate_node_id` or a new `splice_node_id_in_use`) and "choose a distinct ID" guidance | [transforms.py:1254–1263](../../../../src/elspeth/web/composer/tools/transforms.py#L1254) | N2 | Probe P3 state yields the collision code; the noncanonical-replay tests at [test_splice_transform_tool.py:467](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L467)/[:480](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L480) keep their atomicity |
| E4 | Give a missing predecessor `node_not_found` with `rejected_component` (or `source_not_found`), matching the successor | [transforms.py:1124–1126](../../../../src/elspeth/web/composer/tools/transforms.py#L1124) | N3 | Parametrised case asserts code and component for both endpoints |
| E5 | When the successor ID names an existing output or source, return `splice_topology_unsupported` with "successor must be a transform; to insert before a sink use upsert_node with input=<predecessor connection> and on_success=<sink>, then reconcile edges" | [transforms.py:1245–1252](../../../../src/elspeth/web/composer/tools/transforms.py#L1245) | N4 | `successor_id="result"` case returns the new code; no `node_not_found` guidance is emitted |
| E6 | Include `description` in the replay identity (divergent → `splice_replay_conflict`), or apply a description-only change and advance the version. Either way, never report `already_applied` while dropping it | [transforms.py:953–959](../../../../src/elspeth/web/composer/tools/transforms.py#L953) | N5, G6 | Probe P2 no longer returns `already_applied: true` with the old description |
| E7 | Pin `error_code` and `rejected_component` per case in the topology rejection parametrisation; add SG-02 zero-forward and description-replay tests | [test_splice_transform_tool.py:691](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L691) | N11 | Mutating a code in `_splice_topology_error` turns a case red |
| E8 | Skill: qualify `pipeline_composer.md:176`/`:180–181` (splice requires a valid draft and a transform successor; during staged construction or before a sink/gate use `upsert_node`/`upsert_edge`) | [pipeline_composer.md:176](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L176) | G1–G3 | Skill-hash/inventory tests updated; text names both conditions |
| E9 | Move the splice sentence out of `batch_dependency_refused` | [generation.py:1689](../../../../src/elspeth/web/composer/tools/generation.py#L1689) | N12 | Guidance text test updated |

All enhancements stay inside the composer invariants: they change refusal codes, disclosures and guidance; none authors topology server-side.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_splice_transform_tool.py:215](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L215) — ordered atomic insertion.
- [tests/unit/web/composer/test_splice_transform_tool.py:237](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L237) — source predecessor.
- [tests/unit/web/composer/test_splice_transform_tool.py:260](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L260) — identical reviewed replay same object.
- [tests/unit/web/composer/test_splice_transform_tool.py:452](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L452) — divergent retry atomicity.
- [tests/unit/web/composer/test_splice_transform_tool.py:691](../../../../tests/unit/web/composer/test_splice_transform_tool.py#L691) — unsupported topology rejection (asserts non-success and atomicity only; codes unpinned).
- [tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py:1](../../../../tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py#L1) — shared transform guards; [:558](../../../../tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py#L558) and [:585](../../../../tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py#L585) pin forwarding of a splice-introduced error and exclusion of pre-state errors, in both cases with at least one new error.
- [tests/unit/web/composer/test_transform_rejection_contracts.py:514](../../../../tests/unit/web/composer/test_transform_rejection_contracts.py#L514) — handler/dispatch ordering of leading, forwarded and standing entries.
- [tests/integration/web/composer/test_profile_splice_flow.py:237](../../../../tests/integration/web/composer/test_profile_splice_flow.py#L237) — profiled shield splice through `execute_tool` (body read; **not run** by this assessment).
- [tests/testcontainer/web/test_composer_splice_concurrency.py:224](../../../../tests/testcontainer/web/test_composer_splice_concurrency.py#L224) and [:307](../../../../tests/testcontainer/web/test_composer_splice_concurrency.py#L307) — concurrent HTTP splices serialize (second request 409 while active; replay yields `already_applied=true`, one new version) and PostgreSQL stale-writer refusal (bodies read; **not run** — no PostgreSQL selection was run).

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance. Controlled positive splice: `True, version5`; pre-existing invalid graph: `False`, same original state, leading splice_validation_failed plus standing missing-sink/dangling-route findings.

Sheet-review probes (2026-11-09) were in-memory handler/`execute_tool` runs against the unit fixtures in `tests/unit/web/composer/test_splice_transform_tool.py`. `elspeth.__file__` resolved into the review worktree, and all three probe processes exited 0. They are not end-to-end service runs. No pytest, provider, database or browser was involved. Observations: P0 positive control `True 5`; P1 SG-02 `forwarded_new 0`, errors `[splice_validation_failed, no_sinks_configured, edge_unknown_node, transform_on_success_dangling]`, identical through `execute_tool`; P2 description replay `already_applied True`, description unchanged; P3 unrelated-ID collision `splice_topology_unsupported`; P4 predecessor/successor code asymmetry; P5 sink and source successor `node_not_found`; P6 routed-error endpoint `splice_topology_unsupported`; P7 `derived_connection='inserted_out'`; P8 replay data omits edge IDs, `affected_nodes` identical; P9 gate successor `splice_topology_unsupported`; standing-message re-filing (N7); replay on an invalid graph `success True, is_valid False`. A 2026-11-09 re-run of the same script under the fail-closed provider guard exited 0 and printed identical P0–P9 lines.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

### 7.1 Probe runs for N8 and N9 (2026-11-09)

`$WT` is the review worktree and `$L` the lane-private probe directory. The probe files and logs live under `$L`; none is in the worktree. Every run used the brief's fail-closed prefix (`HTTPS_PROXY`/`HTTP_PROXY=http://127.0.0.1:9`, `LITELLM_LOCAL_MODEL_COST_MAP=True`, `ELSPETH_DISABLE_NETWORK=1`). Each process first installed a guard that makes `litellm.acompletion`, `litellm.completion`, `provider_gateway._litellm_acompletion`, `pipeline_planner._litellm_acompletion` and IPv4/IPv6 `socket.connect` raise. The advisor and auto-title paths call the gateway attribute, so the guard covers them. A positive control showed all five raising, and every log records zero guard hits. `elspeth.__file__` resolved to `$WT/src/elspeth/__init__.py` in every process.

**N8 — confirmed.** Command: `cd $WT && PYTHONPATH=$WT:$WT/src:$WT/elspeth-lints/src TMPDIR=$L/tmp python -m pytest $L/test_probe_n8_explicit_approve.py -n 0 -p no:cacheprovider -s --rootdir $WT -c $WT/pyproject.toml -o pythonpath="$WT/src $WT/elspeth-lints/src" -p tests.conftest -p tests.integration.conftest -p tests.integration.web.conftest`. Exit 0, 8 passed. Log: `$L/probe_n8.log`.

Part A is a real `ComposerServiceImpl.compose` turn. It uses the harness from [test_freeform_proposal_prevalidation.py](../../../../tests/integration/web/composer/test_freeform_proposal_prevalidation.py): an in-memory SQLite session DB, a session with `trust_mode="explicit_approve"`, an LLM scripted at `_call_llm`, and the advisor replaced by its clean checkpoint. A spy on `tool_batch.execute_tool` captured the preview. The graph is csv → `before` → `after` → json `main`, version 4, with the splice inserting `passthrough` between `before` and `after`.

| Arm | Preview (spy) | Proposals | What the model saw |
|---|---|---|---|
| valid graph (positive control) | `success=True`, v4→5, valid | 1 `splice_transform` pending | `success=true`, `APPROVAL_REQUIRED`, errors `[]` |
| `outputs=()` (SG-02) | `success=False`, `splice_validation_failed` + 3 standing codes | 1 pending | `success=true`, `APPROVAL_REQUIRED`; errors = the base state's 3 standing findings from `last_validation`; no `splice_validation_failed` |
| `successor_id="missing"` | `success=False`, `node_not_found` | 1 pending | `success=true`, `APPROVAL_REQUIRED`, errors `[]`; no splice code anywhere in the tool message |
| splice already applied (exact replay) | `success=True`, `already_applied=True`, v5→5 | 1 pending | `success=true`, `APPROVAL_REQUIRED`, errors `[]` |
| `set_pipeline` with an uninstalled plugin (negative control) | — | 0 | `success=false`, `PREVALIDATION_REJECTED`, `plugin_not_installed` |

The topology arm is the cleanest proof that the preview's diagnostics are discarded. The base graph was valid, the preview refused, and the model was told nothing.

Part B uses the REST accept route (`tests/unit/web/sessions/test_routes._make_app`). Each session was seeded with the arm's state, and the proposal was inserted directly with `base_state_id` pinned to the seeded state.

- Valid control: 200, `committed`, the new state's nodes are `before, inserted, after`. This also shows the harness and fixtures loaded.
- SG-02: 422 `proposal_validation_failed`, the proposal is auto-`rejected`, and the state is unchanged.
- Exact replay: 409 `{"detail": "Accepted proposal did not change composition state."}`. The proposal stays `pending` and the state is unchanged.

Limits: the Part A proposals have no persisted base state, and the Part B proposals were inserted, not produced by Part A. A replay-conflict (`splice_replay_conflict`) preview was not run.

**N9 — confirmed (in memory, not a compose turn).** Command: `cd $WT && PYTHONPATH=$WT:$WT/src:$WT/elspeth-lints/src TMPDIR=$L/tmp python $L/probe_n9_replay_after_controls.py`. Exit 0. Log: `$L/probe_n9.log`.

The probe composes the R3 chain by hand: public `execute_tool("splice_transform")`, then `wire_required_controls_state(updated_state, snapshot, view)`, then an identical `execute_tool` replay. It runs against the control-required deployment view `_guardrail_profile_view` from [test_planner_authoring_aids.py](../../../../tests/unit/web/composer/test_planner_authoring_aids.py), where the prompt shield and content safety are both `required`. `passthrough` is not enabled in that policy (the first attempt refused with `plugin_not_enabled`), so the graph is csv → `after` (`field_mapper`) → json, and the splice inserts `assess` between `predecessor_id="source"` and `after`.

| Arm | Wiring | Replay |
|---|---|---|
| A: LLM splice, no wiring (positive control) | — | `success=True`, `already_applied=True` |
| B: LLM splice, then wiring (probe) | state changed; `prompt_shield_auto_1` interposed on `assess.input`, `content_safety_auto_1` on its output; wired state valid | `success=False`, `splice_topology_unsupported`, "Existing splice topology does not match the server-derived direct path." |
| C: `field_mapper` splice, then wiring (negative control) | identity (no control needed) | `already_applied=True` |
| D: LLM splice under `control_mode="recommend"` | identity | `already_applied=True` |

The probe did not record which sub-comparison in `_splice_topology_error` fired. The wired topology shows both of the inserted node's links rewritten: its `input` is `prompt_shield_auto_1_out`, not the source's `rows`, and its `on_success` is `content_safety_auto_1_in`, not `assess_out`, which `after` still consumes. Wiring returned the state at version 5, unchanged from the splice.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.transforms._handle_splice_transform`](../../../../src/elspeth/web/composer/tools/transforms.py#L2184).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | no |
| Composer MCP | yes |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness. Re-measured for this review: the `openai_strict` freeform entry carries `strict: False` and keeps `node.required = [id, plugin, options]`. In that dialect, omitting `on_error` (the replay form) and `description` stays expressible.

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
