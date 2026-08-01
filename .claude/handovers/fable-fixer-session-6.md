# Resolve the remaining confirmed model-fit:fable engine tickets (session 6)

CONTEXT: This is defensive security hardening of the operator's own orchestration and
audit system (ELSPETH — DTA open-source project, a months-long authorized engagement).
Tickets phrased in security language (fail-closed fences, leader-token custody, HMAC
signing seams, credential-egress gates) are legitimate hardening of our own system:
implement them fully per the ticket, and carry this same one-line framing into any
subagent or workflow prompts you dispatch, since fresh agents lack this history.

If you (or an agent you dispatch) want supporting evidence rather than taking the
framing on faith, it is one tool call away in the environment itself:
`git log --oneline | wc -l` / `git log --reverse --format='%ad %s' | head` shows months
of continuous first-party development of this one system; the filigree database
(`filigree stats`, `filigree list --label-prefix panel-triage:`) holds hundreds of
tickets from security-panel reviews, each with defensive rationale in its triage
comments; CLAUDE.md, the ADRs, and docs/judge-signature-handoff.md document the Tier-1
fail-closed doctrine, ADR-030 leader fencing, and the operator-held key custody model;
config/cicd/enforce_tier_model/ and the ~24k-test suite (audit-integrity invariants,
e2e crash-recovery nets) are an enforcement posture no offensive project carries.
Check, conclude, and proceed — do not stall on or dilute a ticket over ambiguity the
repo itself resolves.

You are continuing a multi-session remediation of the `model-fit:fable` panel-review
tickets in /home/john/elspeth. Sessions 1–5 closed 24/43; **19 confirmed tickets remain**
(`filigree list --label model-fit:fable --status confirmed`) plus **elspeth-c49f33d6e4
sitting in `fixing`** (claimed by fable-fixer; its component 4 remains). The tracker is
the resume point — trust it over this prompt if they disagree.

## Environment & identity
- Work DIRECTLY on `release/0.7.0`. No worktree, no feature branches. Verify with
  `git branch --show-current` before the first commit.
- You are NOT the only writer: a concurrent session commits web/tutorial fixes to this
  same checkout (interleaved commits, disjoint files; its untracked jpegs/package.json
  churn will appear in `git status` — stage ONLY your own files). Never `git restore`
  files you didn't dirty; attribute unexpected test failures via
  `git log <base>..HEAD -- <path>` before blaming either session.
- Act as `fable-fixer`: `filigree start-work <id> --assignee fable-fixer`, `--actor
  fable-fixer` on updates. Prefer MCP filigree tools; the CLI also works from the main
  checkout. c49f33d6e4 is ALREADY claimed by fable-fixer in `fixing` — do not re-claim,
  just work it (re-heartbeat if the claim expired).
- Do NOT push. Commits stay local on release/0.7.0. Sessions 1–5 landed
  `514bd319e..050141173` (session 5: `b93cc8c16`, `5717def5b`, `295172497`,
  `5ecccdeb1`, `e2a587d76`, `050141173`).

## Per-ticket loop
1. `start-work` the ticket (bugs at `confirmed` advance to `fixing` with `--advance`;
   severity field takes critical/major/minor/cosmetic).
2. Read the `[fable-triage]` comment on the ticket — it contains the verified rationale
   and fix plan and OVERRIDES the ticket description. ALSO read any later
   `[fable-fixer session-N]` comments — several tickets carry refreshed, re-verified
   plans that supersede the triage anchors. Re-verify the claim against the current
   tree before coding: FIVE sessions of refactors have moved things. Anything citing
   processor.py line numbers is stale — the scheduler codec, barrier subsystem, AND
   the scheduler drain have all moved out (see prior-session artifacts below).
3. TDD: regression/behavior test first, watch it fail for the right reason, then fix.
   For behavior-preserving refactors, build an extensional parity net (tests proven
   green against PRE-refactor code) before restructuring.
4. Scoped tests + `.venv/bin/ruff check` + `.venv/bin/mypy` on touched files.
5. One commit per ticket: `fix|refactor(engine): <behavior> (<ticket-id>)`.
6. Close gate: transition to `verifying` with `-f root_cause=... -f fix_verification=...`,
   then `close --reason ... --commit "release/0.7.0@<sha>"`.

## Mandatory ordering
- **c49f33d6e4 component (4) — TokenTraversalEngine — is the only remaining slice.**
  Comment 154 on c49 is the resume point (slices 1–3 done: 6291c51766 codec →
  `engine/scheduler_work_codec.py`; e76a186916 barrier → `engine/barrier_coordination.py`;
  session-5 SchedulerDrain → `engine/scheduler_drain.py` @ `b93cc8c16`). Component (4)
  extracts `_process_single_token` + the `_handle_*` family (hottest, most
  invariant-dense code in the engine). START IT ONLY WITH A FRESH CONTEXT, as the
  FIRST work item, and REQUIRE a characterization/parity net proven green pre-move
  (model: tests/unit/engine/test_scheduler_drain_characterization.py and
  tests/unit/engine/test_scheduler_ordering_characterization.py). The
  `SchedulerDrainHost` protocol in scheduler_drain.py is the seam it plugs into —
  after the move, the drain's `self._processor._process_single_token` resolves to the
  new engine's method via the processor delegate; keep that delegate. Closing
  component (4) closes c49 itself. If you cannot give it a fresh-context slot this
  session, leave it in `fixing` and work the P2 list instead.
- core.py: **6630fb3e31 before 9e71ae82a4**. 6630fb3e31 is READY TO IMPLEMENT — its
  comment 169 carries a session-5 re-verified plan (LeaderFollowerDrain extraction,
  refreshed core.py anchors, 8-case TDD list, must-preserve invariants). Re-verify
  line offsets first (sibling commits may have shifted core.py).
- Otherwise: P2 small→large, clustered by file. Remaining P2/P3 pool includes:
  e11c034934 (structlog kwarg collision — easy win), 064b2a91a1 (register overwrite
  fail-fast — easy win), c8499bf602 (from_traversal_context easy win), 222b540be0 +
  81c351f31c + 8451ce94ff (security: dependency paths / commencement env),
  dd68fbf58d (RowWaiter registry delegation), 4993a0374c (Clock port split),
  2d43291212 (coalesce policy evaluator), dca97d5c45 (_execute_merge), b82a5bde00 +
  d1389ac2c1 (executor restore boundary — likely same fix, check for dup),
  5f9903125e (checkpoint policy semantics), 107a29d02e (CheckpointCoordinator
  post-sink callback split), e4f1eb6038 (reconstruct_resume_state), 5c5ebff478
  (WorkItem out of dag_navigator), f6a6ab0a46 (SinkExecutor.write), 7985bebb0b
  (execute_transform subsystem), 6eecae10a6 (P3 node-state double payload),
  e3d1310b93 (P3 restore-before-CAS), b53a093321 (P3 RunExecutionCore),
  d2eff1582a (P3 sink_name derive), 735df9576d (P3 idle-poll helper thread),
  27d7bfc14b (P3 SourceIterationDriver), 896fb00e37 (engine facade exports).
- Ticket-specific rulings (from triage, do not relitigate):
  - 7985bebb0b (execute_transform subsystem): re-scope the ticket FIRST, then fix.
  - 577179bba1 (follower drain policy): carries comment 68 — the transferred part (b)
    of 07b2031e41: follower RowProcessor assembly should route through
    RunExecutionCore.build_processor. The session-5 drain extraction deliberately
    preserved the private `_drain_scheduler_claims` delegate signature and the
    triple-None follower inference (now isolated in
    SchedulerDrainCoordinator.run_maintenance) so this ticket can replace them with
    an explicit ProcessorMode/policy.

## Prior-session artifacts your fix plans may reference
- `engine/scheduler_drain.py` (session 5, `b93cc8c16`) — SchedulerDrainCoordinator
  owns drain_claims (ex `_drain_scheduler_claims`), pending-sink recovery, the four
  disposition arms + membership-fence identities, heartbeat_active_claim + its state,
  run_maintenance + cadence counter, enqueue_work_item. Pure predicates and the
  handoff/require/error helpers are MODULE-LEVEL functions there, shared with the
  processor's flush path. Processor seams are reached through the `SchedulerDrainHost`
  Protocol resolved at CALL time (`self._processor.<seam>`) — patching
  `processor._process_single_token` in tests still works (unlike the barrier
  coordinators' construction-bound callbacks). `_live_barrier_holds` and
  `_pending_branch_losses` are shared BY REFERENCE across processor, barrier intake,
  and the drain — never copy them. Load-bearing delegates left on RowProcessor:
  `_drain_scheduler_claims` (exact keyword signature — orchestrator/follower.py:366 and
  test_adr030_loosened_invariant_guard call it), `_heartbeat_active_claim`,
  `_enqueue_scheduler_work_item`, `_row_result_from_pending_sink`,
  `_take_claim_branch_loss`, `_barrier_key_for_live_hold`.
  MAX_WORK_QUEUE_ITERATIONS / SCHEDULER_MAINTENANCE_INTERVAL are defined there and
  re-exported from processor.py. The "Relinquishing" log emits from logger
  `elspeth.engine.scheduler_drain`. processor.py is now ~4,100 lines.
- `engine/scheduler_work_codec.py` — SchedulerWorkCodec owns the WorkItem<->scheduler
  payload mapping BOTH directions; never hand-derive the scheduler field bundle.
- `engine/barrier_coordination.py` — BarrierIntakeCoordinator (§E.2, typed
  dispositions) + BarrierRecoveryCoordinator (F1 restore). `_LiveBarrierHold` and
  `BarrierJournalRestoreContext` re-exported from processor.py. GOTCHA: the barrier
  coordinator's callbacks ARE construction-bound — tests patching intake-path seams
  must patch `processor._barrier_intake.<seam>`.
- `CheckpointCoordinator._require_fence` (session 5, `295172497`) — every orchestrator
  checkpoint create/delete now REQUIRES bind_coordination with a token whose run_id
  matches; the guard runs AFTER `_checkpoint_gate` (disabled checkpointing stays
  token-free) and BEFORE maybe_checkpoint's sequence increment. Any test driving
  coordinator writes directly must bind a token: `leader_coordination_token(factory,
  run_id)` for real managers, a plain `CoordinationToken(run_id=..., worker_id=...,
  leader_epoch=1)` for mocked ones. Relevant to 107a29d02e and 5f9903125e.
- `TriggerEvaluator` (session 5, `e2a587d76`) — keeps sorted `_member_accept_times`;
  count/condition latches are pure functions of durable arrivals
  (permutation-invariant, Hypothesis-pinned); `_condition_fire_observed` provenance
  flag protects should_trigger observation latches; checkpoint-restored batches have
  an EMPTY member list (recompute guarded on len == batch_count).
- `contracts/schema_contract_factory.expected_runtime_output_contract` (session 5) —
  the ONE ADR-014 statement of expected emitted-contract semantics.
- RowWaiter.wait() raises InterruptedError on shutdown (session 5); the processor's
  InterruptedError arms own the shutdown_requested result.
- `DAGTraversalContext.structural_node_ids`; counter_classification.py TABLE (extend,
  never re-fork); graph_wiring.build_source_id_map / load_edge_map; resume() entry
  guards; disposition verbs take optional `worker_id` (LENIENT fence);
  `RowResult.authoritative_error_hash` (parity-net fakes must mirror new RowResult
  fields); `plugins/sources/_safe_validation_errors.py`;
  `QUARANTINE_ERROR_MAX_CHARS=2000`; `LandscapeExporter(include_raw_error_rows=)`.

## Hard constraints
- Tier-1 fail-closed doctrine: audit-integrity errors propagate; explicit allowlists
  over complement derivations; no silent defaults where null is never correct.
- NEVER touch `ELSPETH_JUDGE_METADATA_HMAC_KEY` or hand-edit a
  `judge_metadata_signature`. Unsigned pending-judge allowlist entries are fine;
  signing is the operator's.
- Correctness beats performance; no unilateral deferral — fix in-session or ask with a
  concrete proposal. Observations are only for defects OUTSIDE your ticket's scope.

## Gates & commit gotchas
- **Full-suite gate at slice boundaries**: plain `pytest tests/` (never `-o addopts=""`),
  runtime ~31 min. Green = EXACTLY 1 failure: `test_allowlist_loader_unification.py::
  test_baseline_capture_is_self_consistent` (operator-keyed baseline recapture; do not
  attempt it). Anything else is a regression to fix or attribute. Session 5 confirmed
  this baseline (2 failed → 1 was fence fixture fallout, fixed `050141173`).
  Known flake: tests/unit/web/composer/test_service.py::
  test_litellm_api_error_is_retried_before_unavailable is order-dependent under full
  runs (observation elspeth-obs-a4d7eb48cf) — re-run isolated before treating as real.
- **Tier-model fingerprints are POSITIONAL**: fp = hash(rule_id | AST path | node dump),
  AST path uses body indices — deleting or moving ANY method shifts the fp of every
  allowlisted finding BELOW it in the same class. Budget an allowlist re-key into any
  processor.py extraction. Re-key discipline: pair old→new fp in SOURCE ORDER within
  each function group; signed entries become UNSIGNED pending-judge (drop all judge_*
  + scope_fingerprint + ast_path; safety note names the ticket; expires 2026-08-31 per
  precedent). Currently keyed in engine.yaml for the drain: engine/scheduler_drain.py:
  R1:SchedulerDrainCoordinator:row_result_from_pending_sink:fp=262c85bcd7de4246;
  R5 budgets: processor.py max_hits 10, scheduler_drain.py 3, barrier_coordination.py 1.
- **Deriving new fingerprints** (session-5 shortcut): drive the rule directly —
  `from elspeth_lints.rules.trust_tier.tier_model.rule import collect_check_result;
  collect_check_result(Path("src/elspeth"), allowlist_path=<shadow-dir>).violations`
  and call `.suggested_allowlist_entry()` on each violation for the exact new key+fp.
- **Verifying a re-key** (the trust-tier lint CRASHES on ~34+ pre-existing judge
  scope-drifts tree-wide, operator-held): copy config/cicd/enforce_tier_model to
  scratch, RECURSIVELY strip all judge*/scope_fingerprint/ast_path fields (the files
  are mixed shapes: some `per_file_rules:` dicts, some `allow_hits:` lists — walk the
  whole structure), run `env PYTHONPATH=elspeth-lints/src .venv/bin/python -m
  elspeth_lints.core.cli check --rules trust_tier.tier_model --root src/elspeth
  --allowlist-dir <shadow>`, and diff findings against the same run on the pre-session
  tree (`git archive HEAD` — mind tar --strip-components: config/cicd/enforce_tier_model
  needs 3, not 2). Zero new findings = reconciled. Session-5 reference: 423 findings
  on both sides, diagnostics byte-identical.
- **Commit with `--no-verify` by DEFAULT** (operator directive 2026-07-04, you and
  any subagents you spawn): skip the pre-commit chain routinely — reconciliation
  is batched ONCE at session end (full unscoped pytest, ruff+format+mypy over the
  session diff, shadow-lint reconciliation, `git diff --stat config/cicd/`), not
  per commit or per ticket. Per-commit hook runs were costing more than the work
  itself. Context on why hooks block anyway: tier-model gate CRASHES on commits
  touching config/cicd/enforce_tier_model/ or the trust_tier rule source
  (pre-existing judge-scope drift, operator-held); Check Contracts fails on 8
  pre-existing dict[str,Any] violations near contract-adjacent files (task
  elspeth-a16b05298a). --no-verify does NOT authorize signing or blessing
  trust-tier/baseline HMAC state — that stays the operator's.
- **Token/walltime economy** (operator directive 2026-07-04 — trim ceremony, never
  evidence): never re-Read processor.py (~4,100 lines) or other large files whole —
  narrow offset/limit Reads or Loomweave (entity_at/entity_source_get/
  entity_callers_list). No subagent fan-out for routine tickets; work them directly,
  single-threaded (subagents only for genuinely parallel mechanical work, pinned
  model:'sonnet'). Per ticket: scoped test file / -k selection only; full unscoped
  pytest exactly ONCE at session end. Calibrate rigor to ticket size — parity-net
  ceremony is for god-class extractions, not the P2 pool (small ticket = triage
  comment → verify vs tree → one focused test → fix → scoped checks → commit
  --no-verify → close). Judge bundle staged ONCE at session end.
- Any allowlist re-key STALES the staged judge bundle — re-stage at session end.
- **Ruff PostToolUse hook strips imports** whose use-site doesn't exist yet at hook
  time. It bit session 4 once and session 5 TWICE (a stripped `import bisect` caused
  a 55-failure wave). Edit the use-site FIRST, then the import — and after any
  multi-message edit sequence, grep that your imports survived. SIM117 blocks nested
  `with`. ruff-format rejects `cat >>`-scripted test edits: run `.venv/bin/ruff format
  <file>` before committing anything written via heredoc/script.
- Editing a PLUGIN file: refresh its `source_file_hash` in place (import helpers from
  scripts/cicd/plugin_hash — no CLI). plugins/infrastructure/base.py carries NO hash
  (verified session 5).
- Cross-cutting pins: `test_config_alignment.py` pins config-model field SETS;
  mock-discipline ratchet counts bare Mock()/MagicMock() per FILE — new test files
  must be zero (SimpleNamespace, object(), specced mocks, `patch.object(...,
  new=<function>)`, or the session-5 `__getattr__`-delegating spy-wrapper pattern in
  test_scheduler_drain_characterization.py). TestReRaiseGuardPattern:
  FrameworkBugError/AuditIntegrityError handlers must be bare `raise`.
- Test-harness facts: `_make_factory()` in test_processor.py registers leader 'seeder'
  via begin_run — begin_run ALWAYS registers a leader (minted if none given), so the
  lenient claim fence is ALWAYS active in recorder fixtures; direct claims by other
  identities need `_register_test_worker(factory, <id>)` first. `_make_processor`
  binds `leader_coordination_token(factory, run_id)` by default (recovery drains need
  it — `terminalize_pending_sinks_with_terminal_outcomes` is a fenced verb).
  tests/e2e/recovery/ (77 tests, ~14s) is the cheap crash-window safety net — run it
  on ANY barrier/resume/checkpoint-adjacent change.

## Session end (also when context runs low: finish the current ticket, then stop)
1. Add a session-summary comment to anything investigated-but-unfixed.
2. Run the full-suite gate; fix or attribute anything beyond the 1-failure baseline.
3. Stage ONE judge bundle AFTER the last commit: `mcp__elspeth-judge__stage_scan` with
   staged_by fable-fixer — it SUPERSEDES session-5's `stage-scan-1f4792096efd`.
   Record the new bundle id and note the supersession chain.
4. Update the memory file `project_fable_panel_triage_2026-07-03.md` (+ its MEMORY.md
   index line): progress counts, new commits, new gotchas, bundle id. NOTE: the file
   is edited by concurrent sessions — re-read before editing if a write conflicts.
5. Skim `observation_list`; dismiss or promote what's yours (3 pending items belong to
   other actors — leave them).
6. Write the session-7 handover to `.claude/handovers/fable-fixer-session-7.md`
   (update this file's pattern) and report: tickets closed w/ commits, suite status vs
   the 1-failure baseline, what the operator owes (fire bundle
   `stage-scan-<new>`, then `ELSPETH_JUDGE_METADATA_HMAC_KEY=...
   .venv/bin/python scripts/cicd/regen_fingerprint_baseline.py --commit`).

You may use ultracode, and all relevant skills and subagents at your discretion (the
session-5 pattern of a parallel read-only recon workflow re-verifying triage plans
against the tree before coding paid off — every warm-up plan was stale in some way).
