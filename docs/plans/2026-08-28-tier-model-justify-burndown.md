# Tier-model justify-lane burn-down — fanout plan (2026-08-28)

Source: staged bundle `elspeth-600360c72e-rebind-abd70f32f` (bound to abd70f32f, now STALE — 9 commits behind HEAD; the allowlist did not change, so the overnight fire never published). Justify lane = **2,339 findings across 205 files / 224,501 LOC**, zero rationales. The 366-action resign lane is untouched by this plan and is fired separately (`--lanes resign`).

## Sequencing (the bundle stales on every commit — this ordering is not optional)

1. **Fix wave(s)** — agents remove findings by honest code change and commit. No lane edits `config/cicd/enforce_tier_model/*.yaml` and no lane touches the bundle. Findings that are policy-correct get a site-specific rationale written to `docs/agents/sweeps/tier-burndown/<bucket>.rationales.json` (`{key: rationale}`), committed with the code.
2. **Re-stage once** — `stage_scan` after the last fix-lane merge. Expect the justify lane to shrink by every removed finding.
3. **Annotate** — `stage_annotate` the surviving keys from the merged rationale sidecars; `stage_preview` to catch BLOCKs before the operator pays for them.
4. **Operator fires** — `sign-bundle --lanes resign,new_judgment --continue-on-block`; BLOCKs come back as a remediation worklist for a second, much smaller round.

## Lane contract (every bucket)

- Own worktree (`.claude/worktrees/tier-<bucket>`), `PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src`, verify `elspeth.__file__` AND `elspeth_lints.__file__` before trusting a result. `pytest -n 2` max per lane.
- Read `docs/agents/recent-code-hints.md` and ADR-032 first. Preference order per finding: (a) **remove** it with a correct code change (nominal `isinstance` on an owned type, membership-form reads, explicit error paths, `@trust_boundary` on honest Tier-3 parse sites); (b) if genuinely policy-correct, write a rationale that names the flagged pattern and why it is right *at this site*. Never blanket, never alias, never reorder to dodge a fingerprint.
- Evidence: `elspeth-lints check --rules trust_tier.tier_model --root src/elspeth` corpus **count** before/after (whole tree, not `tail`), scoped tests green, and the wave-merge full suite. Deliverable = commit(s) + sidecar + a Filigree comment on the bucket issue listing removed vs. rationalised keys.
- Model: `fable` for buckets touching auth/secrets/redaction/sessions.service/sinks/infra clients/AWS+Textract; `opus` otherwise. Adjust freely.

## Buckets (≤5,000 LOC each, a file is never split; 5 files exceed the cap alone)

### Wave 1 — contracts / core / engine / plugins / telemetry / tui (low contention)

| Bucket | Model | LOC | Findings | Files (findings by rule) |
|---|---|---:|---:|---|
| B01 | opus | 4878 | 36 | `contracts/audit_export.py` (1648 LOC; R5×8)<br>`contracts/runtime_val_manifest.py` (1566 LOC; R1×2)<br>`contracts/sink_effects.py` (1495 LOC; R5×19)<br>`contracts/chat_parts.py` (169 LOC; R5×7) |
| B02 | opus | 160 | 5 | `contracts/emitted_option.py` (160 LOC; R5×5) |
| B03 | opus | 3400 | 1 | `core/config.py` (3400 LOC; L1×1) |
| B04 | opus | 3259 | 21 | `core/schema_shape.py` (2497 LOC; R5×11, R6×1)<br>`core/audit_export_content_store.py` (408 LOC; R7×2)<br>`core/llm_profiles.py` (282 LOC; L1×2)<br>`core/commencement_gate_expression.py` (72 LOC; R5×5) |
| B05 | opus | 4116 | 8 | `core/dag/builder.py` (1977 LOC; R1×4)<br>`core/dag/schema_validation.py` (1685 LOC; R1×2)<br>`core/dag/coalesce_warnings.py` (307 LOC; R1×1)<br>`core/dag/row_union_warnings.py` (147 LOC; R1×1) |
| B06 | opus | 4657 | 4 | `core/landscape/execution/node_states.py` (1218 LOC; R6×1)<br>`core/landscape/execution_repository.py` (1206 LOC; R6×1)<br>`core/landscape/exporter.py` (1164 LOC; R6×1)<br>`core/landscape/run_coordination_repository.py` (1069 LOC; R6×1) |
| B07 | opus | 4960 | 17 | `core/landscape/scheduler/dispositions.py` (932 LOC; R6×1)<br>`core/landscape/execution/sink_effect_lifecycle.py` (908 LOC; R5×4)<br>`core/landscape/execution/sink_effect_finalization.py` (826 LOC; R5×2)<br>`core/landscape/execution/audit_export_snapshots.py` (772 LOC; R5×2, R6×1)<br>`core/landscape/export_read_model.py` (494 LOC; R5×2)<br>`core/landscape/execution/sink_effect_identity.py` (434 LOC; R5×3)<br>`core/landscape/scheduler/group_losses.py` (331 LOC; R5×1)<br>`core/landscape/export_mappers.py` (263 LOC; R5×1) |
| B08 | opus | 154 | 1 | `core/landscape/execution/sink_effect_attempt_results.py` (154 LOC; R5×1) |
| B09 | opus | 4435 | 5 | `engine/barrier_coordination.py` (2684 LOC; R1×1)<br>`engine/journal_restore.py` (1000 LOC; R1×1)<br>`engine/row_union_executor.py` (751 LOC; R1×2, R8×1) |
| B10 | opus | 656 | 1 | `engine/tokens.py` (656 LOC; R1×1) |
| B11 | opus | 4592 | 36 | `engine/executors/sink_effects.py` (1552 LOC; R1×1, R4×1, R5×19)<br>`engine/executors/sink.py` (1450 LOC; R4×1, R5×7)<br>`engine/executors/collector.py` (1098 LOC; R1×6)<br>`engine/executors/state_guard.py` (492 LOC; R5×1) |
| B12 | opus | 1856 | 15 | `engine/orchestrator/preflight.py` (950 LOC; R5×9)<br>`engine/orchestrator/audit_export_effects.py` (490 LOC; R4×1, R5×3)<br>`engine/orchestrator/export.py` (416 LOC; R5×2) |
| B13 | fable | 4858 | 16 | `plugins/infrastructure/base.py` (2423 LOC; R1×1, R2×2, R3×1, R5×9)<br>`plugins/infrastructure/clients/http.py` (1126 LOC; R5×1)<br>`plugins/infrastructure/clients/llm.py` (801 LOC; R1×1)<br>`plugins/infrastructure/clients/retrieval/chroma.py` (508 LOC; R5×1) |
| B14 | fable | 1644 | 34 | `plugins/infrastructure/runtime_factory.py` (495 LOC; R5×10)<br>`plugins/infrastructure/discovery.py` (398 LOC; R1×3)<br>`plugins/infrastructure/validation.py` (331 LOC; R1×2)<br>`plugins/infrastructure/templates.py` (183 LOC; R5×17)<br>`plugins/infrastructure/rasterize/worker.py` (161 LOC; R5×1)<br>`plugins/infrastructure/telemetry.py` (76 LOC; R4×1) |
| B15 | opus | 708 | 14 | `plugins/llm/model_catalog.py` (394 LOC; R1×2, R2×1, R5×6, R6×3, R8×1)<br>`plugins/llm/config_validation.py` (314 LOC; R5×1) |
| B16 | fable | 4582 | 63 | `plugins/sinks/_audit_export_bundle_effects.py` (1317 LOC; R5×5, R6×19, R7×1)<br>`plugins/sinks/aws_s3_sink.py` (1212 LOC; R1×1, R5×17, R6×10)<br>`plugins/sinks/database_sink.py` (1117 LOC; R1×2, R6×2)<br>`plugins/sinks/azure_blob_sink.py` (936 LOC; R5×5, R6×1) |
| B17 | fable | 4582 | 24 | `plugins/sinks/_remote_object_effects.py` (802 LOC; R6×3)<br>`plugins/sinks/_local_file_effects.py` (775 LOC; R6×3)<br>`plugins/sinks/chroma_sink.py` (724 LOC; R5×6, R6×1)<br>`plugins/sinks/dataverse.py` (718 LOC; R1×1)<br>`plugins/sinks/json_sink.py` (626 LOC; R1×1)<br>`plugins/sinks/document_sink.py` (463 LOC; R1×2, R6×1)<br>`plugins/sinks/text_sink.py` (386 LOC; R1×2, R6×1)<br>`plugins/sinks/_diversion_attribution.py` (88 LOC; R5×3) |
| B18 | fable | 2797 | 22 | `plugins/sources/aws_s3_source.py` (1481 LOC; R1×2, R6×10, R7×3)<br>`plugins/sources/llm/source.py` (705 LOC; R1×1, R4×3)<br>`plugins/sources/field_normalization.py` (611 LOC; R6×3) |
| B19 | fable | 4876 | 24 | `plugins/transforms/llm/transform.py` (1999 LOC; R1×4)<br>`plugins/transforms/aws/textract_document_analysis.py` (1090 LOC; R1×4, R5×11, R8×1, R9×1)<br>`plugins/transforms/blob_json_expand.py` (943 LOC; R9×2)<br>`plugins/transforms/field_mapper.py` (844 LOC; R6×1) |
| B20 | fable | 4643 | 75 | `plugins/transforms/llm/providers/gateway.py` (823 LOC; R1×15, R6×1, R9×2)<br>`plugins/transforms/blob_csv_expand.py` (798 LOC; R9×2)<br>`plugins/transforms/aws/textract_client.py` (784 LOC; R1×14, R2×2, R4×1, R5×7, R6×6)<br>`plugins/transforms/aws/textract_result.py` (768 LOC; R1×17, R5×3)<br>`plugins/transforms/aws/textract_inline_analysis.py` (735 LOC; R1×1, R5×1, R9×1)<br>`plugins/transforms/pdf_rasterize.py` (735 LOC; R5×2) |
| B21 | fable | 3905 | 63 | `plugins/transforms/llm/providers/openrouter.py` (620 LOC; R1×4, R6×1, R9×2)<br>`plugins/transforms/aws/guardrails_client.py` (613 LOC; R4×1, R5×6, R6×3)<br>`plugins/transforms/reference_join.py` (550 LOC; R1×1, R5×7, R9×1)<br>`plugins/transforms/aws/textract_bucket_region.py` (411 LOC; R1×9, R4×1, R5×4, R6×3, R9×2)<br>`plugins/transforms/llm/providers/azure.py` (363 LOC; R1×2, R6×2, R9×1)<br>`plugins/transforms/llm/provider.py` (310 LOC; R1×1)<br>`plugins/transforms/llm/langfuse.py` (280 LOC; R4×3)<br>`plugins/transforms/llm/providers/bedrock.py` (251 LOC; R1×2, R6×2, R8×1, R9×1)<br>`plugins/transforms/llm/tracing.py` (208 LOC; R1×1)<br>`plugins/transforms/llm/image_inputs.py` (187 LOC; R1×1)<br>`plugins/transforms/aws/guardrails_live_check.py` (112 LOC; R7×1) |
| B22 | opus | 875 | 4 | `telemetry/manager.py` (875 LOC; R2×1, R4×1, R5×2) |
| B23 | opus | 666 | 1 | `tui/screens/explain_screen.py` (666 LOC; R1×1) |

### Wave 2 — web (non-session)

| Bucket | Model | LOC | Findings | Files (findings by rule) |
|---|---|---:|---:|---|
| B24 | opus | 4278 | 67 | `web/interpretation_state.py` (2356 LOC; R1×36, R5×24)<br>`web/app.py` (1922 LOC; R4×2, R6×4, R7×1) |
| B25 | opus | 4401 | 60 | `web/config.py` (1327 LOC; R1×4, R5×4)<br>`web/aws_ecs_acceptance.py` (806 LOC; R4×1)<br>`web/doctor.py` (675 LOC; R4×16)<br>`web/operator_telemetry.py` (592 LOC; R4×1, R5×2, R6×5)<br>`web/readiness.py` (544 LOC; R1×4, R4×9, R6×1, R7×3)<br>`web/schema_probe.py` (332 LOC; R1×1, R2×2, R4×4, R5×2)<br>`web/paths.py` (125 LOC; R6×1) |
| B29 | opus | 1281 | 3 | `web/audit_readiness/service.py` (1281 LOC; R1×3) |
| B30 | fable | 1312 | 4 | `web/auth/local.py` (1113 LOC; R2×1, R6×2)<br>`web/auth/urls.py` (199 LOC; R5×1) |
| B31 | fable | 3394 | 12 | `web/blobs/service.py` (3394 LOC; R1×4, R6×5, R7×3) |
| B32 | opus | 896 | 6 | `web/catalog/knob_schema.py` (647 LOC; R5×2)<br>`web/catalog/policy_view.py` (249 LOC; R1×4) |
| B33 | opus | 9503 | 23 | `web/composer/service.py` (9503 LOC; R1×7, R2×4, R5×10, R9×2) |
| B34 | opus | 8295 | 47 | `web/composer/state.py` (8295 LOC; R1×9, R5×16, R6×20, R8×2) |
| B35 | opus | 5036 | 42 | `web/composer/pipeline_planner.py` (5036 LOC; R1×21, R5×11, R6×4, R7×4, R9×2) |
| B36 | fable | 4353 | 18 | `web/composer/redaction.py` (4353 LOC; R1×14, R5×4) |
| B37 | opus | 3947 | 17 | `web/composer/guided/chat_solver.py` (3947 LOC; R1×10, R2×5, R6×2) |
| B38 | opus | 3920 | 178 | `web/composer/guided/planning.py` (3920 LOC; R1×133, R5×34, R8×7, R9×4) |
| B39 | opus | 3662 | 8 | `web/composer/tools/generation.py` (3662 LOC; R5×3, R6×5) |
| B40 | opus | 3487 | 1 | `web/composer/tools/_common.py` (3487 LOC; R6×1) |
| B41 | opus | 2830 | 14 | `web/composer/tools/sessions.py` (2830 LOC; R1×1, R5×12, R6×1) |
| B42 | opus | 2711 | 27 | `web/composer/planner_authoring_aids.py` (2711 LOC; R1×23, R5×1, R6×2, R9×1) |
| B43 | opus | 4734 | 88 | `web/composer/guided/protocol.py` (2413 LOC; R1×35, R5×49)<br>`web/composer/tool_batch.py` (2321 LOC; R2×4) |
| B44 | fable | 4500 | 69 | `web/composer/guided/deferred_intents.py` (2307 LOC; R1×35, R5×16, R6×2, R8×9)<br>`web/composer/tools/blobs.py` (2193 LOC; R1×1, R5×5, R7×1) |
| B45 | opus | 3761 | 2 | `web/composer/tools/sources.py` (1921 LOC; R5×1)<br>`web/composer/tools/transforms.py` (1840 LOC; R5×1) |
| B46 | opus | 4108 | 12 | `web/composer/protocol.py` (1386 LOC; R1×1, R6×1)<br>`web/composer/guided/state_machine.py` (1381 LOC; R1×1, R5×5)<br>`web/composer/guided/stage_transitions.py` (1341 LOC; R1×2, R5×2) |
| B47 | opus | 4234 | 72 | `web/composer/audit.py` (1328 LOC; R5×1)<br>`web/composer/llm_response_parsing.py` (994 LOC; R1×12, R5×10, R6×4)<br>`web/composer/required_controls.py` (991 LOC; R1×36, R5×6, R6×2)<br>`web/composer/tools/_dispatch.py` (921 LOC; R1×1) |
| B48 | opus | 4471 | 53 | `web/composer/guided/emitters.py` (842 LOC; R1×13, R4×1, R5×16, R6×1, R8×1)<br>`web/composer/pipeline_proposal.py` (797 LOC; R1×3, R9×1)<br>`web/composer/guided/stage_subjects.py` (726 LOC; R1×2)<br>`web/composer/tutorial_service.py` (725 LOC; R1×1)<br>`web/composer/yaml_importer.py` (715 LOC; R1×4, R5×9)<br>`web/composer/_semantic_validator.py` (666 LOC; R1×1) |
| B49 | opus | 4922 | 72 | `web/composer/yaml_generator.py` (663 LOC; R1×2, R5×4)<br>`web/composer/prompts.py` (639 LOC; R5×7)<br>`web/composer/progress.py` (544 LOC; R1×4)<br>`web/composer/_producer_resolver.py` (399 LOC; R1×2, R8×1)<br>`web/composer/source_demand.py` (374 LOC; R5×5, R6×4)<br>`web/composer/guided/resolved.py` (367 LOC; R5×7)<br>`web/composer/guided/intent_management.py` (296 LOC; R5×1)<br>`web/composer/audit_storage.py` (271 LOC; R1×2)<br>`web/composer/turn_audit.py` (256 LOC; R5×1, R6×1)<br>`web/composer/guided_blob_refs.py` (239 LOC; R1×2, R5×4)<br>`web/composer/reviewed_source_authority.py` (236 LOC; R1×2, R5×4)<br>`web/composer/provider_telemetry.py` (228 LOC; R1×5, R4×3, R6×2)<br>`web/composer/guided/prompts.py` (211 LOC; R1×1)<br>`web/composer/control_messages.py` (101 LOC; R1×2)<br>`web/composer/authority_hashing.py` (98 LOC; R1×6) |
| B50 | opus | 293 | 8 | `web/composer/reasoning.py` (83 LOC; R4×1)<br>`web/composer/discovery_cache.py` (74 LOC; R5×1)<br>`web/composer/advisor_checkpoint_telemetry.py` (47 LOC; R7×2)<br>`web/composer/tutorial_telemetry.py` (46 LOC; R4×2)<br>`web/composer/guided/connection_consumers.py` (43 LOC; R5×1, R8×1) |
| B51 | opus | 3346 | 10 | `web/execution/service.py` (3346 LOC; R1×6, R5×2, R6×2) |
| B52 | opus | 4535 | 27 | `web/execution/routes.py` (1852 LOC; R1×1)<br>`web/execution/_validation_authoring.py` (1097 LOC; R5×2)<br>`web/execution/diagnostics.py` (805 LOC; R1×15, R5×4, R6×1)<br>`web/execution/_validation_diagnostics.py` (781 LOC; R1×2, R5×2) |
| B53 | opus | 3201 | 21 | `web/execution/preflight.py` (724 LOC; R1×3, R5×2)<br>`web/execution/validation.py` (694 LOC; R5×1)<br>`web/execution/fanout_guard.py` (615 LOC; R1×2, R8×1)<br>`web/execution/accounting.py` (588 LOC; R1×5, R6×1)<br>`web/execution/outputs.py` (292 LOC; R6×1)<br>`web/execution/completion_gates.py` (288 LOC; R1×3, R5×2) |
| B55 | fable | 350 | 3 | `web/secrets/service.py` (350 LOC; R6×3) |

### Wave 3 — web/sessions + web/plugin_policy

| Bucket | Model | LOC | Findings | Files (findings by rule) |
|---|---|---:|---:|---|
| B54 | opus | 3352 | 121 | `web/plugin_policy/profiles.py` (1254 LOC; R1×42, R5×10, R6×2, R8×2, R9×1)<br>`web/plugin_policy/coverage.py` (897 LOC; R1×20, R5×9, R6×4, R8×5)<br>`web/plugin_policy/validation.py` (777 LOC; R1×11, R5×2, R6×2, R9×2)<br>`web/plugin_policy/availability.py` (271 LOC; R1×2, R6×2)<br>`web/plugin_policy/compiler.py` (153 LOC; R5×4, R6×1) |
| B56 | fable | 13600 | 158 | `web/sessions/service.py` (13600 LOC; R1×56, R4×1, R5×95, R6×5, R9×1) |
| B57 | opus | 5291 | 37 | `web/sessions/routes/composer/guided.py` (5291 LOC; R1×16, R4×2, R5×12, R6×5, R7×2) |
| B58 | opus | 3448 | 10 | `web/sessions/protocol.py` (3448 LOC; R1×5, R5×5) |
| B59 | opus | 3404 | 20 | `web/sessions/routes/_helpers.py` (3404 LOC; R1×11, R4×1, R5×5, R6×1, R7×2) |
| B60 | opus | 4729 | 48 | `web/sessions/routes/composer/guided_chat_atomic.py` (2030 LOC; R1×5, R4×1, R5×15, R6×7, R7×3)<br>`web/sessions/routes/composer/state.py` (1003 LOC; R1×1, R5×2, R6×3)<br>`web/sessions/routes/composer/guided_chat_intent_management.py` (944 LOC; R6×1)<br>`web/sessions/routes/sessions.py` (752 LOC; R1×5, R4×1, R6×4) |
| B61 | opus | 3297 | 57 | `web/sessions/routes/composer/guided_plan.py` (741 LOC; R1×2, R4×8, R5×7, R6×1, R7×1)<br>`web/sessions/schema.py` (506 LOC; R5×4, R6×1)<br>`web/sessions/guided_replay.py` (467 LOC; R1×4, R5×2)<br>`web/sessions/routes/composer/pipeline_settlement.py` (443 LOC; R1×1, R5×1)<br>`web/sessions/routes/guided_operations.py` (364 LOC; R1×4, R5×5)<br>`web/sessions/guided_audit.py` (340 LOC; R1×4, R5×1, R6×2)<br>`web/sessions/_auto_title.py` (320 LOC; R2×4)<br>`web/sessions/guided_payloads.py` (72 LOC; R5×3)<br>`web/sessions/guided_operations.py` (44 LOC; R1×2) |

### Wave 4 — web/_aws_ecs_acceptance (see note)

| Bucket | Model | LOC | Findings | Files (findings by rule) |
|---|---|---:|---:|---|
| B26 | opus | 4863 | 198 | `web/_aws_ecs_acceptance/operator_telemetry.py` (1167 LOC; R1×16, R4×2, R5×17)<br>`web/_aws_ecs_acceptance/scenario_inventory.py` (1090 LOC; R1×8, R5×27)<br>`web/_aws_ecs_acceptance/receipt_contracts.py` (967 LOC; R1×11, R5×31, R6×1)<br>`web/_aws_ecs_acceptance/orphan_sweep.py` (875 LOC; R1×25, R4×1, R5×23, R7×1)<br>`web/_aws_ecs_acceptance/bedrock.py` (764 LOC; R1×18, R4×2, R5×7, R6×1, R7×7) |
| B27 | opus | 4861 | 215 | `web/_aws_ecs_acceptance/capture.py` (723 LOC; R1×27, R5×6)<br>`web/_aws_ecs_acceptance/contracts.py` (645 LOC; R1×8, R2×1, R5×6, R7×1)<br>`web/_aws_ecs_acceptance/gate_ledger.py` (529 LOC; R5×4)<br>`web/_aws_ecs_acceptance/task_definition.py` (519 LOC; R1×48, R5×20)<br>`web/_aws_ecs_acceptance/manifest.py` (457 LOC; R5×4)<br>`web/_aws_ecs_acceptance/manifest_schema.py` (421 LOC; R5×16)<br>`web/_aws_ecs_acceptance/evidence.py` (371 LOC; R1×18, R5×16, R6×2)<br>`web/_aws_ecs_acceptance/s3.py` (366 LOC; R1×6, R4×10, R5×4, R6×3)<br>`web/_aws_ecs_acceptance/http_client.py` (296 LOC; R1×4, R5×1)<br>`web/_aws_ecs_acceptance/state.py` (286 LOC; R1×3, R5×1, R6×1, R7×1)<br>`web/_aws_ecs_acceptance/approvals.py` (248 LOC; R1×2, R4×1, R5×1) |
| B28 | fable | 636 | 21 | `web/_aws_ecs_acceptance/secure_documents.py` (237 LOC; R5×1, R6×3, R7×2)<br>`web/_aws_ecs_acceptance/receipt_store.py` (205 LOC; R1×9, R5×1)<br>`web/_aws_ecs_acceptance/textract.py` (194 LOC; R1×3, R4×1, R5×1) |

## Notes

- **B26–B28 (`web/_aws_ecs_acceptance`, 434 findings in ~10k LOC)** is the densest region by far: it is an acceptance harness parsing AWS API responses (Tier 3 by construction). Before spending 434 judge calls, decide whether these modules should carry `@trust_boundary` declarations at their parse entry points, which removes most of the R1/R5 corpus honestly. Wave 4 is deliberately last so that decision can be taken with the other waves' evidence in hand.
- **B38 `guided/planning.py` (178, 133×R1)** and **B54 `plugin_policy` (121)** are the same shape — `.get()` walks over planner/policy payloads. A single Tier-3 parse boundary per payload type is the likely fix; brief those lanes to look for the boundary, not 133 individual edits.
- **B56 `web/sessions/service.py` (13,600 LOC, 158)** is over the cap by itself and is the shared-checkout hot file. Run it alone, in Wave 3, with nothing else touching `web/sessions`.
- Concurrency: ≤8 worktree lanes at a time (24 CPUs, `-n 2` each); merge each wave with `--no-ff` and run the full suite once per wave, not per lane.
- Related open tracker items: elspeth-c201fc1dbc (23 app.py/doctor.py sites already rationalised — fold into B24/B25), elspeth-23ee8e3440 (stage_status paste-ready command must learn `--lanes`/`--continue-on-block` — land BEFORE the re-stage in step 2).
