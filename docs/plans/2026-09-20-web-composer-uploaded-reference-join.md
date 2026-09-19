# Web Composer uploaded reference tables implementation plan

> **For implementation:** Use `superpowers:executing-plans` task by task. Use `superpowers:systematic-debugging` for any unexpected failure. The requested `yzmir-llm-specialist:using-llm-specialist` guidance applies to Composer tool descriptions and the LLM prompt acceptance case.

**Goal:** A user can upload a CSV or JSON reference table in Web Composer, wire it to `reference_join`, receive an accurate readiness verdict, and execute the approved pipeline with the same pinned bytes. A user-uploaded text blob can likewise be used as an LLM node prompt and reach the provider unchanged.

**Architecture:** Keep the existing `wire_blob_inline_ref` marker and runtime resolver. Extend preflight to validate the *actual, bounded, fence-authorized* blob bytes for inline refs before plugin construction; preserve the runtime's separate link, hash verification, substitution, and audit write. Cover all three preflight callers, then make freeform upload wording and planner guidance accurately describe non-source uses. No server path authors pipeline structure and no tutorial-only branch is introduced.

**Tech stack:** Python 3.12, FastAPI, SQLAlchemy, pytest, React/TypeScript, Vitest, the existing BlobService and Composer tool loop.

**Prerequisites:** Work on `release/0.8.1` from an isolated worktree with both source roots on `PYTHONPATH`. Read `CONTRIBUTING.md` § Whole-tree gates and the applicable `AGENTS.md` before code. Do not handle the judge HMAC key or globally clear trust-tier signatures. Track the primary defect as `elspeth-0bb1407e98`; coordinate duplicate-key rejection with `elspeth-d90b13eb80`.

---

## Current evidence and release condition

- `validate_pipeline_for_trained_operator` rejects ready uploaded CSV and JSON `reference_join` blobs at `plugin_instantiation`: `src/elspeth/core/blobs_inline.py` substitutes the same generic placeholder for every inline ref, while `ReferenceJoinConfig` parses its table during construction. Direct construction with the actual CSV/JSON is a positive control. This upload path is a release blocker until the table content passes preflight and run admission.
- User-verbatim uploaded text in an LLM `prompt_template` passed a direct `is_valid=True` preflight with no failed checks. The upload route, LLM prompt admission, substitution, and LLM-authored refusal focused tests exited 0 (`28 passed` in the 2026-09-20 local run). That is component-level evidence; it does not prove a browser upload through execution and the provider call.
- Runtime currently reads and hashes pinned blob bytes in `src/elspeth/web/execution/service.py`; preflight currently checks metadata and substitutes placeholder text in `src/elspeth/web/execution/_validation_materialization.py`. Preserve the approved-prompt/executed-prompt attestation contract: the bytes visible in review, validation, and execution must agree or execution must refuse.

## Task 0 — Settle the blob-read and lock boundary

**Files to inspect:** `src/elspeth/web/blobs/service.py`, `src/elspeth/web/execution/service.py`, `src/elspeth/web/composer/service.py`, `src/elspeth/web/sessions/service.py`, `src/elspeth/web/sessions/pending_interpretation.py`, `src/elspeth/web/app.py`.

1. Trace `validate_pipeline` from the authoritative execution preflight, Composer `_runtime_preflight`, and the session `_session_runtime_preflight`. Record the operation context available at each call and whether a session write lock/transaction is held. In particular, interpretation resolution invokes runtime preflight inside its write transaction, while `BlobServiceImpl.read_blob_content_sync()` acquires blob custody and checks the exact operation fence. Prove the lock order with an existing contention test or add a targeted one before making that call path read content.
2. Choose and document one safe contract: either fetch a bounded, immutable verified snapshot before the session transaction and bind it to the state version/fence, or make the in-transaction read safe with a proven lock order. A stale state, expired fence, deleted blob, changed hash, or inaccessible blob must fail closed. Do not use metadata-only placeholder success for a content-dependent plugin.
3. Define one typed preflight access boundary that provides metadata plus `(BlobRecord, bytes)` reads under the same session scope; pass it to the three callers. Avoid a global blob lookup or a context-free callback. Keep content failures in the existing `blob_inline_refs` readiness/error family; do not expose blob bytes in errors or telemetry.

**Verification:** Draw the call/lock sequence in the implementation PR description, run the new contention/fence test with `PYTHONPATH=<worktree>/src:<worktree>/elspeth-lints/src <venv>/bin/python -m pytest <exact-test-node> -n 0` and record the exited code. **Done when:** every entrypoint has a specific safe read strategy and a failing lock/fence test exists where needed. Do not proceed on a guessed lock order.

## Task 1 — Reproduce the preflight failure with real plugins

**Test:** `tests/unit/web/execution/test_validate_blob_inline.py`; optionally extend `tests/integration/pipeline/test_composer_runtime_agreement.py` for the real authoring-to-runtime path.

1. Add parameterized CSV and JSON `reference_join` cases using a ready, user-verbatim blob record and actual bytes, pinned SHA-256, `reference_format`, `reference_key_name`, `key_field`, and an output expression. Call the real `validate_pipeline` with real `reference_join` construction; do not mock settings/plugin instantiation (the current valid-marker test does, which hid the defect).
2. Assert the pre-change result fails at `plugin_instantiation` despite valid bytes. Add controls: actual inline text succeeds; wrong hash, inaccessible session, missing/failed blob, oversized ref, invalid encoding, malformed table, and LLM-authored prompt-surface blob are refused at their proper boundaries.
3. Run the named tests with `-n 0`, a unique log file, and explicit exit-code capture. **RED is expected** for valid CSV and JSON marker cases; the controls must have their intended outcomes. Commit the regression separately after checking `git status --short` and `scripts/branch-safety-check.sh --intent commit`.

**Definition of done:** Tests prove the original false-negative with real plugin construction and distinguish it from true invalid blobs.

## Task 2 — Make validation content-aware without runtime side effects

**Modify:** `src/elspeth/core/blobs_inline.py`, `src/elspeth/web/execution/_validation_materialization.py`, `src/elspeth/web/execution/validation.py`; adjust `src/elspeth/contracts/blobs.py` only if a new typed contract is needed.

1. Add a synchronous, fence-scoped content-read dependency to `validate_pipeline` and thread it through `_validate_pipeline_impl` to `materialize_validation_yaml`. Use `BlobServiceImpl.read_blob_content_sync()` or the existing protocol equivalent. Keep the current per-ref 256 KiB and aggregate 1 MiB limits, checking declared size before reads and actual byte length afterward. Deduplicate reads by blob ID without dropping per-field hash/encoding checks.
2. After metadata and prompt-modality checks, use the existing `_discover_blob_content_refs` and `_substitute_blob_content_refs` logic to verify each pinned SHA-256, decode content, and replace markers with real strings in the validation copy. Feed that YAML to the existing settings/plugin/graph pipeline. Do not link a blob to a run, persist inline-resolution audit rows, or mutate the authored state during validation.
3. Convert expected missing, stale, undecodable, wrong-hash, and user-correctable table errors into explicit failed validation checks with field paths and safe messages. Preserve Tier-1 integrity exceptions as integrity failures. If an inline marker is present but the authorized content dependency is unavailable, fail closed instead of claiming ready. The plugin still owns CSV/JSON semantics; validation must not invent a separate table parser.
4. Run the Task 1 tests to GREEN, then the whole `tests/unit/web/execution/test_validate_blob_inline.py` and `tests/unit/web/execution/test_inline_blob_prompt_surface_readiness.py` selections with `-n 0`. Record terminal exit codes. **Done when:** valid uploaded CSV and JSON pass actual plugin construction and malformed or mismatched bytes are refused before approval.

## Task 3 — Connect every preflight caller and preserve approval parity

**Modify:** `src/elspeth/web/execution/service.py`, `src/elspeth/web/composer/service.py`, `src/elspeth/web/app.py`, and only the necessary session/pending-interpretation interfaces from Task 0.

1. Supply the scoped read dependency in `ExecutionServiceImpl._authoritative_state_preflight_sync`, where metadata is already scoped by session and operation context. Use the same fence for metadata and content.
2. Supply it in `ComposerServiceImpl._runtime_preflight` and its `_cached_runtime_preflight` worker. The cache key must include the state/marker's pinned hash and session authority; prove a changed/deleted/failed blob cannot reuse a ready verdict. If blob mutability makes this unsafe, invalidate or bypass cache for inline refs rather than returning a stale ready verdict.
3. Supply it in `app.py`'s `_session_runtime_preflight` used by interpretation resolution. Implement the lock-safe strategy from Task 0 in both `SessionServiceImpl._validate_patched_composition_state` and `_SessionPendingInterpretationValidator`; the persisted `is_valid` must match content-aware runtime preflight. Keep read failures deterministic and free of partial writes.
4. Add tests for all three entrypoints and an approval-to-execution parity case: approving a valid reference table permits the run; tampering or removing the blob after approval makes admission refuse before execution; a successful run records the resolved field/hash/byte length exactly once. Run each focused test with `-n 0` and explicit exits. **Done when:** no production `validate_pipeline` call silently falls back to placeholder validation for a marker.

## Task 4 — Make uploaded-file roles clear to the planner and user

**Modify:** `src/elspeth/plugins/transforms/reference_join.py`, `src/elspeth/web/composer/skills/pipeline_composer.md`, `src/elspeth/web/frontend/src/components/chat/ChatInput.tsx`, and `ChatPanel.tsx` only if a separate guided/freeform sentence is required.

**Test:** `src/elspeth/web/frontend/src/components/chat/ChatInput.test.tsx`, `ChatPanel.test.tsx`, `ChatPanel.guidedStartRecovery.test.tsx`; add a focused Composer-tool guidance test in `tests/unit/web/composer/` where existing assistance tests live.

1. Teach freeform Composer that an already uploaded ready blob can be discovered with `list_blobs`/metadata and wired directly to `node:<id>.options.reference_content`; `create_blob` is only for content the planner creates. Explain CSV/JSON format and that the table is transform configuration, not a pipeline source. Include the same discovery rule for an uploaded LLM prompt, but preserve the prohibition on wiring LLM-authored blobs to prompt surfaces.
2. Change the freeform upload draft sentence so it names the uploaded file without asserting it is pipeline input. Preserve the guided source-selection wording and behavior, and preserve the originating-session draft behavior after a session switch. The user can explicitly state “reference table” or “LLM prompt” in the draft.
3. Test both freeform roles, guided source selection, and late upload completion after a session switch. Verify the planner is still called for each structural transition; no server-created graph or tutorial-specific path. Run the relevant frontend tests via `cd <worktree>/src/elspeth/web/frontend && npm test -- --run <exact-test-files>` and the focused Python guidance test. **Done when:** the UI and tool guidance do not misclassify a reference table or prompt as a source.

## Task 5 — Reject ambiguous reference-table keys before release

**Modify:** `src/elspeth/plugins/transforms/reference_join.py`; **test:** `tests/unit/plugins/transforms/test_reference_join.py`.

1. Add failing CSV duplicate-header and JSON duplicate-object-member tests. Current `csv.DictReader` and `json.loads` silently keep the last value; this can change joins and output expressions without operator notice. Cover exact duplicate CSV names, duplicate JSON members in any entry, and a nonduplicate control.
2. Reject duplicates in the plugin's single parsing path so CLI, Composer preflight, and runtime agree. Keep field names verbatim; do not normalize them. Run the focused plugin tests with `-n 0` and explicit exit code. Coordinate/close `elspeth-d90b13eb80` only after source and test verification. **Done when:** no duplicate key silently overwrites another.

## Task 6 — Prove uploaded LLM prompt behavior end to end

**Test:** extend `tests/unit/web/execution/test_inline_blob_prompt_surface_readiness.py` and add a focused web/Composer integration test alongside `tests/integration/pipeline/test_composer_runtime_agreement.py` if its fixtures can run a provider stub.

1. Start with the existing positive controls: upload route, user-verbatim modality, prompt-template substitution, and LLM-authored refusal. The 2026-09-20 local focused selection exited `0` with `28 passed`; do not treat that as full user-flow proof.
2. Upload a UTF-8 text file through the authenticated upload API, have the actual Composer tool loop wire its blob marker to an LLM `prompt_template` or query template, validate, and run against a deterministic provider stub. Assert the exact prompt bytes received by the provider and the inline-resolution audit hash agree. A user-verbatim prompt creates no prompt-review card and must not mint an approved-prompt artifact hash (ADR-034). Include session mismatch, tampered bytes, and LLM-authored blob refusal. This tests the complete authorization and content-identity seam without paying or depending on a live provider.
3. Exercise the browser upload affordance in both reference-table and LLM-prompt freeform sessions with Playwright or the repository's browser test harness; assert the uploaded filename/draft survives a session switch and the correct role reaches the planner. **Done when:** an uploaded prompt reaches the provider in the executed run, and forbidden modalities never do.

## Task 7 — Integration and release gate

1. Review every touched file in full for trust-tier boundaries and whole-tree pinned AST/hash gates. Run focused backend/frontend tests, then `scripts/full-suite-gate.sh --execute --detach` from the implementation worktree with both source roots verified; read its `summary.txt` and confirm `frozen=YES`. Because the session preflight/locking boundary is touched, include serial PostgreSQL testcontainer tests. Compare existing trust-tier finding corpus before/after; do not re-sign it.
2. Perform one local authenticated Web Composer acceptance walk for uploaded CSV and JSON reference tables and one uploaded LLM prompt. Record the API validation verdict, approval identity, execution terminal status, and the provider-stub/audit evidence. Do not call a partial unit selection a production acceptance run.
3. Run `git status --short` and `scripts/branch-safety-check.sh` before each commit/merge. Integrate only after all required checks have terminal exits and the target SHA is frozen. Update Filigree `elspeth-0bb1407e98` and `elspeth-d90b13eb80` with measured evidence. **Release-ready condition:** valid uploads pass preflight and execute, invalid/tampered uploads fail before execution, LLM prompt bytes match approval and provider input, and the suite/testcontainer/whole-tree gates show no new regression.
