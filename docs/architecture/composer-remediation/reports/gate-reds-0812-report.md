# Gate reds on the 0.8.12 composer branch: measurement report

Branch fix/composer-0812-gate-reds, started at 586a91d68. No source or pin change was needed.

## B: writer-recipe pin (protocol.py:module-bindings)
- Ten named ids plus the rest of tests/unit/architecture/test_web_reserve_reviewed_contracts.py and
  test_required_work_reserve_admission_on_actual_sources, one run at -n 4: 177 passed, exit 0 (414 s).
- Already repaired by 0d6b17115 (merged in 586a91d68). No further pin refresh, so no new review owed.

## A: `.reserve` unknown mutation receiver (service.py:9097/9129)
- Passes on HEAD (586a91d68): -n 0, 1 passed, exit 0.
- The `.reserve` call sites originate in 1dec423d3 (git log -S); they are not new.
- Also passes, on git archive extracts: 2caf10780 (the commit the red run used), 0d6b17115^ (pre pin
  refresh) and a67628c29 (release/0.8.11-candidate, an ancestor of HEAD). 25301aa93 was not run.
- The red run's summary.txt says `frozen=NO — the tree moved during the run; this run is not evidence`
  (tree hash changed at 03:32). Between 2caf10780 and HEAD only the pin file changed among gate inputs.
- Conclusion: not reproducible at any frozen commit; no unfenced production path found; the red is not
  attributable to a commit. The gate's `reserve_proof` already admits these sites. I changed nothing and
  did not add an inventory entry, because there is nothing to justify one. If it recurs in a frozen run,
  capture it with the `reserve_proof` state.
- Caveat: the test took 582 s in the worktree on the first run but ~5 s in extracts. I did not
  determine why (likely cold cache or load); the pass is real in both cases (exit 0).

## Full tests/unit/architecture at -n 4
- exit=0, `2012 passed, 1 xfailed` in 2259 s. Zero failures, including A and B.
- The one xfail is tests/unit/architecture/test_session_db_mutation_authority.py (approved; unchanged).
  The other approved xfail (lints baseline self-consistency) is outside this directory and was not run.
- Not run: ruff, mypy and check_contracts are moot (no source or test change).
