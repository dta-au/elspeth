# U-30 test-side retirement: validation-tests bucket report

Worktree: `.claude/worktrees/u30-20261025` (branch `codex/0.8.10-u30`, HEAD b0fc0e0eb).
No git state changes were made and nothing under `src/` was edited. Only the 21 bucket files were touched. Edits used the Edit tool, with two exceptions. `test_model_discovery_scope.py` was regenerated with Write, which AGENTS.md allows. `test_tool_result_variant_teaching.py` got one exact-string Python replace that has the same effect as an Edit.

Reach tool before: 78 of 852 test functions flagged across 21 files (`validation-tests-reach.txt`).
Reach tool after: 0 of 825 test functions reach deleted names (`validation-tests-reach-after.txt`). The totals were summed by awk over the `== … N of M tests` lines.

Dead-helper scan: for every module-level `_name` def or class in the 21 files, count its occurrences in the file, then grep `tests/` for outside users. Only one hit: `test_edge_route_reconciliation.py::_error_codes` is used once in-file and nowhere else. It was already dead on base (`u30-base-20261025` file line 161, same single occurrence), so my edits did not orphan it, and I left it alone. No helper was orphaned by these edits. That one hit also shows the scan can find a positive.
Control: a grep of the bucket for `emit_pipeline_proposal|plan_pipeline|planner_tools|hatch_terminal|PLANNER_|test_pipeline_planner|pipeline_planner|_allowlisted|coalesce_reachability|route_destination_fact` now hits only `test_every_allowlisted_keyword_is_a_shape_failure`, an unrelated jsonschema-keyword test. Before the edits the same grep matched every flagged import.

Ported guidance assertions use the live surfaces:
- `ToolResult.validation_guidance`, filled by `tools/_dispatch.py:724 _augment_with_validation_guidance`, for dispatch paths;
- `generation.build_validation_guidance(codes)` / `explain_validation_code(code)` for direct builder or state paths;
- `ToolResult.to_dict()` / `discovery_cache.serialize_tool_result` / `serialize_admitted_discovery_result` for the wire.

## 1. Decisions

| file::test | decision | reason / surviving contract |
|---|---|---|
| test_validation_error_codes.py::TestPlannerFeedbackCarriesStructuralFacts::test_allowlisted_feedback_projects_contract_facts | PORTED → `TestRejectionCarriesStructuralFacts::test_tool_result_wire_carries_contract_facts_and_inline_guidance` | `ToolResult.to_dict()` carries the `contract` detail (state.py:1620), and `build_validation_guidance` resolves `schema_contract_violation` |
| …::test_allowlisted_feedback_codeless_entry_still_names_a_code | DELETED | planner-only `validation_error` placeholder projection |
| …::test_full_replacement_rejection_excludes_saved_findings_from_feedback_and_trail | TRIMMED → `test_full_replacement_rejection_excludes_saved_findings` | the candidate refusal and its normalised form carry exactly one owned entry. The wire entry carries the code, which resolves in the catalogue. The planner projection and the attempt-trail checks were cut |
| …::test_validated_candidate_rejections_pass_through_ungated | DELETED | planner gating plus a monkeypatched `planner_module.coalesce_reachability_facts` |
| …::test_rejection_trail_codes_never_empty_when_entries_exist | DELETED | planner per-attempt trail (`_candidate_rejection_codes`) |
| …::TestCoalesceReachabilityFacts::test_reachability_facts_name_unreachable_pairs_and_produced_connections | DELETED | `coalesce_reachability_facts` was removed from src; no live producer remains (see FINDING F1) |
| …::test_produced_connections_are_sorted_on_a_fixture_authored_out_of_order | DELETED | same |
| …::test_reachability_facts_handle_list_form_branches | DELETED | same |
| …::test_identity_branch_never_carries_a_sink_lure | DELETED | same |
| …::test_unreachable_branches_follow_authored_order_not_sorted_order | DELETED | same |
| …::test_sink_lure_attribution_follows_transform_chains_and_skips_non_sink_dangles | DELETED | same |
| …::test_reachability_facts_empty_for_correctly_wired_coalesce | TRIMMED → `test_correctly_wired_coalesce_is_valid` | positive control: a correctly wired fork/coalesce validates |
| …::test_allowlisted_feedback_projects_connectivity_facts | DELETED | planner `connectivity` projection; the facts no longer exist |
| …::test_route_destination_entries_ship_only_their_own_fields_keys | TRIMMED → `test_doubly_dangling_transform_reports_both_route_codes` | a transform whose on_success and on_error both dangle raises both closed codes |
| …::test_allowlisted_feedback_omits_connectivity_for_other_codes | DELETED | planner projection |
| (class renamed) TestCoalesceReachabilityFacts → TestRouteReachabilityCodes; module docstring and the `test_review_reconciliation_failed_is_closed_and_actionable` docstring reworded | KEPT | `test_orphaned_coalesce_rejects_with_the_single_observed_code` unchanged |
| test_validation_guidance_catalogue.py::test_real_quarantine_producer_has_direct_repair_guidance | PORTED | `build_validation_guidance(state.validate() codes)` gives the direct record's explanation and fix |
| …::test_real_producer_witness_detects_missing_direct_index_record | KEPT (unchanged; relies on the test above) | the witness still goes red: deleting the index record makes `explain_validation_code` return None |
| …::test_real_count_rejection_reaches_planner_with_mode_choice_guidance | PORTED → `test_real_count_rejection_carries_mode_choice_guidance` | the dispatch `result.validation_guidance` carries the direct mode-choice record |
| …::test_real_diff_without_baseline_retains_planner_guidance | PORTED → `test_real_diff_without_baseline_has_catalogue_guidance` | the first error is `diff_baseline_unavailable` and the catalogue explains the baseline. The standing state findings now follow it; the planner used to filter them out |
| …::test_real_coalesce_rejections_retain_planner_guidance (×3) | PORTED → `test_real_coalesce_rejections_have_inline_guidance` | `build_validation_guidance` covers each coalesce code |
| test_set_pipeline_candidate.py::test_candidate_path_refusals_keep_server_roots_out_of_tool_history_and_planner | TRIMMED → `…_out_of_tool_history` | server root absent from `to_dict()` and `serialize_tool_result`; code and subject unchanged |
| …::test_authored_plugin_option_detail_remains_useful | PORTED | authored option detail reaches the live wire (entry message and `serialize_tool_result`) |
| …::test_candidate_retry_policy_classifies_only_unbounded_sequential_queries | PORTED | `build_validation_guidance(["llm_retry_budget_policy"])` names "30 seconds" and "pool_size > 1" |
| …::test_uploaded_observed_guarantee_rejection_has_repair_guidance | TRIMMED | the planner tail duplicated the existing `build_validation_guidance` assertions |
| test_source_rejection_contracts.py::test_source_path_is_safe_in_response_history_redaction_and_planner | TRIMMED + PORTED → `…_in_response_history_and_redaction` | canary absent from response, history and redaction; on dispatch, `validation_guidance` carries the fix |
| …::test_source_prevalidation_retains_exact_tuple_code_identity_and_authored_detail | TRIMMED | code, identity and authored detail are in the entry message; plugin_schemas on dispatch |
| …::test_source_profile_prevalidation_keeps_distinct_owned_tuple_code | TRIMMED | canary absent from `to_dict` and `serialize_tool_result` |
| …::test_source_credential_refusal_retains_repair_without_literal | TRIMMED | literal absent from the live wire; repair payload unchanged |
| …::test_plural_refuses_whole_list_without_reads_or_partial_mutation | TRIMMED | the dispatch `validation_guidance` assertion was already present |
| test_secret_rejection_contracts.py::test_public_context_refusal_attaches_only_mutation_guidance | TRIMMED (via `_assert_discovery_failure`) | the admitted discovery failure wire is unchanged; the planner-serializer equality was cut |
| …::test_readonly_dispatch_context_refusal_is_not_metadata_success | TRIMMED (same helper) | same |
| …::test_availability_discovery_remains_successful_metadata | PORTED | `serialize_admitted_discovery_result(admitted)["data"]` equals the expected metadata |
| …::test_restricted_feedback_keeps_static_repair_and_omits_raw_diagnostic | PORTED → `test_restricted_refusal_carries_static_repair_guidance` | dispatch `validation_guidance["codes"]["unauthorized_secret_ref"]` equals the static catalogue pair. The "omits raw diagnostic" check was planner-only: the live message carries model-authored names by design |
| test_transform_rejection_contracts.py::test_transform_refusal_retains_saved_findings_code_subject_and_guidance | TRIMMED | the saved-findings and dispatch `validation_guidance` assertions are kept |
| …::test_splice_replay_conflict_keeps_saved_state_and_specific_guidance | PORTED | the catalogue fix contains "identical"; on dispatch `validation_guidance` carries it verbatim |
| …::test_transform_profile_outage_retains_distinct_owned_tuple | TRIMMED | canary absent from `to_dict` and `serialize_tool_result` |
| …::test_transform_provider_path_is_static_in_response_history_redaction_and_planner | TRIMMED → `…_in_response_history_and_redaction` | canary absent from response, history and redaction |
| …::test_transform_literal_credential_refusal_retains_complete_repair_payload | TRIMMED | literal absent from the live wire |
| …::test_splice_preserves_new_counterfactual_errors_before_standing_findings | TRIMMED | the normalised error ordering is kept |
| …::test_splice_codeless_boundary_is_preserved_and_planner_refuses_to_invent_code | TRIMMED → `test_splice_codeless_boundary_is_preserved` | the codeless forwarded entry survives normalisation (see F5) |
| test_rejection_operations.py::test_feedback_compiler_refuses_unowned_variant | DELETED | planner `_compile_planner_feedback` |
| …::test_feedback_compiler_keeps_schema_and_argument_wire_distinct | DELETED | planner internals |
| …::test_codeless_state_finding_is_distinct_from_codeless_candidate_refusal | DELETED-as-covered | the producer-side refusal of a codeless rejection is `test_rejection_operations.py::test_candidate_operation_requires_a_nonempty_owned_code`; the placeholder projection was planner-only |
| …::test_withheld_generic_guidance_does_not_infer_impossibility_from_classifiers | DELETED | planner repeat and finalizer-ownership notice |
| …::test_planner_path_policy_projection_has_no_raw_detail_exception (×3) | DELETED-as-covered | `test_set_pipeline_candidate.py::test_candidate_path_refusals_keep_server_roots_out_of_tool_history` (live disclosure canary on all three path-policy surfaces) |
| test_schema_gate_repair_signal.py::test_planner_discovery_argument_error_preserves_closed_schema_facts | DELETED-as-covered | the planner discovery `argument_error` body. The live compose-loop body is pinned by `test_out_of_bounds_limit_names_the_declared_field` (same `list_models {"limit": 0}` case). Module docstring updated |
| test_tool_argument_error_category.py::test_every_category_survives_planner_rejection_code_closure | DELETED | `_closed_planner_rejection_codes` plus the planner attempt record |
| …::test_planner_rejection_closure_still_collapses_unknown_codes | DELETED | same |
| test_tool_result_variant_teaching.py::test_derived_non_envelope_set_is_the_audited_four | TRIMMED → `…_audited_three` | the derived non-envelope set is {request_advisor_hint, handoff_to_user, restore_parked_graph} |
| …::test_skill_result_section_names_every_non_envelope_tool | TRIMMED | the skill names all three; the terminal exemption and the "proposal terminal" pin were cut (see F3) |
| …::test_operator_reference_names_every_non_envelope_tool | KEPT (only the helper changed) | subset check: the operator reference names all three |
| test_edge_route_reconciliation.py::test_graph_refusal_retains_saved_findings_identity_and_static_guidance | TRIMMED + PORTED | on dispatch, `validation_guidance[code]` equals `explain_validation_code(code)`; edge canary absent from the redaction |
| …::test_graph_existing_edge_without_edge_none_guard_is_explicit_boundary_only | TRIMMED (via `_assert_graph_refusal`) | refusal and saved findings across snapshots |
| …::test_graph_late_real_invariant_refuses_private_edge_and_scalar_together | TRIMMED (same helper) | same |
| …::test_graph_late_review_refusal_preserves_private_changes_and_saved_review_anchors | TRIMMED (same helper) | same |
| test_state.py::test_gate_on_error_must_reference_declared_sink_or_discard | TRIMMED | `gate_on_error_unknown_sink` is raised; the `route_destination_facts` check is gone |
| test_state.py::test_aggregation_on_error_route_facts_match_the_unknown_sink_rule | TRIMMED → `test_aggregation_on_error_must_reference_declared_sink` | `aggregation_on_error_unknown_sink` (including for the closer-shaped value 'merge'); docstring reworded |
| test_state_row_union.py::…::test_row_union_flexible_shared_type_conflict_carries_repair_facts | PORTED | `ToolResult.to_dict()` carries `row_union_schema == detail.to_dict()`, and `build_validation_guidance` resolves the code |
| test_generation_schema_response.py::test_snapshot_preserves_wire_and_detaches_producer | TRIMMED | admission keeps the catalogue wire and detaches the producer; the planner projection equality was cut. Module docstring reworded |
| …::test_valid_large_schema_keeps_projection_refusal_separate | TRIMMED → `test_valid_large_schema_is_admitted_whole` | a 49 KiB schema is admitted whole |
| …::test_unknown_finite_schema_extension_is_independent_projection_refusal | TRIMMED → `test_unknown_finite_schema_extension_is_admitted_and_non_json_value_is_refused` | a finite extension is admitted byte-equal; a non-JSON value raises FrameworkBugError |
| test_discovery_response_contracts.py::test_provider_bytes_match_immutable_baseline | DELETED-as-covered | `test_admitted_bytes_match_freeform_baseline` (same golden `freeform` bytes on the live serializer) |
| …::test_original_data_is_not_serialization_authority_after_admission | TRIMMED | `serialize_admitted_discovery_result` and `serialize_tool_result` ignore the replaced original data |
| test_provider_discovery_response.py::test_argument_error_projection_preserves_exact_bytes | DELETED | planner discovery `argument_error` wire bytes |
| …::test_argument_error_projection_keeps_closed_repair_facts_without_private_input (×6) | PORTED → `test_compose_loop_argument_error_keeps_closed_repair_facts_without_private_input` | `tool_error_payloads.arg_error_payload` emits the closed loc/msg/type for choice, type, bound, unknown root, unknown mapping key and malformed root, with no PRIVATE_ leak (including the cause) |
| …::test_argument_error_projection_marks_over_budget_without_leaking_values | DELETED-as-covered | `test_schema_gate_repair_signal.py::test_more_than_eight_violations_collapse_to_one_truncated_entry` (compose loop) |
| test_list_secret_refs_contract.py::test_offered_description_distinguishes_metadata_from_resolution | TRIMMED | the loop tool description contract is kept; the planner-palette equality was cut |
| test_tools_discovery.py::test_discovery_denial_precedes_requested_access_and_preserves_saved_findings | TRIMMED (via `_assert_discovery_refusal`; signature kept for `test_agent_tooling.py`) | admitted discovery refusal wire |
| …::test_discovery_feedback_excludes_private_saved_diagnostics_but_authenticated_raw_keeps_them | TRIMMED → `test_discovery_redaction_excludes_…` | the raw admitted wire keeps the canary and the redaction drops it |
| tools/test_tools_pipeline.py::TestSetPipeline::test_set_pipeline_oversized_inline_csv_field_returns_bounded_failure | TRIMMED (via `_assert_inline_source_refusal`) | the dispatch `validation_guidance` explanation and fix are kept |
| …::test_set_pipeline_malformed_inline_csv_returns_bounded_failure | TRIMMED (same helper) | same |
| …::test_set_pipeline_header_only_candidate_scan_has_fixed_file_read_budget | TRIMMED (same helper) | same |
| test_model_discovery_scope.py::test_model_discovery_surface_preserves_rereads_and_scope (×11) | PORTED → `test_list_models_reports_scope_and_completeness` (×10) | the `list_models` loop tool, through `execute_tool`, reports count, truncated and models per scope (live alias, bundled prefix, subset, summary, empty provider). The planner reread and manifest half was cut |
| …::test_model_discovery_request_predicates_keep_partial_scope_unresolved | DELETED | `PlannerDiscoveryPolicy` / `_tool_information_keys` |
| …::test_full_authoring_catalog_covers_listing_scopes | DELETED | same |
| …::test_model_discovery_completion_uses_selected_response_instead_of_original_root (×2) | DELETED | planner `admit_discovery_result` egress. The live equivalent is `test_discovery_response_contracts.py::test_original_data_is_not_serialization_authority_after_admission` |
| test_planner_authoring_aids.py::TestDiscoveryDigest::test_selected_plugin_schema_remains_available_when_compact_attachment_budget_is_spent (×3) | DELETED | planner `schema_contract_budget_remaining`. Live get_plugin_schema admission is covered by `test_generation_schema_response.py::test_actual_catalogue_schema_wire_bytes_are_preserved` |
| …::TestSession891b7b1eLiveReviewEdits::test_proposal_planner_custody_rule_names_no_unadvertised_tool | DELETED | it checked the rule against the planner palette, which no longer exists (see F2) |

## 2. Files deleted outright

None. `test_model_discovery_scope.py` lost all 4 of its planner tests but now holds the ported `list_models` contract test. It no longer imports `test_pipeline_planner.py`.

## 3. FINDINGS (production behaviour or content that looks wrong; nothing in src was changed)

- **F1: Repair guidance teaches facts that no producer emits.** U-30 deleted `coalesce_reachability_facts` (base `src/elspeth/web/composer/state.py:2925`) and `route_destination_facts` (base `state.py:3035`). Their only consumer was the planner (base `pipeline_planner.py:166-167, :2502`). The live compose loop attaches only static catalogue text (`src/elspeth/web/composer/tools/_dispatch.py:724-752` → `build_validation_guidance`). That catalogue still tells the model to read `'connectivity'` facts: `unreachable_branches`, `sink_lure`, `produced_connections`, `dangling_on_success`, `declared_sinks`, `consumable_connections`, `dangling_on_error`. See `src/elspeth/web/composer/tools/generation.py:792-818, 1035-1036, 1111-1115, 1122-1124, 1133-1135`. So `set_pipeline` rejections for `coalesce_branch_unreachable` and `*_on_success_dangling` / `*_on_error_unknown_sink` now carry codes plus guidance that describes a payload that cannot arrive. The guidance says "when present", so it is not false, but it is dead teaching. This belongs in the same category as the lead's held chains: decide whether these facts belong on the compose loop's `set_pipeline` rejection.
- **F2: The live prompt still ships a planner-only custody rule.** `src/elspeth/web/composer/planner_authoring_aids.py:389-396` ("PROPOSAL PLANNER SURFACE: … use only tools advertised in this request …") goes into every compose-loop prompt through `prompts.py:271` (`build_planner_authoring_aids(catalog)`). There is no proposal planner surface any more. The unflagged test that pins the rule's wording (`test_planner_authoring_aids.py`, the test right before the deleted custody test, asserting on `proposal_rule`) was left as is: it encodes live prompt content, which is a src decision.
- **F3: Stale docs for the retired terminal.** `src/elspeth/web/composer/skills/pipeline_composer.md:393` ("When your request instruction offers a proposal terminal…") and `docs/reference/composer-tools.md:616` (the `emit_pipeline_proposal` row) still describe it. The trimmed variant-teaching tests check only that the docs name the three live tools, so they do not catch the stale mention. Both files are outside my bucket.
- **F4: Stale docstrings that cite deleted names.** `tools/_dispatch.py:727-728`, `tools/generation.py:1382, 2029, 2063` and `tool_error_payloads.py:53` still refer to `pipeline_planner._allowlisted_candidate_feedback` or to "planner discovery" bodies.
- **F5: Lost guard, low severity.** The planner raised `AuditIntegrityError` when a codeless `rejected_mutation` entry reached its projection. The live wire serialises such an entry without that check. `test_splice_codeless_boundary_is_preserved` injects this boundary by hand; its comment says no natural producer emits it today. The producer-side guard at `_candidate_rejection_result` remains (`test_candidate_operation_requires_a_nonempty_owned_code`).

## 4. Final run

Command:
```
cd .claude/worktrees/u30-20261025 && PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src .venv/bin/python -m pytest $(cat .claude/lanes/u30/validation-tests-files.txt) -n 2 -p no:cacheprovider -q -rfE > .claude/lanes/u30/validation-tests-final.log 2>&1; echo "exit=$?"
```
Exit code: `exit=1`. Summary line: `1454 passed, 1 error in 184.49s (0:03:04)`.

The one remaining failure:
- `ERROR tests/unit/web/composer/test_edge_route_reconciliation.py`. First error line: `ImportError: cannot import name '_allowlisted_candidate_feedback' from 'elspeth.web.composer.pipeline_planner'`, raised from `test_transform_tools_review_and_guard_parity.py:29`. My file imports that module at module level for fixtures, at `test_edge_route_reconciliation.py:42`. That file is in the **guard-tests bucket** (`.claude/lanes/u30/guard-tests-files.txt:12`), so the error clears when that agent removes its import. My file no longer references any deleted name.
  - Verification on this tree: the file was run with a test-only pytest plugin, `.claude/lanes/u30/validation-tests-shim/validation_tests_shim.py`. The plugin stubs `_allowlisted_candidate_feedback` to raise if called, which lets the file collect. Result: `exit=0`, `59 passed` (`validation-tests-edge-shim.log`). So no test in my file calls the retired function.
  - Base (`u30-base-20261025`, d7ad18596), same file: `exit=0`, `59 passed` (`validation-tests-base-edge.log`). The error does not occur on base; it is an ordering dependency between buckets on the U-30 tree.

Lint: `ruff check` reports all checks passed on all 21 files, and `ruff format --check` reports 21 files already formatted.

Logs (all under `.claude/lanes/u30/`): `validation-tests-start.log` (start: 35 failed, 329 passed, 16 errors), `validation-tests-step*.log`, `validation-tests-final.log`, `validation-tests-edge-shim.log`, `validation-tests-base-edge.log`, `validation-tests-reach.txt`, `validation-tests-reach-after.txt`.
