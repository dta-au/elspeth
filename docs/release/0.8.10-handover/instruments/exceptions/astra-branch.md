Reviewed `d7ad18596..c6a4e3f9f`. Four findings; no blocker established. Static review only, with read-only AST/hash checks. No source changes or tests run.

1. **Major — P4 failure loses the unpersisted tool-evidence suffix.**
   [service.py:3186](.claude/worktrees/exceptions-20261025/src/elspeth/web/composer/service.py:3186), also the thrown-publication branch at [service.py:2985](.claude/worktrees/exceptions-20261025/src/elspeth/web/composer/service.py:2985).

   After a successful tool followed by a service refusal, failed P4 publication retains the exceptions, LLM calls and failure counts—but no owned carrier retains the unpersisted tool invocations. The outer [cleanup drains only LLM calls](.claude/worktrees/exceptions-20261025/src/elspeth/web/sessions/composer_turn.py:1920), so it cannot recover those tool audit records. This violates F5’s executed-prefix/evidence preservation requirement. The new publication-retention test verifies rollback and exception identity, then stops before recovery.

   **Fix:** retain the current unpersisted invocation suffix and publication disposition in an owned evidence carrier consumed by failure cleanup. Test actual recovery after both P4 failure exits, including an earlier committed turn, and assert exactly-once draining.

2. **Minor — required architecture guards were not updated for the changed contracts.**
   [tool_batch.py:1019](.claude/worktrees/exceptions-20261025/src/elspeth/web/composer/tool_batch.py:1019), [reviewed-full-source-guards.json:26](.claude/worktrees/exceptions-20261025/tests/helpers/required_work_reserve_recipes/reviewed-full-source-guards.json:26).

   The exact carrier model omits `service_failure`; both F1 source guards retain their old hashes. Read-only comparisons returned:

   ```text
                                      base match   HEAD match
   ContinuationBatchState             True         False
   composer_operation_errors.py       True         False
   composer_operations.py             True         False
   ```

   Positive and in-memory mutation controls passed. The prohibited architecture test was neither imported nor executed.

   **Fix:** review and update these precise guards and their negative controls. Do not blanket-regenerate unrelated existing mismatches.

3. **Minor — primary rejection responses omit an available diagnostic reference.**
   [provider_gateway.py:755](.claude/worktrees/exceptions-20261025/src/elspeth/web/composer/provider_gateway.py:755), also line 814.

   Both wrappers retain the classifier’s `diagnostic_reference=None`. When audited provider custody supplies the actual call ID, required audit subsequently stores corresponding `provider-failure` evidence, but neither route binds that reference into the descriptor. F6 explicitly requires preserving available call-derived references. [The route test currently pins the omission](.claude/worktrees/exceptions-20261025/tests/unit/web/composer/test_primary_rejection_routes.py:88).

   **Fix:** enrich the descriptor once the exact failed attempt’s audit identity exists, following the Advisor’s existing binding pattern. Assert that both HTTP/UI routes expose the stored evidence reference; retain `None` when no identity exists.

4. **Minor — restored-Stop preservation test can pass without restoring Stop.**
   [composerOperationObserver.faults.test.ts:138](.claude/worktrees/exceptions-20261025/src/elspeth/web/frontend/src/api/composerOperationObserver.faults.test.ts:138).

   The test checks cancellation targets using `calls.every(...)`, without requiring cancellation after observation is restored. Forgetting persisted Stop during observer construction still leaves the initial cancellation correctly targeted, while scripted GETs independently reach completion. Its assertions therefore do not protect the stated restoration guarantee.

   **Fix:** make the initial cancellation return missing, require a subsequent cancellation from restored observation, then release the authoritative terminal response.

Checked and found sound:

- **F1:** no defect found in executor/database classification, equal-priority ordering, history propagation or U-29 preservation.
- **U-19 production:** no fault-ownership defect found; exact causes, pending custody, cleanup and value-free foreground/background disposition are preserved.
- **F4/F5:** carrier copyback, serialization refusal, batch stopping and EARLY suppression are present. No authority bypass found.
- **F6 otherwise:** SDK causes, debug-independent safe evidence, closed decoding and both route-specific renderers are preserved.
- **F7/invariants:** no additional defect found in the reviewed cancellation/settlement assertions; no server-authored pipeline structure, tutorial-special path or structural Protocol dispatch introduced.
