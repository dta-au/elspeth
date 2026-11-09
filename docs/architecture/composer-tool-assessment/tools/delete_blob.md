# `delete_blob` — retire an unretained session artifact

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only. **Overall confidence: High for traced custody semantics and focused tests.**

## 1. Purpose and usage

Select this tool to delete a session artifact after removing composition bindings and when durable work no longer retains it. It deletes artifact storage and metadata, not graph components or historical audit records. Requires session engine/ID, data root, and a live exact operation authority. Composer MCP excludes it. Destruction is subject to explicit approval when the session uses that mode.

## 2. Contract

Required `blob_id: string`; extra fields are forbidden. The UUID boundary returns repairable error guidance for invalid IDs. Success returns `{"blob_id": ..., "deleted": true}` with the original composition state/version. It is an uncached `BLOB_MUTATION`, blob-store-only effect. It must not be classified as a read because its result is constructed with a discovery-shaped envelope.

Deletion is refused for composition references, active runs, pending proposals, and in-progress forks. Ownership uses exact session authority, not an ambient blob ID. Missing context/data root returns failure; stale or mismatched authority is an integrity/coordination failure. A missing ordinary blob is not automatically a successful no-op. A durable deletion already committed but awaiting cleanup can be completed by retry through the deletion ledger. Thus “retry after interrupted committed deletion” and “repeat an already completed deletion with no receipt” are different paths.

## 3. Implementation trace

The [handler and declaration](../../../../src/elspeth/web/composer/tools/blobs.py#L1809) parse `BlobIdArgumentsModel`, obtain [exact authority](../../../../src/elspeth/web/composer/tools/blobs.py#L1487), acquire session blob custody, reconcile other deletion obligations, and inspect current composition references. [`BlobServiceImpl._delete_blob_with_ledger`](../../../../src/elspeth/web/blobs/service.py#L3118) owns the actual tombstone/metadata/cleanup lifecycle and durable retention guards. Its [reconciliation entry](../../../../src/elspeth/web/blobs/service.py#L2546) is shared with the authenticated API.

Registration and exposure use the [aggregate registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111) and [canonical definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311). [Schema/credential dispatch](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) is called from the [web worker](../../../../src/elspeth/web/composer/tool_batch.py#L3008). [Destructive blob-only approval classification](../../../../src/elspeth/web/composer/tools/discovery.py#L111) prevents the no-graph-delta classification from bypassing approval. [Audit](../../../../src/elspeth/web/composer/audit.py#L812) and [frontend tool cards](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) preserve the semantic outcome independently of graph version.

## 4. Alternative paths

Unbound/unretained deletion proceeds through the shared ledger and reports completion only after the driver returns. Bound artifacts are refused without altering the graph. A missing metadata row can still have a committed cleanup obligation; the driver distinguishes that from genuine absence. If filesystem cleanup was interrupted after metadata commit, a retry can finish the tombstone purge. Foreign-session IDs remain inaccessible. A caller may not supply a proposal ID to bypass retention: it comes only from server-owned execution context.

## 5. Usability assessment

The terse model description does not explain retention eligibility or the difference between completed deletion and recoverable cleanup. **BOS-04 — source-established lifecycle limitation, High confidence:** generic tool retry logic lacks a declared category for an interrupted committed effect, even though the storage driver has that state. The orchestrator must inspect the semantic result and preserve effect evidence; a version comparison alone cannot establish whether deletion happened.

Current composition/run/proposal/fork refusal is an **intended restriction**. No defect in durable deletion was demonstrated by this lane. Workaround for refusal is to release the actual retaining workflow through its normal lifecycle; repeatedly asking to delete must not weaken retention.

## 6. Structural remediation

Expose a shared artifact-effect outcome with operation identity and states such as refused, committed, and cleanup pending, backed by the existing ledger. Keep eligibility and cleanup ownership in the blob custody service. Tool declarations should advertise destructive approval and retry semantics, and orchestration should recover through the receipt rather than guessing from missing rows or graph state.

Acceptance: deletion of a valid unretained blob removes storage and metadata; each independent retention reason refuses without storage change; cross-session and stale-fence requests fail closed; deaths before and after metadata commit reconcile correctly; replay of a committed deletion returns its prior outcome; malformed IDs do not enter durable work. Compatibility risk is Medium for new result/receipt semantics, especially changing current missing-row failures into idempotent outcomes. Preserve current retention restrictions and historical audit evidence.

## 7. Evidence, gaps, and caveats

Read [successful deletion and cleanup assertions](../../../../tests/unit/web/composer/test_tools_blobs.py#L4453), [post-commit retry test](../../../../tests/unit/web/composer/test_tools_blobs.py#L4476), and [nested composition-reference guards](../../../../tests/unit/web/composer/test_tools_blobs.py#L4632). They were included in the seven-file focused run: exit 0, **448 passed, 43 warnings, 66.95s**. These checks establish local unit/SQLite behaviour, not a full PostgreSQL or process-death deployment verdict. No live provider or browser interaction ran.

## Runtime argument schema and measured exposure

Synchronous kind: `blob_mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.blobs._execute_delete_blob`](../../../../src/elspeth/web/composer/tools/blobs.py#L1809).

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
      "description": "ID of the blob to delete."
    }
  },
  "required": [
    "blob_id"
  ],
  "additionalProperties": false
}
```
