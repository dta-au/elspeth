# U-30: retire the empty-state planner route (H2): composer-carries lane

> **RESUME TASK 0 (hung tests) is the release's FIRST priority, owned by u45-firstcompose plus one coordinator-assigned owner. It is NOT this lane's job.** See `.claude/lanes/release-0.8.10/RESUME-FIRST.md`. This lane resumes its own unit, under the guardrails: one pytest at a time, `-n 3` max, `timeout 2400`, both hang files `--ignore`d, no directory runs without a slot. Do not touch the fencing gate, the reserve-contract tests or the pytest config. Maintainer's order, 2026-10-25.

> **Coordinator corrections (10-25, from COLD-START-REVIEW.md). These override the text below:**
> - W7: delete "The brief's `-n 2` already complies": the brief says `-n 3`.
> - I3: the step-1 command's `-n 4` must be `-n 3` under `timeout 2400`, with both hang files ignored.
> - N2: the bucket reports and instruments under `.claude/worktrees/u30-20261025/.claude/lanes/u30/` are copied to `docs/release/0.8.10-handover/instruments/u30/`.


## COLD START (paused 2026-10-25)

### Units, branches, trees
- **U-30**, in progress. Branch `codex/0.8.10-u30` @ `f8fc444c9`, worktree
  `.claude/worktrees/u30-20261025`. Base: `release/0.8.10-assembly` @
  `d7ad18596`. A read-only base tree is at `.claude/worktrees/u30-base-20261025`,
  detached at d7ad18596, for base comparisons.
- Lane notes, logs and instruments live under `<u30 worktree>/.claude/lanes/u30/` (gitignored).
  They are cited below as `L/`.
- U-22 is parked. See the COLD START in `u22-report.md`.

### Commits on the branch (both WIP; squash into coherent green commits before hand-over)
1. `b0fc0e0eb` **wip: source-side retirement.**
   - Deletes `plan_pipeline` and the 128 other planner definitions, plus planner-only
     helpers in capability_skill, planner_authoring_aids, state, provider_quota,
     provider_discovery_response and tools/{_common, generation, wire_projection}.
   - Deletes `PipelinePlanResult.candidate_state`: only plan_pipeline set it, and nothing read it.
   - Boot probe: removes the `planner_tools` and `hatch_terminal` surfaces and their app.py
     consumers. Every `ComposerBootConfigError` is fatal again, and the thinking-route log
     reason is reworded.
   - Removes the hatch route from `ComposerToolContract`, so the `composer_tool_contract_resolved`
     log loses its `hatch_*` fields. This is an operator-telemetry change for the Upgrade notes.
   - New `tests/unit/web/composer/test_planner_route_retirement.py`.
   - Census re-pinned 2738 to 2684; the stale contracts-whitelist rows are removed.
2. `f8fc444c9` **wip: test side, partial.** The validation bucket (21 files) is finished, and
   6 of the 17 guard-bucket files are done. **The suite is red at this commit:** some
   modules still import deleted names.

### Done and verified (evidence)
- **Reachability instrument:** `L/reachability.py`.
  - It is an AST call graph over src/elspeth. A symbol is deletable only when every src
    referrer is deleted too (a fixed point).
  - Positive control: `build_planner_capability_manifest` is found as planner-only, but only
    after I fixed a bug that rooted every `from X import Y`.
  - Negative control: `PipelinePlanResult` keeps a live root chain (`--why`).
  - Output: `L/reach4.txt`. The deleted-name list: `L/deleted-names.json`.
- **Retirement test:** green, 35 passed (`L/retire-green.log`); red before the deletion
  (`L/retire-red.log`). Its controls re-add each retired name in three forms, and each
  surface literal; the finder also has a positive control on a live export.
- **Static checks:** mypy is clean over src/elspeth/web (508 files, `L/mypy1.log`).
  check_contracts is green after the re-pin (`L/contracts3.log`).
- **A9 before (base d7ad18596):** `L/a9-base/exit.txt`.
  - census, tutorial routes, provider='server' rejection and transition ledger all exit 0.
  - `provider_server_src_count=1`.
  - Census ids: `L/a9-base/census-ids.txt`. All 5 transitions pass. The census pins
    per-transition primary counts: authoring 4, source_missing 2+2 Advisor, initial and
    recompose 1+1, followup 1 per turn.
  - Instrument: `L/a9.sh <label>`.
- **Baseline of the ~30 family ids on base:** `L/base-cluster.log`. Exactly 30 fail:
  - fixture_matrix 10, stated_threshold 5, no_tool_semantic 5;
  - repair_and_deferral 3, boot_probe parity 4;
  - schema_mutation 1, TestMessageRoutes 1, compose_service_structure 1.

### In progress (exactly what is half-done)
- **Planner bucket: not started, analysis only.**
  - Plan: `L/planner-tests-report.md`.
  - Files: test_pipeline_planner.py, which is reduced to 5 tests plus a helper spine, with
    the importer list in §5 of that report; the planner_* unit files; and the integration
    files freeform_pipeline_planner, freeform_planner_failure_translation, pipeline_custody
    and composer_against_gateway.
  - Two cautions. Its §0 shows `L/test_reach.py` misses tests that call helpers imported from
    another module. And test_planner_teaching_gate must not be deleted wholesale.
- **Guard bucket: 11 of 17 files not done.** The list and next steps are in
  `L/guard-tests-report.md`, §"Not started" and "Next steps".
- **Not yet started (lead-agent items):**
  - test_boot_probe.py (11 flagged), test_app.py lifespan probe tests (8),
    test_boot_probe_production_parity.py (9). Delete the planner/hatch parity and mutation
    tests; give the loop_tools/advisor parity a mutation control that goes red.
  - the 30 family ids: no_tool_semantic_publication, stated_threshold_data_full_walk,
    fixture_matrix, repair_and_deferral, schema_mutation_controls, TestMessageRoutes
    "legacy planner repair budget", compose_service_structure. These script PLANNER or
    `emit_pipeline_proposal` and are not flagged by the reach tool.
  - Fold "planning_application.py is absent" into test_planner_route_retirement.py, then
    delete test_private_planner_retirement.py.
  - The `composer_planner_repair_budget != 2` Upgrade-notes line (master plan :332), and
    whether that refusal survives the retirement (`provider_cost.py` binds it for the legacy
    v1 lifecycle; it is still live at service.py:1220 and composer_turn.py:1404, refusal at provider_cost.py:146).

### Next step
1. Read `L/planner-tests-report.md`. Apply its plan, then run its files:
   `cd .claude/worktrees/u30-20261025 && PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src .venv/bin/python -m pytest <files> -n 4 -p no:cacheprovider -q -rfE > .claude/lanes/u30/<step>.log 2>&1; echo "exit=$?"`
2. Finish the guard bucket from its report's next steps.
3. Do the lead-agent items above. Then collect the whole affected set:
   `pytest tests/unit/web tests/integration/web --co -q`. There must be no collection errors.
4. Run the A9 after (`L/a9.sh tip`), the gates (masquerade, envelope, reserve reviewed
   contracts, Sessions authority) and the keyless set-diff from d7ad18596 to the tip. Then
   the red-team pass, then squash the WIP into coherent commits.

### Rulings: all resolved by the coordinator except Q2, which goes to the maintainer
- **Q1, ruled 06:40: MOVE.** Put the naming disclosures (`canonical_set_pipeline_schema`,
  `_disclose_runtime_naming_constraints`, tools/_naming_disclosure.py) onto the loop's
  set_pipeline schema. The wire-shape change and its re-pins are accepted. Fix the false
  docstrings that say the check "runs in PRODUCTION". Details are below under "Rulings
  received during the pause".
- **Q2: MAINTAINER QUESTION, still open.** The coordinator recommends parsing the short form at
  the set_pipeline Tier-3 boundary into the owned row (ADR-032). The alternative is to teach the
  full form in the skill and delete the canonicalizer. Evidence for either: A9 per-transition
  provider-call counts. Leave both chains in place until the maintainer rules.
- **F1–F4, ruled IN U-30 SCOPE: fix them all.** No remnant of the planner route survives:
  - F1: the validation guidance in `tools/generation.py` (around 792-818, 1035-1036, 1111-1135)
    teaches `connectivity` facts that nothing emits any more. Remove that teaching, keeping the
    branches that do apply ("call get_pipeline_state...").
  - F2: remove the "PROPOSAL PLANNER SURFACE" rule from `planner_authoring_aids.py:389-396`,
    which enters every loop prompt through `prompts.py:271`, and update its wording-pin test in
    test_planner_authoring_aids.py.
  - F3: remove the stale `emit_pipeline_proposal` docs at `skills/pipeline_composer.md:393` and
    `docs/reference/composer-tools.md:616`. The skill edit moves the skill hash.
  - F4: remove the dead `"emit_pipeline_proposal"` branches in `credential_guard.py:264`,
    `visible_tool_event.py:130/138/301` and `diagnostic_evidence.py:201/345/469/475/487`.
    Also remove the stale justification text in `enforce_tier_model/web.yaml:5167, 5177`
    (keyless set-diff; never hand-edit signatures).
  - **F2 and F4 change the live prompt and the visible_tool_event path,** so they need A9
    per-transition provider-call counts before and after (`L/a9.sh`; the base is recorded in
    `L/a9-base/`). Report the prompt-hash and skill-hash churn as an honest obligation.
- **A9 tutorial.spec.ts, ruled.** Never run it in a linked-.venv worktree: `uv run` deletes the
  MAIN venv through the symlink (the 10-08 incident). Two ways to run it:
  1. Give the worktree its OWN venv: `rm <worktree>/.venv` (the symlink only), then, inside
     the worktree, `uv venv` and `uv sync --frozen --all-extras`.
  2. Run the spec from a dedicated worktree that has a real venv.

  Before Playwright starts, check that `test ! -L <worktree>/.venv` succeeds. Note: u30-20261025
  and u30-base-20261025 both have symlinked `.venv` today.

### Hazards
- The reach tools are AST heuristics with known blind spots: cross-module test helpers, and
  dynamic or string dispatch beyond same-module names. Treat their output as candidates,
  and confirm by reading the code and by collection.
- U-22's seam-2 commits edit `tools/_common.prepare_bounded_pipeline_state`, which U-30
  deletes. Rebasing either branch over the other will conflict there; the deletion wins.
- KNOWN HANG: test_web_landscape_mutation_fencing.py. Exclude it from directory runs.
- Removing `planner_tools` drops the only boot probe that exercised the Anthropic
  extended-thinking route. The loop probe is capped at 16 tokens. Report this coverage change.
- Two maintainer-approved xfails exist (sessions writer-review, lints baseline). Never touch them.
- The tier-model allowlist (`config/cicd/enforce_tier_model/web.yaml`) still has rows for the
  deleted planner code. Signing is suspended. Find them with the keyless set-diff, and never
  hand-edit signatures.

### Landing conditions still owed
- F1–F4 fixed in U-30 (ruled in scope), with A9 counts before and after for F2/F4 and the prompt-hash and skill-hash churn reported.
- A9 `tutorial.spec.ts` exit code, run only from a worktree with its own (non-symlinked) `.venv`.
- Q1 move done (the disclosures on the loop's set_pipeline, docstrings fixed, gate re-pins); Q2 per the maintainer's answer.
- Landing rule (target AGENTS.md § Test Verification Policy): squash the WIP into commits
  that keep green tests green; ids introduced must pass at their commit.
- Keyless set-diff d7ad18596 to the tip; A9 after with per-transition counts; red-team on the
  diff; the Upgrade-notes lines (repair_budget refusal, `hatch_*` log fields, probe surfaces).
- Gates: masquerade, tool-result envelope, reserve reviewed contracts, Sessions authority,
  check_contracts.

### Rulings received during the pause (record only; implement at cold start)
- **Q1 (lead, ruled): MOVE the naming disclosures onto the loop's set_pipeline definition.** Do
  not delete them.
  - The runtime enforces charset, length, reserved edge labels and sink lowercasing, so the
    production tool must disclose them. Disclosure is validation information, not
    server-authored structure.
  - The wire-shape change and its gate re-pins are accepted.
  - Fix the docstrings that claim the check "runs in PRODUCTION" (`tools/schema_contract.py`
    ~531 and ~587, and the `canonical_set_pipeline_schema` comments).
  - Then `capability_skill.py:39` and the capability-doc versus schema CI check compare against
    the loop's set_pipeline schema.
- **Q2: a question for the MAINTAINER, not yet ruled.** It touches the rootless/tutorial
  chokepoint. Leave both chains in place until it is answered.
  - Option A (the lead recommends it): parse the short form `{kind, user_term, draft}` at the
    production set_pipeline boundary into the owned full row, assigning the id and `pending`
    status. Model output is Tier 3, so this is ADR-032 parse-at-the-boundary, not synthesis.
    The candidate builder then sees only the owned form, and the skill and the tool agree on
    one contract. Ships with a red-first test of the short form through `_execute_set_pipeline`.
  - Option B: teach the full form in the skill (`skills/pipeline_composer.md`) and delete the
    canonicalizer, which costs one repair round-trip whenever the model uses the short form.
  - Evidence either option needs: A9 per-transition provider-call counts before and after
    (`L/a9.sh`), plus, for B, the skill-hash change.

### Answers owed to the bucket agents (decided by me as the U-30 owner)
- **Surviving test_pipeline_planner.py helpers.** These are the pure SDK-double spine, and they
  stay:
  - `_ScriptedCompletion`, `_Response`, `_Choice`, `_Message`, `_ToolCall`, `_Function`;
  - `_response`, `_text_response`, `_response_with_call_id`, `_planner_usage`;
  - `_empty_state`, `_budget`, `_pipeline`;
  - plus `_candidate_context`, `_bad_source`, `_INJECTED_PLUGIN` and `_INJECTED_CLAUSE` for the
    5 kept tests.

  Decision: move the spine into a neutral helper module, `tests/unit/web/composer/_scripted_sdk.py`,
  and repoint every importer in the same commit, so live tests no longer import from a file
  named for the retired route. test_pipeline_planner.py then holds only its 5 live tests.
  Rename it after its content (candidate identity and budget-policy carriers) if that stays
  clear.
  - Deleted with the route: `_plan`, `_model`, `_custody`, `_lifecycle`, `_origin` and
    `_session_context`. They build deleted types.
- **test_pipeline_planner_protocol_rejections.py is RETIRED.** Its 12 flagged tests and the
  unflagged over-cap batch test, which uses `_plan`, all drive the planner. Move
  `_cut_off_response` and `_length_stopped_tool_response`, which test_private_received_diagnostics.py
  needs, into `_scripted_sdk.py` first.
- **Cross-bucket collection fallout.** These must be fixed in the SAME commit as the helper
  deletion: no commit may break collection of a passing id. Ten files import unsurvivable helpers
  at module level:
  - test_information_boundary_recovery, test_model_discovery_scope;
  - test_operation_authority_producers (2 live tests that need only `_empty_state`);
  - test_planner_marker_recovery;
  - test_private_received_diagnostics (25 live tests);
  - test_prompt_guard_canonical_containers, test_prompt_guard_json_boundaries;
  - test_provider_context_planner_integration, test_provider_quota_end_to_end;
  - test_session_tool_prompt_guard_projection.

  For each, delete the tests that drive `_plan`, and repoint the live ones to `_scripted_sdk`.
- **Other gate pins in the same commit:**
  - Deleting `test_planner_has_no_request_selected_terminal_contract` leaves stale allowlist rows
    at `tests/unit/test_no_hasattr_branching.py:185-186`, and that gate then fails at `:272`.
    Remove the rows.
  - `tests/unit/web/composer/test_tool_result_envelope_gate.py:118-125` has a stale off-surface
    entry. Remove it.
- **test_app.py lifespan probe tests (my bucket).**
  - `:2478` and `test_hatch_terminal_rejection_is_nonfatal` (about :2485-2537) still pin
    `planner_tools`, `hatch_terminal` and `composer_boot_probe_rejected_nonfatal`. Delete the
    hatch-nonfatal test, because app.py now re-raises every `ComposerBootConfigError`.
  - Port the per-role endpoint, shared-deadline and timeout tests to the two surfaces,
    `loop_tools` and `advisor`.
- **Unattributed.** issue327-light has 5 test_pipeline_planner.py failures on its rebased tree.
  These are probably planner-route tests on a tree without U-30. Attribute them at cold start by
  running those ids at d7ad18596 in `u30-base-20261025`. If they fail there too, they belong to
  this unit.
- **Bucket state at the pause.** The subagents are stopped: all three ended, and no process has
  a cwd in either u30 tree (checked).
  - Validation: done, committed in f8fc444c9. Report: `L/validation-tests-report.md`.
  - Guard: 6 of 17 files done, committed in f8fc444c9. Report: `L/guard-tests-report.md`.
  - Planner: analysis only, no edits. Report: `L/planner-tests-report.md`, §0 and §5.
- **The capacity rules now bind** (AGENT-BRIEF.md "TEST CAPACITY — HARD RULES"):
  - one pytest per lane, at most `-n 3`, `timeout 2400`;
  - directory runs need a slot;
  - `--ignore` both the fencing file and test_web_reserve_reviewed_contracts.py.

  The `L/a9.sh` runs and the bucket commands above must follow them. The brief's `-n 2` already
  complies.
