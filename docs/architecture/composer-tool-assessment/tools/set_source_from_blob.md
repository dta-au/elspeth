# `set_source_from_blob` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Bind one existing READY session blob as a named file-backed source using authoritative storage identity. This is a custody-aware authoring operation, not a content upload or runtime source read.

Select after inspecting a text/tabular session artifact and choosing its connection and failure policy. Requires session engine/session ID and an available inferred or explicit source plugin. LLM-authored content also requires actual storage root and session operation authority to bind reviewed bytes consistently; that branch raises `AuditIntegrityError` (a fault, not a refusal) when either is absent ([blobs.py:1941](../../../../src/elspeth/web/composer/tools/blobs.py#L1941)), whereas missing session engine/ID is an ordinary `source_binding_context_unavailable` refusal ([sources.py:840](../../../../src/elspeth/web/composer/tools/sources.py#L840)). Public routes supply all four.

## 2. Contract

Required blob_id and on_success strings. source_name defaults source; plugin omitted infers csv/json/text from the six-entry MIME table ([sources.py:270](../../../../src/elspeth/web/composer/tools/sources.py#L270)): `text/csv`→csv, `application/json`→json, three JSONL MIME types→json with `format: jsonl`, `text/plain`→text. A `.tsv`-style filename adds `delimiter: "\t"` for csv when the caller supplied none ([sources.py:280](../../../../src/elspeth/web/composer/tools/sources.py#L280)). An explicit `plugin` (including one equal to the inferred plugin) skips the MIME extras entirely, so an explicit `json` on a JSONL blob loses `format: jsonl` (see set_source_from_blob-N2), while the filename delimiter still applies to an explicit `csv`. Supplying `plugin: null` is rejected at the argument model (`_OmittableString` → `_reject_supplied_null`, [redaction.py:1421](../../../../src/elspeth/web/composer/redaction.py#L1421)); an empty string falls through to inference (`if explicit_plugin:` at [sources.py:853](../../../../src/elspeth/web/composer/tools/sources.py#L853)).

options defaults {}; optional on_validation_failure preserves the existing choice only when omitted, otherwise must be explicit and nonempty ([_common.py:1910](../../../../src/elspeth/web/composer/tools/_common.py#L1910)). Preservation is keyed by source_name, so it carries across a rebind to a different blob or plugin. No description argument is declared; supplying one raises ToolArgumentError (`extra="forbid"`, [redaction.py:1529](../../../../src/elspeth/web/composer/redaction.py#L1529)). Caller options merge with authoritative values at [sources.py:873](../../../../src/elspeth/web/composer/tools/sources.py#L873) in this order: caller options, then MIME extras, then filename delimiter, then `path`, `blob_ref`, `mode: "bind_source"` and (LLM-authored only) `source_authoring`. Later keys win silently: caller `path` and `mode` are overwritten without a note (`path` override is pinned by [test_tools_blobs.py:456](../../../../tests/unit/web/composer/test_tools_blobs.py#L456)), and a caller `format` is overwritten by the MIME-derived `format: jsonl` (set_source_from_blob-N3). Caller `blob_ref`, `blobs` and `source_authoring` (other than an exact echo of the stored block) are refused instead.

Result adds `data.source_blob` (`blob_id`, `filename`, `mime_type`, `size_bytes`, `content_hash`; no storage path — [sources.py:330](../../../../src/elspeth/web/composer/tools/sources.py#L330)) and, when echoed server-owned metadata was dropped, `data.server_owned_metadata_note`; plus the applied source echo, validation and a new version. It does not create/update the blob, execute a source, or make a provider call. Repeated binding is not a no-version-change replay contract: an identical rebind advanced version in the in-memory probe (§7).

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance; unlike `set_source`, it receives no pre-proposal dry run (set_source_from_blob-N1). Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/sources.py:1671](../../../../src/elspeth/web/composer/tools/sources.py#L1671) — tool declaration: description, JSON schema, `BLOB_MUTATION` kind, `augments_on_failure=True`.
- [src/elspeth/web/composer/tools/sources.py:1195](../../../../src/elspeth/web/composer/tools/sources.py#L1195) — handler: argument model, source-name check, echo normalization of stored review rows/`source_authoring`, resolver-owned review guard, S3 endpoint policy (before and after resolution), failure-choice resolution, `SourceSpec` construction without `description` ([sources.py:1317](../../../../src/elspeth/web/composer/tools/sources.py#L1317)), review reconciliation.
- [src/elspeth/web/composer/tools/sources.py:797](../../../../src/elspeth/web/composer/tools/sources.py#L797) — shared `_resolve_source_blob` resolver (also called by `set_pipeline` `source.blob_id`): reserved-key refusals, context check, UUID parse then raw-text lookup, READY check, plugin inference, guarantee-stamp guard, authoritative merge, canonical review check, plugin policy and prevalidation.
- [src/elspeth/web/composer/tools/sources.py:882](../../../../src/elspeth/web/composer/tools/sources.py#L882) — LLM-authored branch only: locked metadata+bytes read, hash-change conflict, UTF-8 decode, pending `invented_source` staging and CSV-derived `guaranteed_fields`.
- [src/elspeth/web/composer/tools/blobs.py:499](../../../../src/elspeth/web/composer/tools/blobs.py#L499) and [src/elspeth/web/composer/tools/blobs.py:1930](../../../../src/elspeth/web/composer/tools/blobs.py#L1930) — unlocked raw-text metadata lookup; locked custody read, which canonicalizes the ID through `UUID(...)` in the blob service ([service.py:3278](../../../../src/elspeth/web/blobs/service.py#L3278)) and maps a missing file to `(record, None)` ([blobs.py:1959](../../../../src/elspeth/web/composer/tools/blobs.py#L1959)).
- [src/elspeth/web/composer/tools/_common.py:3500](../../../../src/elspeth/web/composer/tools/_common.py#L3500) — source prevalidation through the profile adapter; [_common.py:3305](../../../../src/elspeth/web/composer/tools/_common.py#L3305) strips `blob_ref`, `source_authoring` and the `bind_source` `mode` before plugin config validation.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces, including this tool; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

### 3.1 Route inventory

| Route | Reaches handler or helper? | Gate / policy | Notes |
|---|---|---|---|
| Canonical registry → `execute_tool` | Handler | Declared kind `BLOB_MUTATION`; dispatch credential guard ([_dispatch.py:908](../../../../src/elspeth/web/composer/tools/_dispatch.py#L908)) | Not in `_MUTATION_TOOLS`; measured (§7) |
| Freeform compose loop, auto-apply | Handler via [tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) | Emitted by `composer_loop_tool_definitions` (both dialects) | Full session/data-dir/operation context |
| Freeform loop, `trust_mode == "explicit_approve"` | Proposal staged at [tool_batch.py:2179](../../../../src/elspeth/web/composer/tool_batch.py#L2179); **no** pre-proposal `execute_tool` preview, because the preview is gated on `tool_name in _MUTATION_TOOLS` ([tool_batch.py:2057](../../../../src/elspeth/web/composer/tool_batch.py#L2057)) | Approval gate [tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728) includes this tool (`is_mutation_tool` true, not blob-store-only) | Model receives `status: APPROVAL_REQUIRED` with no admission verdict ([tool_batch.py:2222](../../../../src/elspeth/web/composer/tool_batch.py#L2222)) |
| Proposal custody | `proposal_blob_reference_ids` reads raw `blob_id` ([proposal_blob_refs.py:157](../../../../src/elspeth/web/sessions/proposal_blob_refs.py#L157)); `validate_proposal_blob_references` raw-text lookup ([proposal_blob_refs.py:174](../../../../src/elspeth/web/sessions/proposal_blob_refs.py#L174)) | Requires owned READY blob at proposal creation; pending proposal retains blob against update/delete | Same BOS-03 raw-text pattern |
| Proposal accept (REST) | Handler via [proposals.py:491](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L491) with `validate_arguments=True`, `require_data_dir_for_paths=True` | Required controls wired after a valid advancing result ([proposals.py:522](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L522)); non-advancing failure auto-rejects | Required controls first appear here, not on the reviewed card |
| `set_pipeline` `source.blob_id` | Helper `_resolve_source_blob` ([sessions.py:1083](../../../../src/elspeth/web/composer/tools/sessions.py#L1083)), `_RejectionOperation.CANDIDATE`, `source_name="source"` only | Legacy default source only; preserves `description` | Shares inference, merge order and BOS-03 |
| Planner (`planner_tool_definitions`) | No | Not emitted (measured inventory) | Planner binds only through `set_pipeline` `source.blob_id` |
| Recovery controls | No | — | No reference in `recovery_controls.py` (grep) |
| Composer MCP | No | Not in `_build_tool_defs()` | — |
| Run admission | Consumes stored `blob_ref`/`path` ([execution/service.py:1428](../../../../src/elspeth/web/execution/service.py#L1428), [execution/service.py:2486](../../../../src/elspeth/web/execution/service.py#L2486)) | Re-resolves custody | Stored `blob_ref` is the DB-canonical `blob["id"]` |
| Frontend | [toolCallDescriptions.ts:88](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L88) via [ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) | — | "Wires a session file as the pipeline's data source." |
| Advertised by other text | Plugin assistance ([csv_source.py:713](../../../../src/elspeth/plugins/sources/csv_source.py#L713), [null_source.py:84](../../../../src/elspeth/plugins/sources/null_source.py#L84), [azure_blob_source.py:465](../../../../src/elspeth/plugins/sources/azure_blob_source.py#L465), [dataverse.py:1159](../../../../src/elspeth/plugins/sources/dataverse.py#L1159)); repair text ([composition_completion.py:298](../../../../src/elspeth/web/composer/composition_completion.py#L298), [generation.py:834](../../../../src/elspeth/web/composer/tools/generation.py#L834), [generation.py:1873](../../../../src/elspeth/web/composer/tools/generation.py#L1873), [no_tool_policy.py:1242](../../../../src/elspeth/web/composer/no_tool_policy.py#L1242)); runtime refusal ([execution/service.py:3852](../../../../src/elspeth/web/execution/service.py#L3852)) | — | See §5.1 |

No tutorial-specific branch was found for this tool (grep of the handler, resolver and callers above).

## 4. Alternative paths

Missing context, malformed/foreign/missing blob, non-READY state, unsupported MIME without override or invalid plugin options produces structured refusal. Refusal codes: `source_binding_context_unavailable`, `source_blob_identity_invalid`, `source_blob_unavailable`, `source_blob_plugin_required`, `source_blob_changed`, `source_blob_encoding_invalid`, `source_data_contract_required`, `interpretation_requirements_invalid`, `plugin_options_invalid`, `failure_route_required`, `aws_s3_endpoint_url_not_allowed`, `review_reconciliation_failed`, plus plugin-policy/prevalidation codes. `source_blob_unavailable` covers three distinct conditions at five sites — not found ([sources.py:847](../../../../src/elspeth/web/composer/tools/sources.py#L847), [sources.py:896](../../../../src/elspeth/web/composer/tools/sources.py#L896)), not READY ([sources.py:850](../../../../src/elspeth/web/composer/tools/sources.py#L850), [sources.py:898](../../../../src/elspeth/web/composer/tools/sources.py#L898)) and storage file missing behind a READY row ([sources.py:900](../../../../src/elspeth/web/composer/tools/sources.py#L900)) — with different recovery actions (set_source_from_blob-N4).

For model-authored content, metadata then locked bytes are compared; changed hash returns source_blob_changed with retry guidance, missing file/invalid UTF-8 refuses, and authoritative interpretation requirements/derived guarantees are staged. User-uploaded content is never read at bind time, so its missing bytes are discovered only at run admission. User-uploaded content does not receive an invented-source review merely because a model binds it. An override still passes plugin/profile/policy admission. After building the SourceSpec, review reconciliation may reject atomically. A valid binding can remain graph-incomplete; runtime re-resolves custody. MCP omits this web blob surface.

## 5. Usability assessment

**BOS-03 — demonstrated for this tool (in-memory dispatch).**
`_resolve_source_blob` validates UUID parseability but passes the original text
to `_sync_get_blob` at `sources.py:842–845`. The in-memory dispatch probe (§7)
bound the canonical ID successfully and refused the uppercase spelling of the
same ID with `source_blob_unavailable` / "Blob '…' not found." The proposal
custody check repeats the raw-text comparison
([proposal_blob_refs.py:174](../../../../src/elspeth/web/sessions/proposal_blob_refs.py#L174)).
The plural sibling lowercases only for duplicate detection
([sources.py:1391](../../../../src/elspeth/web/composer/tools/sources.py#L1391)) and still looks up raw text.
Use the exact canonical ID returned by discovery. A common owned
BlobId admission boundary should canonicalize accepted representations before
lookup, or consistently refuse noncanonical ones, with valid/invalid/foreign
identity controls across metadata, inspection and source binding.

**SG-06 — source-established responsibility split; high confidence.** MIME inference, delimiter handling, source-row guarantees, provenance and review staging are concentrated in the Composer binding adapter. The locked LLM-authored read is a strong existing integrity mechanism and a reference for other readers. Its existence does not prove every other inspection route holds the same custody boundary. **Intended restriction:** new bindings require an explicit failure decision and cannot bypass review with caller-supplied metadata. **Usability limitation (demonstrated in-memory):** rebinding replaces the source with `description=None`, and the tool rejects a `description` argument. For the default `source`, `set_pipeline` with `source.blob_id` is the only route that sets a blob-backed description; a **named** blob-backed source has no description route at all, because `set_pipeline` named sources exclude `blob_id` ([redaction.py:1777](../../../../src/elspeth/web/composer/redaction.py#L1777)), `set_source` refuses `blob_ref`, and `patch_source_options` patches options only (source-established from the argument models). Workaround: retain author-owned options deliberately and inspect the returned source echo.

**set_source_from_blob-N1 — source-established; explicit-approve proposals skip the dry run that `set_source` receives.** The pre-proposal `execute_tool` preview runs only for `tool_name in _MUTATION_TOOLS` ([tool_batch.py:2057](../../../../src/elspeth/web/composer/tool_batch.py#L2057)); this tool is in `_BLOB_MUTATION_TOOLS` (measured). Neither tool's preview blocks staging on an ordinary failed result or a `ToolArgumentError`; a preview that raises any other exception does block `set_source` staging ([tool_batch.py:2119](../../../../src/elspeth/web/composer/tool_batch.py#L2119)), so a context/integrity fault surfaces at staging for `set_source` but only on accept for this tool. The other difference is: a `set_source` proposal whose preview is valid is re-staged as a `set_pipeline` proposal including required controls, while a `set_source_from_blob` proposal is always staged bare. Required controls are then wired on accept ([proposals.py:522](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L522)), so the user's reviewed card can omit components that acceptance adds. For both tools the model learns only `APPROVAL_REQUIRED`; a binding that will refuse (unsupported MIME, non-UTF-8 LLM-authored bytes, missing failure choice) is discovered only when the user accepts and the proposal auto-rejects. Whether the exclusion is deliberate (the locked read may heal crash leftovers under a write guard, [service.py:3281](../../../../src/elspeth/web/blobs/service.py#L3281)) is unverified. Same gate affects the other approval-gated blob mutations.

**set_source_from_blob-N2 — demonstrated bind-time option shape; runtime consequence source-established.** An explicit `plugin` takes the branch at [sources.py:853](../../../../src/elspeth/web/composer/tools/sources.py#L853) and leaves `mime_extra` empty. In-memory: an `application/x-jsonlines` blob named `rows.ndjson` bound with `plugin: "json"` succeeded with no `format` key; the inferred bind of the same blob carried `format: jsonl`. The JSON source auto-detects JSONL only from a `.jsonl` suffix ([json_source.py:223](../../../../src/elspeth/plugins/sources/json_source.py#L223)) and the stored path is `{blob_id}_{filename}` ([service.py:2480](../../../../src/elspeth/web/blobs/service.py#L2480)), so that source would parse JSONL as a JSON array at runtime (not run). Asymmetric with the filename delimiter rule, which keys on the final plugin.

**set_source_from_blob-N3 — demonstrated; MIME-derived `format` silently overrides a caller's explicit `format`.** Merge order `**caller_options, **mime_extra` ([sources.py:873](../../../../src/elspeth/web/composer/tools/sources.py#L873)) bound `format: "json"` as `format: "jsonl"` with `success=true` and no note. The delimiter rule explicitly never clobbers a caller value ([sources.py:295](../../../../src/elspeth/web/composer/tools/sources.py#L295)); the format rule does.

**set_source_from_blob-N4 — source-established result-shape ambiguity and fault softening.** One code, `source_blob_unavailable`, covers not-found, not-READY and storage-file-missing. The last is owned-storage loss behind a READY row, which the blob service treats as `BlobContentMissingError`; the locked reader converts `FileNotFoundError` to `(record, None)` ([blobs.py:1959](../../../../src/elspeth/web/composer/tools/blobs.py#L1959)) and the tool returns an ordinary refusal (BOS-02 class, LLM-authored branch only). The agent cannot distinguish "wrong ID", "wait for READY" and "storage fault; rebinding cannot help".

**set_source_from_blob-N5 — hypothesis (handler half demonstrated); stored guarantee-stamp echo is not scoped to blob identity.** `_planner_guarantee_stamp_error` tolerates a supplied observed-mode `guaranteed_fields` that hash-matches the stored stamp of the same `source_name` ([sources.py:739](../../../../src/elspeth/web/composer/tools/sources.py#L739)) without comparing `blob_ref`. In-memory, with a stamp hand-injected on a source bound to uploaded blob A (no resolved `source_data_contract` row), rebinding to uploaded blob B with the same stamp succeeded and kept the stamp; a different stamp was refused, and the same stamp with no stored counterpart was refused. Design text frames the acknowledgement as a forward-looking promise about demanded fields ([interpretation_state.py:3380](../../../../src/elspeth/web/interpretation_state.py#L3380); `pipeline_composer.md:853` "forward-looking promise"), so carrying it across files may be intended. Unverified: the full flow with a genuinely resolved requirement, and whether an echoed stamp without its requirement row leaves demand silently satisfied (`current_source_data_contract_demand` disregards only fields backed by a resolved row, [interpretation_state.py:1441](../../../../src/elspeth/web/interpretation_state.py#L1441)). Not a demonstrated defect.

### 5.1 Guidance parity

Instrument: `grep -n set_source_from_blob` over both skill files, `prompts.py`, `pipeline_planner.py`, `provider_gateway.py`, `recovery_controls.py`. Control: `set_source_from_blobs` and `create_blob` match in `pipeline_composer.md` (3 and 8 lines) but return 0 in `pipeline_capabilities.md`, so the 0 for this tool in the capabilities skill reflects a skill that names no blob tools, not a broken grep. The capabilities skill mentions blobs only generically (`pipeline_capabilities.md` lines 38–40) and lists `blob_id` in the `source` field inventory (line 247), which applies to `set_pipeline`, not this tool. `prompts.py`, `pipeline_planner.py` and `recovery_controls.py` do not name the tool.

- **G1 — wrong guidance in plugin assistance (GS-03 class).** [csv_source.py:713](../../../../src/elspeth/plugins/sources/csv_source.py#L713): "set_pipeline and set_source_from_blob default to discard when it is omitted." In-memory, a new source without `on_validation_failure` refused with `failure_route_required`; the resolver never supplies a default ([_common.py:1918](../../../../src/elspeth/web/composer/tools/_common.py#L1918)). The text pushes the model into a refusal. Neither skill states the explicit-failure-choice rule (grep for `on_validation_failure` in `pipeline_composer.md`: no match; control `description` at line 221 matches); the only correct statement is the argument description.
- **G2 — skill contradicts the tool on descriptions.** `pipeline_composer.md:221–224` says every source accepts an optional `description` and to supply/refresh it whenever a step is created or changed. This tool rejects the argument and drops an existing description on rebind (SG-06).
- **G3 — the declaration description underclaims and omits preconditions.** [sources.py:1675](../../../../src/elspeth/web/composer/tools/sources.py#L1675) says "Wire a blob as the pipeline source … infers the source plugin from its MIME type." It does not mention: READY-only, named sources (`source_name`), the six inferable MIME types, that an explicit `plugin` disables MIME extras, the explicit failure-choice requirement for new sources, the pending `invented_source` review automatically staged for LLM-authored blobs, the description loss, or the `data.source_blob` result. The `options` description says "merged with blob path" but not that caller `path`, `mode` and `format` are silently overwritten while `blob_ref`/`blobs`/`source_authoring` refuse.
- **G4 — inconsistent tool grouping.** `pipeline_composer.md:342` lists this tool under "Build / edit" while line 344 lists its plural sibling under "Blobs". Minor, but the plural tool is the one taught as a blob tool.
- **G5 — "stage" ambiguity.** [csv_source.py:709](../../../../src/elspeth/plugins/sources/csv_source.py#L709) tells the model to "bind the exact artifact as a blob-backed source, stage invented_source on source.options.interpretation_requirements"; this tool stages that row itself ([sources.py:919](../../../../src/elspeth/web/composer/tools/sources.py#L919)). `pipeline_composer.md:767` correctly says "keep the backend-staged `invented_source` row". The plugin hint can be read as an instruction to author the row by hand.
- **Parity confirmed:** `pipeline_composer.md:637–640` (create_blob plus immediate binding), `:767–771` (pending review bound to exact blob/path; identical content hash on a new blob preserves accepted proof, matching [interpretation_state.py:3342](../../../../src/elspeth/web/interpretation_state.py#L3342) and [test_source_interpretation_requirement_guard.py:514](../../../../tests/unit/web/composer/test_source_interpretation_requirement_guard.py#L514)), `:544`/`:549` (`server_owned_metadata_note`, `source_blob`) and `:845–866` (no planner-authored stamps on uploaded data) match the handler.
- **UI copy:** "Wires a session file as the pipeline's data source" is accurate but singular-source; for a named source the proposal card does not say which root is replaced.

## 6. Structural remediation

Make blob-to-source lowering a shared, typed adapter: immutable blob version plus plugin-owned source capability/config contract in, admitted SourceSpec plus review obligations out. Reuse it for bulk and incremental authoring rather than copy the MIME/provenance policy. Add an explicit source-description preservation/override policy across binding routes. Acceptance: upload vs LLM-authored modalities remain distinguishable; unsupported MIME and forged owned roots refuse before a write; metadata/bytes changing during binding returns a recoverable conflict; rebind and set_pipeline use the same source contract; failed reconciliation preserves prior state. Compatibility risk medium/high for existing blob/review metadata; preserve refusal contracts and never silently reclassify old provenance.

For set_source_from_blob-N1, decide per approval-gated blob mutation whether a side-effect-free admission preview is possible (a metadata-only preview for uploaded blobs needs no locked read) and, where it is, run it through the same required-control finalization as `set_source` so the reviewed card equals the accepted state. If a preview cannot be made side-effect free, say so in the `APPROVAL_REQUIRED` payload. This belongs with R4/R5 and should be decided once for all blob mutations.

### 6.1 Simple enhancements

1. **Apply MIME extras when the explicit plugin equals the inferred plugin** (set_source_from_blob-N2). Where: [sources.py:853](../../../../src/elspeth/web/composer/tools/sources.py#L853). Why: `plugin: "json"` on a JSONL blob silently loses `format: jsonl`. Acceptance: explicit `json` on each JSONL MIME binds `format: jsonl`; explicit `text` on a JSONL blob gets no `format`; existing [test_tools_blobs.py:388](../../../../tests/unit/web/composer/test_tools_blobs.py#L388) still passes.
2. **Stop silently clobbering caller `format`** (set_source_from_blob-N3). Where: [sources.py:873](../../../../src/elspeth/web/composer/tools/sources.py#L873). Why: an explicit caller choice is replaced with no signal. Pick one: honour the caller value (as the delimiter rule does) or refuse the conflict with `plugin_options_invalid`. Acceptance: `format: "json"` on a JSONL MIME either binds `json` or refuses; it never binds `jsonl` with `success=true` and no note.
3. **Disclose server-overwritten keys.** Where: merge at [sources.py:873](../../../../src/elspeth/web/composer/tools/sources.py#L873) and result at [sources.py:1335](../../../../src/elspeth/web/composer/tools/sources.py#L1335). Why: caller `path`/`mode` are dropped silently. Add a bounded `data.server_overwrote: ["path", …]` list (names only, never values), or refuse those keys as `blob_ref` is refused. Acceptance: a call with `options.path` reports `path` in the list or refuses; [test_tools_blobs.py:456](../../../../tests/unit/web/composer/test_tools_blobs.py#L456) updated to assert the chosen behaviour.
4. **Split `source_blob_unavailable`** (set_source_from_blob-N4). Where: [sources.py:847](../../../../src/elspeth/web/composer/tools/sources.py#L847), [850](../../../../src/elspeth/web/composer/tools/sources.py#L850), [896–900](../../../../src/elspeth/web/composer/tools/sources.py#L896). Why: three recovery actions share one code. Use distinct codes (`source_blob_not_found`, `source_blob_not_ready`) and route storage-missing through the BOS-02 decision instead of an ordinary refusal; register each in `explain_validation_code`. Acceptance: each condition yields its own code with an explanation; missing bytes behind READY follows the shared BOS-02 outcome.
5. **Correct the csv plugin hint** (G1). Where: [csv_source.py:713](../../../../src/elspeth/plugins/sources/csv_source.py#L713). Why: it states a discard default that no Composer path supplies. Replace with "a new source requires an explicit failure choice; omit only to keep an existing source's choice." Acceptance: a parity test asserts no source assistance text claims a default failure route for `set_source_from_blob`/`set_pipeline`.
6. **Accept `description` and preserve it on rebind** (SG-06, G2). Where: [redaction.py:1529](../../../../src/elspeth/web/composer/redaction.py#L1529), declaration at [sources.py:1680](../../../../src/elspeth/web/composer/tools/sources.py#L1680), `SourceSpec` at [sources.py:1317](../../../../src/elspeth/web/composer/tools/sources.py#L1317). Why: the skill tells the model to describe every source, and named blob sources have no route to do so. Mirror `set_source`'s optional `description` (omission preserves an existing description for the same source_name; explicit value replaces). Acceptance: bind with description stores it; rebind without description preserves it; explicit new description replaces it; wire-parity tests updated.
7. **Rewrite the declaration description** (G3). Where: [sources.py:1675](../../../../src/elspeth/web/composer/tools/sources.py#L1675). State READY-only, named sources, the inferable MIME set, the explicit-failure-choice rule, automatic `invented_source` staging for generated blobs, and that `data.source_blob` echoes identity. Acceptance: description-parity review against this sheet's §2; no new behaviour.
8. **Add a canonical-ID regression for binding** (BOS-03). Where: `tests/unit/web/composer/test_tools_blobs.py` near [line 288](../../../../tests/unit/web/composer/test_tools_blobs.py#L288). Why: the uppercase case is untested for this tool. Acceptance: uppercase and canonical spellings of one ID give the same outcome (both bind, or both refuse with `source_blob_identity_invalid`), with a foreign-session control.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_source_rejection_contracts.py:638](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L638) — test_source_binding_lock_keeps_real_singular_binding_and_review_custody.
- [tests/unit/web/composer/test_source_rejection_contracts.py:587](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L587) — all authored modalities are refused by plural route.
- [tests/unit/web/composer/test_tools_mutations.py:166](../../../../tests/unit/web/composer/test_tools_mutations.py#L166) — endpoint refusal before lookup.
- [tests/unit/web/composer/test_promote_set_source_from_blob.py:1](../../../../tests/unit/web/composer/test_promote_set_source_from_blob.py#L1) — promotion/authoring tests inspected at file level; not executed in this lane.
- Additional test bodies read (not executed in this review): [test_tools_blobs.py:388](../../../../tests/unit/web/composer/test_tools_blobs.py#L388) (JSONL inference, inferred path only), [:456](../../../../tests/unit/web/composer/test_tools_blobs.py#L456) (caller path overwritten), [:489](../../../../tests/unit/web/composer/test_tools_blobs.py#L489) (caller blob_ref refused); [test_promote_set_source_from_blob.py:576](../../../../tests/unit/web/composer/test_promote_set_source_from_blob.py#L576) (TSV delimiter), [:1130](../../../../tests/unit/web/composer/test_promote_set_source_from_blob.py#L1130) and [:1179](../../../../tests/unit/web/composer/test_promote_set_source_from_blob.py#L1179) (review proof is content-bound, not blob-ID-bound); [test_source_interpretation_requirement_guard.py:183](../../../../tests/unit/web/composer/test_source_interpretation_requirement_guard.py#L183), [:514](../../../../tests/unit/web/composer/test_source_interpretation_requirement_guard.py#L514), [:530](../../../../tests/unit/web/composer/test_source_interpretation_requirement_guard.py#L530). Among the test bodies read, none exercises explicit `plugin` on a JSONL blob, caller `format` on a JSONL MIME or rebind description preservation (not a whole-tree claim). For the uppercase ID: `grep -n '\.upper()'` over every test file that mentions this tool matches only the plural duplicate case ([test_source_rejection_contracts.py:517](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L517)), the inline-ref guard ([test_blob_inline_tools.py:1131](../../../../tests/unit/web/composer/test_blob_inline_tools.py#L1131)) and unrelated SQL matchers, so no singular-binding uppercase test exists in those files.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance.

**Sheet-review probes (2026-11-09).** In-memory handler/dispatch runs, not end-to-end service runs: the test `execute_tool` wrapper over an in-memory SQLite session schema with uploaded (`created_by="user"`) READY blobs, the trained-operator policy view and `_mock_catalog()`; no files, provider or network; `elspeth.__file__` resolved into this worktree. Observed:

```text
canonical bind                      success version=2 data keys=[source_blob]
uppercase same id                   fail source_blob_unavailable "Blob '…' not found."
description argument                ToolArgumentError (set_source_from_blob arguments)
rebind same blob, no description    success; description "Read the uploaded orders" -> None; vf preserved
JSONL mime, inferred plugin         success format=jsonl
JSONL mime (rows.ndjson), plugin=json  success format=<absent> path suffix=ndjson
JSONL mime, caller format=json      success format=jsonl
new source, no on_validation_failure   fail failure_route_required
non-UUID / unknown UUID             fail source_blob_identity_invalid / source_blob_unavailable
stamp echo, no stored stamp         fail source_data_contract_required
stamp echo, stored on blob A -> B   success; B keeps guaranteed_fields=('name',)
different stamp, stored on A -> B   fail source_data_contract_required
membership                          in _MUTATION_TOOLS=False; in _BLOB_MUTATION_TOOLS=True; is_mutation_tool=True
```

The stamp-echo case hand-injected the stamp without a resolved `source_data_contract` row, so it does not exercise review reconciliation. No LLM-authored branch, proposal staging or acceptance route was run.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `blob_mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.sources._execute_set_source_from_blob`](../../../../src/elspeth/web/composer/tools/sources.py#L1195).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | no |
| Composer MCP | no |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "blob_id": {
      "type": "string",
      "description": "Blob ID to use as source."
    },
    "source_name": {
      "type": "string",
      "description": "Source root name to bind. Defaults to 'source' for legacy single-source pipelines."
    },
    "plugin": {
      "type": "string",
      "description": "Source plugin override (e.g. 'csv'). Inferred from MIME type if omitted."
    },
    "on_success": {
      "type": "string",
      "description": "Connection-name string the source PUBLISHES. Some downstream consumer (node 'input' or output 'sink_name') MUST equal this value. Despite the field name, this is NOT a node id — connections match by string, not by topology.",
      "examples": [
        "raw_url_rows",
        "csv_rows",
        "fetched_text"
      ]
    },
    "on_validation_failure": {
      "type": [
        "string",
        "null"
      ],
      "description": "Explicit non-empty failure choice required for new sources. 'discard' records the drop; Any other value, including 'quarantine', must match a configured output/sink name. a named route must satisfy sink and required-control policy. Omit only to preserve the same existing source's choice. Null/empty is invalid; no server default is supplied."
    },
    "options": {
      "type": "object",
      "description": "Plugin-specific config (merged with blob path). Required fields vary by plugin: text sources need 'column' (output field name) and 'schema' (e.g., {mode: 'observed'}). Server/resolver-owned source option roots are not settable: blob_ref, blobs, source_authoring. Bind blobs through set_source_from_blob, set_source_from_blobs, source.blob_id, or source.inline_blob. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
    }
  },
  "required": [
    "blob_id",
    "on_success"
  ],
  "additionalProperties": false
}
```
