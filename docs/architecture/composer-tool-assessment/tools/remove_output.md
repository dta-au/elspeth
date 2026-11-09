# `remove_output` — delete one authored sink instance

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only. **Overall confidence: High for source and focused mutation tests.** The 2026-11-09 sheet review added in-memory dispatch probes. The follow-up probe run (§7.1) added an `explicit_approve` compose-loop service run with a scripted LLM double and a fail-closed provider guard, plus the proposal-accept REST route on a test app. None of this is a live-provider, browser or PostgreSQL run.

## 1. Purpose and usage

Select this tool when a named sink instance should be removed from the authored pipeline. It is a graph edit, not deletion of produced files, blobs, remote records, or audit evidence. It is appropriate during route restructuring, after inspecting references to the sink's connection name. Available through web Composer and common Composer MCP dispatch. The sink must exist in the current composition; the handler has no external service prerequisites.

## 2. Contract

The only argument is required `sink_name: string`; extra arguments are forbidden. Existing output removal returns a successful mutation with incremented composition version and `affected_nodes=(sink_name,)`. On success the applied-component echo is **always** absent: [`_applied_component_echo`](../../../../src/elspeth/web/composer/tools/_common.py#L4088) resolves `affected` against the new state, the removed sink is no longer there, so it returns `None` (measured in-memory: `applied_component=None` for every removal). Current validation and `validation_delta` describe the surviving graph. Missing output returns `output_not_found`, with the untouched prior state and `rejected_component="output:<sink_name>"`. The empty string and whitespace-only names are admitted by both the JSON schema and the argument model and then refused as `output_not_found` (`rejected_component="output:"`; in-memory dispatch). Missing, mistyped, or extra arguments raise `ToolArgumentError` (`schema_shape`) before the handler.

Removal does not cascade into anything that names the sink. It leaves source and node `on_success`, `on_error`, gate `routes`/`fork_to`, sibling sinks' `on_write_failure`, sources' `on_validation_failure`, **and visual edges whose `to_node` is the sink** in place. Each one shows up as a validation finding on the surviving graph. Measured codes, in-memory dispatch over the `_wardline_state` fixture plus added outputs and edges:

| Removed sink was… | New error codes in `validation_delta.new_errors` |
|---|---|
| a transform's `on_success` target, with a drawn edge | `transform_on_success_dangling`, `edge_unknown_node` |
| the shared `on_error` target of two transforms | `transform_on_error_unknown_sink` ×2, `duplicate_connection_producer` |
| the shared `on_success` target of two transforms | `transform_on_success_dangling`, `duplicate_connection_producer`, `edge_unknown_node` |
| a source `on_validation_failure` target | `quarantine_unknown_output` |
| another sink's `on_write_failure` target | `failsink_unknown_output` |
| the last sink | `no_sinks_configured` |

Removing a sink can also turn its own failsink into an unreferenced-output *warning*. No automatic replacement sink or guessed route is generated. The handler does not inspect a sink plugin schema, consult plugin policy, or instantiate the plugin, so removing a configuration stays possible even when its implementation is unavailable or policy-forbidden.

This is uncached `MUTATION` with graph/validation/YAML effects ([`resolve_effects`](../../../../src/elspeth/web/composer/tools/declarations.py#L188)). Web auto-commit applies an admitted edit. Under explicit approval the edit becomes a proposal (see §3.1 and §4: the proposal is created even when the in-memory preview already failed). A successful call changes version, but a second identical call fails as missing-output, so the tool makes no idempotent-delete guarantee. Calls are serialized per session by the compose lock ([async worker](../../../../src/elspeth/web/sessions/composer_async_worker.py#L464), [proposal accept](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L298)) and session operation authority, not by local mutable global state. The orchestrator must preserve returned state/outcome across later failures and must not infer external artifact deletion.

## 3. Implementation trace

The [argument model](../../../../src/elspeth/web/composer/tools/outputs.py#L58), [handler wrapper](../../../../src/elspeth/web/composer/tools/outputs.py#L113) and [declaration](../../../../src/elspeth/web/composer/tools/outputs.py#L121) register through [_registry](../../../../src/elspeth/web/composer/tools/_registry.py#L100) (the `_OUTPUTS_TOOLS_IN_MODULE` splat; the [mutation handler map](../../../../src/elspeth/web/composer/tools/_registry.py#L148) is derived from it) and [get_tool_definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311). [`_execute_remove_output`](../../../../src/elspeth/web/composer/tools/outputs.py#L225) discards its context, admits the model, calls [`CompositionState.without_output`](../../../../src/elspeth/web/composer/state.py#L8290) (outputs tuple only; compare [`without_node`](../../../../src/elspeth/web/composer/state.py#L8250), which also drops edges on the removed node), and returns either typed saved-graph rejection or [`_mutation_result`](../../../../src/elspeth/web/composer/tools/_common.py#L1712). Unlike [`set_output`](../../../../src/elspeth/web/composer/tools/outputs.py#L218) it runs no `_post_mutation_invariant_error` (removal cannot introduce any of those codes). Unlike [`remove_node`](../../../../src/elspeth/web/composer/tools/transforms.py#L1642) it does not call review reconciliation.

[Public dispatch](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) checks credential material, catalog snapshot, closed schema and the [interpretation gate](../../../../src/elspeth/web/composer/tools/_dispatch.py#L772). [Finalization](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752) preserves prior validation, normalizes surviving graph diagnostics and attaches `validation_guidance` on failure. [Web tool-batch execution](../../../../src/elspeth/web/composer/tool_batch.py#L3008) and approval orchestration own persistence/publication; [audit](../../../../src/elspeth/web/composer/audit.py#L812) records semantic and dispatch outcomes; [frontend cards](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L74) present pending/applied/failed outcomes. [MCP dispatch](../../../../src/elspeth/composer_mcp/server.py#L363) uses the same mutation with its own session checkout/persistence authority.

### 3.1 Route inventory

Instrument: `grep -rn "remove_output" src/` (19 hits across 9 files, including the known positives `outputs.py:122` and `pipeline_composer.md:342`), plus reading each caller of `execute_tool`. A tutorial grep over `outputs.py`, `tool_batch.py`, `batch_dependencies.py`, `proposals.py` (route) and `composer_mcp/server.py` matched nothing. The same grep matched 2 lines in `_common.py`, so the instrument works. **No tutorial-specific branch exists on any route.**

| Route | Entry | Gate / policy | Notes |
|---|---|---|---|
| Canonical registry/emitter | [`_registry`](../../../../src/elspeth/web/composer/tools/_registry.py#L100) → [`get_tool_definitions`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311) | Kind `MUTATION`; not cacheable | Description and schema are pinned verbatim by [`test_remove_output`](../../../../tests/unit/web/composer/test_tool_declarations.py#L689) |
| Freeform loop advertisement | [`composer_loop_tool_definitions`](../../../../src/elspeth/web/composer/provider_gateway.py#L391) via `wire_tool_definitions` | Both dialects; no phase filter | — |
| Planner | [`planner_tool_definitions`](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647) | Excluded: planner advertises a discovery-name allowlist only | Not reachable from the planner |
| Recovery controls | `recovery_controls.py` | — | No reference (grep) |
| REST ingress | [`POST /{session_id}/messages`](../../../../src/elspeth/web/sessions/routes/messages.py#L46), [`POST /{session_id}/recompose`](../../../../src/elspeth/web/sessions/routes/composer/compose.py#L16) (both 202) → async worker under the session compose lock → tool batch | Authenticated pipeline user; rate limiter | The only way the web LLM reaches the tool |
| Batch prerequisite refusal | [`referenced_resources`](../../../../src/elspeth/web/composer/batch_dependencies.py#L119), [`tool_batch`](../../../../src/elspeth/web/composer/tool_batch.py#L1551) | `batch_dependency_refused` when an earlier call in the same batch failed and promised that output (e.g. failed `set_output(X)`) | Removal itself promises nothing, so a later same-batch call naming the removed sink is not dependency-refused. It fails or dangles on its own merits |
| Auto-commit dispatch | [`tool_batch`](../../../../src/elspeth/web/composer/tool_batch.py#L3008) | `trust_mode != explicit_approve`; `validate_arguments`, `raise_schema_argument_errors` | If the removal leaves the graph valid, [`_finalize_completed_incremental_mutation`](../../../../src/elspeth/web/composer/tool_batch.py#L3040) wires deployment-required controls and merges their components into `affected_nodes` |
| Explicit-approval proposal | [preview `execute_tool`](../../../../src/elspeth/web/composer/tool_batch.py#L2057), [envelope](../../../../src/elspeth/web/composer/tool_batch.py#L2222) | [`explicit_approval_required`](../../../../src/elspeth/web/composer/tool_batch.py#L1728); per-turn pending-proposal cap | Preview runs on current state. A failed preview is ignored and the proposal is still created (§4; demonstrated, §7.1 probe 1). A preview that becomes valid and gains required controls is [rewritten into a `set_pipeline` proposal](../../../../src/elspeth/web/composer/tool_batch.py#L2108) |
| Proposal summary | [`build_tool_proposal_summary`](../../../../src/elspeth/web/composer/proposals.py#L122) | — | Generic fallback `"Apply composer tool remove_output."`; no sink named (in-memory call; also on the persisted proposal row and in the model's `data.summary`, §7.1 probe 1). `patch_output_options` gets `Update options for output "<name>".` |
| Proposal accept / reject | [`accept`](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L286) re-runs [`execute_tool`](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L491); reject at L720 | Compose lock; [`proposal_base_state_changed` 409](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L416); handler refusal → [auto-reject + 422](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L563) (demonstrated for an absent sink, §7.1 probe 1; the 409 arm was not run) | Required-control finalization repeats after a valid accept |
| Composer MCP | [`_COMPOSER_TOOL_NAMES`](../../../../src/elspeth/composer_mcp/server.py#L117), [`_dispatch_tool`](../../../../src/elspeth/composer_mcp/server.py#L363) | Schema admission; file-sink collision control (not applicable to removal); `data_dir=None` (unused by this handler) | No approval mode, no required-control finalization; response is `to_dict()` plus full `state` |
| Redaction / audit | [`ToolRedaction`](../../../../src/elspeth/web/composer/redaction.py#L3929), [reason](../../../../src/elspeth/web/composer/redaction.py#L2908), [argument-name allowlist](../../../../src/elspeth/web/composer/protocol.py#L964) | Unknown argument keys replaced by sentinel | — |
| Frontend | [`ProposalDiff` arm](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx#L646), [description](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L74), `ToolCallCard` | Pending, non-stale, unchanged base only | When the sink is already absent the arm returns `[]`, which [renders](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx#L752) "No difference from the current pipeline." Applied rows use the server outcome stamp from `GET /messages` |

Direct internal callers outside dispatch: none in `src/`. Tests call it through `execute_tool` ([mutations](../../../../tests/unit/web/composer/test_tools_mutations.py#L2576), [preview](../../../../tests/unit/web/composer/tools/test_tools_preview.py#L4489), [integration](../../../../tests/integration/web/composer/test_colour_multi_query_contract_repair.py#L373)) and as a scripted compose-loop call ([guard audit worker](../../../../tests/unit/web/sessions/test_grouped_guard_audit_worker.py#L176)).

## 4. Alternative paths

Removing an existing terminal output changes only the outputs tuple. Existing producer routes and drawn edges remain for the model to inspect and repair. A missing sink fails cleanly rather than silently claiming a deletion. Whole-graph or interpretation constraints can still reject publication through the shared dispatcher. Under explicit approval the proposed action is not applied until accepted against the unchanged base state. **However, the explicit-approval path does not use its own preview refusal.** The incremental preview's only acted-on outcomes are success-and-valid (required-control finalization) and an exception. [`except ToolArgumentError: pass`](../../../../src/elspeth/web/composer/tool_batch.py#L2114) is documented as intended deferral for argument errors. A `success=False` handler refusal such as `output_not_found` falls through without comment, and a proposal is created anyway. The model receives `success=True`, `APPROVAL_REQUIRED` (validation of the *prior* state, no delta), and the skill tells it to stop. The human sees a generic summary and "No difference from the current pipeline.". Because `base_state_id` pins the same state, Accept deterministically re-runs the refusal and returns the 422 auto-reject (demonstrated, §7.1 probe 1; see §5 `remove_output-N2`). Subsequent validation failure does not roll back previously accepted unrelated edits; it reports the draft's actual state.

## 5. Usability assessment

**Demonstrated (in-memory dispatch) — `remove_output-N1`, edge-cascade inconsistency.** `remove_output` leaves visual edges that target the removed sink, and each becomes a high-severity `edge_unknown_node` error. Positive control on the same state: `remove_node explode` dropped edge `e_sink` while `remove_output errors` kept edge `e_err`. The §7.1 probe 2 re-run added a negative control: the same removal with no drawn edges reports no `edge_unknown_node`. The repo already treats stale sink edges as server-side reconciliation, not authoring ([`without_node`](../../../../src/elspeth/web/composer/state.py#L8250), [`_reconcile_node_sink_mirror_edges`](../../../../src/elspeth/web/composer/tools/transforms.py#L1021)). The model must spend one `remove_edge` per stale edge, and the dispatched guidance for that code says so ("drop the stale edge").

**Demonstrated (in-memory dispatch) — `remove_output-N3`, misleading secondary diagnostic.** When the removed sink was shared by two producers (both `on_error`, or both `on_success`), validation also emits `duplicate_connection_producer`. Re-run in §7.1 probe 2: the code appears when the removed sink was the shared `on_error` target. It does not appear for the single-producer `sink`, or for an unreferenced sink, which produces no new errors at all. Its explanation ([generation.py](../../../../src/elspeth/web/composer/tools/generation.py#L1249)) tells the model to rename a producer's `on_success` or add a coalesce. The real repair is to restore the sink, or re-point the route at an existing sink or `discard`. This belongs to the validator (a non-sink route value treated as a connection), not to the handler.

**Demonstrated (compose turn + accept route, §7.1 probe 1) — `remove_output-N2`, explicit-approval dead end.** See §4. On an `explicit_approve` session the in-turn preview of `remove_output {"sink_name":"absent"}` returned `success=False` with `output_not_found`. One pending proposal was created anyway. The model's tool message was the same as for removing an existing sink: `success=true`, `APPROVAL_REQUIRED`, the prior state's validation, and no `validation_delta`. Accepting such a proposal, with `base_state_id` pinned to the seeded state, returned 422 `proposal_validation_failed` and auto-rejected it. Removing an existing sink was accepted with 200 under the same conditions. It is cross-cutting: every incremental mutation shares the preview branch. Whether deferring a handler refusal is intended is not stated anywhere; only argument-error deferral is commented.

**Demonstrated (compose turn, §7.1 probe 1) — `remove_output-N4`, generic proposal summary.** The summary does not name the sink being deleted (§3.1 row). The persisted proposal row and the model's `data.summary` both read `Apply composer tool remove_output.` for both the existing-sink and the absent-sink call.

**Source-established, intended consequence — `remove_output-N5`.** A removal that leaves the graph valid triggers required-control insertion. Under auto-commit, `affected_nodes` then grows beyond `(sink_name,)`. Under explicit approval, the user is shown a whole-pipeline `set_pipeline` proposal instead of a removal. Neither the description nor the skill discloses this.

**Demonstrated, minor — `remove_output-N6`.** Empty and whitespace `sink_name` pass both schema and model and reach the handler as `output_not_found`. This is harmless but an avoidable round trip.

**Hypothesis / consistency observation — `remove_output-N7`.** `remove_node`, `remove_edge` and `upsert_edge` call `_reconcile_graph_mutation_reviews`; `remove_output` does not. No drift is shown: `reconcile_authoritative_reviews` reconciles source and node options only and builds its stream graph from nodes. A grep for `outputs` in `interpretation_state.py` matched only a docstring. Not a defect unless a review hash is later shown to bind sink identity.

**Intended restriction/editor behaviour.** Removal does not repair scalar consumer/producer routes (`on_success`, `on_error`, `routes`, `fork_to`, `on_write_failure`, `on_validation_failure`), and successful authoring does not imply a runnable graph. Automatic re-pointing of those routes would violate planner ownership and should not be proposed. Dropping edges whose endpoint no longer exists is not re-pointing (N1). After a removal, `validation_delta.new_errors` already lists every dangling reference by component (the post-hoc impact report §6 asks for exists in auto-commit). It is missing under explicit approval (N2).

Workaround: inspect routes and validation before/after removing, then author explicit model-chosen repairs, including `remove_edge` for stale edges. If a completion/result was lost, inspect current state before replaying; missing-output on retry can describe an already applied prior operation rather than a new impossible request.

### 5.1 Guidance parity

Instrument: `grep -n -i` over both skill files for `remove_output`, `output_not_found`, `edge_unknown_node`, `failsink_unknown_output`, `quarantine_unknown_output`, `unreferenced`, `idempot`, and sink-removal phrasing. Control: `remove_output` matches [pipeline_composer.md:342](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L342), the known autogen inventory line.

- **Only mention is the autogen inventory.** `pipeline_composer.md` lists the name at L342; `pipeline_capabilities.md` has zero matches. No skill text says what removal leaves behind. A one-line procedure belongs in the topology guidance: re-point or remove each reference, then drop stale edges. [pipeline_capabilities.md:97-100](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L97) already defines connection strings and is the natural place.
- **Declaration under-discloses.** `"Remove a pipeline output (sink) by name."` / `"Sink name to remove."` does not say that routes, failsink/quarantine references and drawn edges survive as validation errors, that the result carries no applied-component echo, or that a repeat call fails `output_not_found`.
- **Consistent, good:** [pipeline_composer.md:271-276](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L271), [L301-304](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L301) and the L1443 table row steer the model *away* from removing a sink to silence routing errors. `output_not_found` repair text is delivered at dispatch by [`DirectValidationGuidance`](../../../../src/elspeth/web/composer/tools/generation.py#L1756) ("do not create or remove a different destination"), so its absence from the skills is acceptable.
- **Wrong severity word (cross-cutting, minor):** [pipeline_composer.md:307](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L307) and [L1368-1369](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L1368) call unreferenced outputs "errors". The validator emits them as medium **warnings** ([state.py W1](../../../../src/elspeth/web/composer/state.py#L9765)). Removing a sink can create exactly this warning on its former failsink.
- **Approval guidance meets N2:** the skill's `APPROVAL_REQUIRED` rule ("tell the user it awaits approval and stop", [L462-464](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L462)) is correct for the envelope. Combined with N2, though, it makes the model announce a doomed removal as pending.
- **Misleading code guidance (N3):** the `duplicate_connection_producer` explanation steers toward a topology rewrite when a removed shared sink is the cause.

## 6. Structural remediation

Make edit outcome, readiness, and retry semantics explicit in a common mutation contract. Before destructive graph edits, give the model reference-impact information derived from the graph authority, and leave repair decisions with the model. The post-hoc half already exists as `validation_delta`; the gap is pre-approval and replay. A shared state/effect receipt should distinguish never-applied, applied, and replayed removal without making missing targets silently successful by default. The explicit-approval preview should feed its handler-domain refusal back to the model instead of minting a proposal (cross-cutting R1/R4; see N2).

Acceptance: valid removal deletes exactly the named output and leaves other components byte-equivalent. Upstream scalar references are reported accurately and never rewritten server-side. Absent target and malformed arguments reject. Explicit approval protects the exact current candidate and never publishes a proposal whose preview already refused. Replay and concurrent edit scenarios have truthful outcomes, and graph-invalid drafts cannot execute. Compatibility risk is Low for description/impact additions and Medium for changed missing-target/idempotency semantics. Preserve existing graph and artifact retention boundaries.

### 6.1 Simple enhancements

1. **Cascade stale edges (N1).** What: in [`without_output`](../../../../src/elspeth/web/composer/state.py#L8290) also drop edges whose `to_node == output_name`. This matches [`without_node`](../../../../src/elspeth/web/composer/state.py#L8256), and scalar routes stay untouched. Why: removes an `edge_unknown_node` error the model must otherwise clear by hand; the edge is a visual annotation of a now-absent endpoint. Acceptance: on a state with `explode.on_success="sink"` and edge `e_sink→sink`, removal leaves no edge to `sink` and no `edge_unknown_node` in `validation_delta.new_errors`, while `transform_on_success_dangling` is still reported and `explode.on_success` is unchanged. Check the SG-01 sheets (`upsert_edge`, `remove_edge`) for agreement.
2. **Disclose what remains (description).** What: extend the [declaration description](../../../../src/elspeth/web/composer/tools/outputs.py#L125) to say that routes/failsink/quarantine references are not rewritten and appear in `validation_delta`, and that a repeat call returns `output_not_found`. Also mention edges if (1) is not adopted. Why: the declaration is the only tool-specific guidance the model gets. Acceptance: update the verbatim pin [`test_remove_output`](../../../../tests/unit/web/composer/test_tool_declarations.py#L689) together with the change.
3. **Name the sink in the proposal summary (N4).** What: add a `remove_output` branch in [`build_tool_proposal_summary`](../../../../src/elspeth/web/composer/proposals.py#L37) mirroring `patch_output_options`, e.g. `Remove output "<sink_name>".` Why: the human approves a destructive edit, and the model's `APPROVAL_REQUIRED.summary` should identify it. Acceptance: unit test that the summary contains the redacted `sink_name` and falls back to "selected output" when it is absent.
4. **Fail fast on refused incremental previews under explicit approval (N2).** What: in [tool_batch](../../../../src/elspeth/web/composer/tool_batch.py#L2089), when `preview_result.success` is False, route the refusal through the canonical outcome path. Do not create a proposal: return the refusal itself or a `PREVALIDATION_REJECTED`-shaped envelope, following the `set_pipeline` precedent at [L2003](../../../../src/elspeth/web/composer/tool_batch.py#L2003). Why: the refusal is deterministic against the pinned base, and today it surfaces only as a 422 after a human click. Acceptance: with `trust_mode="explicit_approve"` and an absent sink, no proposal row is written, the model sees `output_not_found` with `affected_nodes=()`, and a present-sink removal still produces a pending proposal. Cross-cutting: it changes every incremental mutation's approval behaviour, so confirm intent with the maintainer first.
5. **Reject empty names at admission (N6).** What: `min_length=1` (or a stripped non-empty validator) on [`_RemoveOutputArgumentsModel.sink_name`](../../../../src/elspeth/web/composer/tools/outputs.py#L58) and `minLength: 1` in the schema. Why: saves a round trip and yields an argument error instead of a domain refusal naming `output:`. Acceptance: `{"sink_name": ""}` raises `ToolArgumentError`; the declaration pin is updated.
6. **Missing tests.** Add tests that (a) a removal with a drawn edge reports exactly the expected new codes (pins N1 either way), (b) the explicit-approval absent-sink case (pins N2's chosen behaviour), and (c) `ProposalDiff` for `remove_output` with an absent sink. No frontend test covers that empty-arm case today; the closest are the replacement-scope and present-sink tests at [ProposalDiff.test.tsx:78](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.test.tsx#L78) and [L206](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.test.tsx#L206).

## 7. Evidence, gaps, and caveats

Read [mutation success/version sequence](../../../../tests/unit/web/composer/test_output_rejection_contracts.py#L322), [saved-findings rejection tests](../../../../tests/unit/web/composer/test_output_rejection_contracts.py#L91), [`TestRemoveOutput`](../../../../tests/unit/web/composer/test_tools_mutations.py#L2576), and state/dispatch code. Included in the seven-file focused selection: exit 0, **448 passed, 43 warnings, 66.95s**. The 2026-11-09 sheet review added in-memory `execute_tool` probes against the real catalog service with the trained-operator policy view: the reference-code table in §2, the edge-cascade control, the second-call refusal, empty-name admission and the proposal summary. They were not end-to-end service runs, and no test suite was run for the review. The follow-up probe run (§7.1) exercised N2 and N4 through an `explicit_approve` compose turn and the accept route, and re-ran the N1/N3 in-memory probe with negative controls. No external artifact deletion, browser, live provider, PostgreSQL, or concurrency experiment ran for this tool. The impact-query/receipt design remains proposed; noncascading *scalar-route* deletion is not classified as a defect. Nothing in §6/§6.1 has been implemented.

### 7.1 Probe run (2026-11-09)

`$WT` is the assessment worktree. `$L` is the lane-private probe directory `.claude/lanes/composer-sheet-review-20261109/probes/remove_output` under the main checkout, and it holds the probe files, logs and `TMPDIR`. Nothing was written into the worktree except this sheet.

Every run carried the fail-closed prefix `HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 NO_PROXY= LITELLM_LOCAL_MODEL_COST_MAP=True ELSPETH_DISABLE_NETWORK=1`. Each probe first installs `$L/ro_guard.py`, which makes five entry points raise: `litellm.acompletion`, `litellm.completion`, `provider_gateway._litellm_acompletion`, `pipeline_planner._litellm_acompletion` and IPv4/IPv6 `socket.connect`. The advisor and `planning_application` reach the gateway alias by attribute, so the guard covers them too. Before any probe ran, a positive control showed all five raise when called unscripted. Each probe then asserts the guard was never hit, and both logs record zero hits. Both logs show `elspeth.__file__` resolving to `$WT/src/elspeth/__init__.py`.

**Probe 1, N2 and N4: confirmed.** File `$L/test_probe1_n2_explicit_approve.py`. Exit 0 (5 passed), log `$L/probe1.log`. Command:

`cd $WT && PYTHONPATH=$WT:$WT/src:$WT/elspeth-lints/src TMPDIR=$L/tmp python -m pytest $L/test_probe1_n2_explicit_approve.py -s -n 0 -p no:cacheprovider -c $WT/pyproject.toml --rootdir $WT -o pythonpath="$WT $WT/src $WT/elspeth-lints/src" -p tests.conftest -p tests.integration.conftest -p tests.integration.web.conftest`

The `-p` list loads the autouse fixtures, including `_fenced_compose_for_legacy_tests` from `tests/integration/web/conftest.py`.

Part A is a real `ComposerServiceImpl.compose` turn on an `explicit_approve` session, using the `_harness` from `test_freeform_proposal_prevalidation.py` and a scripted `_call_llm`. The base state is the harness's incremental base plus a json sink `main`. A spy on `tool_batch.execute_tool` records the preview result.

| Arm | Preview result | Proposals | Model sees |
|---|---|---|---|
| `remove_output main` (positive control) | `success=True` | 1 pending, summary `Apply composer tool remove_output.` | `success=true`, `APPROVAL_REQUIRED`, errors `[source_on_success_dangling]`, no `validation_delta` |
| `remove_output absent` (probe) | `success=False`, `output_not_found` | 1 pending, same summary | identical to the positive control |
| `set_pipeline` with an uninstalled plugin (negative control) | not previewed | 0 | `success=false`, `PREVALIDATION_REJECTED`, `plugin_not_installed` |

The errors the model sees in both `remove_output` arms are the *prior* state's (`source_on_success_dangling` comes from the base source). The absent-sink refusal does not reach the model. The session state stayed at version 1 with `main` in every arm.

Part B uses the accept route on `tests/unit/web/sessions/test_routes._make_app`, with a session seeded with the same state. The proposal's `base_state_id` is pinned to the seeded state record.
- Accepting `remove_output {"sink_name":"absent"}` returned **422** `proposal_validation_failed`, with `rejected_mutation: Output 'absent' not found.` The proposal became `rejected` (auto-reject), and the current state was the unchanged seed.
- Control: accepting `remove_output {"sink_name":"main"}` returned 200 `committed`. The new current state had `outputs=[]`. This is consistent with authoring semantics, where accept does not gate on graph validity; it is not a new finding.

Limits:
- Part A proposals carry `base_state_id=None`, because the harness passes an unpersisted state. The pinned-base claim in §4 is therefore shown only by Part B, where the pin was set by hand.
- The 409 `proposal_base_state_changed` arm was not run.
- The Part B proposal was inserted directly, not carried over from a Part A turn.

**Probe 2, N1 and N3 re-run: confirmed.** File `$L/probe2_n1_n3_inmemory.py`. Exit 0, log `$L/probe2.log`. Command:

`cd $WT && PYTHONPATH=$WT:$WT/src:$WT/elspeth-lints/src TMPDIR=$L/tmp python $L/probe2_n1_n3_inmemory.py`

The script runs in-memory `execute_tool` on `_wardline_state` with the real catalog under the trained-operator policy view, plus edges `e_sink` (explode→sink) and `e_err` (scrape→errors). New error codes come from `validation_delta.new_errors`.

| Arm | Edges after | New error codes | `applied_component` |
|---|---|---|---|
| `remove_node explode` (positive control for edge cascade) | `[e_err]` | `transform_on_success_dangling` | populated echo |
| `remove_output errors` (probe) | `[e_err, e_sink]` | `duplicate_connection_producer`, `edge_unknown_node`, `transform_on_error_unknown_sink` ×2 | `None` |
| `remove_output sink` (probe) | `[e_err, e_sink]` | `edge_unknown_node`, `transform_on_success_dangling` | `None` |
| `remove_output errors`, no drawn edges (negative control for N1) | `[]` | `duplicate_connection_producer`, `transform_on_error_unknown_sink` ×2 | `None` |
| `remove_output spare`, unreferenced (negative control) | `[]` | none | `None` |

This reproduces the §2 table rows and the §2 claim that the applied-component echo is always absent on a successful removal.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.outputs._handle_remove_output`](../../../../src/elspeth/web/composer/tools/outputs.py#L113).

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
    "sink_name": {
      "type": "string",
      "description": "Sink name to remove."
    }
  },
  "required": [
    "sink_name"
  ],
  "additionalProperties": false
}
```
