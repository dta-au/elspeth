# `inspect_source` — independent tool assessment

Baseline: ELSPETH **0.8.9**, `3b28d537be98234da8135c418bf185a998c18b68`. Reconnaissance and remediation design only. Incoming `.10 lite` changes are outside this assessment and require affected-source reinspection.

## 1. Purpose and usage

Return bounded structural evidence about a session source artifact: raw/runtime header names, naming provenance, sample counts, lexical types, URL candidates, warnings and redacted identity. This supports model decisions; it does not infer a guaranteed runtime schema from a sample.

Select before declaring tabular schemas or choosing text/CSV/JSON handling. Requires session engine/session ID and a READY owned blob with present bytes and correct SHA-256. Optional encoding is meaningful only for CSV/TSV and does not configure the eventual source.

## 2. Contract

Required blob_id:string; optional encoding:null|string defaults None, strict nonempty length1..128. The handler reads at most an8KiB retained prefix and parses at most100 rows, but streams the entire file for integrity verification. Result data includes source_kind, observed_headers, runtime_headers, field_name_mapping, sample_row_count, inferred_types, url_candidates,warnings,byte_range_inspected,redacted_identity; it excludes row content. Unknown/unsupported codec, non-CSV encoding override, missing/nonready/foreign blob and credential-bearing facts refuse. Hash mismatch/NULL ready hash escalates integrity failure. Discovery does not change state, require approval or cache; repeated calls can observe a changed artifact. There is no automatic retry or durable inspection receipt binding the eventual source config.

This is discovery: the handler retains immutable composition state; successful declared inventory data is admitted under its producer response contract before audit/disclosure. Across both provider dialects, the provider wire projection is decoded to canonical arguments before dispatch; strict transport representation does not grant different authority. A ToolResult's `success` records operation admission, while `validation.is_valid`, runtime preflight, review and execution admission answer separate questions. Owned-code/audit integrity failures escape as faults rather than being reclassified as ordinary invalid model input.

## 3. Implementation trace

- [src/elspeth/web/composer/tools/sources.py:1982](../../../../src/elspeth/web/composer/tools/sources.py#L1982) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:1882](../../../../src/elspeth/web/composer/tools/sources.py#L1882) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/tools/sources.py:1785](../../../../src/elspeth/web/composer/tools/sources.py#L1785) — source-plane declaration/handler and binding admission.
- [src/elspeth/web/composer/source_inspection.py:1](../../../../src/elspeth/web/composer/source_inspection.py#L1) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/tools/blobs.py:1898](../../../../src/elspeth/web/composer/tools/blobs.py#L1898) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/tool_batch.py:3347](../../../../src/elspeth/web/composer/tool_batch.py#L3347) — dependency, contract or downstream consumer.
- [src/elspeth/web/composer/tools/_registry.py:100](../../../../src/elspeth/web/composer/tools/_registry.py#L100) and [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391) — plane aggregation, canonical exposure and provider-loop wire palette. Declaration and redaction argument models are separate checked surfaces.
- [src/elspeth/web/composer/tools/_dispatch.py:795](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) — credential guard, policy snapshot identity, declared-schema admission, ToolContext, handler dispatch, interpretation gate and finalisation. Public routes enable closed-schema and path-boundary guards; direct internal callers have different opt-in flags.
- [src/elspeth/web/composer/tool_batch.py:1728](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [src/elspeth/web/composer/tool_batch.py:3010](../../../../src/elspeth/web/composer/tool_batch.py#L3010) and [src/elspeth/web/composer/audit.py:1063](../../../../src/elspeth/web/composer/audit.py#L1063) — proposal/approval interception, execution with actual session authority, tool audit status, versions and redacted result handling. Dispatch completion and ToolResult.success remain distinct audit facts.
- [src/elspeth/web/composer/tool_batch.py:3258](../../../../src/elspeth/web/composer/tool_batch.py#L3258) — state/result interpretation, validation and provider feedback; [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:148](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L148) renders proposal tool descriptions, while graph/spec views consume the persisted state projection.
- [src/elspeth/composer_mcp/server.py:363](../../../../src/elspeth/composer_mcp/server.py#L363) — separate trained-operator invocation route. Its admitted subset omits web session/blob/advisor surfaces; included tools still share canonical dispatch and strict argument/path guards, without the web chat approval/persistence lifecycle.

## 4. Alternative paths

Normal CSV facts distinguish observed lexical data from normalized runtime column names; null runtime_headers means bounded evidence could not resolve a trustworthy header. Explicit CSV encoding re-inspects the same prefix and appends a warning that source config must match separately. Large blobs still incur a full sequential hash read, so bounded result size is not bounded total I/O time. The handler reads metadata and filesystem separately; unlike get_blob_content it does not call the shared locked/recovery-aware reader. Facts are credential-checked, admitted under SOURCE_INSPECTION_RESPONSE_CONTRACT and registered as turn evidence. MCP excludes this session tool.

## 5. Usability assessment

**BOS-03 — source-established affected caller of inconsistent UUID admission.**
The handler validates UUID parseability and then passes raw text to
`_sync_get_blob` at `sources.py:1911–1915`. Metadata lookup dynamically
demonstrated false absence for uppercase text representing the same canonical
UUID; inspection's affected path was source traced, not separately reproduced.
Use the exact discovered canonical ID. Shared BlobId admission should
canonicalize all accepted forms or refuse them uniformly before reads; regression
must include canonical/equivalent/malformed/foreign identities without weakening
session custody or inspection's bounded-memory property.

**SG-04 — demonstrated custody-admission inconsistency; high confidence.** A controlled handler call with session engine/ID but no data_dir, SessionOperationContext or authority succeeds. The get_blob_content control raises AuditIntegrityError. Source at1915–1931 also reads row and bytes without the shared custody/recovery path. This demonstrates an internal lifecycle-contract gap; it does **not** prove a public authorisation bypass or a reachable concurrent update race. Public route leases may constrain writers, and that reachability remains unresolved. Workaround: use fully wired production session context and re-inspect after artifact changes; operators should not interpret a sampled type as coercion or guaranteed_fields.

## 6. Structural remediation

Give inspection a shared streaming custody read primitive that recovers interrupted replacements and pins metadata/hash/bytes under actual operation authority, without replacing its bounded-memory scan with unbounded read_bytes. The tool owns structural parsing and evidence semantics; blob storage owns version/custody, orchestrator owns schema choice. Regression: missing/expired/foreign authority refuses before filesystem access; genuine corruption raises; a completed allowed replacement returns one coherent version or a recoverable changed-version result; interrupted replacement recovery uses the same path as raw reads;8KiB/100-row and streaming-memory bounds remain. Compatibility risk medium/high at the internal ToolContext seam; preserve facts payload and source encoding semantics.

## 7. Evidence and verification boundaries

- [tests/unit/web/composer/tools/test_tools_preview.py:2035](../../../../tests/unit/web/composer/tools/test_tools_preview.py#L2035) — TestInspectSourceTool: BOM/encoding, integrity, bounded reads and facts contracts.
- [tests/unit/web/composer/tools/test_tools_preview.py:2420](../../../../tests/unit/web/composer/tools/test_tools_preview.py#L2420) — test_inspect_source_does_not_use_unbounded_read_bytes.
- [tests/unit/web/composer/tools/test_tools_preview.py:2462](../../../../tests/unit/web/composer/tools/test_tools_preview.py#L2462) — test_hash_mismatch_raises_blob_integrity_error.

The lane inspected the named test bodies and ran a serial focused selection at this baseline. Initial run: `exit=1; 550 passed, 6 failed`; the six failures required the absent optional Azure identity SDK. After the parent installed frozen Azure/AWS extras, rerunning only those six cases produced `exit=0; 6 passed, 42 deselected`. This is local handler/dispatch evidence, not live provider, PostgreSQL contention or deployed-browser acceptance. The dedicated `TestInspectSourceTool` class separately exited 0: `25 passed, 42 warnings in 2.67s`. Controlled custody probe: `inspect_without_custody_authority True data_dir None operation_context None authority None`; raw-reader control: `AuditIntegrityError Blob content reads require the service storage root`. Public-route race reachability was not exercised.

**Confidence:** high for the cited current-source contracts and reported controlled observations; moderate for proposed architectural improvements, which require design review. **Information gaps and caveats:** no live provider/model compliance, hosted UI, PostgreSQL contention, future `.10 lite` behaviour or unlisted plugin qualification is claimed. Named test coverage is evidence for its assertions, not universal tool correctness. **Risk:** the proposed production changes are unimplemented; acceptance checks above must demonstrate successful behaviour and invalid-input refusal before relying on them.

## Runtime argument schema and measured exposure

Synchronous kind: `blob_discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.sources._execute_inspect_source`](../../../../src/elspeth/web/composer/tools/sources.py#L1882).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | yes |
| Composer MCP | no |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "blob_id": {
      "type": "string",
      "description": "ID of the blob to inspect."
    },
    "encoding": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 128,
      "description": "Explicit CSV/TSV text encoding for this bounded inspection only. Use null for default blob inspection."
    }
  },
  "required": [
    "blob_id"
  ],
  "additionalProperties": false
}
```
