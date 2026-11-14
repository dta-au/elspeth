# The 33 Landscape fencing failures on the 0.8.12 branch: attribution, cause and repair

Branch `fix/composer-0812-gate-repair`. Nothing was pushed.

All 33 failures have one cause: a source-digest pin for `src/elspeth/core/landscape/database.py` that commit `f9c23c59c` (remediation side) did not refresh. The change behind it was reviewed and is sound for what the pin guards, the one value was re-pinned, and all 33 ids now pass. No production code changed. Nothing is left as a blocker.

## Attribution (measured)

The 12 affected test functions (55 cases, which include all 33 failing ids) were run on a `git archive` copy of each revision and on the worktree. Per-case outcomes come from `--junitxml`; the summariser refuses a revision in which any of the 12 functions is absent.

| Test function (failing cases on the branch) | merge-base `6825994b4` | 0.8.11 candidate `a67628c29` | remediation tip `25301aa93` | branch HEAD before repair | branch after repair |
| --- | --- | --- | --- | --- | --- |
| `test_issued_deadline_registry_dependency_binding_rejects_semantic_mutations` (16) | 16 pass | 16 pass | 16 fail | 16 fail | 16 pass |
| `test_cancelled_permit_internal_seat_edges_require_complete_fresh_authority` (5) | 5 pass | 5 pass | 5 fail | 5 fail | 5 pass |
| `test_source_resolved_returned_authority` (3) | 17 pass | 17 pass | 3 fail, 14 pass | 3 fail, 14 pass | 17 pass |
| `test_issued_deadline_registry_dependency_binding_requires_complete_source_but_ignores_comments` (1) | pass | pass | fail | fail | pass |
| `test_actual_lifecycle_release_calls_have_proven_authority` (1) | pass | pass | fail | fail | pass |
| `test_every_landscape_dml_transaction_is_full_token_fenced_first` (1) | pass | pass | fail | fail | pass |
| `test_every_landscape_mutation_api_requires_current_typed_authority` (1) | pass | pass | fail | fail | pass |
| `test_every_landscape_production_caller_forwards_exact_authority` (1) | pass | pass | fail | fail | pass |
| `test_finalization_complete_production_graph_and_writer_grants` (1) | pass | pass | fail | fail | pass |
| `test_finalization_current_writer_delegation_and_factory_returns` (1) | pass | pass | fail | fail | pass |
| `test_journal_outbox_extraction_requires_reviewed_body_event_order_and_closed_caller` (1) | 7 pass | 7 pass | 1 fail, 6 pass | 1 fail, 6 pass | 7 pass |
| `test_reconciliation_establishment_owns_its_pre_admission_transaction` (1) | 3 pass | 3 pass | 1 fail, 2 pass | 1 fail, 2 pass | 3 pass |
| **Total** | 55 passed, exit 0 | 55 passed, exit 0 | 33 failed, 22 passed, exit 1 | 33 failed, 22 passed, exit 1 | 55 passed, exit 0 |

- **The remediation commits introduced all 33.** The 0.8.11 candidate and the merge-base pass every case; the remediation tip fails the same 33 ids as the merged branch (the two failed-id lists are identical).
- **The merge added nothing.** The candidate side changed no file under `src/elspeth/core`, `src/elspeth/engine` or `src/elspeth/web/execution`, and did not touch the fencing test.
- **The commit is `f9c23c59c`** ("preserve schema metadata across database dialects"). The pinned digest of `database.py` matches the pin at `f9c23c59c^` and differs from `f9c23c59c` onward; the other three pinned modules match at every revision checked. This step is a digest computation over git objects, not a pytest run.

Instrument checks: each extracted tree was made its own git repository, because the tree gate drops gitignored files and the lane directory is gitignored. A plugin loaded into each pytest run recorded what it imported: `elspeth`, `elspeth_lints` and the test module resolved inside the tree under test, and the production-unit count equalled that tree's tracked `src/elspeth/*.py` count (985, 987, 993; 995 for the worktree). The positive assertions in these tests (for example exactly 13 proven deadline writers) passing on the merge-base show the scan was not empty. The extracted trees have been deleted.

## Cause

`_REVIEWED_REGISTRY_MODULES` in `tests/unit/architecture/test_web_landscape_mutation_fencing.py` pins a digest of the executable AST (docstrings removed) of four modules: `lease_deadlines.py`, `database_clock.py`, `journal.py` and `database.py`. It exists so that any executable change to deadline-guard installation, journal attachment or the database clock forces a review.

`f9c23c59c` added `_create_landscape_tables` to `database.py` and routed three `metadata.create_all` calls through it. The same commit re-pinned a different digest of the same file in the same test (`_NEW_LANDSCAPE_STAMP_SOURCES`) but missed this one.

One stale value produces 33 failures because the proof is shared:

- 17 cases assert the registry proof directly (the 16 mutation cases and the complete-source test).
- The other 16 depend on it: the deadline-writer and leader-deadline proofs return nothing when the registry is unproven, so every deadline issuance, seat release, cancelled-permit edge and returned-authority relay that rests on those writers is reported as unproven. Messages such as "database helper executes before authority admission" and "`materialize_cancelled_permit` has no explicit current Landscape token" are that cascade, not separate violations: they all clear with the single re-pin and no other edit.

## Review of the source change, and the repair

Read: the full diff of `f9c23c59c` for `database.py` and `src/elspeth/web/schema_probe.py`, the three construction paths in `database.py`, and the callers of the new helper. `f9c23c59c` is the only commit between the merge-base and HEAD that touches `database.py`.

Conclusion: correct for what this pin guards; no source change needed.

- **The diff is only the helper, its module lock and three call sites.** No line containing `install_deadline_guard` or the journal is in the diff.
- **Guard order is unchanged.** The three `install_deadline_guard` calls are where they were. In `LandscapeDB._setup_engine` and `LandscapeDB.from_url` the journal is attached first, then the guard is installed, and table creation runs after both.
- **The lock cannot deadlock on itself.** It is a non-reentrant `threading.Lock` held across DDL. The helper is called only from the three sites in `database.py` and from `init_landscape_schema` in `schema_probe.py`; nothing in `journal.py`, `lease_deadlines.py` or `database_clock.py` calls it, so no commit hook re-enters it.
- **The helper restores state on failure.** `_create_rule` values are captured before `create_all` and restored in `finally`, the same pattern `src/elspeth/web/sessions/schema.py` already uses.

Observation, not a defect: Landscape table creation is now serialised per process, including while PostgreSQL DDL waits on a database lock. That is the intended effect of the change.

Change: one value in `_REVIEWED_REGISTRY_MODULES`, `f95f6b92…864ce246` to `032c18d341b47fc96ea110e88b222201ea98ab12754cc1527220e71c2b2c661f`, with a provenance comment in the existing style. No other pin, inventory or test was edited.

## Negative controls

Each mutation was made on disk, two nodes were run with `-n 0` (the complete-source registry test and `test_every_landscape_mutation_api_requires_current_typed_authority`, one of the cascade tests), and the file was restored.

| Mutation | Result | Log |
| --- | --- | --- |
| None | 2 passed, exit 0 | `control-0-unmutated.log` |
| Comment appended to `database.py` | 2 passed, exit 0 | `control-A-comment-only-database.log` |
| In the new helper, `constraint._create_rule = rule` changed to `= None` | 2 failed, exit 1 | `control-B-executable-helper-edit.log` |
| Old pin value put back | 2 failed, exit 1 | `control-C-old-pin-restored.log` |
| All reverted (`database.py` clean against HEAD, test file byte-identical to the repaired version) | 2 passed, exit 0 | `control-Z-reverted.log` |

The 16 parametrised mutation cases, which each alter one of the pinned modules in memory and require the proof to refuse, also pass again.

## Commands and exit codes

Every pytest run used the worktree's interpreter with `PYTHONPATH=<tree>/src:<tree>/elspeth-lints/src`, the same two roots in `-o pythonpath=`, and `-p no:cacheprovider`; extracted trees also used `--rootdir <tree>`. One pytest process ran at a time, at `-n 6` or `-n 0`. Logs are in `.claude/lanes/fencing-diagnosis/` (local, gitignored).

| Check | Result | Log |
| --- | --- | --- |
| 12 functions on merge-base `6825994b4` | 55 passed, exit 0 | `base.log` |
| 12 functions on candidate `a67628c29` | 55 passed, exit 0 | `cand.log` |
| 12 functions on remediation tip `25301aa93` | 33 failed, 22 passed, exit 1 | `rem.log` |
| 12 functions on the worktree before the repair | 33 failed, 22 passed, exit 1 | `head-before.log` |
| 12 functions on the worktree after the repair | 55 passed, exit 0 | `head-after.log` |
| `test_web_reserve_reviewed_contracts.py`, `test_static_direct_writers.py`, `test_masquerade_gate.py` and the two approved xfails | 431 passed, 2 xfailed, exit 0 | `final-modules.log` |
| Whole fencing file without the three `landscape_long_run` cases (763 cases) plus `test_sink_effect_reserve_contract.py` (21), on the committed re-pin `f44d7809d` | 784 passed, exit 0 | `rest-of-file.log` |
| `test_web_landscape_mutation_fencing.py::test_required_work_reserve_admission_on_actual_sources`, `-n 0`, same commit | 1 passed in 389 s, exit 0 | `reserve-proof-node.log` |
| `python -m scripts.check_contracts` | exit 0 (census unchanged: 2617 soft occurrences, 418 files, 60 boundary-parsed) | `check_contracts.log` |
| `ruff check` and `ruff format --check` on the touched test file | exit 0 | terminal |
| `mypy` on the touched test file | exit 1: 222 errors in 4 test files, none on the changed lines | `mypy-test-file.log` |

The re-pin turns the registry proof from false to true for every test that consults it, not only the 12 that were failing, so the rest of the file was re-run after the commit: all 763 non-long-run cases pass, as do the modules that import the fencing module's proofs (`test_web_reserve_reviewed_contracts.py`, `test_sink_effect_reserve_contract.py`). The reserve-admission proof that the gate repair certified was re-measured for the same reason and still passes. HEAD was `c3b733113` before and after these two runs; its only difference from `f44d7809d` is this report.

The `mypy` result is not a gate result. Tests are outside the type-checked surface (`src/` and `elspeth-lints/src/`), the errors are in code this work did not touch (161 in the fencing test, the rest in three files it imports), and the change is a comment and one string value in a `dict` of strings.

The runner for the attribution rows, per tree, with the 12 functions passed as `file::function` node ids:

```bash
cd <tree> && PYTHONPATH=<tree>/src:<tree>/elspeth-lints/src:<lane>/tools \
  <worktree>/.venv/bin/python -m pytest <12 node ids> \
  --rootdir <tree> -c <tree>/pyproject.toml -o "pythonpath=<tree>/src <tree>/elspeth-lints/src" \
  -p prov_plugin -p no:cacheprovider -n 6 -rA --junitxml <lane>/<label>.xml > <lane>/<label>.log 2>&1
```

`prov_plugin` is the lane-private plugin that records the import paths and unit count. The scripts are `tools/run_rev.sh`, `tools/extract.sh`, `tools/outcomes.py` and `tools/controls.sh` in the lane directory.

The two maintainer-approved xfails (`test_all_production_sessions_writers_are_reviewed_typed_authorities` and `test_baseline_capture_is_self_consistent`) are still XFAIL, and their reason text is identical to the previous agent's `batch-b.log`. Neither was edited.

## Not run

- The other two `landscape_long_run` proofs in the fencing file. They have not been run to completion on this branch, before or after this change.
- `mypy` on the gated surface: no file in it changed.
- The full `pytest tests/` suite and the PostgreSQL testcontainer selection.

## Blockers

None from these 33 failures.
