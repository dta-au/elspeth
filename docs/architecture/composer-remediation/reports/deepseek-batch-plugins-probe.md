# DeepSeek probe: batch plugins authorised (batch_stats, batch_replicate, batch_experiment_compare)

Tree composer-0812 @ 846bc4b0e. Planner `openrouter/deepseek/deepseek-v4.1-flash`, advisor `deepseek-v4-pro`, real provider calls, isolated instance 127.0.0.1:8791, started with `PROBE_EXTRA_PLUGINS=transform:batch_stats,transform:batch_replicate,transform:batch_experiment_compare`. It booted cleanly (0 `web plugin policy invalid` lines). No `/execute` call made. Server stopped by pid file; port 8791 confirmed free (the live service on 8000 was not touched).

**ONE MODEL, n=3 PER CASE (9 runs). Existence observations, not rates.** Runs were fired in parallel on one server, so tap lines interleave; I attributed tap calls to runs through each run's `provider_request_id` (audit rows) = the tap response id.

Raw: `.claude/lanes/skill-ab/deepseek-probe/batchplug/` (`runs/ds-batch-plugins/`, `an4.out` = emitted pipelines and rejections, `server.log`, `battery.log`); tap `data/deepseek-probe/provider-tap.jsonl` lines 285-312.

## 1. Visibility controls (GET /api/catalog/transforms, authenticated)
16 transforms returned: batch_duplicate_annotator, batch_experiment_compare, batch_replicate, batch_stats, batch_top_k, field_mapper, json_explode, keyword_filter, line_explode, llm, reference_join, report_assemble, truncate, type_coerce, value_transform, web_scrape.
- Added plugins: batch_stats True, batch_replicate True, batch_experiment_compare True.
- Positive control (core): value_transform True, web_scrape True.
- Negative controls: `batch_rank` False and `batch_effect_size` False (both exist in src/elspeth/plugins/transforms, not core, not added); `azure_content_safety` False; invented `batch_ghost_nonexistent` False.
The planner's discovery digest in the tap also lists all three (purpose, not_for, required_options), so the model saw them without any list call.

## 2. Per-run results (all 9 battery runs `completed`; all end in a pending proposal)

| Case | Rep | Accepted plugin | Options vs scenario | Repairs | Rejection codes | Accepted via |
|---|---|---|---|---|---|---|
| batch_aggregation | 1 | batch_stats | trigger count 5, group_by category, value_field amount, compute_mean true | 0 | none | planner |
| batch_aggregation | 2 | batch_stats | same | 0 | none | planner |
| batch_aggregation | 3 | batch_stats | same (flexible schema, required category/amount) | 0 | none | planner |
| deaggregation | 1 | batch_replicate | trigger count 3, copies_field copies, include_copy_index true | 3 planner repairs then REPAIR_EXHAUSTED | schema_contract_violation + sink_contract_violation x2, plugin_options_invalid | **advisor hatch** (attempt 5) |
| deaggregation | 2 | batch_replicate | same | 0 | none | planner |
| deaggregation | 3 | batch_replicate | same | 3, REPAIR_EXHAUSTED | schema_contract_violation + sink_contract_violation x2, plugin_options_invalid | **advisor hatch** |
| row_union_ab_experiment | 1 | batch_experiment_compare | variant/score field, baseline control; gate fork 2, 2 value_transform (x1.0, x1.25), row_union, jsonl sink; no trigger (end of source) | 0 | none | planner |
| row_union_ab_experiment | 2 | batch_experiment_compare | same, plus an extra `field_mapper` tidy step | 1 | row_union_downstream_group_invalid | planner (repair) |
| row_union_ab_experiment | 3 | batch_experiment_compare | same | 1 | plugin_options_invalid (source `inline_blob` placed inside `options`) | planner (repair) |

Notes per case:
- Prompts requirement check: batch_aggregation asks for groups of five, count/total/average by category (matches floor). deaggregation asks "three at a time", copies, numbered copies, CSV (trigger 3 and include_copy_index present in all three; observed in hatch outputs too). row_union asks both arms labelled, 25% higher treatment, two rows per ticket, whole-stream comparison vs control, JSON lines: all three have `x1.25`, `baseline_variant: control`, `format: jsonl`.
- Nothing silently substituted or omitted in the final pipelines. The only deviation is run 2's extra `field_mapper` (select_only, identity mapping), a benign unrequested step. The model-facing user text is the canned "pipeline proposal needs your acceptance"; I did not see model prose disclosing choices, so the check is on the pipelines only.
- Discovery, not hesitation about availability: every run called `get_plugin_schema` for the batch plugin directly after reading the digest (batch_aggregation 1 reasoning: "The digest says batch_stats required_options: schema, value_field... It likely has group_by. Let me get the plugin schema"). Six of nine runs also called `get_plugin_assistance`. The hesitation seen was over semantics, never over whether the plugin is available: e.g. "'work through them three at a time' ... suggests a trigger count of 3" (deagg), "batch_stats output is 'derived summary row(s)' ... Output fields unknown ... Let me check assistance" (batch_aggregation 1), "Does batch_stats support group_by? Likely" (batch_aggregation 3, before the schema call).
- Compare with the earlier behaviour (`deepseek-planner-trace-findings.md`): batch_aggregation was a (correct) decline, deaggregation was silently incomplete (4 substitute nodes, "three at a time" dropped), row_union_ab_experiment was a substitution (per-row delta, no whole-stream aggregation). Now: 9/9 use the intended plugin with the stated requirements; no declines, no substitutions. This supports the finding that the earlier outcomes were a policy-visibility gap, not planner reasoning.

## 3. Defects / rough edges in the plugins' web authoring path

1. **batch_replicate: input-schema vs output-field confusion; error text does not point at the fix (2 of 3 deagg runs reached REPAIR_EXHAUSTED, all via the same trap).** The model put the created field `copy_index` into the node's `schema.fields` (input contract). Rejection: `schema_contract_violation`, consumer expand_labels, `missing_fields: ["copy_index"]`, `producer: source`, plus a cascading `sink_contract_violation` (expand_labels "guarantees" none of the fields). The `suggested_fix` says to make the producer guarantee the field ("patch_source_options for a source ..."), which is the wrong direction: the correct repair is to delete `copy_index` from the aggregation's input schema. The model did reason "copy_index is created by the node itself" (run 1 call 290) but then tried `required: false`, then `guaranteed_fields`, hitting a third error: `plugin_options_invalid` detail "'guaranteed_fields' contains optional fields not guaranteed by schema: copy_index" (this one is actionable). Only the advisor, by switching to `mode: observed` plus `required_fields` only, escaped. The batch_replicate `composer_hints` say "avoid input fields with [the name copy_index]" but never that `schema` describes INPUT rows and that the node's added `copy_index` should not be declared there. A hint plus a targeted suggested_fix for "missing field is the consumer's own produced field" would remove this. Candidate for a defect ticket; the cost is 3 failed repair rounds plus an advisor call (reasoning tokens in the hatch call: 7248 and 3239).
2. **batch_stats assistance does not name its output fields** (count, sum, mean per `summary`, but the hint text says only "derived summary row(s)"). The model spent a round on `get_plugin_assistance` just to learn the output names and still reasoned "Output fields unknown". No failure resulted; the downstream sink was observed-mode.
3. **row_union_downstream_group_invalid** (row_union run 2): the model set `trigger: {count: 16}` on batch_experiment_compare after a row_union. The rejection was clear and actionable ("omit trigger or use trigger: {} so only end_of_source flushes"), and the repair worked on the next try. No defect, noted as a good message.
4. No missing-assistance or schema-shape defects found for batch_experiment_compare; its hints name the output metrics (mean_delta, relative_lift, bounds).
5. Run 3 of row_union: `inline_blob` nested in source `options` rejected with "path: Field required / inline_blob: Extra inputs are not permitted"; a general source-authoring slip, not plugin-specific, repaired in one round.

## Limits
n=3 per case, one planner model, one session each; parallel execution. I did not run or preview the pipelines (no `/execute`), so "accepted" means accepted as a proposal by the composer's validation, not that the pipeline produces correct output. I did not verify batch_stats's per-batch `group_by` output semantics against runtime. Executing, accepting proposals and the interpretation cards were out of scope.
