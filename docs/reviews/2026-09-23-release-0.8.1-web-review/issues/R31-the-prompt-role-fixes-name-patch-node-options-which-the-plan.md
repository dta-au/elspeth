# R31. The prompt-role fixes name `patch_node_options`, which the planner surface does not have

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Composer, advisor and planner |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-13#2 |

## Finding

- **Location:** `src/elspeth/web/composer/state.py:1958-1977`, `tools/generation.py:1784`, and `capability_skill.py:17-38`.
- **Wrong:** A FIX that tells the planner to set the option "with patch_node_options" is sent to a planner whose tools are the discovery tools plus `emit_pipeline_proposal`.
- **Fix:** Use wording that works on any surface, for example "(patch_node_options in the tool loop, or re-emit the proposal with it set)".
- **Sources:** be-13#2.
- **Verifier notes:** This is worse than the reviewer said. A call to a non-discovery tool raises a terminal `DISCOVERY_ONLY` (`pipeline_planner.py:4901-4906`), so it fails the whole request rather than just burning a repair attempt. Severity stays low because the provider sees only the declared tools and existing catalogue entries already follow the same pattern.


## Source findings and verification

### be-13#2: Prompt-role fixes name patch_node_options, which the planner surface does not have

- **Reported at:** `src/elspeth/web/composer/tools/generation.py:1784`; reviewer severity low; category half-wired-teaching; diff-anchored True.
- **Summary:** Both new records serve fixes that tell the model to set the option 'with patch_node_options' (state.py:1958-1977). The hunk comment names the planner repair turn as a consumer, and that planner's roster is only the discovery tools plus emit_pipeline_proposal (capability_skill.py:17-38). The surface teaching says never to invoke an unadvertised mutation tool (planner_authoring_aids.py:393-399).
- **Failure scenario:** A plan_pipeline candidate lacks system_prompt. The repair turn receives a FIX saying 'set options.system_prompt with patch_node_options'. A model that obeys the FIX literally issues an undeclared tool call (recorded as undeclared_tool, pipeline_planner.py:1097-1098) and burns a repair attempt. A model that follows the surface teaching must set aside the only concrete instruction it was given.
- **Evidence:** capability_skill.py:17-38 is the planner roster, with no patch_node_options. state.py:1958 and :1973 contain 'with patch_node_options'. Sibling precedent: MULTI_QUERY_UNDECLARED_COLUMNS_REMEDY (plugins/transforms/llm/base.py:72-80) also names patch_node_options, so this repeats an existing pattern rather than introducing a new one.
- **Suggested fix:** Word the FIX so it does not depend on the surface, e.g. 'set options.system_prompt on that node (patch_node_options in the tool loop, or re-emit the proposal with it set)', or add planner-surface teaching that catalogue fixes naming mutation tools mean 're-emit with that change'.
- **Verifier (trace):** upheld, confidence medium, severity low. I traced the path end to end at 74c0ce0db and could not refute it. The freeform rootless dispatch (service.py:5229) calls plan_pipeline. Every candidate goes through Stage-1 validation, which calls _validate_llm_prompt_roles_present (state.py:7564, check at state.py:3813-3825). The repair feedback then adds each closed code's catalogue suggested_fix inline (pipeline_planner.py:2546-2548, 2743-2745). So a proposal whose llm node has no system_prompt reaches the planner with the FIX "set options.system_prompt with patch_node_options" (state.py:1958-1959, 1973-1974). The planner's declared tools are only the discovery tools plus emit_pipeline_proposal (capability_skill.py:17-38, pipeline_planner.py:1081). The surface text says never to invoke an unadvertised mutation tool (planner_authoring_aids.py:393-399). No planner-side teaching maps a catalogue fix that names a mutation tool to "re-emit the proposal", and recent-code-hints.md records no ruling on it. The generation.py:1782-1785 comment says the planner's repair turn is a consumer. One correction to the reviewer: the failure is worse than a burned repair attempt. A call to a tool outside the discovery set raises a terminal PipelinePlannerError DISCOVERY_ONLY (pipeline_planner.py:4901-4906), so the whole planner request fails. Severity stays low for three reasons. The provider is only shown declared tools, so a model has to invent a tool call for this to fire. The planner already receives the full freeform skill, which names set_pipeline and the patch_* tools throughout (pipeline_composer.md:117-122), so this FIX adds little new confusion. And sibling catalogue entries already follow the same pattern (splice_validation_failed names splice_transform, generation.py:1770-1778; MULTI_QUERY_UNDECLARED_COLUMNS_REMEDY).
