# `get_blob_metadata` — exact artifact metadata lookup

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only. **Overall confidence: High for inspected contracts and local reproductions.**

## 1. Purpose and usage

Select this tool when a blob ID is already known and the orchestrator needs current lifecycle, byte size, MIME type, filename, or hash before reading or binding it. It avoids disclosing file contents and host storage paths. Requires the web session engine and exact current session ID; an ID alone grants no cross-session access. Composer MCP does not expose it.

## 2. Contract

The sole required argument is `blob_id: string`; additional arguments are forbidden. A UUID-shape helper returns a repairable failure for invalid UUIDs. Success returns `data.id`, `filename`, `mime_type`, `size_bytes`, `content_hash`, and `status`. Pending/error metadata can be returned and must not be treated as readable content. The response is allowlisted; `storage_path`, source descriptions, secret values, and detailed creator provenance do not leave the handler.

It is an uncached `BLOB_DISCOVERY` operation with a dedicated response contract, no authored state effects, unchanged composition version, and no approval requirement. Missing session context or no matching row returns failure. Wrong-session lookup returns the same safe not-found result. Owned row corruption and invalid producer shapes escalate. Credential-bearing legacy metadata refuses disclosure. Reads can be repeated safely, but there is no snapshot guarantee between this metadata observation and later content access.

## 3. Implementation trace

[Handler and declaration](../../../../src/elspeth/web/composer/tools/blobs.py#L658) enter the [aggregate registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111) and [model catalogue](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311). [Public dispatcher](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) enforces schema, credentials, and request-scoped catalog identity. [`_blob_id_uuid_validation_error`](../../../../src/elspeth/web/composer/tools/blobs.py#L518) checks parseability, then [`_sync_get_blob`](../../../../src/elspeth/web/composer/tools/blobs.py#L499) queries ID plus session. The handler constructs a new allowlisted payload rather than returning the row wholesale; credential checking precedes selected response admission in [tool-batch execution](../../../../src/elspeth/web/composer/tool_batch.py#L3008).

[Audit status](../../../../src/elspeth/web/composer/audit.py#L812) describes dispatch separately from semantic tool success. The [frontend tool card](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) displays server-owned outcome evidence. [MCP's common-tool allowlist](../../../../src/elspeth/composer_mcp/server.py#L116) omits blob tools.

## 4. Alternative paths

Canonical ID plus same-session row succeeds regardless of readiness; canonical ID from another session or an absent row fails without exposing storage details. An invalid UUID produces static repair guidance. A credential-bearing filename can block otherwise valid metadata. Metadata access does not enter the content custody/recovery lock and does not verify file bytes; callers needing current bytes must use the content read authority rather than reconstruct filesystem paths from metadata.

## 5. Usability assessment

**BOS-03 — demonstrated admission inconsistency, High confidence:** the UUID helper accepts alternate valid spellings, but `_sync_get_blob` compares the unnormalised string to canonical database text. The controlled probe looked up the same ID in canonical lowercase and uppercase: canonical succeeded; uppercase returned failure in each 1/50/51 fixture. `wire_blob_inline_ref` instead constructs `UUID` and looks up `str(UUID(...))`. Thus accepted UUID identity has route-dependent meaning. Workaround: use the exact ID returned by inventory/create; the defect does not grant cross-session access.

The lack of content inspection is an intended separation. A returned `ready` status alone is not proof that backing bytes still exist or match the stored hash.

## 6. Structural remediation

Invariant: one admitted blob identity denotes the same resource across tools. Put canonical UUID parsing in one owned boundary value/helper and pass canonical IDs into all repository operations. Apply it to metadata, source binding, inline wiring, read, update, and deletion routes; do not add special string comparisons per handler. Alternatively reject noncanonical spellings consistently, but document that explicit supported boundary.

Acceptance: canonical, uppercase, braced, and hyphenless valid representations either resolve identically within one session or all noncanonical forms are rejected at admission with the same typed error; invalid IDs and IDs from other sessions never reach privileged reads. Compatibility risk is Low when normalising accepted UUIDs; a newly strict rejection policy would need callers reviewed. Separately preserve the content authority's hash/read fencing and the metadata response allowlist.

## 7. Evidence, gaps, and caveats

Read [metadata exclusion, invalid-ID, and wrong-session tests](../../../../tests/unit/web/composer/test_tools_blobs.py#L141) and producer response-contract tests. Included in the seven-file focused run: exit 0, **448 passed, 43 warnings, 66.95s**. Independent SQL-backed probes confirmed canonical versus uppercase divergence with positive controls. No live provider, PostgreSQL, or frontend runtime ran. The alternate spelling finding concerns lookup consistency, not evidence that concurrent byte reads are unsafe.

## Runtime argument schema and measured exposure

Synchronous kind: `blob_discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.blobs._handle_get_blob_metadata`](../../../../src/elspeth/web/composer/tools/blobs.py#L658).

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
      "description": "Blob ID."
    }
  },
  "required": [
    "blob_id"
  ],
  "additionalProperties": false
}
```
