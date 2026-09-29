# Contract, prompt and pricing remediation planning

> **Historical diagnostic input, retained 2026-09-30.** The
> [parent plan's current execution context](../2026-09-23-web-review-remediation.md#current-execution-context)
> governs implementation. The paths, line references, proposed fixes and test
> selections below describe the 2026-09-23 investigation. Guided/deferred paths
> have since been removed and Composer ownership has moved. Reconcile each
> contract requirement against the current freeform pipeline and preserved
> worktree changes before writing code. Do not rebuild obsolete marker machinery
> or erase a downstream contract merely to reproduce an old probe result.

Read-only assessment of R01, R17, R18, R27–R34 and R39–R47. No production edits or tests run. Issue text and verification verdicts read; cited live source inspected. Paths below are relative to the repository root; the frontend is `src/elspeth/web/frontend`, not a top-level `frontend` directory.

## Current evidence and ownership

Measured HEAD: `1e9cafa9d47df5654926509ef13a2a3e9e4452c8` (`git rev-parse HEAD`). `git log --oneline 74c0ce0db..HEAD` and `git diff --stat 74c0ce0db..HEAD` show later polling, control-history and advisor-copy fixes. Direct inspection confirms the assigned defect sites remain. No already-fixed candidate in this subset. The only post-pin change in `sessions/routes/composer/compose.py` changes conversation history at lines 184–191; the R40 progress block remains generic at lines 430–437. Current line references for `_effective_producer_vote_uncached` shifted approximately five lines; use functions and exact expressions, not original line numbers.

Suggested batches (single editor per batch; independent reviewer after implementation):

1. **Contract probes:** R01, R17, R18. Own `_validation_probe.py`, probe regressions and demand regressions. R01 is first/high priority. Same owner prevents incompatible stub and marker fixes.
2. **Azure policy:** R44–R47. Own `plugin_policy/profiles.py`, `plugin_policy/validation.py`, `contracts/azure_ai_search.py` and policy tests. Can develop alongside batch 1; coordinate contract constant/docstring and integration tests.
3. **Prompt integrity:** R32–R34. Own mutation/reconciliation code and review tests. Must coordinate `interpretation_state.py` with any R06 or other review-state lane. Register guidance in one small handoff to batch 4 if it owns generation.py.
4. **Planner teaching/catalog:** R27–R31. Own state.py guidance, planner aid docs/budget, feedback projection and generation guidance. Can develop alongside batch 3 only with explicit nonoverlapping hunks/serialization for `state.py`, `pipeline_planner.py`, `tools/generation.py`.
5. **Pricing failure semantics:** R39–R42. Own planner usage admission, retry classification, shared progress mapping and config bounds. Coordinate `sessionStore.ts`, route helpers and `pipeline_planner.py` with other lanes; land failure taxonomy before client permanent-code handling.
6. **Tutorial failure affordance:** R43. Could be a frontend subtask of batch 5, with api/client.ts ownership assigned once. This is tutorial UI repair, not a tutorial backend authoring shortcut. Guided retirement does not automatically retire tutorial.

The cross-batch review should specifically attempt provider bypass, Tier-1 corruption healing, public-policy leakage and malformed-usage misattribution. No reviewer should approve their own implementation.

## Per-issue changes and proofs

### R01 — profile-only Azure Stage 1 probe

Source: `src/elspeth/web/composer/_validation_probe.py`, `src/elspeth/contracts/azure_ai_search.py`; inspect `src/elspeth/plugins/transforms/azure/ai_search.py` and `src/elspeth/plugins/transforms/rag/core.py` to establish construction and guarantees are binding-independent. Current probe branch is only `plugin == "llm"` at line 66.

Add Azure-specific detached probe binding only when profile is present and all private binding fields are absent. Remove profile in the detached options and use an inert Azure-valid endpoint plus managed identity (or inert credential). Never invoke on_start, resolve a profile/secret, make network calls, persist the endpoint or replace authored pipeline structure. A profile-plus-private-options draft must continue failing; do not broadly sanitize invalid inputs.

Tests: extend `tests/unit/web/composer/test_llm_producer_guarantees.py` or add adjacent `test_azure_search_producer_guarantees.py`; extend `tests/unit/web/composer/test_source_demand.py` and `tests/unit/web/test_interpretation_state_source_data_contract.py`. Use the real registry/plugin constructors. Positive: CSV question → profile search policy prefix → consumer requiring question and policy__rag_context validates and source demand retains question. Compare authored and real lowered contracts for every generated rag field, custom output_prefix, explicit schemas, supported search modes. Negative: omitted upstream question still rejects; invalid schema/index/query config still rejects; profile+endpoint still rejects; original options byte-for-byte unchanged; forbidden provider/network-start spy remains uncalled. Existing `tests/unit/plugins/transforms/azure/test_ai_search.py` and policy tests remain regression gates.

### R17 — multi-query marker misclassification

Source: `_validation_probe.py`, `state.py:2092–2129`; admitted sites are in `src/elspeth/web/execution/_validation_materialization.py`. Solve jointly with R18: replace valid inline markers at exact admitted LLM prompt sites in detached probe options. This avoids treating Pydantic union branch noise as a general license to suppress real config errors. Do not loosen `all(errors)` globally without a separate proof.

Tests: `tests/unit/web/composer/test_deferred_blob_probe.py`, `test_llm_producer_guarantees.py`. Cover dict query templates, admitted list representations only where supported, system_prompt, node prompt_template; mixed valid marker + invalid option remains a failure; invalid sha/blob marker remains a failure; markers in non-prompt fields never gain the exception. If list-form nested markers are not admitted by the actual blob writer, record that boundary and test it as rejected rather than widening authoring scope.

### R18 — prompt marker destroys guarantees

Source same as R17. Establish that contract outputs are independent of prompt text; use an inert string only in the detached constructor probe. Do not claim this validates template syntax or referenced inputs; authoritative materialization remains mandatory before execution. Preserve schema/output_fields/response_field/query configuration intact and retain zero-guarantee deferral for content-dependent plugins such as reference_join.

Tests: positive literal-vs-marker contract parity with downstream required answer/output fields and source demand; real materialized prompt must still be used at execution validation; provider receives original uploaded bytes, never a probe string; missing input requirements and malformed templates are not silently admitted by full preflight. Extend `test_deferred_blob_probe.py` controls for reference_join and unrelated malformed config. A focused materialization regression belongs in `tests/unit/web/execution/test_validation_interpretation_review_drift.py` or the existing materialization fixture location selected during implementation.

### R27 — coalesce repair teaches impossible policy

Source: `src/elspeth/web/composer/state.py:3360–3373`. Teach best_effort with timeout_seconds and first; remove quorum as an offered Composer choice or explicitly state it is unavailable. Preserve the warning that redirected failed rows never reach coalesce and require_all therefore fails. No server policy selection.

Tests: `tests/unit/web/composer/test_coalesce_guarantee_parity.py` and `test_edge_route_reconciliation.py`; exercise actual on_error rejection and verify suggested supported policy examples validate with the required timeout. Reuse existing rejection test; no new prose-only testing framework.

### R28 — catalog budget vs scaffold ratchet

Source: `src/elspeth/web/composer/planner_authoring_aids.py:1175–1184`, `tests/unit/web/composer/test_pipeline_planner.py` scaffold baseline, `docs/agents/recent-code-hints.md`. This is a budget/policy decision, not a correctness failure. The review's first measurement is contradicted by its own verification; do not repeat its 503-slug/17-turn numbers.

Implementation must first measure the live installed registry through the real model-catalog function, canonical bytes with/without cap, omitted providers, and complete initial scaffold request. Pin dependency versions and record raw outputs. Preserve complete-provider deferral and restore useful provider coverage within the existing authority where possible. `test_pipeline_planner.py:160–168` grants a pre-approved 10% growth band above the baseline; a measured repair within that band needs no new operator permission. If the necessary repair exceeds the band, changes the baseline, or crosses the documented shrink boundary, present concrete measured options for the required ruling. Do not weaken the test or trim load-bearing teaching. Keep only genuinely outstanding decisions pending. Never route around provider calls to recover latency.

Tests: `tests/unit/web/composer/test_planner_authoring_aids.py`, `test_pipeline_planner.py`; below budget, exactly-over budget, partial omission, all omission, discoverability whenever any provider is omitted, complete carried lists. No new test solely for docs. Shared file conflicts with R29–31/R41.

### R29 — stale catalog docstrings

Source: `planner_authoring_aids.py:1973–1983`, `pipeline_planner.py:450–460`. Describe complete carried provider lists, per-provider omission, and discovery for any omission. Keep correct condition at line 465. Land with R28's policy decision, or independently correct current behavior now. Direct doc review plus existing aid tests; no new test for wording.

### R30 — planner feedback omits promised query names

Source: `src/elspeth/web/composer/pipeline_planner.py:2549–2563`, `state.py:1966–1976`, `tools/generation.py` prompt-role entries. Prefer including llm_user_prompt_missing in the safe planner detail allowlist behind `not withhold_candidate_facts`; maintain the custody argument that labels came from the rejected planner-authored candidate. Alternatively simplify static text, but explicit query names are better actionable repair.

Tests: `tests/unit/web/composer/test_pipeline_planner.py` feedback tests and `test_validation_error_codes.py`. Two missing queries among four names exactly those two in normal feedback; withheld feedback does not reveal names or candidate text; system-prompt error unchanged. Provider still re-emits the proposal.

### R31 — unadvertised patch tool in repair guidance

Source: `state.py` prompt-role FIX constants and `tools/generation.py` records. Surface-neutral instruction: edit that option and re-emit proposal on planner surface; patch_node_options only when advertised in the tool-loop roster. Never synthesize a system prompt server-side.

Tests: same guidance/planner tests; simulated malformed candidate receives actionable repair that works with declared discovery + emit roster; repaired candidate is provider-authored and retains supplied user prompt verbatim. Reviewer checks both initial rootless and ordinary amendment transitions for provider-call preservation.

### R32 — echoed parts swallow raw prompt edits

Source: `src/elspeth/web/composer/tools/transforms.py:1658–1677`, `src/elspeth/web/interpretation_state.py:3251–3261`. After proper merge-patch semantics, compare any explicit raw prompt with the authoritative render of merged structured parts; reject mismatch before returning success or committing a candidate. Reuse rendering/reconciliation authority instead of building another renderer. Pay attention to removal (`parts: null`), pending interpretation refs and newly changed parts. Do not reject valid parts-only edits because the previous compiled template is stale.

Tests: `tests/unit/web/composer/test_prompt_patch_reviews.py`, `tests/unit/web/test_interpretation_state_review_debt_and_drift.py`. Echo unchanged parts + new raw prompt rejects without changing original state or approval; matching rendered echo succeeds; changed parts update compiled prompt and reopen only affected review; unrelated no-op retains proof; pending refs preserve unresolved semantics; explicit parts removal follows existing policy.

### R33 — corrupt digest healed by alternate mutation door

Source: `tools/transforms.py:1743–1759`, `interpretation_state.py:_reconcile_node_options`. Recommended policy: enforce existing refusal at shared reconciliation boundary against the previous authoritative node before deleting/restamping inherited digest, rather than weakening the patch guard. Tier-1 corruption should not be repaired silently. Validate old evidence before considering the proposed data; otherwise replacement can launder the corrupt value. Explicitly decide behavior for plugin replacement/removal and ensure refusal remains consistent with authorized explicit deletion semantics.

Tests: parameterized actual tools in `test_prompt_patch_reviews.py`, plus `tests/unit/web/test_interpretation_state_review_debt_and_drift.py`: patch/upsert/splice/set_pipeline all refuse preserved corrupt node; valid old proof + legitimate new prompt causes normal invalidation, not false corruption; no-op retains proof; unrelated node mutation cannot silently heal a corrupt sibling. Execution materializer continues deriving/validating digest. This changes shared reconciliation behavior, so central full Python suite is warranted at integration.

### R34 — missing repair-code catalogue entry

Source: `src/elspeth/web/composer/tools/generation.py`, code emitter in `tools/transforms.py`. Add DirectValidationGuidance for prompt_template_parts_required with accurate, surface-aware repair that preserves interpretation_ref entries and explains obtaining parts through the available state inspection tool. Keep R32's final semantics consistent.

Tests: `tests/unit/web/composer/test_validation_error_codes.py` actual explain lookup and dispatch envelope have nonempty actionable guidance; no repair loop to an unknown code. Do not sweep the four other uncatalogued codes without separate scope approval.

### R39 — retry button on permanent pricing configuration fault

Source: `src/elspeth/web/frontend/src/components/chat/MessageBubble.tsx:284`, `src/elspeth/web/frontend/src/stores/sessionStore.ts` retry guard; `src/elspeth/web/sessions/protocol.py`. Share a typed non-retryable classification consumed by both frontend gates, including cost_unavailable. A tiny helper under frontend/src/lib is preferable to duplicated lists. Correct protocol description. Land after R41 separates malformed usage so transient responses are not accidentally made permanent.

Tests: `components/chat/MessageBubble.test.tsx`, `stores/sessionStore.test.ts`. cost_unavailable shows admin action text with no Retry; direct store retry dispatch makes zero HTTP/provider calls; policy_blocked/admission_refused unchanged; provider_unavailable/malformed response retains retry; fresh user message after corrected config remains possible. Coordinate shared sessionStore ownership with other frontend work.

### R40 — recompose progress drift

Source: `src/elspeth/web/sessions/routes/composer/compose.py:422–447`, `routes/messages.py`, `routes/_helpers.py`. Use one shared mapping of failure reason/evidence/next action in both send and recompose, retaining route-specific headline if useful. Correct the mapping comment to include deployment setup faults. Avoid duplicating another conditional that can drift.

Tests: extend `tests/unit/web/sessions/test_routes.py` or add focused `tests/unit/web/sessions/routes/test_planner_failure_progress.py`. Parameterize both endpoints with COST_UNAVAILABLE, provider and malformed response exceptions; capture emitted progress and HTTP failure_code, verify parity and correct durable disposition without duplicate LLM audit records. No PostgreSQL needed solely for wording, but broader shared persistence lane still owns its required database gate.

### R41 — invalid usage misreported as pricing setup

Source: `src/elspeth/web/composer/pipeline_planner.py:4041–4081`, `src/elspeth/core/llm_pricing.py:414–450`, `src/elspeth/web/composer/llm_response_parsing.py` (path verified). Admit required usage and optional subtotal invariants before cost-unavailable classification, using existing typed boundary parsing. Missing cost with valid usage and missing catalog remains COST_UNAVAILABLE. Invalid usage becomes MALFORMED_RESPONSE, audited with that status, before content parse/tool dispatch. Do not simply swap completion_tokens and cost checks while leaving missing prompt tokens or impossible optional counters misclassified.

Tests: `tests/unit/web/composer/test_pipeline_planner.py::test_missing_completion_token_metadata_is_audited_then_rejected`, `tests/unit/core/test_llm_pricing_profile.py`, `tests/unit/web/composer/test_cost_pricing_details.py`. Matrix: absent usage, missing prompt/completion, bool/negative counters, cached>prompt, reasoning>completion, malformed reported cost, unknown price with valid usage, direct valid provider cost. Exactly one call record, correct status/code, no proposal/content/tool use after failure; retry classification distinguishes provider anomaly from operator setup. Do not mutate pricing/audit DTO shapes unless necessary; if doing so, census contracts/gates and migration impact first.

### R42 — bool max_tokens coercion

Source: `src/elspeth/core/llm_profiles.py:130`, `src/elspeth/plugins/sources/llm/config.py:81,322`; mirror transform/query `mode='before'` rejection rather than broad strictness unless supported forms are deliberately changed. Ensure subclass field overrides inherit the guard.

Tests: `tests/unit/core/test_llm_profile_layering.py`, `test_llm_profile_temperature.py` (or new neighboring numeric-config test), `tests/unit/plugins/sources/llm/test_config.py`. Test true and false rejected through direct profile, web environment JSON, YAML profile, every source provider including gateway override; positive integer and None stay valid; allowed numeric strings unchanged if established. Lowered profile never turns a bool into 1. Profile/source schema and contract gate implications must be checked; shared config changes need full Python integration gate.

### R43 — inaccessible tutorial run details

Source: `src/elspeth/web/frontend/src/api/client.ts`, `components/tutorial/TutorialTurn4Run.tsx`, optionally API error type definition; backend `src/elspeth/web/composer/tutorial_service.py` already supplies run_id/counts. Prefer decode known error payload and provide a real navigation affordance using existing run details route; if navigation cannot represent tutorial session context, accurate copy is the bounded alternative, explicitly reviewed as such. Do not change tutorial backend authoring path.

Tests: `src/elspeth/web/frontend/src/api/client.tutorial.test.ts`, `components/tutorial/TutorialTurn4Run.runButton.test.tsx`, `.discard.test.tsx`, `.cancel.test.tsx`; run failure with zero successes exposes correct run ID navigation and relevant row counts; malformed IDs are rejected, generic errors remain generic, cancellation behavior unchanged. Existing `tests/unit/web/composer/test_tutorial_service.py` verifies returned error fields. Coordinate client.ts edits with error-handling lane.

### R44 — false shared-private-set comment

Source: `src/elspeth/contracts/azure_ai_search.py`. State actual consumers accurately, incorporating new validation-probe import from R01 if landed. Existing `tests/unit/plugins/transforms/azure/test_ai_search.py` pins configured fields. No runtime change or new test solely for a comment. Do not revive the refuted privacy finding associated with this source.

### R45 — dropped Azure operational teaching

Source: `src/elspeth/web/plugin_policy/profiles.py:1653–1663`, raw hints `src/elspeth/plugins/transforms/azure/ai_search.py:181–187`. Preserve binding-neutral plugin hints in public assistance alongside profile teaching. Do not perform fragile string-keyword censorship of arbitrary new hints; current hints are known public content and a projection regression should protect the policy boundary. Check public_schema teaching too so surfaces stay consistent. Avoid duplicating an existing identical RAG pairing hint.

Tests: `tests/unit/web/plugin_policy/test_profiles.py`; real assistance projection retains portal chunk/chunk_id advice and search-mode/vectorizer/top_k guidance; private credential/binding option values do not appear; disabled/unavailable profile behavior unchanged. Existing golden schema only regenerated if actual emitted schema changes, with reviewed diff.

### R46 — Azure rejection uses LLM terminology

Source: `src/elspeth/web/plugin_policy/validation.py:490–500,528–539`. Add service-specific Azure binding wording in reachable public-schema unexpected-option arm and maintain consistent lowering fallback; keep values redacted and option names explicit. Avoid pretending Azure is storage merely to reuse wording.

Tests: `tests/unit/web/plugin_policy/test_validation.py`: authored endpoint on an Azure profiled node yields service binding instruction, never offending endpoint value or LLM model/pacing instruction; genuine LLM and S3/Textract wording unchanged. Direct fallback test only if meaningful, since schema rejects before lowering normally.

### R47 — impossible example index

Source: `src/elspeth/web/plugin_policy/profiles.py:1602–1621`. Derive example index from the chosen example alias's closed pin, including the first chosen alias when several profiles exist; open pin retains safe placeholder. Preserve privacy policy: only already-public index pins may appear. Do not broaden allowed indexes to make the example work.

Tests: `tests/unit/web/plugin_policy/test_profiles.py`: one closed pin, multiple profiles where chosen alias is closed, open pin, no available alias; validate produced example through real public schema and lowering admission when profile exists. This is a real false example fix; refuted sibling claim about missing index discovery remains refuted.

## Commands and verification sequence

All are planned, not executed. Use explicit task worktree cwd, worktree-owned environment or primary interpreter with both worktree source roots. Each invocation writes a unique lane-private log, waits for process exit and records exit code; never claim from a piped/tail-only summary. Workers are `-n 0` for every lane. No simultaneous broad suite.

- Probe batch: `.venv/bin/python -m pytest -n 0 tests/unit/web/composer/test_deferred_blob_probe.py tests/unit/web/composer/test_llm_producer_guarantees.py tests/unit/web/composer/test_source_demand.py tests/unit/web/test_interpretation_state_source_data_contract.py tests/unit/plugins/transforms/azure/test_ai_search.py` plus any new Azure test file.
- Azure policy: `.venv/bin/python -m pytest -n 0 tests/unit/web/plugin_policy/test_profiles.py tests/unit/web/plugin_policy/test_validation.py tests/unit/plugins/transforms/azure/test_ai_search.py`.
- Prompt: `.venv/bin/python -m pytest -n 0 tests/unit/web/composer/test_prompt_patch_reviews.py tests/unit/web/test_interpretation_state_review_debt_and_drift.py tests/unit/web/composer/test_validation_error_codes.py`.
- Planner teaching/usage: `.venv/bin/python -m pytest -n 0 tests/unit/web/composer/test_planner_authoring_aids.py tests/unit/web/composer/test_pipeline_planner.py tests/unit/web/composer/test_validation_error_codes.py tests/unit/web/composer/test_coalesce_guarantee_parity.py tests/unit/web/composer/test_edge_route_reconciliation.py`. Split/co-ordinate this large file selection rather than each lane repeating it.
- Config/pricing: `.venv/bin/python -m pytest -n 0 tests/unit/core/test_llm_pricing_profile.py tests/unit/core/test_llm_profile_layering.py tests/unit/plugins/sources/llm/test_config.py tests/unit/web/composer/test_cost_pricing_details.py` plus focused new route tests.
- Frontend cwd `src/elspeth/web/frontend`: `npm test -- src/components/chat/MessageBubble.test.tsx src/stores/sessionStore.test.ts src/api/client.tutorial.test.ts src/components/tutorial/TutorialTurn4Run.runButton.test.tsx src/components/tutorial/TutorialTurn4Run.discard.test.tsx src/components/tutorial/TutorialTurn4Run.cancel.test.tsx`; then `npm run typecheck`, scoped eslint (or `npm run lint` at integration). Confirm supported serial Vitest flags from installed runner before selecting concurrency.
- Whole-tree gates: reread CONTRIBUTING.md before writing; touched composer source brings attribute-contract/masquerade/contract-scan obligations even if focused tests pass. Planned known paths include `tests/unit/web/test_sessions_composer_attribute_contracts.py`, `tests/unit/scripts/test_check_contracts.py`, `tests/unit/elspeth_lints/test_masquerade_gate.py`. Coordinator identifies every scanned-input affected gate from current CONTRIBUTING instead of assuming these three exhaust them.
- Final integrated shared-runtime/contract change requires a single frozen-tree full-suite gate, serial PostgreSQL selection for the overall package's schema/persistence work, and trust-tier corpus comparison key-free. Provider transitions must still be made in rootless/tutorial scenarios. A passing scoped probe test alone cannot count as live Azure RAG acceptance; retain separately listed fake-provider integration and optional operator-authorized live service smoke.
