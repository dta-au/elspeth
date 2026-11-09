# `list_blobs` — session artifact inventory

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance and design; no production change. **Overall confidence: High for the traced contracts and reproduced finding.**

## 1. Purpose and usage

Select this tool to discover files uploaded, created by the assistant, or produced by a pipeline in the current web session. It answers identity, lifecycle, size, and origin questions before metadata lookup, reading, source binding, or destructive operations. It does not inspect content or certify readiness: pending/error entries remain visible. Requires the server-owned session engine and session ID. It is available on the web Composer surface; Composer MCP intentionally excludes blob tools because it has no web session database.

## 2. Contract

Arguments are exactly `{}`; extra fields are rejected. The successful `data` is a list of entries containing `id`, `filename`, `mime_type`, `size_bytes`, `created_by`, `creation_modality`, and `status`, ordered newest first. Storage paths, descriptions, content bytes, and detailed LLM provenance are excluded. The underlying query returns at most 50 rows. There is no cursor, count, truncation flag, or continuation argument.

This is `BLOB_DISCOVERY`, with a selected producer response contract and no declared authored effects. It leaves composition version unchanged, performs a fresh metadata read on every invocation, is not eligible for the per-compose discovery cache, and does not require mutation approval. Missing session context returns a failed `ToolResult`; malformed arguments raise a safe `ToolArgumentError`. Invalid owned row enums or malformed producer output are integrity/framework failures, not coercible external input. Recognized credentials in legacy metadata cause a fixed refusal rather than partial disclosure. There is no transaction spanning this observation and a subsequent bind/update; later tools recheck their own preconditions. Repeating the read has no authored effect, although results can change as artifacts arrive.

## 3. Implementation trace

The [declaration and handler](../../../../src/elspeth/web/composer/tools/blobs.py#L587) register through the [aggregate registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111) and [`get_tool_definitions`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311). Provider wire projection changes transport shape, not these semantic arguments. [Public dispatch](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) checks credential material, matching plugin snapshot, schema, and context, then invokes the handler. [`_sync_list_blobs`](../../../../src/elspeth/web/composer/tools/blobs.py#L534) performs the scoped SQL query; the [row loader](../../../../src/elspeth/web/composer/tools/blobs.py#L472) validates owned metadata. The [credential-safe response guard](../../../../src/elspeth/web/composer/tools/blobs.py#L121) runs before the response contract is admitted by [tool-batch dispatch](../../../../src/elspeth/web/composer/tool_batch.py#L3008).

The web worker executes the synchronous operation off the event loop. [Audit finalization](../../../../src/elspeth/web/composer/audit.py#L812) records dispatch status and the semantic result separately: audit `SUCCESS` alone does not mean `ToolResult.success=true`. The [tool ribbon](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) consumes server-derived outcomes. The [MCP allowlist](../../../../src/elspeth/composer_mcp/server.py#L116) omits this tool.

## 4. Alternative paths

An empty authorized session succeeds with `[]`. A session with pending pipeline outputs includes them, so the orchestrator must inspect `status` before selecting a read/bind action. A missing/wrong session cannot expose another session's inventory. Credential-bearing legacy filenames refuse the entire result; the tool does not redact one entry and call the inventory complete. Database or owned-data faults propagate through the audited crash path. Recovery consists of correcting context or resolving the actual storage/integrity fault; there is no automatic content recovery in this metadata query.

## 5. Usability assessment

**BOS-01 — demonstrated defect, High confidence:** after 51 ready artifacts, both the ordinary and inline inventories succeed with 50 entries and silently omit the oldest artifact. Controlled 1-row and 50-row cases returned every row; a live SQL count of 51 accompanied a 50-entry result, with no completeness field. An orchestrator cannot distinguish “the requested attachment is absent” from “it is beyond the hidden window.” Repeating the same call cannot recover it. If the exact original ID is already known, metadata lookup and binding remain possible; otherwise operator assistance is needed. This is a discovery contract defect, not an authorization defect or evidence of data loss.

## 6. Structural remediation

Invariant: capability/resource discovery must state whether it is complete and provide a bounded route to remaining authorized entries. Ownership belongs to the blob inventory service and shared tool response contract, with orchestrator support for continuation. Replace duplicated fixed-window queries with a session-scoped inventory page abstraction, stable ordering and an opaque cursor; preserve the ordinary inventory's lifecycle/origin fields and the inline inventory's narrower visibility rules. Do not solve the defect by removing the bound.

Acceptance: 0, 1, 50, 51, and larger inventories enumerate every authorized row exactly once; pending rows obey each projection's filter; cross-session or forged cursors are rejected; insertions around page boundaries have specified snapshot/continuation semantics; serializers cannot drop `has_more` or equivalent evidence. Compatibility risk is Medium because the current successful root is a bare list: introduce a versioned envelope or an explicit new paginated tool and migrate model guidance deliberately. A description warning about the latest-50 limit is temporary containment only.

## 7. Evidence, gaps, and caveats

Read meaningful [inventory/origin tests](../../../../tests/unit/web/composer/test_tools_blobs.py#L3267), [metadata isolation tests](../../../../tests/unit/web/composer/test_tools_blobs.py#L216), and [producer root/order contract tests](../../../../tests/unit/web/composer/test_blob_response_contracts.py#L181). The focused seven-file blob/output/secret selection exited 0: **448 passed, 43 warnings, 66.95s**. A local SQLite probe controlled the 1/50/51 boundaries and measured stored rows with SQL, not source matching. No live provider, PostgreSQL, browser, or deployment experiment ran. The concurrent-pagination contract remains design work; this finding does not establish its future implementation.

## Runtime argument schema and measured exposure

Synchronous kind: `blob_discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.blobs._handle_list_blobs`](../../../../src/elspeth/web/composer/tools/blobs.py#L587).

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
