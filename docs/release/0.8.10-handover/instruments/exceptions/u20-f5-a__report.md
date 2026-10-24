F5 part A implemented, uncommitted for reviewer, on codex/0.8.10-exceptions at d42337e1ea03e7af7dddbc42664009d8ac3dabe6.

Files: src/elspeth/web/composer/{_compose_loop_carriers,tool_batch,service,redaction,continuation_batch_data,continuation_p4_batch}.py; new tests/unit/web/composer/{test_service_failure_dispatch,test_service_failure_publication,test_service_failure_live_evidence,test_continuation_service_failure}.py.

The closed nominal family retains original objects through every carrier seam, closes SERVICE_FAILURE once under semantic success=false/audit SUCCESS, stops dispatch, suppresses EARLY, sets live P4 crash_pending, and propagates after successful publication. Independent cancellation/effect evidence preserves original causes. All applicable codecs refuse live service exceptions; supported wire formats and historical vocabulary stay unchanged.

Test IDs: test-ids.log contains all 37 exact new IDs from live pytest collection (exit 0). Root IDs: test_service_failure_publishes_prior_mutation_once_before_original_propagates; test_service_failure_suppresses_otherwise_eligible_early_checkpoint; test_service_failure_projection_admits_only_the_closed_interpretation_refusal; test_successful_service_publication_retains_independent_live_companion[effect,cancel]. Full dispatch/codec IDs and evidence are in dispatch-tests/report.md and codec/report.md.

Accepted R assertions, each pytest exit 1, with source identities recorded:
- Registered read/write SQL/executor: `nominal service failure must retain its original owner` (None versus original).
- Stop/deadline/both fences: `lifecycle authority failure must retain its original owner` (None versus original).
- Prior mutation: `service original must propagate after required P4` (ComposerPluginCrashError versus original OperationalError).
- After prerequisite capture: `P4 must receive the live primary-failure disposition` / `assert False is True`; `service refusal must prevent a new EARLY Advisor call` / actual additional Advisor phases.
See dispatch-tests/red-registered.log, dispatch-tests/red-lifecycle-valid.log, publication-red.log, decision-valid-red.log. Invalid fixture probes are not credited.

Validation: 62 focused tests passed (exit 0, frozen before/after hashes); separate codec run 131 passed (exit 0); required mutation-authority/attribute checks 303 passed, 1 approved xfailed (exit 0). Both approved xfails untouched. Ruff check/format check and diff check passed. Independent review: no unresolved within-scope finding (review/report.md).

Every new test received its named mutation. All mutant processes exited 1: generic capture killed all twelve service/lifecycle cases; execution-copyback omission killed prior-mutation evidence; EARLY/P4 omissions, companion loss, projection loss, all four distinct-neighbor mutations, nine serialization omissions, three historical-field drops and four inverted P4 consistency variants were detected. All reverted; final ten-file hashes exactly match the 62-pass tree. Temporary tools/sessions.py changes also reverted. Reports/logs hold exact assertions and exits.

Pins for reviewer re-derivation, none edited: ContinuationBatchState source model; initialize/prepare/execute/finish helper digests; codec _joint body and new _require_no_live_batch_failure definition/reference inventory; service _advance_live_compose_iteration digest. Review also identifies pre-existing overlay codec pin drift separately. No dedicated DispatchOutcome/P4/redaction pin found; transitive guard effects remain reviewer-owned.

Excluded as requested: F4, returned/thrown P4 failure retention, full suite, testcontainers and forbidden landscape gate. No docs files, commits, suppressions, uv or pin edits. Lifecycle tests stop before revoked-authority P4 and verify the actual worker reducer against current durable records. Commits and whole-tree qualification belong to reviewer; no part-A blocker remains.

Hygiene limit: work-at-risk invoked at start/handover; the final host-wide scan was interrupted (130) while scanning main, to bound unrelated loaded-host work. Partial log retained; no fresh host-wide inventory or cleanup claimed.
