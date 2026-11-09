# `validate_secret_ref` — check current reference resolvability

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance only; nothing below has been implemented. **Overall confidence: High for the current sequential contract; High for the cross-observation and scope-mismatch defects (demonstrated by in-memory dispatch runs against the real secret service over an in-memory SQLite store, not by an end-to-end service run); production frequency of the interleaving is unmeasured.**

## 1. Purpose and usage

Use this tool to refresh whether a named credential reference is accessible to the current authenticated user before attempting a permitted wire. It checks reference availability, not remote API authentication, destination authorization, or execution approval. Requires the scoped server-side resolver and user ID. Available on web Composer (freeform loop and planner); excluded from Composer MCP.

It answers exactly one question: does the **unscoped** marker `{secret_ref: NAME}` resolve under the user-first shadowing rule ([`has_ref`](../../../../src/elspeth/web/secrets/service.py#L180))? It has no `scope` argument, so it cannot answer the question execution asks for a scoped marker `{secret_ref, secret_scope}` such as the server-scoped markers that operator-profile lowering injects (§5, validate_secret_ref-N3).

## 2. Contract

Required `name: string`; extra fields reject. The name has no shape constraint: empty, reserved (`ELSPETH_*`), pattern-invalid and multi-kilobyte names are accepted and answered as ordinary unavailable references (§5, validate_secret_ref-N4). Success always returns the checked `name` and `available: bool`. If the reference appeared in the inventory, the response also carries `scope`, `source_kind`, and `reason`; otherwise it is exactly the minimal `{name, available}` shape with no reason. A false availability remains a successful metadata query. The handler never asks for a credential value.

The handler first calls `list_refs`, then independently calls `has_ref` ([`secrets.py:252-257`](../../../../src/elspeth/web/composer/tools/secrets.py#L252)). It overwrites inventory availability with the live boolean and sets reason to null when available. When unavailable, it preserves an existing inventory reason or derives a fallback (`env_var_not_set` for env/server, otherwise `value_decryption_failed`) ([`secrets.py:188-193`](../../../../src/elspeth/web/composer/tools/secrets.py#L188)). The fallback can never yield `fingerprint_resolver_not_configured`. This is not a transactionally bound resolution result and contains no observation version. The resolver preserves user-scope shadowing rather than falling back from a broken user record.

With the production stores and no concurrent change, the result is coherent:

- A user row is listed `available=True` whenever the fingerprint key is set, without decrypting it ([`user_store.py:346-405`](../../../../src/elspeth/web/secrets/user_store.py#L346)). The `value_decryption_failed` fallback is therefore the correct sequential inference when `has_ref` then fails the decrypt, but it is an inference, not an observation.
- Server inventory and `has_ref` apply the same allowlist/env/fingerprint checks ([`server_store.py:59-79`](../../../../src/elspeth/web/secrets/server_store.py#L59), [`server_store.py:113-164`](../../../../src/elspeth/web/secrets/server_store.py#L113)), so the server fallback branch is reachable only when state changes between the two observations.
- Every name `has_ref` can make true is also listed, so "unlisted but available" is reachable only under an interleaving or with a test double.

It is uncached `SECRET_DISCOVERY`, has a selected response contract, performs no authored mutation, leaves composition version unchanged, and needs no approval. Missing service/user context returns `secret_context_unavailable`. On public routes the dispatcher's schema check runs first, so malformed argument shape raises safe `ToolArgumentError` before the context guard. The handler itself checks context before argument shape, the reverse of `list_secret_refs`. That ordering is pinned by a test and does not affect public routes. Invalid owned result fields/availability-reason combinations fail response admission. Repeated validation is safe and is not suppressed on the planner route, but it does not reserve the reference for later work. Execution resolves again under its own admission boundary, and for scoped markers it asks a different question (§5).

## 3. Implementation trace

The [argument model](../../../../src/elspeth/web/composer/tools/secrets.py#L61) and [handler](../../../../src/elspeth/web/composer/tools/secrets.py#L229)/[declaration](../../../../src/elspeth/web/composer/tools/secrets.py#L266) define the call. [Selected status admission](../../../../src/elspeth/web/composer/tools/secrets.py#L158) supports exactly the rich inventory shape or minimal unlisted shape. [`_live_validation_failure_reason`](../../../../src/elspeth/web/composer/tools/secrets.py#L188) derives fallback reasons. [`WebSecretService.has_ref`](../../../../src/elspeth/web/secrets/service.py#L180), [`list_refs`](../../../../src/elspeth/web/secrets/service.py#L169) and the [scope-bound adapter](../../../../src/elspeth/web/secrets/service.py#L399) own availability semantics.

The [aggregate registry](../../../../src/elspeth/web/composer/tools/_registry.py#L94), [model definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), provider projection, and public [schema](../../../../src/elspeth/web/composer/tools/_dispatch.py#L925)/[context](../../../../src/elspeth/web/composer/tools/_dispatch.py#L935) guards in [`execute_tool`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) connect the handler to the model. [Tool-batch dispatch](../../../../src/elspeth/web/composer/tool_batch.py#L3008) admits discovery results ([`tool_batch.py:3052`](../../../../src/elspeth/web/composer/tool_batch.py#L3052)) before [audit](../../../../src/elspeth/web/composer/audit.py#L812) and provider disclosure. [Frontend tool cards](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) use server-derived outcomes; the tooltip string lives in [`toolCallDescriptions.ts`](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L54). Actual [secret evidence validation](../../../../src/elspeth/web/execution/_validation_authoring.py#L715) later checks destination authorization and resolution prerequisites independently, using [`_secret_ref_exists`](../../../../src/elspeth/web/execution/_validation_authoring.py#L700), which honours a marker's `secret_scope`.

### 3.1 Route inventory

| # | Route / surface | How it reaches, advertises or consumes this tool | Gate |
|---|---|---|---|
| 1 | Canonical registry/emitter | Declaration [`secrets.py:266`](../../../../src/elspeth/web/composer/tools/secrets.py#L266) → `TOOLS_IN_MODULE` [`secrets.py:565`](../../../../src/elspeth/web/composer/tools/secrets.py#L565) → [`_REGISTERED_TOOLS`](../../../../src/elspeth/web/composer/tools/_registry.py#L94) → [`_SECRET_DISCOVERY_TOOLS`](../../../../src/elspeth/web/composer/tools/_registry.py#L151) → [`get_tool_definitions`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311) | None at emission |
| 2 | Freeform loop advertisement | [`composer_loop_tool_definitions`](../../../../src/elspeth/web/composer/provider_gateway.py#L391) (static wire projection) | Always advertised. Production always constructs the resolver ([`app.py:2182`](../../../../src/elspeth/web/app.py#L2182)) |
| 3 | Freeform dispatch | Batch credential scan of raw wire arguments [`tool_batch.py:1031`](../../../../src/elspeth/web/composer/tool_batch.py#L1031); bounded JSON decode [`tool_batch.py:1238`](../../../../src/elspeth/web/composer/tool_batch.py#L1238); `execute_tool(..., validate_arguments=True, raise_schema_argument_errors=True)` [`tool_batch.py:3008-3038`](../../../../src/elspeth/web/composer/tool_batch.py#L3008); argument credential scan [`_dispatch.py:908`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L908); discovery admission [`tool_batch.py:3052`](../../../../src/elspeth/web/composer/tool_batch.py#L3052); audit via `dispatch_with_audit` | Schema, then secret context ([`_dispatch.py:935`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L935)) |
| 4 | Proposal/approval interception | Not intercepted: `explicit_approval_required` requires `is_mutation_tool` ([`tool_batch.py:1728-1733`](../../../../src/elspeth/web/composer/tool_batch.py#L1728)). Discovery budget classification at [`tool_batch.py:1241`](../../../../src/elspeth/web/composer/tool_batch.py#L1241), [`3383`](../../../../src/elspeth/web/composer/tool_batch.py#L3383) | n/a |
| 5 | Planner | Listed in [`PLANNER_DISCOVERY_TOOL_NAMES`](../../../../src/elspeth/web/composer/capability_skill.py#L16) ([line 36](../../../../src/elspeth/web/composer/capability_skill.py#L36)); emitted by [`planner_tool_definitions`](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647); information key `secret.reference:{name}` ([`pipeline_planner.py:461-463`](../../../../src/elspeth/web/composer/pipeline_planner.py#L461)); dispatched by [`execute_discovery_tool_with_context`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L999) via [`pipeline_planner.py:4902`](../../../../src/elspeth/web/composer/pipeline_planner.py#L4902) | Planner discovery policy may narrow the set ([`pipeline_planner.py:1663`](../../../../src/elspeth/web/composer/pipeline_planner.py#L1663)); schema ([`_dispatch.py:1027`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L1027)) then context ([`_dispatch.py:1029`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L1029)). Rereads are not suppressed ([`pipeline_planner.py:4865-4866`](../../../../src/elspeth/web/composer/pipeline_planner.py#L4865)); `DISCOVERY_NO_GAIN` has no raise site in `src/` (`grep -rn DISCOVERY_NO_GAIN src/elspeth` matches only the enum at `composer_planner_audit.py:63`, `error_codes.py:34`, a comment at `pipeline_planner.py:5095` and the e2e tutorial harness) |
| 6 | Recovery controls | No route. `recovery_controls.py` mentions credentials only to scan reply text ([L75](../../../../src/elspeth/web/composer/recovery_controls.py#L75), [L81](../../../../src/elspeth/web/composer/recovery_controls.py#L81)) | n/a |
| 7 | Composer MCP | Excluded: [`_COMPOSER_TOOL_NAMES`](../../../../src/elspeth/composer_mcp/server.py#L117) is discovery ∪ mutation only; a call by name falls to `Unknown tool` ([`server.py:425`](../../../../src/elspeth/composer_mcp/server.py#L425)) | By construction |
| 8 | Async/operation paths | No async variant; runs inside the freeform turn or planner run | n/a |
| 9 | Direct internal callers | None in `src/`; `_handle_validate_secret_ref` is referenced only by the declaration and tests ([`test_secret_rejection_contracts.py:139`](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L139), [`:262`](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L262)) | n/a |
| 10 | Guidance producers routing the agent here | Credential repair `post_hoc_form` sequence [`_common.py:1976-1977`](../../../../src/elspeth/web/composer/tools/_common.py#L1976); `missing_secret_ref` guidance [`generation.py:1912-1919`](../../../../src/elspeth/web/composer/tools/generation.py#L1912); preflight credential note [`composition_completion.py:826-835`](../../../../src/elspeth/web/composer/composition_completion.py#L826) | Fire on credential/secret rejections |
| 11 | Progress, redaction, credential-state | Safe progress text [`progress.py:638`](../../../../src/elspeth/web/composer/progress.py#L638); redaction manifest `handles_no_sensitive_data`, `known_argument_keys=("name",)` [`redaction.py:4049`](../../../../src/elspeth/web/composer/redaction.py#L4049); counted as discovery for batch credential state [`credential_guard.py:200`](../../../../src/elspeth/web/credential_guard.py#L200) | n/a |
| 12 | Frontend | Read-only description map [`toolCallDescriptions.ts:54-55`](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L54) drives the "Looked up" label in [`ToolCallCard.tsx`](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L84) and [`ComposingIndicator.tsx:370`](../../../../src/elspeth/web/frontend/src/components/chat/ComposingIndicator.tsx#L370) | n/a |
| 13 | Adjacent REST sibling (not a route to this tool) | `POST /api/secrets/{name}/validate` [`routes.py:154`](../../../../src/elspeth/web/secrets/routes.py#L154): validates the name ([`validate_secret_name`](../../../../src/elspeth/web/validation.py#L57), 422) and uses [`check_user_ref_resolvable`](../../../../src/elspeth/web/secrets/service.py#L294) with typed 503/409 errors. It is a different contract from the Composer tool | Authenticated pipeline user |
| 14 | Execution consumer | [`validate_secret_evidence`](../../../../src/elspeth/web/execution/_validation_authoring.py#L715) rechecks existence on the lowered state ([`L824`](../../../../src/elspeth/web/execution/_validation_authoring.py#L824)) and reports `missing_secret_ref` by name only, dropping scope ([`L828-838`](../../../../src/elspeth/web/execution/_validation_authoring.py#L828)) | Independent of this tool |

No tutorial-specific branch was found on any route.

## 4. Alternative paths

Listed/resolvable produces rich metadata and null reason. Listed/unresolvable produces rich metadata with a reason. Unlisted names produce minimal metadata with no reason. They are always unavailable sequentially; `available=true` appears only if a row is created between the two observations (probe P). Malformed names take the unlisted path. No-context dispatch refuses before entering the resolver. A missing/decryption/fingerprint problem never returns a value. A credential-shaped `name` is refused at the freeform batch wire scan before audit or dispatch (in-memory: `execute_tool` raised `CredentialMaterialRefused`), which is a whole-batch refusal, not a per-call argument error. An available reference can still be denied for its requested plugin destination or fail after rotation; this tool cannot eliminate that time-of-check/time-of-use boundary.

## 5. Usability assessment

**Intended restriction:** validation means reference resolvability, not credential correctness against a remote service. No live remote authentication was exercised or implied by `available=true`.

**validate_secret_ref-N1 — Demonstrated (in-memory dispatch run, deterministic mutation injected between `list_refs` and `has_ref`; not a timed race, not an end-to-end service run).** The result combines two observations, and a change between them yields a result that describes neither state. This replaces the earlier "unresolved hypothesis":

| Probe | Injected change | Returned | Actual state |
|---|---|---|---|
| M | User row shadowing a server ref deleted | `scope: user, source_kind: user_store, available: true` | Live `true` came from the **server** store |
| N | User row deleted, no server ref | `available: false, reason: value_decryption_failed` | Reference absent; nothing failed to decrypt |
| O | Fingerprint key removed | `available: false, reason: value_decryption_failed` | `fingerprint_resolver_not_configured` |
| P | User row created | Minimal `{name, available: true}` | User-scope reference exists |

M, N and P need only a concurrent user-secret create/delete via `/api/secrets`. O (and an env-cleared server variant, which happens to report the correct `env_var_not_set`) needs a process-environment change, which is unrealistic in production. The tool therefore labels a deletion as a decryption failure, and the [preflight note](../../../../src/elspeth/web/composer/composition_completion.py#L830) tells the agent to report that reason and the layer it names.

**validate_secret_ref-N2 — Source-established.** Reasons are partly inferred, and the result does not say which. A fallback reason derived at [`secrets.py:188-193`](../../../../src/elspeth/web/composer/tools/secrets.py#L188) cannot be distinguished from an observed one. The fallback never produces `fingerprint_resolver_not_configured`. Typed store signals (`FingerprintKeyMissingError`, `SecretDecryptionError`) are collapsed to `False` by `has_ref`, while the REST sibling surfaces them as 503/409 through `check_user_ref_resolvable`. That method is not on the `WebSecretResolver` surface the handler receives.

**validate_secret_ref-N3 — Source-established, both directions demonstrated in memory against the execution helper.** The tool answers only the unscoped, user-first question. Execution checks a scoped marker with `resolve_scoped` and no shadowing ([`_validation_authoring.py:700-713`](../../../../src/elspeth/web/execution/_validation_authoring.py#L700)), and profile lowering writes `{secret_ref, secret_scope: "server"}` markers ([`profiles.py:1395`](../../../../src/elspeth/web/plugin_policy/profiles.py#L1395)). Probe output:

- User row good, server env unset: tool `available: true, scope: user`; execution unscoped `True`, **server-scoped `False`**.
- User row undecryptable, server env good: tool `available: false, reason: value_decryption_failed`; execution unscoped `False`, **server-scoped `True`**.

The `missing_secret_ref` error drops the scope ([`_validation_authoring.py:824`](../../../../src/elspeth/web/execution/_validation_authoring.py#L824)) and then routes the agent to this tool ([`generation.py:1916`](../../../../src/elspeth/web/composer/tools/generation.py#L1916)), which can give the opposite answer for a profile credential. The full profile-lowering → preflight → agent chain was not run end to end.

**validate_secret_ref-N4 — Demonstrated (in-memory public dispatch).** Malformed names return `success: true, available: false`, the same shape as a well-formed missing name: `""`, `ELSPETH_FINGERPRINT_KEY`, `"not a valid/name!"` and a 5,000-character name were all accepted. The shared contract [`validate_secret_name`](../../../../src/elspeth/web/validation.py#L57) (non-empty, ≤256, `^[A-Za-z][A-Za-z0-9_]*$`) is applied by the REST sibling but not by this tool, so a typo'd or invented name gives the agent no signal. On the freeform wire the name is bounded only by the generic 1 MiB raw-argument budget ([`bounded_json.py:15`](../../../../src/elspeth/web/composer/bounded_json.py#L15), checked at [`bounded_json.py:61-62`](../../../../src/elspeth/web/composer/bounded_json.py#L61)). The 65,536-char per-string constant is not applied to decoded string values ([`bounded_json.py:118`](../../../../src/elspeth/web/composer/bounded_json.py#L118) passes `enforce_string_limit=False`). The full name is echoed into the result and the audit row.

**validate_secret_ref-N5 — Source-established.** An unlisted unavailable result has no reason. "Not in this user's inventory", "not allowlisted", "reserved", and "malformed" all collapse into `{name, available: false}`. The description promises "otherwise the reason it cannot resolve" only for inventory hits, but downstream guidance assumes a reason is always present (§5.1).

**validate_secret_ref-N6 — Source-established, low impact.** The direct handler checks context before argument shape, while `list_secret_refs` validates arguments first. [`test_missing_context_does_not_reorder_argument_admission`](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L257) pins this asymmetry. Public routes validate the schema first, so the asymmetry is visible only to direct callers.

**Intended restriction (agent-visible consequence unverified):** a credential-shaped `name` is refused before audit by the batch wire scan ([`tool_batch.py:1031`](../../../../src/elspeth/web/composer/tool_batch.py#L1031)). How the freeform turn presents that whole-batch `ComposerAdmissionRefused` to the agent was not traced here.

Workaround until fixed: treat `scope`/`reason` as diagnostic hints, re-validate immediately before relying on them, and treat any `missing_secret_ref` for a profile-backed plugin as an operator-side server-secret question rather than trusting this tool's answer.

### 5.1 Guidance parity

Grep control: `grep -c validate_secret_ref` returns 1 for `pipeline_composer.md` (the autogenerated inventory line 345, a known mention) and 0 for `pipeline_capabilities.md`; `grep -c list_secret_refs` returns 1 for `pipeline_capabilities.md` (line 88), so the instrument matches that file. `grep secret_scope` over both skills matches nothing, and the same grep over `src/` finds the profile marker at `profiles.py:1395`, so the pattern itself is sound.

| ID | Source | What the LLM is told | What the handler does | Finding |
|---|---|---|---|---|
| G1 | [`composition_completion.py:828-831`](../../../../src/elspeth/web/composer/composition_completion.py#L828) | "If a secret is unavailable, report the returned reason (… three values) and the layer it identifies." | Unlisted results have no reason (N5); a listed reason may be inferred or wrong after an interleaving (N1/N2) | Wrong guidance: assumes a reason always exists and is authoritative |
| G2 | [`generation.py:1912-1919`](../../../../src/elspeth/web/composer/tools/generation.py#L1912), `composition_completion.py:828` | On `missing_secret_ref`, use `list_secret_refs` and `validate_secret_ref` | Tool ignores `secret_scope`; scoped profile markers can disagree (N3). `generation.py` correctly says "reason where present" | Pushes the agent to a tool that answers a different question for profile credentials |
| G3 | [`pipeline_capabilities.md:88-89`](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L88) | "Read the complete `list_secret_refs` result … validate the selected reference" | Correct, but the skill neither names the tool nor says why: user-scope inventory `available` is existence-only and does not decrypt | Missing rationale and result-shape guidance (minimal vs rich shape, what a missing reason means) |
| G4 | [`pipeline_composer.md:345`](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L345), [`:103`](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L103) | Tool appears only in the autogenerated inventory and a generic "use the relevant discovery tool" row | No secret workflow, result-shape or scope guidance | Missing guidance; the secret workflow is taught only by runtime rejection text |
| G5 | Declaration description [`secrets.py:271-275`](../../../../src/elspeth/web/composer/tools/secrets.py#L271) | "Check if a secret reference exists and is accessible … otherwise the reason it cannot resolve" | Existence is not reported separately; reason is inferred and only present for inventory hits; check is unscoped | Overclaim (GS-03 class), mild |
| G6 | [`toolCallDescriptions.ts:54-55`](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L54) | "Checks that a credential reference exists and is accessible." | As G5 | User-facing overclaim, mild |

No contradiction between the two skills was found. Neither references a removed tool or argument. Both are silent on `secret_scope`, which is appropriate while the composer authors only bare markers, but that silence matters once a profile-injected reference fails (G2).

## 6. Structural remediation

Add one value-free `inspect_reference(user_id, name, scope | None)` operation to the secret authority that returns selected scope, availability, observed (not inferred) reason, and an observation identity together from a single resolution attempt. It should reuse the typed failure classes `check_user_ref_resolvable` already distinguishes, with no value returned. Derive the list and point-validation views from the same semantics. The tool handler should not invent a reason for a new boolean. Add an optional `scope` argument to the tool, mirroring `secret_scope`, so the agent can ask the question execution will ask. Have `missing_secret_ref` carry each missing reference's scope. Preserve scoped shadowing for unscoped markers, no-value disclosure, and deny-by-default destination permission. The orchestrator should distinguish unavailable reference, deployment resolver failure, destination denial, and later remote authentication failure.

Acceptance: listed/unlisted and available/unavailable cases preserve current safe shapes. The controlled interleavings M/N/O/P either return one coherent snapshot or explicit "changed/retry" metadata. Unknown causes are never labelled as decryption failures without evidence. A scoped query agrees with `_secret_ref_exists` for both directions of N3. Execution rechecks all refs. Malformed owned data and unauthorized names fail closed. Compatibility risk is Medium for a resolver interface/result change. Additive observation facts and an optional `scope` argument preserve current public tool arguments.

### 6.1 Simple enhancements

| ID | What | Where | Why | Acceptance check |
|---|---|---|---|---|
| E1 | Validate `name` with `validate_secret_name` in the argument model and declare `minLength`/`maxLength: 256`/`pattern` in the schema | [`secrets.py:61-72`](../../../../src/elspeth/web/composer/tools/secrets.py#L61), [`secrets.py:276-283`](../../../../src/elspeth/web/composer/tools/secrets.py#L276) | N4: malformed names currently look like absent references | `""`, a 257-char name and `"bad name"` raise `ToolArgumentError` on freeform and planner seams; a valid unknown name still returns the minimal shape |
| E2 | Re-read the matched inventory item after `has_ref`; if name/scope/source_kind/inventory availability changed, drop the inventory-derived `scope`/`source_kind`/`reason` and return the minimal shape, or report a changed observation | [`secrets.py:251-263`](../../../../src/elspeth/web/composer/tools/secrets.py#L251) | N1: narrows (does not close) the window where M/N report a stale scope or false cause | Probes M and N no longer return `scope: user` from a deleted row or `value_decryption_failed` for a deletion |
| E3 | Rewrite the declaration description: unscoped user-first check; absence of `scope` means "not in your inventory"; `reason` is present only for inventory hits and may be derived; availability does not authorize a destination | [`secrets.py:271-275`](../../../../src/elspeth/web/composer/tools/secrets.py#L271) | G5 | Declaration test pins the new wording; no "exists" claim beyond what the shape encodes |
| E4 | Change the preflight note to "report the returned reason where present; a result without `scope` means the name is not in your inventory" | [`composition_completion.py:828-831`](../../../../src/elspeth/web/composer/composition_completion.py#L828) | G1 | Note text matches `generation.py:1916` "where present" wording; test asserts it |
| E5 | Include each missing reference's scope in the `missing_secret_ref` message, and say when it is server-scoped (operator-owned) | [`_validation_authoring.py:824-838`](../../../../src/elspeth/web/execution/_validation_authoring.py#L824) | N3/G2: the agent cannot tell a profile credential from its own reference | A profile-lowered server-scoped miss names `scope=server`; unscoped misses are unchanged |
| E6 | Add characterization tests for the M/N/O/P interleavings, the malformed-name cases, and both N3 directions using the real `WebSecretService` over in-memory SQLite (the existing tests use doubles or sequential stale inventory) | `tests/unit/web/composer/test_secret_rejection_contracts.py` | Locks current behaviour so E1/E2/§6 changes are measured | Tests pass against current code with the current outputs recorded, then flip intentionally with the fix |
| E7 | Tooltip: "Checks whether a credential reference currently resolves for you — names only, never values." | [`toolCallDescriptions.ts:54-55`](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L54) | G6 | Frontend description test updated |
| E8 | One paragraph in the capabilities skill naming `validate_secret_ref`, its two result shapes, that user-scope inventory availability is existence-only, and that availability ≠ destination authorization | [`pipeline_capabilities.md:88-91`](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L88) | G3/G4 | Skill text names the tool; skill-hash pins updated deliberately |

## 7. Evidence, gaps, and caveats

Read [sequential stale-inventory tests](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L459), [`test_tools.py:1691-1762`](../../../../tests/unit/web/composer/test_tools.py#L1691), [status response tests](../../../../tests/unit/web/composer/test_secret_response_contracts.py#L106) (which also admit an empty `name` in the minimal shape), [shadowing tests](../../../../tests/unit/web/secrets/test_service.py#L790), and the resolver/store source. Included in the seven-file run: exit 0, **448 passed, 43 warnings, 66.95s**.

Added in this review: two in-memory dispatch probes against the real `WebSecretService`/`ScopedSecretResolver` over an in-memory SQLite session store, the real catalog snapshot, and public `execute_tool` / `execute_discovery_tool_with_context` (both exit 0). The interleavings were injected deterministically by wrapping `list_refs` with real store mutations; this is not a timed race, not a concurrent HTTP request, and not an end-to-end service run. No key vault, live provider, browser, PostgreSQL, or profile-lowering → preflight chain was run. N3 compares the tool against `_secret_ref_exists` directly. No test suite was run in this review.

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
