# Gate repair for 0.8.12: findings 1, 1b, 2 and 5 of the loose-ends sweep

Branch `fix/composer-0812-gate-repair`, cut from `fix/composer-remediation-0.8.12` at `2f38b807b`. Nothing was pushed.

All four items are repaired and the reserve-admission proof is clean: no global failures and 32 of 32 web rows proven. No production behaviour changed; the only `src` edit deletes dead code.

| Item | Result | Commit |
| --- | --- | --- |
| 1. Stale whole-file pins | Reviewed, no source fix needed, two hashes re-pinned | `6e226d6e7` |
| 2. Second recovery registration | Contract now admits exactly two named sites, with controls | `6e226d6e7` |
| 3. Unallowlisted chat-message writer | Seed routed through the reviewed session writer; no allowlist entry | `c9e3d7b00` |
| 4. Dead `_blob_id_uuid_validation_error` | Deleted with its unused import | `b9c84a7a7` |

Three things need the maintainer's eye:

- **33 other tests in `test_web_landscape_mutation_fencing.py` fail on this branch, before and after this work.** The failure set is identical on the base commit, they are outside the four items, and they were not diagnosed. Details in the last section.

- **Approved xfail drift grew by two sites.** Item 4 moves `blobs.py` up 16 lines. The Sessions writer-review test keys its read-connection inventory by line, so two more sites are now reported stale and unreviewed. The test was not edited and still xfails. Details under item 4.
- **The official pytest node for the proof passed** (`-n 0`, 383 s, exit 0); details at the end of this document.

All runs used `PYTHONPATH=<worktree>/src:<worktree>/elspeth-lints/src`, the same two roots in `-o pythonpath=`, and `-p no:cacheprovider`; `elspeth.__file__` and `elspeth_lints.__file__` resolved inside the worktree. Logs are in `.claude/lanes/gate-repair/` (local, gitignored); log names are given per result. The host was shared with CI shards (load average about 15), so pytest ran one process at a time at `-n 5` or lower.

## Item 1: review of the lines dc67bdd2f added to the two pinned files

### What was read

The full diff of `dc67bdd2f` for `src/elspeth/web/async_workers.py` (+62) and `src/elspeth/web/required_executor.py` (+15), both files' surrounding code, `src/elspeth/composer_mcp/standalone.py`, the shutdown path in `src/elspeth/web/app.py`, `reports/d2-portable-lifecycle-design.md` (the section on executor ownership), `reports/d2-lifecycle-3-report.md` ("Narrow executor ownership expansion") and `reports/d2-lifecycle-3-fix1-review.md`. The fix-1 review covers only the stdio descriptor fix, so it is not evidence for these lines.

### What the change does

- `required_executor.py`: the custodian takes two optional callbacks. `quarantine()` calls `retain_recovery` before starting the drain thread. The observer now waits for the drain thread to exit and joins it before returning. `_observer_finished` calls `release_recovery` once the thread is dead.
- `async_workers.py`: a module set `_RECOVERING_GENERATIONS` holds quarantined generations whose recovery is outstanding. A generation is released only when its observer and drain both finished without error and without escalation. `shutdown_async_workers` now settles every retained generation: it awaits the observer, joins the drain thread, records a drain error, and records `RequiredGenerationUnavailable` for an escalated one.
- `async_workers.py`: `configure_standalone_executor_recovery` refuses when a recovery callback, shared executor or custodian already exists, then forwards to `configure_required_executor_recovery` without a finalizer owner.

### Conclusion: correct, no source change

- **No admission path changed.** `_submission_unavailable`, `_submit_shared`, the reservation and release callbacks and the witness logic are untouched. `_generation_for` only passes the two new callbacks.
- **The registry is touched from the event-loop thread only.** Every `quarantine()` call is either inside a coroutine or scheduled with `loop.call_soon_threadsafe` (five sites in `async_workers.py`); `_observer_finished` is a task callback; shutdown is a coroutine. A plain `set` is therefore safe.
- **Release happens after physical thread exit.** The drain thread sets `recovery_finished` just before returning, so the observer's extra wait closes the window in which custody was released while the thread was still alive.
- **Failed recoveries are kept for shutdown, not dropped.** A drain error leaves `recovery_finished` unset, so the observer runs to its deadline and escalates; the generation stays retained and shutdown reports both the drain error and the escalation.
- **The web lifecycle tolerates the new shutdown failures.** `app.py` calls `shutdown_async_workers` through `observe_shutdown`, which collects any exception into the lifecycle group. After an escalation the shared executor is deliberately not reset, so a later registration is refused ("Previous shared generation has not joined"); that is fail-closed and matches the existing behaviour for an unjoined generation.
- **The standalone wrapper cannot replace a web registration.** `_RECOVERY_CALLBACK` is assigned only inside `configure_required_executor_recovery` and is never reset (grep over all of `src` finds no reference outside `async_workers.py`; the whole file is also hash-pinned), so once any owner has registered the wrapper refuses for the life of the process. `create_app` registering after a standalone registration overwrites every cell with its own objects.

### Observations, not defects

- If a worker is hung, shutdown waits on the retained drain thread without a bound. The pre-existing loop on `generation.joined` already waited without a bound in the same state; the process watchdog is the bound in both cases.
- The two `assert ... is not None` in the new shutdown loop would raise if `quarantine()` failed between retaining the generation and starting its thread. That needs `Thread.start()` to fail, and in that state shutdown already blocks earlier on `generation.joined`, so the asserts are not reachable first.
- A cancelled observer task would be recorded once as a caller cancellation and once as a failure. Nothing in `src` cancels that task.
- The standalone process registers no finalizer owner. `run_application_finalizer_in_worker` is referenced only in web modules (`app.py`, `coordination/membership_lifecycle.py`, `execution/service.py`). Inferred from that, not traced call by call: the standalone path does not reach it.

### Change

`tests/helpers/required_work_reserve_recipes/reviewed-full-source-guards.json`, two values only:

| File | Old pin | New pin |
| --- | --- | --- |
| `src/elspeth/web/async_workers.py` | `40b670cb…fadd7b034` | `3e97fa7f9a9106faeb419921febf7c5dc9b3c8c015a85ab5327c037df5b73c38` |
| `src/elspeth/web/required_executor.py` | `4ce38dc6…c14b45f9` | `c2477c7d174b8cb31a19fd723196a40974cf29cddbd57ec90864a94d3387166e` |

Both files are byte-identical to `dc67bdd2f`. They were also added to the two existing guard tests in `tests/unit/architecture/test_web_reserve_reviewed_contracts.py`, which did not cover them: the changed-bytes control and the previous-digest control (the old pins above must refuse the current source).

## Item 2: the second recovery registration

### What the single-registration rule protects

`tests/helpers/web_recovery_producer_contract.py` proves that the two recovery cells in `async_workers.py` (`_INSTANCE_DRAINING`, `_APPLICATION_FINALIZER_OWNER`) are stored only by `configure_required_executor_recovery`, and that the only caller is `create_app`, which passes the event and finalizer owner it has just constructed. The reserve proof relies on that: web admission reads those cells. A second caller could install a foreign event or no owner.

### Decision: admit the second site by name (option b)

One shared call site is not available without rewriting `create_app`, whose whole body the contract pins, and the standalone process never constructs a web app. Calling the registration directly from `standalone.py` would still be a second site and would separate the guard from the call. The wrapper is the smaller and clearer shape, and it preserves the protected claim for the reason given in item 1: its guard makes it first-registration-only. An independent read-only review relayed by the dispatching session reached the same conclusion; it was checked against the code rather than taken as given.

### Change

`recovery_cell_contract` now requires exactly:

1. the `create_app` call (unchanged);
2. the forwarding call inside `configure_standalone_executor_recovery`, whose whole body is pinned as `STANDALONE_CONFIGURE` (guard, refusal, forwarding with no finalizer owner);
3. exactly one call of the wrapper, in `run_standalone` in `src/elspeth/composer_mcp/standalone.py`.

The wrapper is added to the protected bindings, so rebinding it, replacing it by reflection, or using it other than as a call target fails the same way as for the base function. The count is exact, not "at least one". `async_workers.py` was not edited.

### Controls (new tests in `test_web_reserve_reviewed_contracts.py`)

| Mutation | Result |
| --- | --- |
| Live tree, all production units and the five-file subset | no failures |
| Third direct call of the base function from a new module | `recovery registration call/escape universe changed` |
| Third call through `async_workers.` qualification | same |
| Second call of the wrapper from a new module | `recovery standalone registration call/escape universe changed` |
| Wrapper returned as a value | `recovery registration callable escape …` |
| `standalone.py` missing, or its call removed | `recovery standalone registration call/escape universe changed` |
| Wrapper's forwarding call removed | `recovery standalone registration unowned-process guard/forwarding contract changed` |
| `create_app` call removed | `recovery caller complete executed producer/dominance/transfer contract changed` |
| Guard loses the existing-owner test, refusal replaced by `pass`, or forwarding gains an argument | `recovery standalone registration unowned-process guard/forwarding contract changed` |

Each test asserts the exact failure list, so a weakened contract fails them.

## Items 1 and 2: the reserve-admission proof

Body of `test_required_work_reserve_admission_on_actual_sources` run directly (`_reserve_run(_production_units())`):

| Tree | `global_failures` | proven / unproven web rows | Log |
| --- | --- | --- | --- |
| Before (`2f38b807b`) | the two `full source changed` strings and `recovery registration call/escape universe changed` | 0 / 32 | `proof-baseline.log`, exit 1, 111 s |
| After | `[]` | 32 / 0 | `proof-after.log`, exit 0, 349 s |

The sweep's inference is confirmed: the 32 unproven rows were a cascade of the global failures, not 32 separate defects. No further failure was hidden behind the full-source check.

### On-disk negative controls (`negative-controls-1-2.log`), each reverted

| Mutation | Full-source guard | Registration contract |
| --- | --- | --- |
| None | `[]` | `[]` |
| One byte appended to each pinned file | both `full source changed …` | `[]` |
| New module under `src/elspeth/web/` calling `configure_required_executor_recovery` | `[]` | `recovery registration call/escape universe changed` |
| Reverted | `[]` | `[]` |

## Item 3: unallowlisted chat-message writer

### Does the 0.8.11 candidate fail on its own? Yes, measured.

`origin/release/0.8.11-candidate` (`a67628c29`) was extracted with `git archive` into the lane directory and `tests/unit/web/sessions/test_static_direct_writers.py` was run from it with `python -I` and explicit paths (`cand-static-writers.log`): 1 failed, 20 passed, exit 1, with the same site, `tests/integration/web/test_shareable_reviews_routes.py:606 [chat_messages/sqlalchemy_table_insert]`. The extracted tree has been deleted.

The first attempt was an instrument failure and is not the evidence: the tree gate drops gitignored files, and the lane directory is gitignored, so the scan saw nothing and reported every reviewed entry as stale. Making the extracted tree its own git repository fixed the instrument.

### Change

The direct insert only created the chat row that the blob's provenance columns point at. `test_preview_reports_llm_authored_file_as_not_importable` now writes that row with `SessionServiceImpl.add_message` under a COMPOSE lease (the route production uses, and the same lease pattern as the `conftest` state helper), and the blob update uses the returned id. No `ReviewedWriter` entry was added. The `chat_messages_table` import became unused and was removed.

### Verification and control

- `test_static_direct_writers.py` and `test_shareable_reviews_routes.py` pass (part of `batch-a.log`).
- Control: with the committed direct insert put back, the gate fails with the original message (`nc3-static-writers-direct-insert.log`, exit 1); restored afterwards.

### The two maintainer-approved xfails

Identified in the tree as:

- `tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities`
- `tests/unit/elspeth_lints/test_allowlist_loader_unification.py::test_baseline_capture_is_self_consistent`

The failing test in this item is a different test from both. Neither xfail was edited. Both were XFAIL before any edit (`xfails-before.log`) and are XFAIL after all four items (`batch-b.log`). Item 3 does not affect either; item 4 changes their reason text, described next.

## Item 4: dead helper `_blob_id_uuid_validation_error`

### Zero callers

`grep -rn _blob_id_uuid_validation_error` over the whole tree (excluding `.venv`, `.git`, `.claude`, `node_modules`) finds the definition and prose in nine documents under `docs/architecture/`, no code. Control: the same grep for `_BLOB_ID_UUID_ERROR` over `src` and `tests` returns 13 lines.

### Change

`src/elspeth/web/composer/tools/blobs.py`: the function and its `@observation_boundary` decorator are deleted, and `observation_boundary` is dropped from the import (it had no other use in the file). `_BLOB_ID_UUID_ERROR` stays. The edit is one hunk near line 520 plus the import line, so it should merge cleanly with the unrelated change another branch is making near line 2160.

### Inventories

| Inventory | Effect |
| --- | --- |
| `config/cicd/enforce_tier_model/web.yaml`, `contracts-whitelist.yaml`, `masquerade_baseline.yaml` | No entry named the helper; nothing to update |
| Soft-mapping census (`scripts.check_contracts`) | Unchanged: exit 0, 2617 soft occurrences across 418 files, 60 boundary-parsed |
| CI trust-boundary rules (`trust_boundary.tests,scope,tier`, `--root src/elspeth`) | exit 0 |
| Keyless tier-model corpus | 3594 findings before, 3593 after |
| Lints fingerprint baseline (approved xfail) | Still XFAIL; reported total 4338 before, 4337 after |
| Sessions writer inventory (approved xfail) | Still XFAIL; drift grew, see below |

The tier-model set difference is confined to `web/composer/tools/blobs.py`: the helper's own `R_TB_SUPPRESSED` finding disappears, and seven findings (one `R6`, six `R_TB_SUPPRESSED`) take new fingerprints. None of the eight old fingerprints appears in `config/` or the lints baseline fixture (control: a fingerprint known to be allowlisted is found). The file's two signed allowlist entries (`_execute_update_blob`, R6 and R7) were already reported as stale before this change and are reported identically after it; their `ast_path` binding names module index 80, and the function was at index 111 before the deletion and is at 110 now. So this change adds no new signed-binding drift, and it does not repair the existing drift either.

### Drift added to the approved Sessions writer xfail

That test's reviewed read-connection inventory is keyed by line. Deleting 16 lines moves two sites:

- `_sync_list_blobs`: line 538 to 522
- `_sync_list_ready_blob_inline_descriptors`: line 563 to 547

The xfail reason text therefore changes: "Stale reviewed read connections" 2 to 4, "Connections outside exact contained authority" 30 to 32, "Unexpected/unreviewed" 106 to 108. The status is unchanged and the other 1653 tests in `batch-b.log` pass. The inventory was left alone because the instruction is not to touch that test; the same kind of line drift is already in its baseline (for example `cli.py:5043`). Updating the two line numbers is a one-line-each edit if the maintainer wants it.

## Commands and exit codes

| Check | Result | Log |
| --- | --- | --- |
| `test_web_reserve_reviewed_contracts.py`, `test_static_direct_writers.py`, `test_shareable_reviews_routes.py`, `test_async_worker_settlement.py`, `test_async_workers.py`, the three `test_required_executor_*.py`, all of `tests/unit/composer_mcp` (`-n 4`) | 716 passed, exit 0 | `batch-a.log` |
| The twelve `tests/unit/web/composer/test_*blob*.py`, `test_tool_result_envelope_gate.py`, `test_composer_rules.py`, `test_sessions_composer_attribute_contracts.py`, `test_masquerade_gate.py`, `test_allowlist_loader_unification.py`, `test_session_db_mutation_authority.py`, `test_check_contracts.py` (`-n 5`) | 1653 passed, 2 xfailed, exit 0 | `batch-b.log` |
| `python -m scripts.check_contracts` | exit 0 | `check_contracts.log` |
| `elspeth_lints.core.cli check --rules trust_boundary.tests,trust_boundary.scope,trust_boundary.tier --root src/elspeth` | exit 0 | `trust-boundary-after.log` |
| `ruff check` and `ruff format --check` on the four touched Python files | exit 0 | terminal |
| `mypy src/elspeth/web/composer/tools/blobs.py` (the test and helper files were not type-checked) | exit 0, no issues | `mypy.log` |
| `scripts/branch-safety-check.sh --intent commit` | FAIL=0 WARN=0 | terminal |

Both batches ran after the last edit and before the commits, so the committed content is what was tested. `proof-after.log` started before the item 4 deletion; the pytest node at the end of this document ran on the committed tree. The baseline before these changes was 21 failures in `test_web_reserve_reviewed_contracts.py` and 1 in `test_static_direct_writers.py` (sweep findings 1 and 2).

## Not run, left for CI

- The full `pytest tests/` suite and the PostgreSQL testcontainer selection. The changes are three test or helper files, one JSON pin and a dead-code deletion; the scoped runs above cover the gates whose inputs moved.
- The other two `landscape_long_run` proofs (not run to completion here).
- The frontend suite (nothing in it was touched).

## Other things noticed, not changed

- The nine documents under `docs/architecture/` that mention `_blob_id_uuid_validation_error` are dated assessments and reports; they now describe a function that no longer exists.
- `tests/unit/architecture/test_web_reserve_reviewed_contracts.py` had no test of the recovery producer contract before this change, which is how the second call site reached the long-running proof unnoticed.

## Official pytest node

`tests/unit/architecture/test_web_landscape_mutation_fencing.py::test_required_work_reserve_admission_on_actual_sources`, run with `-n 0` on the committed tree `b9c84a7a7` (HEAD was the same before and after the run): collected 1 item, **1 passed in 383.16s, exit 0** (`proof-node.log`). The test carries the `landscape_long_run` marker, which the default selection does not exclude; the log shows it was collected and ran.

## The rest of the file that imports the changed contract helper

`tests/helpers/web_recovery_producer_contract.py` is imported only by `tests/unit/architecture/test_web_landscape_mutation_fencing.py`, so the rest of that file was run to check the contract change against its synthetic and mutated-unit tests.

- The whole file at `-n 5` hit a 1750 s limit with 756 of 766 tests finished (`fencing-file.log`, exit 124); the remaining tests include the three `landscape_long_run` proofs.
- The file without the three `landscape_long_run` tests, `-n 5 -v`: **33 failed, 730 passed, exit 1** (`fencing-file-v.log`).
- The same selection on an extracted copy of the base commit `2f38b807b`: **33 failed, 730 passed, exit 1** (`fencing-file-v-base.log`). The two failure sets are identical, so this change adds and removes none.

Those 33 failures are on the branch before this work and are outside the four items. They are in 12 test functions, all about core Landscape fencing rather than the web reserve or recovery contract:

| Test function | Failing cases |
| --- | --- |
| `test_issued_deadline_registry_dependency_binding_rejects_semantic_mutations` | 16 |
| `test_cancelled_permit_internal_seat_edges_require_complete_fresh_authority` | 5 |
| `test_source_resolved_returned_authority` | 3 |
| `test_issued_deadline_registry_dependency_binding_requires_complete_source_but_ignores_comments`, `test_actual_lifecycle_release_calls_have_proven_authority`, `test_every_landscape_dml_transaction_is_full_token_fenced_first`, `test_every_landscape_mutation_api_requires_current_typed_authority`, `test_every_landscape_production_caller_forwards_exact_authority`, `test_finalization_complete_production_graph_and_writer_grants`, `test_finalization_current_writer_delegation_and_factory_returns`, `test_journal_outbox_extraction_requires_reviewed_body_event_order_and_closed_caller`, `test_reconciliation_establishment_owns_its_pre_admission_transaction` | 1 each |

The messages name pinned source of `src/elspeth/core/landscape/database.py` and findings such as "database helper executes before authority admission" in `src/elspeth/web/execution/`. They were not diagnosed here. Whether the 0.8.11 candidate fails them on its own was not measured.
