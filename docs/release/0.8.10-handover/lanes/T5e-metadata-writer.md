# T5e: composition-state metadata writer invariant (assistant lane)

> **RESUME TASK 0 (hung tests) is the release's FIRST priority, owned by u45-firstcompose plus one coordinator-assigned owner. It is NOT this lane's job.** See `.claude/lanes/release-0.8.10/RESUME-FIRST.md`. This lane resumes its own unit, under the guardrails: one pytest at a time, `-n 3` max, `timeout 2400`, both hang files `--ignore`d, no directory runs without a slot. Do not touch the fencing gate, the reserve-contract tests or the pytest config. Maintainer's order, 2026-10-25.


Report body still owed: write it at landing. The required citations are listed under "Landing conditions still owed" below.

## COLD START (paused 2026-10-25)

### Unit, branch, trees
- Unit **T5e**: writers accepted what the reader refuses (a None `metadata_` on composition states). The approved scope is (b)/(ii).
- Branch `codex/0.8.10-t5e-metadata-writer` @ **6c22638f32e4e5563261c61b8bb246bdc1c78da7**. It is one squashed commit, 78 files, not pushed.
- Worktree `.claude/worktrees/t5e`. Lane logs are in `.claude/lanes/t5e/` inside that worktree.
- Base **dcb742ecc**. The base worktree, `.claude/worktrees/t5e-base`, sits at a detached HEAD on dcb742ecc and is clean.
- `git status --short` was empty in both trees at the pause. No WIP commits were needed.

### What 6c22638f3 contains
- `CompositionStateData.metadata_` is required (kw_only). `CompositionStateRecord.metadata_` is non-optional. Both `__post_init__`s run the one rule, `require_stored_state_metadata` (in `web/sessions/protocol.py`).
- The same rule sits in `converters.state_from_record` and runs before each writer's composition_states insert:
  - `service._insert_composition_state`;
  - `repository` `append_state`, `_create_pending` and `_ForkChildSessionMutations.insert_child_state`.
- `HistoricalCheckpointData.metadata_` is required.
- `composition_states.metadata_` is NOT NULL (`sessions/models.py:541`). It is folded into epoch 79, with no migration.
- Dead None branches removed: `routes/composer/state.py` and `routes/_helpers.py::_state_response`.
- Guard pins in `tests/helpers/required_work_reserve_recipes/reviewed-full-source-guards.json`:
  - **service.py re-pinned to 57be012e…** (it had been stale since my 582cc93c2; I verified the formula against the d3d9bf308 pin first);
  - protocol.py re-pinned 30f257a8… → 7d50310a….
  - The formula is `sha256(source.encode("utf-8"))`.
- New `tests/helpers/composition_state.py`, the shared builder.
- New `tests/unit/web/sessions/test_composition_state_metadata_invariant.py`, with 12 tests:
  - the refusal at construction and decode;
  - both writers refusing with zero rows stored;
  - a structural pin that 7 functions each call the rule exactly once.
- Sweep: 188 DTO constructions in 37 files; 49 raw inserts given stored metadata; 6 `metadata_=None` fixtures.

### Done and verified (evidence in `.claude/lanes/t5e/`)
- The writer-only mutant turns both writer tests red (`mutant.log`, `mutant-tests.log`).
- The invariant file passes 12/12. routes_state passes 88/88 (`routes-state.log`).
- Schema files: 592/593 (`schema2.log`). The one failure was a `table.insert()` fixture, swept since, but **not re-run at 6c22638f3**.
- `mypy src` is clean (`mypy-src.log`). check_contracts exits 0 (`contracts.log`).
- **Keyless trust-tier set-diff, base vs tip, is EMPTY** (`ttdiff.txt`; script `ttdiff.py`; scans `tt2-{base,tip}.json`, both exit 1, 3766 findings each).
  - The diff is a multiset over (rule_id, file_path, message). The line column is excluded; the worktree root is normalised to `<ROOT>` in file_path and message.
  - Control: a planted finding (`tt2-ctrl.json`) shows as ADDED.

### Interrupted, NOT evidence
- `web-full.log` (`tests/unit/web tests/integration/web -n 4`): I stopped it at about 77% for the pause. It showed about 88 F with **no base run**, so nothing is attributed. Load was 74 at the time; earlier the e2e sink_effect ids were measured as load flakes.
- `arch.log` and `reserve.log` hung and were killed by the coordinator. Both are on the hang list.
- `reserve-x.log`, `-n 0 -x` on `test_web_reserve_reviewed_contracts.py`: `test_retained_failure_transfer_receipts_identify_exact_actual_four_calls` fails with "retained/private transfer namespace changed src/elspeth/web/composer/audit_storage.py". T5e does not touch audit_storage.py (`git diff --stat dcb742ecc HEAD -- …audit_storage.py` is empty). It is unattributed; the base run was stopped. This file is now --ignored for every lane except U-46.
- The approved-xfail check (`test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities` should still be XFAIL) is **not yet confirmed** at 6c22638f3.

### RESUME ORDER: do this first (coordinator, 2026-10-25, during the pause)
- **The target is red.** carve-partition measured 2 failing ids in `test_session_db_mutation_authority.py`: `composer_preferences_facets...` and `receipt_bound_state_revert...`.
  - Cause: all 38 `sessions/service.py` pins are +3 lines off. My 582cc93c2 added 3 lines at service.py:2303, and the later re-pins were derived without them.
- The fix is carve's **506561cd4** on `codex/0.8.10-isinstance-pairs`. It re-derives the pins with `scan_production_writers` and leaves the approved xfail unchanged. **It lands FIRST.**
- Then **rebase T5e onto the post-506561cd4 target** and **re-derive T5e's own service.py and protocol.py pins with the tools** (scan_production_writers / the guard derivation), because T5e also edits service.py. Do not carry the hand-computed values above (57be012e…, 7d50310a…) over the rebase. The mutation-authority line pins also move.
- Only after that, run the verification below against the rebased tip and the new base.

### Next step (one pytest at a time per lane, `-n 3` at most, `timeout 2400`, ask the coordinator for a slot for directory runs, a log idle for 15 min means hung)
```bash
W=.claude/worktrees/t5e; L=$W/.claude/lanes/t5e; cd $W
H1=tests/unit/architecture/test_web_landscape_mutation_fencing.py
H2=tests/unit/architecture/test_web_reserve_reviewed_contracts.py
# 1. approved xfail + session-db mutation authority (focused)
timeout 2400 env PYTHONPATH=$W/src:$W/elspeth-lints/src .venv/bin/python -m pytest -o "pythonpath=$W/src $W/elspeth-lints/src" -n 0 -p no:cacheprovider -rxXf \
  tests/unit/architecture/test_session_db_mutation_authority.py > $L/sdma.log 2>&1; echo "exit=$?"
# 2. reviewed full-source guard tests (ask coordinator which ids, since the fencing file that verifies pins is the KNOWN HANG)
# 3. web-full (coordinator slot), tip then base (t5e-base), sequentially, --ignore both hang files, --junitxml, then set-diff (id, signature)
timeout 2400 env PYTHONPATH=$W/src:$W/elspeth-lints/src .venv/bin/python -m pytest -o "pythonpath=$W/src $W/elspeth-lints/src" -n 3 -p no:cacheprovider \
  --junitxml=$L/web-full.xml --ignore=$H1 --ignore=$H2 tests/unit/web tests/integration/web > $L/web-full.log 2>&1; echo "exit=$?" > $L/web-full.exit
# 4. testcontainer at -n 0 (PostgreSQL): session schema, composition-state, and the 7 swept testcontainer files (list: $L/sweep.txt / raw-sweep2.txt)
```
Then write this report's body, run `scripts/branch-safety-check.sh --intent merge`, and message the coordinator with the tip and the evidence.

### Landing conditions still owed
- The landing rule: tip failing (id, signature) pairs must be a subset of base's, and every new id must pass. Use web-full on tip and base.
- The testcontainer files at -n 0.
- The approved xfail is still XFAIL.
- The report body must cite:
  - `models.py:74` SESSION_SCHEMA_EPOCH=79;
  - `schema.py:340` user_version refusal;
  - `schema.py ~375` schema_identity_mismatch;
  - `core/schema_shape.py:972-979`, the shape validator that compares nullability, which is why NOT NULL needs the epoch;
  - "no upgrade note needed beyond the existing epoch 78→79 line";
  - the epoch-79 refusal as the reason "historical minimal checkpoint" rows can't exist.

### Hazards
- KNOWN HANG files: `test_web_landscape_mutation_fencing.py` (the only verifier of guard pins) and `test_web_reserve_reviewed_contracts.py` (on overlay trees).
- Guard pins elsewhere in the json may be stale. Only re-pin the files T5e edits.
- The process-death, lease and e2e ids are load-flaky. Re-run them at -n 0 and attribute against the base.
- A bare `pytest` imports the MAIN checkout. Always set PYTHONPATH and `-o pythonpath` (space-separated).
- `.claude/lanes/t5e/test_hole_probe.py` is a scratch probe, not source.

### Next units (after T5e lands)
- **T5f, approved**: DB CHECKs on `runs`:
  - `status IN ('completed','completed_with_failures','empty') ⇒ landscape_run_id IS NOT NULL`;
  - `status='failed' ⇒ error IS NOT NULL`.

  Order:
  1. A measured writer inventory, then a runtime (status, column) recorder over the runs/execution suites as the second check.
  2. Red-first DB tests on SQLite AND PostgreSQL.
  3. The CHECKs in the epoch-79 definition. First measure that no epoch-79 store exists; if one does, bump to 80.
  4. Fix any writer that can produce the NULL, failing-first, in the same unit.

  Own branch off the post-T5e target. Report: `.claude/lanes/release-0.8.10/T5f-terminal-run-checks.md`.
  - The static inventory has started (`.claude/lanes/t5e/t5f-inventory.txt`). It found 12 runs write statements, with the control `run_start_permit_authority.py:137` found and no runs_table aliases.
  - Still to do: extract (status, column) per site, including the dict/`**kwargs` forms in `repository.transition_run_status` (:1880), the generic transition the execution routes use.
- **`_StrictResponse` audit**: unassigned, queued after T5e (coordinator). Not started.

### Open questions / rulings owed
- From the coordinator: which reviewed full-source guard test ids count as T5e landing evidence while the fencing file is a KNOWN HANG.
