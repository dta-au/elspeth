# `update_blob` — replace unbound artifact bytes

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only. **Overall confidence: High for source, focused custody tests, and admission semantics.**

## 1. Purpose and usage

Use this tool to replace UTF-8 content of an existing session artifact before it is bound into a composition or retained by active work. It preserves artifact identity, filename, and MIME type while updating content hash, byte size, and creation provenance. It is not a way to silently edit inputs of a graph under review. Requires session engine/ID, data root, and exact server-owned operation authority; web-only, absent from Composer MCP.

## 2. Contract

Required arguments are `blob_id: string` and `content: string`, with extra fields rejected. Invalid UUID, non-UTF-8-encodable text, or argument shape is refused through tool/model error channels. Success returns `blob_id`, `filename`, `mime_type`, updated `size_bytes`, and `content_hash`. The declaration is `BLOB_MUTATION` with blob-store-only effects. Composition version is unchanged; there is no authored graph delta. It is not cached.

Unlike creation, replacement requires a proposal under `explicit_approve`. The handler refuses a blob referenced anywhere in the supplied composition; durable custody also guards active runs, pending proposals, in-progress forks, quota, and current lifecycle. Proposal execution supplies its server-owned proposal ID to exclude only the proposal being accepted. Provenance is recalculated for replacement bytes; the model cannot overwrite origin metadata directly.

There is no caller-supplied expected hash or idempotency key. The handler snapshots metadata and the replacement coordinator compares the exact record under custody lock before publication. Identical repeated calls can rewrite provenance and repeat durable work; internal reconciliation of an interrupted replacement is distinct from semantic idempotency of a new call. Owned corruption/stale custody must not be treated as an argument retry.

## 3. Implementation trace

The [handler/declaration](../../../../src/elspeth/web/composer/tools/blobs.py#L1694) admits [`UpdateBlobArgumentsModel`](../../../../src/elspeth/web/composer/redaction.py#L1610), acquires [exact authority](../../../../src/elspeth/web/composer/tools/blobs.py#L1487), reconciles interrupted work, reads the authoritative blob, and checks [composition references](../../../../src/elspeth/web/composer/tools/blobs.py#L1255). [`BlobReplacementCoordinator.replace_blob`](../../../../src/elspeth/web/blobs/replacement.py#L229) owns storage-root validation, old-byte verification, quota/retention checks, durable intent/stage/commit/purge phases, fencing, and rollback/recovery.

The [registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111) and [model catalogue](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311) expose the tool; [public dispatch](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) enforces schema and credentials. [Approval classification](../../../../src/elspeth/web/composer/tools/discovery.py#L111) and [tool-batch approval decision](../../../../src/elspeth/web/composer/tool_batch.py#L1729) precede effect execution. The worker supplies proposal/provenance context, and [audit finalization](../../../../src/elspeth/web/composer/audit.py#L812) records result evidence. [Frontend cards](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) distinguish completion from graph application.

## 4. Alternative paths

An unbound ready artifact can be replaced. A currently referenced artifact is rejected before replacement; unbinding and re-reviewing is the supported route. Active run/proposal/fork retention is checked by shared durable authority even if local composition inspection misses it. A missing other-session record maps to safe not-found. Invalid UTF-8 or quota denial leaves storage untouched. Interrupted swaps reconcile to the committed version before the next update/read; readers use the same custody lock and cannot pair old metadata with new bytes. Integrity anomalies escalate rather than becoming retry guidance.

## 5. Usability assessment

**BOS-04 — source-established limitation, High confidence:** the compact model description says it overwrites content while preserving metadata, but real eligibility depends on multiple retention domains and provenance changes. Those restrictions are valid safeguards; however, the orchestrator has no structured preflight eligibility query or declared retry class. It learns retention constraints from a failed mutation. Workaround: inspect bindings and run/proposal state before proposing replacement, then follow the returned refusal. A denial is not solved by repeated calls.

No data-corruption defect was demonstrated in the replacement protocol. Focused tests exercise rollback, active-work guards, and mixed-version read protection. Missing caller optimistic identity is a design limitation; the internal exact-record comparison already supplies a concurrency safeguard.

## 6. Structural remediation

Make artifact mutation eligibility, retry class, and committed effect identity part of a shared artifact lifecycle contract used by tool, API, proposal executor, and orchestrator. Keep the replacement coordinator as the owner of durable phases; do not reimplement its checks in prompts. Add a structured refusal category for retention versus missing dependency versus integrity failure. An optional expected hash can bind user intent, while a stable operation key resolves uncertain completion without redoing replacement.

Acceptance: valid unbound replacement changes exactly the intended blob; graph, active-run, pending-proposal, and in-progress-fork references each reject before publication; concurrent replacement cannot apply to an unexpected version; reads crossing swap/commit see one complete version; injected deaths at each phase recover coherently; result replay returns the committed operation outcome. Compatibility risk is Medium for optimistic identity/receipt additions and Low for richer eligibility disclosure. Preserve all current retain/refuse contracts.

## 7. Evidence, gaps, and caveats

Read [active-run and fork guards](../../../../tests/unit/web/composer/test_tools_blobs.py#L3292), [atomic-write failures](../../../../tests/unit/web/composer/test_tools_blobs.py#L3805), [mixed-version reader test](../../../../tests/unit/web/composer/test_tools_blobs.py#L4013), and [crash reconciliation cases](../../../../tests/unit/web/composer/test_tools_blobs.py#L4153). Included in the seven-file run: exit 0, **448 passed, 43 warnings, 66.95s**. This is local SQLite/unit evidence; PostgreSQL and full process-death integration tests were not run. No live provider or browser acceptance is claimed.

## Runtime argument schema and measured exposure

Synchronous kind: `blob_mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.blobs._execute_update_blob`](../../../../src/elspeth/web/composer/tools/blobs.py#L1694).

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
      "description": "ID of the blob to update."
    },
    "content": {
      "type": "string",
      "description": "New file content."
    }
  },
  "required": [
    "blob_id",
    "content"
  ],
  "additionalProperties": false
}
```
