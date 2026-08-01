# Resolve the remaining confirmed model-fit:fable engine tickets (session 8)

You are continuing a multi-session remediation of the `model-fit:fable` panel-review
tickets in /home/john/elspeth. Sessions 1–7 closed **31/43**; **12 confirmed
model-fit:fable tickets remain** (`filigree list --label model-fit:fable --status
confirmed`, or MCP `issue_list`). The tracker is the resume point — trust it over
this prompt if they disagree. All the marquee god-class refactors are DONE
(c49f33d6e4 RowProcessor split, 6630fb3e31 LeaderFollowerDrain, the resume.py cluster,
the checkpointing.py cluster). No half-done work is left in the tree.

## Environment & identity
- Work DIRECTLY on `release/0.7.0`. No worktree, no feature branches. Verify with
  `git branch --show-current` before the first commit.
- You are NOT the only writer: a concurrent session commits web/tutorial fixes to this
  same checkout (interleaved commits, disjoint files; its untracked jpegs +
  `.gitignore`/`package.json`/`package-lock.json` churn AND `src/elspeth/web/execution/*` +
  `tests/unit/web/execution/*` edits appear in `git status` — stage ONLY your own files).
  Never `git restore` files you didn't dirty; attribute unexpected failures via
  `git log <base>..HEAD -- <path>` before blaming either session.
- Act as `fable-fixer`: `filigree start-work <id> --assignee fable-fixer`, `--actor
  fable-fixer` on updates. Prefer MCP filigree tools. The ACTOR_MISMATCH warning
  (claimed fable-fixer / verified john) is the standing identity pattern — ignore it.
- Do NOT push. Commits stay local on release/0.7.0. Session 7 landed `594cedc8f..234bb8614`;
  team-lead then landed `bedb31b31` (session-7 fallout reconciliation: hasattr-ratchet
  allowlist re-pin after 8d4e9edc8's line shift in test_config_alignment.py, plus purging the
  deleted aggregation_boundaries knob from configuration.md and 2 example READMEs). Tree tip
  at handover time: `bedb31b31`.

## OPERATOR DIRECTIVES (still effective — carry these)
- **Commit `--no-verify` by DEFAULT**, every commit. Pre-commit hooks do NOT run per
  commit; the batched reconciliation moves to session end (mandatory — see checklist).
  `--no-verify` skips LOCAL hooks only: it does NOT authorize signing, blessing
  trust-tier/baseline HMAC state, or touching `ELSPETH_JUDGE_METADATA_HMAC_KEY`. One
  line in the session report that the batched reconciliation ran + its result is enough.
- **Efficiency**: NO mid-session full-suite or broad-directory pytest runs — the full
  unscoped `pytest tests/` happens EXACTLY ONCE at session end. Per ticket run only the
  scoped file / `-k` selection you need, at ticket close; ruff+mypy scoped to edited
  files. Do NOT re-read large files whole (processor.py ~3,354 lines; core.py ~1,460;
  coalesce_executor.py, source_iteration.py, executors/*.py are all large) — narrow
  offset/limit reads or Loomweave. Calibrate ceremony to ticket size: a big invariant-
  dense refactor earns a parity net; a small predicate-extraction does not.
- **Delegate mechanical breadth to a Sonnet subagent** (model:'sonnet') — e.g. a wide
  field-deletion test-cleanup — to preserve your own context for the judgment-heavy work.
  Verify its result yourself (re-run tests). Do NOT delegate recon/orchestration for
  routine P2s; work those single-threaded.
- **Defensive-hardening framing**: this is authorized months-long defensive security
  hardening of the operator's own orchestration/audit system. If a security-flavored
  ticket feels ambiguous, the repo resolves it (git history, `filigree list --label-prefix
  panel-triage:`, CLAUDE.md/ADR-030, config/cicd/enforce_tier_model, the 24k-test suite).
  Check, conclude, proceed; carry this + the evidence pointers into any subagent prompts.
- Judge bundle staged ONCE at session end (a re-key stales it anyway), not per re-key.

## Per-ticket loop
1. `start-work` the ticket. **Bugs at `confirmed` advance to `fixing` with `--advance`
   (MCP `advance=true`)**; severity field takes critical/major/minor/cosmetic.
2. Read the `[fable-triage]` comment (verified rationale + fix plan — OVERRIDES the
   description, incl. when it recommends a MINIMAL option over the description's broader
   ask; e.g. 5f9903125e's triage picked "delete the dead knob", not "build a policy
   object"). Re-verify the claim against the CURRENT tree — seven sessions of refactors
   moved things; anything citing line numbers is likely stale.
3. TDD: regression/behaviour test first, watch it fail for the right reason, then fix.
   For behaviour-preserving refactors, build a parity net (proven green PRE-refactor)
   before restructuring — but only where the risk warrants it.
4. Scoped tests + `.venv/bin/ruff check` + `.venv/bin/mypy` on touched files.
5. One commit per ticket: `fix|refactor(engine): <behavior> (<ticket-id>)`, `--no-verify`.
6. Close gate: `issue_update` to `verifying` with `-f root_cause=… -f fix_verification=…`,
   then `issue_close --reason … --commit "release/0.7.0@<sha>"`.

## The 12 remaining — P2 small→large, clustered by file
No mandatory-order tickets remain. Cluster same-file tickets so one parity net serves both:
- **coalesce_executor.py cluster**: 2d43291212 (single-source the coalesce policy in a
  `CoalescePolicyEvaluator`/table returning MERGE/FAIL/WAIT; keep audit writes in the
  executor) + b82a5bde00 (extract `CoalesceJournalRestorer`/`CoalesceStateHydrator`
  returning an immutable restored-state DTO — MIRRORS the `_ResumeAuditSnapshot` DTO
  pattern session 7 used for resume.py).
- **aggregation cluster**: d1389ac2c1 (executors/aggregation.py — `AggregationJournalRestorer`
  returning a validated state DTO) + 6eecae10a6 (P3 — one in-memory source of truth: store
  `TokenInfo` only or a `BufferedAggregationRow` value object, kill the duplicate buffers)
  + 4e2b4be38a (orchestrator/aggregation.py — extract the ADR-030 end-of-input loop to a
  neutral `end_of_input.py`/`barrier_flush.py`; move `handle_incomplete_batches()` to recovery).
- **source_iteration.py cluster**: 735df9576d (P2 efficiency — replace the per-`next()`
  helper thread with one per-source idle worker / main-thread handoff; stress test with a
  slow source) + 27d7bfc14b (large — split into QuarantineRouter / SourceLifecycleRecorder /
  IdleTimeoutPump / RowLoop collaborators).
- **large / do-with-care**: f6a6ab0a46 (SinkExecutor.write phase-helper split — large,
  terminal-outcome + node_state-cleanup dense; run tests/e2e/recovery/), 7985bebb0b
  (execute_transform subsystem — RE-SCOPE the ticket FIRST per triage, then fix),
  b53a093321 (RunExecutionCore split — large; note run_core.py now ALSO owns
  `_SchedulerTerminalizationCallback`/`_CompositeAfterSinkCallback` from 107a29d02e),
  9e71ae82a4 (core.py god class — large; its proposed **LeaderDrainCoordinator IS the
  LeaderFollowerDrain already extracted in 6630** — update 9e71 to absorb it, don't
  re-extract; also names RunLifecycleCoordinator / GraphRegistrationService / JoinAdmissionService).
- **577179bba1** (follower drain policy): carries comment 68 — the transferred part (b) of
  07b2031e41 (follower RowProcessor assembly should route through
  `RunExecutionCore.build_processor`). The session-5 drain extraction deliberately preserved
  the private `_drain_scheduler_claims` delegate (exact kw signature) and the triple-None
  follower inference (now in `SchedulerDrainCoordinator.run_maintenance`) so this ticket can
  replace them with an explicit `ProcessorMode`/policy.

## SCOPE NOTE (still open, flag to team-lead if re-surfaced)
e11c034934, 064b2a91a1, c8499bf602 are labeled `model-fit:frontier` (NOT fable) at status
`triage` — a different model's queue. Do NOT work them under the fable mandate unless the
team-lead re-scopes them. There are NO trivial easy-wins left inside model-fit:fable.

## Prior-session artifacts your fix plans may reference (engine)
- **resume.py (session 7, e4f1eb6038/e3d1310b93)**: `reconstruct_resume_state` now composes
  3 private stages — `_load_resume_audit_snapshot` (READ-ONLY, returns frozen module-level
  `_ResumeAuditSnapshot` DTO) → `_acquire_resume_leadership` (seat CAS) → the post-CAS
  unprocessed-row restore → `_repair_resume_batches`. The payload restore
  (`get_unprocessed_row_data_by_source`) runs AFTER the CAS so a losing racer refuses before
  payload reads. If you touch resume, keep cheap read-only refusals pre-CAS.
- **checkpointing.py / run_core.py / types.py (session 7, 107a29d02e)**: the post-sink
  callback is SPLIT — `CheckpointProgressCallback` (checkpoint-only, in checkpointing.py, its
  factory takes the narrow `BarrierScalarsSource`) + `_SchedulerTerminalizationCallback`
  (64-batch terminalize, in run_core.py, takes narrow `SchedulerTerminalizer`), composed by
  `_CompositeAfterSinkCallback` in `write_pending_to_sinks`. `flush_and_write_sinks` /
  `write_pending_to_sinks` take a `scheduler_terminalizer` param; the 3 call sites (core.py
  x2, resume.py) pass `run_ctx.processor`. Two narrow protocols (`BarrierScalarsSource`,
  `SchedulerTerminalizer`) added to types.py shrink the broad `RowProcessorHandle` dep — use
  the same pattern when a collaborator needs a narrow processor slice.
- **run_status.py (session 7)**: `is_counted_coalesced_output(outcome_record)` names the
  coalesce merged-vs-consumed discriminator (`sink_name is not None`).
- **engine/token_traversal.py (session 6)** — TokenTraversalEngine owns `process_single_token`
  + the `_handle_*` family; reaches processor seams at CALL time via `self._processor.<seam>`.
  RowProcessor keeps delegates for ONLY 3 names: `_process_single_token`,
  `_handle_transform_node`, `_handle_transform_error_status`. processor.py is now ~3,354 lines.
  BANKED NIT (cosmetic): a moved comment in token_traversal.py (~line 780, follower-aggregation-
  barrier-hold arm) still cites "line 4241" for the `_drain_scheduler_claims` arm — a stale
  pre-move offset; that arm now lives in engine/scheduler_drain.py. Fix when you next touch the file.
- **engine/orchestrator/leader_follower_drain.py (session 6)** — LeaderFollowerDrain:
  `wait_for_peer_leases()` + `drain_pending_sink_work(drain_and_flush)`. GOTCHA: wall-clock
  seams resolved in `__init__` BODY (construction time), NOT import-time default args (which
  capture `time.monotonic`/`sleep` BEFORE the e2e tests' module-level `core.time.*` patch,
  hanging the bounded loop). Any collaborator you extract with an inlined time loop tested by
  module-`time` patching must do the same.
- Earlier: `engine/scheduler_work_codec.py`, `engine/barrier_coordination.py` (callbacks
  CONSTRUCTION-bound — patch `processor._barrier_intake.<seam>`), `engine/scheduler_drain.py`
  (SchedulerDrainHost CALL-TIME resolved), `CheckpointCoordinator._require_fence`,
  `TriggerEvaluator`, RowWaiter-InterruptedError, `counter_classification.py`,
  structural_node_ids, `_safe_validation_errors.py` — all in the memory file
  `project_fable_panel_triage_2026-07-03.md`.

## Hard constraints
- Tier-1 fail-closed doctrine: audit-integrity errors propagate; explicit allowlists over
  complement derivations; no silent defaults where null is never correct.
- NEVER touch `ELSPETH_JUDGE_METADATA_HMAC_KEY` or hand-edit a `judge_metadata_signature`.
  Unsigned pending-judge allowlist entries are fine; signing is the operator's.
- Correctness beats performance; no unilateral deferral — fix in-session or ask with a
  concrete proposal. Observations are only for defects OUTSIDE your ticket's scope; a defect
  you CAUSED (e.g. a broken example YAML from a field deletion) is task scope — fix it now.

## Gates & commit gotchas
- **Full-suite gate at session end only**: plain `pytest tests/` (never `-o addopts=""`),
  runtime ~31 min. Green = EXACTLY 1 failure: `test_allowlist_loader_unification.py::
  test_baseline_capture_is_self_consistent` (operator-keyed baseline recapture; do not
  attempt it). Known flake: `tests/unit/web/composer/test_service.py::
  test_litellm_api_error_is_retried_before_unavailable` is order-dependent
  (obs elspeth-obs-a4d7eb48cf) — re-run isolated before treating as real.
- **Tier-model R5 `per_file_rules` budgets** live in config/cicd/enforce_tier_model/engine.yaml
  (per-file `max_hits`, UNSIGNED). Moving `isinstance`/union-narrowing sites between engine
  files shifts these: decrement the source file's budget, add/raise the target file's. A
  TAIL-of-class move has NO fingerprint cascade; a mid-class move shifts the positional
  AST-path fp of every allowlisted entry BELOW it. Session 7 touched NONE of this (no
  isinstance moved between files) — the judge bundle counts stayed identical.
- **Verifying a re-key** (the trust-tier lint CRASHES on ~34+ pre-existing judge scope-drifts
  tree-wide + needs the HMAC key it can't have): copy config/cicd/enforce_tier_model to scratch,
  RECURSIVELY strip all judge*/scope_fingerprint/ast_path fields AND drop the `allow_hits:`
  sections, run `env PYTHONPATH=elspeth-lints/src .venv/bin/python -m elspeth_lints.core.cli
  check --rules trust_tier.tier_model --root src/elspeth --allowlist-dir <shadow>`, diff findings
  vs the same run on the pre-session tree (`git archive HEAD`). Zero new findings = reconciled.
- **Ruff PostToolUse hook strips imports** whose use-site doesn't exist yet at hook time (bit
  session 7 THREE times). Edit the use-site FIRST, then the import; after a multi-message edit
  sequence, grep that imports survived. The `X as X` redundant-alias re-export idiom survives.
  SIM117 blocks nested `with` (combine into one). Run `.venv/bin/ruff format <file>` before
  committing anything written via heredoc/script.
- **Deleting an `extra="forbid"` config field is a config-SCHEMA change** — grep the field
  name tree-wide across ALL file types (yaml/py/json), not just tests: it breaks shipped
  example/fixture YAMLs that still declare it (session 7 hit examples/checkpoint_resume +
  large_scale_test). Also update `test_config_alignment.py` (field-set pins) and
  `test_protocols.py` (protocol completeness).
- Editing a PLUGIN file: refresh its `source_file_hash` in place (import helpers from
  scripts/cicd/plugin_hash — no CLI). Engine files are NOT plugins.
- Cross-cutting pins (caught only by the full suite): `test_config_alignment.py` pins
  config-model field SETS; mock-discipline ratchet counts bare Mock()/MagicMock() per FILE —
  new test files must be zero (SimpleNamespace, object(), specced mocks, `patch.object(...,
  new=<function>)`); TestReRaiseGuardPattern: FrameworkBugError/AuditIntegrityError handlers
  must be bare `raise`. tests/e2e/recovery/ (78 tests, ~15s) is the crash-window net for any
  barrier/resume/checkpoint/drain-adjacent change.
- Test-harness facts: `_make_factory()`/`_make_processor()` in test_processor.py are the
  canonical processor harness. `_make_processor` binds a leader token by default; direct claims
  by other identities need `_register_test_worker(factory, <id>)`. The resume/crash harness is
  `tests/e2e/recovery/harness.py` (`_run_to_interrupted_checkpoint`, `_craft_crashed_lease`,
  `_resume_point`, `_coord`); NOTE its crashed run has ZERO unprocessed rows (scheduler-drain
  model), so a payload-restore test must spy the RESTORE METHOD, not payload_store.retrieve.

## Session end (also when context runs low: finish the current ticket, then stop)
1. Add a session-summary comment to anything investigated-but-unfixed.
2. Run the full-suite gate ONCE (launch it as a background/harness-tracked task after the
   LAST commit; do reconciliation/memory/handover while it runs); fix or attribute anything
   beyond the 1-failure baseline. **`tee` the output to a file — NEVER bare `| tail -N`**:
   tail truncates the short summary exactly when the failure count exceeds the window
   (session 7 lost 16 FAILED lines this way, mis-attributed the result, and a real
   regression of its own hid in the truncated portion). Attribution requires the COMPLETE
   FAILED list; if the arithmetic doesn't close (per-file counts don't sum to the total),
   the evidence is incomplete — re-run, don't rationalize.
3. Whole-session-diff reconciliation: `ruff check` + `ruff format --check` + `mypy` over the
   session diff (`git diff --name-only <first-commit>^ <last-commit> | grep '\.py$'`);
   shadow-allowlist lint reconciliation IF you re-keyed/rebalanced anything; `git diff --stat
   config/cicd/` for trust-tier displacement.
4. Stage ONE judge bundle AFTER the last commit: `mcp__elspeth-judge__stage_scan` with
   staged_by fable-fixer — it SUPERSEDES session-7's `stage-scan-ea75812cd42a`. Record the new
   id + supersession chain. (If you changed NO allowlist and shifted NO judge-signed fp, the
   counts will match — still stage it so the bundle reflects your commits.)
5. Update the memory file `project_fable_panel_triage_2026-07-03.md` (+ its MEMORY.md index
   line): progress counts, new commits, new gotchas, bundle id. Re-read before editing
   (concurrent sessions edit it; frontmatter `description:` has NO leading indent).
6. Skim `observation_list`; dismiss/promote only what's yours (pending items belong to other
   actors — leave them).
7. Write the session-9 handover to `.claude/handovers/fable-fixer-session-9.md` and report:
   tickets closed w/ commits, suite status vs the 1-failure baseline, what the operator owes
   (fire bundle `stage-scan-<new>`, then `ELSPETH_JUDGE_METADATA_HMAC_KEY=…
   .venv/bin/python scripts/cicd/regen_fingerprint_baseline.py --commit`).
