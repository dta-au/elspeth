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

The probe tested uppercase only. Misses for mixed case, braced, hyphenless and `urn:uuid:` spellings follow from the same string equality ([L502](../../../../src/elspeth/web/composer/tools/blobs.py#L502)) and are **source-established**, not replayed. PostgreSQL text comparison is unverified. The helper's acceptance of all those spellings was measured.

`wire_blob_inline_ref` instead constructs a `UUID` and looks up `str(UUID(...))`, so the meaning of an accepted UUID depends on the route. Workaround: use the exact ID returned by inventory or create. The defect does not grant cross-session access.

**get_blob_metadata-N1 — demonstrated result-shape inconsistency (introspection), Medium impact:** the three ordinary refusal branches carry no `error_code`:

- `"Blob tools require session context."` ([L667](../../../../src/elspeth/web/composer/tools/blobs.py#L667));
- the UUID-helper message ([L668-L670](../../../../src/elspeth/web/composer/tools/blobs.py#L668));
- `"Blob not found for this session."` ([L673](../../../../src/elspeth/web/composer/tools/blobs.py#L673)).

Measured with no session context, the result was `success=false` with `validation.errors[0] = {component: "rejected_mutation", message: "Blob tools require session context."}` and no code. The sibling secret-context refusal under the same harness carried `error_code: "secret_context_unavailable"`. The singular source binder codes all three of the same branches: `source_binding_context_unavailable`, `source_blob_identity_invalid` (the identical helper message) and `source_blob_unavailable` ([sources.py#L841-L847](../../../../src/elspeth/web/composer/tools/sources.py#L841)). `set_source_from_blobs` does the same for the first two ([L1381](../../../../src/elspeth/web/composer/tools/sources.py#L1381), [L1389](../../../../src/elspeth/web/composer/tools/sources.py#L1389)).

An orchestrator using this tool must parse prose to tell the three causes apart: a missing deployment dependency, a malformed ID and an absent resource.

The standing-graph errors (`no_source_configured` and others) that travel with the failure, and the `rejected_mutation` component name on a read-only refusal, are the intended shared [`_failure_result`](../../../../src/elspeth/web/composer/tools/_common.py#L1399) saved-graph disclosure. That is an *intended restriction*, but its consequence is that the validation summary of a failed lookup reads `is_valid: false` regardless of the lookup.

**get_blob_metadata-N2 — source-established planner consequence of BOS-03:** the planner keys this call as `blob.metadata:<raw blob_id>` ([L441-L443](../../../../src/elspeth/web/composer/pipeline_planner.py#L441)). A non-canonical spelling misses, the miss is a semantic failure rather than an argument rejection (`execute_one_discovery` returns `information_resolved=True` for every admitted result and `False` only on the argument-rejection arm; consumed at [L5067](../../../../src/elspeth/web/composer/pipeline_planner.py#L5067)), and [`with_result(available=False)`](../../../../src/elspeth/web/composer/pipeline_planner.py#L293) then records the key as *unavailable*, after which [`covers()`](../../../../src/elspeth/web/composer/pipeline_planner.py#L268) treats it as closed. As a result, an existing blob can be recorded in the request's information manifest as proven unavailable under one spelling, while the canonical spelling stays a separate, still-open key. Invalid (non-UUID) IDs follow the same path: they close their key as unavailable instead of counting toward the argument-rejection budget. Not replayed.

**get_blob_metadata-N3 — source-established latent hazard (no reachable path):** [`_sync_get_blob`](../../../../src/elspeth/web/composer/tools/blobs.py#L499) defaults `session_id=None`, and `None` skips the session predicate entirely. All 6 production callers pass a session ID they have already checked for `None` (§3.1). The session fence is therefore held by caller discipline, not by the helper.

**get_blob_metadata-N4 — source-established test gap:** the credential-refusal branch ([L682-L684](../../../../src/elspeth/web/composer/tools/blobs.py#L682)) has no direct test. Instrument: the surface string `composer_blob_metadata` occurs 3 times in `tools/blobs.py` and 0 times under `tests/`; every `get_blob_metadata` test site in `test_tools_blobs.py` (L141–L260) is a non-credential case. Credential-refusal tests exist for `get_blob_content` (positive control, `test_tools_blobs.py#L2824`). The grep is not exhaustive across all indirect fixtures.

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
   - *Acceptance:* each branch yields its distinct code in `validation.errors[0].error_code`; messages still do not echo the ID; the existing tests at `test_tools_blobs.py#L182-L230` still pass.
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

No live provider, PostgreSQL or frontend runtime ran, and no test was run in this review. The alternative-spelling finding concerns lookup consistency, not evidence that concurrent byte reads are unsafe.

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
