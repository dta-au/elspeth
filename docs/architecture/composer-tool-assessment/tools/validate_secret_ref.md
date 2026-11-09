# `validate_secret_ref` — check current reference resolvability

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only. **Overall confidence: High for current sequential contract; Moderate for concurrency implications.**

## 1. Purpose and usage

Use this tool to refresh whether a named credential reference is accessible to the current authenticated user before attempting a permitted wire. It checks reference availability, not remote API authentication, destination authorization, or execution approval. Requires the scoped server-side resolver and user ID. Available on web Composer; excluded from Composer MCP.

## 2. Contract

Required `name: string`; extra fields reject. Success always returns the checked `name` and `available: bool`. If the reference appeared in the inventory, the response also carries `scope`, `source_kind`, and `reason`; otherwise it is exactly the minimal name/availability shape. A false availability remains a successful metadata query. The handler never asks for a credential value.

The handler first calls `list_refs`, then independently calls `has_ref`. It overwrites inventory availability with the live boolean and sets reason to null when available. When unavailable, it preserves an existing reason or derives a fallback (`env_var_not_set` for env/server, otherwise `value_decryption_failed`). This is not a transactionally bound resolution result and contains no observation version. The resolver preserves user-scope shadowing rather than falling back from a broken user record.

It is uncached `SECRET_DISCOVERY`, has a selected response contract, performs no authored mutation, leaves composition version unchanged, and needs no approval. Missing service/user context returns `secret_context_unavailable`; malformed argument shape raises safe `ToolArgumentError`; invalid owned result fields/availability-reason combinations fail response admission. Repeated validation is safe but does not reserve the reference for later work. Execution resolves again under its own admission boundary.

## 3. Implementation trace

The [argument model](../../../../src/elspeth/web/composer/tools/secrets.py#L61) and [handler/declaration](../../../../src/elspeth/web/composer/tools/secrets.py#L229) define the call. [Selected status admission](../../../../src/elspeth/web/composer/tools/secrets.py#L158) supports exactly the rich inventory shape or minimal unlisted shape. [`_live_validation_failure_reason`](../../../../src/elspeth/web/composer/tools/secrets.py#L188) derives fallback reasons. [`WebSecretService.has_ref`](../../../../src/elspeth/web/secrets/service.py#L180) and [scope-bound adapter](../../../../src/elspeth/web/secrets/service.py#L399) own availability semantics.

The [aggregate registry](../../../../src/elspeth/web/composer/tools/_registry.py#L111), [model definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), provider projection, and [public schema/context guards](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) connect the handler to the model. [Tool-batch dispatch](../../../../src/elspeth/web/composer/tool_batch.py#L3008) admits discovery results before [audit](../../../../src/elspeth/web/composer/audit.py#L812) and provider disclosure. [Frontend tool cards](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) use server-derived outcomes. Actual [secret evidence validation](../../../../src/elspeth/web/execution/_validation_authoring.py#L715) later checks destination authorization and resolution prerequisites independently.

## 4. Alternative paths

Listed/resolvable produces rich metadata and null reason. Listed/unresolvable produces rich metadata with a reason. Unlisted names produce minimal metadata even if `has_ref` reports true. No-context dispatch refuses before entering the resolver. A missing/decryption/fingerprint problem never returns a value. An available reference can still be denied for its requested plugin destination or fail after rotation; this tool cannot eliminate that time-of-check/time-of-use boundary.

## 5. Usability assessment

**Intended restriction:** validation means reference resolvability, not credential correctness against a remote service. No live remote authentication was exercised or implied by `available=true`.

**Unresolved hypothesis, Moderate confidence:** because metadata and availability come from two independent observations, a concurrent user/server shadowing change or deletion can make returned scope/reason describe a different observation than the live boolean. The fallback reason is inferred from earlier metadata rather than returned by the same live probe. Sequential tests deliberately exercise stale availability, but this lane did not reproduce a real store mutation interleaving. Workaround: treat reasons as diagnostic hints and revalidate at execution, not durable identity/authority. A concrete concurrency test is required before classifying a false-cause report as a demonstrated defect.

## 6. Structural remediation

Add one value-free `inspect_reference` operation to the secret authority that returns selected scope, availability, reason, and observation identity together. Derive list and point-validation views from the same semantics; the tool handler should not invent a reason for a new boolean. Preserve scoped shadowing, no-value disclosure, and deny-by-default destination permission. The orchestrator should distinguish unavailable reference, deployment resolver failure, destination denial, and later remote authentication failure.

Acceptance: listed/unlisted and available/unavailable cases preserve current safe shapes; a controlled rotation/deletion/shadow change between observations either returns one coherent snapshot or explicit “changed/retry” metadata; unknown causes are not labelled as decryption failures without evidence; execution rechecks all refs; malformed owned data and unauthorized names fail closed. Compatibility risk is Medium for a resolver interface/result change; additive observation facts can preserve current public tool arguments.

## 7. Evidence, gaps, and caveats

Read [sequential stale-inventory tests](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L459), [status response tests](../../../../tests/unit/web/composer/test_secret_response_contracts.py#L106), and the resolver/store source. Included in the seven-file run: exit 0, **448 passed, 43 warnings, 66.95s**. The race is explicitly unproven: no real rotation/deletion interleaving, key vault, live provider, browser, or PostgreSQL check ran. The recommendation may be simplified if a direct controlled interleaving shows the existing result is already coherent for supported deployments.

## Runtime argument schema and measured exposure

Synchronous kind: `secret_discovery`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.secrets._handle_validate_secret_ref`](../../../../src/elspeth/web/composer/tools/secrets.py#L229).

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
    "name": {
      "type": "string",
      "description": "Secret reference name (e.g. 'OPENROUTER_API_KEY')."
    }
  },
  "required": [
    "name"
  ],
  "additionalProperties": false
}
```
