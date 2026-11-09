# `set_pipeline` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Atomically author a full pipeline replacement, including source(s), nodes, display edges, outputs and metadata. It is not a patch: omitted prior elements are not implicitly retained. The outer web tool accepts a pipeline envelope; internal canonical dispatch and MCP use the flat document. It is a mutation tool with argument-dependent blob-store effects for inline source content.

## 2. Contracts and authority

Exactly one non-null source or named sources map is required, plus nodes, edges and outputs arrays. Failure-routing choices are explicit; metadata defaults are freshly constructed rather than inherited. Node connection fields are runtime wiring authority; edges are advisory display records. Raw credentials, invented source custody and user-authored resolved review metadata are prohibited. Input shape, per-component semantic acceptance, whole-graph validity, candidate acceptability and committed publication are distinct states ([src/elspeth/web/composer/tools/sessions.py:870](../../../../src/elspeth/web/composer/tools/sessions.py#L870), [src/elspeth/web/composer/tools/sessions.py:2113](../../../../src/elspeth/web/composer/tools/sessions.py#L2113), [src/elspeth/web/composer/tools/sessions.py:2355](../../../../src/elspeth/web/composer/tools/sessions.py#L2355)).

`SetPipelineCandidate.acceptable` requires both success and valid validation ([src/elspeth/web/composer/tools/sessions.py:594](../../../../src/elspeth/web/composer/tools/sessions.py#L594)); direct handler success can describe an incomplete authored draft. Whether such a draft is *applied* depends on the route: the freeform `auto_commit` loop and Composer MCP adopt it, while the freeform `explicit_approve` loop and the planner terminal refuse anything that is not `acceptable` (§3.1). The handler creates version+1 in memory, while proposal/operation writers own persistent publication. Inline creation has additional durable blob custody; recovery cannot be assumed to undo it.

Omitted or `null` `metadata` is not "keep current metadata": the builder constructs `PipelineMetadata()` defaults ([sessions.py:1951](../../../../src/elspeth/web/composer/tools/sessions.py#L1951)), measured as `name='Untitled Pipeline', description=''` on a state that previously carried other metadata (in-memory dispatch, review 2026-11-09). The `metadata` argument description says only `{name?, description?}` and does not disclose the reset ([sessions.py:2411](../../../../src/elspeth/web/composer/tools/sessions.py#L2411)).

## 3. Source trace to the user

1. Exposure and wire decoding: canonical declaration → web pipeline envelope → closed semantic admission ([src/elspeth/web/composer/tools/wire_projection.py:1](../../../../src/elspeth/web/composer/tools/wire_projection.py#L1), [src/elspeth/web/composer/tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399)).
2. Candidate construction: argument model → explicit failure choices → policy/plugin admission → source/path/blob ownership → node option/review/expression/barrier checks → output secret/path/file-collision checks ([src/elspeth/web/composer/tools/sessions.py:652](../../../../src/elspeth/web/composer/tools/sessions.py#L652), [src/elspeth/web/composer/tools/sessions.py:999](../../../../src/elspeth/web/composer/tools/sessions.py#L999), [src/elspeth/web/composer/tools/sessions.py:1275](../../../../src/elspeth/web/composer/tools/sessions.py#L1275), [src/elspeth/web/composer/tools/sessions.py:1495](../../../../src/elspeth/web/composer/tools/sessions.py#L1495), [src/elspeth/web/composer/tools/sessions.py:1720](../../../../src/elspeth/web/composer/tools/sessions.py#L1720)).
3. Construct immutable specs and version+1 state; validate structural invariants and reconcile authoritative reviews; reject unknown/forged review bindings ([src/elspeth/web/composer/tools/sessions.py:1855](../../../../src/elspeth/web/composer/tools/sessions.py#L1855), [src/elspeth/web/composer/tools/sessions.py:1959](../../../../src/elspeth/web/composer/tools/sessions.py#L1959)).
4. Direct dispatch persists prepared inline blob bytes only after a `success` candidate (valid **or** authored-invalid) and reports quota failure ([src/elspeth/web/composer/tools/sessions.py:2057](../../../../src/elspeth/web/composer/tools/sessions.py#L2057), [sessions.py:2078](../../../../src/elspeth/web/composer/tools/sessions.py#L2078)); `_prepare_blob_create` itself writes nothing and allocates a fresh `uuid4` blob id on every build ([src/elspeth/web/composer/tools/blobs.py:1380](../../../../src/elspeth/web/composer/tools/blobs.py#L1380)). The freeform path can prevalidate/finalize a candidate before explicit proposal custody or automatic application ([src/elspeth/web/composer/tool_batch.py:1748](../../../../src/elspeth/web/composer/tool_batch.py#L1748), [src/elspeth/web/composer/tool_batch.py:1882](../../../../src/elspeth/web/composer/tool_batch.py#L1882)).
5. Settlement has three distinct paths (corrected 2026-11-09; the earlier text attributed auto-commit intent to this tool):
   - **Freeform `auto_commit`** (`trust_mode != "explicit_approve"`, [tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728)): finalize, then `_do_dispatch` → `execute_tool`, which rebuilds the candidate, persists any inline blob, and the loop adopts `result.updated_state` whenever no prevalidated rejection exists, including an invalid draft ([tool_batch.py:3004](../../../../src/elspeth/web/composer/tool_batch.py#L3004), [tool_batch.py:3290](../../../../src/elspeth/web/composer/tool_batch.py#L3290)). No `pipeline_commit_intent` is involved.
   - **Freeform `explicit_approve`**: an `acceptable` candidate becomes a `PipelineProposal` with a base binding ([tool_batch.py:2142](../../../../src/elspeth/web/composer/tool_batch.py#L2142)); an unacceptable one returns `PREVALIDATION_REJECTED` and is not applied ([tool_batch.py:2003](../../../../src/elspeth/web/composer/tool_batch.py#L2003)). Accept revalidates exact canonical authority via `prepare_pipeline_proposal_commit` ([src/elspeth/web/composer/pipeline_commit.py:364](../../../../src/elspeth/web/composer/pipeline_commit.py#L364), [src/elspeth/web/sessions/routes/composer/proposals.py:347](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L347)).
   - **Planner terminal**: `emit_pipeline_proposal` produces a set_pipeline-shaped `PipelinePlanResult`; `pipeline_commit_intent` is created only there ([src/elspeth/web/composer/planning_application.py:890](../../../../src/elspeth/web/composer/planning_application.py#L890)) and settled through the shared route owner ([src/elspeth/web/sessions/composer_turn.py:1132](../../../../src/elspeth/web/sessions/composer_turn.py#L1132)).
6. Audit binds canonical authored arguments and resulting version; the inline-custody audit projection replaces raw inline bytes ([src/elspeth/web/composer/tool_batch.py:1486](../../../../src/elspeth/web/composer/tool_batch.py#L1486)). ToolCallCard shows pending/committed/rejected proposal state, while graph and readiness reflect settled state ([src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:147](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L147)).

### 3.1 Route inventory

Measured/traced 2026-11-09 against this worktree. "Gate" lists what decides whether the route runs.

| # | Route | How set_pipeline is reached / consumed | Gate | Source |
|---|---|---|---|---|
| R1 | Canonical registry/emitter | `_SET_PIPELINE_DECLARATION` (`ToolKind.MUTATION`, `augments_on_failure=True`, argument-dependent `BLOB_STORE` effect for `source.inline_blob` only) | always registered | [sessions.py:2105](../../../../src/elspeth/web/composer/tools/sessions.py#L2105), [sessions.py:2113](../../../../src/elspeth/web/composer/tools/sessions.py#L2113) |
| R2 | Public dispatcher | `execute_tool`: credential-material guard → closed Draft 2020-12 schema (`validate_arguments`) → handler → interpretation gate → `finalize_tool_result` | caller flags `validate_arguments`, `require_data_dir_for_paths`, `raise_schema_argument_errors` | [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795), [_dispatch.py:925](../../../../src/elspeth/web/composer/tools/_dispatch.py#L925) |
| R3 | Freeform loop advertisement | Wire projection wraps the flat document in a required `{"pipeline": …}` envelope on both dialects; `strict` is `false` on `openai_strict`; description and inner schema equal the flat declaration (measured) | always in `composer_loop_tool_definitions` | [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391), [wire_projection.py:288](../../../../src/elspeth/web/composer/tools/wire_projection.py#L288) |
| R4 | Freeform decode/audit | Envelope unwrap → canonical/authority hashing → inline-custody audit projection → batch-dependency refusal (`batch_dependency_refused`) | sent-tool membership | [tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399), [tool_batch.py:1486](../../../../src/elspeth/web/composer/tool_batch.py#L1486), [tool_batch.py:1550](../../../../src/elspeth/web/composer/tool_batch.py#L1550), [src/elspeth/web/composer/batch_dependencies.py:81](../../../../src/elspeth/web/composer/batch_dependencies.py#L81) |
| R5 | Freeform `auto_commit` | Finalize (required controls, review canonicalization; builder called 1–3×) then `execute_tool` (builder again) and in-loop state adoption | `trust_mode != "explicit_approve"` | [tool_batch.py:699](../../../../src/elspeth/web/composer/tool_batch.py#L699), [tool_batch.py:1748](../../../../src/elspeth/web/composer/tool_batch.py#L1748), [tool_batch.py:3004](../../../../src/elspeth/web/composer/tool_batch.py#L3004) |
| R6 | Freeform `explicit_approve` | Finalize (only if `redact_tool_call_arguments` parses the arguments, [tool_batch.py:1842](../../../../src/elspeth/web/composer/tool_batch.py#L1842); otherwise the call falls through to `execute_tool`'s schema gate, N5) → inline proposal custody (`prepare_pipeline_custody`, blob finalized) → rebuild with `source.blob_id` → `PipelineProposal.create` / `create_pipeline_composition_proposal`; or `PREVALIDATION_REJECTED` | `trust_mode == "explicit_approve"`, session service present, per-turn proposal cap | [tool_batch.py:1850](../../../../src/elspeth/web/composer/tool_batch.py#L1850), [tool_batch.py:1920](../../../../src/elspeth/web/composer/tool_batch.py#L1920), [tool_batch.py:2142](../../../../src/elspeth/web/composer/tool_batch.py#L2142) |
| R7 | **set_pipeline as proposal vehicle for other tools** (previously unlisted) | Under `explicit_approve`, an incremental mutation whose preview becomes valid and gains required controls is re-expressed as `proposal_tool_name = "set_pipeline"` carrying owned composition-state authority | `explicit_approve` + incremental preview valid + controls changed | [tool_batch.py:2057](../../../../src/elspeth/web/composer/tool_batch.py#L2057), [tool_batch.py:2108](../../../../src/elspeth/web/composer/tool_batch.py#L2108) |
| R8 | Proposal persistence/accept | Blob-reference validation for set_pipeline proposals; only `set_pipeline` proposals route through pipeline commit; accept → `prepare_pipeline_proposal_commit` → builder + `execute_tool("set_pipeline", …)` | pending status, base id/hash, operation fence, timeout | [src/elspeth/web/sessions/mutation_capabilities.py:217](../../../../src/elspeth/web/sessions/mutation_capabilities.py#L217), [src/elspeth/web/sessions/proposal_blob_refs.py:114](../../../../src/elspeth/web/sessions/proposal_blob_refs.py#L114), [routes/composer/proposals.py:161](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L161), [pipeline_commit.py:470](../../../../src/elspeth/web/composer/pipeline_commit.py#L470) |
| R9 | Planner terminal (name not advertised) | `emit_pipeline_proposal.parameters.pipeline` is `canonical_set_pipeline_schema()` (the declaration **plus** runtime naming constraints); builder called raw → canonical → custody-safe (3×); proposals stored with `tool_name="set_pipeline"` | planner route selected; candidate must be `acceptable` | [src/elspeth/web/composer/pipeline_planner.py:1595](../../../../src/elspeth/web/composer/pipeline_planner.py#L1595), [pipeline_planner.py:3068](../../../../src/elspeth/web/composer/pipeline_planner.py#L3068), [planning_application.py:663](../../../../src/elspeth/web/composer/planning_application.py#L663) |
| R10 | Turn audit/settlement binding | Successful set_pipeline invocations become `PipelineDispatchAuditBinding`s for persisted turn audit | `ComposerToolStatus.SUCCESS` | [composer_turn.py:729](../../../../src/elspeth/web/sessions/composer_turn.py#L729), [src/elspeth/web/sessions/routes/_helpers.py:2069](../../../../src/elspeth/web/sessions/routes/_helpers.py#L2069) |
| R11 | Composer MCP | Same flat declaration (including `source.blob_id`/`inline_blob`); MCP-local file-sink collision precheck; `execute_tool(data_dir=None, session_engine=None, require_data_dir_for_paths=True)` with a trained-operator snapshot | always listed in `_COMPOSER_TOOL_NAMES` | [src/elspeth/composer_mcp/server.py:210](../../../../src/elspeth/composer_mcp/server.py#L210), [server.py:271](../../../../src/elspeth/composer_mcp/server.py#L271), [server.py:402](../../../../src/elspeth/composer_mcp/server.py#L402) |
| R12 | Read-before-rebuild companion | `get_pipeline_state(component='set_pipeline_arguments')` serializes the exact public payload or `round_trip_unavailable` (named/multiple blob-backed sources cannot round-trip) | — | [src/elspeth/web/composer/tools/_common.py:3981](../../../../src/elspeth/web/composer/tools/_common.py#L3981), [_common.py:4027](../../../../src/elspeth/web/composer/tools/_common.py#L4027) |
| R13 | Frontend | Card copy, whole-pipeline proposal diff projector (compares only supplied keys; does not predict resets), redacted-argument rendering | proposal/tool event present | [toolCallDescriptions.ts:63](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L63), [ProposalDiff.tsx:292](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx#L292), [ProposalDiff.tsx:681](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx#L681), [ToolCallCard.tsx:147](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L147) |

Not a route: `recovery_controls.py` and `graph_recovery.py` contain no `set_pipeline`/candidate-builder reference (`grep -n "set_pipeline\|SetPipeline" src/elspeth/web/composer/recovery_controls.py src/elspeth/web/composer/graph_recovery.py` → no output, exit 1; the same pattern matches 34 lines in `tool_batch.py`, so the instrument works). No tutorial-specific branch was found: `grep -n -i tutorial` over `tools/sessions.py`, `tool_batch.py`, `pipeline_commit.py` and `composer_mcp/server.py` matches only one historical comment ([sessions.py:2021](../../../../src/elspeth/web/composer/tools/sessions.py#L2021)), not a code branch.

## 4. Branches, failures and recovery

Branches requiring separate regression: singular path source; existing blob source; inline literal source; named sources; trusted reviewed source rehydration; source/blob ambiguity; missing storage context; quota exhaustion; policy refusal; malformed options; failure-route omission; gate expression/routes; queue/row_union/collector contracts; secret references; file-sink collision policy; review reconciliation; invalid but authored draft; acceptable candidate; explicit proposal; automatic commit; stale acceptance; rejected proposal; cancellation after blob work and before graph settlement. Candidate failure preserves prior graph; complete replacement intentionally resets omitted metadata to defaults. Component rejection accumulation skips dependent checks after a local normalization failure rather than emitting speculative downstream errors ([src/elspeth/web/composer/tools/sessions.py:1283](../../../../src/elspeth/web/composer/tools/sessions.py#L1283)).

**Cache, idempotency, concurrency and retry:** `ToolKind.MUTATION`, `cacheable=False`. Repeating the same public arguments is not idempotent: construction assigns current version+1 and inline content may allocate/persist new blob custody. Measured (in-memory dispatch): resubmitting identical arguments on the resulting state advanced version 2 → 3 with an equal `composition_content_hash`, and the shared progress predicate reported progress (see set_pipeline-N3). No caller idempotency token is part of this tool schema. Reuse a proposal/operation's durable identity for recovery rather than issuing a new authoring call after uncertain delivery. The synchronous handler has no automatic retry loop. The proposal commit path checks exact pending status, base state ID/content hash and operation fence before work (`pipeline_commit.py:364–395`); stale concurrent changes therefore require reinspection/reproposal, not blind replay. Provider request retries, proposal acceptance retries and new set_pipeline calls have distinct authorities and effects.

## 5. Defects, restrictions and hypotheses

The original lane demonstrated no set_pipeline behavioral defect. The 2026-11-09 sheet review added the findings below, first from in-memory `execute_tool`, MCP `_dispatch_tool` or `tool_batch._finalize_complete_set_pipeline_candidate` runs with the test trained-operator mock catalog (`tests/unit/web/composer/test_tools.py::_mock_catalog`). The 2026-11-09 probe runs (§7) then re-ran N1 through the stdio-constructed MCP server's `call_tool` handler with the real catalog, and N4, N5 and N8 through the freeform `compose()` loop (scripted model at `_call_llm`, SQLite session store, real catalog). None was a stdio byte-transport, live provider, PostgreSQL or browser run.

| ID | Classification | Finding | Source |
|---|---|---|---|
| set_pipeline-N1 | Demonstrated (through the stdio-constructed MCP server's `call_tool` handler, real catalog; stdio byte transport not exercised; probe P2, §7) | **Source-path fail-closed bypass.** `_validate_source_path` fails closed only when `require_data_dir=True`. `set_source` and `patch_source_options` pass `context.require_data_dir_for_paths`, but both set_pipeline source branches omit it. With `data_dir=None, require_data_dir_for_paths=True`, `set_source` with `path=/etc/passwd` returned `source_path_policy_invalid` while `set_pipeline` returned `success=True`, version 1 → 2, and stored `/etc/passwd`. MCP is the live route: it hard-codes `data_dir=None` + `require_data_dir_for_paths=True`. Web routes always supply `ToolBatchCustody.data_dir: str`, so this is latent there: the web path is safe only because upstream supplies a data_dir. `_validate_sink_path`/provider-config path validators have no fail-closed mode on any tool, so this is not a parity claim about them. Probe P2 reproduced it on the handler the stdio transport runs: `set_pipeline` with `source.options.path="/etc/passwd"` returned `success=True`, version 1 → 2, authoring validation `is_valid=True`, and `get_pipeline_state` showed the stored `/etc/passwd`; `set_source` with the same path returned `source_path_policy_invalid`. Downstream containment observed in the same run: `preview_pipeline`'s stage-2 runtime preflight failed `path_allowlist` ("outside allowed source directories", `preview_is_valid=False`), and in this run the public YAML from `generate_yaml` carried neither the source nor the sink path. So the defect is an authoring-time verdict of "valid" for a path the runtime preflight refuses, not an export of the path. | [sessions.py:1238](../../../../src/elspeth/web/composer/tools/sessions.py#L1238), [sessions.py:1451](../../../../src/elspeth/web/composer/tools/sessions.py#L1451), [sources.py:1130](../../../../src/elspeth/web/composer/tools/sources.py#L1130), [sources.py:2195](../../../../src/elspeth/web/composer/tools/sources.py#L2195), [_common.py:2311](../../../../src/elspeth/web/composer/tools/_common.py#L2311), [_dispatch.py:895](../../../../src/elspeth/web/composer/tools/_dispatch.py#L895), [server.py:402](../../../../src/elspeth/composer_mcp/server.py#L402), [tool_batch.py:826](../../../../src/elspeth/web/composer/tool_batch.py#L826) |
| set_pipeline-N2 | Demonstrated (in-memory) + source-established | **Planner and freeform admit different identifier languages.** The planner terminal advertises `canonical_set_pipeline_schema()`, the registered schema plus runtime naming rules (node id ≤38 `^[a-zA-Z][a-zA-Z0-9_-]*$`, sink names lowercase ≤38, connection names ≤64, reserved `continue`/`fork`/`on_success` and `__` prefixes). The freeform and MCP declaration discloses none of these; set_pipeline `strict=false` on `openai_strict`, so they are wire-legal. A 50-character node id, `sink_name="Main"` and connection `__hidden` were each accepted with `success=True` and version+1. The model learned of them only from closed validation codes `node_id_invalid`, `output_name_invalid` and `connection_label_invalid`, while the planner schema rejects all three up front. | [schema_contract.py:403](../../../../src/elspeth/web/composer/tools/schema_contract.py#L403), [pipeline_planner.py:1595](../../../../src/elspeth/web/composer/pipeline_planner.py#L1595), [sessions.py:2113](../../../../src/elspeth/web/composer/tools/sessions.py#L2113) |
| set_pipeline-N3 | Demonstrated (in-memory) | **Identical resubmit counts as progress.** Same arguments on the resulting state: version 2 → 3, equal content hash, and `tool_result_mutated_composition_state` returned `True`, so anti-anchor records a mutation success for a semantic no-op. This is the SG-03 class on a full-replacement tool. | [sessions.py:1966](../../../../src/elspeth/web/composer/tools/sessions.py#L1966), [discovery_cache.py:38](../../../../src/elspeth/web/composer/discovery_cache.py#L38) |
| set_pipeline-N4 | Demonstrated (freeform `compose()` loop, both trust modes; probe P3, §7) | **Ambiguity refusal is non-actionable on public routes.** The builder's specific `source_binding_ambiguous` ("either source or sources, not both") is not reached on either trust mode. With both keys supplied, the tool message the model saw under `explicit_approve` was `… got invalid_schema` with `validation_errors=[{loc: [], msg: "Validation failed"}]` (closed-schema root `oneOf`), and under `auto_commit` `… got ValidationError` with `[{loc: [], msg: "Value is invalid"}]`: the finalize step's builder ran, but `SetPipelineArgumentsModel.model_validate` refused before the ambiguity branch ([sessions.py:871](../../../../src/elspeth/web/composer/tools/sessions.py#L871)). Neither carried a `repair_instruction`; version unchanged, no proposal. Control: a `null` `on_validation_failure` under `explicit_approve` did carry the field-specific `repair_instruction`, so the channel exists. A *missing* `on_validation_failure` is caught earlier by the required-path precheck and returns a bare `{"error": "… missing required argument(s): source.on_validation_failure"}` with no repair text on both modes ([tool_batch.py:1680](../../../../src/elspeth/web/composer/tool_batch.py#L1680)). | [sessions.py:880](../../../../src/elspeth/web/composer/tools/sessions.py#L880), [sessions.py:2421](../../../../src/elspeth/web/composer/tools/sessions.py#L2421) |
| set_pipeline-N5 | **Refuted as originally stated; narrower finding demonstrated** (freeform `compose()` loop; probe P3, §7) | **Original claim (struck):** ~~under `explicit_approve` a both-`source`-and-`sources` payload returns `PREVALIDATION_REJECTED` with `source_binding_ambiguous`, while `auto_commit` returns ARG_ERROR~~. The run showed both modes return ARG_ERROR (status `arg_error`, no proposal, version unchanged); the premise was wrong because that payload is not pydantic-valid. In `explicit_approve`, finalization runs only if `redact_tool_call_arguments` parses the arguments ([tool_batch.py:1842](../../../../src/elspeth/web/composer/tool_batch.py#L1842)), so a pydantic-invalid payload skips it and meets `execute_tool`'s closed-schema gate. In `auto_commit` the finalize step runs first ([tool_batch.py:1775](../../../../src/elspeth/web/composer/tool_batch.py#L1775)) and the builder's pydantic parse raises `MODEL_VALIDATION` without schema violations ([sessions.py:871](../../../../src/elspeth/web/composer/tools/sessions.py#L871)). **Demonstrated residual:** the same malformed input meets a different gate by trust mode, and that changes what the model is told. For a `null` `source.on_validation_failure`, `explicit_approve` returned category `schema_shape` with the field-specific `repair_instruction` from [tool_error_payloads.py:79](../../../../src/elspeth/web/composer/tool_error_payloads.py#L79); `auto_commit` returned category `model_validation` with the same `loc` but **no** `repair_instruction`. For both keys, the categories differ (`schema_bound` vs `model_validation`) and the messages differ ("Validation failed" vs "Value is invalid"); neither is actionable (N4). Semantic rejections were unaffected: `plugin_not_installed` reached the model on both modes, with `PREVALIDATION_REJECTED` status only under `explicit_approve` (intended). | [tool_batch.py:1775](../../../../src/elspeth/web/composer/tool_batch.py#L1775), [tool_batch.py:1842](../../../../src/elspeth/web/composer/tool_batch.py#L1842), [sessions.py:871](../../../../src/elspeth/web/composer/tools/sessions.py#L871), [tool_error_payloads.py:79](../../../../src/elspeth/web/composer/tool_error_payloads.py#L79) |
| set_pipeline-N6 | Source-established; intended policy, undisclosed precondition | **Draft-validity policy differs by route.** `auto_commit` and MCP apply an authored-invalid draft. `explicit_approve` and the planner refuse anything not `acceptable`, so a staged, repairable draft cannot be proposed under explicit approval. The declaration and skills do not say this; it is the same class as SG-02. | [tool_batch.py:1916](../../../../src/elspeth/web/composer/tool_batch.py#L1916), [tool_batch.py:3290](../../../../src/elspeth/web/composer/tool_batch.py#L3290), [pipeline_planner.py:3083](../../../../src/elspeth/web/composer/pipeline_planner.py#L3083) |
| set_pipeline-N7 | Source-established; reachability hypothesis | **Post-custody revalidation failure is softened on one route only.** After explicit-approval inline custody is finalized (the blob is durable), the loop rebuilds with `source.blob_id`. If that rebuild is not `acceptable`, the loop returns an ordinary `PREVALIDATION_REJECTED` and creates no proposal. The planner treats the identical condition as `AuditIntegrityError("custody-safe pipeline failed canonical revalidation")`. The skill tells the model that "Failed mutations do not create reusable blobs". No input that reaches this branch was found or constructed. | [tool_batch.py:1973](../../../../src/elspeth/web/composer/tool_batch.py#L1973), [tool_batch.py:1987](../../../../src/elspeth/web/composer/tool_batch.py#L1987), [pipeline_planner.py:3142](../../../../src/elspeth/web/composer/pipeline_planner.py#L3142), [pipeline_composer.md:1440](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L1440) |
| set_pipeline-N8 | Demonstrated (pytest, freeform `compose()` loop; probe P1, §7) test-instrument finding | **"Builder once" test cannot see the finalize build.** `test_auto_commit_set_pipeline_uses_candidate_builder_once` patches `elspeth.web.composer.tools.sessions.build_set_pipeline_candidate`, but `tool_batch` binds the re-export at import. The unmodified test passes (exit 0). Running the same scenario with both names patched gave sessions-bound = 1 and tool_batch-bound = 1, so two builds per plain auto-commit call, of which the test counts one; the test's own facts (version+1, no proposal) still held. The original `== 1` assertion applied to the combined count goes red (`assert (1 + 1) == 1`, exit 1). The controls/review-changed path (raw, compact and canonical finalize builds plus the dispatch build, so four) remains a source trace and was not run. The assertion is true; the name overclaims. | [test_freeform_proposal_prevalidation.py:1542](../../../../tests/integration/web/composer/test_freeform_proposal_prevalidation.py#L1542), [tool_batch.py:165](../../../../src/elspeth/web/composer/tool_batch.py#L165), [tool_batch.py:699](../../../../src/elspeth/web/composer/tool_batch.py#L699) |
| set_pipeline-N9 | Demonstrated (MCP `_dispatch_tool`, in memory) | **MCP advertises dead source fields.** The MCP declaration includes `source.blob_id` and `source.inline_blob`, but MCP has no session engine. Both returned `source_binding_context_unavailable`: "Blob tools require session context." (no component prefix; names "Blob tools" rather than set_pipeline) and "set_pipeline source.inline_blob requires session context." Neither tells an MCP caller to use `options.path`. The module docstring says blob tools are excluded; these fields leak through the shared declaration. | [server.py:210](../../../../src/elspeth/composer_mcp/server.py#L210), [sources.py:841](../../../../src/elspeth/web/composer/tools/sources.py#L841), [sessions.py:1110](../../../../src/elspeth/web/composer/tools/sessions.py#L1110) |
| set_pipeline-N10 | Source-established; intended restriction, under-disclosed | **Named blob-backed sources cannot be rebuilt by this tool.** `sources` values have no `blob_id`/`inline_blob`, and `set_pipeline_arguments` returns `round_trip_unavailable` for named or multiple blob-backed sources. The `sources.*.options` description nevertheless says to "Bind blobs through … source.blob_id, or source.inline_blob", which no `sources` entry can use. | [_common.py:3990](../../../../src/elspeth/web/composer/tools/_common.py#L3990), [sessions.py:2183](../../../../src/elspeth/web/composer/tools/sessions.py#L2183), [sessions.py:2197](../../../../src/elspeth/web/composer/tools/sessions.py#L2197) |
| set_pipeline-N11 | Source-established, low | **Result-code casing.** The quota refusal uses `BLOB_QUOTA_EXCEEDED`, the only upper-case literal `error_code=` in `src/elspeth/web/composer` (`grep -rhoE 'error_code="[A-Z_]{4,}"'` → 2 hits, both this code), beside lower-snake closed codes. It is registered in `error_codes.py`, so guidance resolves; the inconsistency is cosmetic. | [sessions.py:2087](../../../../src/elspeth/web/composer/tools/sessions.py#L2087), [tool_batch.py:1964](../../../../src/elspeth/web/composer/tool_batch.py#L1964) |

Checked and **not** a set_pipeline defect: SG-07 does not reproduce here. A queue carrying `scope_name/scope_opener/scope_policy` was refused with `queue_config_invalid` and the state was unchanged, because set_pipeline passes the scope fields into `NodeSpec` before `queue_node_contract_error` ([sessions.py:1902](../../../../src/elspeth/web/composer/tools/sessions.py#L1902), [sessions.py:1911](../../../../src/elspeth/web/composer/tools/sessions.py#L1911)). Duplicate node ids, duplicate sink names, a transform with `plugin: null` and an edge to an unknown node are each accepted as authored-invalid drafts with closed validation codes, consistent with the incremental-draft policy. A non-gate `condition` is deliberately not expression-validated ([tests/unit/web/composer/tools/test_tools_pipeline.py:2243](../../../../tests/unit/web/composer/tools/test_tools_pipeline.py#L2243)).

**Structural observation:** candidate construction owns substantial domain logic for sources, transforms, outputs, secrets and interpretation metadata, while batch/commit paths also perform finalization and custody. That makes public-tool parity and authority-order review necessary; file size alone is not a defect. **Restriction:** exact historical reconstruction is not guaranteed; read-before-rebuild and preserve/refuse remain required. Any source-owned required-control insertion must remain validation/admission of provider-authored structure, within the Composer invariant.

### 5.1 Guidance parity

Instruments: `grep -c set_pipeline` matches 31 lines in `pipeline_composer.md` and 2 in `pipeline_capabilities.md`, which are the known mentions. Concept greps (`-i`, per file, composer/capabilities): `set_pipeline_arguments` 0/1, `round_trip_unavailable` 0/0, `PREVALIDATION_REJECTED` 1/0, `inline_blob` 6/2, `advisory|display record` 0/0, `lowercase|max length|maxLength|reserved` matched only unrelated lines (header normalization at composer:688 and template reserved names at capabilities:342), which shows that grep would have caught a naming-rule passage had one existed.

| ID | Kind | What the model is told vs what happens | Where |
|---|---|---|---|
| set_pipeline-G1 | Missing guidance | The skill says to use a full replacement "only when you can supply the exact current bindings" but never names how to get them. `get_pipeline_state(component='set_pipeline_arguments')` and its `round_trip_unavailable` outcome appear in neither skill; the composer skill's state bullet teaches only the full-state aliases. The capabilities skill says inspection records are "not a substitute for exact `set_pipeline_arguments`" without saying how to request them. The rule exists only in authoring-aid rules and in the handler's no-source repair text. | [pipeline_composer.md:180](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L180), [pipeline_composer.md:341](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L341), [pipeline_capabilities.md:240](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L240), [planner_authoring_aids.py:383](../../../../src/elspeth/web/composer/planner_authoring_aids.py#L383), [sessions.py:628](../../../../src/elspeth/web/composer/tools/sessions.py#L628) |
| set_pipeline-G2 | Missing guidance (N2) | Neither skill nor the freeform/MCP declaration states the runtime naming rules that the planner schema discloses: id/sink length, lowercase sinks, reserved connection names, `__` prefix. Freeform learns them only from post-hoc validation codes. | [sessions.py:2113](../../../../src/elspeth/web/composer/tools/sessions.py#L2113), [schema_contract.py:403](../../../../src/elspeth/web/composer/tools/schema_contract.py#L403) |
| set_pipeline-G3 | Wrong (self-contradictory) declaration text (N10) | `sources.*.options` reuses the singular-source ownership note "Bind blobs through … source.blob_id, or source.inline_blob", while `sources` itself says values are "source's shape minus blob_id/inline_blob". The capabilities skill states the restriction correctly ("Named sources … without inline custody fields"). | [sessions.py:2186](../../../../src/elspeth/web/composer/tools/sessions.py#L2186), [sessions.py:2197](../../../../src/elspeth/web/composer/tools/sessions.py#L2197), [pipeline_capabilities.md:258](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L258) |
| set_pipeline-G4 | Missing guidance with a harmful interaction | Omitting `metadata` resets name/description to defaults, which was measured. The composer skill tells the model to record unregistered pipeline decisions in `metadata.description`, so a later full rebuild that omits `metadata` silently discards that reviewer-facing record. Neither the `metadata` argument description nor the skill discloses the reset. | [sessions.py:1951](../../../../src/elspeth/web/composer/tools/sessions.py#L1951), [sessions.py:2414](../../../../src/elspeth/web/composer/tools/sessions.py#L2414), [pipeline_composer.md:1234](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L1234) |
| set_pipeline-G5 | Missing guidance (N6) | The `PREVALIDATION_REJECTED` and `APPROVAL_REQUIRED` statuses are taught generically. The skill does not say that under explicit approval a set_pipeline must be a complete, valid candidate to be proposed, while "Complex New Pipeline Batching" recommends staged construction. | [pipeline_composer.md:186](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L186), [pipeline_composer.md:461](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L461) |
| set_pipeline-G6 | Mild tension | The declaration calls edges "Advisory display records … [] is complete for a fully-wired topology". The capabilities skill says "Edges state reviewed graph relationships". The composer skill gives no set_pipeline edge rule (0 hits for advisory/display record). The two statements do not contradict on runtime authority, but the skill does not tell the model that `edges: []` is acceptable. | [sessions.py:2355](../../../../src/elspeth/web/composer/tools/sessions.py#L2355), [pipeline_capabilities.md:262](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L262) |
| set_pipeline-G7 | Overclaim on one route (hypothesis-grade, N7) | "Failed mutations do not create reusable blobs" holds for direct dispatch, where the blob persists only after `success`. On the explicit-approval path a custody blob is already durable before the final rebuild that can still yield `PREVALIDATION_REJECTED`. Reachability is unproven. | [pipeline_composer.md:1440](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L1440), [tool_batch.py:1973](../../../../src/elspeth/web/composer/tool_batch.py#L1973) |
| set_pipeline-G8 | Surface mismatch (N9) | On MCP, the declaration advertises `source.blob_id`/`inline_blob` and says "Use blob_id to bind an already uploaded session blob", but neither can work there and no MCP-specific description is projected. | [sessions.py:2129](../../../../src/elspeth/web/composer/tools/sessions.py#L2129), [server.py:221](../../../../src/elspeth/composer_mcp/server.py#L221) |

Parity confirmed (no finding): the tool-selection table matches the declaration description's redirect to `patch_node_options` / `splice_transform` / `upsert_node` / `upsert_edge` ([pipeline_composer.md:175](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L175), [sessions.py:2118](../../../../src/elspeth/web/composer/tools/sessions.py#L2118)). The `rejected_component` vocabulary (`source`, `source:<name>`, `node:<id>`, `output:<name>`, absent = whole candidate) matches the builder; `source` and `node:t1` were observed in probes ([pipeline_composer.md:445](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L445)). The taught data keys `server_owned_metadata_note`, `source_blob` and `inline_blob` are exactly the keys the handler emits ([pipeline_composer.md:544](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L544), [sessions.py:2039](../../../../src/elspeth/web/composer/tools/sessions.py#L2039), [sessions.py:2089](../../../../src/elspeth/web/composer/tools/sessions.py#L2089)). "A failed mutation does not persist partial state" holds for every candidate rejection path, which returns the prior `state` ([pipeline_composer.md:311](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L311)).

## 6. Ownership, remediation and dependencies

Orchestrator owns proposal/commit lifecycle and exact state authority; candidate builder owns a deterministic candidate; source/blob, plugin, graph, review and sink policy owners own their respective validation. Prefer explicit candidate/effect preparation and one settlement boundary over another server synthesis path. Before moving code, define which transformations are normalization, required-control admission or planner authorship and prove each transition still invokes the provider where required. Dependency order: exact authority/result contracts → candidate parity → lifecycle settlement → public tool migration. Preserve current data; no migration/reset is authorized.

Structural follow-through from the 2026-11-09 review (R5/R1 scope, not simple fixes): carry the single finalized candidate into dispatch instead of rebuilding it 2–4× per auto-commit call (N8). Explicit approval adds a post-custody rebuild and two more builds at accept time (`prepare_pipeline_proposal_commit` plus `execute_tool`); route all three settlement paths through one stated draft-validity policy (N5/N6); and have the freeform/MCP declaration and the planner terminal advertise one schema authority (N2). Each must preserve provider authorship. The candidate builder validates, it never authors structure.

**Implementation risk:** High: changes span graph replacement, review binding and blob/proposal settlement. Preserve current bytes/authority and separately test cancellation before and after each durable effect.

### 6.1 Simple enhancements

Bounded, non-architectural fixes. None is implemented.

| ID | What | Where | Why | Acceptance check |
|---|---|---|---|---|
| E1 | Pass `require_data_dir=context.require_data_dir_for_paths` to both `_validate_source_path` calls | [sessions.py:1238](../../../../src/elspeth/web/composer/tools/sessions.py#L1238), [sessions.py:1451](../../../../src/elspeth/web/composer/tools/sessions.py#L1451) | N1: honours the documented "public LLM/MCP entry points fail closed" contract that `set_source` already meets. **Tradeoff to surface first:** on MCP this removes the only way to author a local file source, because `set_source` already fails closed there. Pair it with MCP passing its stdio `data_dir`/scratch authority, or record an explicit MCP decision. | Parameterise `test_source_path_arguments_require_data_dir_for_s2_confinement` ([test_tool_dispatch_boundary.py:192](../../../../tests/unit/web/composer/test_tool_dispatch_boundary.py#L192)) over `set_source` and `set_pipeline` (`source` and `sources` forms): `source_path_policy_invalid`, `rejected_component` `source` / `source:<name>`, state unchanged. Negative control: with `require_data_dir_for_paths=False` the existing `test_tools_pipeline.py` `/data/in.csv` fixtures still succeed. |
| E2 | Advertise the runtime naming constraints in the set_pipeline declaration, for example by reusing `_disclose_runtime_naming_constraints` | [sessions.py:2113](../../../../src/elspeth/web/composer/tools/sessions.py#L2113), [schema_contract.py:403](../../../../src/elspeth/web/composer/tools/schema_contract.py#L403) | N2/G2: freeform and MCP would learn the same naming language the planner gets before the call, not after. | The schema byte pins in `test_tool_declarations.py` change deliberately. A 50-character id, `sink_name="Main"` and connection `__hidden` are refused at the schema gate as ARG_ERROR naming the field, on freeform and MCP. |
| E3 | Map a root `oneOf` failure to a specific repair: "supply exactly one of `source` or `sources`; omit the other key (null is not accepted)" | Add a branch beside the existing set_pipeline failure-choice repair in [tool_error_payloads.py:79](../../../../src/elspeth/web/composer/tool_error_payloads.py#L79); the root `oneOf` is at [sessions.py:2421](../../../../src/elspeth/web/composer/tools/sessions.py#L2421). The payload builder serves only the compose loop, so MCP needs its own equivalent. | N4: today `loc: []`, "Validation failed", no `repair_instruction`. | Both-keys and neither-key payloads return a `repair_instruction` naming `source`/`sources` under **both** `explicit_approve` and `auto_commit`. `auto_commit` currently refuses through the builder's `MODEL_VALIDATION` error, which carries no schema violations (N5), so a fix keyed only on schema violations would miss it. The existing failure-choice repair text is unchanged. |
| E4 | Say in the `metadata` description: "Full replacement: omitted or null metadata resets name and description to defaults; re-supply them from `set_pipeline_arguments`" | [sessions.py:2414](../../../../src/elspeth/web/composer/tools/sessions.py#L2414) | G4: avoids silent loss of reviewer-facing `metadata.description`. | Declaration text pinned. A test asserts that omitting `metadata` on a state named `X` yields `Untitled Pipeline`, documenting the behaviour rather than changing it. |
| E5 | Give `sources.*.options` its own ownership note without the `source.blob_id` / `source.inline_blob` clause, and state that named blob-backed sources cannot be rebuilt here (`round_trip_unavailable`) | [sessions.py:2197](../../../../src/elspeth/web/composer/tools/sessions.py#L2197) | G3/N10: removes a self-contradiction and an undisclosed dead end. | Declaration pin. `sources.*.options.description` no longer contains `source.blob_id`. |
| E6 | Retarget the "builder once" test patch to `elspeth.web.composer.tool_batch.build_set_pipeline_candidate` as well, then assert the true count, or rename the test | [test_freeform_proposal_prevalidation.py:1559](../../../../tests/integration/web/composer/test_freeform_proposal_prevalidation.py#L1559) | N8: the current instrument is blind to the finalize build. | With both bindings patched, the plain auto-commit case counts 2. The test fails if a third build is introduced. |
| E7 | Use a set_pipeline-specific, component-prefixed refusal for `source.blob_id` without session context, plus an MCP hint ("this surface has no session blobs; use `options.path`") | [sources.py:841](../../../../src/elspeth/web/composer/tools/sources.py#L841), [sessions.py:1110](../../../../src/elspeth/web/composer/tools/sessions.py#L1110) | N9: "Blob tools require session context." names the wrong tool and gives no next step. | MCP `_dispatch_tool` with `source.blob_id` returns a message prefixed `Source 'source':` that names set_pipeline. The singular inline_blob refusal is unchanged. |
| E8 | Disclose the explicit-approval precondition in the tool description or skill: "under explicit approval only a complete, valid pipeline is proposed; an invalid candidate returns PREVALIDATION_REJECTED and is not applied" | [sessions.py:2118](../../../../src/elspeth/web/composer/tools/sessions.py#L2118) or [pipeline_composer.md:461](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L461) | N6/G5: same remedy class as SG-02's disclosure. | Text pinned. `test_semantic_rejection_reaches_next_model_turn_then_repair_creates_one_proposal` still passes. |
| E9 | Name `get_pipeline_state(component='set_pipeline_arguments')` and `round_trip_unavailable` in the composer skill's rebuild guidance | [pipeline_composer.md:180](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L180) | G1: the read-before-rebuild path is currently taught only in authoring aids. | Skill text plus the skill-hash/taught-marker tests that pin skill bytes. |
| E10 | Add a set_pipeline regression for N7: if the post-custody rebuild is not acceptable, either raise like the planner or document and test the orphaned custody blob | [tool_batch.py:1987](../../../../src/elspeth/web/composer/tool_batch.py#L1987) | N7: two routes disagree on the same integrity condition. | A forced-unacceptable rebuild (monkeypatched builder) produces the chosen outcome, and the blob row count is asserted. |

## 7. Evidence, regression checks and limits

Read full-replacement, version and gate-preservation tests ([tests/unit/web/composer/tools/test_tools_pipeline.py:196](../../../../tests/unit/web/composer/tools/test_tools_pipeline.py#L196), gate plugin refused with state unchanged; [test_tools_pipeline.py:1842](../../../../tests/unit/web/composer/tools/test_tools_pipeline.py#L1842), replacement; [test_tools_pipeline.py:1870](../../../../tests/unit/web/composer/tools/test_tools_pipeline.py#L1870), version increment); inspected argument/source/review guards in production.

2026-11-09 review reproductions: in-memory only, using `PYTHONPATH=<worktree>:<worktree>/src:<worktree>/elspeth-lints/src`, with `elspeth.__file__` printed and resolving into the worktree. They used the test `execute_tool` shim and `_mock_catalog` / `_valid_pipeline_args` from `tests/unit/web/composer/test_tools.py`, `elspeth.composer_mcp.server._dispatch_tool`, and `tool_batch._finalize_complete_set_pipeline_candidate` with `run_sync_in_worker` replaced by an inline await. The probe runs below then added pytest runs through the compose loop and the MCP handler.

**2026-11-09 probe runs.** `$WT` is the audit worktree and `$L` the lane-private probe directory (outside the worktree). Every run carried `HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 NO_PROXY= LITELLM_LOCAL_MODEL_COST_MAP=True ELSPETH_DISABLE_NETWORK=1 PYTHONDONTWRITEBYTECODE=1` and loaded a guard plugin (`-p sp_guard`) that replaces `litellm.acompletion`, `litellm.completion`, `provider_gateway._litellm_acompletion` (which the advisor and planning application read at call time) and `pipeline_planner._litellm_acompletion` with raisers and refuses IPv4/IPv6 `socket.connect`. Each log records the guard's positive control (all five raised) and `GUARD_HITS_DURING_RUN = 0`. `elspeth.__file__` resolved to `$WT/src/elspeth/__init__.py` in every log. The model was scripted at `provider_gateway._call_llm` and the advisor checkpoint was the harness's clean double. Probe files under `$L` import the worktree test helpers (`_harness`, `_ScriptedLLM`, `_valid_pipeline_args` from `tests/integration/web/composer/test_freeform_proposal_prevalidation.py`; `_call_handler`, `_stdio_output_args` from `tests/unit/composer_mcp/test_server.py`) and load the conftest chain with `-p`, checked by asserting the autouse fixtures are in `request.fixturenames`. Common pytest form: `cd $WT && PYTHONPATH=$WT:$WT/src:$WT/elspeth-lints/src:$L TMPDIR=$L/tmp python -m pytest <id> --rootdir $WT -c $WT/pyproject.toml -n 0 -p no:cacheprovider -p sp_guard -p tests.conftest <conftests> -o pythonpath="$WT/src $WT/elspeth-lints/src" -s`.

| Probe | Finding | Outcome | Command (ids) | Exit | Observed |
|---|---|---|---|---|---|
| P1a | N8 | baseline | `tests/integration/web/composer/test_freeform_proposal_prevalidation.py::test_auto_commit_set_pipeline_uses_candidate_builder_once`, brief's fixed form plus `-p sp_guard` | 0 | The unmodified test passes. |
| P1b | N8 | **confirmed** | `$L/test_probe_sp_n8_n5.py::test_n8_both_bindings_counted` (conftests: `tests.integration.conftest`, `tests.integration.web.conftest`) | 0 | `tool_batch.build_set_pipeline_candidate is sessions.build_set_pipeline_candidate` at import; sessions-bound builds = 1, tool_batch-bound builds = 1; version 1 → 2; 0 proposals. |
| P1c | N8 (red check) | **confirmed** | `$L/test_probe_sp_n8_n5.py::test_n8_red_check_original_assertion_on_combined_count` | 1 (expected) | The original `== 1` assertion on the combined count fails: `assert (1 + 1) == 1`. |
| P2 | N1 | **confirmed** | `$L/test_probe_sp_n1.py` (3 tests; conftest `tests.unit.conftest`; real `create_catalog_service()`; `_build_stdio_server(catalog, scratch, data_dir)` under `$L`) | 0 | Driven through the stdio-constructed server's `call_tool` handler; the stdio byte pipe was not exercised because it needs a server process. Control: `set_source` with `/etc/passwd` → `success=False`, `source_path_policy_invalid`. Probe: `set_pipeline` with the same path → `success=True`, version 1 → 2, `is_valid=True`; `get_pipeline_state` shows `sources.source.options.path = "/etc/passwd"`. Then `preview_pipeline` → runtime preflight `path_allowlist` failed ("outside allowed source directories"), `preview_is_valid=False`; `generate_yaml` succeeded and its public YAML carries no source or sink paths. Known-negative: in-`data_dir` relative path `blobs/in.csv` → `set_pipeline` success; `set_source` refuses even that path on MCP (`source_path_policy_invalid`), which is the E1 tradeoff. |
| P3 | N5 (and N4) | **refuted** as stated; narrower trust-mode difference demonstrated | `$L/test_probe_sp_n8_n5.py::test_n5_channel` (4 cases × `explicit_approve`/`auto_commit`) | 0 | Both keys: ARG_ERROR on both modes (`schema_bound`, "Validation failed" vs `model_validation`, "Value is invalid"), `loc: []`, no `repair_instruction`, no `PREVALIDATION_REJECTED`, no `source_binding_ambiguous`. Control `null` `on_validation_failure`: explicit → `schema_shape` **with** `repair_instruction`; auto → `model_validation` **without** it. Control missing `on_validation_failure`: `missing_required_path`, bare error, both modes. Control `uninstalled_canary_plugin`: tool status success, `plugin_not_installed` on both, with `data.status = PREVALIDATION_REJECTED` only under explicit. Version unchanged and 0 proposals in every case. |

Logs: `$L/n8-baseline.log`, `$L/n8-probe.log`, `$L/n8-red.log`, `$L/n1-mcp-handler.log`, `$L/n5-probe.log`. Positive controls: `set_source` refused the same out-of-tree path that set_pipeline admitted (N1); the unmodified `_valid_pipeline_args` control was `success=True, valid=True` before each mutation (N2/N3); and the SG-07 queue case was refused as expected. Existing coverage gap: the S2 fail-closed tests exercise only `set_source` ([tests/unit/web/composer/test_tool_dispatch_boundary.py:192](../../../../tests/unit/web/composer/test_tool_dispatch_boundary.py#L192)). Entire test_tools_pipeline.py passed within the 370-test selection. This proves the covered handler/dispatch branches, not deployment settlement or PostgreSQL races. Confidence: high in described ownership/branch structure, moderate in exhaustive safety. Required later checks: whether any pydantic-valid but schema-invalid payload still reaches different gates by trust mode (P3 tested only the payloads in its table); inline blob rejection leaves no unauthorized publication, including the post-custody rebuild branch (N7); stale proposed state cannot commit; cancellation preserves actual effects and audit; full replacement and incremental construction yield equivalent runtime wiring; N8's four-build count on the controls/review-changed path; and N1 over the real stdio byte transport.

Source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection and selected local tests do not establish live provider behavior, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.sessions._handle_set_pipeline`](../../../../src/elspeth/web/composer/tools/sessions.py#L2097).

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
    "source": {
      "type": "object",
      "additionalProperties": false,
      "description": "Source configuration. Use blob_id to bind an already uploaded session blob, or inline_blob to materialize user-provided literal data atomically with the pipeline. null carries NO meaning — it is not 'keep the existing source'. Every call must supply source or sources as a real object: on a full rebuild, re-supply the complete existing source block, including blob_id only when that block carries one.",
      "properties": {
        "plugin": {
          "type": "string"
        },
        "blob_id": {
          "type": [
            "string",
            "null"
          ],
          "description": "Existing ready session blob ID to bind as this source (resolved exactly like set_source_from_blob)."
        },
        "options": {
          "type": "object",
          "description": "Plugin-specific source config. Required by most file/data sources. Server/resolver-owned source option roots are not settable: blob_ref, blobs, source_authoring. Bind blobs through set_source_from_blob, set_source_from_blobs, source.blob_id, or source.inline_blob. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
        },
        "on_success": {
          "type": "string",
          "description": "Connection-name string the source PUBLISHES. Some downstream consumer (node 'input' or output 'sink_name') MUST equal this. Connections match by string, not by node id."
        },
        "on_validation_failure": {
          "type": "string",
          "description": "Required explicit non-empty source-validation failure choice: 'discard' or an eligible named sink. Any other value, including 'quarantine', must match a configured output/sink name. Full replacement does not reuse prior choices; null/empty is invalid."
        },
        "description": {
          "type": [
            "string",
            "null"
          ],
          "description": "One short sentence of plain prose saying what this step does, shown to reviewers on the Spec tab. Supply it when creating the step and refresh it whenever you change what the step does. Informational only — never affects validation or execution."
        },
        "inline_blob": {
          "type": [
            "object",
            "null"
          ],
          "additionalProperties": false,
          "description": "Inline source content to create as a session blob before binding. Fields mirror create_blob.",
          "properties": {
            "filename": {
              "type": "string"
            },
            "mime_type": {
              "type": "string",
              "enum": [
                "application/json",
                "application/jsonl",
                "application/x-jsonlines",
                "text/csv",
                "text/jsonl",
                "text/plain"
              ]
            },
            "content": {
              "type": "string",
              "maxLength": 262144
            },
            "description": {
              "type": [
                "string",
                "null"
              ]
            }
          },
          "required": [
            "filename",
            "mime_type",
            "content"
          ]
        }
      },
      "required": [
        "plugin",
        "on_success",
        "on_validation_failure"
      ]
    },
    "sources": {
      "type": "object",
      "description": "Named source roots keyed by stable source name; use instead of source for multi-source pipelines. Values share source's shape minus blob_id/inline_blob. null carries NO meaning; every call must supply source or sources as a real object.",
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "plugin": {
            "type": "string"
          },
          "options": {
            "type": "object",
            "description": "Plugin-specific source config. Server/resolver-owned source option roots are not settable: blob_ref, blobs, source_authoring. Bind blobs through set_source_from_blob, set_source_from_blobs, source.blob_id, or source.inline_blob. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
          },
          "on_success": {
            "type": "string"
          },
          "on_validation_failure": {
            "type": "string",
            "description": "Required explicit non-empty source-validation failure choice: 'discard' or an eligible named sink. Any other value, including 'quarantine', must match a configured output/sink name. Full replacement does not reuse prior choices; null/empty is invalid."
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
          "plugin",
          "on_success",
          "on_validation_failure"
        ]
      }
    },
    "nodes": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "id": {
            "type": "string"
          },
          "node_type": {
            "type": "string"
          },
          "plugin": {
            "type": [
              "string",
              "null"
            ]
          },
          "input": {
            "type": "string",
            "description": "Upstream published connection this node consumes, normally an on_success or gate route. Valid implicit aggregation/queue/coalesce publications can equal the publisher's node id. For coalesce/row_union use their branch/input rules. Error routes cannot feed arbitrary processing nodes; only a permitted enclosing bound-region closer is eligible."
          },
          "on_success": {
            "type": [
              "string",
              "null"
            ],
            "description": "Connection-name string this node PUBLISHES; some downstream input/sink_name MUST equal it. Omit for gates (they route via condition+routes). A coalesce publishes under its own node id, and its on_success, when set, may ONLY name a sink. A row_union's on_success must name a downstream processing connection, never a sink."
          },
          "on_error": {
            "type": [
              "string",
              "null"
            ],
            "minLength": 1,
            "description": "Node-level error policy (transform/aggregation/gate): 'discard' or a declared sink name. A transform or gate inside a valid bound region may instead divert loss to that enclosing region's closer; aggregations cannot. Arbitrary processing inputs are forbidden. Transforms/aggregations require an explicit non-empty choice; full replacement does not reuse prior choices. For a gate it covers row expression-evaluation errors and is authored on the node, never as an edge; omit it to preserve fail-fast behavior. A failed aggregation batch applies this policy to every buffered row; discard records quarantine outcomes."
          },
          "options": {
            "type": "object",
            "description": "Plugin-specific node config. Row-column options use the field name carried by upstream rows, normally a normalized header or source field_mapping target. Runtime-owned LLM option fields are not settable: approved_prompt_artifact_hash. Omit them; ELSPETH re-derives them during review reconciliation or execution. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
          },
          "condition": {
            "type": [
              "string",
              "null"
            ]
          },
          "routes": {
            "type": [
              "object",
              "null"
            ],
            "additionalProperties": {
              "type": "string"
            },
            "description": "Gate route mapping to sink names, downstream connection names, 'fork', or 'discard' for an audited terminal drop."
          },
          "fork_to": {
            "type": [
              "array",
              "null"
            ],
            "items": {
              "type": "string"
            }
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
            "description": "Branch inputs for coalesce or row_union. For row_union, every branches value is a real consumed input connection; input must repeat the first value only as an adapter placeholder."
          },
          "policy": {
            "type": [
              "string",
              "null"
            ]
          },
          "merge": {
            "type": [
              "string",
              "null"
            ]
          },
          "trigger": {
            "type": [
              "object",
              "null"
            ],
            "properties": {
              "count": {
                "type": [
                  "integer",
                  "null"
                ]
              },
              "timeout_seconds": {
                "type": [
                  "number",
                  "null"
                ]
              },
              "condition": {
                "type": [
                  "string",
                  "null"
                ]
              }
            },
            "additionalProperties": false
          },
          "output_mode": {
            "type": [
              "string",
              "null"
            ],
            "description": "Aggregation mode: transform (default), or passthrough only when the plugin's catalogue aggregation_output_modes includes it; passthrough requires one emitted row per buffered row."
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
            "description": "Optional finite positive timeout for structural barrier nodes (coalesce or row_union)."
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
            "description": "Group arrival policy (collector only): 'require_all' or 'best_effort'. REQUIRED for collectors — there is no default; the author decides whether a lost member fails the group."
          }
        },
        "required": [
          "id",
          "node_type",
          "input"
        ],
        "additionalProperties": false
      },
      "description": "Node specs. A queue node is a structural fan-in point: node_type='queue', id == input == the shared connection name, plugin and every routing field omitted, options at most an optional description; multiple producers may publish that name precisely because the queue is declared. A row_union is plugin-free require_all fork reconvergence: at least two ordered branches, input set to the first branch connection, on_success published to downstream processing (not a sink), no options/routing/policy/merge fields, optional timeout_seconds. A collector closes a declared EXPAND scope: node_type='collector', a batch-aware plugin, and the scope binding authored on the node — scope_name, scope_opener (the multi-row transform that opens the group), and scope_policy ('require_all' or 'best_effort', no default)."
    },
    "edges": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "id": {
            "type": "string"
          },
          "from_node": {
            "type": "string"
          },
          "to_node": {
            "type": "string"
          },
          "edge_type": {
            "type": "string",
            "enum": [
              "on_success",
              "on_error",
              "route_true",
              "route_false",
              "fork"
            ]
          },
          "label": {
            "type": [
              "string",
              "null"
            ]
          }
        },
        "required": [
          "id",
          "from_node",
          "to_node",
          "edge_type"
        ],
        "additionalProperties": false
      },
      "description": "Edge specs. edge_type='on_error' is supported for transform/aggregation sink wiring only; a gate's evaluation-error policy is the node's on_error field. Advisory display records: node connection fields are the authoritative runtime wiring, and [] is complete for a fully-wired topology."
    },
    "outputs": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "sink_name": {
            "type": "string",
            "description": "Sink identifier and routing target: name it from an upstream success/gate route or an eligible source-validation, processing-error or sink-write-failure policy. It need not match a node id."
          },
          "plugin": {
            "type": "string"
          },
          "options": {
            "type": "object",
            "description": "Plugin-specific sink config. The interpretation_requirements option root is not settable on outputs. Stage an authorable review on its source or node instead."
          },
          "on_write_failure": {
            "type": "string",
            "description": "Required explicit non-empty failure choice; full replacement does not reuse prior choices."
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
          "sink_name",
          "plugin",
          "on_write_failure"
        ],
        "additionalProperties": false,
        "examples": [
          {
            "sink_name": "results",
            "plugin": "json",
            "options": {
              "path": "outputs/results.json",
              "schema": {
                "mode": "observed"
              },
              "mode": "write",
              "collision_policy": "auto_increment"
            },
            "on_write_failure": "discard"
          }
        ]
      },
      "description": "Output specs. For csv/document/json/text file sinks in runnable web pipelines, options must include path, schema, explicit mode ('write' or 'append'), and explicit collision_policy. Sink schema.fields and custom header keys use carried row names, normally normalized source headers or mapping targets."
    },
    "metadata": {
      "type": [
        "object",
        "null"
      ],
      "additionalProperties": false,
      "description": "Pipeline metadata: {name?, description?}",
      "properties": {
        "name": {
          "type": [
            "string",
            "null"
          ]
        },
        "description": {
          "type": [
            "string",
            "null"
          ]
        }
      }
    }
  },
  "oneOf": [
    {
      "required": [
        "source"
      ]
    },
    {
      "required": [
        "sources"
      ]
    }
  ],
  "required": [
    "nodes",
    "edges",
    "outputs"
  ],
  "additionalProperties": false
}
```
