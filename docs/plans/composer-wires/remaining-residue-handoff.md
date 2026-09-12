# Remaining Seam 5 residue — read-only census

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.

Measured 2026-09-10 against `.claude/worktrees/composer-wires-campaign`, HEAD `1278c5c215fc8f749d53fd16d57413e416b93a00` plus concurrent uncommitted campaign work. Source reads and AST only; no provider calls, imports, tests, tracker writes, or production edits. No mutable envelope-gate import. Complete issue_get envelopes are adjacent in `remaining-residue-envelopes.json`; all five comment_list envelopes were retrieved completely (`has_more=false`). No memory matches were used.

## Per-row verdicts

| Issue | Current evidence / prior ruling | Verdict and bounded next action |
|---|---|---|
| elspeth-5e81b50f2e | `_vf_destination_note` still emits `data.note`; four caller sites in sources.py and sessions.py. Crucial change since description: state.py:8636-8660 already emits **error**, code `quarantine_unknown_output`, from the same stored state; `_mutation_result` calls `new_state.validate()` at _common.py:1592. Original F7 ruling was teach now, structurally carry later. | **Fix producer**, but do not add a duplicate warning or demote the existing build error. Remove redundant note producer/calls, retain structured error, and teach the existing code. If operator insists on warning severity, that is a severity-policy decision; ordinary fix preserves current error severity. |
| elspeth-d83095ee87 | CORRECTION: earlier 139/118 numbers counted syntactic tuple elements, including starred generator expansions; they were NOT expanded regex-row/code counts. Expanded membership/order must be measured from a dependency-complete frozen snapshot after MODEL. `explain_validation_code` still searched the regex table in the read snapshot. No comments on issue. | **Fence legacy prose lookup now**, with membership/order ratchet and direct code-keyed catalogue for new guidance; defer deletion until actual uncoded-producer census reaches zero. Existing request explicitly says freeze, not delete now. Do not infer growth or select a freeze baseline from the invalid earlier counts. |
| elspeth-c00e6d9795 | AST still finds exactly **12 subscript stores** into `payload`/`closed` in the two named functions; `data: NotRequired[object]` still admits arbitrary shapes. No comments. | **Fix producer**: construct final branch payloads once, using closed typed data arms and a helper receiving final outcome/data. Eliminate ambient closure mutation. Preserve disclosure decisions byte-for-byte. Add a separate restricted-surface gate/probes; do not pretend Composer TAUGHT gate covers planner capabilities. |
| elspeth-6aa477c78e | Comment 9285 records prior partial fix: validation.errors[].error_code extraction landed at 2c5e3c8d8. Current battery_score.py:322 confirms it. `_approval_required` still needs nested data.status and persisted mutations redact data wholesale. | **Fix consumer observability** without claiming absent approval: explicit not-observable reporting is bounded and avoids broadening disclosure. To actually score approval on captures, add a minimal closed approval-status carrier to existing audit/capture wire and join by tool_call_id, with producer-based tests. Capturing full canonical results is a larger alternative, not the default repair. |
| elspeth-9e76d9436b | Comment 9283 corrects headline: mocked harness legitimately supplies dict errors and relaxation works there; HTTP state still supplies list[str]. Wording fix landed at 456511ba5. Current schemas.py:333 and scorer.py:677-708 confirm mismatch remains. | **Fix consumer contract**, preferably with explicit ValidationSummary input from caller, preserving legacy message-only states as not observable. Alternatively consume structured HTTP errors after held upstream error-record decision. Do not rewrite working mocked-harness behavior as an impossible fixture. |

Every issue currently has `status_category=open` and no explicit blocked_by/blocks edges. Three tasks are open; two scorer bugs are triage and require severity before confirmed. c00e6d9795 has stale historical claim metadata (`assignee=unassigned`, expiry 2026-09-05, is_ready=false); use atomic tracker claim with current ownership handling, not blind status mutation.

## Exact implementation surfaces and probes

### Source destination note

Production: `src/elspeth/web/composer/tools/_common.py` (helper at 1605, mutation validation at 1592), `tools/sources.py` (1162, 1289, 1509), `tools/sessions.py` (1794), `tools/generation.py` (code guidance), `skills/pipeline_composer.md` (remove old note split, preserve housekeeping explanation). Existing structured error authority: `src/elspeth/web/composer/state.py:8636`.

Tests: `tests/unit/web/composer/test_state.py` already pins `quarantine_unknown_output` at 156 and 2955; `test_edge_route_reconciliation.py:678` also pins it. Update mutation tests for all four callers: unknown destination emits the code once in validation.errors and no data.note; discard and existing output emit neither; server_owned_metadata_note survives independently. Envelope gate currently names `_vf_destination_note` in its extraction helper map: coordinate with TAUGHT owner before removing that map row. Extend catalogue/teaching tests to resolve the code without user prose. Do not introduce a second validator.

### Legacy prose catalogue freeze

Production authority: `src/elspeth/web/composer/tools/generation.py` (`_VALIDATION_ERROR_PATTERNS:363`, `_CLOSED_VALIDATION_ERROR_CODES:1442`, `explain_validation_code:1672`, `_execute_explain_validation_error:1814` vicinity). Planner consumers depend on bare-code guidance; existing `tests/unit/web/composer/test_pipeline_planner.py:5258` imports this table to test terminal advice.

Minimal useful close: preserve legacy matching order and patterns, introduce direct code->guidance lookup for new code entries, and gate that legacy pattern set cannot grow. Pin all current bare-code resolutions against expected prior results during extraction; fuzzy code-in-noise behavior remains. A count-only cap permits replace-one/add-one drift, so gate membership against a frozen historical set as well as a cap. Report its intentional fixture role rather than calling it derived authority. Whole-tree producer completeness is separate work: errors can originate in state, validation_authoring modules, core DAG and execution validation. The historical `_validation_authoring.py` path no longer exists; enumerate actual files before any broad producer sweep.

Probes: reject new legacy row, allow removal, exercise direct new-code lookup, retain first-match order for legacy prose, ensure all previously resolved codes return guidance, and preserve terminal no-reemit semantics. Run `test_generation.py`, `test_planner_teaching_gate.py`, affected planner tests. This census did not enumerate uncoded producers, so deleting regex fallback is unsupported.

### Restricted discovery structural close

Production: `src/elspeth/web/composer/pipeline_planner.py:3059-3253`. Store locations: 3106 data; 3169 success; 3170 data; 3183 success; 3184 data; 3192 data; 3200 success; 3201 data; 3221 sources; 3234 node; 3246 output; 3250 whole projected state. Closed payload validation shape lives alongside it. Companion typed errors may reuse `tool_result_envelope.py` only if semantics truly match.

Keep state-bearing validation messages stripped, projected node/output precedence, budget failures and schema projection failures, nonrestricted canonical path, and preview fail-closed behavior unchanged. Preserve internal corruption raising (missing candidate id) rather than laundering it as projection unavailable. No new global lint rule needed; local owning-root gate should find all constructions/aliases and reject later mutation.

Tests: `tests/unit/web/composer/test_pipeline_planner.py` existing cases around 2535-2564, 2794, 2839, 3088-3205, particularly `test_every_restricted_discovery_success_uses_the_closed_provider_envelope`. Add AST mutation probes for named-local subscript store, helper aliases, and update/merge escape if covered by chosen gate. Test every success/failure arm, absent data behavior, each restricted surface and nonrestricted pass-through. Preserve existing signed metadata; report honest function fingerprint drift without signing.

### Battery scorer

Production: `evals/lib/battery_score.py` (`_approval_required:283`, `_codes_from:299`, event processing:361, approval branch:487); `evals/lib/battery_capture.py` (`Capture:59`, `ToolRow:120`, `load_capture:140`, `tool_rows:252`, audit pairing:275`). Tests: `tests/unit/evals/composer_battery/test_battery_score.py` and capture tests in same directory. Current canonical producer: `src/elspeth/web/composer/audit.py:681` finish_success keeps normalized canonical output. Current persisted message redaction: `src/elspeth/web/sessions/routes/_helpers.py:1715-1730` documents it. Service path at `service.py:7582-7594` is not evidence the HTTP message exposes raw canonical data.

If implementing explicit observability first, test redacted data placeholder, absent data, wire APPROVAL_REQUIRED, normal applied mutation, and missing/duplicate correlation. Do not classify an unobservable row as approval absent or count it as definitely applied. Keep the existing real validation-code test; no need to redo already-landed defect 2. Full canonical capture would widen stored data; a closed status-only carrier gives the scorer the fact it needs with much narrower implications. Exact harness writer/API route for a new carrier needs discovery before implementation; not claimed identified by this census.

### RGR scorer

Production: `evals/lib/composer_rgr_score.py:656-708`, `_relaxed_invalid_state_reason:711`; producer: `src/elspeth/web/sessions/schemas.py:321-343`, protocol state records, service serialization. Tests: `tests/unit/evals/lib/test_composer_rgr_score.py`, `tests/unit/evals/test_convergence_scenarios_mocked_llm.py::_state_dict_for_scoring`, `tests/unit/evals/test_convergence_scenarios.py`.

Preferred standalone option: scorer receives separately named structured validation summary; test from actual serialized ValidationSummary and actual CompositionStateResponse instead of invented top-level state fields. Missing explicit summary on HTTP-shaped state remains not observable, and cannot grant an invalid-state relaxation. Preserve working harness route during migration. Upstream structured state option is semantically dependent on elspeth-8fe09316ab and its already-held error-record epoch/null decision; do not ask the same decision again.

## Decisions versus routine repair

Routine under campaign authorization: remove redundant free-text destination fact while retaining current structured error; close discovery branch construction without changing disclosure; freeze legacy regex expansion; make scorer uncertainty explicit; wire a caller-owned summary where already available.

Potential new decision only if selected: expand audit/export retention with full canonical tool payloads. Existing D4 preview audit deliberately hides data and runtime_preflight; ordinary provider receiving original ToolResult does not authorize retaining or replaying it. Standard-battery/candidate egress remains unauthorized; the separately approved exact synthetic baseline probe is complete and confers no wider permission. Missing replay/eval evidence is an evidence boundary, not a reason to weaken disclosure. Prefer a minimal closed status carrier only after proving it fits existing policy. The regex ticket already ratifies incremental freezing; deleting fallback before producer completeness is neither requested nor supported. No new global no-TypedDict-store lint rule is needed for this ticket.

Already-held error-record epoch/nulls, hidden-label ordering, and public-model null/minimum/hidden-sha256 decisions are deliberately not duplicated here. Tests/probes above are proposed, not executed. Concurrent work means source line numbers must be refreshed before editing.
