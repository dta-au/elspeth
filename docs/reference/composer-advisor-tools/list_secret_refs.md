# `list_secret_refs` — tool reference and intended contract

`composer.list_secret_refs` discovers the current caller's secret-reference **metadata**, including unavailable entries. It lets the Composer choose a reference to validate and wire without receiving its value. Listing is not proof that a stored user credential decrypts or will remain available at execution.

| Version and scope | Value |
|---|---|
| Tool identity | `composer.list_secret_refs`; wire name `list_secret_refs`; no supported alternate name established |
| Contract | `1.0-candidate`; proposed documentation, not an adopted product-interface change |
| Implementation | Base `9c22be98e4323465c3472188a034f893cfec983e`, tree `086dd40185fd6808d86092f353421f7dde592301`, plus dirty declaration-only correction and tests bound in r003; release number not established by this audit |
| Surfaces | Ordinary Composer and default planner local builders, `none` and `openai_strict`; local composer MCP excludes secret tools |
| Evidence | Source dossier plus narrow executed synthetic-store/dispatch controls; whole-tool conformance incomplete. Astra full review and LSR-R1 correction check completed: source/document accuracy and calling/document fitness PASS for the reviewed candidate; source-scoped purpose OK. |
| Material caveat | TC-013: base description promised stronger user-store readiness than listing establishes. Candidate corrects the declaration; integration, deployed behavior and TC-013 release closure remain separate. |

[Call guide](#call-guide) · [Availability](#availability-authority-and-supplied-instructions) · [Intended contract](#normative-intended-contract) · [Inputs](#current-input-contract-and-transport) · [Outputs](#current-outputs-refusals-and-caller-recovery) · [Implementation](#current-white-box-implementation) · [Lifecycle](#current-state-custody-and-lifecycle) · [Audit](#formal-source-audit) · [Verification](#verification-and-adversarial-controls)

## Call guide

1. Use the name offered in the current palette. The web caller supplies authenticated user identity and an auth-scoped resolver; these are not tool arguments. Establish any required plugin/operator-profile authoring prerequisites independently: a metadata secret hit neither supplies a profile nor authorizes bypassing profile-based authoring with raw credential wiring.
2. Send `{}` under `none`. Under `openai_strict`, send exactly `{"_elspeth_no_arguments":true}`; its decoder removes the marker before semantic admission.
3. Read the complete returned list, including unavailable entries. There is no filter, pagination, or per-tool result cap.
4. Select the intended reference and call `validate_secret_ref` for its current resolvability. A user-store `available:true` proves metadata readiness, not successful decryption. Do not invent missing names or substitute values, environment interpolation or a `secret://` URI.
5. If authoring requires wiring, use `wire_secret_ref` and retain destination authorization, placement, execution acknowledgement and later resolution gates. Discovery does not grant those permissions. Missing service context needs corrected server context; repeating the same call does not supply it.

A successful list may accompany an invalid graph. It neither mutates the draft nor validates the user's whole workflow.

## Availability, authority and supplied instructions

| Surface / caller role | Current availability and authority | Definition and limits |
|---|---|---|
| Ordinary web Composer, primary model | Registered `SECRET_DISCOVERY`; ordinary local loop builder includes it in both dialects. Authenticated web context is server-owned. | `provider_gateway.composer_loop_tool_definitions`; actual local definitions retained in the binding JSON. Missing secret context is a refusal, not an empty list. |
| Pipeline planner, primary planner model | `capability_skill.PLANNER_DISCOVERY_TOOL_NAMES` includes it; `planner_tool_definitions` selects the policy's order-preserving subset. | Default-policy local builder retained. A policy subset or terminal-only overtime palette may omit it. Registration is not proof of actual request delivery. |
| Advisor | Named ordinary hint/checkpoint boundary supplies no tools: `advisor_request.build_advisor_request_options` adds model/response options, and `advisor_checkpoint` passes messages while capturing/auditing `tools=None`. | `SOURCE_REVIEWED_ONLY` absence on this named route; final remote delivery and every possible Advisor overlay unverified. Composer registration does not grant Advisor this tool. |
| Local `composer_mcp` | Explicitly excluded by `_COMPOSER_TOOL_NAMES`, which includes only core discovery/mutation maps. | No CLI secret-service authority follows from web registration. |
| REST secret inventory / frontend | Separate UI/API workflows may read the same stores. | They do not invoke this tool or inherit its envelope/error contract. Live API/UI execution not performed. |

Exact current candidate declaration (base text retained under TC-013 below):

> List secret-reference metadata (API keys, credentials), never values. Each entry carries `name`, `scope`, `source_kind`, `available`, and `reason` (null when available). User-store availability means a stored row exists and fingerprinting is configured; listing does not check decryption. Server availability checks the allowlisted environment value and fingerprinting configuration. Call validate_secret_ref for the selected reference's current resolvability before relying on it.

The strict builder appends exactly:

> This tool takes no semantic arguments. Call it with {"_elspeth_no_arguments": true} exactly.

The capability core's exact paragraph under the heading ancestry retained in [binding JSON](list_secret_refs.binding.json) says:

> Model identifiers come from the supplied model catalog where the request provides one, and otherwise only from `list_models`. Read the complete `list_secret_refs` result before describing credential state, validate the selected reference, and use the discovered schema's secret-reference object; never invent an identifier or substitute a raw value, `secret://` URI, or environment interpolation.

`pipeline_composer.md` also lists `- **Secrets:** list_secret_refs, validate_secret_ref, wire_secret_ref` using backtick formatting around each name. `composition_completion._compose_preflight_repair_message` supplies further conditional credential-diagnosis guidance (its complete producer module is bound): for `fabricated_secret` or `missing_secret_ref`, call both list and validate before asserting availability, report the returned categorical reason, never inline/interpolate a credential, and only wire after validation returns true. `prompts.build_system_prompt` assembles the capability core, Composer interaction skill and optional deployment skill; `render_with_pipeline_capabilities` prepends the exact core text. Core text/hash loading is cached atomically and disk drift is checked. Deployment overlays are optional and bounded at 65,536 bytes; **effective overlay bytes and final provider request instructions were not collected**. The exact core paragraph is not a claim that all dynamic instructions were rendered or tested.

The exact conditional diagnostic passage is:

```text
Credential-secret diagnostic requirement:
- When diagnosing credential availability for the user's request, call list_secret_refs and validate_secret_ref before asserting availability for the intended secret name (for example OPENROUTER_API_KEY when the user asked for OpenRouter).
- If a secret is unavailable, report the returned reason (fingerprint_resolver_not_configured, env_var_not_set, or value_decryption_failed) and the layer it identifies. Do not answer by repeating the runtime preflight complaint.
- Do not inline a literal credential, use ${VAR} interpolation, or keep placeholders. Only wire {secret_ref: NAME} after validate_secret_ref reports available=true.
```

## NORMATIVE intended contract

These clauses preserve shipped privacy, scoping and workflow authority. The historical live-resolution wording is retained as a characterized disagreement; the candidate explicitly corrects its promise rather than changing store behavior.

| Clause | Intended requirement and authority | Current assessment |
|---|---|---|
| LSR-C1-EMPTY | No semantic arguments; reject extras. Declaration's empty closed schema and direct `EmptyToolArgumentsModel` are authority. | `TESTED_PASS`, scoped to the two valid dialects and five invalid strict marker shapes in r003. Semantic null/extra/public-context ordering remains source-only. |
| LSR-C2-SCOPE | List references authorized for the server-supplied current user/auth-provider namespace; deployment-controlled server allowlist and disabled-user mode apply. Service/store contracts are authority. | `TESTED_PASS` for synthetic other-user, other-provider, disabled-user, missing-fingerprint and collision cases; not deployed auth admission. |
| LSR-C3-NO-VALUES | Never return secret values. Declaration, metadata-only user-store contract and selected response contract are authority. | `TESTED_PASS` for synthetic real-store serialized egress and existing no-decrypt/no-material-column and closed-shape controls. This is not general DLP assurance for arbitrary metadata strings. |
| LSR-C4-READONLY | Successful discovery preserves draft identity/version and has no affected nodes. `_discovery_result` and declared effect class are authority. | `TESTED_PASS` for executed valid synthetic calls, including invalid graph. No enclosing transaction/turn rollback claim. |
| LSR-C5-READINESS | Candidate declaration distinguishes metadata readiness from live decryption and requires targeted validation before reliance; the store contract and existing capability core support this correction. | `TESTED_PASS` for both local ordinary/default-planner dialect descriptions plus intact/corrupt/old-key paired behavior in r003. Historical base promise is retained as LSR-C5-LEGACY / `KNOWN_NONCONFORMANCE`; integration/adoption remains unverified. |
| LSR-C6-CONTEXT | Missing secret service or user must refuse while preserving saved graph findings, not fabricate empty discovery success. Dispatch/handler refusal contract is authority. | `SOURCE_REVIEWED_ONLY`; existing tests read, not included in r003. |
| LSR-C7-EGRESS | Only producer-selected, closed admitted metadata may reach selected tool response; failure data is omitted. Selected response and discovery-envelope contracts are authority. | `SOURCE_REVIEWED_ONLY` for complete route; r003 exercises successful admission/serialization and malformed-data refusal subcases at selected response admission, not earlier broken-resolver faults or authenticated turn/audit/reload. |
| LSR-C8-LIFECYCLE | No execution approval, reference lease, immutable snapshot, durable commit or cancellation rollback may be inferred from listing. Existing authoring/execution authority boundaries are authority. | `UNVERIFIED` end-to-end lifecycle. Source supports separation; races/provider/persistence not executed. |

No compatibility migration or stored-data change is proposed. Correcting the description changes the advertised interpretation, not the five-field wire shape or metadata-only store behavior.

## CURRENT input contract and transport

Semantic schema S from the current local registry:

```json
{"type":"object","properties":{},"required":[],"additionalProperties":false}
```

There are no argument defaults, filters, limits, identities, strings, coercions or units to supply. Omission of individual properties is therefore inapplicable. A root JSON `null` is not an object. Any semantic key, including the strict marker, is invalid; no additional user/session/provider/scope argument can override server context. The direct handler independently checks `EmptyToolArgumentsModel`.

Unicode normalization/length boundaries on semantic strings are inapplicable because this tool accepts none. Output strings are preserved by the selected encoder; r003's existing `inventory` legacy-byte case includes synthetic Unicode `clé`. Actual store naming rules remain a separate store-input contract; typed response admission alone does not impose those rules on every possible producer.

| Variant | Exact wire parameters W | Wrapper / strictness | Decode |
|---|---|---|---|
| `none` ordinary/default planner | S above | `{"type":"function","function":{"name":"list_secret_refs","description":<exact declaration>,"parameters":S}}`; no `strict` stamp | `{}` remains `{}` |
| `openai_strict` ordinary/default planner | `{"type":"object","properties":{"_elspeth_no_arguments":{"type":"boolean","enum":[true]}},"required":["_elspeth_no_arguments"],"additionalProperties":false}` | Same wrapper; description adds exact instruction above; `function.strict:true` | Only canonical true marker becomes `{}`. Missing/false/1/null/string/extra-key shapes raise wire-envelope `ToolArgumentError`; no truthiness coercion. |

All **seven actual local targeted builder definitions** (registry, wire/loop/default-planner × two dialects) are losslessly retained in the binding JSON. This is local synthesis evidence, not historical E02 inheritance, final LiteLLM adapter parity or provider acceptance. No new schema/checker is introduced.

## CURRENT outputs, refusals and caller recovery

### Success envelope and data

The JSON envelope contains `success`, `version`, `data`, `validation`, and `affected_nodes`. `success:true`, `version` unchanged, `affected_nodes:[]`; `validation` describes the saved draft and may be invalid. Its members are `is_valid`, `errors`, `warnings`, `suggestions`, `semantic_contracts`, and `graph_repair_suggestions`. Entry and repair-detail grammars are owned by [ValidationEntry and ValidationSummary](../../../src/elspeth/web/composer/state.py#L1539) and [ToolResult.to_dict](../../../src/elspeth/web/composer/tools/_common.py#L1079); this tool does not invent a validation payload. Mutation-only `validation_delta`, `applied_component`, `plugin_schemas` and `validation_guidance` are not populated by normal listing; neither runtime preflight nor post-call plugin hints are added. Successful empty inventory has `data:[]`; missing context has no data key. There is no planner-specific compact list projection: `_serialize_provider_discovery_result` falls through to `serialize_admitted_discovery_result` for this tool.

| Data member | Type / presence | Meaning |
|---|---|---|
| `data` | Complete JSON list, including empty; all five fields on every admitted item | Service-sorted metadata inventory; no pagination/truncation marker |
| `name` | Exact string | Store/allowlist identity; no tool-level renaming or Unicode normalization |
| `scope` | `user`, `server`, or `org` | Closed response vocabulary. Current WebSecretService has user/server stores; literal `org` is not proof of an org store. |
| `source_kind` | Exact string, open vocabulary | Current user/server producers emit `user_store` / `env`; empty strings or synthetic `stored` are admitted if other fields cohere. |
| `available` | Exact bool | User row exists plus fingerprint configuration; server allowlist env value is nonempty plus fingerprint configuration. Point-in-time observation, not a lease. |
| `reason` | Always present; null iff available | Otherwise one of `fingerprint_resolver_not_configured`, `env_var_not_set`, `value_decryption_failed`. User inventory itself does not detect per-row decrypt failure. |

No value, masked value, fingerprint, salt or ciphertext field is admitted. Strict reconstruction checks owned field types, scope/reason literals and the availability/reason biconditional, and freezes an owned tuple. A matching arbitrary object/subclass is not automatically an owned inventory item.

### Ordered refusals and faults

| Stage / trigger | Observable result | State/effect and recovery |
|---|---|---|
| Strict marker decode fails | `ToolArgumentError`, wire-envelope category | No semantic dispatch. Send canonical marker, not null/false or semantic extras. |
| Public credential admission, catalog/snapshot mismatch | Credential gate refusal/exception or `ValueError("plugin_snapshot_catalog_mismatch")` | Shared gate precedes handler; do not reinterpret as empty inventory. Exact credential detector/routing is owned by shared integration audit. |
| Public argument schema invalid | Failed ToolResult by default, or typed `ToolArgumentError` when `raise_schema_argument_errors=True`; read-only planner seam always raises | Shape admission precedes missing-context guard. Remove invalid keys/root; no service lookup. Direct handler also rejects extras first. |
| Missing service, user, or both | `success:false`, rejection `secret_context_unavailable`, exact message `Secret tools require secret service context.` | Same saved graph/version, no affected nodes/data, standing findings retained. Discovery does not gain mutation-only inline guidance. Correct server/auth context. |
| `list_refs` / store / SQL infrastructure fault | No tool-local exception-to-empty fallback | Public dispatch does not catch it here. Enclosing audited tool/request worker may report failure. A later retry can observe changed metadata; no durable exactly-once promise. |
| Invalid internal resolver return before selected admission: noniterable inventory or item without required attributes | Handler payload construction can raise `TypeError` or `AttributeError` before a ToolResult exists | `_inventory_item_payload` reads attributes directly while the handler iterates `list_refs` output. No tool-local conversion or successful-empty fallback; the enclosing fault/lifecycle limits above apply. These are broken producer returns, not supported user arguments. |
| Malformed metadata reaching selected response admission or wrong admitted response shape | `FrameworkBugError`, generic `Malformed secret inventory response` or selected-contract integrity failure | Fail closed before selected data serialization; no private value interpolation in this generic selected metadata-admission error. Fix the producer, not arguments. |
| Provider, prompt guard, audit or commit fails later | Enclosing operation failure, not a local listing refusal | Earlier read may already have completed; no delivery/persistence/rollback implication. Shared owner retains exact outcome mapping. |

For example, a broken resolver returning `[{}]` fails at `item.name` before selected metadata admission. This is a source-derived, unexecuted counterexample; it establishes neither the outer error presentation nor a privacy leak. Normal stores construct owned `SecretInventoryItem` records. The executed selected-contract rejection tests exercise the later admission boundary and do not cover this earlier invalid-producer path.

### Bounds

No local per-tool response/list length, pagination or response byte cap exists. Generic bounded JSON applies before semantic dispatch (raw 1,048,576 UTF-8 bytes, depth 64 and traversal budgets); all nonempty semantic arguments still fail S. Later whole-request guard/provider limits and bounded persistence redaction are independent. Redaction's 65,536-byte projection output ceiling does not cap the original metadata list or establish lossless history. No deployment cardinality/size measurement was taken.

### Examples

**Executed synthetic local dispatch, `none` / strict after decode:** empty allowlist and no owned user rows:

```json
{}
```

Result data is exactly `[]`, success true, version 7 and affected nodes empty in r003; validation is false because the synthetic draft is empty. Under strict W the call is instead `{"_elspeth_no_arguments":true}`.

**Executed synthetic same-name collision:** corrupt user ciphertext plus ready server env under the same name, fingerprint configured:

```json
[{"name":"SYNTHETIC_LIST_SHARED","scope":"user","available":true,"source_kind":"user_store","reason":null}]
```

This is the complete **data field**, not a full result. Targeted validation of that name returns the same metadata with `available:false`, `reason:"value_decryption_failed"`. User scope still shadows server scope. The correct action is targeted validation/operator repair, never server fallback invented by the model or bulk decryption.

**Invalid strict call, executed:** `{"_elspeth_no_arguments":1}` raises `ToolArgumentError`; `1` is not canonical `true`. **Missing context, source-derived/unexecuted in r003:** `{}` gets the context refusal above, not successful empty data.

## CURRENT white-box implementation

| Stage and exact owner | Trust/authority and ordered operation | Result / boundary |
|---|---|---|
| `tools._registry` / declaration | Immutable registration yields SECRET_DISCOVERY mapping and selected response contract. | Not cacheable; effects do not mutate graph. |
| `provider_gateway.composer_loop_tool_definitions`, `pipeline_planner.planner_tool_definitions`, `tools.wire_projection` | Local palette selection and dialect framing. | Actual definitions retained; policy state/final delivery remains separate. |
| `tools._dispatch.execute_tool` | Credential material guard; catalog snapshot identity; registered handler lookup; current catalog validation; optional public schema admission; required secret context. | Context constructed from trusted caller fields, not arguments. Shared paths read only. |
| `execute_discovery_tool_with_context` | Frozen request context; discovery-only registry; required schema; context guard; finalization. | Cannot reach mutation map; no implicit widening from ordinary registry. |
| `secrets._handle_list_secret_refs` | Empty-model validation; missing-context guard; exactly one `context.secret_service.list_refs(context.user_id)` call. | `_inventory_item_payload` copies only five metadata fields; no `resolve`/`has_ref`/decryption in handler. |
| `ScopedSecretResolver.list_refs` | Deployment auth provider is bound in adapter; user ID is from trusted context. | Calls WebSecretService with both namespace dimensions. |
| `WebSecretService.list_refs` | User names if enabled, server allowlist metadata; dictionary merge user wins; sort by name. | No session-ID resource namespace; caller namespace is user/auth-provider plus server deployment. |
| `UserSecretStore.list_secrets` | SQL selects `name` only, filters user and auth provider, orders by name. | No ciphertext/salt selection or per-row PBKDF/decrypt. Fingerprint configuration determines available/reason. |
| `ServerSecretStore.list_secrets` | Curated validated allowlist; reserved ELSPETH names disallowed; fingerprint gap first, then nonempty env. | Does not enumerate arbitrary environment names; values used for presence only, not returned. |
| `_discovery_result` / `finalize_tool_result` | Same state, state validation then current catalog normalization. | Success need not mean validation valid; no version increment. |
| `_SECRET_INVENTORY_RESPONSE`, `admit_discovery_result` | Exact owned fields/types/literals/invariant reconstructed and frozen; success requires data, failure forbids data. | Encoder creates fresh five-field wire mappings; original mutable producer data is not traversed on selected egress. |
| Ordinary `tool_batch` / planner serializer | Worker executes; selected contract admitted; ordinary selected JSON becomes tool message. Planner uses same non-special serialization. | Local tests cover dispatch through selected serializer, not actual worker/provider consumption. |

Source locators: [handler and declaration](../../../src/elspeth/web/composer/tools/secrets.py#L196), [public dispatch](../../../src/elspeth/web/composer/tools/_dispatch.py#L908), [read-only seam](../../../src/elspeth/web/composer/tools/_dispatch.py#L999), [merge](../../../src/elspeth/web/secrets/service.py#L169), [user listing](../../../src/elspeth/web/secrets/user_store.py#L346), [server listing](../../../src/elspeth/web/secrets/server_store.py#L119), [selected egress](../../../src/elspeth/web/composer/discovery_response.py#L51). Lines are locators; exact qualified AST signatures/bodies, algorithm and Git blob identities are in the binding JSON.

### Representation and trust transitions

Provider W is untrusted arguments; decoder restores S without granting context authority. Trusted server context supplies caller and resolver. Store metadata is reconstructed as owned immutable response records; it remains descriptive **data**, not instructions or execution approval. The selected serializer emits fresh wire values. Metadata names and `source_kind` are strings, not arbitrary trusted prompt instructions; finite value-field closure is not a proof against malicious names or a general secret detector. No tutorial-specific branch or server-authored pipeline structure is introduced by this read.

## CURRENT state, custody and lifecycle

### Dependencies and lifetime

Declarations and wire projections are process imports. Core guidance is cached atomically; deployment overlay/policy are independent inputs. Secret references are not among cacheable discovery tools. Planner information identity is `secret.index`; recording that identity does not cache a resolution result or grant permission. Store reads are fresh per call; user/server reads are sequential, not one cross-store snapshot. A mutation between reads can change a later inventory or validation. Server-only mode removes user listings and shadowing. `has_ref` is stronger for user rows because it checks decryption; it remains point-in-time.

There is no graph mutation, secret write, run creation, approval, billing call or provider invocation inside the list handler. Normal enclosing composition still invokes a model and can create audit/history records. No physical-effect absence for the enclosing request is claimed from unchanged graph identity.

### Failure, cancellation and retry

The synchronous handler has no local timeout/retry/cancellation API. Ordinary tool work runs through `async_workers.run_sync_in_worker`; queued ordinary work may be cancelled, while a running thread cannot be interrupted and retains admission until completion. A caller timeout/disconnect can therefore coexist with a completed secret metadata read whose result is discarded. Reads have no graph rollback requirement, but sibling/earlier calls, provider completions and audit obligations belong to the enclosing lifecycle. A successful retry reads fresh metadata and is not proof the first attempt did nothing. Cancellation/race paths are source-only and were not exercised. No new controller or broad lifecycle suite was run.

### Audit, persistence, reload and UI

| Representation | What source establishes | Evidence limit |
|---|---|---|
| Selected result and ordinary tool message | `tool_batch` admits selected discovery before serialization, appends tool outcome then tool message; default planner serializer shares response. | Executed local dispatch/selected serialization; no remote provider delivery. |
| Redaction policy | `redaction.MANIFEST["list_secret_refs"]` uses declarative metadata policy, no known arguments, unknown argument keys redacted. Result fields pass bounded closed projection/summarization rather than assuming every string safe. | No lossless stored inventory guarantee; exact stored/reloaded metadata not exercised. |
| Audit invocation/outcome | Tool batch routes response through ordinary outcome/audit accumulation. | Audit receipt is not database commit witness; no new persistence/ledger implementation audit. Shared integration owner retains transaction/failure details. |
| Reloaded history / UI | History may use redacted representation; UI description says credential references, names only/never values. | Live reload, tool-card parity, keyboard/UI behavior unverified. Description is not proof that only `name` exists in tool data. |

## Formal source audit

### TC-013 — supported disagreement, executed characterization

Exact base declaration before correction:

> List available secret references (API keys, credentials). Each entry carries the reference `name`, its `scope`, `source_kind`, `available` (true when it resolves for you), and `reason` (why not, when it does not); never values.

**Promise:** the current declaration says `available` is true when a reference resolves for the caller. **Current:** metadata-only user listing requires a stored row and fingerprint configuration, so it can return true when ciphertext is corrupt or encrypted under an old master key. The synthetic paired controls exercise actual user/server stores, scoped service, `execute_tool`, selected admission and serializer. Intact row validates true; corrupt/old-key rows validate false with `value_decryption_failed`. All three list true. A ready same-name server secret remains shadowed.

**Consequence:** a caller can overstate credential readiness if it trusts the list flag alone. **Counterevidence:** shipped capability guidance already requires targeted validation. Live `has_ref`, wiring, execution acknowledgement and later resolution remain stronger boundaries; this audit establishes neither value disclosure nor an execution bypass. The no-bulk-decrypt/no-material-column design is explicit and independently controlled.

**Disposition:** retain existing identity TC-013 (historical register candidate); current evidence supports the narrow description defect, not a new finding ID or automatic register status change. The authorized dirty declaration-only correction explains metadata readiness and directs `validate_secret_ref`. No handler/store/guidance behavior changed. Do not repair by decrypting all rows, weakening guards, altering stores or silently redefining the old promise as conformant.

The overlapping .10 source line was compared using immutable Git reads: `.10` commit `51bb8131723baa822c1ce65ce1a981722d6c90f6` changes only `_WireSecretRefArgumentsModel` documentation and the wire tool's `target_id` description in this module relative to the audit base. The candidate `_LIST_SECRET_REFS_DECLARATION` description hunk is disjoint. The .10 module was not copied or replayed. Its newer `pipeline_composer.md` is a different blob, including generated teaching and narrative changes; this unit did not overwrite it or claim its old-base guidance bindings describe .10. Integration must preserve both owners' changes and rebind the effective combined guidance/interface.

### UNVERIFIED questions and decisions

| Dimension | Established | Missing evidence / decision |
|---|---|---|
| Effective exposure | Seven targeted local definitions and policy-filtering source | Actual configured role palettes, overtime variants, SDK/adapters and provider receipt |
| Interface correction | Narrow authorized dirty patch and affected r003 rerun | Independent review and integration; combined .10 guidance/interface rebinding |
| Persistence/privacy | Selected closure and bounded redaction source | Authenticated turn/audit commit/reload, malicious metadata strings, oversized inventory and sink behavior |
| Lifecycle | Sync reads and shared worker cancellation semantics | Cancel/race/late completion observations and enclosing commit witness |
| Product readiness | Listing and targeted validation distinction measured | Actual secret/deployment health, live acceptance, execution authorization — no real secrets queried |

Existing .10 findings remain their assigned owners/#327. This unit neither reimplements their fixes nor counts them as new tool defects.

## Verification and adversarial controls

r003 used ordinary serial pytest with original log, exit and JUnit retained under `.claude/lanes/tool011-active-20261021/list_secret_refs/`. Native exit is **0**; JUnit reports **39 tests, 0 failures, 0 errors, 0 skipped**, duration **8.156 seconds**. Exact before/after tracked source/test/config/lint/lock inputs and dirty declaration/test files match byte-for-byte; both package imports resolve inside the assigned worktree. This is scoped evidence, not aggregate whole-tool PASS.

| Claim / challenge | Active assertions and cases | Current evidence / remaining gap |
|---|---|---|
| LSR-C1 dialect framing | `test_dialect_decode_enters_real_dispatch[none-raw0]`, `[openai_strict-raw1]`; five `test_strict_empty_marker_rejects_noncanonical_calls` cases | Both canonical calls decode to `{}` and dispatch to empty list; missing/false/1/null/extra marker calls raise. Semantic root null/extra not newly run. |
| LSR-C2 scope | `test_scoping_and_unavailability_boundaries[other-user|other-provider|disabled-user|missing-fingerprint|empty-server]` | Server fallback only when user namespace absent/disabled; fingerprint gap wins; intact owned row shadows empty server env. Synthetic SQLite only. |
| LSR-C3/C4 selected privacy/read-only | `_call` asserts every executed valid result: same graph object/bytes/version7, affected empty, invalid graph preserved, value canaries absent from serialized envelope | Value-free output and draft preservation in executed fixtures. No persistence/cancel guarantee. |
| LSR-C5 / TC-013 | `test_metadata_readiness_is_not_live_resolution[intact|corrupt|old-key]` asserts exact list and targeted result records | Meaningful positive intact row versus two negative decryption states, with ready server counterevidence. Tests characterize discrepancy; they do not make old description pass. |
| No bulk decrypt / material reads | Existing `test_list_secrets_does_not_decrypt_each_row`, `test_list_secrets_selects_no_secret_material_columns` | Trap rejects decrypt attempts; SQL names-only assertion checks selected columns. Guard policies remain intact. |
| Selected response boundary | Existing `test_secret_response_contracts.py` active cases; exact assertions bound in conformance JSON | Reject wrong scalars, extra/missing fields, incoherent reason/availability and impostors; own immutable fresh copy, preserve legacy bytes. Includes companion validate shapes only as shared selected-boundary counterevidence. |
| Candidate description | `test_offered_description_distinguishes_metadata_from_resolution[none|openai_strict]` and exact declaration pin | Both ordinary/default-planner builders carry the corrected description; n001 restored only the old production wording and both cases failed, exit1/JUnit2 failures. Exact r003 source bytes were then restored. |
| Context and schema order | Existing `test_secret_rejection_contracts` assertions read | `SOURCE_REVIEWED_ONLY`; r003 did not execute them. |
| Fault/cancel/persistence/provider | Source path above | `UNVERIFIED`, not replaced by successful unit counts. |

Original r001 is retained: 13 passed and two failed due to the new test using `DecodedArguments.arguments` instead of its actual `semantic` field. It is not a successful coverage receipt. Corrected r002 passed36 against the old description and remains historical. Current r003 supersedes it after the declaration and tests changed. Negative-control n001 has its own frozen inputs, original log/exit/JUnit and is evidence of test sensitivity, never product PASS. The normal runner, no custom controller, was used; no full Python/PostgreSQL suite, provider, live store or deployment action ran.

Focused ruff check/format and mypy on the production module/new tests pass. Static-gate run g001 completed exit0/252pass but overlaps the intentional n001 description mutation and is **not frozen-tree evidence**. The frozen follow-up g002 ran only the three live attribute-contract, masquerade-inventory and mock-discipline assertions: native exit0, JUnit3 passed/0failure/error/skip,103.314s, before/after exact source/test/config inputs equal. These static checks cover their own inventories, not product runtime conformance. Full independent review and the same-reviewer LSR-R1 consistency check completed; source/document accuracy and calling/document fitness PASS, source-scoped purpose OK. Integration remains outstanding. The correction changed no production, interface, guidance or test bytes and does not convert the unexecuted early-producer fault into tested coverage.

## Version maintenance and evidence limits

[Binding JSON](list_secret_refs.binding.json) records exact current Git/AST identity, descriptions/schemas and guidance paragraphs using the reviewed `tool_contracts.capture_source` / `symbol_facts` mechanism. [Conformance JSON](list_secret_refs.conformance.json) records exact assertion-to-active-case links, original receipt hashes, command, fixture/config/import and dirty inputs. These compact records do not adopt a new canonical package or introduce another checker/schema. Markdown remains this candidate's edit source.

Relevant drift in handler, stores, scoping, declaration, dialect decode, palette policy, selected response, prompt assembly/guidance, redaction, worker or test/fixture/config invalidates affected evidence. Shared modules are conservatively whole-file bound; this can over-invalidate. AST fingerprints prove identity within their declared algorithm, never semantic equivalence or behavioral soundness. The dirty description candidate is rebound to exact current bytes and r003; retain r002 as old-interface history. Subsequent integration needs affected rerun/review; source re-review alone cannot restore tested claims under changed bindings.

Historical 67ad14c register/report and E02 copies supplied leads, not current source or whole-tool certification. No Gold status, adoption, merge, signature clearance, release qualification or deployed acceptance is claimed.
