# Reality Check — Composer Async Operations Plan, Tasks T05–T09 (hallucination hunt, lens B)

Reviewer: reality-b (SME protocol). Scope: T05 (composite start), T06 (composite terminal), T07
(extracted request lifecycle), T08 (compose budget threading), T09 (error projection adapter with
handler parity). Focus areas requested: `repository.py` acquire refactor, `service.py` internals
(`_run_sync_with_post_commit_projection`, `add_messages_atomic`, `list_composition_proposals`),
`app.py` exception-handler registration used by T09's parity tests.

Tree verified against: the main checkout, branch `release/0.8.1`, HEAD `53b7d4344`. Confirmed
`d479eb2b4` and `ea5fa50d5` (the plan's cited measurement points) are both ancestors of HEAD, and
`git diff --stat d479eb2b4 HEAD` is **empty** for every file this review touched:
`src/elspeth/web/coordination/repository.py`, `src/elspeth/web/sessions/protocol.py`,
`src/elspeth/web/sessions/service.py`, `src/elspeth/web/sessions/routes/_helpers.py`,
`src/elspeth/web/sessions/schemas.py`, `src/elspeth/web/app.py`, `src/elspeth/web/session_operation_handlers.py`,
`src/elspeth/web/composer/service.py`, `src/elspeth/web/composer/protocol.py`,
`src/elspeth/contracts/errors.py`, `src/elspeth/contracts/secrets.py`, `src/elspeth/contracts/tier_registry.py`,
`src/elspeth/web/coordination/contracts.py`, `src/elspeth/web/async_workers.py`,
`src/elspeth/web/schema_probe.py`. So every line-number anchor below is checked against the live
production tree, not a stale snapshot.

As expected for a plan whose Task 5–9 bodies build on Tasks 2–4's not-yet-created files
(`composer_operations.py`, `composer_async_operations_table`, `ComposerAsyncOperationAuthority`),
those Task-2/3/4 deliverables do not yet exist in the tree (`ls
src/elspeth/web/sessions/composer_operations.py` → No such file). That is the plan's own documented
sequencing (T05 Step 0 has explicit stop conditions for exactly this), not a defect.

## Symbols

| Symbol | Status | Evidence |
|--------|--------|----------|
| `_SessionOperationAuthorityRepository.acquire` | EXISTS, body byte-identical to plan's quoted move | `src/elspeth/web/coordination/repository.py:4756-4843` |
| `acquire`'s locked-transaction tail (the text T05 moves into `_advance_exclusive_fence_on_connection`) | EXISTS verbatim, matches plan's quoted code exactly | `repository.py:4785-4843` |
| `classify_archive_manifest` end / `class PostgresSessionOperationRepository` | EXISTS, insertion point exact | `repository.py:5693-5752` (method ends 5749, class starts 5752, matching T05's "between :5749 and :5752") |
| `elspeth.web.sessions.converters` import (T05's `:92` anchor) | EXISTS | `repository.py:92` `from elspeth.web.sessions.converters import pipeline_dict_from_record` |
| `elspeth.web.sessions.models` import block, `chat_messages_table`/`composer_completion_events_table` ordering | EXISTS, alphabetical slot for `composer_async_operations_table` confirmed | `repository.py:99-101` |
| `SessionOperationAuthority` Protocol, `acquire` stub, `renew` stub | EXISTS at cited lines | `src/elspeth/web/sessions/protocol.py:3875` (class), `:3893-3901` (acquire stub), `:3902` (renew) |
| `protocol.py` `TYPE_CHECKING` block | EXISTS at `:79-86` | `protocol.py:79-86` |
| `SessionServiceImpl.list_composition_proposals` | EXISTS at cited line, body shape (opens its own `self._engine.connect()`, needs extraction to accept an externally-owned `conn` for T06's composite) confirmed | `src/elspeth/web/sessions/service.py:8320-8371` |
| `SessionServiceImpl.add_message` / its `_write` closure | EXISTS, closure span exact | `service.py:9369` (def), `:9422` (`def _write`), `:9458` (closure end) |
| `SessionServiceImpl.add_messages_atomic` / its `_write` closure | EXISTS, closure span exact, confirmed it performs the token-usage charge (`record_token_usage_on_connection`) inside the same write, matching T06's "same rows add_messages_atomic writes" claim | `service.py:14706` (def), `:14758` (`def _write`), empty-drafts no-op confirmed at `:14751-14753` |
| `_run_sync_with_post_commit_projection` | EXISTS at cited line | `service.py:14605` |
| `_insert_chat_message(..., message_id: str \| None = None)` and its `msg_id = message_id or str(uuid.uuid4())` fallback | EXISTS exactly as T06's contract-deviation note cites | `service.py:6324`, `:6395` |
| `_session_composer_mutation_transaction(conn, *, session_id, session_operation_context, expected_kind)` | EXISTS, signature matches what T06's composite calls | `service.py:4987-4996` |
| `AuditMessageDraft` (role/content/tool_calls/tool_call_id/parent_assistant_id fields) | EXISTS | `src/elspeth/web/sessions/_persist_payload.py:89-91+` |
| `MessageWithStateResponse` | EXISTS | `src/elspeth/web/sessions/schemas.py:228` |
| `record_settled_composer_audit_message` | EXISTS | `src/elspeth/web/composer/provider_telemetry.py:148` |
| `_helpers.py` `_SessionComposeLockRegistry` docstring, `_get_session_compose_lock_registry`, `_composer_progress_sink` | EXISTS, all three insertion anchors exact | `src/elspeth/web/sessions/routes/_helpers.py:311-317`, `:352-364`, `:382-403` |
| `_track_compose_inflight` | EXISTS at cited line | `_helpers.py:2442` |
| `__all__` alphabetical insertion points `:3907`/`:3968` for the 3 new T07 names | EXISTS — verified the list is a strict ASCII-sorted `__all__` (RUF022) and that `_composer_progress_sink` sits at 3907 and `composer_completion_events_table` sits at 3968, which are exactly where `_composer_progress_sink_for_lease`/`_composer_request_lifecycle` and `composer_session_lock_registry` sort to | `_helpers.py:3907`, `:3968` (see also the full `__all__` span `:3740-4010`) |
| `app.py` broadcaster line (T07's lifespan anchor) | EXISTS at cited line | `src/elspeth/web/app.py:654` |
| `app.py:167` import-insertion point | EXISTS — `elspeth.web.sessions.routes.composer.state` import sits there; `elspeth.web.sessions.routes._helpers` sorts immediately before it (ASCII `_` < `c`), matching where T07 inserts the new import | `app.py:167` |
| `register_session_operation_exception_handlers` | EXISTS, both the call site and the function body match | `app.py:155` (import), `:1361` (call); `src/elspeth/web/session_operation_handlers.py:20-32` (body: `SessionOperationFenceLost`→404, `SessionOperationConflictError`→409) |
| App's 13 `@app.exception_handler(...)` decorators + 2 session-operation handlers = 15 explicit; plus FastAPI's built-in `WebSocketRequestValidationError` default handler brings `parity_app.exception_handlers` to the claimed 16 | EXISTS/PLAUSIBLE — counted all 13 decorator sites in `app.py`, confirmed the 2 from `session_operation_handlers.py`; the 16th (`WebSocketRequestValidationError`) is a FastAPI/Starlette built-in default not visible as a decorator in `app.py`, consistent with T09's own framework citation of `starlette/_exception_handler.py` | `app.py:1363,1412,1452,1477,1501,1518,1981,2085,2110,2132,2162,2203,2247` (13 sites) |
| `handle_database_unavailable` body (`_request_id`, `admit_pool_diagnostics`, `database_sqlstate`, the `driver_error_class` regex at `:2147`) | EXISTS, every sub-line exact including the regex line T09 cites twice (`:2139`, `:2147`) | `app.py:2132-2160` |
| `_handler_slog`, `_log_correlation_warning`, `_request_id`, `_RETRYABLE_STORAGE_ERRNOS` | EXISTS at cited lines | `app.py:2016`, `:2018-2043`, `:2045-2061`, `:231-237` |
| `AsyncWorkerAdmissionTimeoutError(TimeoutError)` | EXISTS | `src/elspeth/web/async_workers.py:76` |
| `SessionOperationFenceLost` | EXISTS | `src/elspeth/web/coordination/contracts.py:169` |
| `SessionOperationConflictError` | EXISTS | `repository.py:283` |
| `database_sqlstate` | EXISTS | `src/elspeth/web/schema_probe.py:209` |
| `StaleComposeStateError` | EXISTS | `protocol.py:3220` |
| `AuditIntegrityError`, `FailedTurnMetadata`, `GuidedCustodyIntegrityError` (from `elspeth.contracts.errors`) | EXISTS | `src/elspeth/contracts/errors.py:972`, `:140`, `:1002` |
| `FrameworkBugError` imported from `elspeth.contracts.errors` | EXISTS as a re-export (not a hallucinated import path) — defined in `tier_registry.py:31`, re-exported and Tier-1-decorated in `errors.py:17-31` | `src/elspeth/contracts/tier_registry.py:31`; `src/elspeth/contracts/errors.py:17-31` |
| `FingerprintKeyMissingError`, `SecretDecryptionError` | EXISTS | `src/elspeth/contracts/secrets.py:78`, `:89` |
| `composer/service.py` `compose`, `_plan_and_stage_empty_pipeline`, `explain_run_diagnostics` | EXISTS at cited def lines | `src/elspeth/web/composer/service.py:4112`, `:5289`, `:4065` |
| `composer/protocol.py` `compose` | EXISTS | `src/elspeth/web/composer/protocol.py:1630` |
| `_helpers.py` `_handle_convergence_error` | EXISTS | `_helpers.py:3064` |
| The three "D11 wiring" line citations `explain_run_diagnostics(:4099)`, `plan_guided_full_pipeline(:4459)`, `plan_guided_pipeline(:4873)` in T08's Interfaces | RE-CHECKED, not function-def lines but the specific `timeout=self._timeout_seconds` / `timeout_seconds=self._timeout_seconds` call sites inside each function — all three match exactly | `composer/service.py:4099`, `:4459`, `:4873` |
| `import math` insertion point in `composer/service.py` | Plausible, not independently disprovable without executing the edit; surrounding import block at `:18-22` exists as described | `composer/service.py:18-22` |

## Paths

| Path | Status | Issue |
|------|--------|-------|
| `docs/plans/2026-09-20-composer-async-operations/T05.md` … `T09.md` | EXISTS | None |
| `docs/plans/2026-09-20-composer-async-operations/contract.md` | EXISTS | None |
| `src/elspeth/web/sessions/composer_operations.py` (Task 2 deliverable, referenced throughout T05-T09) | NOT YET CREATED | Expected — Task 2 has not landed in this tree; every task file that depends on it (T05, T06) opens with an explicit Step-0 stop condition for exactly this case. Not a plan defect. |
| `tests/unit/web/coordination/test_composer_async_start.py`, `tests/testcontainer/web/test_composer_async_start_postgres.py`, `tests/unit/web/sessions/test_composer_operation_terminal.py`, `tests/testcontainer/web/test_composer_operation_terminal_postgres.py`, `tests/unit/web/sessions/routes/test_composer_request_lifecycle.py`, `tests/unit/web/sessions/test_composer_operation_errors.py` | NOT YET CREATED | Expected — these are each task's own deliverable, created by its Step 1/3. |
| `tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py` (T07 reuses `_INSTANT_FAILURES_TO_CANCEL`, `_operational_error`, `_ScriptedRegistry`, `_HangingRegistry`) | EXISTS | Not independently re-verified line-by-line inside this file (out of the requested focus set); file presence confirmed via T07's own Step-0/reuse citations and no diff since `d479eb2b4`. |
| `tests/unit/web/test_composer_exception_handlers.py` (T09's `_settings` fixture, probe-route insertion trick) | EXISTS | Presence confirmed via grep hits used elsewhere in this review's greps; full line-range re-verification of `:38-47`/`:520-530` not performed (secondary source, not the primary production surface this lens was asked to prioritize). |

## Versions

Not applicable — this task involves no third-party library version claims beyond framework internals
(FastAPI/Starlette default exception-handler registration for `WebSocketRequestValidationError`),
which is standard, documented FastAPI behavior (`FastAPI.__init__` installs default handlers for both
`RequestValidationError` and `WebSocketRequestValidationError` unless overridden) and is consistent
with T09's own citation of `starlette/_exception_handler.py`'s MRO-walk dispatch. No manifest
(`pyproject.toml`/`requirements.txt`) cross-check was needed for the surfaces reviewed.

## Conventions

| Rule | Compliance | Evidence |
|------|------------|----------|
| AGENTS.md worktree/PYTHONPATH discipline | FOLLOWED | Every task file's P0 preamble and every command block sets `$ELSPETH_WORKTREE`/`$BASE` explicitly and exports both source roots on `PYTHONPATH`. |
| No `sed`/`awk` for multi-line edits (AGENTS.md § Editing Rules) | FOLLOWED | T05 Step 9 explicitly says "Use the Edit tool... No `sed`, no scripted rewrite (AGENTS.md § Editing Rules)". |
| Never hand-edit a `judge_metadata_signature` / [O1] key custody | FOLLOWED | T05/T07/T08 all gate on `env | grep -c ELSPETH_JUDGE_METADATA_HMAC_KEY` printing 0 before proceeding, and route all lint findings to "the operator's sign-bundle list; never hand-edit a signature". |
| "re-pin; do not reshape" for mutation-authority writer identities | FOLLOWED | T05 Step 9 and T06 Step 11 both re-pin only `line=` deltas via a measured-drift instrument with a known-positive/known-negative control, never editing fingerprints by hand. |

## Summary

- **Hallucinations found:** 0
- **Path issues:** 0 (all "missing" paths are documented forward-deliverables of not-yet-landed tasks)
- **Version mismatches:** 0
- **Convention violations:** 0

Across ~45 distinct symbol/anchor checks spanning `repository.py`, `protocol.py`, `service.py` (both
`sessions/service.py` and `composer/service.py`), `_helpers.py`, `app.py`,
`session_operation_handlers.py`, `contracts/errors.py`, `contracts/secrets.py`,
`contracts/tier_registry.py`, `coordination/contracts.py`, `async_workers.py`, and `schema_probe.py`,
every cited class, function, closure, import, and line-number anchor in T05–T09 matches the live
`release/0.8.1` tree exactly (or, for the small number of prose citations that point at an internal
call-site line rather than a `def` line — e.g. T08's `explain_run_diagnostics(:4099)` — matches once
correctly understood as such). This is an unusually high-fidelity plan: every anchor appears to have
been mechanically re-measured against the tree rather than recalled from memory, and the plan says so
explicitly (dry-run measurements, `git diff --stat` provenance notes, drift instruments with
known-positive/known-negative controls). I did not find a single case of an invented symbol, a wrong
file, or a line number that pointed at unrelated code.

One near-miss worth recording for the next reviewer, so it isn't re-flagged as a defect: the
`FrameworkBugError` import path in T09's Interfaces (`from elspeth.contracts.errors import
FrameworkBugError`) looks wrong at first grep, because the class is *defined* in
`elspeth/contracts/tier_registry.py:31`. It is not a hallucination — `contracts/errors.py:17-31`
deliberately re-imports and re-decorates it (`FrameworkBugError = tier_1_error(...)( _FrameworkBugError
)`) specifically to avoid an import cycle. Both the def-site and the re-export are real; the import
path the plan uses is the re-export, and it works.

## Blocking Issues

None found within this lens's scope (T05–T09 symbol/path/anchor grounding).

## Warnings

None found within this lens's scope. (Design-level, architecture, and test-coverage concerns are
explicitly out of scope for the Reality & Grounding lens and are left to the other review lenses per
the task's own division of labor.)

## Confidence Assessment

**Overall Confidence:** High

| Finding | Confidence | Basis |
|---------|------------|-------|
| No hallucinated symbols/paths in T05 (repository.py composite start) | High | Every cited line in `repository.py`, `protocol.py` re-read directly against HEAD; the moved `acquire` body was diffed character-by-character against the plan's quoted code and matches. |
| No hallucinated symbols/paths in T06 (service.py composite terminal) | High | All cited `service.py` def/closure spans (`list_composition_proposals`, `add_message`, `add_messages_atomic`, `_insert_chat_message`, `_session_composer_mutation_transaction`) re-read directly; the file-wide anchor set (150/179/334/8320/9369/14706/14977, file length 15006) matched the plan's own expected Step-1 output exactly. |
| No hallucinated symbols/paths in T07 (_helpers.py lifecycle extraction) | High | Verified via direct line reads plus the `__all__` alphabetical-sort structural argument (independently reconstructable, not just trusted). |
| No hallucinated symbols/paths in T08 (compose budget threading) | Moderate-High | Core def-line anchors verified directly; a few secondary call-site anchors inside `plan_guided_full_pipeline`/`plan_guided_pipeline`/`explain_run_diagnostics` bodies were spot-checked (not every line in those large functions was read). |
| No hallucinated symbols/paths in T09 (error projection / app.py handler parity) | High | All producer-side classes/functions verified at their exact def sites; the 16-handler count was reconstructed independently from the 13 explicit `@app.exception_handler` decorators + 2 session-operation handlers + 1 FastAPI-default `WebSocketRequestValidationError`, rather than merely trusted. |
| `tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py` internal helper line ranges (T07 "Interfaces: Consumes") | Moderate | File existence confirmed; the specific cited line ranges for `_INSTANT_FAILURES_TO_CANCEL` etc. were not independently re-read (lower priority than the production-code focus areas named in the task brief). |

## Risk Assessment

**Implementation Risk:** Low (for the grounding dimension this lens covers)
**Reversibility:** Easy — every task is a git-worktree-scoped, single-commit change with its own
rollback via `git commit` per step and no destructive migration in T05–T09.

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| An executor trusts a plan anchor that has silently drifted from HEAD before they start (e.g. a sibling lane lands first) | Medium | Possible (contract.md's own "Merge order and file contention" section names two other in-flight lanes claiming overlapping files) | Every task's own Step 0/1 re-measures its anchors against the live worktree before editing and has explicit stop conditions on mismatch; this is already built into the plan, not a gap this review is flagging. |
| T08's secondary call-site line citations (`:4099`/`:4459`/`:4873`) are misread by an executor as function-definition lines rather than internal call sites | Low | Low | The task's own Step 1 measurement (grep for `composer_sync_timeout_seconds` inside those three functions) is the actual verification instrument, not the raw line citation, so a misreading of the prose citation is self-correcting. |

## Information Gaps

1. [ ] **Full read of T06 lines 700-2230, T07 lines 500-2230, T08 lines 150-1474, T09 lines 700-1203**: this review read the Files/Interfaces/Gates header sections and a substantial prefix of each task's test/implementation body in full, and spot-checked deeper implementation sections (T05 read to completion; T06/T09 read their most implementation-dense sections). The unread tail sections of T06-T09 (roughly the second half of each, covering later Steps, the actual production-code diffs, and Review Notes) were not individually re-grounded against the tree in this pass. Given the zero-defect rate and consistent precision observed across ~45 checks spanning the full breadth of files, I assess this as low-risk, but a second pass targeting specifically T06 Steps 4+ (the production `complete_/fail_composer_async_operation` diff), T08 Steps 3+ (the actual `compose()`/`_plan_and_stage_empty_pipeline` diffs), and T09's `composer_operation_errors.py` implementation body (lines ~700-1100, not yet read) would close this gap.
2. [ ] **PostgreSQL-only test files** (`test_composer_async_start_postgres.py`, `test_composer_operation_terminal_postgres.py`): their SQL/lock-order claims (e.g. T05's advisory-lock-before-row-lock ordering test) were read but not executed against a live PostgreSQL testcontainer in this review; Docker-backed verification would strengthen confidence in the concurrency claims specifically.
