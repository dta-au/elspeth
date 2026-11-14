# `diff_pipeline` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

The tool compares the current pipeline with a baseline supplied by the caller. Each route supplies a different baseline, or none, so the tool does not inherently mean "last saved state" or "request start". It is noncacheable discovery and cannot change either input state.

Measured per route at this baseline (see §3.1):

- **Freeform web loop:** no baseline is ever supplied, so every call fails (GS-01).
- **Web planner:** the baseline is the same object as the diffed state, so every call succeeds with zero changes (`diff_pipeline-N1`).
- **Composer MCP:** the baseline is the state captured at server start, `new_session` or `load_session`. Since `load_session` also resets it on failure, the baseline is never `None` there (`diff_pipeline-N2`).

No web route currently yields a non-trivial comparison. The only route that does is the local MCP server.

## 2. Contracts and authority

The tool takes no semantic arguments. The handler first admits arguments through `EmptyToolArgumentsModel` ([src/elspeth/web/composer/tools/generation.py:4647](../../../../src/elspeth/web/composer/tools/generation.py#L4647)). Useful behaviour requires the caller-owned `ToolContext.baseline`, but the generic dispatcher API treats it as optional ([src/elspeth/web/composer/tools/_dispatch.py:810](../../../../src/elspeth/web/composer/tools/_dispatch.py#L810)).

**Success.** `data` is the `DiffResponse` record ([src/elspeth/web/composer/tools/_generation_responses.py:89](../../../../src/elspeth/web/composer/tools/_generation_responses.py#L89)), with these fields:

- `from_version`
- `sources_changed`, `metadata_changed`
- `nodes` / `edges` / `outputs` change sets (`added` / `removed` / `modified` ID lists)
- `sources`, present exactly when `sources_changed` (enforced at line 102)
- `warnings_introduced` / `warnings_resolved`
- `total_changes`

The "to" version is the envelope's own `version`.

**Missing baseline.** The result is a `SAVED_GRAPH` rejection ([src/elspeth/web/composer/tools/generation.py:4665](../../../../src/elspeth/web/composer/tools/generation.py#L4665)):

- `success: false` and no `data`.
- A leading `validation.errors` entry with `component: "rejected_mutation"`, message "No baseline available. Load or create a session first." and `error_code: "diff_baseline_unavailable"`.
- The current state's standing errors and warnings appended after that entry.
- `validation.is_valid` forced to `false` ([src/elspeth/web/composer/tools/_common.py:1605](../../../../src/elspeth/web/composer/tools/_common.py#L1605)).

There is no top-level `error` key. The declaration text claims otherwise (`diff_pipeline-N6`).

## 3. Source trace to the user

`_execute_diff_pipeline` reads `context.baseline` and `context.current_validation` ([src/elspeth/web/composer/tools/generation.py:4659](../../../../src/elspeth/web/composer/tools/generation.py#L4659)), then proceeds as follows:

1. It refuses when the baseline is `None`.
2. Otherwise it validates the baseline through the policy catalog ([line 4672](../../../../src/elspeth/web/composer/tools/generation.py#L4672)).
3. It calls `diff_states` ([src/elspeth/web/composer/tools/_common.py:1136](../../../../src/elspeth/web/composer/tools/_common.py#L1136)).
4. It returns a `_discovery_result` over the current state ([line 4679](../../../../src/elspeth/web/composer/tools/generation.py#L4679)).

Baseline supply differs by caller:

- **Planner:** passes `current_state` as the baseline ([src/elspeth/web/composer/pipeline_planner.py:3434](../../../../src/elspeth/web/composer/pipeline_planner.py#L3434)). It also passes `current_state` as the diffed state ([src/elspeth/web/composer/pipeline_planner.py:4905](../../../../src/elspeth/web/composer/pipeline_planner.py#L4905)), so the planner comparison is identically empty (`diff_pipeline-N1`).
- **MCP:** forwards its held `baseline_ref` ([src/elspeth/composer_mcp/server.py:864](../../../../src/elspeth/composer_mcp/server.py#L864), [src/elspeth/composer_mcp/server.py:409](../../../../src/elspeth/composer_mcp/server.py#L409)).
- **Freeform:** the callsite omits the baseline ([src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)).

**Freeform exposure.** The tool reaches the freeform model through `get_tool_definitions`, then the wire projection, then `composer_loop_tool_definitions` ([src/elspeth/web/composer/tools/_dispatch.py:311](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391)).

**Planner exposure.** The planner emits an ordered subset of `PLANNER_DISCOVERY_TOOL_NAMES` ([src/elspeth/web/composer/pipeline_planner.py:1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647)). The API allows narrowing, but no 0.8.9 constructor narrows the subset: both `discovery_tool_names=` sites pass the full tuple ([src/elspeth/web/composer/pipeline_planner.py:379](../../../../src/elspeth/web/composer/pipeline_planner.py#L379), [src/elspeth/web/composer/pipeline_planner.py:398](../../../../src/elspeth/web/composer/pipeline_planner.py#L398)). In practice the planner always advertises `diff_pipeline`, including on the escape-hatch turn ([src/elspeth/web/composer/pipeline_planner.py:4268](../../../../src/elspeth/web/composer/pipeline_planner.py#L4268)).

**Freeform admission.** Before `execute_tool`, public freeform calls pass credential and batch admission, wire decoding, and closed semantic-schema admission. Internal direct calls must explicitly opt into public argument validation ([src/elspeth/web/composer/tool_batch.py:1010](../../../../src/elspeth/web/composer/tool_batch.py#L1010), [src/elspeth/web/composer/tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399), [src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)).

**After the handler:**

1. `finalize_tool_result` normalizes validation ([src/elspeth/web/composer/tools/_dispatch.py:752](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752)). Inline `validation_guidance` is scoped to mutation tools ([src/elspeth/web/composer/tools/_dispatch.py:742](../../../../src/elspeth/web/composer/tools/_dispatch.py#L742)), so a freeform diff refusal carries no repair guidance.
2. On the web routes, discovery result admission selects the declared response contract before serialization ([src/elspeth/web/composer/tool_batch.py:3052](../../../../src/elspeth/web/composer/tool_batch.py#L3052)). MCP does not apply it (`diff_pipeline-N4`).
3. The loop records the invocation, sends a tool result back to the model, and persists its redacted tool-turn cohort ([src/elspeth/web/composer/tool_batch.py:3344](../../../../src/elspeth/web/composer/tool_batch.py#L3344), [src/elspeth/web/composer/service.py:1515](../../../../src/elspeth/web/composer/service.py#L1515)).
4. Later operation settlement owns user-facing success or failure ([src/elspeth/web/sessions/composer_turn.py:1051](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)).

The freeform loop also stores the failed result's validation (including the synthetic `rejected_mutation` entry) as `last_validation` ([src/elspeth/web/composer/tool_batch.py:3291](../../../../src/elspeth/web/composer/tool_batch.py#L3291)). That value feeds the next call's `prior_validation` (`diff_pipeline-N5`).

ToolCallCard renders the recorded outcome and explanatory tooltip; this tool has no direct action button of its own ([src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:90](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L90)). A completed lookup does not imply pipeline readiness.

### 3.1 Route inventory

Instruments:

- `grep -rn "diff_pipeline\|_execute_diff_pipeline\|diff_baseline_unavailable" src/` found every source site.
- `grep -n "baseline" tool_batch.py service.py` returned 0 lines. The same grep matched `sessions/models.py` and `composer_mcp/server.py`, so the instrument works.
- Membership comes from the read-only introspection in §7: loop 45 tools, planner 23 and MCP 32, with `diff_pipeline` present in all three and `upsert_node` absent from the planner.

| Route | Entry / advertisement | Baseline supplied | Gate / policy | Observable outcome |
|---|---|---|---|---|
| Declaration and registry | `_DIFF_PIPELINE_DECLARATION` ([generation.py:4682](../../../../src/elspeth/web/composer/tools/generation.py#L4682)); `ToolKind.DISCOVERY`, `cacheable=False`, so it is in `_SESSION_MUTABLE_DISCOVERY_TOOL_NAMES` ([_registry.py:191](../../../../src/elspeth/web/composer/tools/_registry.py#L191)) | n/a | Closed empty-object schema | Canonical emitter includes it ([_dispatch.py:311](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311)) |
| Freeform web loop | [provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) → decode [tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399) → `execute_tool` [tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008) → `admit_discovery_result` [tool_batch.py:3052](../../../../src/elspeth/web/composer/tool_batch.py#L3052) → serialize [tool_batch.py:3345](../../../../src/elspeth/web/composer/tool_batch.py#L3345) | **None** (no `baseline=` kwarg) | Credential/batch/wire/schema admission; not proposal-intercepted (discovery); never cached (`cacheable=False`, so the cache replay at [tool_batch.py:1619](../../../../src/elspeth/web/composer/tool_batch.py#L1619) does not apply); failure enters the anti-anchor failure window ([tool_batch.py:3343](../../../../src/elspeth/web/composer/tool_batch.py#L3343)) | Always `diff_baseline_unavailable` (GS-01) |
| Web planner | `PLANNER_DISCOVERY_TOOL_NAMES` ([capability_skill.py:16](../../../../src/elspeth/web/composer/capability_skill.py#L16)) → `planner_tool_definitions` ([pipeline_planner.py:1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647)) → `execute_discovery_tool_with_context` ([_dispatch.py:999](../../../../src/elspeth/web/composer/tools/_dispatch.py#L999)) at [pipeline_planner.py:4902](../../../../src/elspeth/web/composer/pipeline_planner.py#L4902) | `current_state`, identical to the diffed state ([pipeline_planner.py:3434](../../../../src/elspeth/web/composer/pipeline_planner.py#L3434)) | Discovery-only seam; an `AuditIntegrityError` fires if state changes ([pipeline_planner.py:4909](../../../../src/elspeth/web/composer/pipeline_planner.py#L4909)); credited as information key `pipeline.diff` ([pipeline_planner.py:470](../../../../src/elspeth/web/composer/pipeline_planner.py#L470)) | Always success, `total_changes: 0` (`diff_pipeline-N1`) |
| Composer MCP (local) | `_COMPOSER_TOOL_NAMES` = all sync discovery and mutation tools ([server.py:117](../../../../src/elspeth/composer_mcp/server.py#L117)), filtered at [server.py:210](../../../../src/elspeth/composer_mcp/server.py#L210) → `_dispatch_tool` → `execute_tool` ([server.py:402](../../../../src/elspeth/composer_mcp/server.py#L402)) | `baseline_ref[0]`: empty v1 state at start ([server.py:718](../../../../src/elspeth/composer_mcp/server.py#L718)), reset on `new_session`/`load_session` ([server.py:907](../../../../src/elspeth/composer_mcp/server.py#L907)) | Trained-operator catalog; public argument validation; **no response-contract admission** (`result.to_dict()` at [server.py:415](../../../../src/elspeth/composer_mcp/server.py#L415)) | Real delta since start or session load; unrecoverable baseline loss after a failed load (`diff_pipeline-N2`) |
| Recovery controls | none: `grep diff recovery_controls.py` → 0 lines; control `grep -c handoff_to_user` → 3 | — | — | Not a route |
| Proposal / approval / replay | none: discovery tool, no proposal, park or replay branch; non-cacheable | — | — | Not a route |
| Async / operation | none tool-specific; the result rides the ordinary tool-turn cohort and operation settlement | — | — | — |
| Direct internal callers | `_execute_diff_pipeline` is called only via the registry (plus tests). `diff_states` is re-exported ([tools/\_\_init\_\_.py:56](../../../../src/elspeth/web/composer/tools/__init__.py#L56)) but its only production caller is [generation.py:4673](../../../../src/elspeth/web/composer/tools/generation.py#L4673) | — | — | — |
| REST routes | none: no `diff_pipeline`/`diff_states` hit under `src/elspeth/web/**/routes`; a `grep diff` there matched only "different", which shows the instrument works | — | — | — |
| Validation catalogue | `diff_baseline_unavailable` pattern and guidance ([generation.py:459](../../../../src/elspeth/web/composer/tools/generation.py#L459), [generation.py:1968](../../../../src/elspeth/web/composer/tools/generation.py#L1968)); closed code ([error_codes.py:80](../../../../src/elspeth/web/composer/error_codes.py#L80)) | — | Served by `explain_validation_error` and planner rejection feedback; not attached inline on freeform | Text attributes the failure to the session (`diff_pipeline-N7`) |
| Redaction | declarative `ToolRedaction` ([redaction.py:3844](../../../../src/elspeth/web/composer/redaction.py#L3844)) with reason ([redaction.py:2821](../../../../src/elspeth/web/composer/redaction.py#L2821)) | — | `handles_no_sensitive_data=True` | Stale justification (`diff_pipeline-N11`) |
| Progress | [progress.py:545](../../../../src/elspeth/web/composer/progress.py#L545), [progress.py:646](../../../../src/elspeth/web/composer/progress.py#L646) | — | — | "The model is checking the current pipeline." |
| Frontend | tooltip ([toolCallDescriptions.ts:37](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L37)); read-only name list ([toolCallDescriptions.test.ts:36](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.test.ts#L36)); ToolCallCard | — | — | "Looked up" label; tooltip overclaims (`diff_pipeline-N12`) |

**Tutorial check.** `grep -n -i tutorial` over `_dispatch.py`, `composer_mcp/server.py` and `tool_batch.py` returned 0 lines. The two `generation.py` hits (lines 1518 and 2559) are unrelated to this handler. No tutorial-specific branch exists on any diff route.

## 4. Branches, failures and recovery

**Branches:**

- **No baseline:** explicit saved-graph failure. Reachable only on freeform and from direct callers; MCP and the planner always supply one.
- **Identical baseline and current:** success with zero changes. Always the outcome on the planner.
- **Changed metadata:** success with `metadata_changed: true`, counted as 1.
- **Structural collections:** nodes, edges and outputs use identity-based diffs (`id`/`name` keys plus whole-spec equality). A modified entry reports only its ID, not which fields changed.
- **Sources:** reported by name, but all source changes together contribute **1** to `total_changes`, whatever their number ([src/elspeth/web/composer/tools/_common.py:1228](../../../../src/elspeth/web/composer/tools/_common.py#L1228)).
- **Warnings:** compared as sets of message text only ([src/elspeth/web/composer/tools/_common.py:1221](../../../../src/elspeth/web/composer/tools/_common.py#L1221)). Errors are not diffed at all (`diff_pipeline-N8`, `diff_pipeline-N9`).

**Failure handling.** A malformed baseline or catalog failure raises from `validate_composition_state` or `diff_states` rather than becoming zero changes. Argument rejection occurs before the handler. Expected policy or semantic refusal is a failed result on the current state. Controlled-code exceptions remain framework or plugin failures, and the common loop retains the executed prefix and audit evidence. Cancellation, timeout and final persistence failure belong to the request or operation owner, not to a handler-local retry. None of these discovery handlers commits graph changes or needs a proposal approval step. Retrying a lookup can recover from an argument or infrastructure problem, but cannot repair the graph by itself.

**Cache, idempotency, concurrency and retry:**

- **Kind and cache:** `ToolKind.DISCOVERY`, `cacheable=False`.
- **Idempotency:** a repeated call over the same immutable baseline/current pair gives the same semantic delta and has no graph or storage effect, but is recorded as a distinct audit invocation.
- **Concurrency:** caller-supplied current state, prior validation and baseline define the observation. The handler acquires no session write lock and does not refresh them from storage.
- **Stale baseline:** can still produce a mathematically valid comparison with the wrong scope, hence R1's explicit context requirement.
- **Retry:** no automatic handler retry exists, and repeated freeform calls cannot remedy the missing baseline. Each repeat also adds to the freeform anti-anchor failure window. Provider transport retries are separate.
- **Validation symmetry:** the baseline is validated through the policy catalog. The current side uses the caller's `current_validation`, which `execute_tool` sets to `prior_validation or catalog.validate_composition_state(state)` ([src/elspeth/web/composer/tools/_dispatch.py:923](../../../../src/elspeth/web/composer/tools/_dispatch.py#L923)). On freeform `prior_validation` is the previous tool's normalized validation, so a future freeform wiring could compare differently sourced summaries (`diff_pipeline-N13`, hypothesis).

## 5. Defects, restrictions and hypotheses

**GS-01 — confirmed freeform route wiring defect.** The advertised freeform capability cannot compare against a baseline:

- the production `execute_tool` call supplies none;
- the dispatcher defaults the baseline to `None` ([src/elspeth/web/composer/tools/_dispatch.py:810](../../../../src/elspeth/web/composer/tools/_dispatch.py#L810));
- the handler rejects that condition.

Direct controls reproduce failure with omitted context, one metadata change with a supplied baseline, and zero changes with an identical baseline. Probe P3 (§7.1) also drove the real freeform compose loop with a scripted provider: `diff_pipeline` was advertised and its result reached the model as `success: false` with `diff_baseline_unavailable`. That is a service-level run of this defect, not only a handler-level one. The user receives "Load or create a session first," which does not repair this route's omission. MCP wiring is counterevidence against a broken diff algorithm, not against the freeform integration defect. *Correction: this sheet previously also cited planner wiring as counterevidence. Planner wiring is the degenerate case below, not a working route.*

**diff_pipeline-N1 — demonstrated route defect (probe P1, §7.1).** The planner advertises `diff_pipeline` but can only diff `current_state` against itself:

- the request context binds `baseline=current_state` ([src/elspeth/web/composer/pipeline_planner.py:3434](../../../../src/elspeth/web/composer/pipeline_planner.py#L3434));
- the single invocation site passes the same `current_state` as the diffed state ([src/elspeth/web/composer/pipeline_planner.py:4905](../../../../src/elspeth/web/composer/pipeline_planner.py#L4905));
- that site asserts state is unchanged ([src/elspeth/web/composer/pipeline_planner.py:4909](../../../../src/elspeth/web/composer/pipeline_planner.py#L4909));
- `current_validation` is the same catalog validation of that state ([src/elspeth/web/composer/pipeline_planner.py:3399](../../../../src/elspeth/web/composer/pipeline_planner.py#L3399)).

Every planner call therefore returns `total_changes: 0` with empty warning deltas. It still consumes a provider round-trip and is credited as satisfying `pipeline.diff` ([src/elspeth/web/composer/pipeline_planner.py:470](../../../../src/elspeth/web/composer/pipeline_planner.py#L470)). The planner never applies its candidate before the terminal `emit_pipeline_proposal`, so request-start compared with current carries no information there. The comparison a planner would want (candidate against current) is not what the tool computes.

This is the "baseline equal to current state" anti-pattern that §6 warns against, already present in production. The control `diff_states(s, s)` returns `total_changes 0`, and the positive control (metadata changed) returns 1 (§7). Probe P1 ran the real `plan_pipeline` with a scripted provider over a non-empty state (4 nodes, 1 edge, 1 output, 1 source). It returned `total_changes: 0` both in the tool message sent back to the model and in the audited invocation. A lane-private mutant that binds a different baseline at line 3434 returned `total_changes: 1` from the same probe, so the zero comes from the route binding and the probe can tell the two apart. No live provider was called.

**diff_pipeline-N2 — demonstrated MCP baseline-lifetime defect (probe P2, §7.1).** After a dispatch whose name is `new_session` or `load_session`, the MCP server sets `baseline_ref[0] = new_state` *before* checking `success` ([src/elspeth/composer_mcp/server.py:907](../../../../src/elspeth/composer_mcp/server.py#L907)). A `load_session` that hits `SessionNotFoundError` returns `success: false` with the **current working state** ([src/elspeth/composer_mcp/server.py:573](../../../../src/elspeth/composer_mcp/server.py#L573), [src/elspeth/composer_mcp/server.py:577](../../../../src/elspeth/composer_mcp/server.py#L577)). A mistyped session id therefore silently rebases the baseline to "now", and later diffs omit every edit made before the failed load. The server's comment says the baseline is "captured at session create/load" ([src/elspeth/composer_mcp/server.py:717](../../../../src/elspeth/composer_mcp/server.py#L717)), not on a failed load. Before any session tool runs, the baseline is the empty version-1 initial state ([src/elspeth/composer_mcp/server.py:708](../../../../src/elspeth/composer_mcp/server.py#L708)). That case is *intended but undisclosed*: such a diff reports everything as added.

Probe P2 drove the server's `call_tool` handler and confirmed both behaviours. After `new_session` → `set_metadata` → `load_session` with an unknown id (`success: false`), `diff_pipeline` reported `total_changes: 0` and `from_version: 2`. The same sequence without the failed load reported `total_changes: 1` and `from_version: 1`. A successful load reset the baseline to 0, which is the E1 positive control. With no session tool called, the diff was measured against the empty version-1 state.

**diff_pipeline-N4 — source-established result-shape route inconsistency.** MCP serializes `result.to_dict()` through `_ensure_serializable` ([src/elspeth/composer_mcp/server.py:415](../../../../src/elspeth/composer_mcp/server.py#L415)). It never selects `DIFF_PIPELINE_RESPONSE_CONTRACT`: `grep -rc "admit_discovery\|response_contract" src/elspeth/composer_mcp/` → 0 in all four files, while the control `grep -c` on `tool_batch.py` → 5. The closed `DiffResponse` record (`extra="forbid"`, `sources` presence rule) is therefore enforced on the web routes only. Today `diff_states` happens to produce a conforming shape. A producer drift would reach MCP clients unrefused. The same applies to every MCP discovery tool that declares a response contract.

**diff_pipeline-N5 — demonstrated (probe P3, §7.1).** After a failed freeform diff, `last_validation = result.validation` ([src/elspeth/web/composer/tool_batch.py:3291](../../../../src/elspeth/web/composer/tool_batch.py#L3291)) holds the synthetic `rejected_mutation` "No baseline available…" error and the forced `is_valid: false`. It becomes `prior_validation` for the next call ([src/elspeth/web/composer/tool_batch.py:3023](../../../../src/elspeth/web/composer/tool_batch.py#L3023)). The next successful mutation's `validation_delta.resolved_errors`, keyed by `(component, message)`, then lists the diff refusal as a "resolved" error ([src/elspeth/web/composer/tools/_common.py:716](../../../../src/elspeth/web/composer/tools/_common.py#L716)), even though the mutation did not resolve it. This applies to every saved-graph discovery refusal; GS-01 makes it happen on every freeform diff call.

Probe P3 ran the real freeform compose loop with a scripted provider: `diff_pipeline`, then `set_metadata`. The `set_metadata` tool message sent to the model carried `validation_delta.resolved_errors` = [`rejected_mutation` / `diff_baseline_unavailable` / "No baseline available. Load or create a session first."]. Its standing `no_source_configured` and `no_sinks_configured` errors stayed unresolved, as they should. In the control (`list_models` first, then the same `set_metadata`), `resolved_errors` was `[]`.

**diff_pipeline-N6 — demonstrated description overclaim (incidental to probe P3, §7.1).** The failed freeform envelope the model received in P3 had only the keys `affected_nodes`, `success`, `validation` and `version`. The declaration ends "Without a baseline it fails with `error` and `error_code` only" ([src/elspeth/web/composer/tools/generation.py:4687](../../../../src/elspeth/web/composer/tools/generation.py#L4687)). The failed envelope has no `error` key: the closed ToolResult vocabulary has no `error` among its required or optional keys ([src/elspeth/web/composer/tool_result_envelope.py:25](../../../../src/elspeth/web/composer/tool_result_envelope.py#L25)), and the freeform serializer emits `result.to_dict(include_data=False)` ([src/elspeth/web/composer/discovery_response.py:38](../../../../src/elspeth/web/composer/discovery_response.py#L38)). The refusal is `validation.errors[0]` (`component: rejected_mutation`, `message`, `error_code`), and the standing state errors accompany it. evidence.md shows `no_source_configured` and `no_sinks_configured` riding along, and [tests/unit/web/composer/test_generation_response_contracts.py:206](../../../../tests/unit/web/composer/test_generation_response_contracts.py#L206) pins the absence of `data`. The handler's own comment says the result is "not a success carrying an `error` key" ([src/elspeth/web/composer/tools/generation.py:4662](../../../../src/elspeth/web/composer/tools/generation.py#L4662)).

**diff_pipeline-N7 — source-established misleading refusal and guidance text.** Two texts misdirect:

- **Handler message:** "Load or create a session first." ([src/elspeth/web/composer/tools/generation.py:4667](../../../../src/elspeth/web/composer/tools/generation.py#L4667)).
- **Catalogue explanation:** "the session was created without a loaded baseline state. Do not retry diff_pipeline in this session." ([src/elspeth/web/composer/tools/generation.py:461](../../../../src/elspeth/web/composer/tools/generation.py#L461)).

Both attribute the failure to session lifecycle. On the only route where it occurs (freeform web) the cause is the route's omission, and no session action can fix it. On MCP the condition is unreachable. The "do not retry" advice is correct in effect but for the wrong reason.

**diff_pipeline-N8 — source-established result-semantics inconsistency.** In `total_changes`, every node, edge or output change counts once, but any number of changed sources count as 1 and metadata counts as 1 ([src/elspeth/web/composer/tools/_common.py:1227](../../../../src/elspeth/web/composer/tools/_common.py#L1227)). Warnings are excluded, and validation errors are not compared at all. Neither the description nor the skills define `total_changes`, so a model cannot tell "3 sources replaced" from "1 source tweaked".

**diff_pipeline-N9 — mechanism source-established; impact hypothesis.** The warning delta uses `{e.message}` sets ([src/elspeth/web/composer/tools/_common.py:1221](../../../../src/elspeth/web/composer/tools/_common.py#L1221)). `_compute_validation_delta` keys on `(component, message)` ([src/elspeth/web/composer/tools/_common.py:710](../../../../src/elspeth/web/composer/tools/_common.py#L710)). As a result:

- identical warning text on two components collapses;
- a warning that moves components shows no change.

No concrete misleading case was measured.

**diff_pipeline-N10 — demonstrated, trivial.** The handler's "docstring" follows the first statement, so it is a discarded expression and `_execute_diff_pipeline.__doc__ is None` (measured, §7). It also claims the web composer passes `context.baseline`, which is false for freeform ([src/elspeth/web/composer/tools/generation.py:4648](../../../../src/elspeth/web/composer/tools/generation.py#L4648)).

**diff_pipeline-N11 — source-established stale audit justification.** `_DIFF_PIPELINE_REASON` has three problems ([src/elspeth/web/composer/redaction.py:2821](../../../../src/elspeth/web/composer/redaction.py#L2821)):

- it cites "the JSON schema at tools.py:1323", and `src/elspeth/web/composer/tools.py` does not exist (`ls` → no such file; the module is the `tools/` package);
- it says the diff "reports field names plus before/after summaries", but `diff_states` emits only IDs, booleans, counts and warning-message text;
- it describes the comparison as "versus the session baseline", which is wrong on both web routes.

The data-safety conclusion is not contested, but the recorded reasoning does not describe the code. Warning messages can carry user-authored values such as an `on_success` target; [tests/unit/web/composer/test_tools_discovery.py:453](../../../../tests/unit/web/composer/test_tools_discovery.py#L453) shows a canary in raw feedback, removed by redaction and planner feedback.

**diff_pipeline-N12 — source-established user-facing overclaim (GS-03 class).** The tooltip reads "Compares two pipeline configurations to show what changed between them." ([src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts:37](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L37)). The user picks no configurations, and on the web routes the tool either always fails or always reports nothing.

**diff_pipeline-N13 — hypothesis/unverified.** The baseline and current-state validations come from different sources on freeform (§4). If R1 wires a freeform baseline without also fixing `current_validation` to a catalog validation of the current state, `warnings_introduced`/`warnings_resolved` may report differences that come from the validation source rather than from the graph.

### 5.1 Guidance parity

Instruments: `grep -n -i "diff_pipeline\|diff\b\|baseline\|what changed\|compare"` over both skill files. The control `grep -c preview_pipeline` gave 5 hits in `pipeline_composer.md`. For `pipeline_capabilities.md`, `diff\b` matched line 417, which is itself the known positive. Both skills reach the freeform prompt ([src/elspeth/web/composer/prompts.py:75](../../../../src/elspeth/web/composer/prompts.py#L75)). The capability core is also required as the prefix of the planner's first system message ([src/elspeth/web/composer/capability_skill.py:203](../../../../src/elspeth/web/composer/capability_skill.py#L203)).

- **G1 — guidance pushes toward a tool that cannot answer (source-established).** [pipeline_capabilities.md:417](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L417) says "…authors the proposal through ordinary node/edge tools, then preview and diff it." On freeform the diff always fails (GS-01). On the planner it always reports zero (N1), and the planner has no node or edge tools: `upsert_node` is absent from the planner's emitted list (measured, §7). This is the only workflow instruction mentioning diff in either skill.
- **G2 — inventory without semantics (source-established).** [pipeline_composer.md:341](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L341) lists `diff_pipeline` under "State / preview" (autogen block). Neither skill says what the baseline is, that it differs per route, or that freeform supplies none. Given the route behaviour, the skills should either disclose "freeform: unavailable" or stop listing the tool there until R1 lands.
- **G3 — declaration overclaims.** "the baseline supplied by this surface, which may be the request-start state" holds on no web route in a useful sense: freeform supplies nothing, and the planner's "request-start" equals current. The failure-shape sentence is wrong (N6).
- **G4 — catalogue guidance misattributes the cause (N7).** [generation.py:461](../../../../src/elspeth/web/composer/tools/generation.py#L461) speaks of a session "created without a loaded baseline state", which is MCP vocabulary. It reaches the planner through rejection feedback ([tests/unit/web/composer/test_validation_guidance_catalogue.py:188](../../../../tests/unit/web/composer/test_validation_guidance_catalogue.py#L188)), on a branch the planner cannot actually reach. It does not reach freeform inline, because guidance attachment is mutation-only.
- **G5 — user tooltip overclaims (N12).**
- **No contradiction between the two skills.** Both are simply silent on baseline semantics. No stale reference to removed arguments was found (the schema is the empty object).

## 6. Ownership, remediation and dependencies

The orchestrator context owner should define the baseline semantics for each invocation. It should either supply an immutable request-start baseline to freeform dispatch, or stop advertising the capability where it is intentionally unsupported. Supplying the baseline matches the stated tool purpose, but must specify baseline lifetime across multi-turn authoring and retries.

Do not inject a fabricated baseline equal to current state, which always reports zero. The planner already does exactly that (N1). For the planner, choose between two options:

- remove `diff_pipeline` from `PLANNER_DISCOVERY_TOOL_NAMES`; or
- redefine the planner's comparison as candidate against current, which is a different tool contract.

Either change alters the byte-pinned planner tool list and prompt hashes ([tests/unit/web/composer/test_pipeline_planner.py:188](../../../../tests/unit/web/composer/test_pipeline_planner.py#L188), [tests/unit/web/composer/test_pipeline_planner.py:928](../../../../tests/unit/web/composer/test_pipeline_planner.py#L928)), so it belongs in R1 rather than in a §6.1 patch.

The MCP baseline lifetime (N2) and its missing response-contract admission (N4) belong to the MCP adapter owner. Regression depends on explicit request context before any larger dispatch consolidation.

**Implementation risk:** Medium. Choosing the wrong baseline lifetime creates plausible false comparisons. Require changed/unchanged and inter-turn controls before wiring it.

### 6.1 Simple enhancements

Each item below is bounded and non-architectural. None has been implemented.

| # | What | Where | Why | Acceptance check |
|---|---|---|---|---|
| E1 | Set the MCP baseline only on a successful `new_session`/`load_session` | [server.py:907](../../../../src/elspeth/composer_mcp/server.py#L907) | N2: a failed load silently rebases the diff | Test: mutate, call `load_session` with an unknown id (fails), then `diff_pipeline` still reports the earlier mutation; positive control: a successful load resets to zero |
| E2 | Correct the declaration's failure-shape sentence (refusal is `validation.errors[0]` with `error_code: diff_baseline_unavailable`; no `error` key; standing errors accompany it) and state which routes supply which baseline | [generation.py:4687](../../../../src/elspeth/web/composer/tools/generation.py#L4687) | N6/G3 | Description test asserts no `error` key is promised; wire-schema and planner prompt-byte pins updated deliberately |
| E3 | Replace the refusal message and catalogue explanation with route-neutral text ("this surface supplied no comparison baseline; use get_pipeline_state") | [generation.py:4667](../../../../src/elspeth/web/composer/tools/generation.py#L4667), [generation.py:461](../../../../src/elspeth/web/composer/tools/generation.py#L461) | N7/G4 | Existing `test_fails_closed_without_baseline` / `test_real_diff_without_baseline_retains_planner_guidance` updated; grep finds no "session" in the diff refusal text |
| E4 | Move the docstring above the first statement and drop the false "web composer" claim | [generation.py:4647](../../../../src/elspeth/web/composer/tools/generation.py#L4647) | N10 | `_execute_diff_pipeline.__doc__ is not None` |
| E5 | Fix `_DIFF_PIPELINE_REASON` (no `tools.py:1323`; IDs and warning text, not "field names plus before/after summaries"; route-specific baseline) | [redaction.py:2821](../../../../src/elspeth/web/composer/redaction.py#L2821) | N11 | Text review; the redaction policy tests stay green |
| E6 | Correct the tooltip, e.g. "Compares the current pipeline with this session's comparison baseline, where one is available." | [toolCallDescriptions.ts:37](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L37) | N12 | Frontend description test updated |
| E7 | Remove or qualify "then preview and diff it" until a route supplies a meaningful baseline | [pipeline_capabilities.md:417](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L417) | G1 | Skill-hash pins updated deliberately; grep for `diff` in the skill matches only qualified text |
| E8 | Document `total_changes` (sources and metadata each count 1; warnings excluded) in the declaration, or count sources per name | [_common.py:1227](../../../../src/elspeth/web/composer/tools/_common.py#L1227) | N8 | Test with two changed sources asserts the documented count |
| E9 | Add a service-level freeform regression asserting the current behaviour (refusal) so GS-01's fix has a red-to-green pin; existing tests all inject `baseline=` or call the handler directly (the lane-private probe P3, §7.1, is a working template; it is not in `tests/`) | [tests/unit/web/composer/test_agent_tooling.py:774](../../../../tests/unit/web/composer/test_agent_tooling.py#L774) | GS-01 coverage gap | Scripted freeform turn calling `diff_pipeline` observes `diff_baseline_unavailable` today; turns green only when R1 wires a baseline |

## 7. Evidence, regression checks and limits

**Generation controls.** The controlled probe uses changed metadata and same-state positive and negative controls; raw results are retained in `generation-controls.txt`. Existing contract fixtures explicitly pass a baseline and therefore cannot catch the production omission ([tests/unit/web/composer/test_generation_response_contracts.py:31](../../../../tests/unit/web/composer/test_generation_response_contracts.py#L31)). The no-baseline rejection is pinned at [line 206](../../../../tests/unit/web/composer/test_generation_response_contracts.py#L206).

**Other pinning tests:**

- [tests/unit/web/composer/test_agent_tooling.py:734](../../../../tests/unit/web/composer/test_agent_tooling.py#L734): handler/dispatch/readonly refusal parity.
- [tests/unit/web/composer/test_agent_tooling.py:742](../../../../tests/unit/web/composer/test_agent_tooling.py#L742): successful-diff custody with an injected baseline.
- [tests/unit/web/composer/test_agent_tooling.py:774](../../../../tests/unit/web/composer/test_agent_tooling.py#L774) and [line 784](../../../../tests/unit/web/composer/test_agent_tooling.py#L784): fail-closed and changed-source cases.
- [tests/unit/web/composer/test_validation_guidance_catalogue.py:188](../../../../tests/unit/web/composer/test_validation_guidance_catalogue.py#L188): planner feedback on a no-baseline result.

Every one of these injects or omits the baseline directly. No repository test exercises the freeform callsite, the planner's `baseline=current_state` binding, or the MCP `baseline_ref` lifecycle. The lane-private probes in §7.1 now exercise all three, but they are not in `tests/`. These tests passed in the 370-test selection. The only existing planner test that `-k diff` selects is `test_every_freeform_planner_discovery_tool_returns_admitted_state[diff_pipeline]`. It passed (P1 selection check), and it asserts nothing about `total_changes`. The §7.1 probes are scripted-provider service runs, not HTTP tests.

**Required regression:**

- a fake provider calls diff after a real earlier mutation in the same request;
- assert an accurate result plus a persisted tool audit across fresh-session, existing-session and retry paths;
- add a planner assertion that either `diff_pipeline` is not advertised or its comparison is non-degenerate;
- add an MCP failed-`load_session` baseline test (E1).

**Sheet-review measurements (read-only introspection, worktree `elspeth.__file__` confirmed):**

```text
loop openai_strict diff_pipeline=True upsert_node=True 45
planner openai_strict diff_pipeline=True upsert_node=False 23
loop none diff_pipeline=True upsert_node=True 45
planner none diff_pipeline=True upsert_node=False 23
mcp diff_pipeline=True new_session=True 32
_execute_diff_pipeline.__doc__ -> None
_SESSION_MUTABLE_DISCOVERY_TOOL_NAMES -> ['diff_pipeline', 'get_pipeline_state', 'preview_pipeline']
diff_states(s, s) total_changes 0 warnings_introduced []     # negative control (planner shape)
diff_states(s, s.with_metadata(name)) total_changes 1          # positive control
```

The `diff_states` controls exercise the pure algorithm with catalog-free validation. Taken alone, they establish the planner outcome only together with the source binding at [pipeline_planner.py:3434](../../../../src/elspeth/web/composer/pipeline_planner.py#L3434) and [pipeline_planner.py:4905](../../../../src/elspeth/web/composer/pipeline_planner.py#L4905). Probe P1 (§7.1) has since executed a scripted planner request.

### 7.1 Probe runs (2026-11-09)

These runs used the worktree at `2ac312207`, whose production source is the 0.8.9 baseline. `$WT` is the worktree, `$L` is the lane-private probe directory and `$VENV` is the main checkout's `.venv`, which is never a bare `python`. Probe scripts, logs, scratch stores and `TMPDIR` are all under `$L`. Nothing was written to the worktree except this sheet. Every run printed `elspeth.__file__`, and the path resolved into `$WT/src`, or into `$L/mut-n1/src` for the mutant. No live provider, network, server or Docker was used.

The pytest probes live outside `tests/`, so they load the repository conftests as plugins. Common form (`<probe>`, `<SRC>` substituted):

```bash
cd $WT && export PYTHONPATH=$WT:<SRC>:$WT/elspeth-lints/src TMPDIR=$L/tmp PROBE_EXPECT_SRC_ROOT=<SRC>/ && \
  $VENV/bin/python -m pytest $L/<probe> -c $WT/pyproject.toml --rootdir=$WT \
  -p tests.conftest -p tests.unit.conftest -p tests.unit.web.conftest -p tests.unit.web.composer.conftest \
  -n 0 -p no:cacheprovider -s -o pythonpath="$WT <SRC> $WT/elspeth-lints/src"
```

| Probe | Finding | Outcome | Exit | Observed |
|---|---|---|---|---|
| P1 planner self-diff (`test_probe_n1_planner_self_diff.py`, `<SRC>=$WT/src`) | N1 | **confirmed** | 0 | Real `plan_pipeline` with a scripted `litellm.acompletion`: `diff_pipeline`, then `emit_pipeline_proposal`, over a non-empty state (v4; 4 nodes, 1 edge, 1 output, 1 source). The tool was advertised. Both the model-visible tool message and the audited invocation returned `success: true`, `from_version: 4`, `total_changes: 0`, every change set empty and `metadata_changed: false`. |
| P1 mutant control (same file, `<SRC>=$L/mut-n1/src`; line 3434 → `baseline=current_state.with_metadata({"name": "probe mutant baseline"})`) | N1 | control discriminates | 0 | `from_version: 5`, `metadata_changed: true`, `total_changes: 1`. The baseline zero comes from the route binding, not from the probe. |
| P1 selection check: `pytest tests/unit/web/composer/test_pipeline_planner.py -k diff -n 0` | N1 | inconclusive by design | 0 | 1 passed, 365 deselected. The selected test is `test_every_freeform_planner_discovery_tool_returns_admitted_state[diff_pipeline]`, and it asserts nothing about `total_changes`. |
| P2 MCP failed load (`python $L/probe_n2_mcp_failed_load.py $L/mcp-scratch`, driving `create_server(...).request_handlers[CallToolRequest]`) | N2 | **confirmed** | 0 | A: `new_session` → `set_metadata` → `load_session("deadbeef0000")` (`success: false`, "Session not found") → `diff_pipeline` gives `total_changes 0`, `from_version 2`. B, the positive control without the failed load: `total_changes 1`, `from_version 1`. C, the successful-load control: 1 before the load, 0 after. D, with no session tool: `total_changes 1` against `from_version 1`, so the startup baseline is the empty v1 state. |
| P3 freeform delta (`test_probe_n5_freeform_delta.py`, `<SRC>=$WT/src`; `ComposerServiceImpl.compose` with scripted `litellm.acompletion` and prompt-guard HTTP faked by `httpx.MockTransport`) | N5 (also GS-01, N6) | **confirmed** | 0 | Variant `diff_pipeline` → `set_metadata`: the diff result reached the model as `success: false` with errors `rejected_mutation/diff_baseline_unavailable`, `no_source_configured` and `no_sinks_configured`, and with no `error` key. The `set_metadata` `validation_delta.resolved_errors` was exactly the `diff_baseline_unavailable` entry. Control `list_models` → `set_metadata`: `resolved_errors: []`. |
| P4 exposure re-measure (`python -c` introspection) | §3.1 / N10 | **confirmed** (no change) | 0 | Planner `diff_pipeline` present on `openai_strict` and `none`. MCP present. `_execute_diff_pipeline.__doc__` is `None`. Controls: planner `upsert_node` was absent on both dialects, the MCP name `no_such_tool` was absent, and `create_server.__doc__` was non-None. |

Source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection, selected local tests and the scripted §7.1 probes do not establish live provider behaviour, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

Synchronous kind: `discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.generation._execute_diff_pipeline`](../../../../src/elspeth/web/composer/tools/generation.py#L4642).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | yes |
| Composer MCP | yes |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ, so exposure does not certify execution readiness. For this tool, exposure on the freeform loop and the planner is measured, but neither route yields a non-trivial comparison (§1, §3.1).

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary. Owned argument models and kind-specific or domain guards can impose the further constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {},
  "required": [],
  "additionalProperties": false
}
```
