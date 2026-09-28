# Guidance review: what ELSPETH tells the planner, checked against lane elspeth-5887

This review is read-only. No guidance, source or test file was edited, and nothing was written to git.

**What was reviewed.** `fix/5887-rebased` at **`4bb77fbd51fe215e0a280688d265c17ff160eaea`**, taken with `git archive` into `scratch/guidance-review/tree/`. Base is `63ffb7ee1` (130 lane commits). Two side-branch tips were archived the same way:
- `fix/5887-codex-final` at `688a654fe`, in `tree-codex-final/`
- `fix/5887-example-rank` at `55dbdf290`, in `tree-example-rank/`

**What it was checked against.**
- The changes in RULINGS.md, STATUS-AND-PLAN.md and BATCH-SUITE-PLAN.md.
- Decided but not yet implemented work:
  - T1: the arbiter's typing of computed rewrites (specialist2-arbiter.md)
  - G3: the template row API (specialist2-template-api.md)
  - C2: `tojson` over a projected row
  - E7 / G1b: `union_collision_policy` (patches/G1-E7-union-collision.patch)
  - C3: value-free reasons
  - Merge 2: six new batch plugins

Each change is one row in the matrix at `scratch/guidance-review/change-matrix.md` (rows M01–M33). Findings cite those rows.

**How it was done.** Four review agents each took one group of surfaces. Every claim about behaviour was probed with `python -m elspeth.cli validate --settings <ABS>` and, where needed, `run --execute`, on the archive, with `PYTHONPATH=<archive>/src:<archive>/elspeth-lints/src`. Every probe log prints `elspeth.__file__` = `.../scratch/guidance-review/tree/src/elspeth/__init__.py`. Probe configs and logs are in `scratch/guidance-review/probes/{A,B,C,D}/`; the evidence notes below cite them by case name. I re-read the evidence for S-02, P-03, T-01 and the S-03/S-04 cases myself. For S-02 I also ran an extra execute probe (`probes/X/`). For T-01, the `vt1` audit database's `transform_errors` row reads `{"actual":"str","expected":"int","field":"label",…,"reason":"type_mismatch"}`. The harness also refused my own attempt to save the per-agent findings as scratch files, so their content lives only in this report. The probe directories hold every config and log cited.

The harness refused to let the agents write their own findings files, so this document is the only consolidated record. The behaviour-diff report at `REPORT-4bb77fbd5.md` has no observations (every entry is MISSING) and is not cited anywhere.

---

## Summary

**Counts:**

| priority | count | of which true now | of which becomes true when unlanded work merges |
|---|---|---|---|
| P1 | 18 | 13 | 5 (S-06, S-07, R-04, R-05, P-04) |
| P2 | 31 | 29 | T-03 and P-06 become P1 when T1 lands |
| P3 | 9 entries | | |

**Top 5:**
1. **T-01 / S-05.** Every tool schema and the composer's error table say a node's `schema:` declares what arrives, "never its transformed result". Under ADR-050, for a field the node creates, it *is* the output declaration and is enforced on every row.
2. **S-03 / S-04 / R-02 / R-03.** The row-template teaching is wrong in two places:
   - "`[]` shows the whole row" is false: `| tojson`, `{{ row }}` and method calls are broken under `[]`.
   - The claim that `row.to_dict()` / `row.keys()` "read fields" is false.

   The refusals then send the planner to `[]`, or to "declare `keys`". Both produce a config that validates while every row fails.
3. **R-01.** The only remedy text the planner receives for the passthrough refusal still describes the retired rule that named `batch_replicate`.
4. **S-01 / S-02.** `pipeline_composer.md` says to use "the literal observed header", and says a template reads `row['Price USD']` by its header spelling. The spelling rule and the config check refuse both.
5. **P-01 / T-02.** The `required_input_fields` description (shown on 24 transforms) predates ADR-051 and points at a Python function the planner cannot call. The multi-query aid calls `row.source_row` "the whole source row".

### Summary table

| id | surface | pri | one line | depends on |
|---|---|---|---|---|
| S-01 | composer.md:527-531 | P1 | "use the literal observed header" contradicts the spelling rule in the next paragraph | — |
| S-02 | composer.md:549-552 | P1 | says a template reads `row['Price USD']` by header spelling; config refuses it under a declared list, though the runtime resolves it | lane-owner decision: extractor or teaching |
| S-03 | capabilities:297-299 | P1 | "`[]` shows the whole row" is false for `tojson`, `{{ row }}` and methods | final wording: G3, C2 |
| S-04 | capabilities:302-304 | P1 | "`row.to_dict()` … read fields of those names" is false | G3 |
| S-05 | composer.md:1236 table row | P1 | "a value_transform schema declares what ARRIVES … never the result" | — |
| S-06 | capabilities:136-139 (HEAD and example-rank) | P1 (conditional) | the passthrough line names a plugin count that is wrong after each merge | example-rank, merge 2 |
| S-07 | capabilities:318-325 | P1 (conditional) | "an expression result … is `any`" | T1 |
| S-08 | capabilities (templates) | P2 | a whole-row read with no declaration renders empty; not taught | G3 (refusal) |
| S-09 | capabilities / composer §Untrusted content | P2 | an omitted declaration is as unprovable as `[]` to a prompt shield | — |
| S-10 | composer.md:533-537 | P2 | HEAD lacks the `field_mapping` / headerless-CSV spelling teaching | codex-final |
| S-11 | composer.md one-shot table | P2 | missing rows for `edge_field_type_incompatible` and the runtime reasons | — |
| S-12 | capabilities aggregation | P3 | a failed batch → `on_error`, and `discard` = quarantined, not taught | — |
| S-13 | guided step_3_transforms.md | P3 | overstated declared-field rule; `[]` presented as a harmless opt-out (guided is being removed) | — |
| S-14 | capabilities coalesce | P3 | `union_collision_policy` cannot be authored; a note only | E7 |
| T-01 | tool schemas (upsert_node, set_pipeline, terminal, patch_node_options), config_base `schema` Field, generation.py repairs | P1 | "schema declares what ARRIVES, never its transformed result" | — |
| T-02 | planner_authoring_aids.py:884-891, llm/base.py:199 | P1 | multi-query `row.source_row` called "the whole source row"; declaration not taught | — |
| T-03 | aids:846-851 | P2 (P1 at T1) | value_transform output `any` narrowed only by type_coerce | T1 |
| T-04 | output_mode description, catalogue | P2 | the planner cannot see which batch plugins run under passthrough | example-rank, merge 2 |
| T-05 | inspect_source, set_output, set_pipeline outputs | P2 | nothing says headers are raw and declarations must be normalized | codex-final clause |
| T-06 | get_expression_grammar, reference_join `output` | P2 | stored sets refused, typing of nested results, result typing after T1 | T1 (part) |
| T-07 | MCP composer server | P2 | MCP clients never receive the skill-only teaching | — |
| T-08 | aids fork/coalesce rules | P2 | missing the union one-type-per-field rule | T1 softens |
| T-09 | aids field_mapper and sink rules | P2 | normalized names not required | codex-final clause |
| T-10 | on_error descriptions | P3 | failed-batch routing | — |
| T-11 | aids sink hygiene | P3 | `_usage` must be `any` | — |
| R-01 | generation.py:1431-1444 | P1 | `batch_transform_misplaced` explanation describes the retired by-name rule | — |
| R-02 | llm/base.py:633-638 | P1 | row-API refusal offers `[]`, under which every row fails | G3 |
| R-03 | llm/base.py:860-866, rag/core.py:237-250, state.py:4043-4061 | P1 | `row.keys()` refused as an undeclared field "keys" | G3 |
| R-04 | generation.py:1816-1832 | P1 (conditional) | explain says "normalized form"; codex-final requires the `field_mapping` target | codex-final |
| R-05 | generation.py:935-956, :3294-3297 | P1 (conditional) | "untyped target is `any`"; "type_coerce type derived automatically" | T1 |
| R-06 | coalesce_schema_mode_mixed remedy | P2 | "…or all observed" leads to the union type refusal | — |
| R-07 | coalesce_union_type_incompatible explain | P2 | vaguer than the build message; "not one it computes itself" contradicts C2 | — |
| R-08 | edge_field_type_incompatible | P2 | no explain record | — |
| R-09 | generation.py:1074-1077 | P2 | common-traps list teaches `[]` as an opt-out | — |
| R-10 | llm/base.py:699-712 | P2 | "`[]` # Accept runtime risk"; "use extract_jinja2_fields()" | — |
| R-11 | rag/core.py:237-243 | P2 | offers `[]` for a static read | — |
| R-12 | llm/base.py:895-902, state.py:4243-4248, generation.py:1496-1502 | P2 | "use row.source_row.<column>" without the declaration | — |
| R-13 | generation.py:1427-1431 | P2 | offers passthrough without its constraint | — |
| R-14 | explain catalogue | P2 | runtime reasons the skill names have no explain entry | E7 (one reason) |
| R-15 | various | P3 | seven wording items (B-16 … B-23) | — |
| P-01 | config_base.py:758 `required_input_fields` | P1 | pre-ADR-051 description; points at a Python API | — |
| P-02 | rag/transform.py:213, ai_search.py:186, rag/core.py:88/91 | P1 | query_template hint and description predate ADR-051 (f) | — |
| P-03 | type_coerce.py:546 | P1 | "on_error … instead of crashing": a missing conversion field now aborts (exit 4) | — |
| P-04 | batch_rank.py:143, :178 (example-rank) | P1 (conditional) | "the only batch plugin passthrough accepts" | merge 2 |
| P-05 | batch_outlier_annotator.py:187, :225 | P2 | invites passthrough, which is now refused | — |
| P-06 | value_transform.py:626 | P2 (P1 at T1) | `any` hint omits the union refusal; wrong after T1 | T1 |
| P-07 | value_transform hints / `operations` | P2 | target spelling rule | — |
| P-08 | field_mapper.py:171 | P2 | mapping values are declarations; dotted source → `any` | — |
| P-09 | every column option | P2 | "use the normalized name" suffix missing | codex-final clause |
| P-10 | llm/transform.py:2040, :2043 | P2 | bound output types and `_usage` | — |
| P-11 | llm/base.py:223, llm/transform.py:2026 | P2 | template row API and spelling | G3 (method rule) |
| P-12 | prompt-shield hints | P2 | the protected LLM must list its fields | — |
| P-13 | config_base.py:560, :564 | P2 | `field_mapping` / `columns` descriptions misstate what is declared | codex-final |
| P-14 | catalogue payloads | P2 | created-output types never published | — |
| P-15 | merge-2 plugins | P2 | what their guidance must say | merge 2 |
| P-16 | batch reducer hints | P3 | "transform only" and failed-batch routing | — |
| P-17 | type_coerce.py:545, :552 | P3 | contradicts source text; "derived automatically" | T1 |
| P-18 | catalogue enums | P3 | enums shown as `[object]` (predates the lane) | — |

---

## How each surface reaches the planner (found from the code)

- **System prompt.**
  - `web/composer/prompts.py:47,50` loads `skills/pipeline_composer.md` and `skills/pipeline_capabilities.md` with `load_skill_with_hash`.
  - `render_system_prompt` (`prompts.py:72-95`) returns `render_with_pipeline_capabilities(_strip_advisor_disabled_fallback(composer.md))`. That function (`capability_skill.py:153-157`) prepends the exact bytes of the capabilities core.
  - An optional deployment overlay, `{data_dir}/skills/pipeline_composer.md`, is appended after `\n\n---\n\n`. The shipped `data/skills/pipeline_composer.md.example` covers providers, secrets and vocabulary only; the lane does not touch it. **Not reviewed:** any overlay actually served on a deployment or the dev box. It lives outside the repo and could repeat any of the stale rules below.
  - The freeform planner's system message is `service.py:2658` (`_composer_skill_text`) passed as `rendered_skill` (`service.py:4533, 5371, 8003`) and sent at `pipeline_planner.py:3900`. The advisor gets it at `service.py:8528`.
  - Guided mode: `guided/prompts.py:70-95` builds `base.md` + `step_N_*.md` with the same capability core prepended.
- **Planner reviewed-context message.** `planner_authoring_aids.py` builds the rule blocks and the discovery digest. It is imported at `prompts.py:24` and `pipeline_planner.py:92`.
- **Tool descriptions and schemas.** `web/composer/tools/__init__.py:get_tool_definitions()` (42 tools, dumped to `probes/D/tool_defs.json`). `emit_pipeline_proposal` (`pipeline_planner.py:1520`) embeds `canonical_planner_terminal_contract().schema`. Its node-property text is byte-identical to `set_pipeline`'s.
- **Expression grammar.** `tools/generation.py:264` `get_expression_grammar()`, static text.
- **Plugin catalogue.**
  - `list_transforms`, `list_sources` and `list_sinks` (`tools/transforms.py:157-189`) return `CatalogServiceImpl._to_summary` (`web/catalog/service.py:376-418`). Each entry carries the first docstring line, `config_fields` (including pydantic Field descriptions), usage text, `capability_tags`, `composer_hints` and related metadata.
  - `get_plugin_schema` (`generation.py:299-336`) returns the full docstring and `json_schema`.
  - `get_plugin_assistance` (`generation.py:2185-2275`) returns summary, fixes, examples and hints.
  - No payload carries `is_batch_aware`, `flush_emits_one_row_per_buffered_row` or created-output types. Measured with `probes/C/dump.py`: 0 hits at HEAD. Controls: llm shows 30 hints and value_transform shows the lane's new hints.
- **Refusal text.**
  - `explain_validation_error` is `generation.py:2066`. It looks the code up in `_VALIDATION_GUIDANCE_BY_CODE`, which is built from `_VALIDATION_ERROR_PATTERNS` (:449) and `_DIRECT_VALIDATION_GUIDANCE` (:1748); it then tries the regexes, then a fuzzy code match, then prints the list of closed codes.
  - Planner repair feedback (`pipeline_planner.py:2601` `_allowlisted_candidate_feedback`) **withholds raw messages**. The planner gets the code plus the explain record. Only `plugin_options_invalid` also carries the validator text (as `detail`). For every other code, the explain record is the planner's only remedy text.
  - Plugin config refusals reach the planner verbatim through `_prevalidate_transform` (`tools/_common.py:3290`).
- **MCP composer server** (`composer_mcp/server.py`).
  - `Server("elspeth-composer")` at :684 has no `instructions` and no system prompt.
  - `_build_tool_defs` (:209-221) serves `get_tool_definitions()` filtered by `_COMPOSER_TOOL_NAMES`, plus the session tools.
  - So an MCP client sees tools, grammar, catalogue and explain text, but **never either skill file**.
- **Tutorial.** `PlannerSurface.TUTORIAL_PROFILE` only restricts the tool palette. No tutorial-only teaching was found, so invariant 2 holds.

---

## Surface 1: skill files (`pipeline_capabilities.md`, `pipeline_composer.md`, guided skills)

### S-01 P1: "Use the literal observed header" contradicts the spelling rule in the next paragraph
- **Location:** `tree/src/elspeth/web/composer/skills/pipeline_composer.md:527-531` (§Source Facts).
- **Current text:** "If the source is bound, inspect the source and use the literal observed header such as `approved`".
- **Problem:** `inspect_source` returns raw `observed_headers` (`source_inspection.py:96,975`). Lines 533-552 (P1 spelling rule, M07, RULINGS 09-25) refuse a header spelling in any declaration. The example `approved` hides the conflict because it is already lowercase.
- **Recommended:** "…use the observed column, declared by its normalized name (header `Approved` is declared `approved`, see below)…"
- **Depends on unlanded work:** none.
- **Evidence:** `probes/A/h_literal_header` (header `Approved`, `fields: ['Approved: str']`) gives `vexit=1`: "Field name header spelling … 'Approved' is a header spelling of 'approved' … Declare 'approved'." The control `h_normalized` gives `vexit=0 rexit=0 success:2`.

### S-02 P1: Says templates read a field by its header spelling; config refuses it
- **Location:** `pipeline_composer.md:549-552`.
- **Current text:** "Row LOOKUPS keep either spelling: an expression or template reading `row['Price USD']`, a rename source, and an option that only locates a field … resolve the header too."
- **Problem:** for an LLM template under a declared list, a header-spelled read is refused.
  - Evidence, `probes/C/cfg/hdr/a` (csv `id,Name`, `prompt_template: {{ row['Name'] }}`, `required_input_fields: [name]`): `vexit=1`, "LLM prompt_template reads 'Name' under 'row', which options.required_input_fields does not declare — it declares 'name' … a spelling the declaration does not carry works at best by accident…".
  - Under `[]` the same template validates (`hdr/c`: "Pipeline configuration valid").
- **The runtime resolves the header spelling; only the config check refuses it.** Probe `probes/X/c.yaml` (a copy of `hdr/c`, under `[]`), run with `run --execute` against ChaosLLM: `rexit=0`, 1 row delivered. The stored request payload has user content `"alice"`, so `row['Name']` resolved to the value of `name`. Two rulings point toward the header read being accepted:
  - The spelling ruling says row lookups, template reads included, "keep resolving either spelling".
  - Q5 says the projection filters `name_index()` so "both spellings of a declared field resolve".

  So the disagreement is between the LLM config extractor (which compares the literal `Name` against the declared `name`) and the runtime and the rulings. **Lane owner to decide:** either make the extractor resolve the spelling through the declaration (a small change, and the teaching then stands), or keep the refusal and change the teaching.
- **Recommended, if the code stands:** "Expressions, rename sources and locate-only options resolve either spelling. A template reads a field by the normalized name its node declares (`row.price_usd`)."
- **Depends on unlanded work:** a lane-owner decision; the probe shows HEAD behaviour.

### S-03 P1: "`[]` shows the whole row" is false for `tojson`, `{{ row }}` and methods
- **Location:** `pipeline_capabilities.md:297-299`. The same text is at codex-final :297 and example-rank :296.
- **Current text:** "`[]` shows the whole row but proves nothing to a field-scoped prompt-injection control, so declare the fields."
- **Problem:** under `[]`, only iterating forms work (M13, M14, Codex C2). Probes, all with `required_input_fields: []`:

  | probe | template | result |
  |---|---|---|
  | `a_tojson_optout` | LLM `{{ row \| tojson }}` | `vexit=0 rexit=2`, failure:2, "Template rendering failed: TypeError" |
  | `r_tojson_optout` | RAG, same template | `rexit=2` |
  | `a_row_text_optout` | `{{ row }}` | delivers `"Score <elspeth.plugins.infrastructure.templates.TemplateRow object at 0x…>"` to the provider |
  | `a_keys_optout`, `a_todict_optout` | method calls | `rexit=2` |
  | `a_dictsort_optout`, `a_dictrow_tojson_optout`, `r_dictsort_optout`, `r_dictrow_tojson_optout` (controls) | iterating forms | all succeed |

- **Recommended now:** "`[]` makes the whole row available to whole-row filters (`row | dictsort`, `row | list`, `dict(row)`, `dict(row) | tojson`). It proves nothing to a field-scoped prompt-injection control, so declare the fields."
- **After G3 and C2:** add "`{{ row }}` and `row | tojson` render the fields it shows."
- **Depends on unlanded work:** false now; the final wording depends on G3 and C2.

### S-04 P1: `row.to_dict()` and the other methods do not "read fields of those names"
- **Location:** `pipeline_capabilities.md:302-304` (codex-final :303, example-rank :298).
- **Current text:** "`row` holds fields only: `row.to_dict()`, `row.contract`, `row.items()`, `row.keys()` and `row.values()` read fields of those names."
- **Problem (M13; template-api §5 item 5 names this line):**

  | probe | template | declaration | result |
  |---|---|---|---|
  | `a_todict_list` | `row.to_dict()` | list | `vexit=1`, "uses dynamic row field access (row-api via row API) … or set options.required_input_fields: [] to explicitly opt out" |
  | `a_mq_sr_todict_list` | `row.source_row.to_dict()` | list | same refusal |
  | `a_keys_list` | `row.keys()` | list | refused as an undeclared field `keys` |
  | `a_todict_optout`, `a_keys_optout`, `a_mq_sr_todict_optout`, `r_keys_optout` | these forms | `[]` | `rexit=2`, every row fails ("…TemplateRow object' has no attribute 'to_dict'") |

- **Recommended:** "`row` has fields and one method, `get` (`row.get('f', default)`). To enumerate fields, use filters: names with `row | list`, pairs with `row | items` or `row | dictsort`, a mapping with `dict(row)`. Read a column named like a method with item syntax (`row['keys']`). Never call a method on `row`."
- **Depends on unlanded work:** false now; after G3 these forms are also refused at config under every declaration.

### S-05 P1: The error-table row says a value_transform schema never declares its result
- **Location:** `pipeline_composer.md:1236` (the `gate_expression_type_mismatch_against_source_schema` row).
- **Current text:** "A `type_coerce` (or `value_transform`) node's `schema:` block declares what ARRIVES … never the transformed result…"
- **Problem:** under ruling C2 (M03), a type on a new value_transform target is its output declaration.
  - `probes/A/v_typed_new_target`: builds and delivers.
  - `v_untyped_new_target`: refused at a typed consumer ("expected float, got typing.Any | None").
  - This is the same false claim as T-01.
- **Recommended:** "On a `value_transform`, a type declared for a NEW target is that target's output type: it is checked on every row, routed on mismatch, and lets a typed consumer build. A type declared for a field the node reads and rewrites is also its input contract, so declare what arrives there."
- **After T1:** add arbiter D8: "a target's type is derived where its inputs are declared".
- **Depends on unlanded work:** none.

### S-06 P1 (conditional): The passthrough line names a plugin count that goes false at each merge
- **Location:**
  - HEAD `pipeline_capabilities.md:136-139`: "no discovered batch plugin does … so every one is refused under `passthrough`."
  - example-rank `:136-141`: "`batch_rank` is the one discovered batch plugin that does…"
- **Problem:**
  - The HEAD text is false once X1 merges (M18).
  - The example-rank text is false at merge 2, which adds `batch_normalize` and `batch_duplicate_flag` (M19).
  - It names a plugin in the static core, which says of itself (:383-385) that it holds no deployment plugin inventory.
  - RULINGS 09-27 A made admission a declaration, not a name.
- **Recommended:** "`passthrough` keeps the buffered rows as the same tokens. It carries only a batch plugin whose flush emits exactly one row per buffered row (a per-row annotator such as a within-batch rank; see the plugin's catalogue entry). Every other batch plugin reduces, replicates or skips rows and is refused ('Use output_mode: transform')."
- **Needs T-04** so the planner can see the capability.
- **Depends on unlanded work:** example-rank and merge 2.

### S-07 P1 (conditional on T1): "An expression result … is `any`"
- **Location:** `pipeline_capabilities.md:318-325`.
- **Current text:** "a value computed without a declared type (an expression result, an extracted nested value) is `any` … validation refuses the coalesce".
- **Problem:** true at HEAD (G2). After T1 (arbiter §4 D2/D7 and A.7), co2 (`price = row['price'] + 1` over `price: int`) derives `int`, builds and delivers. Keeping the line would teach the planner to restate types the system already derives.
- **Recommended at T1 (arbiter A.7 wording):** "…an expression result whose type cannot be derived from the declared input types, or an extracted nested value, is `any`…". Keep the remedy.
- **Depends on unlanded work:** T1.

### S-08 P2: Nothing says what a whole-row template sees when the declaration is omitted
- **Location:** `pipeline_capabilities.md:299-302`.
- **Problem:** a single-query `{{ row | dictsort }}` with no declaration validates and sends an empty row.
  - `probes/A/a_dictsort_none`: `vexit=0 rexit=0`, payload `"Score []"`.
  - G3 item F refuses this. Control: `a_field_none` (`{{ row.note }}`, no declaration) is refused.
- **Recommended:** "A template that shows the row as a whole needs a declaration: list the fields it shows, or `[]` for the whole row. With none, its row is empty (a retrieval template's row holds only its query field)."
- **Depends on unlanded work:** G3 for the refusal; the silent empty render happens now.

### S-09 P2: An omitted declaration is as unprovable as `[]` for a prompt shield
- **Location:** `pipeline_capabilities.md:297-302`; `pipeline_composer.md:973-988` (§Untrusted content).
- **Problem:** `web/plugin_policy/coverage.py:225-240` treats an omitted declaration like `[]`: the result is `input_fields_unprovable` unless the control scans `fields: all` (M12, RULINGS Q5). Only `[]` is taught.
- **Recommended, in §Untrusted content:** "A field-scoped prompt-injection control proves coverage only from the model node's `required_input_fields` (plus each query's `input_fields`). With `[]` or none, the node is `input_fields_unprovable` unless the control scans every field."
- **See also:** P-12, which covers the plugin side.

### S-10 P2: HEAD lacks the `field_mapping` / headerless-CSV spelling teaching
- **Location:** HEAD `pipeline_composer.md:533-537`. codex-final carries the fix at :538-542 and in table row :1245.
- **Recommended:** land the codex-final text as written, together with R-04, P-13 and the field_mapping clauses of T-05, T-09 and P-09.
- **Depends on unlanded work:** codex-final.

### S-11 P2: The one-shot error table is missing rows
- **Location:** `pipeline_composer.md` one-shot table (around :1200-1250).
- **Problem:**
  - No row for `edge_field_type_incompatible`, which 8ee3510bc widened to optional fields (M31, listed in the CHANGELOG as newly refused).
  - No row for the runtime reasons the skill itself names or that the planner will see in run diagnostics: `type_mismatch`, `contract_type_conflict`, `declared_field_is_header_spelling`, `target_is_header_spelling`, "Undeclared field", and `union_field_collision` (E7).
  - The existing `field_name_header_spelling` row (:1241) covers only the build-time case with `missing_fields`.
- **Recommended:** one row each, pairing with R-08 and R-14. For the header-spelling row add: "at run time, the reason `target_is_header_spelling` / `declared_field_is_header_spelling` names the canonical spelling".
- **Depends on unlanded work:** E7, for `union_field_collision` only.

### S-12 P3: Failed-batch routing is not taught
- **Location:** `pipeline_capabilities.md:96-98, 130-139`.
- **Problem:** M20 is not taught.
- **Recommended:** "If the plugin fails a batch, every row of that batch goes to the aggregation's `on_error`; `discard` records them as quarantined."
- Follower retry (M22) never reaches the planner, so it needs no teaching.

### S-13 P3: Guided `step_3_transforms.md:117-122`
- **Problem:**
  - It says "every declared field must be interpolated". That is false: a declared field the template never reads is admitted (`a_declared_unread`, `vexit=0`); a declaration is refused only when the template reads no row at all (`a_declared_norow`).
  - It presents `[]` as a deliberate opt-out without its costs (S-03, S-09).
  - It has no spelling-rule or ADR-051 teaching.
- Guided mode is being removed (John, 09-22). Record this only; do not invest in it.

### S-14 P3 (note): `union_collision_policy` cannot be authored in the composer
- It is in `_UNSUPPORTED_COALESCE_FIELDS` (`yaml_importer.py:35-40`) and absent from the node inventory. The default is `last_wins` (`config.py:1049`).
- E7's build refusal therefore never reaches the planner. Only the runtime reason needs teaching (S-11, R-14).
- If `union_collision_policy` is ever made authorable, the field inventory gate G-02 fires.

---

## Surface 2: tool descriptions and schemas, grammar, planner aids, MCP server

### T-01 P1: "The schema: block declares what ARRIVES at the node, never its transformed result"
- **Locations:**
  - `tools/transforms.py:278-281`: the upsert_node `options` description. It is byte-identical in `set_pipeline` and in the terminal `emit_pipeline_proposal` schema, which MCP also serves.
  - `tools/transforms.py:1860`: patch_node_options.
  - The `suggested_repair` strings at `tools/generation.py:3294-3297, 3426-3427`.
  - `config_base.py:745`: the generic `schema` Field, "INPUT contract", shown on every transform. `llm/transform.py:2041` repeats it.
- **Problem:** for a field the node creates, the `schema.fields` type is the output declaration (ADR-050 D1, M01; C2, M03; LLM binding, M05).
  - It contradicts `pipeline_capabilities.md` §Declared Types, the G2 remedy ("declare … on every branch's last node") and value_transform's own runtime reason.
  - Evidence, `probes/D/vt1`: `schema: {mode: flexible, fields: ['label: int']}` with `target: label, expression: "'x'"` gives `vexit=0 rexit=2`, both rows routed with `{"reason":"type_mismatch","expected":"int","actual":"str","message":"Operation target 'label' computed a value of type str, but this node's schema declares it int…"}`.
- **Recommended, as one shared text:** "A node's `schema:` declares two things. For a field that arrives, it declares the arriving type: convert on the SOURCE schema or in an upstream `type_coerce`, never here. For a field the node creates (a value_transform target, an LLM output field, a created name), it declares the output type, which is checked on every row; a value of another type goes to `on_error`. An `int` satisfies `float`."
- **Depends on unlanded work:** none.

### T-02 P1: Multi-query `row.source_row` is called "the whole source row"
- **Locations:**
  - `planner_authoring_aids.py:884-891`: "(the whole source row is {{ row.source_row.<column> }}…)".
  - `llm/base.py:199`: the `queries` Field, "Multi-query specs (None = single-query mode)".
- **Problem:** under ADR-051 (M10, RULINGS Q5), `row.source_row` holds only the node's `required_input_fields`, which must cover every query's `input_fields`.
  - `probes/D/mq1` (`{{ row.source_row.meta }}`, `[note]`): `exit=1`, "Query 'q1' reads row column 'meta' … which options.required_input_fields does not cover — it declares 'note'."
  - `mq2` (declaration removed): refused.
- **Recommended:** "`{{ row.source_row.<column> }}` reads a column of the source row, which holds only the columns the node lists in `options.required_input_fields`. Declare there, by normalized name, every column any query reads, through its `input_fields` values or through `row.source_row`." Use the same text in the `queries` Field description.
- **Depends on unlanded work:** none.

### T-03 P2 (P1 when T1 lands): The aid says a value_transform output is `any` and only type_coerce narrows it
- **Location:** `planner_authoring_aids.py:846-851`.
- **Current text:** "A producer's any-typed field (json_explode, blob_json_expand, value_transform outputs) is narrowed by inserting a type_coerce…"
- **Problem:**
  - Already true at HEAD: a target can be typed in the node's own schema (C2; T-01 evidence), and the aid never says so.
  - After T1, a target carries its derived type (arbiter D2/D8).
- **Recommended:** "Type a value_transform target in the node's own `schema.fields`; it is checked on every row. Otherwise its type is [T1: the type its expression computes over declared inputs, else] `any`. A json_explode or blob_json_expand element is `any`: narrow it with `type_coerce`, never by widening consumers to `any`."
- **Depends on unlanded work:** T1 for the bracketed clause.

### T-04 P2: The planner cannot see which batch plugins run under passthrough
- **Locations:**
  - `tools/transforms.py:340`: upsert_node `output_mode`, "Aggregation output mode (aggregation only). Defaults to 'transform' if omitted."
  - set_pipeline and the terminal schema give `output_mode` no description.
  - The catalogue and discovery digest expose only `capability_tags`. On example-rank, `batch_outlier_annotator` (refused) and `batch_rank` (admitted) share the tag `annotation`.
  - The refusal (`state.py:1799-1808`) ends "…declares flush_emits_one_row_per_buffered_row = True on its class", an attribute the planner can neither see nor set.
- **Problem:** ruling 09-27 A said the declaration is read by both surfaces. The planner can learn it only from prose that names plugins, which the ruling retired, and any count in that prose goes stale at each merge.
- **Recommended:**
  - (a) Describe `output_mode`: "`passthrough` keeps the same rows and admits only a batch plugin whose flush emits exactly one row per buffered row; every other batch plugin is refused, so use `transform`."
  - (b) Have the catalogue derive a published fact from the class attribute, for example `aggregation_output_modes: [transform, passthrough]` or a derived tag, and surface it in `list_transforms`, `get_plugin_schema` and the digest. Point the skill (S-06) and the refusal ("plugins that support it list passthrough in list_transforms") at that fact.
  - This publishes a fact and authors nothing, so the composer invariants hold.
- **Depends on unlanded work:** (b) is a small code change; its value grows with example-rank and merge 2. Watch the digest budget (G-08).

### T-05 P2: Tools do not say headers are raw and declarations are normalized
- **Locations:**
  - `tools/sources.py:1850` inspect_source: "observed headers and inferred types tell you which fields the source actually contains".
  - `tools/outputs.py:90` set_output `options`.
  - `tools/sessions.py:2225` set_pipeline `outputs`.
- **Problem:** M07 applies here, plus M08 on codex-final. For an MCP client these descriptions are the only rule text (T-07).
- **Recommended:**
  - inspect_source: "…`observed_headers` are RAW headers. Every declaration names the normalized field (`First Name` → `first_name`), or the source `field_mapping` target that renames it."
  - Sinks: "Sink `schema.fields` names and custom header keys are declarations: use the normalized names rows carry."
  - Optionally, have inspect_source return each header's normalized name as a fact.
- **Depends on unlanded work:** codex-final, for the `field_mapping` clause.

### T-06 P2: The grammar does not say a stored set is refused, or how result types are decided
- **Locations:** `tools/generation.py:234, 246`; `reference_join.py:104` (`output` Field).
- **Current text:** "{..} sets … for membership tests" and "Types are guaranteed by the source schema — no coercion is needed in expressions."
- **Problem:**
  - A value_transform operation or reference_join output that can produce a set is refused at build (M26: 07f6dccd5, 99daf656c). value_transform got a hint; reference_join and the grammar did not.
  - A nested subscript result is `any` (M27).
  - After T1, the grammar determines each target's type.
  - "Guaranteed by the source schema" is incomplete under ADR-050: upstream declarations guarantee types too.
- **Recommended:**
  - "A value that value_transform or reference_join stores must not be able to be a set (no canonical order); build a list. A set used in place (`x in {…}`) is fine."
  - "Types come from upstream declarations (source schema, `type_coerce`, a node's declared outputs); expressions do not coerce."
  - After T1: "A value_transform target's recorded type is the type the expression provably computes over declared inputs, else `any` (for example a nested subscript)."
  - Append the set sentence to reference_join's `output` description.
- **Depends on unlanded work:** T1, for the last grammar sentence.
- **Gate:** G-05.

### T-07 P2: MCP composer clients never see the skill-only teaching
- **Location:** `composer_mcp/server.py:684` (no `instructions`) and :209-221.
- **Problem:** several rules exist only in the skill files, so an MCP client planner meets them only as refusals: int satisfies float, LLM output binding, `_usage` must be `any`, the ADR-051 None/`[]`/list semantics, passthrough admission, the union one-type rule, and stored sets.
- **Recommended:** put a one-line form of each rule on surfaces both planners read:
  - llm, rag and ai_search `composer_hints` (P-10, P-11);
  - value_transform hints (P-06, P-07);
  - the `output_mode` description (T-04a);
  - the coalesce `merge` description: "a field two union branches both carry must have one type on every branch; `any` counts as a type there".

  Alternatively, give the MCP server `instructions` rendered from the same `pipeline_capabilities.md` bytes, with one owner. Either way this is teaching only, with no server-side authoring.

### T-08 P2: The fork/coalesce aid lacks the union type rule
- **Location:** `planner_authoring_aids.py:416-477` (`_FORK_COALESCE_RULES`).
- **Problem:** G2 (M23) refuses a union where two branches carry the same field with different types (the co2/co6 shape from F2 r3 F-1). The aid covers only fields each branch creates for itself.
- **Recommended:** "A field every branch carries (a forwarded parent field such as the id) must keep one type on every branch. Write a branch's rewritten value under a new name, or declare the field's type on each branch's last node." Keep the exemplar colour-free.
- **Depends on unlanded work:** after T1, derivable rewrites agree automatically (arbiter D7), so soften the rewrite clause then.

### T-09 P2: The field_mapper and sink aids do not ask for normalized names
- **Location:** `planner_authoring_aids.py:457-458, 902-908`.
- **Current text:** "declare the arriving names there…"; "declare them in sink schema.fields with their types".
- **Problem:** a field_mapper schema that declares its rename source by header (`{Name: given}` with `Name: int?`) is refused (M07; Codex C1, M08).
- **Recommended:** "…declare the arriving names in their normalized form (`first_name` for header `First Name`); mapping keys may use either spelling." For sinks, add "by normalized name".
- This rule must stay in the aids: `field_mapper` is a forbidden word in the static skill (G-03).

### T-10 P3: The on_error descriptions omit failed-batch routing
- **Location:** `tools/transforms.py:270`; `tools/sessions.py:2072`.
- **Recommended:** append "For an aggregation, a failed batch sends every buffered row to this policy; 'discard' records them as quarantined." (M20)

### T-11 P3: The sink-hygiene aid omits the `_usage` type rule
- **Location:** `planner_authoring_aids.py:892-894`.
- **Recommended:** append "…if you declare `<response_field>_usage`, its type is `any`." (M05)

---

## Surface 3: refusal and remedy text (explain catalogue, composer messages, plugin config refusals)

### R-01 P1: The `batch_transform_misplaced` explanation describes the retired by-name rule
- **Location:** `tools/generation.py:1431-1444`.
- **Current text:** "…and batch_replicate as an aggregation without output_mode: transform." Fix: "batch_replicate: set output_mode: 'transform'."
- **Problem:** since P5 (b3ebbf738, decision A, M17), every batch plugin without the 1:1 declaration is refused under passthrough: all 13 at HEAD. `state.py:1757-1810` / `:8005` emit this code with a correct message, but the planner channel withholds that message (`pipeline_planner.py:2612`). So a planner that puts `batch_stats` under passthrough is told only about batch_replicate. Evidence: `probes/B/v2.log` (pt_stats).
- **Recommended:**
  - Third placement: "a batch plugin under `output_mode: passthrough` whose flush does not emit exactly one row per buffered row (a reducer, `batch_replicate`, or `batch_outlier_annotator`, which skips rows)".
  - Fix: "set `output_mode: 'transform'` (the default), or choose a batch plugin whose catalogue entry lists passthrough".
  - Change only the explanation and fix text; the regex is frozen (G-04).
  - Re-cut `test_validation_guidance_catalogue.py:274` (it asserts `"batch_replicate" in explanation`) on batch_stats.
- **Depends on unlanded work:** none.

### R-02 P1: The row-API refusal offers `[]`, under which every row fails
- **Location:** `plugins/transforms/llm/base.py:633-638`.
- **Current text:** "LLM prompt_template uses dynamic row field access (row-api via row API)… or set options.required_input_fields: [] to explicitly opt out and accept runtime risk."
- **Problem:** `[]` is honest for computed keys, but not for `row.contract`, `row.to_dict()`, `row.source_row.to_dict()` or `.contract`.
  - Under `[]` these fail every row (`a_todict_optout`, `a_mq_sr_todict_optout`, F2 r3 l8: `rexit=2`).
  - Or they silently read a column named `contract` (panel2 C_contract_attr_E).
  - Under a field-scoped prompt shield, `[]` is also `input_fields_unprovable` (M12, `plugin_policy/validation.py:237-242, 326-334`).
  - G3 §4C requires this message never to suggest `[]`.
- **Recommended:** split the message by kind.
  - Row-API: "a template row has fields and one method, `get`; use `dict(row)`, `row | items` or `row | list`; read a column named `contract` as `row['contract']`." No `[]`.
  - Computed key: keep the `[]` remedy, and add "a field-scoped prompt shield cannot credit `[]`".
- **Depends on unlanded work:** G3, which owns this message. It is wrong today.

### R-03 P1: `row.keys()` is refused as an undeclared field named `keys`
- **Locations:**
  - `llm/base.py:860-866`, with the remedy text at :57-65;
  - `rag/core.py:237-250`;
  - `web/composer/state.py:4043-4061, 1982-1991`;
  - the explain record for `prompt_template_undeclared_row_fields`.
- **Current text:** "reads 'keys' under 'row', which options.required_input_fields does not declare… Add a name ONLY if the upstream producer guarantees that exact name". The RAG refusal with no declaration adds "…or set options.required_input_fields: [] to opt out".
- **Problem:** declaring `keys`, or setting `[]`, gives a config that validates while every row fails (`a_keys_optout`, `r_keys_optout`, F2 r3 F-3: `rexit=2`). G3 requires the row-API message to take precedence so the planner is never told "declare `keys`".
- **Recommended:** G3's text on all three surfaces (single-query, `row.source_row`, RAG): "`row.keys()` calls the value of field `keys`: a template row has fields and one method, `get`. For names use `row | list`, for pairs `row | items` or `row | dictsort`, for a mapping `dict(row)`; read a column named `keys` as `row['keys']`."
- **Depends on unlanded work:** G3.

### R-04 P1 (conditional on codex-final): The spelling explain record says "normalized form"
- **Location:** `generation.py:1816-1832`, unchanged on codex-final.
- **Current text:** "Rewrite each name in 'missing_fields' as its normalized form".
- **Problem:** codex-final changed the refusal ("the source's field_mapping renames 'name' to 'b'. Declare 'b'", `field_spelling.py:473-476`) and the skill table, but not this record. Under `field_mapping: {name: b}`, the normalized `name` is itself refused (M08, Codex C1). Because raw messages are withheld, this record is what the planner reads.
- **Recommended:** "Declare the name the refusal gives: the normalized header or, when the source's `field_mapping` renames it, the mapping target (`{name: b}` → declare `b`; `Name`, `NAME` and `name` are all refused). A headerless CSV renames each column as written."
- **Depends on unlanded work:** fold into the codex-final cherry-pick.

### R-05 P1 (conditional on T1): Explain text that goes false when T1 lands
- **Locations:**
  - `generation.py:935-956`: "a value_transform writing a target adds it as 'any' when undeclared".
  - `generation.py:3294-3297`: "the coerced type belongs to the node's output contract and is derived automatically from its conversions".
- **Problem:**
  - The first is true now (`probes/B/v1.log` co2: "branch 'path_a' carries 'any' (declared by transform 'vt_a' (value_transform))") and false after T1 (M04).
  - The second is false at build time in observed mode now: type_coerce publishes an empty stamp table (arbiter §5). It becomes true only when T1 makes type_coerce publish.
- **Recommended at T1:** "an untyped target carries the type its expression computes from declared inputs; `any` only where that cannot be derived". Keep "not a wildcard".
- **Recommended now, for :3294:** "…declare the conversion's target type on the consumer's upstream path, or type the target" (T-01 wording).
- **Depends on unlanded work:** T1.

### R-06 P2: The mixed-schema remedy leads to the union type refusal
- **Location:** the `coalesce_schema_mode_mixed` explain record and builder text.
- **Current text:** "…or all branches produce observed schemas".
- **Problem:**
  - Declaring on one branch only is refused as mixed (`probes/B/v4.log` co5).
  - Going all-observed is refused by G2 in every mode (`v1.log` co2).
  - Only declaring on every branch builds (`v4.log` co8).
- **Recommended:** "All-observed does not avoid a certain type conflict. Declare the field on every branch's last node (`mode: flexible`)."

### R-07 P2: The `coalesce_union_type_incompatible` explain text is vaguer than the build message
- **Location:** `generation.py:946-956`, compared with `union_merge.py:276`.
- **Problem:**
  - It omits "every branch".
  - Its "but not one the producer computes itself" contradicts C2 (co8 builds).
  - "Declare on every branch's last node" fails when that node is a value_transform rewriting the field from another type, because the declaration is then also its input contract (`probes/A/v_typed_rewrite_str_to_int`).
- **Recommended:** "Declare `<field>: <type>` on EVERY branch's last node (`mode: flexible`). For a value_transform, type the computed target; if it rewrites a field that arrives with another type, write the result under a new name instead. For type_coerce, retype `to`. For a field_mapper rename, declare the SOURCE field."
- **Related, P3:** `state.py:5409-5413` emits a second text with no remedy; route it through `union_type_conflict_message`.

### R-08 P2: `edge_field_type_incompatible` has no explain record
- **Location:** emitted at `error_codes.py:84` and `state.py:6680-6685`; absent from `_VALIDATION_GUIDANCE_BY_CODE`.
- **Problem:** the planner receives the bare code. The lane widened when it fires (8ee3510bc optional fields, M02, M05; M31). Measured: 41 of 175 emitted codes have no record (`set(_EMITTED…) - set(_CLOSED…)`). This is the only one the lane is responsible for; `transform_string_input_field_type_incompatible` predates the lane.
- **Recommended:** add a `DirectValidationGuidance` (new regexes are frozen, G-04):
  - Explanation: "A consumer declares a type (required or optional) its producer's declared type can never satisfy. An `int` satisfies `float`; nothing else widens; an LLM `number` is `float`."
  - Fix: "Match the producer's type, or convert upstream on a field every row carries. Never declare the post-conversion type on the consumer, and never widen to `any`."
- **Gates:** G-06, G-07.

### R-09 P2: The common-traps list for `plugin_options_invalid` teaches `[]` as an opt-out
- **Location:** `generation.py:1074-1077`: "(or [] to opt out)".
- **Problem:** it contradicts the same catalogue's "Do not answer this by emptying required_input_fields" and is refused under M12. The list also lacks the M15 template-literal refusals and the M07 spelling refusals.
- **Recommended:** "Declare exactly the fields the template reads. `[]` shows the whole row, is needed only for a computed key, and a field-scoped shield cannot credit it." Add one line each on template literals and on normalized names.

### R-10 P2: The LLM "required_input_fields not declared" refusal
- **Location:** `llm/base.py:699-712`. It reaches the planner verbatim as `plugin_options_invalid` detail.
- **Current text:** "options.required_input_fields: []   # Accept runtime risk (opt-out)" and "Use extract_jinja2_fields() from elspeth.core.templates".
- **Problem:** the planner cannot call Python. "Runtime risk" misdescribes `[]` under ADR-051, and `[]` fails M12.
- **Recommended:** drop the function sentence. Offer `[]` only "when the template must show the whole row or uses a computed key; a field-scoped prompt shield refuses it". G3 item F adds the refusal for a whole-row read with no declaration.

### R-11 P2: The RAG no-declaration refusal offers `[]` for a static read
- **Location:** `rag/core.py:237-243`.
- **Problem:** the fix is to declare the field. Under `[]`, a `tojson` template (C2) or a row-API read (G3) fails every row. The computed-key message at :227-231 may keep `[]`. Evidence: `probes/B/v3.log` (R_f01_keys_N).
- **Recommended:** "declare the field in required_input_fields"; no `[]` in this branch.

### R-12 P2: "Use `row.source_row.<column>` for direct row access" leaves out the declaration
- **Locations:** `llm/base.py:895-902`; `state.py:4243-4248`; `generation.py:1496-1502`.
- **Problem:** following it verbatim is refused as `query_input_columns_undeclared` (`probes/B/v5.log`).
- **Recommended:** "…for a column the node declares in `required_input_fields`".

### R-13 P2: The `aggregation_output_mode_invalid` fix offers passthrough without its constraint
- **Location:** `generation.py:1427-1431`: "Set output_mode to 'passthrough' or 'transform'".
- **Recommended:** "'transform' (the default). 'passthrough' only for a batch plugin that emits one row per buffered row; anything else is refused (`batch_transform_misplaced`)."

### R-14 P2: Runtime reasons named in the teaching have no explain entry
- **Problem:** `declared_field_is_header_spelling`, `target_is_header_spelling`, `type_mismatch`, `contract_type_conflict`, "Undeclared field" and, once E7 lands, `union_field_collision`: 0 hits in `src/elspeth/web` guidance, and explain answers "does not match any known validation message".
- **Recommended:** add a direct record for each, pairing with S-11.

### R-15 P3: Wording items
- **B-16** `query_input_columns_undeclared`: "every row that arrives without it fails". Under projection, it is every row. Verify before rewording.
- **B-17** `llm/base.py:57-65`, `state.py:1982-1991`: "works at best by accident of the producer's original header". Resolving either spelling is now a rule for lookups; only declarations must be normalized. This interacts with S-02.
- **B-18**: two texts for `coalesce_union_type_incompatible` (see R-07).
- **B-19** `schema_validation.py:596-603`, option 2: add "on a field every arriving row carries" (M09).
- **B-21**: the runtime reason "'…TemplateRow object' has no attribute 'to_dict'" names an internal class. G3 §4D replaces it.
- **B-22** `generation.py:3736`: "See pipeline_composer.md rule 10". There is no rule 10; this predates the lane.
- **B-23** `generation.py:2956-2963` `_NUMERIC_VALUE_FIELD_AGGREGATION_PLUGINS`: a list of names (example-rank adds `batch_rank`). Merge 2 must add `batch_normalize` and `batch_correlation`, or read a declared capability instead.

---

## Surface 4: plugin assistance, option descriptions, catalogue

### P-01 P1: The `required_input_fields` description predates ADR-051
- **Location:** `plugins/infrastructure/config_base.py:758`. It is the base Field on 24 transforms and reaches the planner through `list_transforms.config_fields` and `get_plugin_schema`.
- **Current text:** "Fields this transform requires in input. Used for DAG validation … For templates, use elspeth.core.templates.extract_jinja2_fields() to discover fields."
- **Problem:** for template nodes, the declaration now decides what the template sees (M10, M12):
  - omitted = an empty row;
  - `[]` = the whole row, unprovable to a shield;
  - a list = exactly those fields.

  The text also points at an uncallable Python API. Probes: `probes/C/cfg/p1_omit_rif` and `p2_undecl` are refused; `p3_empty` validates.
- **Recommended:** override the description on the LLM and retrieval configs: "The row fields this node's template may read; the template sees exactly these. List them by normalized name. Omitted: the row holds no field (a retrieval template still gets `query`). `[]`: the whole row, which a field-scoped prompt-injection control cannot prove, so prefer a list." Remove the `extract_jinja2_fields()` sentence everywhere.
- **Gate:** G-16 (knob goldens).

### P-02 P1: The RAG `query_template` hint and description predate ADR-051 (f)
- **Locations:** hint at `rag/transform.py:213` and `azure/ai_search.py:186`; Fields at `rag/core.py:91` (`query_template`) and `:88` (`query_field`).
- **Current text:** "Query template uses row-field interpolation; document what fields are read so downstream consumers can wire them." / "Optional template used to build the retrieval query from row fields."
- **Problem:** since ADR-051 (f):
  - the template sees `query` plus only the declared `row` fields;
  - a read with no declaration is refused (`probes/C/cfg/rag/r1_undecl`);
  - a declaration the template never reads is refused (`r3_unread`).
- **Recommended:** "A `query_template` sees `query` (the `query_field` value) and only the `row` fields listed in `required_input_fields`. Declare exactly the fields it reads, and none if it reads only `query`."

### P-03 P1: type_coerce says on_error prevents a crash; a missing conversion field aborts the run
- **Location:** `plugins/transforms/type_coerce.py:546`.
- **Current text:** "Set on_error to a quarantine sink to capture un-coercible rows for audit, instead of crashing the run."
- **Problem (M09, ruling Q4 (b), which owes this teaching line):**
  - `probes/C/cfg/tc` (observed jsonl `{"a":"1","b":"2"}` then `{"a":"3"}`, `conversions: [{field: b, to: int}]`, on_error set): `vexit=0`. At run time: "DeclaredRequiredInputFieldsViolation: Transform 'type_coerce' … declared required input fields ['b'] but row … only exposed ['a']; missing ['b']", exit 4. The quarantine sink received nothing.
  - Behind a fixed schema, the same config is refused at build.
- **Recommended:** add "Each conversion field is a declared input: every row reaching the node must carry it, under its normalized name. A row that lacks it stops the run (on_error does not catch it). Route or fill sparse fields upstream first."

### P-04 P1 (conditional): batch_rank calls itself "the only" passthrough plugin
- **Location:** `tree-example-rank/src/elspeth/plugins/transforms/batch_rank.py:143, 178`.
- **Problem:** false at merge 2 (M19), and a by-name claim where the rule is a declaration.
- **Recommended:** "It emits exactly one row per buffered row, in order, so it runs under `output_mode: passthrough` as well as `transform`."
- **Depends on unlanded work:** example-rank and merge 2. On the example-rank tip alone this is P3.

### P-05 P2: batch_outlier_annotator invites passthrough, which is now refused
- **Location:** `batch_outlier_annotator.py:187, 225`: "preserving each valid source row with added outlier fields".
- **Problem:** this plugin under passthrough delivered on release and is now refused (M17; `probes/C/cfg/pt/batch_outlier_annotator.yaml`, `vexit=1`).
- **Recommended:** "Run it with `output_mode: transform`: a null or non-finite value drops its row, so it is refused under passthrough. To keep every row with a batch-relative score, use a batch plugin that supports passthrough." Name batch_rank only once example-rank lands.

### P-06 P2 (P1 when T1 lands): value_transform's `any` hint
- **Location:** `value_transform.py:626`: "…its output type is 'any' (never inferred from a row). Before a typed consumer, use type_coerce…"
- **Recommended now:** append "`any` is a type of its own at a union coalesce: a branch carrying the field typed differently is refused. Type the target, or write it under a new name." (M23)
- **Recommended at T1:** replace the hint with arbiter D8's line: "a target's type is derived from its expression where its inputs are declared; type it, or add type_coerce, only where it is not (an expression over an observed or nested input stays `any`)."

### P-07 P2: value_transform does not teach how to spell a target
- **Location:** `value_transform.py:623-630` and the `operations` Field.
- **Problem:** a header-spelled target routes `target_is_header_spelling`, or is refused behind a declared upstream. Two targets that spell each other are refused (M07).
- **Recommended:** "A target is a declaration: name it by the normalized field name. Use `name` to overwrite a field the row carries as `name`, a new name for a new field, and never two spellings of one name."

### P-08 P2: field_mapper's `mapping` description
- **Location:** `field_mapper.py:171`: "Mapping from existing input field names to output field names."
- **Recommended:** "Keys name fields to read (either spelling resolves). Values are new field names and must be normalized: `{Name: name}` keeps a header, never `{name: Name}`. A dotted source extracts a nested value typed `any`, which a union coalesce beside a typed branch refuses." (M07, M23)

### P-09 P2: Column options never say "use the normalized name"
- **Locations:**
  - batch plugins: `value_field` (e.g. `batch_stats.py:38`), `group_by`, `variant_field`, `score_field`, `pair_field`, `cohort_field`, `actual_field`, `predicted_field`, `inspect_fields`, `text_field`;
  - `keyword_filter.py:156` and the guardrails' `fields`;
  - `web_scrape.py:175 url_field`; the blob_json_expand options;
  - `rag/core.py:88 query_field`; reference_join `key_field`; type_coerce `conversions[].field`.
- **Problem:** each is a declaration under M07.
- **Recommended:** one shared suffix constant, not a per-name lookup: "Use the field's normalized name (a source `field_mapping` target where one renames it), not the raw header."
- **Depends on unlanded work:** codex-final, for the `field_mapping` clause.
- **Gate:** G-16.

### P-10 P2: The LLM output-type teaching is incomplete
- **Location:** `llm/transform.py:2040, 2043`.
- **Problem (M05):** `_usage` typed as anything but `any` is refused or fails every row. `int` over a `number` output is refused, and a downstream `int` over `number` routes every row.
- **Recommended:**
  - "Declare a field the model writes with its bound type (`integer`→`int`, `number`→`float`, `boolean`→`bool`, `string`/`enum`→`str`); `int` over `number` is refused, and downstream nodes must match."
  - "If you declare `<response_field>_usage`, type it `any`."
- **Gate:** G-17 (policy_view golden `transform__llm.json`).

### P-11 P2: The LLM template row API and spelling
- **Location:** `llm/base.py:223`; `llm/transform.py:2026`.
- **Problem:** after G3 (M13), `row` has fields and one method, `get`. The spelling half is true now (`probes/C/cfg/hdr/a`).
- **Recommended:** "…with `{{ row.<field> }}`, using the normalized names you list in `required_input_fields`. `row` has fields and one method, `get`; for all declared fields use `row | items` or `dict(row)`."
- **Depends on unlanded work:** G3, for the method sentence.

### P-12 P2: Prompt-shield hints
- **Location:** prompt-shield hints; `llm/transform.py:2046`.
- **Problem:** M12, `coverage.py:87, 465`.
- **Recommended:** "The LLM this shield protects must list its fields in `required_input_fields`; `[]` or none cannot be proved covered." See S-09.

### P-13 P2: The source `field_mapping` / `columns` descriptions
- **Location:** `config_base.py:560, 564`.
- **Current text:** "Explicit normalized column names for headerless tabular input." / "Optional mapping from observed source field names to normalized pipeline field names."
- **Problem:** M08. Keys are normalized names (for headerless CSV, the `columns` names as written); the values are what downstream declares; `columns` need not be normalized. `csv_source.py:680` states the direction the opposite way.
- **Recommended:** "Renames source fields. Keys are the normalized field names (for headerless CSV, the `columns` names as written); values are the names rows carry, and downstream declarations use the values." / "Column names for headerless tabular input, in order."
- **Depends on unlanded work:** codex-final.

### P-14 P2: No payload publishes the types of created fields
- **Problem:** the skill says to "declare the type the value actually has", and G2 refuses certain union conflicts, yet discovery never shows output types (M06, M23). `created_output_fields` and `output_field_declarations` appear only in validation code (`state.py:4881-4915, 6491`).
- **Recommended:** project each plugin's created fields and their ADR-050 types into `get_plugin_schema`, or a hint, derived from `created_output_fields()` and pinned by a test. This is a published fact, not authoring.

### P-15 P2: What the six merge-2 plugins' guidance must say
- **batch_normalize and batch_duplicate_flag:**
  - they emit exactly one row per buffered row, in order, and run under passthrough or transform (never "the only");
  - state null handling and each created field's type;
  - dropping duplicates is a downstream gate's job.
- **batch_rater_agreement, batch_calibration, batch_correlation and batch_retrieval_metrics:**
  - "run under `transform`; passthrough is refused";
  - each metric's type and when it is null;
  - which inputs fail the whole batch (routed by on_error, value-free).
- **All six:**
  - every `*_field`, `key_fields`, `group_field` option carries the P-09 suffix;
  - `example_use` shows the correct `output_mode` (`test_batch_catalogue_metadata.py:154` derives it from the flag on example-rank);
  - add them to `_NUMERIC_VALUE_FIELD_AGGREGATION_PLUGINS` where they apply (B-23), or retire that list.

### P-16 P3: The batch reducers' hints
- **Recommended:** add one shared hint: "Run under `output_mode: transform`; passthrough is refused. A failed batch sends every buffered row to `on_error`; `discard` records them as quarantined." (M17, M20)

### P-17 P3: type_coerce hints
- **Location:** `type_coerce.py:545, 552`.
- **Problem:**
  - :545 says "Sources already validate/coerce…", which contradicts the csv source's own text and the skill's `declared_input_type_mismatch` remedy.
  - :552 says "…derived automatically from its conversions", which holds only when the schema declares the field; in observed mode nothing is published until T1 (`probes/C/cfg/tc2`).
- **Recommended for :545:** "Use type_coerce when values arrive as the wrong type: an observed CSV/JSON source keeps strings, and an upstream transform may emit `any`."
- **Depends on unlanded work:** T1, for :552.

### P-18 P3: Enums show as `[object]` in `list_transforms`
- **Location:** `web/catalog/service.py:450-583`. 91 fields at HEAD, including batch_rank's `order` and `ties`.
- **Problem:** predates the lane.
- **Recommended:** resolve `$ref` enums to `string` plus `enum` values.

---

## Guidance that contradicts itself across surfaces

1. **Whether a node's schema declares its created fields' types.**
   - Says it never does: T-01 (tool schemas, terminal schema, patch_node_options), `config_base.py:745`, S-05 (`composer.md:1236`), generation.py repairs :3294/:3426.
   - Says it does: `pipeline_capabilities.md` §Declared Types, the G2 remedy, the value_transform runtime reason, and `value_transform.py:6-8`.
2. **Spelling.**
   - `composer.md:527-531` ("literal observed header") contradicts `:533-552` and the build refusal (S-01).
   - `composer.md:549-552` ("template reads the header") contradicts the LLM config refusal (S-02), and the rulings and the code disagree with each other there too.
   - On codex-final, the skill table says to declare the `field_mapping` target while explain says "normalized form" (R-04).
   - `csv_source.py:680` and `config_base.py:564` state the mapping direction in opposite ways (P-13).
3. **`[]`.**
   - Suggested: `llm/base.py:637, 706`, `rag/core.py:230, 243`, `generation.py:1076`, guided step_3:120.
   - Forbidden: `state.py:1990`, `llm/base.py:63`, the `query_input_columns_undeclared` guidance.
   - Refused by the web control: `plugin_policy/validation.py:237, 333`.
   - Described as "shows the whole row" in `pipeline_capabilities.md:297`, where tojson, `{{ row }}` and methods fail.
4. **Row methods.**
   - `pipeline_capabilities.md:302-304` says they "read fields".
   - The LLM refusal calls the same forms "dynamic row field access (row-api via row API)".
   - The CHANGELOG inventory says RAG `row.items()/keys()/values()` is "refused at configuration", but that holds only under a list: `r_keys_optout` under `[]` validates and every row fails.
5. **Passthrough.**
   - HEAD capabilities say no plugin qualifies.
   - example-rank capabilities and batch_rank's own text say "the one"/"the only".
   - Merge 2 adds two more.
   - The explain record names batch_replicate.
   - The emitted message goes by declaration.
6. **Multi-query.** The aid says "the whole source row"; ADR-051 and the refusal say only the declared fields (T-02).
7. **Union coalesce remedies.**
   - The build message says "every branch's last node".
   - The explain text says "each such producer … not one it computes itself".
   - The mixed-schema remedy says "or all observed" (R-06, R-07).
   - Two different emitted texts exist for one code (R-15/B-18).
8. **type_coerce.**
   - `generation.py:3296` and `type_coerce.py:552` say its type is "derived automatically", which is false in observed mode until T1.
   - `type_coerce.py:546` says on_error catches everything, which is false for a missing field (P-03).
   - `type_coerce.py:545` contradicts the csv source text.
9. **Guided vs. the runtime.** step_3:119 says "every declared field must be interpolated"; the runtime admits a declared field the template does not read.

## Refusal remedies that point at something itself refused

| remedy | where | why it fails | condition |
|---|---|---|---|
| `required_input_fields: []` for row-API reads (`to_dict`, `contract`, `source_row.to_dict`) | `llm/base.py:633-638` | every row fails at render (`a_todict_optout`, `a_mq_sr_todict_optout`); G3 refuses it | always |
| "declare `keys`", or `[]`, for `row.keys()` | `llm/base.py:57-65, 860-866`, `rag/core.py:237-250`, `state.py:1982-1991, 4043-4061` | every row fails (`a_keys_optout`, `r_keys_optout`) | always |
| any `[]` opt-out | `llm/base.py:637, 706`, `rag/core.py:230, 243`, `generation.py:1076`, guided step_3:120 | `input_fields_unprovable` | a field-scoped prompt shield is configured |
| RAG `[]` | `rag/core.py:230, 243` | a `{{ row \| tojson }}` template fails every row | until Codex C2 is fixed |
| "use `row.source_row.<column>`" | `llm/base.py:901`, `state.py:4247`, `generation.py:1501` | `query_input_columns_undeclared` | the column is undeclared |
| "make every branch observed" | `coalesce_schema_mode_mixed` guidance and builder text | `coalesce_union_type_incompatible` (G2 refuses in every mode) | a certain type conflict |
| "declare the type on every branch's last node" | `pipeline_capabilities.md:323-325`, union_merge message | refused as the input contract of a value_transform that rewrites the field from another type (`v_typed_rewrite_str_to_int`) | a type-changing rewrite |
| "Set output_mode to 'passthrough'" | `generation.py:1430` | `batch_transform_misplaced` | every shipped batch plugin (all but batch_rank once X1 lands) |
| "batch_replicate: set output_mode: 'transform'" | `generation.py:1444` | not refused, but aimed at the wrong plugin for reducers under passthrough | always |
| "rewrite as its normalized form" | `generation.py:1827` on codex-final | `field_name_header_spelling` again | a source `field_mapping` renames the header |
| "insert a type_coerce upstream" | `schema_validation.py:600`, composer.md table | run ends with `DeclaredRequiredInputFieldsViolation`, or refused at build | the field is absent on some rows |
| "Use `extract_jinja2_fields()`" | `llm/base.py:699-712`, `config_base.py:758` | not refused, but the planner cannot call Python | always |

---

## Gates the updater must regenerate or respect

| id | gate | what it pins | triggered by | how |
|---|---|---|---|---|
| G-01 | Skill hash: `skills/__init__.py` `load_skill_with_hash` / `assert_skill_hash_unchanged_on_disk`; `composer_skill_hash` audit rows | No test holds a literal SHA (fixtures use placeholders). At runtime: `RuntimeError("Composer skill hash mismatch…")` if a file changes after the cache fills. | any skill edit | Nothing to regenerate. Never edit `skills/*.md` while a suite or elspeth-web is running (skill-hash-mismatch errors); restart elspeth-web after deploy. |
| G-02 | Capability field inventory: `capability_skill.validate_capability_field_contract`; `tests/.../test_capability_skill_identity.py:257-265` | `documented_capability_fields(core) == actual_fields`; each `[capability-node:*]` anchor exactly once | making `union_collision_policy` (or any field) authorable; moving an anchor | Hand-edit the table at `pipeline_capabilities.md:211-223`. |
| G-03 | Capability ownership and no-deployment-facts: `test_capability_skill_identity.py:153-176, 194-228` | the six `[capability:*]` anchors appear once, only in the core; `build_system_prompt(None)` contains none of `web_scrape, field_mapper, azure_prompt_shield, collision_policy, url_field, response_field, llm_response…` | skill rewrites. `field_mapper` wording must stay in the aids (T-09). `batch_rank` is not on the list, but S-06 recommends not naming it. | Keep plugin names out of the skills. |
| G-04 | Legacy explain-pattern freeze: `test_validation_guidance_catalogue.py:30-40` + `validation_guidance_legacy_patterns.json` | pattern order and membership frozen; explanation and fix text may change; new codes must be `DirectValidationGuidance` (`:231-244`) | R-01 (text only, allowed); R-08, R-14, G3/E7 codes (direct records) | Never edit the JSON. |
| G-05 | Frozen discovery bytes: `tests/unit/web/composer/fixtures/discovery_response_scaffold.json` via `test_discovery_response_contracts.py:162-164` | the grammar and get_audit_info responses, byte-exact, on every surface | T-06 | Regenerate by hand; there is no script. |
| G-06 | Frozen generation bytes: `fixtures/generation_response_wire_4f206253.json` via `test_generation_response_contracts.py:108-116` | the `explain_none` case embeds the whole closed-code list (`generation.py:2136`); `assistance` embeds passthrough's assistance | any new closed code (R-08, R-14, G3, E7, merge 2); passthrough hint edits | Regenerate from that module's `collect()`, by hand. |
| G-07 | Registered codes: `test_error_code_redaction.py:300-327` | `set(_VALIDATION_GUIDANCE_BY_CODE) - REGISTERED_ERROR_CODES == set()` | every new code | Add it to `error_codes._EMITTED_VALIDATION_ERROR_CODES`. |
| G-08 | Aids size budgets: `test_planner_authoring_aids.py:1297-1305` (digest ≤ 28 KiB), `:1685-1694, :1882` (≤ 8 KiB) | measured digest: HEAD 22,704 B; example-rank 23,152 B; estimate after merge 2 about 25.8 KiB | T-04b, T-02/T-08/T-09 aid text, merge 2 | Keep additions short. |
| G-09 | Aid exemplars must pass `build_set_pipeline_candidate` (`test_planner_authoring_aids.py`) | exemplar validity | editing an exemplar; rule text alone does not trigger it | Re-run the module. |
| G-10 | Runtime-rejection parity: `config/cicd/runtime_rejection_parity.yaml` (`scripts/cicd/runtime_rejection_parity.py`; `test_runtime_rejection_parity_gate.py`) | identity = (path, qualname, exception, message, ordinal); `unadjudicated` fails | any runtime refusal message edit (R-02, R-03, R-10, R-11, R-12), G3, E7 (builder.py), T1 D5; codex-final already adds 10 lines | `.venv/bin/python scripts/cicd/runtime_rejection_parity.py --write`, then adjudicate by hand. |
| G-11 | Created-output-type census: `tests/invariants/test_created_output_field_types_are_accounted.py:36-82` | every `any` field has a `_REASONS` entry; a stale reason fails; `("value_transform","*"): _EXPRESSION` "(B7 ruling)" | T1 (the reason narrows or goes stale), merge 2 | Hand-edit. |
| G-12 | Composer/runtime agreement: `tests/integration/pipeline/test_composer_runtime_agreement.py` classes BatchPlacement (~7637), TemplateLiteral (~7930), LlmAuthoredOutputType (~8071), FieldNameSpelling (~8195), RagQueryTemplate (~8531), CertainUnionTypeConflict (~8611), CoalesceUnionType (~5687) | both surfaces refuse identical configs and emit identical text for `llm/base.py` and `rag/core.py` refusals | R-02, R-03, R-10, R-11, R-12, G3, T1, E7 (its patch touches no composer file, so the ruling's "composer + runtime" agreement is not yet mirrored) | Add rows per refusal. |
| G-13 | Proof-catalog inventory: `docs/architecture/state_engine/proof-catalog/v3/catalog.json` (+ `evidence_selectors.json`) via `tests/unit/plugins/test_discovery.py:21` | a changed plugin inventory requires a catalog revision | batch_rank (example-rank +176 lines), each merge-2 plugin | `scripts/state_engine_plugin_matrix.py` / `scripts/state_engine_assessment_lib/catalog.py`, as X1 did |
| G-14 | `config/cicd/soft-mapping-census.yaml` (generated), `contracts-whitelist.yaml` | per-file counts | new plugin code | `python scripts/check_contracts.py --write-census` |
| G-15 | Skill phrase pins: `test_skills_loader.py:36-92`, `test_reference_join.py:120-125` | exact phrases in `pipeline_composer.md`, e.g. the "Joined or enriched field" row's "get_plugin_schema", "get_plugin_assistance", "Preserve supplied reference data", "explicit conversion before the consumer" | rewriting those rows (S-05, S-11) | Keep the phrases or update the tests. |
| G-16 | Knob-schema goldens: `tests/golden/web/catalog/knob_schema/*.json` (56 files) via `test_knob_schema_golden.py` | every Field description; file set = plugin set | P-01, P-02, P-08, P-09, P-13, T-02 (`queries` Field), T-06 (reference_join), T-01 (`config_base` schema), every new plugin | No tool: rewrite with the test's own `_stable_json` over `CatalogServiceImpl(get_shared_plugin_manager())._schema_cache`. |
| G-17 | Policy-view goldens: `tests/golden/web/catalog/policy_view/*.json` (4 files) via `test_policy_view_golden.py` | `transform__llm.json` pins llm hint text | P-10, P-11, P-12 | Same method as G-16. |
| G-18 | Batch catalogue metadata: `test_batch_catalogue_metadata.py` | `EXPECTED_BATCH_TAGS`, `_REQUIRED_GUIDANCE` phrase tuples, the example `output_mode` (`== "transform"` at :147 on HEAD; derived from the flag at :154 on example-rank) | P-05, P-16, T-04b, merge 2 | Hand-edit. |
| G-19 | Assistance and reference tests: `test_plugin_assistance_coverage.py`, `test_composer_hint_config_consistency.py`, `test_value_transform.py:851-906`, `test_catalog_reference_content.py`, `test_core_catalogue_metadata.py`, `test_external_catalogue_metadata.py`, `tests/fixtures/catalog_reference.py` | hint and assistance text | P-03, P-05 … P-12, P-16, P-17 | Hand-edit. |
| G-20 | `test_validation_guidance_catalogue.py:274` | asserts `"batch_replicate" in explanation` | R-01 | Re-cut on batch_stats. |
| G-21 | Scenario-corpus `source_file_hash` manifest (`docs/architecture/dag/scenario-corpus/v1/manifest.yaml`) | a plugin file's hash | any edit to a plugin source file (`llm/base.py`, `rag/core.py`, `type_coerce.py`, `value_transform.py`, `field_mapper.py`, batch plugins) for P-* / R-* text | Rotate the manifest (lane precedent: 967aea7ab, f53d2ac34). |

No test pins the tool-description strings changed by T-01, T-04a, T-05 or T-10. Tool bytes are hashed only per call (`effective_tool_hash`, `test_capability_skill_identity.py:327-409`); no snapshot is committed.

---

## Order of work (suggested; composer invariants respected throughout)

1. **Now, needing nothing unlanded:**
   - S-01, S-05, T-01, R-01, T-02, P-01, P-02, P-03.
   - Resolve S-02: decide whether the code or the teaching changes, then change that one.
   - Then the P2 teaching lines.
   - Every fix teaches or refuses; none routes around the planner. T-04b and P-14 publish plugin facts derived from class declarations and author nothing.
2. **With G3:** S-03, S-04, S-08, R-02, R-03, R-09 … R-11, P-11. Use one row-API message and never offer `[]` for row-API misuse.
3. **With the codex-final cherry-pick:** R-04, S-10, P-13, and the `field_mapping` clauses of T-05, T-09 and P-09.
4. **With T1:** S-07, R-05, T-03, P-06, T-06, T-08, P-17, and the G-11 census reason.
5. **With the example-rank cherry-pick, then merge 2:** S-06, P-04, T-04, P-15, B-23.
6. **With E7:** R-14/S-11 rows for `union_field_collision` only (the policy is not authorable in the composer).

Coverage of the CHANGELOG draft's "Newly refused configurations": every bullet has some teaching line except the optional-field `edge_field_type_incompatible` (S-11/R-08) and the type_coerce missing-field abort (P-03). The `to_dict`/`contract`/`keys` part is actively contradicted (S-04). The alias form is taught only on codex-final (S-10). The CHANGELOG's own RAG-methods line needs an "under a declared list" qualifier (contradiction 4).
