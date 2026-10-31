F4 implemented, uncommitted on `codex/0.8.10-exceptions`.

Files changed:

- `src/elspeth/web/composer/advisor_checkpoint.py`: retain nominal admission failure before generic `TimeoutError` via `_AdvisorFirstPartyFailure`.
- `src/elspeth/web/composer/tool_batch.py`: use the existing closed service family, carrier and `SERVICE_FAILURE` disposition. Its runtime tuple is shared with the existing session-tool catch; no inheritance, family, carrier or deadline change.
- `tests/unit/web/composer/test_service_failure_dispatch.py`: add the R/P cases below.

New test IDs in that module:

- `test_hint_admission_reaches_operation_owner[compose-limited]`
- `test_hint_admission_reaches_operation_owner[advisor-limited]`
- `test_plain_hint_timeout_remains_recoverable_with_original_deadline`

R reaches real hint preparation and the injected `get_session` refusal with non-null secret inventory, owned graph and live authority. It proves exact exception/cause retention through P4, operation projection `(503, executor_unavailable)`, no Advisor SDK call/feedback, and no later edit. P reaches the audited Advisor seam twice, preserves recoverable `ADVISOR_ERROR`, permits independent edits and retains the entry deadline across batches.

Failing-first: `red.log`, pytest exit **1**, **2 failed / 1 passed**, unchanged production at HEAD `3035bfeebc43f6ad2c8bacf5c1d547e3fbcf11ac`. Advisor/batch blobs respectively: `0e41b3f4513b563a6f37be9bc7f48573a6895b95`, `60c2e53cf6d6e278c7bd2a00e0d3faea8c26fd24`. Actual red:

`AssertionError: hint admission must retain the original service failure for the operation owner`

`assert None is AsyncWorkerAdmissionTimeoutError('private hint admission refusal')`

Mutations, all reverted; each pytest exit **1**:

- Catch admission below `TimeoutError`: both R cases fail that assertion (`mutation-order-final.log`).
- Restore generic first-party crash treatment: both R cases fail there (`mutation-crash-final.log`).
- Capture plain timeouts as first-party failures: P raises original `TimeoutError` (`mutation-broad-timeout.log`).
- Refresh batch deadline: P fails `hint timeout recovery must keep the original compose deadline` (`mutation-deadline.log`).

Validation: **25 focused tests passed**, exit **0**, serial, frozen sources (`focused.log`), including unchanged ValueError/conflict/preflight neighbors and selected Advisor controls. After the final test-only exception-observation adjustment, both R mutations were repeated and **3 new cases passed**, exit **0** (`final-new-tests.log`). Final production blobs match the 25-test run. Focused mypy (silent imports), Ruff, formatting and diff checks passed.

Expected reviewer pin changes:

- `test_web_landscape_mutation_fencing.py::_reserve_BATCH_HELPER_SOURCE_SHA256["_execute_continuation_call_live"]`
- `scripts/cicd/composer_advisor_binding_templates.py::TEMPLATES["batch._execute_continuation_call_live"]`

No pins/baselines/signatures/approved xfails/docs edited. Controlled raw finding-set comparison: `tier-focused-raw-final.json`. The required nominal `isinstance(first_party_exc, _SERVICE_FAILURE_TYPES)` adds one R5 site, left explicit for adjudication; the existing R6 catch changes spelling, alongside fingerprint rebinding. No new advisor/test finding patterns. The core lint CLI ignored `--files` and produced a tip-only whole-tree scan, exit 1 (`tier-tip.json`); no whole-tree base comparison or signature clearance claimed.

Full suite, testcontainers and the excluded architecture test were not run, as instructed; reviewer owns whole-tree qualification and pin derivation. No commit: `.git` is read-only. The prolonged startup work-at-risk census was interrupted to limit loaded-host work. All evidence files are beside this note.
