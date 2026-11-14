# DeepSeek v4.1 flash planner trace: what in the skills and tool text confused the model

Evidence: `data/deepseek-probe/provider-tap.jsonl`, calls 0..60 (later lines were appended by another agent's use of the probe and are not analysed). Probe model `openrouter/deepseek/deepseek-v4.1-flash`, planner surface (23 read-only tools plus `emit_pipeline_proposal`).
Labels: **[V]** verified against raw tap output or source in this session; **[I]** inferred from reasoning text or reading; **[U]** unverified.
Reasoning-character figures are character counts of reasoning paragraphs matching a keyword set (measured by script over the tap). They over-count, since one paragraph often carries several themes, and are meant for ranking only.

## 0. Call-to-case map and cost [V]

The case is read from `intent` in each request. Calls 0-1 are the threshold_gate smoke. Calls 2-20 are the canary repeats. Call 23 is an advisor call.

| case | calls | reasoning chars | secs |
| --- | --- | --- | --- |
| template_lookups | 56-57 | 148k | 75 |
| deaggregation | 26-27 | 123k | 78 |
| canary (all repeats) | 2-20 | 121k | 86 |
| row_union_ab_experiment | 49-51 | 90k | 54 |
| deep_routing | 28-29 | 69k | 40 |
| multi_query_assessment | 43 | 59k | 42 |
| openrouter_sentiment | 44-45 | 46k | 31 |
| fork_coalesce | 36-37 | 41k | 26 |
| batch_aggregation | 21-22 | 41k | 25 |
| llm_source | 41-42 | 40k | 25 |
| threshold_gate (smoke and call 58) | 0-1, 58 | 40k | 26 |
| statistical_batch_plugins | 54-55 | 34k | 21 |
| report_assemble | 46-48 | 35k | 22 |
| explicit_routing | 32-33 | 32k | 22 |
| boolean_routing | 24-25 | 27k | 16 |
| error_routing | 30-31 | 27k | 18 |
| schema_contracts_demo | 52-53 | 24k | 16 |
| json_explode | 38-40 | 22k | 16 |
| transform_pipeline | 59-60 | 19k | 12 |
| field_drop | 34-35 | 12k | 10 |

Total about 1.05M reasoning characters. The tap has 72 `get_plugin_schema`, 32 `emit_pipeline_proposal`, 10 `get_plugin_assistance` and 3 `list_*` calls [V].

Every case has the same shape: one discovery turn, then the harness message "All declared information gaps are closed; emit the terminal proposal now.", then one emit turn. The exceptions are the three rejections in section 2 and json_explode, which used an extra assistance turn.

## 1. Recurring confusion points, ranked

### C1. Who authors the `invented_source` requirement on the proposal-only surface (about 25% of all reasoning)

- **Reach [V]:** 57 of 61 calls mention it, about 216k characters in matching paragraphs. 31 emits carried `inline_blob`. About half authored `source.options.interpretation_requirements=[{invented_source, inline_source_data, draft}]` (calls 1, 10, 12, 16, 25, 27, 31, 37, 45, 47, 48, 50, 51, 53, 58, 60). The other half omitted it (3, 4, 6, 8, 11, 14, 18, 20, 29, 33, 35, 40, 55). All were accepted and got the same "staged for your review. It has interpretation review cards" message.
- **Quotes:** "Hmm, but is user_term allowed to be arbitrary? The invented_source user_term is server-derived…" (call 0). "Given ambiguity, I lean toward including it" (24). "Safer: omit the invented_source row and let the backend auto-stage it" (55). "I'll leave it out — the backend stages it" (40). The model also worried that retyping the whole artifact into `draft` is byte-unsafe (50, 54, 55).
- **Contradictory passages:**
  - `src/elspeth/web/composer/skills/pipeline_composer.md:580-586`: "keep its backend-staged `invented_source` … The `user_term` is server-derived and already staged … never invent".
  - `pipeline_composer.md:1233`: the ownership matrix says "backend on generated blob bind".
  - `pipeline_composer.md:765-767`: "keep the backend-staged row".
  - `src/elspeth/web/composer/tools/_common.py:182` and `:193` (the emit-tool schema note): "On a proposal-only surface, include the pending requirements in the appropriate source/node options".
  - `src/elspeth/web/composer/planner_authoring_aids.py:466`: "Author rows only for the planner-owned kinds (vague_term …, registered pipeline_decision, invented_source)".
  - `planner_authoring_aids.py:403`: "stage the invented_source interpretation review".
  - The model reads the last three as "author it" and the first three as "don't".
- **What the code does:**
  - [V] `src/elspeth/web/composer/tools/sources.py:383-396` (`_options_with_source_blob_review`) builds the requirement from the bound bytes, with `user_term` from `_source_blob_review_user_term` (`sources.py:362-372`) and `draft=content`. It passes `replace_kind=INVENTED_SOURCE`, so it replaces any authored row. It is invoked from `tools/sessions.py:391`.
  - [I] The proposal path binds `inline_blob` through the same function. The evidence is that rows with and without the authored row were accepted identically.
  - [U] I did not trace the sealed-proposal compile path end to end. The terminal `reviews.json` files are empty because the surface is excluded from scoring.
- **One-sentence resolution:** "On the proposal-only surface, bind generated rows with `source.inline_blob` and do not author an `invented_source` row; the server stages it from the bound bytes, and an authored row is replaced."

### C2. Catalog said to be "complete" but its own text points at plugins that are not in it (decline, silent downgrades, about 250k characters)

- **Reach [V]:** The digest lists 13 transforms. In the digest text:
  - `batch_top_k.not_for` names `batch_distribution_profile` (`src/elspeth/plugins/transforms/batch_top_k.py:100`).
  - `line_explode` and `reference_join` name `blob_csv_expand` (`plugins/transforms/line_explode.py:353`, `reference_join.py:909`).
  - `web_scrape` names `blob_fetch` (`web_scrape.py:840`).
  - `src/elspeth/web/composer/planner_authoring_aids.py:1916` says the digest "is the complete selection".
- **Effect on the case floors [V]:** Three floors depend on plugins that are not in this deployment's visible catalog:
  - batch_aggregation needs `batch_stats`, which exists in `src/elspeth/plugins/transforms/batch_stats.py` with `group_by` plus sum, count and mean.
  - deaggregation needs `batch_replicate`.
  - row_union_ab_experiment needs `batch_experiment_compare`.
- **Why hidden [U]:** The catalog is filtered by `WebPluginPolicy.authorized` (`src/elspeth/web/plugin_policy/models.py`). I could not determine whether the probe deployment's policy or the product default omits them. This is a product question, not a guidance defect.
- **Quotes:**
  - Call 22: "batch_distribution_profile is referenced … as if it exists … Hmm, that's a strong hint …".
  - Call 22: "Maybe the digest is incomplete. Let me call list_transforms" (it then called `list_transforms`, 66 KB).
  - Call 50: "Hmm, but … the digest says it's complete … Actually, maybe I should call list_transforms … But I've been given a terminal instruction: emit now".
  - Call 27: roughly 50k characters circling `batch_top_k`, `batch_duplicate_annotator` and `report_assemble` to find a "three at a time" mechanism.
- **One-sentence resolution:** Render `not_for` and hint text only with plugin names that are visible in this deployment's catalog, and say "the list above is every plugin you may use; a plugin named in another plugin's prose that is not listed is unavailable, not hidden".

### C3. The review taxonomy for LLM and routing nodes (about 130k characters across 37 calls)

- **Reach [V]:** Matches by theme:

| theme | calls | chars |
| --- | --- | --- |
| `vague_term` | 31 | 36k |
| `pipeline_decision` / `gate_condition_authored` / `review_registry` | 37 | 53k |
| cleanup / data minimization | 29 | 40k |
| shield | not separately counted | |

- **Worst cases:** template_lookups (calls 56-57, 41k plus 25k characters on "tone"/ambiguity plus 18k and 12k on review staging) and multi_query (call 43: 13.6k characters on cleanup and projection).
- **Quote (call 43):** "Hmm, this is getting complicated. Let me look at the exemplar…". Call 45: "I must stage a vague_term for authored judgement … two vague_terms … one for the confidence scale", which stages a review for a scale the user did not constrain but also did not ask to be reviewed.
- **Passages:**
  - `pipeline_composer.md:25-28`, `:886-923` (`vague_term` and the `prompt_template_parts` wiring).
  - `:953` (`gate_condition_authored`).
  - `:1193` (Output data minimization) and `:1444` (the rejection-table row that says insert a cleanup transform and stage `pipeline_decision`).
- **Observed result [V]:** Cleanup transforms were added voluntarily. Deaggregation added `field_mapper` with `select_only`, and call 43 weighed one. Over-staging occurred in call 45.
- **One-sentence resolution:** "Stage a `vague_term` only for a scale, rubric or boundary you chose and the user did not state; do not add a projection or cleanup transform unless the user asked to drop fields or raw web or LLM text would otherwise reach a sink."

### C4. The skill teaches tools the planner cannot call (about 67k characters)

- **Reach [V]:** `request_interpretation_review` is mentioned in 42 calls (67k characters). `handoff_to_user` appears in 23 calls (21k) and `set_pipeline` in 22 (16k). `preview_pipeline` appears in 13 (12k). `request_advisor_hint` appears in 2.
- **Passages:** `pipeline_composer.md:121-130` has one clause, "The proposal-only palette instead permits an ordinary text reply", buried in an advisor paragraph. `:165` (step 5, "surface every staged assumption with request_interpretation_review") and `:580` are written for the incremental surface.
- **Quote (call 24):** "I don't have request_interpretation_review in my function list. Right, … on this surface … include pending requirements".
- **Consequence [V]:** This is the engine of C1. The model derives its authoring duties from instructions for a different surface.
- **One-sentence resolution:** Add a short "surface map" at the top of the skill: "On the planner surface you have only discovery tools plus `emit_pipeline_proposal`; ignore every instruction to call `set_pipeline`, `upsert_*`, `request_interpretation_review` or `request_advisor_hint`."

### C5. Blob-bound source options (about 10k characters in 24 calls, plus one rejection)

- **Reach [V]:** The text and json source schemas show `path` as required. The custody rule says "author schema.mode and on_validation_failure on a blob-bound source, never path or blob_ref" (`planner_authoring_aids.py:~360`, where the aid text says "Custody owns the storage binding"). The model checks this each time (calls 34, 38, 39, 46, 47).
- **Canary rejection [V] (call 10 to 11):** the first emit put `"inline_blob": null` inside `source.options` and was rejected with `plugin_options_invalid` ("inline_blob: Extra inputs are not permitted"). The message carries the full `plugin_contracts` (5 KB).
- **Related inconsistency [V]:** The model switches between `fields: ["name: str"]` and `fields: [{"name":..,"field_type":..}]` across cases (compare calls 25 and 55). Both are accepted. The prompt shows only one form.
- **Resolution:** Add one planner-surface exemplar showing a csv, json and text `inline_blob` source with no `path`, and state which `fields` form is canonical.

### C6. Schema mode and declared types (about 100k characters across 43 calls, small per call)

- **Reach [V]:** 43 calls on observed/flexible/fixed (51k), 33 calls on whether declared types coerce (48k).
- **Quote (call 25):** "Risk: CSV source coercion of 'true'/'false' to bool … Actually, risk … I'll declare approved: str and gate row['approved'] == 'true'".
- **Evidence:** The skills do not say how a CSV `"true"` becomes `bool`; the only coercion mention is `pipeline_capabilities.md:318` (`boolean → bool`). Whether the CSV source coerces `true`/`false` to `bool` is [U].
- **Resolution:** One sentence in the CSV hints: which string spellings coerce to bool, int, float.

### C7. Gate wiring (about 30k characters in 31 calls, small per call)

- **Reach [V]:** `on_error` fail-fast (31 calls, 20k) and routes to sink name versus connection name (17 calls, 9k).
- **Evidence:** Deep_routing and error_routing show the same restatement each time, for example "Actually routes values can be sink names" (calls 29, 31). The model resolves it correctly each time.
- **Resolution:** Low priority; one example of a chained gate (`routes` naming a sink and a downstream connection) would end this.

### C8. Forced "emit now" while a fact is missing

- **Reach [V]:** 7 calls (6.4k characters). Call 39 considered one more `get_plugin_assistance` to learn the json_explode index field name and complied only after reasoning about it.
- **Evidence:** The `schema_contract_evidence` block was empty (`omitted: []`), so every fact came from discovery.
- **Resolution:** None needed beyond fixing `get_plugin_assistance` (see section 3).

## 2. Rejections and the decline

### report_assemble (call 47 emit, rejected, re-emitted in call 48) [V]

- **Rejected:** Node `pair_reports` (a `report_assemble` aggregation) carried `required_input_fields`.
- **Exact text the model received:** `batch_required_fields_invalid`: "A batch-aware plugin cannot enforce the row-mode required_input_fields option; batch input requirements use its schema contract." `suggested_fix`: "Remove required_input_fields from the rejected node's options. Declare batch input requirements with schema.required_fields using the plugin's documented schema shape; call get_plugin_schema for the transform plugin to inspect that contract, then re-emit the corrected node."
- **Actionable?** Yes. The next call used about 1.8k reasoning characters and fixed it in one shot.
- **Did the skill lead it in?** Yes. `pipeline_composer.md:34` says to check each consumer's `required_input_fields`, and `grep` finds no mention of `schema.required_fields` in either skill or in `planner_authoring_aids.py`. The rejection code is raised in `src/elspeth/web/composer/tools/sessions.py:1619` and `tools/transforms.py:783`.

### row_union_ab_experiment (call 50 emit, rejected, re-emitted in call 51) [V]

- **Rejected:** The `row_union` node `label_union` used arbitrary branch aliases.
- **Exact text:** `row_union_branch_alias_unreachable`: "A row_union branch alias does not match a fork branch declared upstream." `suggested_fix`: "Use the upstream gate's fork_to branch names as the row_union branches keys."
- **Actionable?** Yes; the model fixed it with about 0.3k reasoning characters.
- **Did the skill lead it in?** Partly. `pipeline_capabilities.md:212-221` says "Declare at least two ordered `branches` as `{branch_name: input_connection}`" without saying that `branch_name` is the `fork_to` name. `planner_authoring_aids.py:485` does say it, but the model did not act on it.

### Other rejections [V]

- **Canary repeat (call 10 to 11):** `plugin_options_invalid`, described in C5. Not one of the two cases you named; canary is surface-excluded.
- **A failed discovery call (call 36 to 37):** `get_plugin_assistance` was called with `name` instead of `plugin_name`. It returned `SCHEMA_VALIDATION` ("Required value is missing" at `plugin_name`; "Unexpected value"). `get_plugin_schema` takes `name` and `get_plugin_assistance` takes `plugin_name` (`src/elspeth/web/composer/tools/generation.py:2420`).

### batch_aggregation decline (calls 21-22) [V]

- **What the model said:** the visible catalog has no sum or average aggregation. It named `batch_top_k`, `batch_duplicate_annotator` and `report_assemble`, rejected `value_transform` (row-level only) and rejected using an LLM. It declined with plain text and asked the operator to enable a numeric statistics plugin.
- **Was declining correct?** Correct for the catalog it saw. The scenario floor needs `batch_stats` with `group_by=category`, `value_field=amount`, `compute_mean`, `trigger.count=5`, and that plugin exists in source but is not in the visible catalog [V]. So the decline was an accurate report of a deployment gap, not a reasoning error.
- **Did guidance push it wrongly?** Partly. Two contributing misleads:
  1. The `batch_top_k` `not_for` points to a plugin that is not available (C2).
  2. The model asserted "this is not a security policy denial", yet the visible catalog is itself policy-filtered, and the `prohibited` list is empty [V]. I could not establish the policy reason [U].
- **Inconsistency [V]:** batch_aggregation declined, while deaggregation and row_union_ab_experiment hit the same gap (a missing batch plugin) and shipped a substitute (section 4). The skill has no rule for choosing between them.

## 3. Wrong or wasteful tool use

- **Bloated payloads [V]:** `list_transforms` 66,237 bytes, `list_sources` 16,532, `list_sinks` 16,483 (all in call 22, which the digest says are rarely needed). Largest `get_plugin_schema` is `llm` at 16.7 KB. Largest `get_plugin_assistance` is `llm` at 12.6 KB.
- **Noise in every discovery result [V]:** each discovery response is wrapped in `"validation": {"is_valid": false, "errors": [no_source_configured, no_sinks_configured]}` for the blank pipeline. The model ignored it but pays the tokens in 72+ results.
- **Redundant assistance [V]:** 10 `get_plugin_assistance` calls. For the json source, 5 of 8 long strings in the assistance result also appear in the `get_plugin_schema` result (`composer_hints`).
- **Failed call [V]:** the argument-name mismatch above (call 36).
- **Repeated identical schema loads [I]:** 72 `get_plugin_schema` calls for 31 emits, about 2-3 per case, mostly the same csv or json source and sink. The `schema_contract_evidence` that could supply them is empty (`omitted: []`).
- **Tools the model wanted but could not use [V]:**
  - `request_interpretation_review` (C4).
  - A way to check a draft before emit. `preview_pipeline` is on the palette but needs state, and the model never used it.
  - A way to learn why a plugin is unavailable (call 22).
  - A multi-row `llm` source. The model noted "emits at most one row" (call 41), then built `llm` source plus `line_explode`.
- **Not used [V]:** `get_expression_grammar` was never called even though 15 calls mention it, because the grammar is supplied in `authoring_aids`. `list_models` was never called.

## 4. Correctness spot-check (6 cases) [V unless marked]

Pipeline contents were read from the emit call arguments in the tap.

1. **template_lookups (call 57), partly wrong against the prompt.** The prompt asks for classification into billing, technical or general, "taking the priority into account for the tone it recommends", with the answer in one `classification` field. The model added a second field `recommended_tone` (string) and invented a mapping from priority to tone in the prompt ("critical priority gives an urgent and apologetic tone…") staged as a `vague_term`. The floor has no tone field. The structure is right: llm node with enum `classification` and `on_error` to the quarantine sink, both outputs JSON. The prompt itself is ambiguous (call 56 spent about 41k characters on it), so this is partly a corpus defect. The invented priority-value mapping (`critical/high/medium/low`) is data the model made up for a field the user never defined [I].
2. **deaggregation (call 27), silently incomplete.** Four nodes (`value_transform` to `(name + '\n') * copies`, `line_explode` with index, `value_transform` `copy_index + 1`, `field_mapper select_only`) give one row per label, numbered from 1, as CSV. The "work through them three at a time" part is dropped. The metadata description says nothing about it. The reasoning calls this "a downgrade" [V] and ships anyway. The floor expects `batch_replicate` with `trigger.count=3`. Whether `line_explode` on `"name\n"*n` emits a spurious empty trailing row is [U].
3. **deep_routing (call 29), correct.** Screening by `keyword_filter` (`on_error` to a quarantine CSV), `field_mapper` renaming the three columns, `truncate` notes to 40, then chained gates: `<5000` micro; `>=700` then `mortgage` then `term>=240` long or short, other types approved; `<700` then `>=50000` high risk else manual review. Seven outputs. The blocked patterns are `[Pp]assword|[Pp]asswd|[Ss]ecret|[Cc]onfidential|[Ii]nternal|[Cc]redential`, which miss all-caps spellings although the metadata calls them "case-tolerant" (minor overclaim). Generated rows include a flagged "Internal note", "password is hunter2", and "Confidential valuation", so quarantine is exercised.
4. **boolean_routing (call 25), correct but typed as text.** The `approved` flag is `str` with values `true`/`false` and the gate is `row['approved'] == 'true'`. It satisfies the split, but the CSVs carry the text, not a boolean. Chosen to avoid the unclear coercion (C6).
5. **fork_coalesce (call 37), correct.** `truncate` 40, a gate with `fork_to [path_a, path_b]`, a `coalesce` with `merge: nested` and `require_all`, a final `condition: True` gate, then JSON. Branches have no per-path transform, which the model worried about (call 36). Nesting gives each path's copy under its own key.
6. **statistical_batch_plugins (call 55), correct structure, weak data.** `batch_top_k`, `field=predicted_label`, `k=3`, `group_by=model_name`, trigger count 8, JSON lines. The 24 generated rows cycle four models, so each batch of 8 holds only 2 rows per model and the "three most frequent labels" is trivial per batch. Not wrong, but the demonstration data does not exercise `k=3`.

Also read: **row_union_ab_experiment (call 51), a substitution.** The prompt asks to "compare the arms against the control" once the whole stream is through. The accepted pipeline computes a per-row `score_delta` and `percent_change` against `baseline_score` with `value_transform`; there is no end-of-stream aggregation. The metadata description records the per-row choice but not that the whole-stream comparison was not built. The percent change divides by `baseline_score` with no zero guard [I]. The floor expects `batch_experiment_compare`.

**llm_source (call 42).** `llm` source with `line_explode` and a JSONL sink. The prompt asks for single-line JSON objects, and each line lands as a string in one field, so the JSONL rows hold JSON-as-string rather than ticket objects. The floor is `llm` source straight to the sink. [I]

## 5. Top 10 fixes (impact = cases affected × reasoning wasted or wrongness ÷ effort)

1. **Settle `invented_source` authorship (C1).** Edit `pipeline_composer.md:580-586` and `:1233`, `tools/_common.py:182,193` and `planner_authoring_aids.py:403,466` to agree on one rule. Replacement text: "Proposal-only surface: bind generated rows with `source.inline_blob`. Do not author an `invented_source` requirement; the server stages it from the bound bytes and replaces any row you author. Author only `vague_term` and registered `pipeline_decision` rows." Affects about 31 of 31 inline-blob emits and about 25% of reasoning. Effort: five text edits. Confirm the proposal-path claim first [I].
2. **Add a surface map at the top of `pipeline_composer.md` (C4).** Replacement text: "PLANNER SURFACE: you have discovery tools and `emit_pipeline_proposal` only. Any instruction below to call `set_pipeline`, `upsert_*`, `request_interpretation_review` or `request_advisor_hint` does not apply to you. To refuse, reply in plain text." About 42 calls. Effort: one paragraph.
3. **Make the catalog statement true (C2).** Filter plugin names out of `not_for` and hint prose when they are not in the deployment's visible catalog, and reword `planner_authoring_aids.py:1916`: "These are all the plugins you may use; a name in another plugin's prose that is absent here is unavailable." Affects batch_aggregation, deaggregation, row_union, llm_source and the large catalog-doubt blocks (about 250k characters). Effort: small code change in the digest renderer, or edits to the four plugin `not_for` strings.
4. **Resolve the plugin-policy gap behind three floors.** Decide whether `batch_stats`, `batch_replicate` and `batch_experiment_compare` belong in the web `authorized` set (`src/elspeth/web/plugin_policy/models.py`) or whether the floors should change. This is a product decision. Today three of 19 cases cannot be built faithfully, and the planner reports that correctly [V]. Effort: decision plus a policy or corpus edit.
5. **Add a downgrade-disclosure rule.** `pipeline_composer.md:121-130` covers declining only. Replacement text: "If you ship a pipeline that omits or substitutes any part of the request, say so in the first sentence of `metadata.description` ('Not built: …'). If the omitted part is the core operation, decline instead." Affects deaggregation, row_union, llm_source, template_lookups (silent omissions and substitutions) and removes the inconsistency with the batch_aggregation decline. Effort: one paragraph.
6. **Narrow when a review row is required (C3).** Replace the long `vague_term` and minimization passages (`pipeline_composer.md:886-923`, `:1193`, `:1444`) with a short decision table: stage `vague_term` only for a scale, rubric or boundary you chose; never for a category list or threshold the user stated; add no cleanup or projection transform unless the user asked to drop fields or raw web or LLM text would reach a sink. Affects about 37 calls (53k characters), including over-staging in call 45. Effort: medium, because it must keep the policy-required rows.
7. **Fix the discovery tool contract.** Make `get_plugin_assistance` accept `name` (or rename `plugin_name` to `name`, `tools/generation.py:2420`), and drop the assistance fields that duplicate `composer_hints` from the schema result (5 of 8 strings duplicated for json source). Also strip the blank-pipeline `validation` block from discovery results, and cap `list_transforms` to names and one-line purposes (66 KB now). Effort: low to medium. One failed call and about 13 KB per `llm` assistance call.
8. **Fix two skill lines that led to the rejections.** In `pipeline_capabilities.md:212` change `{branch_name: input_connection}` to `{fork_to branch name: connection published by that branch's last transform}`. In the aggregation guidance add: "Aggregation nodes: never set `required_input_fields`; declare input requirements in `schema.required_fields`." Both rejections were actionable, so this saves only a turn each, but it is two sentences.
9. **Add planner-surface exemplars for blob-bound sources (C5).** One csv, one json and one text `inline_blob` source showing no `path`, the canonical `schema.fields` form, and where `inline_blob` goes (a source sibling, never in `options`). Removes the canary rejection and about 10k characters of path doubt.
10. **State CSV type coercion in the CSV source hints (C6).** One sentence: which text values coerce to `bool`, `int` and `float` under a declared schema. The model otherwise avoids declared types (call 25) or doubts them in 33 calls (48k characters). Effort: one hint line, after checking the coercion code [U].
