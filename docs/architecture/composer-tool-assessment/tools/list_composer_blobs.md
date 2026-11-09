# `list_composer_blobs` — ready inline-content candidates

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only. **Overall confidence: High for source contracts and the reproduced inventory defect.**

## 1. Purpose and usage

Use this narrowly scoped inventory before `wire_blob_inline_ref` when the planner needs an authorized ready artifact's identity and pinned hash without seeing its content. Unlike a general artifact list, this tool exposes only ready blobs and the metadata needed for inline-content authoring. Session engine and session ID must be supplied by the web service. It is not exposed by Composer MCP, which has no web blob custody context.

## 2. Contract

Arguments are exactly `{}`. `data` is `{"blobs": [...]}`; each item contains `blob_id`, `mime_type`, `size_bytes`, `content_hash`, and `filename`. It excludes content, previews, storage paths, descriptions, creator fields, and non-ready entries. Results are newest first and bounded to 50 records, without any completeness marker or continuation selector. A ready row with null hash raises `AuditIntegrityError` rather than fabricating an identity. Missing context returns a failed tool result; schema/model failures are safe argument errors; legacy credential-bearing metadata produces a fixed refusal.

The declaration is `BLOB_DISCOVERY`, with a dedicated response contract and no authored effects. It does not change composition version, does not enter the per-compose discovery cache, requires no mutation approval, and is safe to repeat as a fresh observation. The returned hash is an observation, not a read lease: wiring resolves authoritative metadata again, and runtime dereference verifies pinned identity. There is no automatic retry or durable idempotency receipt for this read.

## 3. Implementation trace

The [handler/declaration](../../../../src/elspeth/web/composer/tools/blobs.py#L617) is aggregated by the [registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111), then included in the canonical [model definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311). Provider-specific projection is handled by the shared wire layer. [Schema/context dispatch](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) invokes [`_sync_list_ready_blob_inline_descriptors`](../../../../src/elspeth/web/composer/tools/blobs.py#L559), whose SQL filters exact session and `ready` status. The dedicated [response admission and encoder](../../../../src/elspeth/web/composer/tools/blobs.py#L310) preserve the envelope's closed shape; the credential guard runs before outward publication.

[Tool-batch execution](../../../../src/elspeth/web/composer/tool_batch.py#L3008) runs the handler in a worker and admits its discovery response before audit/disclosure. [Audit](../../../../src/elspeth/web/composer/audit.py#L812) distinguishes successful dispatch from semantic success, and the [frontend ribbon](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) shows the server-derived outcome. [Composer MCP](../../../../src/elspeth/composer_mcp/server.py#L116) excludes blob tools.

## 4. Alternative paths

No ready files yields a successful empty list inside the envelope, even when pending/error rows exist. A malformed owned hash or response causes an integrity/framework failure. A credential-bearing legacy filename refuses the whole response. After successful inventory, a blob can become retained, replaced, or unavailable before the next action; `wire_blob_inline_ref` must therefore check readiness and pin current metadata itself. The inventory does not perform content integrity reads or storage recovery.

## 5. Usability assessment

**BOS-01 — demonstrated defect, High confidence:** a session containing 51 ready rows produces a successful 50-item `blobs` list without `has_more`, cursor, count, or truncation evidence. The oldest eligible file becomes undiscoverable through this tool. Controls with 1 and 50 ready rows returned all rows; the same SQL-backed probe measured 51 stored versus 50 returned. This can make a planner conclude that an inline source/reference does not exist when it merely falls outside the newest window. Knowing the original ID allows direct wiring; repeatedly listing does not help.

The absence of content and origin fields is an **intended visibility restriction**, not incomplete metadata leakage protection. Use the ordinary inventory or content tool when the task actually requires those facts.

## 6. Structural remediation

Make inventory completeness and bounded continuation part of the blob discovery authority, then derive both ordinary and inline projections from it. Keep this tool's ready-only, content-free contract. The orchestrator should represent “more authorized resources remain” separately from “resource unavailable.” A paginated schema must reject cross-session, expired, malformed, or wrong-projection cursors and must define stability under concurrent insertion/deletion.

Positive checks should enumerate 51+ eligible files across pages, prove content/path/description never appears, and preserve authoritative hashes. Negative checks should reject forged cursors and corrupted ready metadata, and distinguish no ready rows from an incomplete page. Compatibility risk is Medium: `data.blobs` can remain, but new pagination arguments and metadata must reach every provider projection, response admission, audit redaction, and planner instruction. Merely adding “latest 50” to prose contains confusion but leaves the capability gap.

## 7. Evidence, gaps, and caveats

Read [visibility and ready filtering test bodies](../../../../tests/unit/web/composer/test_blob_inline_tools.py#L283) and the [actual producer response tests](../../../../tests/unit/web/composer/test_blob_response_contracts.py#L181). These were in a seven-file focused selection that exited 0: **448 passed, 43 warnings, 66.95s**. The independent controlled SQL probe established the 1/50/51 boundary. No PostgreSQL contention, live provider, browser, or deployment check ran. Future pagination concurrency semantics are proposed acceptance work, not verified current behaviour.

## Runtime argument schema and measured exposure

Synchronous kind: `blob_discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.blobs._handle_list_composer_blobs`](../../../../src/elspeth/web/composer/tools/blobs.py#L617).

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
  "properties": {},
  "required": [],
  "additionalProperties": false
}
```
