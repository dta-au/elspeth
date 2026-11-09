# `read_attachment` — complete guarded attachment text

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only. **Overall confidence: High for source and controlled positive/rejection probes; Moderate for unmeasured resource impact.**

## 1. Purpose and usage

Select this tool for one authorized instruction, requirement, or reference attachment whose complete UTF-8 text the orchestrator needs before reasoning. It exists to avoid confusing a short prefix with a complete document or spending many dependent model turns on small pages. It does not parse tabular source structure. Requires web session engine/ID, data root, and exact read-capable operation authority; unavailable through Composer MCP.

## 2. Contract

Required `blob_id: string`; optional `expected_content_hash` is omitted/null or canonical lowercase 64-hex SHA-256. Extra fields and type coercion are rejected. Every supplied hash must match the verified original bytes. Success always returns full exact decoded content and metadata/origin with `start_char=0`, `end_char=total_chars`, `truncated=false`, and `next_offset_chars=null`. A complete serialized response must fit **262,144 UTF-8 bytes** at this baseline, derived from the bounded JSON/credential traversal policy; aggregate provider/guard limits still apply. An oversized file or escaped response explicitly refuses rather than truncates. Paging is an alternative only if the whole original text passes credential admission.

It is uncached `BLOB_DISCOVERY`, with a selected attachment response contract, unchanged composition version, and no mutation approval. Content is read/hash-verified under custody, decoded as UTF-8, matched to the expected hash, and credential-inspected before success. Missing context/not-found/not-ready/bad encoding/hash-selector mismatch/size refusal returns failure without content. Corrupt bytes or null hash escalate; missing backing bytes currently return ordinary failure. Redaction summarises content in persisted projections while the provider receives guarded untrusted content.

## 3. Implementation trace

The [handler/declaration](../../../../src/elspeth/web/composer/tools/blobs.py#L2175) admits the strict [attachment selector model and size policy](../../../../src/elspeth/web/composer/redaction.py#L3411), invokes [`_locked_read_ready_blob`](../../../../src/elspeth/web/composer/tools/blobs.py#L1930), then checks byte size, decoding, identity, credentials, and actual serialized output size. The [shared custody context](../../../../src/elspeth/web/blobs/service.py#L3259) reconciles interrupted storage before returning a version. The nominal [attachment response contract](../../../../src/elspeth/web/composer/_attachment_response.py) binds exact full-content semantics and untrusted content parts.

The [registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111) and [model definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311) supply exposure; provider projection handles wire differences. [Public dispatch](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) and [tool-batch worker/admission](../../../../src/elspeth/web/composer/tool_batch.py#L3008) provide authority and finalize results. [Content-part construction](../../../../src/elspeth/web/composer/discovery_cache.py#L69) preserves attachment trust, [audit](../../../../src/elspeth/web/composer/audit.py#L812) records semantic result separately from dispatch status, and the [frontend card](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) uses server-derived outcomes.

## 4. Alternative paths

A small valid file yields one complete response. A matching expected hash constrains identity; a mismatch discloses nothing. Invalid UTF-8 has no lossy replacement fallback. If source bytes exceed the whole-input limit, neither this tool nor page reading can bypass it. If only JSON escaping/envelope overhead exceeds the response bound, selected pages may remain available. Pending/error blobs and missing files refuse; corruption escalates. A storage recovery obligation is reconciled under custody before reading.

## 5. Usability assessment

The full-delivery contract is useful and explicit. **BOS-02 — demonstrated taxonomy divergence:** a ready metadata row with missing storage returns a generic failed tool result here, while the canonical content service raises `BlobContentMissingError`. Same-fence local probes verified that difference, plus a restored-file positive control and expected-hash rejection. Workaround: report an infrastructure/storage problem rather than cycling tool arguments.

**BOS-05 — source-established resource limitation:** the handler checks its 262,144-byte limit only after `_locked_read_ready_blob` reads/hashes the entire file. Therefore a successful bounded response or an eventual refusal does not establish bounded read allocation. No operational memory/latency effect was measured. Complete-content admission and refusal are intended safety constraints, not defects to evade by slicing first.

## 6. Structural remediation

The blob service should own one verified-content observation contract with explicit read purpose, full-input admission limits, integrity failure classification, and identity. Enforce whole-read eligibility before materialising a known oversized artifact where possible; use the existing streamed verification machinery when complete verification is still required. Preserve exact content and untrusted provenance. The orchestrator should distinguish “complete document delivered,” “pages available,” “input forbidden/too large,” and “owned storage failure.”

Acceptance: a complete small Unicode document returns exact text and terminal ranges; escaped response overflow refuses without any prefix; supplied wrong hash, wrong session, stale authority, bad encoding, credentials anywhere, and input limit violations disclose no content; missing owned bytes classify consistently with API; resource bounds are measured with oversized valid artifacts. Compatibility risk is Medium for enriched failure outcomes and shared reader changes. The existing complete-success shape can remain unchanged.

## 7. Evidence, gaps, and caveats

Read the handler, nominal response model, shared custody implementation, and [existing attachment-adjacent producer tests](../../../../tests/unit/web/composer/test_blob_response_contracts.py#L200). A controlled local probe ran the actual dispatcher with genuine SQLite operation authority: restored valid text succeeded with `truncated=false` and `end_char=31`; a supplied wrong hash returned failure with `data=null`; missing bytes reproduced the API/tool taxonomy difference. The related seven-file run exited 0: **448 passed, 43 warnings, 66.95s**; this total is not a claim of dedicated whole-read coverage. No live provider/guard deployment, PostgreSQL, browser, or memory profile ran. Full attachment response delivery under every provider transport remains a separate verification requirement.

## Runtime argument schema and measured exposure

Synchronous kind: `blob_discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.blobs._execute_read_attachment`](../../../../src/elspeth/web/composer/tools/blobs.py#L2175).

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
      "description": "ID of the single attachment to read completely."
    },
    "expected_content_hash": {
      "type": [
        "string",
        "null"
      ],
      "pattern": "^[0-9a-f]{64}$",
      "description": "Expected original-byte SHA256, or null for no expected identity constraint. Every supplied hash is checked."
    }
  },
  "required": [
    "blob_id"
  ],
  "additionalProperties": false
}
```
