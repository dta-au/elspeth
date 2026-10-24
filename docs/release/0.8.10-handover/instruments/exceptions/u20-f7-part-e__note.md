F7 preservation controls (P), tests only. Branch `codex/0.8.10-exceptions`, HEAD `ef4096ebb0e3ed296643bc01cda707e9196b5b74`. No baseline defect was established and no production repair was made. All reported reds below are named behavioral mutations, subsequently reverted.

Files changed:

- `tests/unit/web/composer/test_advisor_tool.py`: extend the existing Advisor control with cost/guard refusals and a real recoverable provider neighbor.
- `tests/unit/web/composer/test_f7_tool_preservation.py`: add the missing started-stage cancellation/closure control and successful neighbor.
- `tests/unit/web/composer/test_provider_quota.py`: extend settlement coverage; add body-plus-audit and body-plus-cost controls.
- `tests/unit/web/composer/test_compose_loop_persistence.py`: extend existing cancellation and committed-prefix controls.
- `tests/unit/web/sessions/test_run_composer_turn.py`: extend existing completed-precedence control through repeated cancellation, audit drain and the actual worker settlement owner.

Test IDs used below (append each bracket suffix literally to its path/function; [final-focused.log](final-focused.log) lists all 16 complete collected node IDs):

- A: `tests/unit/web/composer/test_advisor_tool.py::test_first_party_failure_in_the_advisor_path_is_not_reported_as_a_provider_outage`, suffixes `[first_party]`, `[cost]`, `[guard]`, `[provider]`.
- T: `tests/unit/web/composer/test_f7_tool_preservation.py::test_started_tool_stage_closes_cancellation_once_and_rethrows_the_original`.
- Q1: `tests/unit/web/composer/test_provider_quota.py::test_required_provider_audit_preserves_body_and_construction_failure`, suffixes `[body-error]`, `[body-cancellation]`.
- Q2: `tests/unit/web/composer/test_provider_quota.py::test_cost_charge_does_not_precede_failed_required_settlement`, suffixes `[settlement-only]`, `[body-and-settlement]`.
- Q3: `tests/unit/web/composer/test_provider_quota.py::test_provider_body_and_cost_refusal_follow_actual_settlement`, suffixes `[cost-only]`, `[body-and-cost]`.
- C: `tests/unit/web/composer/test_compose_loop_persistence.py::test_cancellation_during_sync_tool_waits_for_result_audit_persist`, suffixes `[completed-neighbor]`, `[repeated-cancellation]`.
- D: `tests/unit/web/composer/test_compose_loop_persistence.py::test_unwind_failure_retains_only_current_turn_after_committed_prefix`.
- S: `tests/unit/web/sessions/test_run_composer_turn.py::test_marker_delivered_while_the_settlement_child_runs_is_deferred_and_completed_wins`, suffixes `[completed-neighbor]`, `[repeated-cancellation]`.

Every control was exercised against its named mutation:

| Control / test IDs | Mutation, process exit and actual red assertion |
| --- | --- |
| First-party Advisor / A[first_party], recoverable A[provider] | Return recoverable Advisor feedback instead of the original failure. Exit 1: `Failed: DID NOT RAISE <class 'elspeth.contracts.errors.AuditIntegrityError'>`. Provider neighbor passed. |
| Started-tool cancellation / T | Omit cancellation closure: exit 1, `assert 0 == 1` at `assert len(recorder.invocations) == 1`. Swallow cancellation: exit 1, `Failed: DID NOT RAISE <class 'asyncio.exceptions.CancelledError'>`. Both repeated on the final test body. |
| Body plus audit construction / Q1[body-error], Q1[body-cancellation] | Retain only the audit error. Exit 1, both cases fail: `assert isinstance(caught.value, BaseExceptionGroup)` / `AssertionError: assert False`; actual outcome is the audit error alone. Successful audit and audit-only neighbors run on baseline. |
| Body plus settlement/cost / Q2, Q3 | Raise only settlement failure: exit 1, body-and-settlement fails `assert isinstance(caught.value, ExceptionGroup)` / `AssertionError: assert False`; settlement-only neighbor passes. Raise only cost refusal: same assertion/exit for body-and-cost; cost-only neighbor passes. Skip settlement on body failure: exit 1, both body cases fail (missing settled-output integrity outcome / `assert len(refusals) == 1`, actual `0 == 1`); both no-body neighbors pass. |
| Repeated cancellation / C[repeated-cancellation], completed neighbor C[completed-neighbor] | Return before joining: exit 1, `AssertionError: cancellation escaped while the synchronous tool still owned an in-flight side effect`. Consume deferred cancellation: exit 1, `assert isinstance(outcomes[0], asyncio.CancelledError)` / `AssertionError: assert False` (actual normal compose result). Correct neighbor passes each mutation. |
| Already committed evidence / D and both S cases | Drain cumulative crash buffer: exit 1, expected `['call_unpersisted_crash']`, actual includes `call_committed`. Drain an already durable cohort: exit 1, both S cases fail `assert len(rows) == 1`, actual `assert 3 == 1`. Attempt another terminal after completion: exit 1, both S cases fail `assert failed_terminal_attempts == []` (actual one/two attempts). |
| Policy and guard refusal / A[cost], A[guard], A[provider] | Remove both explicit refusal branches and use generic crash handling. Exit 1: `assert <ComposerToolStatus.PLUGIN_CRASH: 'plugin_crash'> is <ComposerToolStatus.SUCCESS: 'success'>`. Cost and guard fail; first-party and recoverable provider neighbors pass. |

The baseline assertions bind exact original exceptions/causes and guard evidence identities. They inspect real P4 tool publication, actual provider settlement/ledger/audit rows, and completion receipts. Cancellation uses rendezvous observations and joins its owned work, including on mutation failures. The successful tool/model neighbor runs normally; the cancelled path starts neither its next tool nor its next model turn. Recovery drains the carried suffix through actual SQL while preserving every previously committed row. Completed precedence retains the original operation record without another failure-publication call, assistant row or audit duplicate. Quota controls inspect actual SQL effects and empty proposal queries; they do not independently exercise a complete compose publication path.

Final combined focused command used the requested dual-root `PYTHONPATH`, `.venv/bin/python -m pytest`, the eight selectors above, `-n 0 -v`. Actual process exit **0**, raw summary **`16 passed in 18.47s`**. The six production files and five test files were unchanged throughout: [final-frozen.log](final-frozen.log), exit **0**. Both import roots were measured inside this worktree. Ruff check and format check passed; `git diff --check` passed.

Source identities, exact mutation diffs and complete stdout/exit evidence: [service-mutations.json](service-mutations.json), the corresponding root `*.log`/`*.exit` files, [tools/report.md](tools/report.md), and [quota/report.md](quota/report.md). The original early-return harness timed out and initial durable-row expectations omitted independent redacted/audit evidence; these test-oracle/harness attempts are excluded from mutation qualification. The corrected early-return assertion red is `return-before-tool-join-final.log`.

Pins believed moved: **none** from the final tests-only diff. No pins, digests, baselines or approved xfails were edited. Reviewer re-derivation and whole-tree gates remain unrun and authoritative. Per instruction, no full suite, testcontainer suite, `test_web_landscape_mutation_fencing.py`, signing or `uv` command was run. No commit was attempted; the reviewer commits. No docs files were created. No supplied `-o` pathname was present, so this lane-private file is the output note.

The auxiliary startup work-at-risk scan completed (raw log `work-at-risk-start.log`). Its handover rerun remained at the inventory header and was stopped on this loaded host, actual exit 130 (`work-at-risk-handover.exit`); that incomplete maintenance scan is not test evidence.
