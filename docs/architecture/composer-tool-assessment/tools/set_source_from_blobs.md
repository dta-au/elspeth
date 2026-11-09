# `set_source_from_blobs` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Bind a sequence of existing session documents as a blob_rows source, one retained document per runtime row in authoring order. This preserves payload custody instead of inserting binary bytes into model context.

Use for uploaded/pasted binary documents feeding supported extraction/rasterisation plugins. Requires session engine/session ID and available blob_rows source. The plural route serves user-verbatim uploads; it refuses model-authored blobs whose review custody it cannot represent.

## 2. Contract

Required blob_ids: nonempty array of strings, at most 1000, and on_success:string. source_name defaults source; options defaults {}; optional on_validation_failure has preserve-on-omission semantics for an existing source, explicit nonempty choice for a new one. IDs must be valid UUIDs and unique after case normalisation; distinct IDs with equal content hashes also refuse as duplicate documents. Each persisted blobs entry comes from authoritative metadata: blob_id,payload_ref,filename,mime_type,size_bytes. Result returns data.source_blobs in order, applied component, validation and new version. The call validates the whole list before mutating CompositionState. It neither reads binary content nor writes blobs; byte staging and integrity admission happen at execution. No auto retry or idempotency key.

The handler returns an immutable candidate; it does not itself durably save the conversation. In web explicit_approve mode, the operation is staged as a proposal and re-admitted on acceptance. Otherwise the enclosing tool batch applies the accepted state, and the session turn persists it under its operation authority. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/sources.py:1620](../../../../src/elspeth/web/composer/tools/sources.py#L1620) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:1500](../../../../src/elspeth/web/composer/tools/sources.py#L1500) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:1338](../../../../src/elspeth/web/composer/tools/sources.py#L1338) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:1458](../../../../src/elspeth/web/composer/tools/sources.py#L1458) — source-plane declaration/handler and binding admission.
- [src/elspeth/plugins/sources/blob_rows.py:1](../../../../src/elspeth/plugins/sources/blob_rows.py#L1) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

Manual blobs/blob_ref/source_authoring options, forged guarantees, invalid IDs, missing or foreign blobs, non-READY members, duplicate hashes and LLM-authored modalities refuse the whole list unchanged. A ready row without a content hash is an audit integrity fault, not user error. Server fabricates the blob_rows fixed-field guarantee from the plugin row contract and then runs plugin/source prevalidation. The selected source may remain graph-incomplete; missing destination sinks appear in validation after successful admission. A mixed document set is described as requiring separate homogeneous pipelines, while metadata binding itself does not inspect binary encodings or guarantee downstream extractor acceptance.

## 5. Usability assessment

**BOS-03 — source-established affected caller of inconsistent UUID admission.**
The plural binder normalizes IDs only for duplicate detection at
`sources.py:1391`, then passes the original text to `_sync_get_blob` at line1405.
The metadata tool's controlled equivalent-uppercase UUID failure demonstrates
the shared lookup problem; this plural route was source traced rather than
separately reproduced with alternate spellings. Preserve the exact canonical
IDs returned by discovery as a workaround. One owned BlobId boundary should
govern duplicate identity and lookup consistently, with equivalent-ID, duplicate,
malformed and foreign-session rejection controls for every binding route.

**SG-06 — source-established plugin/adapter ownership weakness; high confidence.** The adapter imports private blob_rows._ROW_FIELDS and stamps schema guarantees itself (the source explains this was placed here to avoid source-hash churn). That couples Composer authoring to private plugin representation rather than an authoritative public capability/lowering contract. **Intended restrictions:** content deduplication and refusal of LLM-authored documents are explicit, tested boundaries, not accidental single-blob limitations. Workaround: use singular reviewed text binding for representable model-authored content; keep uploaded binary documents on this route.

## 6. Structural remediation

Expose a public immutable plugin-owned document-row contract and a typed custody-binding adapter shared by discovery, authoring, preview and execution. Review/provenance admission remains in the web authority layer; plugins must not invent human approval. Regression: legal ordered documents produce exactly the plugin-declared row fields; duplicate IDs/hashes, foreign and non-ready blobs refuse atomically; every authored modality is rejected until a complete equivalent review path exists; metadata-only binding performs no content reads; runtime revalidates retained hashes. Preserve current duplicate/refuse policy unless separately changed. Compatibility risk medium because persisted blobs entries and schema guarantees are runtime inputs.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/test_set_source_from_blobs.py:127](../../../../tests/unit/web/composer/test_set_source_from_blobs.py#L127) — ordered authoritative field binding, missing/foreign/pending/duplicate rejection.
- [tests/unit/web/composer/test_source_rejection_contracts.py:460](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L460) — metadata order with no content reads.
- [tests/unit/web/composer/test_source_rejection_contracts.py:492](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L492) — whole-list refusal without partial mutation.
- [tests/unit/web/composer/test_source_rejection_contracts.py:548](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L548) — corrupt owned-row escalation.
- [tests/unit/web/composer/test_source_rejection_contracts.py:587](../../../../tests/unit/web/composer/test_source_rejection_contracts.py#L587) — authored modality review boundary.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `blob_mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.sources._execute_set_source_from_blobs`](../../../../src/elspeth/web/composer/tools/sources.py#L1500).

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
    "blob_ids": {
      "type": "array",
      "items": {
        "type": "string",
        "description": "Ready session blob ID."
      },
      "minItems": 1,
      "maxItems": 1000,
      "description": "Blob IDs in authoring order (becomes source-row order). No duplicates."
    },
    "source_name": {
      "type": "string",
      "description": "Source root name to bind. Defaults to 'source' for legacy single-source pipelines."
    },
    "on_success": {
      "type": "string",
      "description": "Connection-name string the source PUBLISHES. Some downstream consumer (node 'input' or output 'sink_name') MUST equal this value.",
      "examples": [
        "documents"
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
      "description": "Optional blob_rows config (e.g. schema). Server/resolver-owned source option roots are not settable: blob_ref, blobs, source_authoring. Bind blobs through set_source_from_blob, set_source_from_blobs, source.blob_id, or source.inline_blob. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
    }
  },
  "required": [
    "blob_ids",
    "on_success"
  ],
  "additionalProperties": false
}
```
