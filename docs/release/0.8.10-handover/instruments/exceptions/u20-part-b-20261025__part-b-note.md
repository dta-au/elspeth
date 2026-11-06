U-20 part B implemented; uncommitted for reviewer on `codex/0.8.10-exceptions`, HEAD `748d548db7f7a3fd27b9f82670b957b85590bcd4`.

Files changed:

- `src/elspeth/web/composer/service.py`: join a thrown P4 failure with the exact retained service original; turn returned unwind unavailability into an integrity-controlling companion carrying P4's failed-turn metadata. Existing causes, successful sole-service identity, and independent companions remain intact. F4 is untouched.
- `tests/unit/web/composer/test_service_failure_publication_retention.py`: `test_service_failure_retains_p4_publication_failure` with IDs `[sole-returned-unavailability]`, `[sole-thrown-integrity]`, `[concurrent-returned-unavailability]`, `[concurrent-thrown-integrity]`.

Qualifying red: `red-valid.log`, exit **1**, **4 failed** on unchanged production source. `service.py` SHA-256: `534e40f687793a868f4f42fcd8633c77ca2a201b166ecc81ad2b54772e730708`; complete identities in `red-valid-source.txt`. Actual assertions:

- Returned: `AssertionError: required P4 failure must control integrity`; `assert (503, 'database_unavailable') == (500, 'audit_integrity_error')`.
- Thrown: `AssertionError: P4 failure must retain the exact service original`; `assert False`.

Preliminary runs (`red.log`, `red-confirmed.log`) exposed an incorrect test expectation and missing basetemp parent; neither counts as regression evidence. The corrected metadata expectation is three issued calls, two executed outcomes, zero committed responses after the injected rollback.

Mutation results (each reverted; each exit **1**, **2 failed**):

- `mutation-returned`: omit the new unavailable-integrity outcome → both returned variants fail integrity control.
- `mutation-no-join`: rethrow publication without the join → both thrown variants lose service identity.
- `mutation-only-publication`: keep only publication → both thrown variants lose service identity.
- `mutation-only-service`: keep only service → both thrown variants fail integrity control.

The tests reach the real publisher and its SQL disposition, inject after real inserts, verify rollback by authoritative readback, and exercise a successful-publication neighbor. Concurrent cases retain cancellation and thrown settlement evidence without cycles. The existing effect-verification preservation control also passes.

Final focused run: **9 passed in 32.20s**, exit **0** (`final.log`, `final.exit`): the new file plus `test_service_failure_publication.py` and `test_service_failure_live_evidence.py`. All asynchronous work settled; source hashes matched before/after (`final-source-before.txt`). Ruff check, Ruff format check, and `git diff --check` exit **0**.

Expected pin movement: the reviewed `_advance_live_compose_iteration` source digest in `test_web_landscape_mutation_fencing.py::_reserve_compose_iteration_state_receipt` (around line 16089). Its source hash moved `b170469ac421cf1cdc38b6f25502bbc8d7889a446affaaaee94b2dafab64fb1f` → `0534b47d4f9a0af44ef735d7eb913d49783f734034b013bce9f592544eb2c85a`; `_compose_loop` was the unchanged extraction control (`pin-source-evidence.txt`). No pins, baselines, signatures, or approved xfails were edited.

No commit: `.git` is read-only and reviewer owns commits. No full suite, testcontainer suite, fencing gate, or whole-tree pin regeneration was run, as instructed. No `uv` or docs files were created.

The required handover `work-at-risk.sh` census was interrupted (process exit **130**) after it stalled before reporting any worktree rows, to limit host load. No census conclusions are claimed.
