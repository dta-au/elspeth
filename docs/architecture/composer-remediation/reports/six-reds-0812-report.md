# Six reds on the 0.8.12 remediation branch

Worktree `composer-0812-six`, branch `fix/composer-0812-six-reds`.

## Attribution (measured)

The three test files were run with `-n 0` on `git archive` extracts, 34 tests each:

| Tree | Result |
|---|---|
| a67628c29 (origin/release/0.8.11-candidate) | 34 passed |
| 6825994b4 (merge-base of the two) | 34 passed |
| 25301aa93 (remediation tip before the candidate merge) | 6 failed, 28 passed |

The reds come from the remediation side. None of the three test files changed
between the merge-base and 25301aa93 (`git diff --stat` empty), so production
moved under them. `-n 0` does not clear them: they are deterministic, not an
order or parallelism flake.

## Item 1: three failure-validation-guidance tests (production wrong, fixed)

Introduced by 1bc91710f "enforce configured MCP paths at admission". It added
`is_local_path_value` to `_validate_source_path` ahead of the existing
`data_dir is None and not require_data_dir -> return None` escape. A call with
no confinement in force (no `data_dir`, no MCP root authority, not required)
therefore got `source_path_policy_invalid` for `path: 123`, with Web prose
telling the model to "use the session's blob binding". Before, plugin option
validation caught it as `plugin_options_invalid` and attached the catalogued
repair text. The tests' intent is that the model receives that text.

Fix, in `src/elspeth/web/composer/tools/_common.py`: return early when no
confinement applies, before the lexical gate. No confinement is relaxed. The
MCP-root, Web-`data_dir` and require-`data_dir` routes still run the lexical
gate and the containment checks. The unconfined route never confined anything,
and plugin validation still rejects the malformed value.

Related site, not changed: `_validate_transform_provider_config_path` has the
same ordering (lexical check before the `data_dir is None` skip), so a non-str
`provider_config.persist_directory` with no `data_dir` is now refused where it
used to pass through. No test in my set pins it. Worth a follow-up decision.

## Item 2: checkpoint no-return `[True]` (test stale, intended change)

Introduced by dc67bdd2f "own standalone MCP recovery through process
settlement". `_release_generation_recovery` deliberately keeps a generation that
`escalated` (or has an observer or drain error) in `_RECOVERING_GENERATIONS`,
and `shutdown_async_workers` now raises
`RequiredGenerationUnavailable("Required generation recovery escalated")` for
it. The `expire_generation=True` case forces escalation (the drain deadline
expires and `instance_draining` is set), so the test's cleanup
`await shutdown_async_workers()` now raises. The new behaviour is the commit's
purpose (the comment in `required_executor.py` says failed ownership remains
for shutdown). The test's assertions on the receipt, the retained projection
and the absence of persisted rows are unchanged. The cleanup now asserts the
escalation with `pytest.raises` in the expired case and a clean shutdown in the
other. The `[False]` variant stays green.

## Items 3 and 4: two source-path dispatch-boundary tests (test stale, intended change)

Introduced by 1bc91710f. `_path_policy_refusal_message` now gives
`Local file paths require configured data root authority and must remain within
the configured data root (--data-dir).` when `data_dir is None` and
`require_data_dir_for_paths` is true. That is exactly what both tests construct.
It is the standalone MCP route (server.py passes `data_dir=None` and
`require_data_dir_for_paths=True`). Only this message changed. The code
(`source_path_policy_invalid`), the rejected component, the unchanged state and
the absence of the path in the message are all still asserted. Confinement is
not relaxed: it refuses as before. Real Web routes pass a real `data_dir` and
keep the blob-binding wording.

## Verification

- The three files with `-n 0` in this worktree: 34 passed (exit 0).
- tests/unit/web/composer, tests/unit/composer_mcp, test_paths and
  test_async_worker_settlement with `-n 4`: 13993 passed, 1 failed:
  `test_portable_supervisor.py::test_healthy_child_fails_closed_when_supervisor_is_killed`,
  a process-kill test that passes with `-n 0` (66 passed) here and on the 25301aa93
  extract, so a parallel-load flake, not this change. The sessions
  static-direct-writers test was not in the selection.
- ruff check, ruff format --check and mypy on `_common.py`: clean.
- `python -m scripts.check_contracts`: exit 1, a soft-mapping census drift
  (2617 pinned, 2618 live) in `src/elspeth/web/composer/pipeline_planner.py`.
  Not from this change: the same check exits 0 on the 25301aa93 extract, and
  the planner file changed in 5af93d3ad and b0b96cf00 on the landing branch. It
  needs a re-pin or an owned type from whoever owns those commits.
