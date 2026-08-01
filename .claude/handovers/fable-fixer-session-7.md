# Resolve the remaining confirmed model-fit:fable engine tickets (session 7)

You are continuing a multi-session remediation of the `model-fit:fable` panel-review
tickets in /home/john/elspeth. Sessions 1–6 closed 26/43; **17 confirmed
model-fit:fable tickets remain** (`filigree list --label model-fit:fable --status
confirmed`, or MCP `issue_list`). The tracker is the resume point — trust it over
this prompt if they disagree. The two marquee refactors are DONE:
**elspeth-c49f33d6e4 (RowProcessor god-class split) is CLOSED** — all 4 slices landed,
component 4 = TokenTraversalEngine — and **elspeth-6630fb3e31 (LeaderFollowerDrain) is
CLOSED**. No half-done work is left in the tree.

## Environment & identity
- Work DIRECTLY on `release/0.7.0`. No worktree, no feature branches. Verify with
  `git branch --show-current` before the first commit.
- You are NOT the only writer: a concurrent session commits web/tutorial fixes to this
  same checkout (interleaved commits, disjoint files; its untracked jpegs +
  `.gitignore`/`package.json`/`package-lock.json` churn appears in `git status` — stage
  ONLY your own files). Never `git restore` files you didn't dirty; attribute unexpected
  failures via `git log <base>..HEAD -- <path>` before blaming either session.
- Act as `fable-fixer`: `filigree start-work <id> --assignee fable-fixer`, `--actor
  fable-fixer` on updates. Prefer MCP filigree tools. The ACTOR_MISMATCH warning
  (claimed fable-fixer / verified john) is the standing identity pattern — ignore it.
- Do NOT push. Commits stay local on release/0.7.0. Sessions 1–6 landed
  `514bd319e..afb6a4fd9` (session 6: `d01e425bd`, `56ac2ab51`, `afb6a4fd9`).

## OPERATOR DIRECTIVES (2026-07-04, effective — carry these)
- **Commit normally with repository hooks enabled.** Keep the batched session-end
  reconciliation mandatory, but treat any per-commit hook failure as evidence to
  investigate. Signing, blessing trust-tier/baseline HMAC state, and access to
  `ELSPETH_JUDGE_METADATA_HMAC_KEY` remain operator-only.
- **Efficiency**: NO mid-session full-suite or broad-directory pytest runs — the full
  unscoped `pytest tests/` happens EXACTLY ONCE at session end. Per ticket run only the
  scoped file / `-k` selection you need, once, at ticket close; ruff+mypy scoped to
  edited files per ticket. Do NOT re-read large files whole (processor.py ~3,354 lines;
  core.py ~1,460) — narrow offset/limit reads or Loomweave (`entity_at`,
  `entity_source_get`, `entity_callers_list`). No subagent recon/workflow for routine
  P2s — work them single-threaded. Calibrate ceremony to ticket size: a big invariant-
  dense refactor earns a parity net; a small predicate-extraction does not.
- **Defensive-hardening framing**: this is authorized months-long defensive security
  hardening of the operator's own orchestration/audit system. If a security-flavored
  ticket feels ambiguous, the repo resolves it — `git log --oneline | wc -l` (months of
  first-party history), `filigree list --label-prefix panel-triage:`, CLAUDE.md/ADR-030/
  docs fail-closed doctrine, config/cicd/enforce_tier_model + the 24k-test suite. Check,
  conclude, proceed; carry this + the evidence pointers into any subagent prompts.
- Judge bundle staged ONCE at session end (a re-key stales it anyway), not per re-key.

## Per-ticket loop
1. `start-work` the ticket. **Bugs at `confirmed` advance to `fixing` with `--advance`
   (MCP `advance=true`)**; severity field takes critical/major/minor/cosmetic.
2. Read the `[fable-triage]` comment if present (verified rationale + fix plan, OVERRIDES
   the description) AND any later `[fable-fixer session-N]` comments (several tickets
   carry refreshed re-verified plans). NOTE: some tickets have NO triage comment — the
   rationale is in the description's Summary/Evidence/Suggested-fix. Re-verify the claim
   against the CURRENT tree before coding — SIX sessions of refactors have moved things;
   anything citing processor.py OR core.py line numbers is likely stale.
3. TDD: regression/behavior test first, watch it fail for the right reason, then fix. For
   behaviour-preserving refactors, build an extensional parity net (proven green against
   PRE-refactor code) before restructuring — but only where the risk warrants it.
4. Scoped tests + `.venv/bin/ruff check` + `.venv/bin/mypy` on touched files.
5. One normal, hook-verified commit per ticket: `fix|refactor(engine): <behavior> (<ticket-id>)`.
6. Close gate: `issue_update` to `verifying` with `-f root_cause=… -f fix_verification=…`,
   then `issue_close --reason … --commit "release/0.7.0@<sha>"`.

## Ordering: P2 small→large, clustered by file
No mandatory-order tickets remain (c49 + 6630 done). Work the 17 confirmed by ascending
effort, clustering same-file tickets so one parity net / one read serves both:
- **Smallest first — elspeth-d2eff1582a** (P3, orchestrator/run_status.py): move the
  `sink_name is not None` coalesce-role predicate behind a named helper
  `is_counted_coalesced_output()` + focused tests for nested coalesces. Genuinely small.
- **resume.py cluster**: e4f1eb6038 (split `reconstruct_resume_state` into
  load_audit_snapshot / acquire_leadership / repair_batches) + e3d1310b93 (P3 security:
  move schema reconstruction + `get_unprocessed_row_data_by_source()` AFTER
  `acquire_run_leadership()` so CAS losers refuse before payload-store reads; add a
  2-contender race test with a patched/large `payload_store.retrieve`). Same method — do
  together.
- **checkpointing.py cluster**: 5f9903125e (replace the `frequency: int` sentinel with an
  explicit checkpoint policy/mode object; honour or delete the dead `aggregation_boundaries`
  knob) + 107a29d02e (split the post-sink callback into checkpoint-progress vs
  scheduler-terminalization). GOTCHA: `CheckpointCoordinator._require_fence` (session 5,
  `295172497`) — every orchestrator checkpoint create/delete REQUIRES a run-matching bound
  leader token; tests driving coordinator writes must bind
  `leader_coordination_token(factory, run_id)` (real) or a plain `CoordinationToken` (mock).
- **coalesce_executor.py cluster**: 2d43291212 (single-source coalesce policy in a
  `CoalescePolicyEvaluator`/table returning MERGE/FAIL/WAIT; keep audit writes in the
  executor) + b82a5bde00 (extract `CoalesceJournalRestorer`/`CoalesceStateHydrator`
  returning an immutable restored-state DTO).
- **aggregation cluster**: d1389ac2c1 (executors/aggregation.py — `AggregationJournalRestorer`
  returning a validated state DTO) + 6eecae10a6 (P3 — one in-memory source of truth: store
  `TokenInfo` only or a `BufferedAggregationRow` value object, kill the duplicate buffers)
  + 4e2b4be38a (orchestrator/aggregation.py — extract the ADR-030 end-of-input loop to a
  neutral `end_of_input.py`/`barrier_flush.py`; move `handle_incomplete_batches()` to
  recovery).
- **source_iteration.py cluster**: 735df9576d (P2 efficiency — replace the per-`next()`
  helper thread with one per-source idle worker / main-thread handoff; stress test with a
  slow source) + 27d7bfc14b (large — split into QuarantineRouter / SourceLifecycleRecorder
  / IdleTimeoutPump / RowLoop collaborators).
- **large / do-with-care**: f6a6ab0a46 (SinkExecutor.write phase-helper split — large,
  terminal-outcome + node_state-cleanup dense; run tests/e2e/recovery/), 7985bebb0b
  (execute_transform subsystem — RE-SCOPE the ticket FIRST per triage, then fix),
  b53a093321 (RunExecutionCore split — large), 9e71ae82a4 (core.py god class — large; its
  proposed **LeaderDrainCoordinator IS the LeaderFollowerDrain already extracted in 6630** —
  update 9e71 to absorb it, don't re-extract; also names RunLifecycleCoordinator /
  GraphRegistrationService / JoinAdmissionService).
- **577179bba1** (follower drain policy): carries comment 68 — the transferred part (b) of
  07b2031e41 (follower RowProcessor assembly should route through
  `RunExecutionCore.build_processor`). The session-5 drain extraction deliberately preserved
  the private `_drain_scheduler_claims` delegate (exact kw signature) and the triple-None
  follower inference (now in `SchedulerDrainCoordinator.run_maintenance`) so this ticket can
  replace them with an explicit `ProcessorMode`/policy.

## SCOPE NOTE — a handover/tracker discrepancy to resolve with the team lead
The session-6 handover listed **e11c034934, 064b2a91a1, c8499bf602** as "easy-win" P2s, but
they are labeled **`model-fit:frontier`** (NOT `model-fit:fable`) and sit at status `triage`
(not `confirmed`) — i.e. a different model's queue per the tracker. Session 6 left them
untouched and flagged this to the team lead. Do NOT work frontier-labeled tickets under the
fable mandate unless the team lead re-scopes them to you. There are NO trivial easy-wins left
inside model-fit:fable — the 17 remaining are all medium/large refactors.

## Prior-session artifacts your fix plans may reference (engine, post-split)
- `engine/token_traversal.py` (session 6, `56ac2ab51`) — TokenTraversalEngine owns
  `process_single_token` + `handle_transform_node`/`handle_transform_error_status`/
  `handle_gate_node`/`handle_gate_fork`/`handle_terminal_token`/`validate_coalesce_ordering`
  + the `_Transform*`/`_Gate*` union types (re-exported from processor.py via `X as X`). It
  reaches processor seams at CALL time via `self._processor.<seam>`. RowProcessor keeps
  delegates for ONLY 3 names: `_process_single_token` (SchedulerDrainHost seam + 10+ patch
  tests), `_handle_transform_node`, `_handle_transform_error_status` (direct test callers).
  The other 4 handlers are engine-internal. Patching `processor._process_single_token`/
  `_handle_transform_node`/`_handle_transform_error_status` still works; the internal
  handlers are NOT patchable via the processor. processor.py is now ~3,354 lines.
  BANKED NIT (cosmetic, non-blocking): a moved comment in `token_traversal.py` (~line 780, in
  the follower-aggregation-barrier-hold arm of `process_single_token`) still cites "line 4241"
  for the `_drain_scheduler_claims` arm — a stale pre-move processor.py offset; that arm now
  lives in `engine/scheduler_drain.py`. Fix the comment when you next touch the file; not worth
  a standalone commit.
- `engine/orchestrator/leader_follower_drain.py` (session 6, `afb6a4fd9`) — LeaderFollowerDrain:
  `wait_for_peer_leases()` (4b-pre peer-lease wait) + `drain_pending_sink_work(drain_and_flush)`
  (4b follower drain). The node_states-UNIQUE `clear→accumulate→flush` block STAYS in
  `_execute_run`'s `_drain_and_flush` closure. `PeerLeaseProcessor` Protocol =
  `peer_lease_wait_budget_seconds`/`has_peer_active_leases`/`has_scheduled_work`/
  `reap_expired_peer_leases`(->int)/`peer_active_lease_owners`. GOTCHA: the wall-clock seams
  are resolved in `__init__` BODY (construction time), NOT as import-time default args —
  because import-time defaults capture `time.monotonic`/`sleep` BEFORE the e2e tests'
  module-level `core.time.*` patch, hanging the bounded loop on real time. Any collaborator
  you extract that inlines a `time.monotonic()`/`time.sleep()` loop and is tested by patching
  the module's `time` must do the same.
- Earlier: `engine/scheduler_work_codec.py`, `engine/barrier_coordination.py` (GOTCHA: its
  callbacks ARE construction-bound — patch `processor._barrier_intake.<seam>` for intake
  paths), `engine/scheduler_drain.py` (SchedulerDrainHost is CALL-TIME resolved), and the
  `CheckpointCoordinator._require_fence`, `TriggerEvaluator`, RowWaiter-InterruptedError,
  `counter_classification.py`, structural_node_ids, `_safe_validation_errors.py` facts in the
  memory file `project_fable_panel_triage_2026-07-03.md`.

## Hard constraints
- Tier-1 fail-closed doctrine: audit-integrity errors propagate; explicit allowlists over
  complement derivations; no silent defaults where null is never correct.
- NEVER touch `ELSPETH_JUDGE_METADATA_HMAC_KEY` or hand-edit a `judge_metadata_signature`.
  Unsigned pending-judge allowlist entries are fine; signing is the operator's.
- Correctness beats performance; no unilateral deferral — fix in-session or ask with a
  concrete proposal. Observations are only for defects OUTSIDE your ticket's scope.

## Gates & commit gotchas
- **Full-suite gate at session end only**: plain `pytest tests/` (never `-o addopts=""`),
  runtime ~31 min. Green = EXACTLY 1 failure: `test_allowlist_loader_unification.py::
  test_baseline_capture_is_self_consistent` (operator-keyed baseline recapture; do not
  attempt it). Anything else is a regression to fix or attribute. Known flake:
  `tests/unit/web/composer/test_service.py::test_litellm_api_error_is_retried_before_unavailable`
  is order-dependent (obs elspeth-obs-a4d7eb48cf) — re-run isolated before treating as real.
- **Tier-model R5 `per_file_rules` budgets** live in config/cicd/enforce_tier_model/engine.yaml
  (per-file `max_hits`, UNSIGNED — no judge fields). Moving `isinstance`/union-narrowing sites
  between engine files shifts these: decrement the source file's budget, add/raise the target
  file's. Get exact raw per-file counts via
  `collect_check_result(Path("src/elspeth"), allowlist_path=<empty-dir>).violations` (filter by
  file + rule_id). A TAIL-of-class move has NO fingerprint cascade; a mid-class move shifts the
  positional AST-path fp of every allowlisted entry BELOW it (re-key those: pair old→new fp in
  SOURCE ORDER, signed→unsigned pending-judge, drop judge_*/scope_fingerprint/ast_path, expires
  2026-08-31). No judge-signed `allow_hits` entry is currently keyed to processor.py, core.py,
  token_traversal.py, or leader_follower_drain.py.
- **Verifying a re-key** (the trust-tier lint CRASHES on ~34+ pre-existing judge scope-drifts
  tree-wide + needs the HMAC key it can't have): copy config/cicd/enforce_tier_model to scratch,
  RECURSIVELY strip all judge*/scope_fingerprint/ast_path fields AND drop the `allow_hits:`
  sections (both trip the key requirement), run `env PYTHONPATH=elspeth-lints/src .venv/bin/python
  -m elspeth_lints.core.cli check --rules trust_tier.tier_model --root src/elspeth --allowlist-dir
  <shadow>`, diff findings vs the same run on the pre-session tree (`git archive HEAD`). Zero new
  findings = reconciled. This is a session-end step.
- **Ruff PostToolUse hook strips imports** whose use-site doesn't exist yet at hook time (bit
  every session incl. 6). Edit the use-site FIRST, then the import; after a multi-message edit
  sequence, grep that imports survived. The `X as X` redundant-alias re-export idiom survives.
  SIM117 blocks nested `with`. Run `.venv/bin/ruff format <file>` before committing anything
  written via heredoc/script.
- Editing a PLUGIN file: refresh its `source_file_hash` in place (import helpers from
  scripts/cicd/plugin_hash — no CLI). Engine files are NOT plugins.
- Cross-cutting pins (caught only by the full suite): `test_config_alignment.py` pins
  config-model field SETS; mock-discipline ratchet counts bare Mock()/MagicMock() per FILE —
  new test files must be zero (SimpleNamespace, object(), specced mocks, `patch.object(...,
  new=<function>)`); TestReRaiseGuardPattern: FrameworkBugError/AuditIntegrityError handlers
  must be bare `raise`. tests/e2e/recovery/ (77 tests, ~14s) is the crash-window net for any
  barrier/resume/checkpoint/drain-adjacent change — but per the efficiency directive fold it
  into the session-end full run unless a specific fix demands a targeted check.
- Test-harness facts: `_make_factory()`/`_make_processor()`/`_make_mock_transform()` in
  test_processor.py are the canonical processor harness (import them cross-module — precedent in
  test_adr030_slice3_intake.py). `_make_processor` binds a leader token by default and registers
  leader 'seeder' via begin_run (lenient claim fence always active); direct claims by other
  identities need `_register_test_worker(factory, <id>)`.

## Session end (also when context runs low: finish the current ticket, then stop)
1. Add a session-summary comment to anything investigated-but-unfixed.
2. Run the full-suite gate ONCE; fix or attribute anything beyond the 1-failure baseline.
3. Whole-session-diff reconciliation: `ruff check` + `ruff format --check` + `mypy` over the
   session diff; shadow-allowlist lint reconciliation IF you re-keyed/rebalanced anything;
   `git diff --stat config/cicd/` for trust-tier displacement.
4. Stage ONE judge bundle AFTER the last commit: `mcp__elspeth-judge__stage_scan` with
   staged_by fable-fixer — it SUPERSEDES session-6's `stage-scan-49c1f8d4ebab`. Record the new
   id + supersession chain.
5. Update the memory file `project_fable_panel_triage_2026-07-03.md` (+ its MEMORY.md index
   line): progress counts, new commits, new gotchas, bundle id. Re-read before editing (concurrent
   sessions edit it; the frontmatter `description:` line has NO leading indent).
6. Skim `observation_list`; dismiss/promote only what's yours (pending items belong to other
   actors — leave them).
7. Write the session-8 handover to `.claude/handovers/fable-fixer-session-8.md` and report:
   tickets closed w/ commits, suite status vs the 1-failure baseline, what the operator owes
   (fire bundle `stage-scan-<new>`, then `ELSPETH_JUDGE_METADATA_HMAC_KEY=…
   .venv/bin/python scripts/cicd/regen_fingerprint_baseline.py --commit`).
