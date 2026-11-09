# `get_blob_content` — bounded, identity-pinned text pages

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only. **Overall confidence: High for traced/read-test contracts and missing-storage reproduction; Moderate for unmeasured resource impact.**

## 1. Purpose and usage

Use this tool for selected pages of an authorized UTF-8 artifact, particularly when a complete attachment response would exceed the whole-read response limit. For small instruction/reference documents, `read_attachment` provides a stronger complete-delivery contract. Source structure should be investigated with source inspection instead of treating a text prefix as schema proof. Requires session engine/ID, storage root, and an exact read-capable session operation authority. Composer MCP does not expose blob tools.

## 2. Contract

Required `blob_id: string`; `offset_chars` is a nonnegative integer defaulting to 0. Optional `expected_content_hash` is null/omitted at offset 0 or a lowercase 64-hex SHA-256. Continuation beyond zero requires a non-null hash. Every supplied hash is checked. Character offsets count original Unicode code points, not bytes; use returned `next_offset_chars` rather than guessed offsets or dependent parallel requests.

Success includes content, blob identity/name/MIME, full byte size, recorded origin (`created_by`, `creation_modality`), verified original-byte hash, full decoded character count, range `[start_char,end_char)`, truncation, and continuation. The complete serialized JSON response is bounded to 4,096 UTF-8 bytes; metadata/escaping reduce usable content. There is also a 50,000-code-point upper range bound. A terminal empty page at EOF is valid; nonterminal pages must advance. The complete decoded artifact is credential-inspected before any page is disclosed, so paging does not bypass whole-input inspection limits or credentials beyond a page boundary.

It is uncached `BLOB_DISCOVERY`, does not change composition version, and needs no mutation approval. Metadata/read/hash are fenced under the shared custody lock. Malformed arguments are safe argument errors; missing/not-ready blob, bad encoding, offset, expected-hash mismatch, or disclosure-size refusal returns failure without content. Hash corruption/null ready hash escalates. The current helper converts a missing backing file into a failed result instead of an integrity exception. Tool content is redacted in persisted tool-result projections; provider delivery still treats it as untrusted evidence. Repeating a pinned page is safe but performs a new read/inspection.

## 3. Implementation trace

[Handler and declaration](../../../../src/elspeth/web/composer/tools/blobs.py#L1970) use the strict [argument/range models](../../../../src/elspeth/web/composer/redaction.py#L3328). [`_locked_read_ready_blob`](../../../../src/elspeth/web/composer/tools/blobs.py#L1930) enters the [shared custody/recovery context](../../../../src/elspeth/web/blobs/service.py#L3259), reads and hashes bytes, then the handler decodes, checks expected identity, inspects credentials, and binary-searches the page using the actual selected response serializer. [Response admission](../../../../src/elspeth/web/composer/tools/blobs.py#L269) closes range/origin shape.

Registration/exposure use the [registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111) and [canonical definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), followed by provider wire projection. [Public dispatch](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) and [worker execution](../../../../src/elspeth/web/composer/tool_batch.py#L3008) supply context and admit discovery data before audit/disclosure. [Content-part construction](../../../../src/elspeth/web/composer/discovery_cache.py#L69) retains untrusted provenance, [audit finalization](../../../../src/elspeth/web/composer/audit.py#L812) records dispatch and semantic outcomes separately, and [frontend cards](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) consume server-derived outcomes.

## 4. Alternative paths

A complete small file returns a terminal page; larger admissible text returns variable-size pages with an explicit next offset. A replaced artifact rejects continuation under the previous hash. Credentials after or spanning a page boundary refuse the first page. Non-UTF-8 content fails safely; this inspection tool has no encoding selector even though runtime inline references support several encodings. Pending/error states refuse before disclosure. Hash corruption escalates through the audited crash path. An interrupted blob replacement/deletion is reconciled before reading a committed version.

## 5. Usability assessment

**BOS-02 — demonstrated failure-taxonomy divergence, High confidence:** for ready owned metadata with a missing backing file, this tool returns ordinary `success=false`, while `BlobServiceImpl.read_blob_content_sync` raises `BlobContentMissingError` under the same operation fence. The local probe reproduced both paths. No bytes leak, but the model receives a repairable-looking tool failure for an infrastructure/integrity problem it cannot fix. Operator storage recovery is the workaround; repeated argument edits are inappropriate.

**BOS-05 — source-established resource limitation:** each small page uses a complete `Path.read_bytes()`, whole-file hash/decode, and whole-content credential pass. The response bound does not bound work or peak retained bytes, and repeated pages repeat that work. No latency/memory magnitude was measured. The whole-input credential restriction itself is intended and must remain.

## 6. Structural remediation

Use one owned verified-content reader for API, tools, source inspection, and preview, with consistent lifecycle/integrity exceptions and structured actionability. Share snapshot/identity facts and resource policy; do not weaken full-artifact credential admission to inspect only disclosed pages. For admissible text, reuse a request-scoped verified identity/content admission or bounded streaming design where practical, invalidated by replacement and policy changes. The orchestrator should understand full-read versus page-read completion evidence.

Acceptance: exact Unicode reconstruction, byte-bounded serializer output, progressing offsets, and expected-hash refusal remain; corrupt/missing owned bytes have the same nonrepairable classification across routes; credentials anywhere in the artifact refuse every page; resource work is explicitly bounded/measured; cross-session/stale authority fails closed. Compatibility risk is Medium for failure categories/caching, Low for preserving current page schema. A maximum-input warning is temporary containment, not the shared reader solution.

## 7. Evidence, gaps, and caveats

Read [exact Unicode/serialization tests](../../../../tests/unit/web/composer/test_tools_blobs.py#L2687), [credential seam cases](../../../../tests/unit/web/composer/test_tools_blobs.py#L2824), hash/encoding failures, and [mixed-version read proof](../../../../tests/unit/web/composer/test_tools_blobs.py#L4013). The seven-file focused run exited 0: **448 passed, 43 warnings, 66.95s**. A controlled SQLite probe demonstrated the missing-file divergence. No live provider, PostgreSQL, browser, memory profile, or throughput measurement ran; source-level full reads establish work shape, not measured operational severity.

## Runtime argument schema and measured exposure

Synchronous kind: `blob_discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.blobs._execute_get_blob_content`](../../../../src/elspeth/web/composer/tools/blobs.py#L1970).

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
      "description": "ID of the blob to read."
    },
    "offset_chars": {
      "type": "integer",
      "minimum": 0,
      "default": 0,
      "description": "Start offset in original Unicode code points."
    },
    "expected_content_hash": {
      "type": [
        "string",
        "null"
      ],
      "pattern": "^[0-9a-f]{64}$",
      "description": "Verified original-byte SHA256. Use null at offset 0 for no expected identity constraint; a canonical non-null hash is required after offset 0. Every non-null hash is checked, even at offset 0."
    }
  },
  "required": [
    "blob_id"
  ],
  "additionalProperties": false
}
```
