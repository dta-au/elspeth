# `get_blob_metadata` — exact artifact metadata lookup

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only. **Overall confidence: High for inspected contracts and local reproductions.** Nothing proposed here has been implemented. No live provider, PostgreSQL or browser evidence exists for this tool.

## 1. Purpose and usage

Use this tool when the orchestrator already has a blob ID and needs that blob's current lifecycle status, byte size, MIME type, filename or hash before reading or binding it. It does not disclose file contents or host storage paths. It needs the web session engine and the exact current session ID. An ID by itself grants no access to another session. Composer MCP does not expose it.

It is not a discovery entry point: it cannot find an ID. IDs come from [`list_blobs`](list_blobs.md), [`list_composer_blobs`](list_composer_blobs.md), `create_blob`, a `source.inline_blob` result, or a content reader's `blob_id`. On success it returns the database's own canonical `id`. A REST endpoint with the same name exists for the browser (see §3.1). It is a different authority with different semantics.

## 2. Contract

The only argument is `blob_id: string`. Additional arguments are rejected at two layers. The declared JSON Schema has `additionalProperties: false`, but the dispatcher only enforces it when `validate_arguments`/`raise_schema_argument_errors` is set ([`_dispatch.py#L908-L937`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L908)). Both loop routes set it. The handler's own [`BlobIdArgumentsModel`](../../../../src/elspeth/web/composer/tools/blobs.py#L115) is `extra="forbid"` and raises `ToolArgumentError` for any caller. The schema declares no `format: "uuid"`.

The [UUID-shape helper](../../../../src/elspeth/web/composer/tools/blobs.py#L518) returns a repairable failure for strings Python's `UUID()` cannot parse. It accepts canonical lowercase, uppercase, mixed case, braced, hyphenless and `urn:uuid:`-prefixed spellings, and rejects leading whitespace and non-UUIDs. Those classes were measured by introspection, listed in §7.

A success returns `data.id`, `filename`, `mime_type`, `size_bytes`, `content_hash` and `status`. `status` is one of `ready`/`pending`/`error`. `content_hash` may be `null` on a non-ready row, and the [response contract](../../../../src/elspeth/web/composer/tools/blobs.py#L205) admits `null` for any status. A DB CHECK ([`ck_blobs_ready_hash`](../../../../src/elspeth/web/sessions/models.py#L2478)) is what guarantees a well-formed 64-hex hash on `ready` rows. Pending or error metadata can be returned, and callers must not treat it as readable content. The response is an allowlist: `storage_path`, `source_description`, `created_by`, `creation_modality`, `created_from_message_id` and the `creating_*` provenance fields never leave the handler.

It is an uncached `BLOB_DISCOVERY` operation with a dedicated response contract ([`_BLOB_METADATA_RESPONSE`](../../../../src/elspeth/web/composer/tools/blobs.py#L389)). It has no prospective effect domains (measured `ToolEffects(domains=())`), leaves the composition version unchanged ([`_discovery_result`](../../../../src/elspeth/web/composer/tools/_common.py#L1279)) and needs no approval.

Failure cases:

- **Missing session context, an invalid UUID, or no matching row:** each returns `success=false` with a fixed message and **no `error_code`** (see `get_blob_metadata-N1`).
- **Wrong-session lookup:** returns the same not-found result as a missing row, and the message does not echo the ID.
- **Owned row corruption:** `_guard_blob_row_literals` checks the `status`/`created_by`/`mime_type`/`creation_modality` literals and raises `AuditIntegrityError` ([service.py#L2200](../../../../src/elspeth/web/blobs/service.py#L2200)).
- **Invalid producer shapes:** raise `FrameworkBugError` at contract admission.
- **Credential-bearing legacy metadata:** disclosure is refused with `error_code="credential_material_rejected"` ([blobs.py#L682](../../../../src/elspeth/web/composer/tools/blobs.py#L682)).

Reads can be repeated safely, but nothing guarantees that the metadata observed here still holds when the content is accessed later.

## 3. Implementation trace

The [handler](../../../../src/elspeth/web/composer/tools/blobs.py#L658) and its [declaration](../../../../src/elspeth/web/composer/tools/blobs.py#L688) are listed in the module's [`TOOLS_IN_MODULE`](../../../../src/elspeth/web/composer/tools/blobs.py#L2287). That tuple is spread into the [aggregate `_REGISTERED_TOOLS`](../../../../src/elspeth/web/composer/tools/_registry.py#L94), which derives the [blob-discovery handler map](../../../../src/elspeth/web/composer/tools/_registry.py#L149) and [name set](../../../../src/elspeth/web/composer/tools/_registry.py#L162). [`response_contract_for`](../../../../src/elspeth/web/composer/tools/_registry.py#L108) selects the contract. The [model catalogue `get_tool_definitions`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311) emits the definition.

The [public dispatcher `execute_tool`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) runs in this order:

1. The argument credential scan ([L908](../../../../src/elspeth/web/composer/tools/_dispatch.py#L908)).
2. The catalog/snapshot identity check.
3. Optional schema validation.
4. Handler dispatch.
5. `finalize_tool_result`.

The handler then checks session context, then the [UUID helper](../../../../src/elspeth/web/composer/tools/blobs.py#L518), then calls [`_sync_get_blob`](../../../../src/elspeth/web/composer/tools/blobs.py#L499). That function queries the raw string with `blobs_table.c.id == blob_id` ([L502](../../../../src/elspeth/web/composer/tools/blobs.py#L502)) together with `session_id`. The handler builds a new six-key allowlisted payload rather than returning the row, and passes it through `_credential_safe_blob_discovery_result` before `_discovery_result`.

`execute_tool` and `finalize_tool_result` do **not** apply the selected response contract (`_dispatch.py` has no `admit_discovery_result` call). For this tool, the loop callers admit it. Freeform admits it at [`tool_batch.py#L3052-L3053`](../../../../src/elspeth/web/composer/tool_batch.py#L3052), after the `execute_tool` call at [L3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008). The planner admits it at [`pipeline_planner.py#L4911`](../../../../src/elspeth/web/composer/pipeline_planner.py#L4911), and its message serialiser at [L2942](../../../../src/elspeth/web/composer/pipeline_planner.py#L2942) re-admits only when the result arrives unadmitted. Direct callers receive the raw `ToolResult`.

[Audit `finish_success`](../../../../src/elspeth/web/composer/audit.py#L812) records dispatch status separately from semantic tool success: a not-found is a SUCCESS dispatch with `success=false` in the result. The [frontend tool card](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) displays server-owned outcome evidence. [MCP's common-tool allowlist](../../../../src/elspeth/composer_mcp/server.py#L116) omits blob tools.

### 3.1 Route inventory

| Route | How it reaches the tool | Gates / notes |
|---|---|---|
| Canonical emitter | `_REGISTERED_TOOLS` → `_TOOL_DEFS_BY_NAME` → [`get_tool_definitions`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311) (measured 43 definitions; the docstring says 42) | Static; no phase or policy filter. |
| Freeform loop | [`composer_loop_tool_definitions`](../../../../src/elspeth/web/composer/provider_gateway.py#L391) → wire projection (measured 45 tools in both `openai_strict` and `none` dialects, tool present in both) → `tool_batch` `_do_dispatch` → `execute_tool` with `validate_arguments`, `require_data_dir_for_paths` and `raise_schema_argument_errors` all `True` ([L3034-L3036](../../../../src/elspeth/web/composer/tool_batch.py#L3034)) → contract admission ([L3052](../../../../src/elspeth/web/composer/tool_batch.py#L3052)) | Not cacheable (measured `is_cacheable_discovery_tool=False`); a success is not written to the discovery cache ([L3368](../../../../src/elspeth/web/composer/tool_batch.py#L3368)). Session engine and ID come from turn custody. Extra keys raise `ToolArgumentError` and are recorded as ARG_ERROR. |
| Planner | [`PLANNER_DISCOVERY_TOOL_NAMES`](../../../../src/elspeth/web/composer/capability_skill.py#L31) (always advertised by [`PlannerDiscoveryPolicy.initial`](../../../../src/elspeth/web/composer/pipeline_planner.py#L379)) → [`execute_discovery_tool_with_context`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L999) with the request-scoped [`ToolContext`](../../../../src/elspeth/web/composer/pipeline_planner.py#L3422) → state-unchanged assertion ([L4910](../../../../src/elspeth/web/composer/pipeline_planner.py#L4910)) → admission ([L4911](../../../../src/elspeth/web/composer/pipeline_planner.py#L4911)) | This seam does not run `require_no_credential_material_for_tool`; the planner scans arguments for credentials at response parse instead ([L1882](../../../../src/elspeth/web/composer/pipeline_planner.py#L1882)). Schema validation always raises. The information key is `blob.metadata:<raw blob_id>` ([L441-L443](../../../../src/elspeth/web/composer/pipeline_planner.py#L441)). A failed lookup closes that key as *unavailable* ([`with_result`](../../../../src/elspeth/web/composer/pipeline_planner.py#L293), [L5075](../../../../src/elspeth/web/composer/pipeline_planner.py#L5075)); see N2. |
| Async composer operations | `sessions/composer_turn.py` → `composer.compose(...)` ([L1486](../../../../src/elspeth/web/sessions/composer_turn.py#L1486)) → freeform or planner as above | Same gates; no tool-specific async path. Traced to the compose call only. |
| Recovery controls | None | `recovery_controls.py` has no blob reference (`grep -ci blob`: 0). Control: `grep -c 'restore_parked_graph\|handoff_to_user'` returns 6 in the same file. |
| Proposal / approval interception | None | Discovery kind with no effect domains, so it executes immediately and is never staged or replayed as a proposal. |
| Composer MCP | Excluded | `_COMPOSER_TOOL_NAMES` = core discovery + mutation only ([server.py#L117](../../../../src/elspeth/composer_mcp/server.py#L117)); measured `False`; pinned by `test_blob_and_secret_tools_excluded`. MCP *does* expose `get_plugin_assistance`, whose `reference_join` hint names this tool (N5). |
| Direct internal callers | None in production | Grep for `_handle_get_blob_metadata` / `_GET_BLOB_METADATA_DECLARATION` finds only the declaration, the module tuple and `test_blob_response_contracts.py#L243`. The shared helper `_sync_get_blob` has 6 call sites under `src/` (`grep -rn "_sync_get_blob(" src/`): blobs.py L671/L987/L1025 and sources.py L845/L1405/L1915. |
| Redaction / audit persistence | Declarative [`ToolRedaction`](../../../../src/elspeth/web/composer/redaction.py#L3999): `known_argument_keys=("blob_id",)`, unknown keys redacted, `handles_no_sensitive_data=True` ([reason](../../../../src/elspeth/web/composer/redaction.py#L2992)) | `blob_id` is persisted verbatim (snapshot `sensitive_path_count: 0`), even when it was rejected as non-UUID (N6). |
| Frontend | Read-only description map [`toolCallDescriptions.ts#L46`](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L46) ("Looks up a session file's name, size, and status.") → "Looked up" prefix; [`ToolCallCard.tsx#L84`](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) | Label only; the result payload is not rendered. The description understates (it omits MIME type and hash) but does not overclaim. |
| Guidance carriers | Skill [L344](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L344), [L387](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L387), [L664](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L664); [`_SOURCE_CUSTODY_RULES`](../../../../src/elspeth/web/composer/planner_authoring_aids.py#L349) → `build_planner_authoring_aids` → [`prompts.py#L266`](../../../../src/elspeth/web/composer/prompts.py#L266) (catalog context, both routes, only when the custody exemplar renders); [`reference_join` composer hints](../../../../src/elspeth/plugins/transforms/reference_join.py#L972) via `get_plugin_assistance`/schema contract | See §5.1. |
| REST sibling (not a route) | [`GET /api/sessions/{session_id}/blobs/{blob_id}` `get_blob_metadata`](../../../../src/elspeth/web/blobs/routes.py#L374) | A different authority. It takes a `UUID` path parameter (so spellings canonicalise), acquires a `SessionOperationLease(BLOB_READ)` ([L383-L389](../../../../src/elspeth/web/blobs/routes.py#L383)), reads through the fenced `BlobServiceImpl`, and returns the wider [`BlobMetadataResponse`](../../../../src/elspeth/web/blobs/routes.py#L50) (session_id, created_at, created_by, source_description, provenance). `tests/unit/contracts/test_web_blob_fencing.py` fingerprints **this endpoint**, not the composer tool. |
| Tutorial-specific branch | None found | `grep -ic tutorial` = 0 in `tools/blobs.py`, `tools/_dispatch.py`, `tool_batch.py` and `pipeline_planner.py`. Control: the same grep hits `tools/_common.py` (L497, L1424) and `planner_authoring_aids.py`. |

## 4. Alternative paths

A canonical ID that matches a row in the same session succeeds regardless of readiness. A canonical ID from another session, or one with no row, fails without exposing storage details. An invalid UUID produces static repair guidance pointing to `list_blobs`/`list_composer_blobs`/`create_blob`. A credential-bearing filename can block otherwise valid metadata.

The **composer tool**'s metadata access does not enter the session-operation lease, the content custody/recovery fence or `BlobServiceImpl`, and it does not verify the bytes. It is a direct `engine.connect()` read. The same-named REST endpoint does take a `BLOB_READ` lease (§3.1); the two must not be conflated. Callers that need the current bytes must use the content read authority rather than reconstruct filesystem paths from metadata.

## 5. Usability assessment

**BOS-03 — demonstrated admission inconsistency, High confidence:** the UUID helper accepts alternative valid spellings, but `_sync_get_blob` compares the unnormalised string to the canonical text stored in the database. The controlled probe looked up the same ID in canonical lowercase and in uppercase: canonical succeeded and uppercase failed in each of the 1/50/51 fixtures (SQLite; [evidence](../evidence.md)).

The 2026-11-09 probe (§7, P1) replayed every accepted spelling against one stored row on SQLite. The helper admitted uppercase, mixed case, braced, hyphenless and `urn:uuid:` spellings, and each one missed on the not-found arm. The canonical spelling succeeded with `data.id` equal to the stored ID. All five misses are therefore **demonstrated (SQLite)**. They follow from the string equality at [L502](../../../../src/elspeth/web/composer/tools/blobs.py#L502). PostgreSQL text comparison is still unverified, because the PostgreSQL probe needs Docker and was not runnable here.

`wire_blob_inline_ref` instead constructs a `UUID` and looks up `str(UUID(...))`, so the meaning of an accepted UUID depends on the route. Workaround: use the exact ID returned by inventory or create. The defect does not grant cross-session access.

**get_blob_metadata-N1 — demonstrated result-shape inconsistency (introspection), Medium impact:** the three ordinary refusal branches carry no `error_code`:

- `"Blob tools require session context."` ([L667](../../../../src/elspeth/web/composer/tools/blobs.py#L667));
- the UUID-helper message ([L668-L670](../../../../src/elspeth/web/composer/tools/blobs.py#L668));
- `"Blob not found for this session."` ([L673](../../../../src/elspeth/web/composer/tools/blobs.py#L673)).

Measured with no session context, the result was `success=false` with `validation.errors[0] = {component: "rejected_mutation", message: "Blob tools require session context."}` and no code. The sibling secret-context refusal under the same harness carried `error_code: "secret_context_unavailable"`. The singular source binder codes all three of the same branches: `source_binding_context_unavailable`, `source_blob_identity_invalid` (the identical helper message) and `source_blob_unavailable` ([sources.py#L841-L847](../../../../src/elspeth/web/composer/tools/sources.py#L841)). `set_source_from_blobs` does the same for the first two ([L1381](../../../../src/elspeth/web/composer/tools/sources.py#L1381), [L1389](../../../../src/elspeth/web/composer/tools/sources.py#L1389)).

An orchestrator using this tool must parse prose to tell the three causes apart: a missing deployment dependency, a malformed ID and an absent resource.

The standing-graph errors (`no_source_configured` and others) that travel with the failure, and the `rejected_mutation` component name on a read-only refusal, are the intended shared [`_failure_result`](../../../../src/elspeth/web/composer/tools/_common.py#L1399) saved-graph disclosure. That is an *intended restriction*, but its consequence is that the validation summary of a failed lookup reads `is_valid: false` regardless of the lookup.

**get_blob_metadata-N2 — demonstrated planner consequence of BOS-03 (at the planner discovery seam, SQLite; §7 P3):** the planner keys this call as `blob.metadata:<raw blob_id>` ([L441-L443](../../../../src/elspeth/web/composer/pipeline_planner.py#L441)). A non-canonical spelling misses, the miss is a semantic failure rather than an argument rejection (`execute_one_discovery` returns `information_resolved=True` for every admitted result and `False` only on the argument-rejection arm; consumed at [L5067](../../../../src/elspeth/web/composer/pipeline_planner.py#L5067)), and [`with_result(available=False)`](../../../../src/elspeth/web/composer/pipeline_planner.py#L293) then records the key as *unavailable*, after which [`covers()`](../../../../src/elspeth/web/composer/pipeline_planner.py#L268) treats it as closed. As a result, an existing blob can be recorded in the request's information manifest as proven unavailable under one spelling, while the canonical spelling stays a separate, still-open key. Invalid (non-UUID) IDs follow the same path: they close their key as unavailable instead of counting toward the argument-rejection budget. The probe drove `execute_discovery_tool_with_context` and `admit_discovery_result`, the two calls `execute_one_discovery` makes, and the planner's own `_resolved_discovery_information_keys` and `with_result`. The `ToolArgumentError` → `False` arm selection was mirrored from the planner rather than run through the async planner loop. The probe measured `covers()`/`supplies()`. It did not measure whether the loop's dispatch filter then skips a repeated call.

**get_blob_metadata-N3 — source-established latent hazard (no reachable path):** [`_sync_get_blob`](../../../../src/elspeth/web/composer/tools/blobs.py#L499) defaults `session_id=None`, and `None` skips the session predicate entirely. All 6 production callers pass a session ID they have already checked for `None` (§3.1). The session fence is therefore held by caller discipline, not by the helper.

**get_blob_metadata-N4 — demonstrated test gap (by mutation, two test files; §7 P4):** the credential-refusal branch ([L682-L684](../../../../src/elspeth/web/composer/tools/blobs.py#L682)) has no direct test. The branch itself works. A stored row named `"sk-" + "A"*48 + ".txt"` returned `error_code="credential_material_rejected"` with `data` set to `None` and no echo of the token, and a clean filename on the same engine succeeded. Disabling the branch in a lane-private copy left `test_tools_blobs.py` green (128 passed) and `test_blob_response_contracts.py` green (83 passed). A control mutant run on the first file went red. Instrument: the surface string `composer_blob_metadata` occurs 3 times in `tools/blobs.py` and 0 times under `tests/`. Credential-refusal tests exist for `get_blob_content` (positive control, `test_tools_blobs.py#L2824`). Other test files that name the tool were grepped and have no credential-filename metadata case, but the search is not exhaustive.

The lack of content inspection is an intended separation. A returned `ready` status alone does not prove that the backing bytes still exist or match the stored hash.

*Hypothesis, unverified:* a lookup interleaved with `update_blob`/`delete_blob` can observe metadata that is stale by the time a subsequent read runs. That is consistent with the stated no-snapshot contract, and no torn single-row read is claimed.

### 5.1 Guidance parity

- **Declaration and argument description.** The description lists the six returned keys accurately, which is what the [envelope teaching gate](../../../../tests/unit/web/composer/test_tool_result_envelope_gate.py#L1) counts as teaching. It does not say that `status` may be `pending`/`error`, that `content_hash` is `null` on non-ready rows, that the result is not a content or readiness proof, or where an ID comes from. The `blob_id` description is just `"Blob ID."` with no UUID format ([L697](../../../../src/elspeth/web/composer/tools/blobs.py#L697)). The sibling `wire_blob_inline_ref` says `"format": "uuid"` and "Ready blob ID…" ([L1124](../../../../src/elspeth/web/composer/tools/blobs.py#L1124)). Nothing tells the model that the exact returned spelling is required (BOS-03).
- **"Copy it verbatim" exists only in authoring aids.** `_SOURCE_CUSTODY_RULES` ([L349-L350](../../../../src/elspeth/web/composer/planner_authoring_aids.py#L349)) says blob IDs come only from `list_blobs`, `list_composer_blobs`, `create_blob` and `get_blob_metadata`, "Copy it verbatim", and that rule mitigates BOS-03. It is delivered on both routes via the catalog context, but only when the custody exemplar renders ([L2700](../../../../src/elspeth/web/composer/planner_authoring_aids.py#L2700)). `pipeline_composer.md` never says to copy blob IDs verbatim (grep `verbatim`: hits L589, L790, L943, L975 concern drafts and prompts, not IDs). The rule's list is also incomplete and partly circular: `get_blob_metadata` only echoes an ID it was given, and the `blob_id` returned by `source.inline_blob`, `read_attachment` and `get_blob_content` is not listed.
- **`pipeline_composer.md` L664** tells the model, for a `reference_join` table, to "use `get_blob_metadata` and wire … with `wire_blob_inline_ref`". The [`reference_join` hint](../../../../src/elspeth/plugins/transforms/reference_join.py#L972) says "discover an uploaded ready table with list_blobs and get_blob_metadata". Neither says what fact `get_blob_metadata` adds. After `list_blobs` (which already returns `status`, `size_bytes` and `mime_type`) the only new field is `content_hash`, and `wire_blob_inline_ref` re-reads status and pins the hash itself ([L1025-L1032](../../../../src/elspeth/web/composer/tools/blobs.py#L1025)). `list_composer_blobs` (ready-only, with hash) is the declared inline-authoring inventory. The guidance is not wrong; it adds an unexplained turn and points away from the purpose-built inventory (source-established inefficiency, low severity).
- **`pipeline_composer.md` L387** ("Check file size with `list_blobs` or `get_blob_metadata`") matches the handler. **L344** lists the tool in the autogenerated inventory.
- **Lifecycle semantics are untaught.** The skill says to "choose a ready blob" (L656-L658), but neither skill explains what `pending` or `error` means for this tool's result or what to do next. Every `pending` hit in `pipeline_composer.md` is review-related. The binding tools enforce readiness (`set_source_from_blob` refuses non-ready, [sources.py#L849](../../../../src/elspeth/web/composer/tools/sources.py#L849)), so the model learns it only through a refusal.
- **`pipeline_capabilities.md`** does not name the tool (grep `list_blobs\|get_blob_metadata`: 0; control: 5 `list_blobs` hits in `pipeline_composer.md`). L38-L40 says only "Use blob and secret-reference discovery when the request needs them". For the planner surface this is acceptable because the tool is advertised and described by declaration; noted, not a gap.
- **MCP mismatch (N5).** The `reference_join` hint names `list_blobs`, `get_blob_metadata` and `wire_blob_inline_ref`, and is delivered through `get_plugin_assistance`, which Composer MCP exposes while all three named tools are absent there (measured). MCP clients are told to call tools that surface does not offer.
- **Redaction reason text (N6).** It states "blob_ids are UUIDs assigned by the composer service and contain no operator payload" ([L2994-L2996](../../../../src/elspeth/web/composer/redaction.py#L2994)). The schema admits any string, and a rejected non-UUID value is persisted verbatim in audit arguments, after passing the argument credential scan. The handler test deliberately keeps that value out of the LLM-facing message (`test_get_blob_metadata_rejects_invalid_blob_id_without_echoing_value`). The reason text overclaims a property the schema does not enforce. Any privacy impact is a hypothesis.

## 6. Structural remediation

Invariant: one admitted blob identity denotes the same resource across all tools. Put canonical UUID parsing in a single owned boundary value or helper and pass canonical IDs into every repository operation. Apply it to the metadata, source binding, inline wiring, read, update and deletion routes. Do not add per-handler string comparisons or per-handler `str(UUID(...))`. The alternative is to reject noncanonical spellings consistently, and to document that as an explicit supported boundary.

Acceptance: canonical, uppercase, mixed-case, braced, hyphenless and `urn:uuid:` valid representations either resolve identically within one session, or every noncanonical form is rejected at admission with the same typed error. Invalid IDs and IDs from other sessions never reach privileged reads. The planner's information key uses the same canonical identity, so one blob cannot be both open and unavailable.

Compatibility risk is Low when accepted UUIDs are normalised; a newly strict rejection policy would require reviewing callers. Separately, preserve the content authority's hash/read fencing and the metadata response allowlist. Do not let the REST endpoint's wider response or lease semantics leak into the LLM surface by unifying the two without a decision.

### 6.1 Simple enhancements

1. **Typed refusal codes.**
   - *What:* add `error_code` to the three refusals, reusing the sibling vocabulary where it fits: `blob_context_unavailable`, `blob_id_invalid`, `blob_not_found`.
   - *Where:* [blobs.py#L667](../../../../src/elspeth/web/composer/tools/blobs.py#L667), [#L668-L670](../../../../src/elspeth/web/composer/tools/blobs.py#L668), [#L673](../../../../src/elspeth/web/composer/tools/blobs.py#L673).
   - *Why:* N1.
   - *Acceptance:* each branch yields its distinct code in `validation.errors[0].error_code`; messages still do not echo the ID; the existing tests at `test_tools_blobs.py#L182-L230` still pass. Those tests pin only message substrings. `test_get_blob_metadata_not_found_does_not_echo_blob_id` asserts only `"not found"`, as the §7 P4 control mutant measured, so the new code needs its own assertion.
2. **Disclose ID spelling and lifecycle in the declaration.**
   - *What:* state that the ID must be the exact value returned by a blob tool; that `pending`/`error` rows are returned with `content_hash: null` possible; and that the result is not a content or readiness proof.
   - *Where:* [blobs.py#L688-L701](../../../../src/elspeth/web/composer/tools/blobs.py#L688).
   - *Why:* §5.1 and BOS-03 containment until §6 lands.
   - *Acceptance:* the pinned dict at [`test_tool_declarations.py#L930`](../../../../tests/unit/web/composer/test_tool_declarations.py#L930) is updated deliberately; the skill inventory `--check` stays green. Note the prompt-cache prefix change.
3. **Make the session scope mandatory in the helper.**
   - *What:* change `_sync_get_blob(..., session_id: str | None = None)` to a required `session_id: str`.
   - *Where:* [blobs.py#L499](../../../../src/elspeth/web/composer/tools/blobs.py#L499).
   - *Why:* N3; guard at the consumer.
   - *Acceptance:* mypy passes at all 6 callers, and no call site can omit the session.
4. **Missing tests.**
   - (a) A credential-shaped filename on a stored row makes `get_blob_metadata` return `credential_material_rejected` with no `data` (N4).
   - (b) A test pinning *this tool's* non-canonical-spelling policy. Today it would assert the miss as a known red, or the chosen policy once §6 lands. Neighbouring tools already have spelling tests (`test_implicit_decisions.py#L173`, `test_source_rejection_contracts.py#L517`); this tool has none.
   - *Acceptance:* the test goes red when the credential refusal is removed, or when the spelling policy changes without the test changing.
5. **Bounded identity slice (optional precursor to §6).**
   - *What:* make the UUID helper return the canonical string (or a typed value) and route all six `_sync_get_blob` callers through it.
   - *Where:* [blobs.py#L511-L531](../../../../src/elspeth/web/composer/tools/blobs.py#L511).
   - *Why:* closes BOS-03 at one owner.
   - *Caveat:* this changes the `@observation_boundary` invariant string. The binding churn is an honest obligation, with no per-handler workaround.
   - *Acceptance:* the uppercase probe in [evidence](../evidence.md) succeeds for every accepted spelling.
6. **Redaction reason accuracy.**
   - *What:* reword the reason so it does not claim the argument is always a UUID, or make the claim true via enhancement 5 plus a schema `format`.
   - *Where:* [redaction.py#L2992-L2996](../../../../src/elspeth/web/composer/redaction.py#L2992).
   - *Why:* N6.
   - *Acceptance:* the reason text matches enforced admission, and the snapshot hash in `redaction_policy_snapshot.json` is updated deliberately.

## 7. Evidence, gaps, and caveats

Read the [metadata exclusion, invalid-ID and wrong-session tests](../../../../tests/unit/web/composer/test_tools_blobs.py#L141), the tampered-status escalation test (L241), and the producer response-contract tests ([`test_blob_response_contracts.py#L238`](../../../../tests/unit/web/composer/test_blob_response_contracts.py#L238), which cover explicit `null` hash on pending). Those tests were included in the seven-file focused run: exit 0, **448 passed, 43 warnings, 66.95s**. Independent SQL-backed probes confirmed the canonical versus uppercase divergence, with positive controls.

Sheet review, 2026-11-09, read-only introspection against the worktree (`elspeth.__file__` resolved into the worktree):

- **UUID helper acceptance:** canonical, upper, mixed, braced, hyphenless and `urn:uuid:` were accepted; leading-space and non-UUID were rejected.
- **Declaration:** kind `BLOB_DISCOVERY`, handler first line 658, contract identity true, `cacheable=False`, effects `domains=()`.
- **Exposure:** 43 `get_tool_definitions` entries; 45 freeform wire tools per dialect, tool present; MCP exclusion confirmed with `list_sources` as the control.
- **N1:** an in-memory `execute_tool` call with no session context, using test-module fixtures, with the repository root added to `PYTHONPATH`. No database.

Probe runs, 2026-11-09. `$WT` is the audit worktree and `$L` is this tool's lane-private probe directory. Scripts and logs live under `$L` and nothing was written into `$WT`. Every run printed `elspeth.__file__`, and each resolved into the source tree under test (`$WT/src`, `$L/mutant/src` or `$L/control/src`). Each probe below gives its outcome, the command, the exit code and what was observed.

- **P1 — BOS-03, every accepted spelling on SQLite: confirmed.**
  - *Command:* `cd $WT && PYTHONPATH=$WT:$WT/src:$WT/elspeth-lints/src .venv/bin/python $L/probe_gbm.py`, exit 0, log `$L/probe_gbm.log`.
  - *Setup:* one ready row was inserted with a canonical `uuid4()` ID, and `execute_tool("get_blob_metadata", ...)` was called with each spelling.
  - *Observed:* `_blob_id_uuid_validation_error` returned `None` for all six forms. Canonical succeeded with `data.id` equal to the stored ID. Uppercase, mixed case, braced, hyphenless and `urn:uuid:` each returned `success=false` with `"Blob not found for this session."` and no `error_code`.
  - *Controls:* a fresh canonical UUID landed on the same not-found arm. A leading-space form was rejected by the helper and landed on the UUID-repair arm.
- **P2 — BOS-03 on PostgreSQL: not runnable here.** It needs a PostgreSQL testcontainer (Docker), which this run did not permit. PostgreSQL behaviour remains unverified.
- **P3 — N2, planner manifest: confirmed at the seam.** Same script, run and log as P1.
  - *Keys:* `_tool_information_keys` produced different keys for the uppercase and canonical spellings.
  - *Discovery seam:* `execute_discovery_tool_with_context` (a policy catalog view plus its snapshot, with the session engine and session ID) was followed by `admit_discovery_result`. The uppercase ID returned `success=false` without raising and was admitted, so it falls on the `information_resolved=True` arm. `"not-a-uuid"` behaved the same way.
  - *Controls:* an extra key and an integer ID raised `ToolArgumentError`, which is the `False` arm.
  - *Manifest:* `_resolved_discovery_information_keys` followed by `with_result(available=False)` gave `covers(upper)=True`, `supplies(upper)=False` and `covers(canonical)=False`. After the canonical success the canonical key was in `supplied` and the uppercase key in `unavailable`, so one blob was recorded both ways.
  - *Limit:* the arm selection was mirrored from the planner, not driven through the async planner loop.
- **P4 — N4, credential-filename refusal and test gap: confirmed.**
  - *Behaviour (same script as P1):* a ready row named `"sk-" + "A"*48 + ".txt"` returned `error_code="credential_material_rejected"` and `data=None`, and the token was absent from `to_dict()`. A clean filename on the same engine succeeded.
  - *Discrimination:* rerunning the script on the mutant copy exited 1 with exactly the three P4 checks failing (`$L/probe_gbm_mutant.log`).
  - *Mutation:* in lane-private copies of `src`, the mutant replaced `if refusal is not None:` in `_handle_get_blob_metadata` with `if False:` and left the `_credential_safe_blob_discovery_result` call in place. The control changed the not-found message to text without "not found".
  - *pytest form:* `cd $WT && export PYTHONPATH=<src>:$WT/elspeth-lints/src TMPDIR=$L/tmp && .venv/bin/python -m pytest tests/unit/web/composer/test_tools_blobs.py -n 0 -p no:cacheprovider -o pythonpath="<src> $WT/elspeth-lints/src"`.

  | Run | Exit | Result | Log |
  |---|---|---|---|
  | Baseline, `test_tools_blobs.py` | 0 | 128 passed | `$L/pytest_blobs_baseline.log` |
  | Mutant, `test_tools_blobs.py` | 0 | 128 passed | `$L/pytest_blobs_mutant.log` |
  | Control, `test_tools_blobs.py` | 1 | 1 failed: `test_get_blob_metadata_not_found_does_not_echo_blob_id` | `$L/pytest_blobs_control.log` |
  | Mutant, `test_blob_response_contracts.py` | 0 | 83 passed | `$L/pytest_contracts_mutant.log` |

  - *Discarded control:* the first control string still contained "not found", stayed green and was discarded as invalid (`$L/pytest_blobs_control_v1_invalid.log`).
  - *Limits:* the contracts-file mutant run had no control of its own; the import mechanism was proven on `test_tools_blobs.py` with the same environment form. Because the mutation keeps the helper call, the structural consumer check in `test_tool_result_envelope_gate.py` was neither the subject of this probe nor run.

All runs were repeated fail-closed and gave identical results: the probe script exited 0 with 29/29 checks passing, and the pytest runs gave the same exit codes and counts. The repeats ran with `HTTPS_PROXY`/`HTTP_PROXY` pointed at `127.0.0.1:9`, `NO_PROXY` empty and `ELSPETH_DISABLE_NETWORK=1`. The script also ran under a guard that makes `litellm.acompletion`, `litellm.completion`, `provider_gateway._litellm_acompletion` and `pipeline_planner._litellm_acompletion` raise. A positive control confirmed that each of the four raises. None of these probes reaches `compose()`, the planner loop or a provider. No live provider, PostgreSQL or frontend runtime ran. The only pytest runs were the four single-file runs listed under P4 and their fail-closed repeats. The alternative-spelling finding concerns lookup consistency, not evidence that concurrent byte reads are unsafe.

## Runtime argument schema and measured exposure

Synchronous kind: `blob_discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.blobs._handle_get_blob_metadata`](../../../../src/elspeth/web/composer/tools/blobs.py#L658).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | yes |
| Composer MCP | no |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ, so exposure does not certify execution readiness.

This is the canonical input schema as the runtime emits it, before provider wire projection. Public dispatch closes the root object where necessary. Owned argument models and kind-specific or domain guards can impose the additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "blob_id": {
      "type": "string",
      "description": "Blob ID."
    }
  },
  "required": [
    "blob_id"
  ],
  "additionalProperties": false
}
```
