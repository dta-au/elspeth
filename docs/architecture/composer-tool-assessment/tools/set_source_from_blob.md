# `set_source_from_blob` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Bind one existing READY session blob as a named file-backed source using authoritative storage identity. This is a custody-aware authoring operation, not a content upload or runtime source read.

Select after inspecting a text/tabular session artifact and choosing its connection and failure policy. Requires session engine/session ID and an available inferred or explicit source plugin. LLM-authored content also requires actual storage root and session operation authority to bind reviewed bytes consistently.

## 2. Contract

Required blob_id and on_success strings. source_name defaults source; plugin omitted infers csv/json/text from MIME, with JSONL mode and filename delimiter assistance where applicable. options defaults {}; optional on_validation_failure preserves the existing choice only when omitted, otherwise must be explicit and nonempty. No description argument is declared. Caller options merge with authoritative path/blob_ref/mode/source-authoring metadata, whose values win; caller cannot provide those owned roots. Result adds data.source_blob (bounded identity/provenance), applied source echo, validation and a new version. It does not create/update the blob, execute a source, or make a provider call. Repeated binding is not a no-version-change replay contract.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/sources.py:1671](../../../../src/elspeth/web/composer/tools/sources.py#L1671) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:1195](../../../../src/elspeth/web/composer/tools/sources.py#L1195) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:797](../../../../src/elspeth/web/composer/tools/sources.py#L797) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:882](../../../../src/elspeth/web/composer/tools/sources.py#L882) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/blobs.py:1930](../../../../src/elspeth/web/composer/tools/blobs.py#L1930) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/tools/_common.py:3500](../../../../src/elspeth/web/composer/tools/_common.py#L3500) — shared mutation/config/result machinery.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

Missing context, malformed/foreign/missing blob, non-READY state, unsupported MIME without override or invalid plugin options produces structured refusal. For model-authored content, metadata then locked bytes are compared; changed hash returns source_blob_changed with retry guidance, missing file/invalid UTF-8 refuses, and authoritative interpretation requirements/derived guarantees are staged. User-uploaded content does not receive an invented-source review merely because a model binds it. An override still passes plugin/profile/policy admission. After building the SourceSpec, review reconciliation may reject atomically. A valid binding can remain graph-incomplete; runtime re-resolves custody. MCP omits this web blob surface.

## 5. Usability assessment

**BOS-03 — source-established affected caller of inconsistent UUID admission.**
`_resolve_source_blob` validates UUID parseability but passes the original text
to `_sync_get_blob` at `sources.py:842–845`. The metadata tool's controlled
uppercase-versus-canonical case demonstrates the shared lookup inconsistency;
this binding path was source traced, not separately exercised with alternate
spellings. Use the exact canonical ID returned by discovery. A common owned
BlobId admission boundary should canonicalize accepted representations before
lookup, or consistently refuse noncanonical ones, with valid/invalid/foreign
identity controls across metadata, inspection and source binding.

**SG-06 — source-established responsibility split; high confidence.** MIME inference, delimiter handling, source-row guarantees, provenance and review staging are concentrated in the Composer binding adapter. The locked LLM-authored read is a strong existing integrity mechanism and a reference for other readers. Its existence does not prove every other inspection route holds the same custody boundary. **Intended restriction:** new bindings require an explicit failure decision and cannot bypass review with caller-supplied metadata. **Usability limitation:** rebinding replaces the source without preserving its optional description. Workaround: retain author-owned options deliberately and inspect the returned source echo.

## 6. Structural remediation

Make blob-to-source lowering a shared, typed adapter: immutable blob version plus plugin-owned source capability/config contract in, admitted SourceSpec plus review obligations out. Reuse it for bulk and incremental authoring rather than copy the MIME/provenance policy. Add an explicit source-description preservation/override policy across binding routes. Acceptance: upload vs LLM-authored modalities remain distinguishable; unsupported MIME and forged owned roots refuse before a write; metadata/bytes changing during binding returns a recoverable conflict; rebind and set_pipeline use the same source contract; failed reconciliation preserves prior state. Compatibility risk medium/high for existing blob/review metadata; preserve refusal contracts and never silently reclassify old provenance.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_source_rejection_contracts.py:638](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L638) — test_source_binding_lock_keeps_real_singular_binding_and_review_custody.
- [tests/unit/web/composer/test_source_rejection_contracts.py:587](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L587) — all authored modalities are refused by plural route.
- [tests/unit/web/composer/test_tools_mutations.py:166](../../../../tests/unit/web/composer/test_tools_mutations.py#L166) — endpoint refusal before lookup.
- [tests/unit/web/composer/test_promote_set_source_from_blob.py:1](../../../../tests/unit/web/composer/test_promote_set_source_from_blob.py#L1) — promotion/authoring tests inspected at file level; not executed in this lane.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance.

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
