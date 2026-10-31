# Daybreak review: Prompt Shields and Advisor gateway rejection

Review date: 2026-10-20. Source target: commit
`67ad14c22b3987a16f0e9a71155102ee451da342`, tree
`f3d8c44a1cfa0bf8284d40fa01f96d05e2aec4c9`, sole parent
`2d5c0557e2d3dfcf6f176b94e9d17290d3da7a31` (0.8.9 candidate).
The review used immutable Git object reads and the existing sanitized incident
summary. It did not import the product, run tests, call a provider or detector,
inspect the original unretained request, or change product source. Line numbers
below refer to the target commit unless marked as the parent.

## Verdict

The earlier reported `422 prompt_guard_blocked` cannot be explained by an
Azure Prompt Shields HTTP 400 merely being swallowed through the pinned client:
`blocked/policy_blocked` requires an admitted 200 response with
`attackDetected=true` (`src/elspeth/web/composer/prompt_guard_clients.py:75-92,149-171`).
The client maps every non-200 status to `unavailable/service_error`, which the
application projects as `503 prompt_guard_unavailable`
(`src/elspeth/web/composer/prompt_guard.py:108-110,152-170`). Local input
envelope refusal instead becomes `422 prompt_guard_unsupported_input`
(`prompt_guard_clients.py:38-66,129-135`; `prompt_guard.py:152-160`). The
reported *typed* response, if its record and deployed code identity are
accurate, therefore points to a detector decision. It does not prove which
part of the unretained complete request triggered that decision.

The later reported 03:53/03:55/04:01 UTC gateway `bounds_check` rejections are
a separate occurrence. On the incident parent, the SDK error was classified
`bad_request` and nonretryable, but the terminal Advisor path collapsed it to
`unavailable`, yielding misleading model-availability wording (parent
`src/elspeth/web/composer/provider_errors.py:35-38`,
`advisor_checkpoint.py:1409-1434`, `advisor_policy.py:492-499`). The new
candidate addresses that presentation loss for future nominal SDK
`BadRequestError` events. The exact historical gateway producer, violated
bound, and same-call request evidence remain unavailable in this review.

## Source trace and boundaries

| Boundary | Observed target behavior | Limit |
| --- | --- | --- |
| Guard request and detector | `_request` partitions complete untrusted units, packs without slicing and hashes the exact request (`prompt_guard.py:206-339`). The Azure client batches whole documents, accepts a strict response, and treats only boolean `attackDetected` as blocked (`prompt_guard_clients.py:38-92,129-172`). | The complete historical blocked request was not retained. A graph-containing slice triggering a detector in a separate control does not identify the original triggering unit. |
| Guard audit and application API | `enforce_prompt_guard` writes an audit envelope with `audit_id`, operation identity, role, adapter, policy fingerprint, request hash, disposition and reason before refusal (`prompt_guard.py:477-511`); the session operation maps the typed refusal to its local HTTP status and error type (`src/elspeth/web/sessions/composer_operation_errors.py:194-199`; `composer_turn.py:1804-1825`). | The aggregate guard audit does not include detector HTTP status, physical batch index, or an Azure request correlation ID. `service_error` covers distinct non-200 outcomes. This is an existing diagnostic gap, not a new regression in the candidate. |
| Model gateway and SDK | The physical model call is gated after guard admission (`src/elspeth/web/composer/provider_gateway.py:537-555`). The new classifier admits only three closed codes from nominal SDK body/code or an already buffered, bounded HTTP JSON response (`src/elspeth/web/composer/provider_errors.py:47-118,121-153`). An unknown or conflicting reason does not become actionable advice. | `invalid_request` proves only a request rejection. It does not establish a particular gateway bound, local versus upstream origin, or gateway implementation parity. An unread, malformed or oversized response may leave the reason unavailable. |
| Advisor and durable review | Advisor retains the original exception in a process-local `ProviderRequestFailure`, uses a safe `SafeRequestRejection` for public projection, and stops retrying a nominal nonretryable rejection (`src/elspeth/web/composer/advisor_checkpoint.py:390-405,1439-1458,1496-1525`). The graph gate stores the safe descriptor; strict reload reconstructs it and drops attribution on a changed graph (`src/elspeth/web/execution/completion_gates.py:227-255,296-373,376-438`). The durable checkpoint and graph-review vocabularies include `request_rejected` (`src/elspeth/web/composer/advisor_audit.py:75-76,180-197,284-285`). | Original exception identity is deliberately internal; `repr=False` is not a generic serialization guarantee. Only `details.to_wire()` is safe to project (`provider_errors.py:19-28`; `src/elspeth/contracts/provider_rejection.py:72-85`). A historical event cannot acquire a new reason retrospectively. |
| API and UI | Validation schemas carry the optional safe descriptor (`src/elspeth/web/execution/schemas.py:187-190,258-261`); explicit-state validation reloads completion gates (`src/elspeth/web/execution/routes.py:963-985`). Frontend admission checks the seven-field vocabulary and renders the Advisor detail in both validation views (`src/elspeth/web/frontend/src/api/validationReadiness.ts:7-43`; `components/execution/ValidationResult.tsx:231-250`). | The UI displays the derived explanation. It does not show the raw SDK error or prove the gateway's underlying violated bound. |

The guard implementation and provider dispatch boundary are byte-identical
between parent and target: Git blob IDs are respectively
`bd8ded7210b8ef9e83e4cc5897a2621023205c53` for `prompt_guard.py`,
`345f71ef3e705a3d7e1f0be5927992c194d40ab3` for
`prompt_guard_clients.py`, and `4a4ca9027da38a3b19c43dc0c5ac1f606da877ba`
for `provider_gateway.py`. The new patch changes rejection retention and its
downstream projections, not the detector decision mapping.

## Counterevidence, hardening, and qualification

The supplied sanitized account describes an earlier 01:12 UTC Advisor guard
block after a Composer guard allow, and a later 04:57 UTC email about three
local gateway bounds rejections. They have no established common operation,
call, policy, or request-hash chain. A future same-`audit_id` guard row with
`unavailable/service_error` paired to a `prompt_guard_blocked` response would
falsify the source-level mapping and require deployed-version or association
investigation. To identify the original detector result, use the response's
`audit_id` to find the same audit row, then verify role, policy fingerprint,
request hash, disposition and reason. A safe status/boolean receipt for the
same physical Azure call would strengthen the upstream link without raw
content.

For the later gateway event, the smallest missing evidence is an immutable
deployed gateway source/package identity, its bounds validator and HTTP error
mapper, and a same-call gateway-to-SDK correlation with a closed reason code.
A record showing adapter/upstream dispatch on that call would falsify the
claimed *local* pre-adapter mechanism. The reported short durations alone do
not prove the absence of an upstream hop.

The next targeted hardening is to retain a bounded, non-content transport
outcome for each Prompt Shields physical batch: status class, batch ordinal,
and safe correlation identifier, tied to the existing guard audit identity.
Keep the present fail-closed admission and redaction rules. This would
distinguish a future detector denial from an Azure 400 or service failure and
identify which batch returned the decision. It would not recover the missing
historical request. Any change to that audit contract needs focused negative
controls for 200/blocked, 400/unavailable, local unsupported input, malformed
response and multi-batch early exit, plus the project whole-tree gates that
scan the affected source.

Existing candidate tests include synthetic installed-SDK body loss,
safe-code/unknown-code controls and completion-gate reload
(`tests/unit/web/composer/test_provider_errors.py:109-243`,
`test_advisor_checkpoint.py:120-200,340-396`). They were read here, not run.
Prior bounded packet results recorded in the private lane are separate from
this source review. PostgreSQL, broad integration, live detector/gateway
behavior, deployment and historical causal identity are unqualified here.
