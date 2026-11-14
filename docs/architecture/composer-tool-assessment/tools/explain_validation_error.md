# `explain_validation_error` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Translate a validation message or stable error code into an explanation and repair guidance. The result is advice to the planner. It is not a validation verdict and it does not repair the graph. The tool is cacheable discovery on the freeform loop. Every route that advertises it advertises it on every turn: the planner's discovery list is the static `PLANNER_DISCOVERY_TOOL_NAMES` tuple, and no policy narrows it by phase ([src/elspeth/web/composer/capability_skill.py:16](../../../../src/elspeth/web/composer/capability_skill.py#L16), [src/elspeth/web/composer/pipeline_planner.py:359](../../../../src/elspeth/web/composer/pipeline_planner.py#L359), [src/elspeth/web/composer/pipeline_planner.py:393](../../../../src/elspeth/web/composer/pipeline_planner.py#L393)). Both repair surfaces also point the model at the tool in their rejection feedback (§3.1, §5.1).

The lookup universe is the closed guidance catalogue: 168 codes, indexed from 128 legacy regex records plus direct records. The catalogue is a strict subset of the 263 registered emitted codes in [src/elspeth/web/composer/error_codes.py:283](../../../../src/elspeth/web/composer/error_codes.py#L283). The other 95 registered codes have no guidance at all (measured, §7).

## 2. Contracts and authority

Requires `error_text: string`, with a closed root (`additionalProperties: false`). No `minLength` applies, so `""` and whitespace are admitted (§4). The handler always re-admits its arguments through `ExplainValidationErrorArgumentsModel` (`extra="forbid"`). That happens inside the handler, so it covers every route, including direct internal calls that skip the opt-in JSON-schema gate. A failure raises `ToolArgumentError(MODEL_VALIDATION)` ([src/elspeth/web/composer/tools/generation.py:290](../../../../src/elspeth/web/composer/tools/generation.py#L290), [src/elspeth/web/composer/tools/generation.py:2176](../../../../src/elspeth/web/composer/tools/generation.py#L2176), [src/elspeth/web/composer/tools/_common.py:1234](../../../../src/elspeth/web/composer/tools/_common.py#L1234)).

Output is admitted by `ExplanationResponse`, which is strict, `extra="forbid"`, and preserves wire order. It requires `error_text`, `explanation` and `suggested_fix`. `error_code` is optional, and an explicit null is a `FrameworkBugError`. The contract does not check that `error_code` is a closed code ([src/elspeth/web/composer/tools/_generation_responses.py:108](../../../../src/elspeth/web/composer/tools/_generation_responses.py#L108), [src/elspeth/web/composer/tools/_generation_responses.py:345](../../../../src/elspeth/web/composer/tools/_generation_responses.py#L345)).

The handler validates the current graph independently for the envelope; that state is distinct from the text being explained ([src/elspeth/web/composer/tools/generation.py:2178](../../../../src/elspeth/web/composer/tools/generation.py#L2178)). Error text is untrusted. Its persisted argument projection keeps the value only when it is exactly a catalogued code, and otherwise records a length-only sentinel. The response `data` (which echoes `error_text`) is left undeclared, so it fails closed in persistence ([src/elspeth/web/composer/redaction.py:2696](../../../../src/elspeth/web/composer/redaction.py#L2696), [src/elspeth/web/composer/redaction.py:3796](../../../../src/elspeth/web/composer/redaction.py#L3796)).

## 3. Source trace to the user

`_execute_explain_validation_error` ([src/elspeth/web/composer/tools/generation.py:2171](../../../../src/elspeth/web/composer/tools/generation.py#L2171)) tries five branches in order:

1. A case-sensitive exact match on the runtime reason `union_field_collision` ([2182](../../../../src/elspeth/web/composer/tools/generation.py#L2182)).
2. An exact catalogue code via `explain_validation_code` ([2194–2203](../../../../src/elspeth/web/composer/tools/generation.py#L2194)).
3. The first matching legacy regex in catalogue order, which is case-sensitive ([2204–2216](../../../../src/elspeth/web/composer/tools/generation.py#L2204)).
4. The first catalogue code found case-insensitively as a substring, in catalogue order rather than text position ([2222–2239](../../../../src/elspeth/web/composer/tools/generation.py#L2222); `error_code` is set at [2236](../../../../src/elspeth/web/composer/tools/generation.py#L2236)).
5. An unknown-text result whose `suggested_fix` inlines all 168 codes ([2247–2261](../../../../src/elspeth/web/composer/tools/generation.py#L2247)).

Only branch 4 emits `error_code`. Branches 3–5 append the validator `Expected …` span ([src/elspeth/web/composer/tools/generation.py:1987](../../../../src/elspeth/web/composer/tools/generation.py#L1987), [src/elspeth/web/composer/tools/generation.py:2015](../../../../src/elspeth/web/composer/tools/generation.py#L2015)). That span is cut at a sentence boundary, not at a length: with no `.`+whitespace boundary it runs to the end of the caller's text. `EXPLAIN_VALIDATION_ERROR_RESPONSE_CONTRACT` preserves the result shape.

Freeform exposure runs `get_tool_definitions` → wire projection → `composer_loop_tool_definitions`. Planner exposure is `planner_tool_definitions` over the same wire entries ([src/elspeth/web/composer/tools/_dispatch.py:311](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311), [src/elspeth/web/composer/provider_gateway.py:391](../../../../src/elspeth/web/composer/provider_gateway.py#L391), [src/elspeth/web/composer/pipeline_planner.py:1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647)). Public freeform calls go through these stages before `execute_tool`, in order: the batch credential scan, wire decoding, the dispatcher credential check, closed JSON-schema admission and, finally, the handler's own argument model ([src/elspeth/web/composer/tool_batch.py:1015](../../../../src/elspeth/web/composer/tool_batch.py#L1015), [src/elspeth/web/composer/tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399), [src/elspeth/web/composer/tools/_dispatch.py:908](../../../../src/elspeth/web/composer/tools/_dispatch.py#L908), [src/elspeth/web/composer/tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)).

After the handler, `finalize_tool_result` normalizes validation, and discovery result admission selects the declared response contract before serialization ([src/elspeth/web/composer/tools/_dispatch.py:752](../../../../src/elspeth/web/composer/tools/_dispatch.py#L752), [src/elspeth/web/composer/tool_batch.py:3052](../../../../src/elspeth/web/composer/tool_batch.py#L3052)). The loop records the invocation, returns a tool result to the model and persists its redacted tool-turn cohort. The later operation settlement owns user-facing success or failure ([src/elspeth/web/composer/tool_batch.py:3344](../../../../src/elspeth/web/composer/tool_batch.py#L3344), [src/elspeth/web/composer/service.py:1515](../../../../src/elspeth/web/composer/service.py#L1515), [src/elspeth/web/sessions/composer_turn.py:1051](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)). ToolCallCard renders the recorded outcome and tooltip; the tool has no action button of its own ([src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:90](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L90)). A completed lookup does not imply pipeline readiness.

### 3.1 Route inventory

| Route | Entry / gate | Result consumer | Notes |
|---|---|---|---|
| Canonical registry | `_EXPLAIN_VALIDATION_ERROR_DECLARATION` (`ToolKind.DISCOVERY`, `cacheable=True`) registered in the discovery tuple ([generation.py:2264](../../../../src/elspeth/web/composer/tools/generation.py#L2264), [generation.py:4703](../../../../src/elspeth/web/composer/tools/generation.py#L4703)) | `execute_tool` / `execute_discovery_tool_with_context` | Same handler on all routes. |
| Freeform loop | Static `composer_loop_tool_definitions(dialect)`, with no phase filter; `execute_tool(..., prior_validation=_last_validation, validate_arguments=True, raise_schema_argument_errors=True)` ([tool_batch.py:3008](../../../../src/elspeth/web/composer/tool_batch.py#L3008)) | `admit_discovery_result` → model; per-`compose()` discovery cache ([service.py:2500](../../../../src/elspeth/web/composer/service.py#L2500), [tool_batch.py:1586](../../../../src/elspeth/web/composer/tool_batch.py#L1586), [tool_batch.py:3368](../../../../src/elspeth/web/composer/tool_batch.py#L3368)) | Only successful results are cached; the unknown branch counts as a success. |
| Freeform rejection pointer | A failed *mutation* envelope carries `validation_guidance.explain_tool = EXPLAIN_TOOL_GUIDANCE` when some entry has no catalogue code ([_dispatch.py:721](../../../../src/elspeth/web/composer/tools/_dispatch.py#L721), [generation.py:2062](../../../../src/elspeth/web/composer/tools/generation.py#L2062), [generation.py:2071](../../../../src/elspeth/web/composer/tools/generation.py#L2071)) | Model | This is an advertisement route, not an execution route. See explain_validation_error-N1. |
| Planner (one-shot) | Always advertised (`PLANNER_DISCOVERY_TOOL_NAMES`); `execute_discovery_tool_with_context` with `request_context.current_validation` ([pipeline_planner.py:4895](../../../../src/elspeth/web/composer/pipeline_planner.py#L4895), [_dispatch.py:999](../../../../src/elspeth/web/composer/tools/_dispatch.py#L999)) | `admit_discovery_result`; information key `validation.code:<code or raw text>` ([pipeline_planner.py:424](../../../../src/elspeth/web/composer/pipeline_planner.py#L424)) | There is no discovery cache. Repetition is governed by information-manifest coverage, not by result reuse. |
| Planner rejection pointer | Candidate feedback adds top-level `guidance = EXPLAIN_VALIDATION_ERROR_GUIDANCE` when some entry lacks inline `explanation` ([pipeline_planner.py:2322](../../../../src/elspeth/web/composer/pipeline_planner.py#L2322), [pipeline_planner.py:2561](../../../../src/elspeth/web/composer/pipeline_planner.py#L2561)) | Model | Raw `message` is withheld on this surface. See explain_validation_error-N1. |
| Inline catalogue reuse (no tool call) | `explain_validation_code` / `explain_withheld_validation_code` feed planner entries and freeform `validation_guidance.codes` ([pipeline_planner.py:2437](../../../../src/elspeth/web/composer/pipeline_planner.py#L2437), [generation.py:2023](../../../../src/elspeth/web/composer/tools/generation.py#L2023)) | Model | These read the same records the tool's exact branch reads, but not its regex/message branch (explain_validation_error-N2). |
| Recovery controls | None. `handoff_to_user` / `restore_parked_graph` neither call nor reference the tool. | — | Instrument: `grep -rn explain_validation src/` lists no recovery file. |
| Proposal / approval interception | None. A discovery tool creates no proposal and has nothing to replay. | — | — |
| Async / operation | None. Synchronous handler; operation settlement owns cancel, timeout and persistence. | — | — |
| Composer MCP | `_COMPOSER_TOOL_NAMES` includes all `_DISCOVERY_TOOLS` ([server.py:117](../../../../src/elspeth/composer_mcp/server.py#L117)); `execute_tool(..., validate_arguments=True)` with no `prior_validation` ([server.py:402](../../../../src/elspeth/composer_mcp/server.py#L402)) | `result.to_dict()` returned directly; **no `admit_discovery_result`** ([server.py:415](../../../../src/elspeth/composer_mcp/server.py#L415)) | The selected response contract is not applied on this route (explain_validation_error-N6). No cache. Trained-operator plugin snapshot. |
| REST / frontend replay | `GET /messages` stamps a server-derived outcome; ToolCallCard renders "Looked up" + `TOOL_CALL_DESCRIPTIONS` ([toolCallDescriptions.ts:41](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L41)) | User | The tooltip says "for a validation error code"; the tool also accepts messages. |
| Credential refusal | `require_no_credential_material_for_tool` at dispatch, plus the batch/wire scans | Raises `CredentialMaterialRefused` → whole-request 422, not a tool result ([composer_turn.py:1838](../../../../src/elspeth/web/sessions/composer_turn.py#L1838)) | `error_text` is the one discovery argument that carries pasted free text. |
| Tutorial | None found. `grep -n -i tutorial` over the handler span (2171–2262) and `pipeline_planner.py` returns 0. The same grep returns comment hits at generation.py:1518/2559 (known positive). | — | No composer-invariant defect. |

## 4. Branches, failures and recovery

Exact-code and noisy-message branches can produce equivalent guidance through different shapes, and in two measured cases different guidance (explain_validation_error-N2). Unknown text succeeds with a clear "not known" explanation and a 4,861-character `suggested_fix` that lists every catalogue code and tells the model to call again with "the exact error_code string". That success means the explanation operation completed. It does not mean the graph is valid. When the caller already passed an exact registered code that has no catalogue entry, the instruction is circular (explain_validation_error-N1). `""` and whitespace-only `error_text` take the same unknown branch (measured).

Regex/substring matching is advisory only. Argument rejection happens before the handler on public routes, and again inside it through the argument model. Expected policy or semantic refusal is a failed result on the current state. Controlled-code exceptions remain framework or plugin failures, and the common loop retains the executed prefix and audit evidence. A credential-shaped `error_text` is refused at request level, not as a tool failure. Cancellation, timeout and final persistence failure belong to the request/operation owner, not to a handler-local retry. The handler commits no graph changes and needs no proposal approval. Retrying a lookup can recover an argument or infrastructure problem but cannot repair the graph by itself.

**Cache, idempotency, concurrency and retry:** `ToolKind.DISCOVERY` with `cacheable=True`. On the freeform loop, successful results are cached by tool and canonical arguments within one `compose()` call ([src/elspeth/web/composer/service.py:2497](../../../../src/elspeth/web/composer/service.py#L2497)). That includes the unknown-text result. Cache hits are reattached to current state and normalized validation, so cached advice is not a cached graph verdict ([src/elspeth/web/composer/tool_batch.py:1617](../../../../src/elspeth/web/composer/tool_batch.py#L1617)).

The planner has no result cache. Its information key is `validation.code:<error_text>`, so a message and its code are distinct keys, and any successful result (including the unknown one) marks its key supplied ([src/elspeth/web/composer/pipeline_planner.py:5056](../../../../src/elspeth/web/composer/pipeline_planner.py#L5056), [src/elspeth/web/composer/pipeline_planner.py:5075](../../../../src/elspeth/web/composer/pipeline_planner.py#L5075)); the key-supplied step is source-established, not probed. The MCP route has no cache.

Repeated equal inputs are effect-free but still audited. The static guidance lookup has no write lock or internal retry. Concurrent graph edits cannot turn its advice into a current diagnostic, so the planner must supply new error evidence after an edit.

The handler computes full composition validation itself and ignores `context.current_validation`. Its result carries no `_validation_snapshot_hash`, so `normalize_tool_result_validation` validates again. One lookup therefore costs two full validations when a prior validation is supplied, and three when none is (the MCP call shape) ([src/elspeth/web/composer/tools/_dispatch.py:923](../../../../src/elspeth/web/composer/tools/_dispatch.py#L923), [src/elspeth/web/composer/tools/_common.py:3815](../../../../src/elspeth/web/composer/tools/_common.py#L3815)). Probe P1 counted these calls (§7.1): 2 with `prior_validation`, 3 without, against 1 for the `get_expression_grammar` control. This runs against the compose loop's stated intent to avoid redundant validation ([src/elspeth/web/composer/service.py:2502](../../../../src/elspeth/web/composer/service.py#L2502)); its wall-clock cost is not measured.

## 5. Defects, restrictions and hypotheses

**GS-02 — demonstrated low-impact contract discrepancy, refined (re-confirmed by probe P1, §7.1):** the published description promises `error_code` "when the text matched a closed code" ([src/elspeth/web/composer/tools/generation.py:2269](../../../../src/elspeth/web/composer/tools/generation.py#L2269)). Only the case-insensitive substring branch emits it ([src/elspeth/web/composer/tools/generation.py:2236](../../../../src/elspeth/web/composer/tools/generation.py#L2236)). The exact branch omits it ([src/elspeth/web/composer/tools/generation.py:2202](../../../../src/elspeth/web/composer/tools/generation.py#L2202)), and so does the regex branch. A lowercase code inside noise usually reaches the regex branch first, so `LOG unknown_node_type END` omits `error_code` while `LOG UNKNOWN_NODE_TYPE END` carries it. Whether a noisy call carries the field therefore depends on letter case and on whether the code has a legacy regex.

The omission on the exact branch is pinned by a dedicated assertion, `list(exact.data) == ["error_text", "explanation", "suggested_fix"]` ([tests/unit/web/composer/test_validation_guidance_catalogue.py:255](../../../../tests/unit/web/composer/test_validation_guidance_catalogue.py#L255)), as well as by the frozen-wire fixture. The explanation remains useful, and no graph corruption is alleged. Fuzzy and regex matching pick the first catalogue record, not the first code in the text, so consumers must not treat the chosen code as an authoritative diagnosis. At baseline no catalogue code is a substring of another (measured, §7).

**explain_validation_error-N1 — demonstrated guidance defect (repair pointer targets a guaranteed miss); demonstrated end to end on the planner and the freeform envelope by probes P2 and P3 (§7.1):**

- *Planner surface.* The planner attaches inline `explanation` to an entry exactly when `explain_validation_code(code)` is non-None ([src/elspeth/web/composer/pipeline_planner.py:2437](../../../../src/elspeth/web/composer/pipeline_planner.py#L2437)). It adds `guidance: "To expand any code, call explain_validation_error with the exact code string."` exactly when some entry lacks `explanation` ([src/elspeth/web/composer/pipeline_planner.py:2322](../../../../src/elspeth/web/composer/pipeline_planner.py#L2322), [src/elspeth/web/composer/pipeline_planner.py:2561](../../../../src/elspeth/web/composer/pipeline_planner.py#L2561)). So it is emitted only for uncatalogued codes. The planner withholds the raw `message`, so the code is the only input the model can pass.
- *Freeform surface.* `EXPLAIN_TOOL_GUIDANCE` appears only when an entry's code is uncatalogued or absent. It still tells the model to pass "the entry's error_code" first, and the message only "when it has none" ([src/elspeth/web/composer/tools/generation.py:2062](../../../../src/elspeth/web/composer/tools/generation.py#L2062)). The builder's own docstring says the message is what can still help ([src/elspeth/web/composer/tools/generation.py:2098](../../../../src/elspeth/web/composer/tools/generation.py#L2098)). Probe P3 refutes that claim for every case sampled. A failed freeform `upsert_node` envelope on a state holding a cycle carried `explain_tool`. For both of its uncatalogued entries (`node_id_collides_with_source_or_sink`, `pipeline_cycle`) the code form and the full-message form both returned the unknown branch. Across six distinct uncatalogued codes emitted by real validation (adding `connection_label_invalid`, `output_name_invalid`, `node_id_invalid`, `failsink_self_reference`), the message form resolved 0 of 6. Controls: the catalogued entries' messages in the same runs resolved (`plugin_options_invalid`, `connection_sink_name_overlap`, `gate_route_labels_mismatch`, `plugin_not_installed`).
- *Measurement.* All 95 registered-but-uncatalogued codes return the unknown branch when passed as exact `error_text`: 95 unknown, 0 regex, 0 fuzzy. The negative control is all 168 catalogued codes, which take the exact branch (168 of 168). The positive control, `unknown_node_type`, resolves. Every producer code is registered by the pinned census in `error_codes.py`.
- *Consequence (P2, scripted SDK).* A candidate rejected with `node_id_collides_with_source_or_sink`, `pipeline_cycle` (both bare) and `connection_sink_name_overlap` (enriched) carried the planner `guidance` pointer. The scripted obedient call for each bare code cost one provider request (3 requests, `repair_count` 1) and returned the unknown branch, with a 4,861-character `suggested_fix` (about 5.4 KB per tool result). The enriched control returned 749 bytes, identical to its inline `explanation`. Both `information_aware` settings behaved the same way. A rejection with only enriched codes carried no pointer (control). The key `validation.code:<code>` then becomes supplied; that step is source-established, not probed.
- *Scope note (P2 exploration).* A schema-violation rejection (`canonical_schema`, uncatalogued) carried no pointer, because that feedback does not pass through `_compile_planner_feedback`. The pointer defect is therefore confined to the state-validation feedback path. The docstring claim that the pointer fires "exactly when the call can add something" is the inverse of the measured behaviour.
- *Test gap.* The tests pin the inverted behaviour itself:
  - the planner pointer is asserted present beside a bare `validation_error` entry ([tests/unit/web/composer/test_pipeline_planner.py:2956](../../../../tests/unit/web/composer/test_pipeline_planner.py#L2956)) and beside a codeless entry ([tests/unit/web/composer/test_pipeline_planner.py:10462](../../../../tests/unit/web/composer/test_pipeline_planner.py#L10462)). Both project to `validation_error`, which returns unknown;
  - the parity suite pins its bytes ([tests/integration/web/composer/parity/test_repair_and_deferral.py:149](../../../../tests/integration/web/composer/parity/test_repair_and_deferral.py#L149));
  - the freeform pointer is pinned for an unknown code ([tests/unit/web/composer/test_failure_validation_guidance.py:144](../../../../tests/unit/web/composer/test_failure_validation_guidance.py#L144)).

  The scripted planner follow-up call reads a catalogued code ([tests/unit/web/composer/test_pipeline_planner.py:2297](../../../../tests/unit/web/composer/test_pipeline_planner.py#L2297)). None of the guidance, catalogue, planner or parity tests read asserts that the advertised input resolves.

**explain_validation_error-N2 — demonstrated message/code divergence:** the message `No source configured.`, emitted with code `no_source_configured` at [src/elspeth/web/composer/state.py:8473](../../../../src/elspeth/web/composer/state.py#L8473), matches the older generic pattern at [src/elspeth/web/composer/tools/generation.py:465](../../../../src/elspeth/web/composer/tools/generation.py#L465) (record index 1, 72-character fix "Use set_source…"). The code itself resolves to record 96 ([src/elspeth/web/composer/tools/generation.py:1206](../../../../src/elspeth/web/composer/tools/generation.py#L1206)), a 284-character set_pipeline/blob_id-aware fix. `No sinks configured` / `no_sinks_configured` behave the same way.

The description presents message and code as interchangeable inputs, and the freeform envelope's inline `validation_guidance.codes` uses the code record. A model that follows "Pass the entry's `message`" therefore gets different (older) advice from the advice already inline. The legacy match order is frozen by tests (`test_legacy_patterns_preserve_historical_occurrences_and_order`, `test_every_historical_code_keeps_its_first_legacy_record`), so any fix must move that freeze deliberately. The instrument only checked literal pattern alternatives, so "2" is a lower bound. Probe P1 re-ran this on the entries a validated empty state actually emits: both pairs (`no_source_configured`, `no_sinks_configured`) diverge, 2 of 2, with fixes of 72 vs 284 and 49 vs 116 characters.

A third non-interchangeability, seen in probe P3: `source_path_policy_invalid` resolves as a code, but its emitted message ("Source 'source': Source file path options (path or file) must use this session's permitted …") returns the unknown branch. P3 triggered it with a `set_pipeline` whose source path fell outside the session's permitted directory, because the session id did not match. A model that follows "Pass the entry's `message`" gets no guidance for that catalogued code.

**explain_validation_error-N3 — demonstrated redundant work (call count, probe P1):** two full composition validations run per lookup when a prior validation is supplied. Three run when none is supplied, which is the MCP call shape. P1 measured this through the dispatcher's no-`prior_validation` path; it did not execute `composer_mcp/server.py`. The handler ignores the `context.current_validation` already threaded to it (§4). The control discovery tool needs one. Wall-clock cost has not been measured. The pattern also appears in `_execute_preview_pipeline` ([src/elspeth/web/composer/tools/generation.py:4534](../../../../src/elspeth/web/composer/tools/generation.py#L4534)).

**explain_validation_error-N4 — demonstrated stale planner key branch (probe P1):** `_tool_information_keys` reads `arguments["error_code"]`, which is not a declared argument. Given one, it keys on it (`validation.code:y`, not `validation.code:x`). Strict dispatch rejects that argument set with `ToolArgumentError` (`invalid_schema`), and the `error_text`-only control succeeds. The closed root rejects it, so the branch is dead ([src/elspeth/web/composer/pipeline_planner.py:425](../../../../src/elspeth/web/composer/pipeline_planner.py#L425)). It is harmless today but misleads readers about the tool's argument surface.

**explain_validation_error-N5 — demonstrated minor inconsistencies (each item re-measured by probe P1):**

- `union_field_collision` matches only exactly. `UNION_FIELD_COLLISION` returns unknown (measured), even though the substring branch advertises "any case" matching. The reason is not in the closed catalogue, so the audit argument summarizer records it as `<redacted-validation-error-text:21-chars>` (measured), and it is absent from the unknown branch's help list.
- The comment at [src/elspeth/web/composer/tools/generation.py:2243](../../../../src/elspeth/web/composer/tools/generation.py#L2243) says 117 codes; there are 168.
- The handler's docstring sits after its first statement, so `_execute_explain_validation_error.__doc__` is `None` (measured).
- The `Expected …` hint is sentence-bounded, not length-bounded.

**explain_validation_error-N6 — source-established route gap:** Composer MCP returns `result.to_dict()` without `admit_discovery_result`, so the selected response contract is not enforced on that route ([src/elspeth/composer_mcp/server.py:415](../../../../src/elspeth/composer_mcp/server.py#L415)). For this tool the producer happens to satisfy the contract today, so no output difference is demonstrated. The gap is that the guard does not run at this consumer.

**Hypothesis/unverified:** a validator message that quotes a credential-shaped option value, pasted into `error_text` as the skills instruct, would refuse the whole request (422) rather than fail one lookup. Earlier credential guards on authored options probably prevent such a message existing, but no message has been produced that demonstrates the path.

### 5.1 Guidance parity

The tool name grep is controlled: `grep -n explain_validation_error` hits pipeline_composer.md:105, 343 and 500, and pipeline_capabilities.md:35 and 491. The concept greps (`error_code`, `suggested_fix`, `validation_guidance`, `explain_tool`) hit only pipeline_composer.md:442, 491 and 496–500. In pipeline_capabilities.md they match nothing relevant (the only hit, line 79, is unrelated).

- **Wrong argument advice (freeform):** [pipeline_composer.md:499–501](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L499) and `EXPLAIN_TOOL_GUIDANCE` say to call the tool "with that entry's `error_code`, or with its full `message` when it has none". `explain_tool` appears precisely when the code is uncatalogued, so the code form returns unknown (N1). Leading with the message would not fix this on its own: in probe P3 the message form also missed for all six uncatalogued codes sampled (N1).
- **Inverted advertisement (planner):** `EXPLAIN_VALIDATION_ERROR_GUIDANCE` ("with the exact code string") is attached only when that code is guaranteed unknown, and the message is unavailable on this surface (N1).
- **Description overclaims equivalence:** the declaration says "Pass the entry's `message`, or its `error_code`". The two can give different advice (N2), and `error_code` is promised for any closed-code match but is returned only on one branch (GS-02). The argument description ("The validation error message to explain.") omits the code form that every pointer recommends.
- **Generic encouragement without limits:** [pipeline_composer.md:105](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L105), [pipeline_capabilities.md:35](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L35), [pipeline_capabilities.md:491](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L491) and the planner authoring-aid line ([src/elspeth/web/composer/planner_authoring_aids.py:1937](../../../../src/elspeth/web/composer/planner_authoring_aids.py#L1937)) all recommend the tool for any rejection. None says that inline `explanation`/`suggested_fix` or `validation_guidance.codes` already carries the catalogue text, so calling the tool for an enriched code wastes a turn (the rationale behind elspeth-41b406c9fc).
- **Planner feedback shape untaught:** neither skill describes the planner's entry-level `explanation`/`suggested_fix` or the top-level `guidance` key. pipeline_composer.md teaches only the freeform `validation_guidance` envelope. The planner may learn the shape from the feedback itself; whether it needs teaching is unverified.
- **UI parity:** the tooltip "for a validation error code" ([toolCallDescriptions.ts:41](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L41)) omits message input. This is minor and is not a GS-03-class overclaim.
- **No contradiction between the two skills** on this tool. No stale tool or argument references were found in this tool's mentions or concept hits listed above; the files were not audited as a whole. The stale `error_code` argument exists only in planner code (N4).

## 6. Ownership, remediation and dependencies

The generation/response-contract owner should decide one canonical identity rule and align the producer, declaration, selected-contract fixtures and the pinning test at [tests/unit/web/composer/test_validation_guidance_catalogue.py:255](../../../../tests/unit/web/composer/test_validation_guidance_catalogue.py#L255). The preferred rule is to carry the exact matched code on every branch that resolves to a catalogue record, including the regex branch's record code. This is an additive output decision that still needs frozen-wire fixture review.

The catalogue owner should decide whether a message and its code may diverge (N2). If they may not, legacy prose patterns that shadow a later code record need reordering or retirement behind an explicit change to the frozen order. Both repair-pointer owners (planner feedback and the freeform envelope) should advertise the tool only when the advertised input can resolve (N1). Do not move text pattern matching into orchestrator control flow. Dependency: settle result-contract semantics before updating planner consumers or the UI.

**Implementation risk:** low for the matched-code correction and the pointer wording, medium for response-schema or frozen-wire compatibility changes and for reordering frozen legacy patterns. Keep explanation advice separate from authoritative validation.

### 6.1 Simple enhancements

1. **Suppress the planner pointer when it cannot help.** *Where:* [pipeline_planner.py:2322](../../../../src/elspeth/web/composer/pipeline_planner.py#L2322), [2561](../../../../src/elspeth/web/composer/pipeline_planner.py#L2561). *Why:* N1. On this surface an unenriched entry has only an uncatalogued code, which the tool cannot resolve. *Acceptance:* a candidate rejection that contains only uncatalogued codes carries no `guidance` key. A test asserts that every code the pointer could name returns a non-unknown result, and that test goes red when an uncatalogued code is injected.
2. **Lead the freeform pointer with the message — premise weakened by probe P3; needs a maintainer decision.** *Where:* [generation.py:2062](../../../../src/elspeth/web/composer/tools/generation.py#L2062) and the taught block at pipeline_composer.md:496–501. *Why:* N1. *Finding:* the message form resolved 0 of 6 sampled uncatalogued codes, so rewording alone still points at a miss. The choice is between catalogue coverage for the codes that reach a failed envelope and suppressing `explain_tool` as in #1, or both. *Acceptance (if kept):* the text says to pass the entry's full `message`, a test asserts the advertised input resolves for an emitted uncatalogued entry, and the skill-teaching parity tests are updated in the same change.
3. **Carry `error_code` on the exact (and regex-record) branches.** *Where:* [generation.py:2202](../../../../src/elspeth/web/composer/tools/generation.py#L2202), [2211](../../../../src/elspeth/web/composer/tools/generation.py#L2211). *Why:* GS-02. *Acceptance:* `unknown_node_type`, `LOG unknown_node_type END` and `LOG UNKNOWN_NODE_TYPE END` all return `error_code == "unknown_node_type"`, and unknown text still omits it. Update `generation_response_wire_4f206253.json` and the test_validation_guidance_catalogue.py:255 assertion together. Optionally declare `data.error_code` as a known safe response key in the redaction manifest (a closed constant), so the audit row records which code a message-form lookup resolved. That changes `redaction_policy_snapshot.json`.
4. **Reuse threaded validation.** *Where:* [generation.py:2178](../../../../src/elspeth/web/composer/tools/generation.py#L2178). *Why:* N3. *Acceptance:* the handler uses `context.current_validation` when present; a spy on `validate_composition_state` shows one fewer call per lookup, and envelope bytes are unchanged.
5. **Correct the declaration and argument text.** *Where:* [generation.py:2269](../../../../src/elspeth/web/composer/tools/generation.py#L2269), and the `error_text` description at generation.py:2278. *Why:* N2 and GS-02. *Acceptance:* the text says a message or a code is accepted, that the two may yield different records, and when `error_code` is returned. The wire-parity fixtures are regenerated.
6. **Remove the stale planner key branch.** *Where:* [pipeline_planner.py:425](../../../../src/elspeth/web/composer/pipeline_planner.py#L425). *Why:* N4. *Acceptance:* the key is derived from `error_text` alone and the existing planner information-key tests still pass.
7. **Housekeeping.** Fix the "117 entries" comment (2243). Move the handler docstring above line 2176. Make the `union_field_collision` branch case-insensitive or include it in the help list. *Acceptance:* `__doc__` is non-None, and `UNION_FIELD_COLLISION` resolves.
8. **Reject empty input honestly (optional, frozen-wire).** *Where:* the JSON schema at generation.py:2275. *Why:* `""` currently returns 4.9 KB of help. *Acceptance:* `minLength: 1` is either added (with wire fixtures updated) or explicitly declined.

## 7. Evidence, regression checks and limits

Read the exact/noisy/unknown producer fixtures and frozen-wire assertions ([tests/unit/web/composer/test_generation_response_contracts.py:43](../../../../tests/unit/web/composer/test_generation_response_contracts.py#L43), [tests/unit/web/composer/test_generation_response_contracts.py:111](../../../../tests/unit/web/composer/test_generation_response_contracts.py#L111)), plus the noisy-code retention test at [line 219](../../../../tests/unit/web/composer/test_generation_response_contracts.py#L219). The exact-branch omission is also pinned by [tests/unit/web/composer/test_validation_guidance_catalogue.py:255](../../../../tests/unit/web/composer/test_validation_guidance_catalogue.py#L255). Existing tests preserve current bytes and therefore do not refute GS-02. The selected test files passed (see [evidence.md](../evidence.md), Generation/session/async and Generation/preview selections). The controlled exact/noisy/unknown observations are recorded in [evidence.md § Generation baseline and diagnostic identity](../evidence.md): exact keys omit `error_code`, noisy uppercase keys include it, and the unknown control omits it.

The sheet-review pass added read-only dispatcher introspection on the worktree. `elspeth.__file__` resolved into the worktree. The handler-execution runs added the worktree root to `PYTHONPATH` to import the test helpers `_empty_state`, `_mock_catalog` and `execute_tool` (as evidence.md's probe does). That pass ran no tests; the probe runs in §7.1 later re-measured every line below:

```text
closed catalogue codes 168; legacy patterns 128; registered codes 263; catalogue ⊂ registered (catalogue-not-registered = [])
registered-uncatalogued 95 -> exact lookup: unknown 95, regex 0, fuzzy 0   (positive control unknown_node_type -> known)
planner server rejection codes 19 -> all uncatalogued (incl. validation_error, canonical_schema)
"No source configured" -> record 1 (fix 72 chars); "no_source_configured" -> record 96 (fix 284 chars); sinks likewise
"LOG unknown_node_type END" keys [error_text, explanation, suggested_fix]; "LOG UNKNOWN_NODE_TYPE END" adds error_code
"" / unknown text -> unknown branch, suggested_fix 4861 chars; "UNION_FIELD_COLLISION" -> unknown
catalogue substring pairs (any order) 0   (control ["abc","xabcx"] -> 1 pair)
redaction summary("union_field_collision") -> <redacted-validation-error-text:21-chars>; handler __doc__ None
```

### 7.1 Probe runs (2026-11-09)

The probes ran against the worktree at `2ac312207` (production source = baseline). Each script printed `elspeth.__file__` and asserted that it resolves into the worktree's `src/`. Scripts and logs are in the lane-private probe directory `$L` (`.claude/lanes/composer-sheet-review-20261109/probes/explain_validation_error/`). Nothing was written into the worktree; `git status --short` was empty before the sheet edit. `$PY` below is the main checkout's `.venv/bin/python`, because the worktree has no `.venv`.

**P1 — catalogue re-confirmation (assigned, evidence.md row "N1–N5"): confirmed.** Exit 0, log `$L/probe1_catalogue.log`.

`cd $WT && TMPDIR=$L/tmp PYTHONPATH=$WT:$WT/src:$WT/elspeth-lints/src $PY $L/probe1_catalogue.py`

```text
closed catalogue codes 168 legacy patterns 128 registered 263; catalogue-not-registered []
registered-uncatalogued 95 -> {'unknown': 95};  catalogued 168 -> {'exact': 168}  (negative control)
empty-state emitted no_source_configured / no_sinks_configured: message->regex (72/49 chars), code->exact (284/116 chars); 2 of 2 diverge
GS-02: unknown_node_type exact, no error_code; "LOG unknown_node_type END" regex, no error_code; "LOG UNKNOWN_NODE_TYPE END" fuzzy, error_code=unknown_node_type
"" / "   " / unknown / validation_error -> unknown, suggested_fix 4861
N5: UNION_FIELD_COLLISION -> unknown; summarizer -> <redacted-validation-error-text:21-chars> (control unknown_node_type kept); __doc__ None; "(117 entries)" comment present
catalogue substring pairs 0 (control 1)
N4: key with error_code -> validation.code:y; dispatch with error_code -> ToolArgumentError invalid_schema; error_text-only control success
N3: validate_composition_state calls: no prior 3, with prior 2, control get_expression_grammar 1
```

**P2 — planner pointer under a scripted SDK (assigned, lane report "not run"): confirmed.** Exit 0, 3 passed, log `$L/probe2_planner_n1.log`. The file is ad hoc, `$L/test_probe2_planner_n1.py`, built on the `_plan` / `_ScriptedCompletion` harness from `tests/unit/web/composer/test_pipeline_planner.py`, which patches `litellm.acompletion`; no network is used.

`cd $WT && export PYTHONPATH=$WT:$WT/src:$WT/elspeth-lints/src TMPDIR=$L/tmp && $PY -m pytest $L/test_probe2_planner_n1.py -n 0 -p no:cacheprovider -s -c $WT/pyproject.toml --rootdir $WT -o pythonpath="$WT $WT/src $WT/elspeth-lints/src"`

It observed the pointer beside the two bare codes, an unknown result for each, and the enriched control resolving. A second test is the enriched-only control, which carries no pointer. An exploratory companion (`$L/test_probe2_explore.py`, exit 0) supplied the candidate shapes and the `canonical_schema`-has-no-pointer scope note.

**P3 — freeform envelope pointer (extra, not in the proposed list): confirmed for the code form; refutes the message-form fallback for the sampled codes.** Exit 0, log `$L/probe3_freeform_pointer.log`.

`cd $WT && TMPDIR=$L/tmp PYTHONPATH=$WT:$WT/src:$WT/elspeth-lints/src $PY $L/probe3_freeform_pointer.py $L`

It uses the real plugin catalog through the strict dispatcher. Results are under N1 and N2.

N6 had no proposed probe and was not run; it stays source-established.

Confidence is high for GS-02, N1 and N2, and operational severity is low to moderate: N1 can cost a planner repair turn but does not corrupt the graph. Regression checks should compare exact, noisy-lowercase and noisy-uppercase variants of the same code, compare each catalogued code's message against its code, prove each pointer names a resolvable input, and confirm that unknown input remains honestly unknown.

The source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection, dispatcher introspection and selected local tests do not establish live provider behaviour, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

Synchronous kind: `discovery`. Declaration cache opt-in: `true`. Handler: [`elspeth.web.composer.tools.generation._execute_explain_validation_error`](../../../../src/elspeth/web/composer/tools/generation.py#L2171).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | yes |
| Composer MCP | yes |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "error_text": {
      "type": "string",
      "description": "The validation error message to explain."
    }
  },
  "required": [
    "error_text"
  ],
  "additionalProperties": false
}
```
