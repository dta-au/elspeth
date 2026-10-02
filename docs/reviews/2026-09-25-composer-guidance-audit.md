# LLM-facing guidance audit — read-only snapshot

Snapshot: task checkout `<task-checkout>`, branch `fix/session-ed3c015b-convergence`, HEAD `e32366035ee7802791f173ed32ea4a211423e675`. The checkout has concurrent sibling edits; conclusions below are tied to the per-file hashes. No repository code, database, provider, service, tracker, or tracked documentation was changed for this audit. Interpreter was `<repository>/.venv/bin/python` with both task `src` and task `elspeth-lints/src` on PYTHONPATH; printed `elspeth.__file__` and `elspeth_lints.__file__` both resolved inside this task worktree. The raw live-registry assistance and field metadata snapshots are adjacent as `guidance-inventory.txt` and `guidance-field-descriptions.txt` (both ignored lane files).

The adjacent `guidance-file-shas.txt` records SHA256 values for the 56 registered classes' source files and the Composer guidance/tool source tree at the pre-fix read; it is a provenance manifest, not evidence that every line of every hashed file was semantically audited. Sibling edits began after capture. At the final check, the original hashes for CSV source, Dataverse source, LLM transform, line_explode, and guided step 1 had changed to `0b8bd1d3`, `00952f68`, `2d2d3c98`, `b026efce`, and `9dff13da` respectively (SHA256 prefixes). The findings describe the quoted original text and were **not** revalidated against those later edits. HEAD was still `e32366035ee7802791f173ed32ea4a211423e675`.

## Confirmed defects

### P2 — Dataverse guidance instructs a nonexistent config option

* Guidance: `src/elspeth/plugins/sources/dataverse.py:1153`, `DataverseSource.get_agent_assistance(None)`: “Choose query_mode 'fetchxml' for complex filters, 'odata' for simple field selection.” File SHA256 `7d38c007cbdbe9e015c538c57756790ad4e426a346b7f1a4a0f1b43b8920a77d`.
* Contract: `DataverseSourceConfig.model_fields` contains `entity`, `fetch_xml`, `select`, `filter`, `orderby`, `top`; no `query_mode`. `dataverse.py:216-224` requires exactly one of `entity` or `fetch_xml` and permits select/filter/orderby/top only with entity. The parser forbids extra input.
* Controlled reproduction: `DataverseSourceConfig.from_dict` accepted a minimal valid entity config (known positive); the same config plus `query_mode='odata'` rejected with `PluginConfigError ... query_mode: Extra inputs are not permitted`; a separate unknown sentinel field was also rejected (known negative). No network call or run was made. Existing parser tests: `tests/unit/plugins/sources/test_dataverse_source.py:143-165` supply valid structured/FetchXML examples.
* Consequence: A planner following discovery assistance authors a rejected source and spends repair calls removing the field; the suggested enum also obscures the actual mutually exclusive query contract. Correct the hint to say `entity` selects structured OData and `fetch_xml` selects FetchXML; select exactly one.

### P2 — LLM plugin assistance assigns backend-owned prompt review to planner

* Guidance: `src/elspeth/plugins/transforms/llm/transform.py:1994,1997`, `LLMTransform.get_agent_assistance(None)`: “stage an llm_prompt_template review” and “put both interpretation_requirements in the LLM node options ... one vague_term ... and one llm_prompt_template.” File SHA256 `37ef675216220f0bc31c88bc65b5070055b52f83ea1c2fc60f32c701749277b5`.
* Contract: `src/elspeth/web/composer/skills/pipeline_composer.md:1004-1012` says the backend stages and surfaces every `llm_prompt_template` row and “Never author the row; never call the review tool for it.” `src/elspeth/web/composer/planner_authoring_aids.py:2306-2313` agrees. `src/elspeth/web/composer/tools/sessions.py:276-284` rejects `request_interpretation_review(kind='llm_prompt_template')` with a tool argument error. Skill SHA256 `03f803854df4808ab9667c83154542f7f7f7e9c58b386edc2defffe3ebaa9b4c`.
* Controlled check: live registry returned these hints for the registered `transforms/llm` class, while the request tool's validated kind set excludes `llm_prompt_template`. This proves a guidance conflict and a rejected explicit review request. It does **not** prove that an extra caller-authored interpretation row always fails at `set_pipeline`; that narrower effect was not exercised. The root's live case05/06 evidence can assess observed planner behavior separately.
* Consequence: The planner may author a duplicate/backend-owned row or waste a request call, obscuring the separate caller-owned `vague_term` requirement and delaying convergence. Correct the plugin hint to stage/wire only the `vague_term` row for authored judgment semantics and describe prompt review as automatic. Keep the advice to preserve the authored prompt for user review.

### P2 — LLM hint excludes its own structured-output parser

* Guidance: `src/elspeth/plugins/transforms/llm/transform.py:2007`: “Prompt-requested JSON keys are not separate pipeline fields unless another transform parses them.” This is true for prompt wording alone, but false when this same plugin's `output_fields` option is configured; the qualifier directs planners away from a supported native output contract. Same file SHA256 as the LLM review finding above.
* Contract: `transform.py:443-448` calls `extract_structured_fields(content, self.output_fields)` and updates the emitted row; `transform.py:1602-1624` adds those unprefixed names to `declared_output_fields` and its output contract. Controlled constructor probe using inert OpenRouter config with one row field: plain single-query node declared `answer`, `answer_model`, `answer_usage`; adding `output_fields:[{suffix:'score',type:'integer'}]` also declared `score`. No provider call was made, so the probe proves configuration and output contract; the row extraction path was verified in source.
* Consequence: Composer may add an unnecessary parser transform or claim a requested typed JSON output is unavailable. Say that prompt wording alone does not create fields; configured single-query `output_fields` are parsed by the LLM transform itself and become typed row fields, while other raw JSON replies need a supported parser.

### P3 — line_explode promises to remove blank lines but emits them

* Guidance: `src/elspeth/plugins/transforms/line_explode.py:397`, `LineExplode.get_agent_assistance(None)`: “emits one row per non-empty line.” File SHA256 `ca7aee13a7977c96cb7a935293fbd37f11077937ed2be5cbc0d8be85ec832509`.
* Runtime: `_splitlines_bounded` appends each segment including empty segments at lines 282-293; `process` emits each returned segment at lines 500-505. Controlled function probe: positive blank case `a\n\nb` returned `(['a', '', 'b'], 3)`; negative no-blank case `a\nb` returned `(['a', 'b'], 2)`. This exercises the splitter directly, not a full engine run; the emission loop was checked in source.
* Consequence: A composer using the summary to prepare generated multiline text or a text-sink path can produce extra empty rows/output records or misstate row accounting. Correct summary to say blank segments are retained, or change runtime behavior deliberately with tests if dropping blank lines is intended. This report recommends only a guidance correction absent a product decision.

### P2 — guided source skill describes fixed-schema rejection as column projection

* Guidance: `src/elspeth/web/composer/guided/skills/step_1_source.md:47-49`: “`fixed` silently *drops* every unlisted column — a quiet all-rows hazard.” File SHA256 `bf8c57eff3a196f1c013a35b06bf3479853c4d02ea76351cb7eaff3422e76c1b`.
* Contract: `src/elspeth/plugins/infrastructure/schema_factory.py:107-110` builds fixed schemas with `extra="forbid"`, which rejects rows carrying undeclared columns. The CSV source's `on_validation_failure` then quarantines or discards invalid rows; it does not project away those fields. Controlled schema probe: fixed `id: str` accepted `{'id':'a'}` (positive), rejected `{'id':'a','extra':'x'}` (contradicts claim), and rejected `{'missing':'x'}` (negative). This probes the common schema validator, not a full source run. Schema factory SHA256 `c7f9449572c076724d47a6636c37548fca46b15174928cd13d16f20e1e84cff4`.
* Consequence: Guided planning may tell the operator extra fields are quietly removed when instead complete input rows fail validation; with discard routing, all such rows can be lost. Replace the sentence with the actual row rejection/quarantine behavior while keeping the recommendation to avoid guessed fixed schemas.

### P3 — CSV invented-source hint asks for a redundant draft that the review tool byte-matches

* Guidance: `src/elspeth/plugins/sources/csv_source.py:686` (discovery assistance): “call request_interpretation_review ... and llm_draft equal to the exact CSV text.” File SHA256 `d4ce0e9b4ce45797657e909b04263d067d39a01861ae31db695e41e7ed75051e`.
* Contract: `src/elspeth/web/composer/skills/pipeline_composer.md:430-444` says to omit `llm_draft` for invented sources because the server resolves the staged exact artifact; retyping it risks escape-sequence drift. `src/elspeth/web/composer/tools/sessions.py:2475-2494` accepts an explicitly supplied draft only when it matches the pending requirement byte-for-byte, otherwise raises `ToolArgumentError`. Tool file SHA256 `0dfb7b1621c71aa7a3bf9358bf0bf64fa16bab18fa04a352b4cbb0784e475aa5`.
* Consequence: Following the plugin hint can turn an otherwise ready source review into a rejected tool call after newline or escape round-tripping. This is a conflicting and fragile recommendation, not an always-invalid option: exact matching text is accepted. Correct the hint to omit `llm_draft` and use the staged source requirement as authority.

### P2 — CSV fixed-schema hint calls audited validation failures silent drops

* Guidance: `src/elspeth/plugins/sources/csv_source.py:677`, `CSVSource.get_agent_assistance(None)`: “fixed mode silently drops rows that don't match.” The same class's `get_post_call_hints` at lines 710-713 says “Fixed mode drops every row whose columns don't exactly match the declared fields.” Original file SHA256 `d4ce0e9b4ce45797657e909b04263d067d39a01861ae31db695e41e7ed75051e`.
* Runtime: fixed schema sets `extra="forbid"` in `src/elspeth/plugins/infrastructure/schema_factory.py:107-110`. CSV source calls `model_validate` at `csv_source.py:577`; its `ValidationError` arm records a validation-error event at 621-626, then emits a quarantined `SourceRow` at 628-634 if the configured destination is not `discard`. With `discard`, the row is audited but not emitted. The earlier controlled fixed-schema probe accepted `{'id':'a'}` and rejected `{'id':'a','extra':'x'}`; those are the positive and contrary controls. This is source/runtime inspection plus a schema validator probe, not a full engine run.
* Consequence: “silently drops” conceals the configured quarantine path and audit event, and can lead the planner to explain a complete-row validation rejection as quiet column projection. Say fixed schemas reject nonconforming rows; `on_validation_failure` determines audited discard versus quarantine. Align the post-call hint with that behavior.

### P3 — CSV failure-route default claim is true only on some Composer seams

* Guidance: `src/elspeth/plugins/sources/csv_source.py:689`: “Set on_validation_failure to a sink name for quarantine/review, or 'discard' to drop with audit. Default is 'discard'.” Same original SHA256 as above.
* Contract: the plugin's `SourceDataConfig.on_validation_failure` is required with `Field(...)` at `src/elspeth/plugins/infrastructure/config_base.py:507-510`; the Composer `set_source` schema requires it at `src/elspeth/web/composer/tools/sources.py:258`. Controlled `CSVSourceConfig.from_dict` accepted explicit `discard` (positive), rejected omission with `PluginConfigError ... on_validation_failure: Field required` (contrary), and rejected an unknown sentinel option (negative). Other Composer seams normalize an omitted route to `discard` (`tools/_common.py:1775-1815`; the `set_pipeline` and `set_source_from_blob` schemas do not require it), so the hint is context-dependent rather than universally wrong.
* Consequence: A planner using the CSV config directly or `set_source` may omit a required value and hit an avoidable validation error. Qualify the convenience-tool default and instruct explicit `on_validation_failure` for plugin config and full source authoring.

## Already assigned context, excluded from new findings

The root reported sibling fixes for `keyword_filter.keywords` versus `blocked_patterns`, `json_explode.field` versus `array_field`, the LLM top-level `prompt_template` requirement when per-query templates are valid, and CSV sink `headers` versus actual column-order controls. Current CSV assistance at `src/elspeth/plugins/sinks/csv_sink.py:600` says `schema.fields` in fixed mode controls order and `headers` controls display names; this file was changing concurrently (snapshot SHA256 `b88ed4890fcaae54c346c4411ee3b16a0f49a722f3c972eba59f3cac225c3ed2`). The audit does not claim these are landed or independently validated. The strict parameterless marker is transport-specific and was not treated as plugin guidance.

## Checked ambiguities and remaining candidates

The LLM transform hint at `transform.py:2005` says `<response_field>_usage` and `<response_field>_model` are appended automatically. Its wording initially looked inconsistent with audit-only success-reason comments, but `src/elspeth/plugins/transforms/llm/__init__.py:256-276` writes those two fields into the emitted row. That hint is **not** a defect; usage/model provenance outside those row fields remains audit metadata. The 42-tool schema read-through exposed minor asymmetries in how much detail each mutation tool gives, but none independently proved a contradictory or invalid recommendation. No additional unverified candidate is promoted to a finding.

### Targeted follow-up: `list_models` is exposed; its unconditional-call wording is overprescriptive

At the later LLM transform SHA256 `603952f576fb030feb4f63229861722d1ddba67c3b9f2bc7eac353f9255fb8c4`, `src/elspeth/plugins/transforms/llm/transform.py:1990` still says “Call list_models before pinning 'model:'.” A live, import-backed palette check found `list_models` in the flat registry (42 tools), freeform web loop (42), CLI MCP exposure (32), and the planner discovery policy on all four `PlannerSurface` values (14–15 discovery tools), even when the prompt aid already supplied `model.catalog`. A deliberately nonexistent `list_modelz` was absent from all three primary palettes as a negative control. The guided step-2 sink-only discovery subset does not contain `list_models`; that subset does not author an LLM model. This establishes that the named tool is real and callable on the relevant authoring paths. Source points: `tools/generation.py:2504-2535`, `service.py:876-892`, `composer_mcp/server.py:116,220`, `capability_skill.py:17-25`, and `pipeline_planner.py:340-360`.

The instruction to call it **before every literal model pin** is less precise than current authoring aids: `planner_authoring_aids.py:2061-2064` says a slug already supplied in `authoring_aids.model_catalog.models_by_provider` can be bound directly with no discovery call; `pipeline_composer.md:678-687` prefers an operator-approved `options.profile`, which omits `model` entirely, and allows a literal model only from a session-served catalog. Thus this is a low-severity guidance ambiguity and possible unnecessary call, not a nonexistent-tool or contract-invalid defect. Suggested clarification if touching the hint: prefer a usable operator profile; when binding a literal model, use a slug in the supplied session catalog, and call `list_models` when that catalog omits the needed provider or a refresh is required. No provider call or model choice was made in this audit.

## Method and coverage

The source of truth for identities was `discover_all_plugins()` (not a source regex). Its live result was 9 sources, 38 transforms, and 9 sinks; all 56 returned a discovery-time `PluginAssistance`. For each registered class, the audit captured and **read** its class descriptor, `get_agent_assistance(None)` summary/hints/examples, and its `config_model.model_fields` plus field descriptions where present. The parser option check was controlled by a known-valid and known-invalid Dataverse config. The line-split check used blank and nonblank controls. These probes establish the findings above, not semantic correctness of every sentence in all 56 assistance entries. The read-through focused on contract-invalid option recommendations, source/sink behavior, schema guarantees, emitted shapes, review ownership, and prompt/field contracts. Nested model properties named by hints (Azure CSV/JSON options, Chroma field mapping, S3 CSV options, Textract and Document Intelligence extract maps) were checked against live Pydantic JSON Schema; the RAG provider_config.collection claim was checked against the registered Chroma config. All six literal issue-specific assistance codes found from registered methods were read and invoked.

The shared surfaces **read** were all of `composer/skills/pipeline_composer.md`, `pipeline_capabilities.md`, guided `base.md` and steps 1-4, `composer/prompts.py`, `capability_skill.py`, `guided/prompts.py`, and the actual 42-tool flat Composer palette returned by `get_tool_definitions()` (40 registered declarations plus `request_advisor_hint` and `request_interpretation_review`). All 42 top-level descriptions and all 128 nested schema descriptions were read; exact captures are in `guidance-tool-descriptions.txt` and `guidance-tool-schema-descriptions.txt`. The six plugin `get_post_call_hints` overrides were also read (CSV and JSON sources, LLM and web-scrape transforms, JSON and database sinks); their conditions and emitted text showed no additional confirmed mismatch in this pass. Relevant discovery/schema projections and validator code were followed for suspicious claims. Dynamic deployment overlays, policy-specific catalog projections, nested plugin option schemas not implicated by suspicious guidance, and actual paid provider behavior were outside this lane's read-only evidence. No paid provider or live acceptance case was run in this lane. A claim of exhaustive semantic parity across the entire guidance corpus would need a frozen checkout plus per-plugin controlled behavior tests and policy-specific catalog projections.

### Live plugin inventory and reviewed coverage

Every row below was returned by the registry and had its discovery assistance, top-level model fields, field descriptions, and class descriptor read. “Reviewed” means prose compared against field names, shared Composer contracts, and targeted runtime paths for suspicious claims; it does not mean every behavior assertion was reproduced.

| Kind | Registered plugin | First-pass coverage |
| --- | --- | --- |
| sources | `aws_s3` | Reviewed; no confirmed mismatch in this pass |
| sources | `azure_blob` | Reviewed; no confirmed mismatch in this pass |
| sources | `blob_rows` | Reviewed; no confirmed mismatch in this pass |
| sources | `csv` | Reviewed; confirmed invented-source review finding above |
| sources | `dataverse` | Reviewed; confirmed finding above |
| sources | `json` | Reviewed; no confirmed mismatch in this pass |
| sources | `null` | Reviewed; no confirmed mismatch in this pass |
| sources | `text` | Reviewed; no confirmed mismatch in this pass |
| sources | `llm` | Reviewed; no confirmed mismatch in this pass |
| transforms | `batch_classifier_metrics` | Reviewed; no confirmed mismatch in this pass |
| transforms | `batch_data_quality_report` | Reviewed; no confirmed mismatch in this pass |
| transforms | `batch_distribution_profile` | Reviewed; no confirmed mismatch in this pass |
| transforms | `batch_drift_compare` | Reviewed; no confirmed mismatch in this pass |
| transforms | `batch_effect_size` | Reviewed; no confirmed mismatch in this pass |
| transforms | `batch_experiment_compare` | Reviewed; no confirmed mismatch in this pass |
| transforms | `batch_outlier_annotator` | Reviewed; no confirmed mismatch in this pass |
| transforms | `batch_paired_preference` | Reviewed; no confirmed mismatch in this pass |
| transforms | `batch_replicate` | Reviewed; no confirmed mismatch in this pass |
| transforms | `batch_stats` | Reviewed; no confirmed mismatch in this pass |
| transforms | `batch_threshold_summary` | Reviewed; no confirmed mismatch in this pass |
| transforms | `batch_top_k` | Reviewed; no confirmed mismatch in this pass |
| transforms | `blob_csv_expand` | Reviewed; no confirmed mismatch in this pass |
| transforms | `blob_fetch` | Reviewed; no confirmed mismatch in this pass |
| transforms | `blob_json_expand` | Reviewed; no confirmed mismatch in this pass |
| transforms | `blob_text_expand` | Reviewed; no confirmed mismatch in this pass |
| transforms | `field_mapper` | Reviewed; no confirmed mismatch in this pass |
| transforms | `json_explode` | Reviewed; no confirmed mismatch in this pass |
| transforms | `keyword_filter` | Reviewed; no confirmed mismatch in this pass |
| transforms | `line_explode` | Reviewed; confirmed finding above |
| transforms | `passthrough` | Reviewed; no confirmed mismatch in this pass |
| transforms | `pdf_rasterize` | Reviewed; no confirmed mismatch in this pass |
| transforms | `reference_join` | Reviewed; no confirmed mismatch in this pass |
| transforms | `report_assemble` | Reviewed; no confirmed mismatch in this pass |
| transforms | `truncate` | Reviewed; no confirmed mismatch in this pass |
| transforms | `type_coerce` | Reviewed; no confirmed mismatch in this pass |
| transforms | `value_transform` | Reviewed; no confirmed mismatch in this pass |
| transforms | `web_scrape` | Reviewed; no confirmed mismatch in this pass |
| transforms | `aws_bedrock_content_safety` | Reviewed; no confirmed mismatch in this pass |
| transforms | `aws_bedrock_prompt_shield` | Reviewed; no confirmed mismatch in this pass |
| transforms | `aws_textract_document_analysis` | Reviewed; no confirmed mismatch in this pass |
| transforms | `aws_textract_inline_analysis` | Reviewed; no confirmed mismatch in this pass |
| transforms | `azure_ai_search` | Reviewed; no confirmed mismatch in this pass |
| transforms | `azure_content_safety` | Reviewed; no confirmed mismatch in this pass |
| transforms | `azure_document_intelligence` | Reviewed; no confirmed mismatch in this pass |
| transforms | `azure_prompt_shield` | Reviewed; no confirmed mismatch in this pass |
| transforms | `llm` | Reviewed; confirmed finding above |
| transforms | `rag_retrieval` | Reviewed; no confirmed mismatch in this pass |
| sinks | `aws_s3` | Reviewed; no confirmed mismatch in this pass |
| sinks | `azure_blob` | Reviewed; no confirmed mismatch in this pass |
| sinks | `chroma_sink` | Reviewed; no confirmed mismatch in this pass |
| sinks | `csv` | Reviewed; no confirmed mismatch in this pass |
| sinks | `database` | Reviewed; no confirmed mismatch in this pass |
| sinks | `dataverse` | Reviewed; no confirmed mismatch in this pass |
| sinks | `document` | Reviewed; no confirmed mismatch in this pass |
| sinks | `json` | Reviewed; no confirmed mismatch in this pass |
| sinks | `text` | Reviewed; no confirmed mismatch in this pass |

### Failure-time assistance check

An AST walk of each registered class's `get_agent_assistance` method found six literal `issue_code` branches: `batch_distribution_profile.value_field.numeric`, `json_explode.array_field.list`, `line_explode.source_field.line_framed_text`, `web_scrape.content.compact_text`, `document.field.verbatim_text`, and `text.field.single_line_text`. All six were invoked and returned assistance; raw text is in `guidance-issue-assistance.txt`. This AST probe was controlled against the known-positive `line_explode.source_field.line_framed_text` branch and an unrelated unknown code that returns `None`. It does not prove absence of dynamically assembled issue-code branches, although none were apparent on method read-through. The line_explode failure-time text does not explicitly promise blank-line removal; the false claim is in its discovery summary.

## Parent review and correction status

The audit agent was report-only and made no product changes. The parent reviewed the eight confirmed findings and assigned a separate implementation lane. All eight teaching corrections passed independent review, runtime/parser-backed controls, registered-plugin metadata checks, and old-guidance mutation controls. The CSV post-call copy was also corrected during review. Integrated suite and fresh live acceptance remain pending; focused checks alone do not establish release acceptance.

The low-severity `list_models` follow-up above remains a documented wording ambiguity. Its tool is exposed and its advice yields a valid model selection; it was not included in the eight confirmed contract/behavior corrections.

## Live-battery follow-up: CSV reference value types

The parent requested a further report-only Sol audit after live case05 at
`e0ea1c873` produced six valid LLM classifications but failed at an integer sink.
The audit confirmed a separate teaching omission: ReferenceJoin did not explain
that numeric-looking CSV lookup cells remain strings. Controlled CSV and JSON
tables produced `str` and `int` respectively; the actual strict CSV sink rejected
the former for an integer field and accepted the latter. FieldMapper's existing
no-coercion guidance was correct. A separate graph-validation defect lost this
known type through an observed selection; that is not attributed to guidance.

The audit agent made no product changes. The parent assigned a different lane to
correct ReferenceJoin assistance, its format-option description and the shared
repair table. These now explain retaining strings or explicitly converting joined
values, without replacing user-supplied CSV. The generic conversion example names
an illustrative `amount` field. Runtime-backed teaching controls, independent
old-guidance mutations and the canonical catalog golden passed. This is a ninth
confirmed teaching correction following the original snapshot, not a claim that
the earlier audit found it or proved exhaustive semantic parity.


### Shared-prompt placement correction

The integrated gate subsequently caught plugin-specific wording in the new shared
repair row. The plugin's dynamically discovered assistance retains the concrete
CSV/string, JSON/numeric and explicit-conversion facts. The shared row instead
instructs the planner to consult plugin schema and assistance, preserve supplied
data, and establish conversion explicitly when required. Both pre-existing
shared-prompt boundary tests stay unchanged; restoring the offending row makes
both fail. The restored affected run passed 217 tests. This correction preserves
the distinction between generic Composer rules and installed-plugin facts.


### Final parent verification

All nine confirmed corrections were implemented by separately assigned lanes,
reviewed by the parent and independently checked; the Sol auditor remained
report-only. Two complete ten-workflow real-provider batteries passed after the
runtime repairs. The final one includes the generic shared-prompt placement
correction and is bound to `1ef793a887f06f29feb11db3afc7762aad4f0448`.
All formerly failing tests passed in an exact 54-ID rerun on that candidate;
static checks and unchanged lint-corpus accounting are detailed in the
[convergence report](2026-09-25-composer-session-convergence.md).
The documented `list_models` wording ambiguity and stated audit coverage limits
remain; this report does not promote them into confirmed runtime defects.
