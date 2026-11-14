# Tracker 11.19: planner recovery received-evidence classification

Disposition: **genuine, bounded private audit-evidence classification defect; recommend a next-wave fix.** The intentional separation of terminal recovery controls from graph-mutation dispatch does not justify reporting their received tool category as `unknown_tool`. The evidence still retains the original provider name, ID, and eligible arguments; this is not evidence of lost restoration authority, a failed restore, or a public disclosure.

This is planning-only static triage of `/workspace/elspeth/.claude/worktrees/composer-remediation-landscape-metadata`, whose HEAD was read twice as `dc67bdd2f94bcedd6f59d29957791c5bccd58c06`. Read AGENTS.md and CONTRIBUTING.md, the central park report/review, and the relevant production contracts, callers, persistence projector, credential filter, and existing tests. No source/test edits, test/provider runs, scans, installs, commits, or agents were performed. Only this requested central lane note was written. The current-wave source gate, fix review, freeze, and integration decision remain parent-owned and unaffected.

## Existing measurements versus this inspection

The central `park-identity-report.md`, “Adjacent observation and scope limits,” reports the existing planner category and says the new identity test selects by retained provider facts. Its measured corrected provider matrix is reported as 42 successful cases across seven spellings, three routes, and two dialects. `park-identity-review.md`, “Quality and evidence assessment,” independently accepts the provider-facts selection for that existing category and explicitly does not claim a categorization fix. Its separate refusal-oracle finding concerns public refusal comparisons, not this category.

Those are prior lane measurements at the park lane's stated candidate/base, not new measurements of this checkout. This triage did not rerun them or establish a new route inventory dynamically. The scope and causal conclusion below follow from static contracts and call sites at the inspected HEAD.

## Why this is a bug rather than a category boundary

1. `pipeline_planner.py:1680–1695` describes and constructs a palette of discovery, **recovery controls**, and proposal. `capability_skill.py:217–232` makes both recovery names part of the required terminal suffix of the capability manifest. `_PLANNER_DECLARED_TOOL_NAMES` and `_planner_tool_name` (`pipeline_planner.py:1241–1260`) likewise recognize recovery names as declared. Recovery is separate from mutation decoding/dispatch, but is an expressly declared provider tool contract.
2. The planner's receipt capture (`pipeline_planner.py:3917–3922`) supplies only the registry wire names plus `emit_pipeline_proposal`. Recovery definitions are outside that registry. Consequently `capture_received_response` (`private_diagnostic_facts.py:201–273`) maps a safely captured `handoff_to_user` or `restore_parked_graph` name to `unknown_tool`, while keeping the original name in `provider_tool_name`.
3. The same out-of-registry argument would apply to the proposal terminal, yet the caller expressly recognizes that terminal. The known-name vocabulary is therefore already broader than graph-mutation dispatch.
4. On the primary gateway route, recovery controls also bypass ordinary dispatch, but their private receipt uses the sent palette's names (`provider_gateway.py:919–953`), so it categorizes those controls by their real names. The existing real-session refusal test at `tests/unit/web/sessions/test_graph_recovery_turn.py:101–143` explicitly selects `restore_parked_graph` evidence on freeform and malformed-head routes.
5. `PrivateReceivedDiagnostic` is a bounded received slot **independent of admission authority** (`contracts/composer_audit.py:143–159`). Its `tool_name` is passed unchanged into a persisted `ComposerDiagnosticEvidence` with `status="received_response"` (`audit_storage.py:170–185`). It is not a dispatch eligibility field. The result's disposition/refusal code already carries the relevant stage classification.

The practical defect is misleading route-dependent private evidence and incomplete category-based querying: a consumer selecting received restore evidence by `tool_name` misses planner controls unless it decodes the separate provider facts. Static inspection establishes this mismatch; it does not establish an affected external consumer or quantify its operational incidence.

## Affected routes and stages

| Path or stage | Existing behavior and expected next-wave category |
| --- | --- |
| Main planner calls, including discovery continuation, proposal repair, and prose retry | All use the shared `call_model_body` receipt site. A captured recovery-name slot is currently `unknown_tool`; it should retain the recovery name. Applies to both `none` and `openai_strict`. |
| Planner escape-hatch model | `pipeline_planner.py:4294–4300` supplies a hatch-dialect recovery palette through the same body. Same defect, independently of primary/hatch dialect or model configuration. |
| Planner with legacy recorder/quota wrapper or owned provider custody | Both `call_model` branches call the same body (`pipeline_planner.py:4140–4183`), so classification does not depend on the custody mode. Persistence/custody authority remains unchanged. |
| Successful planner handoff or restore request | Generic response admission records the receipt, then the recovery branch validates it and raises `PlannerHandoff` (`4379–4392`). The later application prepares recovery audit drafts and the owning route settles them (`planning_application.py:1197–1215`). Actual restoration success or `target_unavailable` happens after receipt capture and cannot change the captured category. |
| Planner returned response rejected before generic tool admission | Capture precedes metadata construction, cost/token budgets, truncation, and tool parsing. Safely captured recovery names therefore also miscategorize slots on metadata failure, credential refusal, missing cost, missing/over-budget completion tokens, cumulative cost refusal, length stop, excess calls, duplicate/malformed IDs, or malformed/non-object JSON. Adding the names must preserve every existing refusal code and payload-unavailability outcome. |
| Planner rejected after generic admission | Mixed recovery/discovery/proposal batches, multiple recovery calls, or strict recovery-model failures (invalid action/type, missing/extra fields, blank/oversized values) reach `4379–4389` after `record_response_call(call, None)` at `4132`. Their existing receipt disposition can be `admitted_receipt` with no refusal code even though the recovery request then fails. That denotes this earlier admission stage; changing stage semantics is a separate concern and is not needed for 11.19. |
| Invalid/missing/overlong/credential-bearing provider name | Do not promote arbitrary names. `_safe_text` returns absent/filtered bounded facts; genuinely unknown names remain `unknown_tool`. A name-bearing malformed slot can be recognized without recognizing its ID, JSON, or request as valid. |
| No readable tool slot or receipt overflow | Empty/unavailable envelopes yield the existing response summary; only the first 32 slots are captured, with overflow summarized. No recovery classification can be asserted for an uncaptured name. |
| Failure before any returned response | Provider transport errors, timeouts, cancellation or pre-dispatch refusals do not pass this receipt site with a returned response. No new received slot should be fabricated. |
| Primary/freeform, corrupt-head `compose_recovery`, and recovery-capable reply stage | These use `ProviderGateway._call_llm_with_audit_body` and already classify captured recovery batches against sent wire names. They are comparison/control routes, not affected by the planner known-name omission. |

Primary-route capture has its own stage boundary: `recovery_batch` is assigned only after `_call_llm` returns an owned, credential-checked completion (`provider_gateway.py:820–825`). A malformed control that fails provider envelope/tool-batch admission or the earlier credential guard can therefore fail without this recovery receipt. A batch that reaches `recovery_batch` is captured before downstream sole-call/strict recovery validation in service code. Do not broaden primary raw capture or assert new malformed-primary parity as part of this one-line planner classification fix; that would require a separate admission/evidence design.

## Minimal coherent fix

Extend only the known-name set supplied by the planner capture site with its already imported `PLANNER_RECOVERY_TOOL_NAMES`, e.g.:

```python
known_tools=(
    frozenset(_WIRE_TOOL_DEFS[effective_dialect])
    | frozenset(PLANNER_RECOVERY_TOOL_NAMES)
    | {_TERMINAL_TOOL_NAME}
),
```

This preserves the existing distinction between “known to the system” and “advertised on this particular call.” In particular, do not replace the current set with just `active_tools`: the planner currently recognizes registered tools even when discovery policy does not advertise them. Changing that behavior would introduce an unrelated classification change. Reusing the existing planner declaration vocabulary also avoids adding new literals or moving controls into the mutation registry.

No change is required to `capture_received_response`, the public audit schema, the evidence schema, wire tool definitions, dispatch, receipt binding, recovery parsing, settlement, or stored rows. Historical `unknown_tool` evidence remains valid and readable through its original provider facts; no migration or rewrite is proposed.

## Behavioral test oracles for the implementation round

Use production planner/gateway/persistence paths and the existing scripted-provider helpers. These are proposed oracles, not tests run by this triage.

- **Main planner recognition:** extend `test_planner_graph_recovery.py` with a recorder for both control names and both dialects (handoff actions may reuse existing coverage). Assert exactly one returned provider round, the unchanged intent/outcome, and exactly one matching `received_tool_arguments` receipt whose `tool_name` equals the actual recovery name. Select the receipt by original provider facts, then assert the category; selecting only by desired category can hide the wrong/missing slot.
- **Hatch recognition:** extend the existing hatch handoff case, including a distinct primary/hatch dialect configuration. Assert that the hatch physical call's recovery receipt has the concrete name, correct ordinal, provider ID, and physical-call binding; the preceding invalid proposal's evidence stays unchanged.
- **Pre-admission malformed/rejected control:** use at least a malformed JSON control plus a valid-JSON control rejected for duplicate IDs or an authoritative length stop, through the shared planner body. Assert the concrete recovery name despite refusal, the unchanged exception/code/no dispatch, and `received_not_admitted` with the existing refusal code. Invalid JSON must keep canonical arguments unavailable and persisted wire unavailable; never turn the name fix into argument admission.
- **Post-admission control rejection:** pin a mixed recovery/discovery batch and a valid JSON object rejected by the strict recovery model. Assert no discovery/mutation/candidate effects, unchanged failure, concrete recovery category, and the existing generic receipt-stage disposition. Do not demand `received_not_admitted` where current code has already admitted the generic tool batch.
- **One persisted planner restore:** add a direct category assertion to the current actual-provider park alias success test or a narrow equivalent. Assert category plus original alias/message, exact eligible wire, provider ID, physical-call and operation/epoch binding, and unchanged public exclusion. This catches wiring errors beyond a capture-helper unit test. Existing freeform/malformed-head tests serve as unchanged route controls; a new full identity spelling cross-product is unnecessary for this classification change.
- **Unknown-name and privacy controls:** retain an arbitrary unknown name as `unknown_tool`, with bounded provider facts retained/filtered as before. Preserve proposal/discovery classification, and preserve a known but unadvertised registry tool's existing category. Use a recovery control rejected for credential material to verify no credential-shaped value or its derived hash reaches persisted arguments/result/wire/public output. Existing `test_private_received_diagnostics.py` supplies payload bounds, invalid JSON, hostile-object, namespace, public serialization, and replay controls to reuse rather than duplicate.
- **Negative control:** removing just the recovery-name union must fail the main and hatch receipt-category assertions while the real recovery outcomes remain unchanged. Known/unknown and public/private controls must remain meaningful; do not weaken the tests to accept `unknown_tool` as an alternative.

For a later implementation, choose focused planner/recovery/private-evidence tests and affected contract gates according to the final patch, then follow the repository's required frozen-source/keyless-delta and review workflow. This note grants no test result, scan clearance, source-gate decision, or review approval.

## Invariants to preserve

- Provider receipt is observational only: recognizing a name does not authorize recovery, bypass the provider, validate arguments, dispatch a mutation, or grant execution readiness.
- Capture the original bounded arguments before owned `GraphRecoveryIntent` normalization. Canonical UUID selection must not overwrite the raw park alias or provider-authored message in private evidence.
- Keep `_safe_text` name/ID bounds and credential filtering, inert snapshot reads, the 32-slot and aggregate byte limits, strict duplicate-key/bounded JSON decoding, canonicalization, and evidence namespace unchanged.
- Keep credential filtering at persistence. `build_private_tool_evidence` applies the same general filtering to recovery names and `unknown_tool`; `_project` has no recovery-specific exception. Eligible original wire is retained only when it is decodable, unfiltered, and semantically matches canonical arguments. Malformed, filtered, or mismatching wire stays unavailable, with no credential-derived digest.
- Keep `bind_received_response` physical-call identity and refusal semantics, session/operation/epoch authority, atomic persistence, evidence integrity, private-role exclusion, public envelopes and repr exclusion unchanged. Do not infer recovery success from `admitted_receipt`.

Recommended tracker disposition: retain 11.19 as a next-wave, narrowly scoped audit classification fix with the behavioral oracles above; do not attach it to the current park-identity production change or treat the park report's successful restoration measurements as a failing recovery run.
