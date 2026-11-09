# `list_secret_refs` — discover credential reference metadata

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance/design only. **Overall confidence: High for inspected metadata and custody contracts.**

## 1. Purpose and usage

Select this tool before credential wiring when the orchestrator needs to know which named references are available to the current authenticated user. It discovers names/scopes/availability, never values. It does not grant permission to place any reference in any plugin option, and it does not approve execution. Requires the server-supplied scoped secret resolver and current user ID. Web Composer exposes it; Composer MCP deliberately excludes secret tools.

## 2. Contract

Arguments are exactly `{}`; extra keys reject. Successful `data` is a list of metadata objects with `name`, `scope`, `available`, `source_kind`, and `reason`. Scope is user/server; absence/decryption/fingerprint reasons use a closed vocabulary. A false availability is a successful discovery fact, not a failed tool operation. Real secret bytes never enter the payload or tool argument model.

The underlying resolver merges curated server and user inventories, sorts names, and lets a user-scoped record shadow a same-name server record, even when the user record cannot resolve. Server-only deployments omit user records. Server inventory only contains operator-allowlisted names and excludes reserved ELSPETH internals. A missing fingerprint resolver makes references unavailable with a deployment-specific reason; the correct response is to surface that condition, not invent another secret.

This is uncached `SECRET_DISCOVERY` with a selected response contract, no authored effects, unchanged composition version, and no mutation approval. Missing resolver/user context returns `secret_context_unavailable` and prior graph findings; malformed owned metadata is a framework failure. A result is an observation, not a lock or reservation: rotation/deletion can change availability before validation, wiring, or execution. Repeating the discovery is safe and fresh; there is no automatic retry or idempotency receipt for this read.

## 3. Implementation trace

The [handler/declaration](../../../../src/elspeth/web/composer/tools/secrets.py#L196) admits empty arguments and calls `secret_service.list_refs(user_id)`. The [payload/selected response contract](../../../../src/elspeth/web/composer/tools/secrets.py#L100) admits exact metadata fields and reconstitutes owned `SecretInventoryItem` values. [`WebSecretService.list_refs`](../../../../src/elspeth/web/secrets/service.py#L169) owns scope precedence; [`ScopedSecretResolver`](../../../../src/elspeth/web/secrets/service.py#L399) binds the authenticated provider scope. [Server store](../../../../src/elspeth/web/secrets/server_store.py#L113) and [user store](../../../../src/elspeth/web/secrets/user_store.py#L346) own inventory availability and reasons.

The [registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111), [model definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), provider wire layer, and [public context guard](../../../../src/elspeth/web/composer/tools/_dispatch.py#L935) expose and admit the operation. [Worker dispatch/response admission](../../../../src/elspeth/web/composer/tool_batch.py#L3008) precede [audit finalization](../../../../src/elspeth/web/composer/audit.py#L812). The [frontend tool card](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) displays the server-owned outcome. The redaction manifest treats these as reference metadata, not credential values.

## 4. Alternative paths

Empty inventory succeeds. Listed but unavailable references remain visible with their reason. Same-name user records shadow server records; a failing user record must not silently fall back to a server credential. Missing context refuses without calling the resolver. A malformed owned status/reason/extra payload fails admission rather than serializing arbitrary data. Availability can change after listing; execution therefore resolves again. A destination allowlist denial cannot be repaired by repeatedly listing or choosing another spelling of the same reference.

## 5. Usability assessment

No confirmed defect was demonstrated for this tool. **Intended restriction:** availability and wiring permission are different authorities. The inventory supplies no allowed-destination matrix, so an orchestrator cannot infer from `available=true` that `wire_secret_ref` will be authorized. That is a material discoverability limitation, not evidence that the deny-by-default control is wrong. Workaround: follow the target plugin's secret requirements and treat an exact destination denial as an operator policy decision; never ask the model to supply literal credentials.

Metadata is not durable resolution evidence. A successful list with unavailable entries should lead to an explicit prerequisite or operator action, rather than false completion.

## 6. Structural remediation

Expose a value-free secret capability view that combines resolver metadata with only the destination authorization information the current user may see. Keep credential storage/resolution in the secret service, plugin secret-field semantics in the plugin contract, deployment allowlist in web policy, and run approval outside model control. Reuse that view in discovery, wiring, validation, and execution disclosure without allowing a discovery answer to grant authority.

Acceptance: inventories never expose values/reserved names; shadowing and server-only modes remain consistent; allowed and denied destinations are explainable without resolving credentials; rotation/deletion causes revalidation; unknown/malformed response shapes fail closed; available metadata alone cannot bypass authorization or run acknowledgement. Compatibility risk is Low for additive capability metadata and Medium for expanding the response envelope/redaction policy. No migration or secret-store reset is proposed.

## 7. Evidence, gaps, and caveats

Read [secret response-contract tests](../../../../tests/unit/web/composer/test_secret_response_contracts.py), [context guard tests](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L146), [availability metadata tests](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L459), and backing scope-resolution source. Included in the seven-file focused run: exit 0, **448 passed, 43 warnings, 66.95s**. Those tests use contract doubles for some resolver states; no real key vault, secret rotation race, browser, PostgreSQL, or live provider ran. Permission-discovery design is a recommendation, not a verified current capability.

## Runtime argument schema and measured exposure

Synchronous kind: `secret_discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.secrets._handle_list_secret_refs`](../../../../src/elspeth/web/composer/tools/secrets.py#L196).

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
