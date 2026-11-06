# planner-tests report: U-30 test retirement, planner bucket (PAUSED before any edit)

Status: **analysis done, no file edited, no file deleted.** I stopped on the coordinator's pause
order (`.claude/lanes/release-0.8.10/PAUSE-PROTOCOL.md`), which arrived before
my first edit. Every decision below is a PLAN, not an applied change. The 11 modified files that
`git status --short` shows in this worktree belong to the other two test agents. None of them is in
my bucket, and I did not stage or commit them.

Evidence files (lane dir `.claude/lanes/u30/`):
- `planner-tests-reach-tpp.txt`: `test_reach.py` output for test_pipeline_planner.py (242 of 247 flagged)
- `planner-tests-nonplan.txt`: the 68 flagged tests there that do not go through `_plan`
- `planner-tests-reach-others.txt`: `test_reach.py` output for the other 12 bucket files
- helper-needs scan (script in my scratchpad, output reproduced in section 5)

## 0. Reach-tool blind spot (affects every count below)
`test_reach.py` does not flag a test that calls a dead helper **imported from another module**.
`_plan`, `_model`, `_custody`, `_lifecycle`, `_origin` and `_session_context` all build deleted types,
but they are not local defs and not in deleted-names.json. Misses I confirmed by reading:
- test_planner_graph_recovery.py::test_mixed_recovery_batch_is_rejected_before_dispatch (calls `_plan`, patches `pipeline_planner.execute_discovery_tool_with_context`)
- test_planner_conversation_history.py::test_assistant_history_obeys_the_request_byte_limit_before_dispatch (calls `_plan`)
- test_freeform_pipeline_planner.py around line 1057 (`await _plan(` inside a test the tool did not flag)
- test_pipeline_planner_protocol_rejections.py::test_over_cap_batch_reaches_the_tool_call_cap_disposition (the only unflagged test in that file; it uses `_plan`)

Next step: grep each bucket file for `_plan(`, `_model(`, `_custody(`, `_lifecycle(`, `_origin(`,
`_session_context(`, `_direct_manifest_case(`, `_terminal_with_sibling(` and `_TERMINAL`, and
combine the hits with the tool's output.

## 1. Planned decisions

### tests/unit/web/composer/test_pipeline_planner.py (11,038 lines): reduce to 5 tests plus a helper spine
The module imports 20 deleted names at module level, so today it fails at import. Every file that
imports from it fails too.

KEPT (unflagged; read in full; all exercise live code):
| test | reason |
|---|---|
| test_state_aware_canonicalization_uses_trusted_existing_source_and_node_ids | `canonicalize_authored_node_review_requirements` is a held chain (brief) |
| test_budget_policy_is_frozen_slotted_and_rejects_non_decimal_cost | `PlannerBudgetPolicy` is a surviving carrier |
| test_tool_result_envelope_omits_data_without_traversal | `ToolResult.to_dict`, live |
| test_every_producer_records_the_identity_it_resolved | `build_set_pipeline_candidate`, live (needs `_candidate_context`, `_bad_source`, `_empty_state`) |
| test_the_secret_ref_placement_producer_records_its_identity_too | same; needs `_INJECTED_CLAUSE`/`_INJECTED_PLUGIN` |

DELETE (planned), by category. The per-test names are in `planner-tests-reach-tpp.txt`:
- **~174 tests that go through `_plan`** (→ `plan_pipeline`): the planner request lifecycle, discovery palette, escape hatch, repair loop, nudge budget, manifest, custody settlement, deadline, retry and settlement. These pin retired behaviour. The shared components they also touch have their own production-route tests. Cite these when writing the final rows:
  - cache markers: test_provider_cache_markers.py, test_boot_probe.py::test_anthropic_routes_mirror_the_production_cache_markers
  - reasoning effort: test_reasoning_kwargs.py, test_boot_probe.py::test_loop_and_planner_requests_use_their_own_reasoning_efforts
  - cost: test_llm_response_parsing_azure_cost.py, test_cost_usage_validation.py
  - malformed and empty responses: test_compose_loop_llm_audit.py::test_empty_choices_records_malformed_response
  - provider-served metadata: test_llm_provider_served_audit.py
- **`_allowlisted_candidate_feedback` / `_FinalizerOwnedRefs` / `_entry_component_ref` / `explain_withheld_validation_code` / `_feedback_error_codes` / `_rejection_fingerprint` / `_withheld_component_count` / `_structural_schema_violations` / `_canonical_schema_feedback` / `argument_error_response`** (~30 tests): the planner's repair feedback, which is retired. The two tests with live sub-assertions are already covered elsewhere:
  - test_gate_on_error_repair_feedback_carries_sink_connectivity_and_guidance: covered by test_state.py:2828 (`gate_on_error_unknown_sink` from `state.validate()`) and test_tools.py::test_gate_on_error_unknown_sink_teaches_node_level_policy
  - test_aggregation_on_error_repair_feedback_carries_sink_connectivity_and_guidance: covered by test_state.py:2887 and test_edge_route_reconciliation.py::test_aggregation_on_error_unknown_sink_is_error
- **Planner palette, manifest and information classes** (test_planner_palette_is_pinned_read_only_and_terminal_schema_is_exact, test_whole_attachment_discovery_is_audited_as_blob_content, test_unknown_planner_information_prefix_is_refused, test_pipeline_information_semantic_dominance_is_directional, test_blob_discovery_information_is_operation_specific_and_order_sensitive, test_discovery_policy_rejects_unknown_aid_supplied_keys_on_direct_construction, test_planner_has_no_request_selected_terminal_contract): retired.
- **`_parse_response_tool_calls`** (7 tests), **`_serialize_provider_discovery_result`** (3), **`_candidate_shape_hash`** (2), **`_closed_planner_rejection_codes`**, **`_derive_finalizer_owned_refs`**, **`_await_custody_settlement`** (2), **`_settle_lifecycle`** (2): planner internals, retired.
- **`_model` tests** (11: the escape-hatch and endpoint pair validation of `PlannerModelConfig`, plus the pricing identity): retired type.
- **TestRoutingProseAdmission** (3 tests, through `_assert_provider_authored_fanout_admitted` → `_plan`): planner route. Before writing the final row, check whether test_freeform_planner_failure_translation.py::test_provider_can_answer_without_graph_mutation_for_any_request_wording covers "any wording reaches the loop without a server verdict", and cite it if so.
- test_planner_physical_helper_refuses_missing_custody_even_with_live_fence (line 5244): DELETED-as-covered by test_provider_quota_end_to_end.py::test_custody_free_gateway_refuses_before_admission_or_callback. That test pins the same `provider_gateway._litellm_acompletion` refusal ("requires exact explicit custody").

Helper spine to keep: `_Function`, `_ToolCall`, `_Message`, `_Choice`, `_Response`, `_ScriptedCompletion`, `_planner_usage`, `_response`, `_response_with_call_id`, `_text_response`, `_empty_state`, `_budget`, `_candidate_context`, `_bad_source`, `_INJECTED_PLUGIN`, `_INJECTED_CLAUSE` (plus `_TEST_SESSION_ID` if `_candidate_context` needs it). Delete everything else, including `_pipeline` unless an in-bucket survivor turns out to need it. (My message to main listed `_pipeline` as kept; the scan in section 5 shows no live outside test needs it.)

### Other bucket files
| file | planned decision |
|---|---|
| test_pipeline_planner_protocol_rejections.py | DELETE the file: all 13 tests are on the planner route (12 flagged, plus test_over_cap_batch… through `_plan`). Outside importer: test_private_received_diagnostics.py:14 and :720 (`_cut_off_response`, `_length_stopped_tool_response`), used only by that file's dead planner tests. |
| test_planner_strict_dialect.py | DELETE the file: all 10 tests pin the planner palette and dialect stamping (`planner_tool_definitions`, `stamp_planner_terminal`, `PlannerDiscoveryPolicy`). |
| test_planner_review_reference_repair.py | DELETE the file: 4 tests on `_allowlisted_candidate_feedback`, 1 on `_plan`. |
| test_planner_capacity_and_attachment_clarification.py | DELETE the 3 flagged tests (planner route). test_deployed_capacity_environment_override_retains_transport_cost_and_repair_controls was NOT read yet; read it before deciding. |
| test_planner_conversation_history.py | DELETE 3 (two flagged, plus the byte-limit test through `_plan`); KEEP test_assistant_question_is_context_without_becoming_a_user_request and test_unmarked_user_text_and_tool_results_do_not_become_intent_authority (live `request_history.authoritative_conversation_context`). Drop the test_pipeline_planner and `PlannerDeclined` imports. |
| test_planner_graph_recovery.py | DELETE the file: all 5 tests go through `_plan` or `PlannerHandoff`. |
| test_planner_teaching_gate.py | TRIM. The detail-key half stays live, because `ValidationEntry.to_dict` (state.py:1614-1624) ships `contract`, `row_union_schema` and `coalesce_union_type` on the loop's ToolResult, and test_tool_result_envelope_gate.py:1816-1821 explicitly defers those sub-keys to this gate. The connectivity half has no producer left (see FINDING 1). Plan: drop `_route_destination_shapes`, `_connectivity_sites`, the `pipeline_planner` and `route_destination_fact_keys` imports, DELETE test_route_destination_facts_emit_exactly_the_three_pinned_shapes and test_connectivity_sites_enumerate_every_coalesce_reachability_key, and KEEP test_every_shipped_fact_key_is_taught_or_fenced and test_fence_entries_are_live_untaught_keys (they now cover detail sites only). PORT test_gate_catches_prose_that_stops_naming_a_taught_key to a detail key that is taught today; verify the target first. |
| test_private_planner_retirement.py | DELETE the file (brief). |
| integration/test_freeform_pipeline_planner.py | DELETE the 2 flagged `_direct_manifest_case` tests and the `_plan` test near line 1057 (still to be identified); the rest are compose-loop tests. Keep `_pipeline` and `_routing_terminal_response`: test_common_mutation_authority*.py (unit and integration), test_common_mutation_authority_cancellation.py and test_stated_threshold_data_full_walk.py import them. Then check `_routing_terminal_response` for the emit_pipeline_proposal name. |
| integration/test_freeform_planner_failure_translation.py | KEEP all 7 flagged tests. They are compose-loop tests flagged only on the `"emit_pipeline_proposal"` string: negative `not in names` assertions, and `_valid_pipeline_completion`, whose tool name every caller overwrites with `set_pipeline` or ignores. TRIM: delete the module-level `from ...pipeline_planner import _PROSE_NUDGE_BUDGET` (line 42), which breaks collection of all 22 tests, and the unused `_malformed_completion` (lines 137-178), its only user. Consider renaming `_valid_pipeline_completion`'s tool to `set_pipeline`, but first check what test_common_mutation_authority*.py import from this file. |
| integration/test_pipeline_custody.py | Probably DELETE test_cancelled_planner_leaves_blocked_custody_to_settle_exactly_once (`_await_custody_settlement`). First check whether test_freeform_pipeline_planner.py::test_freeform_matching_provider_cancellation_persists_once_without_self_cause or ::test_cancellation_preserves_actual_common_or_pending_proposal_lifecycle covers the loop-side "settles exactly once" contract. |
| integration/test_composer_against_gateway.py | test_sessionless_gateway_boot_probe_is_unqualified_without_dispatch is flagged only on `'planner_tools'`. Likely TRIM of the planner-probe assertions. Not yet read. |

## 2. Files to delete (planned, none deleted)
test_pipeline_planner_protocol_rejections.py, test_planner_strict_dialect.py, test_planner_review_reference_repair.py, test_planner_graph_recovery.py, test_private_planner_retirement.py (all under tests/unit/web/composer/).

## 3. FINDINGS (production code; not touched)
1. **The guidance teaches facts nothing ships any more.** `src/elspeth/web/composer/tools/generation.py:792, 814, 1035-1036, 1111-1115, 1122-1124, 1133` tell the model that rejections carry `'connectivity'` facts (`dangling_on_success`, `declared_sinks`, `consumable_connections`, `unreachable_branches`). Their producers (`route_destination_facts`, `coalesce_reachability_facts`, `_allowlisted_candidate_feedback`) were deleted in b0fc0e0eb. Measurement: `grep -rln "\"connectivity\"\|'connectivity'\|route_destination_facts\|coalesce_reachability_facts" src/elspeth` hits only generation.py, and only inside guidance prose. The compose loop's `explain_validation_error` now describes a payload that never arrives. Lines like 1115 ("Only without connectivity facts: call get_pipeline_state...") are now the only applicable branch.
2. **Dead branches for the retired tool name remain in src**: `credential_guard.py:264`, `visible_tool_event.py:130, 138, 301`, and `diagnostic_evidence.py:201, 345, 469, 475, 487` still special-case `"emit_pipeline_proposal"`. These are harmless but dead. They are for the source owner to decide.
3. Stale justification text in `config/cicd/enforce_tier_model/web.yaml:5167, 5177` cites test_freeform_planner_failure_translation.py test ids, one of them already non-existent (`…_on_both_surfaces`). This is config, so I report it only.

## 4. Final run
**Not run.** I made no edits, and the pause protocol forbids starting new test runs.

## 5. Cross-bucket consequences (for composer-carries to decide)
- **Module-level imports of unsurvivable helpers** (`_plan`, `_model`, `_custody`, `_lifecycle`, `_origin`, `_session_context`). These files will fail collection once test_pipeline_planner.py is reduced: test_information_boundary_recovery, test_model_discovery_scope, test_operation_authority_producers (its 2 live tests need only `_empty_state`), test_planner_marker_recovery, test_private_received_diagnostics (25 live tests), test_prompt_guard_canonical_containers, test_prompt_guard_json_boundaries, test_provider_context_planner_integration, test_provider_quota_end_to_end, test_session_tool_prompt_guard_projection.
- **Live outside needs found by the helper scan**:
  - test_prompt_guard_producer_parts (`_ScriptedCompletion`, `_response`, `_text_response`)
  - test_prompt_guard_wiring::test_common_primary_preserves_guard_identity_after_prior_calls (`_ScriptedCompletion`, `_response_with_call_id`)
  - test_provider_context_planner_integration::test_nonempty_compose_sends_current_amended_parts_through_real_provider_boundary (`_ScriptedCompletion`, `_response`)
  - test_operation_authority_producers (2 tests, `_empty_state`)
- **Stale allowlist**: deleting test_planner_has_no_request_selected_terminal_contract leaves `tests/unit/test_no_hasattr_branching.py:185-186` stale, and that gate fails on stale entries (line 272-273).
- test_tool_result_envelope_gate.py:118-125 names `("pipeline_planner.py", "_serialize_provider_discovery_result")` as an off-surface data site "pinned in test_pipeline_planner.py". The function is deleted, so that entry is stale on the source side regardless of this bucket.
- test_static_direct_writers.py:1410 names test_freeform_pipeline_planner.py as a path. Re-check it if that file's writer sites change.

## COLD START (paused 2026-10-25)
- Unit: U-30 test side, planner bucket (agent name planner-tests). Dispatched by composer-carries, which owns U-30.
- Worktree `.claude/worktrees/u30-20261025`, branch `codex/0.8.10-u30` @ b0fc0e0eb (source side). Base for comparison: `.claude/worktrees/u30-base-20261025` @ d7ad18596.
- Done: analysis only (sections 0-5). Evidence: the reach files listed at the top.
- In progress: nothing half-done. No edits, no WIP commit from me (I own no changed paths; the 11 modified test files belong to the sibling agents).
- Next step:
  1. Get composer-carries' decision on the helper keep-list and the cross-bucket imports.
  2. Run the blind-spot grep from section 0.
  3. Read the three unread tests: capacity test_deployed_capacity…, test_composer_against_gateway, test_pipeline_custody.
  4. Apply section 1 with Edit or `rm`.
  5. Lint the touched files: `.venv/bin/ruff check <files> && .venv/bin/ruff format <files>`.
  6. Run `cd .claude/worktrees/u30-20261025 && PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src .venv/bin/python -m pytest <bucket files> -n 2 -p no:cacheprovider -q -rfE > .claude/lanes/u30/planner-tests-final.log 2>&1; echo "exit=$?"`, then compare remaining reds on the base.
- Rulings owed:
  - composer-carries: the helper keep-list and the outside module-level imports; the test_no_hasattr_branching.py:185-186 entries.
  - Source owner: FINDING 1 (connectivity guidance) and FINDING 2.
- Hazards: `test_reach.py` under-reports (section 0). `"emit_pipeline_proposal"` string hits in the failure-translation file are false positives (negative assertions). test_planner_teaching_gate must not be deleted wholesale, because the envelope gate defers detail keys to it.
- Processes left: none (I launched no pytest).
