# `upsert_edge` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Add or replace a diagram relationship and, only for supported sink-target routes, update the corresponding executable producer routing slot. The orchestrator must understand this distinction before selecting the tool.

Select for a visual relationship or direct sink routing change. To connect processing nodes, their input/on_success connection strings must already be or separately be authored consistently; drawing an edge alone does not wire runtime execution. The scalar routing fields are the runtime authority; sink-targeting edges are their mirror ([state.py:3436](../../../../src/elspeth/web/composer/state.py#L3436)).

Mirroring happens only when both endpoints already resolve at call time: the producer must be an existing node or a source present in `state.sources`, and the target must be a declared output. Otherwise the edge is stored as an advisory edge and no scalar is written (see §4 and upsert_edge-N1/N3/N4).

## 2. Contract

Required id,from_node,to_node strings and edge_type enum on_success/on_error/route_true/route_false/fork ([transforms.py:131](../../../../src/elspeth/web/composer/tools/transforms.py#L131)). label is optional null|string. The shared lowering matrix ([state.py:3464](../../../../src/elspeth/web/composer/state.py#L3464)) admits source/node/output combinations. Sources may only use on_success. on_error sink wiring accepts transforms/aggregations only. Gate expression errors are refused with a pointer to upsert_node.on_error. on_error from collector, coalesce, row_union and queue is refused too, each with its own message. route_true/route_false/fork are gate-only. Gate on_success to a sink, queue to a sink and row_union to a sink are refused. The matrix is pinned exhaustively by [test_edge_route_reconciliation.py:579](../../../../tests/unit/web/composer/test_edge_route_reconciliation.py#L579). Unresolved endpoints (kind `None`) and edges *from* an output are not refused by the matrix ([state.py:3472](../../../../src/elspeth/web/composer/state.py#L3472)).

A sink-routing scalar slot can have only one edge, with fork uniqueness per destination. This admission check compares against other *edges* only, not against the scalar ([transforms.py:1547](../../../../src/elspeth/web/composer/tools/transforms.py#L1547)). An existing scalar route with no drawn edge is overwritten silently. Replacing an edge ID clears the former sink route unless another legacy edge still expresses it, then applies the new route ([transforms.py:1569](../../../../src/elspeth/web/composer/tools/transforms.py#L1569)). Clearing a source route writes the placeholder `on_success="discard"` ([transforms.py:1079](../../../../src/elspeth/web/composer/tools/transforms.py#L1079)). Stage 1 then reports `source_on_success_dangling`, because source `"discard"` is not a runtime keyword. Result returns affected endpoints `(from_node, to_node)`, validation and version changes from immutable graph helpers. It also carries the applied-component echo, which is `None` when the state cannot be projected as `set_pipeline` arguments or the echo exceeds 16 KiB ([_common.py:4085](../../../../src/elspeth/web/composer/tools/_common.py#L4085)). The tool does not execute a graph or plugin. Same-ID replay is not a no-version-change guarantee: `with_edge` bumps the version on every call ([state.py:8259](../../../../src/elspeth/web/composer/state.py#L8259)), even when nothing else changes (upsert_edge-N6). No cache/automatic retry; explicit approval applies.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/transforms.py:464](../../../../src/elspeth/web/composer/tools/transforms.py#L464) — declaration (description, per-argument schema, example); handler wrapper at [:456](../../../../src/elspeth/web/composer/tools/transforms.py#L456); argument model at [:131](../../../../src/elspeth/web/composer/tools/transforms.py#L131).
- [src/elspeth/web/composer/tools/transforms.py:1510](../../../../src/elspeth/web/composer/tools/transforms.py#L1510) — `_execute_upsert_edge`: lowering admission (:1538), slot-uniqueness (:1547), replace-then-apply commit (:1569), blocking-invariant check (:1584), review reconciliation (:1588, helper [:1594](../../../../src/elspeth/web/composer/tools/transforms.py#L1594)), result (:1591). Unlike `upsert_node` ([:908](../../../../src/elspeth/web/composer/tools/transforms.py#L908)), it does not call `composition_review_contract_error`.
- [src/elspeth/web/composer/tools/transforms.py:978](../../../../src/elspeth/web/composer/tools/transforms.py#L978) — `_apply_sink_edge_route`: writes the scalar only when `to_node` is a declared output and `from_node` is a key of `state.sources` or an existing node; otherwise returns the state unchanged.
- [src/elspeth/web/composer/tools/transforms.py:1068](../../../../src/elspeth/web/composer/tools/transforms.py#L1068) — `_clear_removed_sink_edge_route` (shared with `remove_edge`) and [:962](../../../../src/elspeth/web/composer/tools/transforms.py#L962) `_sink_route_still_expressed` legacy-duplicate guard.
- [src/elspeth/web/composer/tools/transforms.py:1021](../../../../src/elspeth/web/composer/tools/transforms.py#L1021) — `_reconcile_node_sink_mirror_edges`, called by `upsert_node` ([:894](../../../../src/elspeth/web/composer/tools/transforms.py#L894)). It retargets or deletes edges this tool created when the node's scalars change. It does nothing to edges whose target is not yet a declared output ([:1048](../../../../src/elspeth/web/composer/tools/transforms.py#L1048)).
- [src/elspeth/web/composer/state.py:3446](../../../../src/elspeth/web/composer/state.py#L3446) and [:3464](../../../../src/elspeth/web/composer/state.py#L3464) — shared component-kind classifier (treats the literal `"source"` as a source even when no source has that key, [:3453](../../../../src/elspeth/web/composer/state.py#L3453)) and edge-lowering contract.
- [src/elspeth/web/composer/state.py:3544](../../../../src/elspeth/web/composer/state.py#L3544) and [:3588](../../../../src/elspeth/web/composer/state.py#L3588) — Stage 1 mirror-agreement (`edge_route_mismatch`) and edge-route contract. Only sink-targeting edges are checked for agreement ([:3614](../../../../src/elspeth/web/composer/state.py#L3614)); `edge_unknown_node` lives at [:8496](../../../../src/elspeth/web/composer/state.py#L8496).
- [src/elspeth/web/composer/state.py:8259](../../../../src/elspeth/web/composer/state.py#L8259) — `CompositionState.with_edge` (add/replace by id, unconditional version bump); immutable model at [:8155](../../../../src/elspeth/web/composer/state.py#L8155).
- [src/elspeth/web/composer/tools/_registry.py:94](../../../../src/elspeth/web/composer/tools/_registry.py#L94) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

### 3.1 Route inventory

| Route | Where | Gate / condition |
|---|---|---|
| Canonical registry | `TOOLS_IN_MODULE` [transforms.py:2278](../../../../src/elspeth/web/composer/tools/transforms.py#L2278) → `_REGISTERED_TOOLS` [_registry.py:94](../../../../src/elspeth/web/composer/tools/_registry.py#L94) | Always registered; kind `MUTATION` |
| Freeform loop (both dialects) | [provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) → [wire_projection.py:852](../../../../src/elspeth/web/composer/tools/wire_projection.py#L852) | Emitted in `openai_strict` and `none`; decoded to canonical arguments before dispatch |
| Planner | [pipeline_planner.py:1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647) | **Not emitted** (re-measured, both dialects). The planner authors edges only inside its terminal proposal, which does not run this handler |
| Recovery controls | `recovery_controls.py` | No reference (src-wide grep below) |
| Composer MCP | `_build_tool_defs` [server.py:210](../../../../src/elspeth/composer_mcp/server.py#L210), dispatch [server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) | Emitted; shares canonical dispatch; no web approval lifecycle |
| Dispatch | `execute_tool` [_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) | Schema admission raises `ToolArgumentError` via [_common.py:1234](../../../../src/elspeth/web/composer/tools/_common.py#L1234); `context` unused by the handler |
| Batch prerequisite gate | `promised_resources` promises `("edge", id)` [batch_dependencies.py:77](../../../../src/elspeth/web/composer/batch_dependencies.py#L77); `referenced_resources` references both endpoints as `component` [batch_dependencies.py:110](../../../../src/elspeth/web/composer/batch_dependencies.py#L110); enforced at [tool_batch.py:1551](../../../../src/elspeth/web/composer/tool_batch.py#L1551) | A same-response call whose endpoint was promised by an earlier *refused* call is blocked with `batch_dependency_refused` (pinned at [test_dependent_batch_refusal.py:112](../../../../tests/unit/web/composer/test_dependent_batch_refusal.py#L112)) |
| explicit_approve proposal | [tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728) | Session preference `trust_mode == "explicit_approve"` and `is_mutation_tool` ([discovery.py:86](../../../../src/elspeth/web/composer/tools/discovery.py#L86)); staged and re-admitted on acceptance |
| Audit redaction | Declarative manifest entry [redaction.py:3899](../../../../src/elspeth/web/composer/redaction.py#L3899), rationale [redaction.py:2860](../../../../src/elspeth/web/composer/redaction.py#L2860) | `handles_no_sensitive_data=True`; unknown argument keys sentinel'd; response keys go through the declarative closed projection |
| Argument-error protocol | [protocol.py:945](../../../../src/elspeth/web/composer/protocol.py#L945) | Maps `"upsert_edge arguments"` to the model expectation string |
| Error-code registry / guidance | [error_codes.py:88](../../../../src/elspeth/web/composer/error_codes.py#L88); direct guidance [generation.py:1571](../../../../src/elspeth/web/composer/tools/generation.py#L1571); legacy regex guidance [generation.py:1239](../../../../src/elspeth/web/composer/tools/generation.py#L1239) | Served through `explain_validation_error` and planner repair feedback |
| Cross-tool steering | `patch_node_options` refusals [transforms.py:1705](../../../../src/elspeth/web/composer/tools/transforms.py#L1705)–[:1722](../../../../src/elspeth/web/composer/tools/transforms.py#L1722) and description [:1994](../../../../src/elspeth/web/composer/tools/transforms.py#L1994); `set_pipeline` description [sessions.py:2121](../../../../src/elspeth/web/composer/tools/sessions.py#L2121) | Advertise `upsert_edge` as the routing-edit tool |
| Frontend approval | [ProposalDiff.tsx:605](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx#L605) (edge-only summary [:82](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx#L82)); card sentence [toolCallDescriptions.ts:70](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L70) | The diff shows the edge only, not the scalar mirror it rewrites |
| Frontend graph | [GraphView.tsx:1267](../../../../src/elspeth/web/frontend/src/components/inspector/GraphView.tsx#L1267) | Explicit edges are drawn as stored, including advisory edges that runtime does not execute; ids resolve to sources only via `state.sources` keys ([:1255](../../../../src/elspeth/web/frontend/src/components/inspector/GraphView.tsx#L1255)) |

Instrument: `grep -rn "upsert_edge\|_execute_upsert_edge\|_handle_upsert_edge\|_apply_sink_edge_route\|_clear_removed_sink_edge_route\|_sink_route_still_expressed\|_UPSERT_EDGE_DECLARATION" src/`, with the declaration block filtered out. Positive controls: the `transforms.py:962/978/1510` hits, `batch_dependencies.py` and `ProposalDiff.tsx`. It finds no direct internal caller of the handler outside dispatch, and nothing in `recovery_controls.py` or `pipeline_planner.py`. `_apply_sink_edge_route` has exactly one caller (this tool). `_clear_removed_sink_edge_route` is shared with `remove_edge`. Tutorial check: `grep -ci tutorial` returned 0 for `tools/transforms.py`, `tool_batch.py`, `state.py`, `batch_dependencies.py`, `pipeline_planner.py` and `ProposalDiff.tsx`. Positive control: `composition_completion.py` matches. No tutorial-specific branch exists on this tool's path.

## 4. Alternative paths

Illegal source/kind/edge combinations refuse `edge_not_lowerable`. **Unknown endpoints are not refused**: they are admitted with `success=True`, no scalar rewrite and an `edge_unknown_node` validation error. This is intended for incremental authoring ([state.py:3464](../../../../src/elspeth/web/composer/state.py#L3464) docstring) and pinned by [test_edge_route_reconciliation.py:914](../../../../tests/unit/web/composer/test_edge_route_reconciliation.py#L914). Conflicting sink route edges refuse `edge_route_conflict`. Replacement updates diagram and scalar mirrors before invariant/review checks. A blocking invariant or reconciliation failure returns the original state (`review_reconciliation_failed`). Edges between processing nodes remain visual, even when their connection strings disagree. Stage 1 does **not** report that disagreement as an edge finding: `edge_route_mismatch` is computed only for sink-targeting edges. Node-level findings such as `node_input_not_reachable` appear only when the scalars themselves are broken (upsert_edge-N8). Gate expression-error policy must use upsert_node.on_error. Removing/replacing a sink edge can clear node/source routes, unlike visual-only edge changes; a cleared source becomes `"discard"` and is flagged dangling. A successful edit does not certify runtime-ready configuration.

Late-arriving endpoints are not reconciled. Later `set_output` does not re-mirror an edge whose sink did not exist yet (upsert_edge-N4). Later `upsert_node` can delete a forward-referenced sink edge (upsert_edge-N3). `remove_output` leaves both the edge and the scalar naming the removed sink (cross-cutting).

## 5. Usability assessment

**SG-01 — intended dual authority with a material orchestration cost; high confidence.** One verb means display-only relation for processing edges but executable routing change for sink edges. The description now discloses it, and tests explicitly pin non-sink no-sync behaviour; this is not an undiscovered implementation bug. Nevertheless, the model must reason over two representations and choose a different operation for equivalent-looking edits. Workaround: inspect runtime connection fields, author them explicitly, and use preview rather than infer readiness from the diagram.

The following provisional findings were found in this review. "Demonstrated" means an in-memory handler/dispatch run through the test `execute_tool` boundary ([test_tools.py:344](../../../../tests/unit/web/composer/test_tools.py#L344)) with a mock catalog. These were not end-to-end service runs, and no provider, database, browser or pytest run was involved. Commands and outputs are in the lane report. The 2026-11-09 probe runs (§7.1) later added jsdom vitest renders of GraphView (N1's UI half) and of the pending proposal card (N9); those are not browser runs, and the canvas draw and the hosted Accept panel were not run.

| ID | Classification | Finding |
|---|---|---|
| upsert_edge-N1 | Demonstrated | **Literal `"source"` alias desynchronises silently under named sources.** With `sources={"customers": ...}`, `from_node="source"` → sink is admitted as kind `source` ([state.py:3453](../../../../src/elspeth/web/composer/state.py#L3453)). The edge is stored, but `_apply_sink_edge_route` tests `state.sources` membership ([transforms.py:990](../../../../src/elspeth/web/composer/tools/transforms.py#L990)) and writes nothing. `sink_edge_route_mismatch` makes the same test ([state.py:3556](../../../../src/elspeth/web/composer/state.py#L3556)) and returns `None`, and `edge_unknown_node` whitelists the literal ([state.py:8496](../../../../src/elspeth/web/composer/state.py#L8496)). Observed: `success=True`, `customers.on_success` unchanged (`in`), **zero edge findings**. Control: `from_node="customers"` mirrored correctly. With no source at all, the same call also has zero findings until a `"source"` key appears, when it becomes `edge_route_mismatch`. The declaration's `from_node` description ("Source node ID or 'source'") invites this. **UI (probe P1, §7.1, demonstrated in jsdom):** GraphView maps the edge to `source -> out` while its only source node is `source:customers` ([GraphView.tsx:1255](../../../../src/elspeth/web/frontend/src/components/inspector/GraphView.tsx#L1255)), so the edge's source matches no graph node. The same expandable Wiring panel then contradicts itself. The "Inspect pipeline routes" list shows `source → out: success (success)`, and the Wiring table shows `Source: customers … Send to in (not connected)`. The control (`from_node="customers"`) renders `source:customers → out` with no dangling edge. The canvas half is source-established, not run: React Flow 12.11.2 `EdgeWrapper` returns `null` when `nodeLookup` has no source node, so the diagram draws no line and reports no error. |
| upsert_edge-N2 | Demonstrated | **Edges from a sink, or to a source, are admitted and then misdescribed.** `edge_lowering_error` returns `None` for `from_kind == "output"` ([state.py:3472](../../../../src/elspeth/web/composer/state.py#L3472)), so `from_node=<sink>` succeeds. Validation then says "references unknown node 'out' as from_node" although `out` is a declared sink. A transform → `"source"` edge behaves the same way ("unknown node 'source' as to_node"). Neither direction can ever lower. |
| upsert_edge-N3 | Demonstrated | **A forward-referenced sink edge is stored unmirrored, then silently deleted by `upsert_node`.** `upsert_edge(t9→out)` before `t9` exists returns `success=True` plus `edge_unknown_node`, and writes no scalar. A later `upsert_node(t9, on_success="conn_x")` runs `_reconcile_node_sink_mirror_edges` ([transforms.py:1021](../../../../src/elspeth/web/composer/tools/transforms.py#L1021)), which removes the edge. Observed: edges `[]` afterwards. The `upsert_node` result discloses this only through the edge's absence from the echo. The declaration's "Edges targeting sinks also update the producer's ... routing field" is false for this ordering. |
| upsert_edge-N4 | Demonstrated | **Edge-before-sink ordering leaves a lying sink edge that no tool repairs.** `upsert_edge(t1→later)` before `set_output(later)` stores an advisory edge and leaves `t1.on_success` unchanged. After `set_output` the state carries `edge_route_mismatch`, because `set_output` does not reconcile edges ([outputs.py:137](../../../../src/elspeth/web/composer/tools/outputs.py#L137)). The permutation property test [test_edge_route_reconciliation.py:653](../../../../tests/unit/web/composer/test_edge_route_reconciliation.py#L653) ("never leave a lying sink edge") pre-declares every sink, so it does not cover this order. The skill encourages batches of `set_output`/`upsert_edge` without stating an order ([pipeline_composer.md:190](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L190)). |
| upsert_edge-N5 | Demonstrated; disclosure gap, behaviour intended | **A sink edge overwrites a live processing connection.** `t1.on_success="stream_a"` feeding `t2`; `upsert_edge(t1→out, on_success)` rewrites it to `out`. `success=True`; `validation_delta.new_errors=[node_input_not_reachable for t2]`; the applied-component echo shows the new `t1`. There is no explicit "replaced `stream_a`" fact, and the description does not warn that the previous value is discarded. |
| upsert_edge-N6 | Demonstrated | **Identical replay advances version each time** (versions 4→5→6 for three identical calls; edges and nodes equal; empty `validation_delta`). This is the same semantic-no-op class as SG-03. |
| upsert_edge-N7 | Demonstrated | **A fork edge on a gate with no `fork` route succeeds into a new high error.** Gate routes `{"true":"out","false":"out2"}`; `upsert_edge(fork→out)` writes `fork_to=("out",)` and returns `success=True` with new `gate_fork_to_without_fork_route` ([state.py:8754](../../../../src/elspeth/web/composer/state.py#L8754)). That code is not in `_MUTATION_BLOCKING_INVARIANT_CODES` ([_common.py:3905](../../../../src/elspeth/web/composer/tools/_common.py#L3905)). Neither the description ("fork for a gate fan-out relationship") nor explain guidance names the `routes→"fork"` prerequisite. Whether to block is a design decision; non-disclosure is the defect. |
| upsert_edge-N8 | Demonstrated; behaviour intended, sheet previously overstated | **Advisory processing edges that contradict the scalars produce no edge finding.** A gate `route_true` edge to a processing node `t2` leaves `routes` unchanged (`true→out`) with zero new errors. GraphView still draws the advisory edge ([GraphView.tsx:1267](../../../../src/elspeth/web/frontend/src/components/inspector/GraphView.tsx#L1267)). Duplicate advisory edges `t1→t2` (`d1`, `d2`) and a self-loop `t1→t1` are also accepted with zero edge findings. |
| upsert_edge-N9 | Demonstrated (proposal card, jsdom); Accept panel source-established | **The approval surface omits the routing side-effect.** `ProposalDiff` summarises an `upsert_edge` proposal as `from -> to (type)` only ([ProposalDiff.tsx:82](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx#L82), [:605](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx#L605)). The card sentence is "Adds or replaces a connection between two nodes" ([toolCallDescriptions.ts:70](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L70)). An operator approving an explicit_approve proposal is not shown that a producer's `on_success`/`on_error`/`routes`/`fork_to`, or a source's `on_success` (to `"discard"` on replacement), will change. **Probe P2 (§7.1):** a pending `ToolCallCard` was rendered from real backend outputs: server summary, `affects`, redacted arguments and the before-state. The handler moves `t1.on_success` from `stream_a` to `out`. The card shows only `Added edge e_t1_out t1 -> out (on_success)`, the generic summary "Apply composer tool upsert_edge." and "Affects: graph, validation, yaml". No text, including the raw-JSON expander, mentions `stream_a` or any routing change. The Accept button lives in `DecisionPanel`'s `ProposalItem` ([DecisionPanel.tsx:332](../../../../src/elspeth/web/frontend/src/components/chat/DecisionPanel.tsx#L332)), which shows only `summary` and `affects`. Its summary is the generic fallthrough in [proposals.py:122](../../../../src/elspeth/web/composer/proposals.py#L122), so the operator approves "Accept proposal: Apply composer tool upsert_edge." This half is source-established; DecisionPanel was not rendered. |
| upsert_edge-N10 | Demonstrated (in-memory lookup) | **Explain guidance has no exact-code record for two codes this tool produces.** `explain_validation_code` returns `None` for `edge_route_mismatch` (registered at [error_codes.py:90](../../../../src/elspeth/web/composer/error_codes.py#L90)) and for `gate_fork_to_without_fork_route`. Controls: `edge_route_conflict` returns guidance and an unregistered code returns `None`. Planner repair feedback keyed by code therefore attaches nothing for them ([generation.py:2023](../../../../src/elspeth/web/composer/tools/generation.py#L2023)). |
| upsert_edge-N11 | Demonstrated | **Replacing a source→sink edge id writes a placeholder that reads like a keyword.** Observed: `source.on_success` becomes `"discard"` and `source_on_success_dangling` is reported. Validation is honest, but `"discard"` is a meaningful keyword on node routes, while for a source it is an unrouted placeholder ([core/config.py:1212](../../../../src/elspeth/core/config.py#L1212) requires a connection or sink name). Shared with `remove_edge` and pinned at [test_edge_route_reconciliation.py:899](../../../../tests/unit/web/composer/test_edge_route_reconciliation.py#L899). |
| upsert_edge-N12 | Source-established; no leak shown | **The audit rationale describes a stale response shape.** `_UPSERT_EDGE_REASON.why_responses_safe` says the response is "validation summary plus the edge id list" and "carries no plugin option values" ([redaction.py:2868](../../../../src/elspeth/web/composer/redaction.py#L2868)). The result now carries `applied_component`, which includes the affected nodes' full `options` (observed in the echo). The declarative walker still applies its closed projection to known envelope keys ([redaction.py:4332](../../../../src/elspeth/web/composer/redaction.py#L4332)), so this is a rationale-accuracy finding, not a demonstrated disclosure. |
| upsert_edge-N13 | Hypothesis / unverified | **The `upsert_node`/`upsert_edge` asymmetry may matter for review contracts.** `upsert_edge` reconciles reviews but never calls `composition_review_contract_error`, which `upsert_node` runs after reconciliation ([transforms.py:908](../../../../src/elspeth/web/composer/tools/transforms.py#L908)). No case was constructed in which a graph-only edge change breaks the review contract. Reconciliation reopening is pinned at [test_transform_tools_review_and_guard_parity.py:193](../../../../tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py#L193). |

### 5.1 Guidance parity

Instrument: `grep -n "upsert_edge"` and `grep -n -i "edge"` over both skills, `prompts.py` and `pipeline_planner.py`. Control: `upsert_edge` matches 5 lines in `pipeline_composer.md`. `pipeline_capabilities.md` has 0 `upsert_edge` matches, and its `edge` grep does match ([:252](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L252), the `get_pipeline_state` edge row). The capability core is prepended to the freeform system prompt ([capability_skill.py:154](../../../../src/elspeth/web/composer/capability_skill.py#L154), [prompts.py:75](../../../../src/elspeth/web/composer/prompts.py#L75)), so the freeform model reads both files.

- **Contradiction across skills (GS-03 class).** [pipeline_capabilities.md:305](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L305) says "Edge objects alone do not make a sink receive rows; route through every intervening cleanup node and then from the cleanup node to the sink." [:262](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L262) says "Edges state reviewed graph relationships, while routing fields still determine the runtime connection contract." Both hold for `set_pipeline` edge objects. They contradict this tool's sink mirror and [pipeline_composer.md:302](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L302)/[:1443](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L1443), which tell the model to use `upsert_edge` to a sink "so routing is synchronized".
- **Guidance that pushes the model toward the wrong tool.** [pipeline_composer.md:178](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L178) offers "`upsert_node` / `upsert_edge`" for "Add or rewire a node", and [sessions.py:2121](../../../../src/elspeth/web/composer/tools/sessions.py#L2121) repeats it in the `set_pipeline` description. Neither says that rewiring between processing nodes requires `upsert_node` (`input`/`on_success`) and that `upsert_edge` there is display-only.
- **Stale guidance served by `explain_validation_error`.** [generation.py:1241](../../../../src/elspeth/web/composer/tools/generation.py#L1241) and [:1246](../../../../src/elspeth/web/composer/tools/generation.py#L1246) say "Edges are UI-only". That contradicts the declaration's sink mirror. For `edge_unknown_node` on a forward-referenced sink edge (N3/N4), it also tells the model the edge has no runtime meaning when the model intended a sink route.
- **Missing guidance.** Neither skill explains:
  - forward references: an edge stored before its producer or sink exists is advisory and is not re-mirrored later (N3/N4);
  - that the literal `"source"` is valid only for the single default source (N1);
  - that `route_true`/`route_false` to a processing node does not set gate routes (use `upsert_node.routes`) (N8);
  - that `fork` needs a gate route whose destination is `"fork"` (N7);
  - replacement-by-id semantics and source `"discard"` on replacement (N11);
  - the one-edge-per-slot rule behind `edge_route_conflict` (catalogue guidance exists, skill text does not).

  The declaration's per-argument descriptions (`id`: "Unique edge identifier.") do not say that reusing an id replaces and re-routes.
- **Accurate guidance (no finding).**
  - Gate on_error belongs on the node: [pipeline_capabilities.md:143](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L143) matches the declaration and [state.py:3488](../../../../src/elspeth/web/composer/state.py#L3488).
  - Collector has no error edge: [pipeline_capabilities.md:205](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L205) matches the matrix.
  - The `applied_component.edges` echo is taught at [pipeline_composer.md:535](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L535).
  - `prompts.py` and `pipeline_planner.py` contain no `upsert_edge` text (0 matches), which is consistent with planner non-exposure.

## 6. Structural remediation

Make executable connection identity authoritative and derive visual edges, or explicitly type visual annotations separately from executable route edits. Do not silently change existing non-sink edges into runtime wiring. Provide model-selected atomic connect/rewire operations over one graph contract, with typed effects and review invalidation. Regression: two processing nodes are connected only by the declared runtime operation; diagrams and runtime sinks agree after add/replace/remove; wrong-kind routes, duplicate slots and gate error-edge misuse reject atomically; review hashes reopen when upstream dataflow changes. Compatibility risk high because saved edges and scalar routes coexist; migrate by preserve/refuse inspection, not heuristic rewiring.

Until then, the edge/scalar mirror needs a single reconciliation point that runs on every endpoint change: `set_output`, `remove_output`, source binding and `upsert_node`. Today only `upsert_node` reconciles, and only for already-declared outputs. Per-tool fixes for N3/N4 would otherwise keep regressing.

### 6.1 Simple enhancements

Bounded, non-architectural; none implemented.

1. **Resolve or refuse the `"source"` alias consistently (N1).**
   - Where: [state.py:3453](../../../../src/elspeth/web/composer/state.py#L3453), [transforms.py:990](../../../../src/elspeth/web/composer/tools/transforms.py#L990), [state.py:3556](../../../../src/elspeth/web/composer/state.py#L3556), [state.py:8496](../../../../src/elspeth/web/composer/state.py#L8496).
   - What: classify `"source"` as a source only when `"source" in sources` or `sources` is empty. Make the mirror writer and the mismatch check use the same rule. Alternatively, refuse with `edge_not_lowerable` and name the actual source keys.
   - Why: today this combination yields a stored edge, no scalar and no finding.
   - Acceptance: with `sources={"customers"}`, `from_node="source"` → sink is refused (or flagged) and `customers.on_success` is unchanged. `from_node="customers"` still mirrors. The single-default-source tests (1899, 914) stay green.
2. **Refuse edges from an output or to a source (N2).**
   - Where: [state.py:3472](../../../../src/elspeth/web/composer/state.py#L3472).
   - What: return a lowering error for `from_kind == "output"` and `to_kind == "source"`, with the message "sinks have no outgoing routes" / "sources have no inbound routes".
   - Acceptance: both calls return `success=False`, `edge_not_lowerable`, unchanged state. Extend the matrix pin at [test_edge_route_reconciliation.py:579](../../../../tests/unit/web/composer/test_edge_route_reconciliation.py#L579) with the output row.
3. **Disclose the advisory outcome of a sink-intended edge (N3/N4).**
   - Where: [transforms.py:1582](../../../../src/elspeth/web/composer/tools/transforms.py#L1582)–[:1591](../../../../src/elspeth/web/composer/tools/transforms.py#L1591).
   - What: when `edge_type` is a sink-capable slot and either endpoint is unresolved, add a result fact. Example: `data={"runtime_route_written": false, "reason": "producer_not_declared" | "target_not_declared"}`. Set `runtime_route_written: true` with the slot name when the mirror is written.
   - Acceptance: the forward-ref call reports `false` and the declared-endpoints call reports `true`. The skill teaches the field.
4. **Name the overwritten value (N5).**
   - Where: same commit block.
   - What: include `{"replaced_route": {"component": ..., "field": ..., "previous": ...}}` in `data` when `_apply_sink_edge_route` changes a non-null scalar, or when the replacement clear rewrites a different producer.
   - Acceptance: the S5 case reports `previous="stream_a"`; the already-matching case (1982) reports nothing.
5. **Semantic no-op on identical replay (N6).**
   - Where: [transforms.py:1569](../../../../src/elspeth/web/composer/tools/transforms.py#L1569).
   - What: if `old_edge == edge` and the scalar already matches, return the unchanged state without a version bump (aligned with the SG-03 R4 policy).
   - Acceptance: two identical calls leave the version unchanged on the second. Test 1982's `v_before + 1` still holds for the first call.
6. **Disclose the gate fork prerequisite (N7).**
   - Where: declaration [transforms.py:481](../../../../src/elspeth/web/composer/tools/transforms.py#L481).
   - What: add "fork requires a gate route whose destination is 'fork' (set with upsert_node.routes)" and a direct guidance record for `gate_fork_to_without_fork_route`.
   - Acceptance: the description string contains the clause, and `explain_validation_code("gate_fork_to_without_fork_route")` returns non-None.
7. **Add guidance for `edge_route_mismatch` (N10).**
   - Where: `_DIRECT_VALIDATION_GUIDANCE` [generation.py:1571](../../../../src/elspeth/web/composer/tools/generation.py#L1571).
   - What: an explanation that the drawn sink edge and the producer's routing field disagree; the fix is to call `upsert_edge` again with the same id (re-mirrors) or `remove_edge`.
   - Acceptance: an exact-code lookup returns the record. Extend the catalogue test at [test_edge_route_reconciliation.py:925](../../../../tests/unit/web/composer/test_edge_route_reconciliation.py#L925).
8. **Replace "Edges are UI-only".**
   - Where: [generation.py:1241](../../../../src/elspeth/web/composer/tools/generation.py#L1241), [:1246](../../../../src/elspeth/web/composer/tools/generation.py#L1246).
   - What: "Processing-node edges are advisory; sink-targeting edges mirror the producer's routing field."
   - Acceptance: a grep for "UI-only" in `generation.py` returns 0, and `explain_validation_error("edge_unknown_node")` carries the new text.
9. **Fix the skill contradiction and the rewire row.**
   - Where: [pipeline_capabilities.md:305](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L305), [pipeline_composer.md:178](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L178).
   - What: scope the capability sentence to `set_pipeline`/proposal edge objects. Split the table row into "rewire processing connection → `upsert_node`" and "route a producer to a declared sink → `upsert_edge`".
   - Acceptance: skill hash tests updated; no sentence in either skill says edges never route sinks.
10. **Show the routing effect in approval (N9).**
    - Where: [ProposalDiff.tsx:605](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx#L605), [toolCallDescriptions.ts:70](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L70).
    - What: when `to_node` is a declared output in `currentState`, add a second entry for the producer's routing field (before → after). Extend the sentence with "...and, for a sink, updates the producer's routing".
    - Acceptance: a `ProposalDiff.test.tsx` case with a sink target shows the producer field change.
11. **Update the audit rationale (N12).**
    - Where: [redaction.py:2868](../../../../src/elspeth/web/composer/redaction.py#L2868).
    - What: describe the actual envelope, including `applied_component`, and why the closed projection makes it safe.
    - Acceptance: the redaction policy snapshot ([redaction_policy_snapshot.json](../../../../tests/unit/web/composer/redaction_policy_snapshot.json)) is regenerated deliberately; a test asserts `applied_component` projection for `upsert_edge`.
12. **Per-argument descriptions.**
    - Where: [transforms.py:478](../../../../src/elspeth/web/composer/tools/transforms.py#L478)–[:480](../../../../src/elspeth/web/composer/tools/transforms.py#L480).
    - What:
      - `id`: "Reusing an existing id replaces that edge and releases the route it expressed."
      - `from_node`: "A node id or a source name; 'source' only for the single default source."
      - `to_node`: "For a runtime sink route, the sink must already be declared."
    - Acceptance: the wire projection ledger test ([test_wire_projection.py:518](../../../../tests/unit/web/composer/test_wire_projection.py#L518)) passes with the new sentences.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_tools_mutations.py:1511](../../../../tests/unit/web/composer/test_tools_mutations.py#L1511) — `test_adds_edge` (advisory source→node edge). Sink success/error mirrors are [:1531](../../../../tests/unit/web/composer/test_tools_mutations.py#L1531) and [:1573](../../../../tests/unit/web/composer/test_tools_mutations.py#L1573); gate route and fork mirrors are [:1769](../../../../tests/unit/web/composer/test_tools_mutations.py#L1769) and [:1816](../../../../tests/unit/web/composer/test_tools_mutations.py#L1816); the source mirror is [:1899](../../../../tests/unit/web/composer/test_tools_mutations.py#L1899).
- [tests/unit/web/composer/test_tools_mutations.py:1612](../../../../tests/unit/web/composer/test_tools_mutations.py#L1612) — gate error edge rejection (and [:1655](../../../../tests/unit/web/composer/test_tools_mutations.py#L1655) to a processing node).
- [tests/unit/web/composer/test_tools_mutations.py:1940](../../../../tests/unit/web/composer/test_tools_mutations.py#L1940) — test_edge_to_node_does_not_sync.
- [tests/unit/web/composer/test_tools_mutations.py:1982](../../../../tests/unit/web/composer/test_tools_mutations.py#L1982) — already-matching sink route avoids an extra helper version bump.
- [tests/unit/web/composer/test_edge_route_reconciliation.py](../../../../tests/unit/web/composer/test_edge_route_reconciliation.py) — dedicated suite, previously uncited:
  - mirror atomicity on retarget/replace/duplicate (:183–:263);
  - lowerability admission (:284–:404) and exhaustive matrix pin (:579);
  - permutation "no lying sink edge" property (:653; pre-declared sinks only, see N4);
  - named-source mirror clear to `"discard"` (:899);
  - advisory/unresolved edges succeed without scalar rewrite (:914);
  - catalogue guidance for `edge_not_found`/`edge_not_lowerable`/`edge_route_conflict` (:925).
- [tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py:193](../../../../tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py#L193) and [:218](../../../../tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py#L218) — graph mutations reopen drifted reviews and map reconciliation failure to a repairable rejection.
- [tests/unit/web/composer/test_applied_component_echo.py:115](../../../../tests/unit/web/composer/test_applied_component_echo.py#L115) — echo carries the server-retargeted mirror edge (upsert_edge call at :138).
- [tests/unit/web/composer/test_dependent_batch_refusal.py:112](../../../../tests/unit/web/composer/test_dependent_batch_refusal.py#L112) — batch prerequisite refusal for a dependent edge.
- [tests/unit/composer_mcp/test_server.py:267](../../../../tests/unit/composer_mcp/test_server.py#L267) — MCP exposes `upsert_edge`.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance.

Sheet review (2026-11-09) added in-memory handler/dispatch observations for N1–N8, N10 and N11, through the test `execute_tool` trust boundary with a mock catalog. `elspeth.__file__` resolved into this worktree. No pytest, database, provider or browser was involved, and none of these are test-suite results. No new test was run. The two UI probes in §7.1 below were run later, as ad-hoc lane-private vitest files. They are not committed tests.

### 7.1 Probe runs (2026-11-09)

All runs used the fail-closed prefix (`HTTPS_PROXY`/`HTTP_PROXY=http://127.0.0.1:9`, `NO_PROXY=`, `LITELLM_LOCAL_MODEL_COST_MAP=True`, `ELSPETH_DISABLE_NETWORK=1`, `PYTHONDONTWRITEBYTECODE=1`), with `TMPDIR=$L/tmp`. Inputs came from real backend outputs. `$L/gen_states.py` first installs a guard that makes `litellm.acompletion`, `litellm.completion`, `provider_gateway._litellm_acompletion` and `pipeline_planner._litellm_acompletion` raise. Its positive control passed (all four raised). It then runs `upsert_edge` and `set_output` through the test `execute_tool` boundary and writes `CompositionState.to_dict()` JSON, plus `build_tool_proposal_summary` and `redact_tool_call_arguments` output for the N9 proposal. `elspeth.__file__` resolved to `$WT/src/elspeth/__init__.py`. Command: `cd $WT && PYTHONPATH=$WT:$WT/src:$WT/elspeth-lints/src <venv>/bin/python $L/gen_states.py $L/out`, `exit=0`. It re-observed N1 (`customers.on_success` stays `in`, zero edge findings; control mirrors to `out`) and the N9 effect (`t1.on_success` `stream_a` → `out`).

The frontend `include` admits only `src/**`. The probes therefore ran through a lane-private config (`$L/vitest.probe.config.mts`: `root` = the frontend, `@` alias, the frontend's own setup files, `include` = `$L/*.probe.test.tsx`, and `cacheDir` under `$L`). `$L/node_modules` is a symlink to the frontend `node_modules`. Form: `cd $WT/src/elspeth/web/frontend && FE=… L=… npx vitest run --config $L/vitest.probe.config.mts $L/<file>`. The vitest runs had the environment prefix only; no in-process guard was installed in them (the guard above is in `gen_states.py`). Each probe has a known-positive and a known-negative case. A `PROBE_FLIP=1` rerun inverts the observed-now assertion and must turn red.

| Probe | Finding | Outcome | Exit (run / flip) | Observed |
|---|---|---|---|---|
| P1 `graphview_n1.probe.test.tsx` (React Flow, dagre and `useTheme` mocks copied verbatim from `GraphView.test.tsx`) and `graphview_n1_realdagre.probe.test.tsx` (real dagre) | N1 (UI) | Two outcomes. GraphView projection in jsdom: **confirmed**. Canvas draw of the edge: **not runnable here** (needs a browser; the library-source reading below is not a run) | 0 / 1; real dagre 0 | Case: graph nodes `[out, source:customers]`, edges handed to React Flow `[source->out]`, dangling `[source->out]`; dagre was given the edge with no `source` node, and the real dagre layout did not throw. Routes list: `source → out: success (success)`; Wiring table: `Source: customers … Send to in (not connected)`. Control (`from_node="customers"`): edge `source:customers->out`, dangling `[]`. The mock renders every edge it is handed. Whether the canvas draws the line therefore comes from the library source only: `@xyflow/react` 12.11.2 `EdgeWrapper` returns `null` for a missing source node. |
| P2 `proposal_n9.probe.test.tsx` (renders `ToolCallCard` pending, current state = real before-state) | N9 | Two outcomes. Proposal card in jsdom: **confirmed**. Hosted Accept panel (`DecisionPanel`): **not runnable here** (needs a browser running the approval flow); its content is source-established only | 0 / 1 | Server effect `t1.on_success: stream_a → out`. Visible card text: heading sentence, "Apply composer tool upsert_edge.", "Affects: graph, validation, yaml", "Added edge e_t1_out t1 -> out (on_success)". Neither `stream_a` nor any route/routing wording appears, even in the raw-JSON expander. Positive control: a summary that states the change is detected. The first draft of the detector failed this control (a `\b` across concatenated `textContent`) and was fixed before the case run. |

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. For an N1 edge, GraphView's edge model and its routes and Wiring lists were measured in jsdom (§7.1 P1). Whether the React Flow canvas draws the edge was not run in a browser; it is established from the library source only. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.transforms._handle_upsert_edge`](../../../../src/elspeth/web/composer/tools/transforms.py#L456).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | no |
| Composer MCP | yes |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

Re-measured during sheet review by importing the live producers:
- canonical: 43 tools, present;
- loop `openai_strict` and `none`: 45 each, present;
- planner `openai_strict` and `none`: 23 each, absent;
- MCP: 32, present.

Controls: `emit_pipeline_proposal` and `get_pipeline_state` are present in both planner sets, and a sentinel name is absent everywhere.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Unique edge identifier."
    },
    "from_node": {
      "type": "string",
      "description": "Source node ID or 'source'."
    },
    "to_node": {
      "type": "string",
      "description": "Destination node ID or sink name."
    },
    "edge_type": {
      "type": "string",
      "enum": [
        "on_success",
        "on_error",
        "route_true",
        "route_false",
        "fork"
      ],
      "description": "Connection kind. Use on_success for a direct success relationship; on_error only from a transform or aggregation to a sink; route_true or route_false for gate routes; fork for a gate fan-out relationship. Gate expression errors use the gate node's on_error field instead. Edges targeting sinks also update the producer's corresponding runtime routing field; other edges are display relationships and do not replace node connection fields."
    },
    "label": {
      "type": [
        "string",
        "null"
      ],
      "description": "Display label."
    }
  },
  "required": [
    "id",
    "from_node",
    "to_node",
    "edge_type"
  ],
  "additionalProperties": false,
  "examples": [
    {
      "id": "e_judge_layers_error",
      "from_node": "judge_layers",
      "to_node": "llm_failures",
      "edge_type": "on_error",
      "label": "LLM failures"
    }
  ]
}
```
