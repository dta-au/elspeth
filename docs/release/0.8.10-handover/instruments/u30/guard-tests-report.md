# guard-tests report (U-30 test-side retirement, guard/projection/audit/wire bucket)

STATUS: PAUSED PART-WAY (coordinator PAUSE, `.claude/lanes/release-0.8.10/PAUSE-PROTOCOL.md`).
6 of 17 files are finished and green. 11 are untouched. Nothing is committed: the brief
(`AGENT-BRIEF-tests.md` § Hard constraints) forbids state-changing git, and other agents are
editing the same worktree. The dispatcher owns the commit. My edits are only the 6 paths in § 1.

Worktree `.claude/worktrees/u30-20261025`, branch `codex/0.8.10-u30`,
HEAD b0fc0e0eb (uncommitted edits on top). Base for comparison: `.claude/worktrees/u30-base-20261025` (d7ad18596).

## 1. Decisions so far (finished files)

| file::test | decision | reason |
|---|---|---|
| test_composer_tool_contract.py::test_each_route_resolves_from_its_own_model_and_endpoint | TRIMMED (renamed `test_primary_route_resolves_from_its_own_model_and_endpoint`) | `ComposerToolContract.hatch` no longer exists. The six primary-route cases stay. id `proxy_hatch` became `proxy_advisor`: it now shows that an advisor proxy does not move the primary route |
| test_composer_tool_contract.py::test_service_resolves_its_dialects_and_summary_from_settings | TRIMMED | removed the `service._hatch_dialect` assertion; the planner-dialect, summary and count contract survives |
| test_composer_tool_contract.py::test_resolution_event_carries_closed_values_and_counts_only | TRIMMED | `hatch_*` fields left `composer_tool_contract_resolved` (service.py); the closed-value event pin survives |
| test_composer_tool_contract.py::test_a_malformed_env_base_never_stops_construction_and_is_never_logged | TRIMMED | the check now loops over the primary route only; non-fatal construction, the diagnostic and no-log-leak survive |
| test_composer_tool_contract.py::test_a_malformed_env_base_never_stops_the_probe_builder | TRIMMED | surfaces are now `["loop_tools", "advisor"]`; added `tool_count > 0` on the loop request so the zero-strict check is not vacuous |
| test_system_status_tool_contract.py::test_hatch_route_is_not_disclosed | DELETED | the hatch route is gone from the contract. Status-body non-disclosure on the live route stays pinned by `test_planner_route_is_not_disclosed` in the same file |
| test_system_status_tool_contract.py::test_probe_outcomes_do_not_change_the_published_value | TRIMMED | the `hatch-rejected` case is retired because the non-fatal hatch branch was deleted (app.py: every `ComposerBootConfigError` now re-raises). The `all-accepted` case survives, and the parametrize was dropped |
| test_system_status_tool_contract.py::test_each_sent_surface_logs_one_owned_boot_outcome | PORTED | now rejects `advisor` (fatal, so it runs under `pytest.raises(ComposerBootConfigError)`). Still pins one `composer_boot_probe_outcome` per sent surface, including a `rejected` status (logged in app.py's `finally`), plus owned counts and no key or endpoint leak. Nothing else in tests/ asserts that event, measured by grep |
| test_system_status_tool_contract.py::_boot (helper) | TRIMMED | sent surfaces are now `["loop_tools", "advisor"]`; a rejected surface is wrapped in `pytest.raises` |
| test_wire_projection.py::test_stamp_planner_terminal | DELETED | `stamp_planner_terminal` was deleted; it stamped only the retired planner terminal |
| test_llm_tool_contract_audit.py::TestDerivation::test_a_list_stamped_only_false_is_openai_strict_with_zero | PORTED | uses synthetic `strict=False` tools instead of the stamped planner terminal. The `build_llm_call_record` derivation (all-False stamps give openai_strict with 0) is live |
| test_wire_fidelity_matrix.py::test_openrouter_planner_list_forwards_its_stamps | DELETED (covered) | planner request builder retired. Loop-list forwarding on the same route: `test_openrouter_forwards_the_stamped_list_verbatim`. Module docstring updated |
| test_prompt_guard_json_boundaries.py::test_public_urls_keep_json_token_boundaries | TRIMMED + PORTED | removed the `canonical_planner` surface (drove `_plan`). `compact_tool_arguments` now builds a `set_pipeline` assistant call through the compose loop's own producer `tool_batch._retain_llm_tool_argument_parts`. Added an assertion that the producer attached the parts. `freeform_state` is unchanged |
| test_prompt_guard_json_boundaries.py::test_decoded_credential_tokens_remain_closed_at_physical_guard | TRIMMED | the hand-built tool name `emit_pipeline_proposal` became `set_pipeline`; the guard contract is unchanged |
| test_prompt_guard_json_boundaries.py::test_actual_credentials_still_refuse_before_detector_and_sdk | PORTED | goes through the ported `_tool_messages` (the compose-loop producer) |

The file no longer imports from `test_pipeline_planner`.

## 2. Files deleted outright
None so far.

## 3. FINDINGS
None in production. Outside my bucket (reported to the coordinator, who passes them to composer-carries):
`tests/unit/web/test_app.py:2478` and `test_hatch_terminal_rejection_is_nonfatal` (about lines 2485-2537)
still pin the `planner_tools` / `hatch_terminal` surfaces and `composer_boot_probe_rejected_nonfatal`.

## 4. Runs
- Before any edit, the whole bucket: `.claude/lanes/u30/guard-tests-step0.log`: exit=1, `52 failed, 71 passed, 11 errors`
  (11 collection ImportErrors, for example `prepare_bounded_pipeline_state` from tools._common, `_FINALIZER_OWNS_NOTHING` from pipeline_planner).
- test_system_status_tool_contract.py + test_composer_tool_contract.py: `guard-tests-step1.log`: exit=0, `22 passed`.
- json_boundaries + wire_projection + wire_fidelity_matrix + llm_tool_contract_audit: `guard-tests-step2.log`: exit=0, `115 passed`.
- ruff check and ruff format on the 6 touched files: clean, no changes.
- No final whole-bucket run yet (paused).

## COLD START (paused 2026-10-25)
- **Unit:** U-30 test side, guard-tests bucket (17 files listed in `.claude/lanes/u30/guard-tests-files.txt`). Branch `codex/0.8.10-u30` @ b0fc0e0eb, worktree above. Base d7ad18596 at `u30-base-20261025`.
- **Done and verified:** the 6 files in § 1 (evidence: step1 and step2 logs).
- **Uncommitted:** my edits are exactly these paths. Commit them by pathspec; the other ` M` files in `git status` belong to sibling agents:
  tests/unit/web/composer/test_composer_tool_contract.py, tests/unit/web/test_system_status_tool_contract.py,
  tests/unit/web/composer/test_wire_projection.py, tests/unit/web/composer/test_llm_tool_contract_audit.py,
  tests/unit/web/composer/test_wire_fidelity_matrix.py, tests/unit/web/composer/test_prompt_guard_json_boundaries.py
- **Not started (11 files),** with flagged tests in `.claude/lanes/u30/guard-tests-reach.txt`:
  test_content_projection.py (5), test_content_projection_guard.py (1), test_prompt_guard_canonical_containers.py (3),
  test_prompt_guard_producer_parts.py (6 flagged, plus about 20 unflagged advisor/freeform ids failing in step0), test_prompt_guard_reply_context.py (3),
  test_private_received_diagnostics.py (3), test_provider_quota_end_to_end.py (2), test_explicit_provider_custody.py (1),
  test_tool_redaction_policy.py (1), test_transform_tools_review_and_guard_parity.py (2), test_capability_skill_identity.py (11).
- **Next steps:**
  1. `prepare_bounded_pipeline_state` (6 content_projection tests): run `git show b0fc0e0eb -- src/elspeth/web/composer/tools/_common.py src/elspeth/web/composer/state.py`, then grep the surviving src for its callees. Port the tests onto a live producer if one builds the same hash-only references; otherwise delete all six.
  2. For `emit_pipeline_proposal` / `_assistant_tool_calls_message` tests in reply_context and producer_parts, use the same port pattern as json_boundaries `_tool_messages` (compose-loop `_retain_llm_tool_argument_parts`). Delete `test_real_planner_*` / `test_actual_planner_*` tests whose only path is `_plan`.
  3. Find out why the unflagged advisor tests in producer_parts fail at step0 before deciding anything about them (`grep -n "test_real_advisor" -A30 .claude/lanes/u30/guard-tests-step0.log`).
  4. capability_skill_identity: delete the manifest tests and the `_manifest` helper, plus `test_capability_core_names_no_planner_exclusive_terminal_tool` (vacuous now) and `test_planner_request_instruction_carries_the_terminal_contract`. Keep the tests of the capability document against the schema.
  5. transform_tools parity: if the subject is splice/batch dispatch, trim the `_allowlisted_candidate_feedback` oracle and assert the dispatch output directly.
  6. Run the whole bucket: `cd .claude/worktrees/u30-20261025 && PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src .venv/bin/python -m pytest $(cat .claude/lanes/u30/guard-tests-files.txt) -n 2 -p no:cacheprovider -q -rfE > .claude/lanes/u30/guard-tests-final.log 2>&1; echo "exit=$?"`. Check each remaining failure against the base.
- **Open questions:** which `test_pipeline_planner.py` helpers survive (`_ScriptedCompletion`, `_text_response`, `_response`, `_pipeline`), and whether `test_pipeline_planner_protocol_rejections.py` is retired. Owner: composer-carries. Default meanwhile: keep the bucket free of planner-route-only helpers.
- **Hazards:** a bare `pytest` imports the main checkout; use the brief's command. Do not `uv`. At most `-n 2`.
- **Processes left:** none (verified with `ps`).
