# Agent / LLM wiring audit — A, B, C (composer-desk-audit-20261112)

HEAD: `1a7ff0e6` (Merge PR #358, release/0.8.11-candidate). Read-only; nothing under src/ tests/ docs/ edited.
Scratch scripts + raw outputs: `.claude/lanes/composer-desk-audit-20261112/opus/agent-llm-wiring/`
(`a_multiquery_interp.py` → `a_all_override.out`, `a_mixed.out`; `b_report_document.py` → `b.out`;
`c_notfor_xrefs.py` → `c_core.out`, `c_core_no_llm_profile.out`; `c_detail.py` → `c_detail.out`;
`c_follow_notfor.py` → `c_follow.out`). All exited 0.

| | Verdict | Demo severity |
|---|---|---|
| A. Multi-query + vague_term review | **CONFIRMED** (silent drop; tool admits it) | **HIGH** (integrity; the live model avoided it) |
| B. report_assemble → document HIGH warning | **CONFIRMED**, plugin-code gap, not composer-fixable | **HIGH** (visible ⚠ on the recommended pairing; does not block) |
| C. `not_for` names unavailable plugins | **CONFIRMED** for default policy; **not present** in the live demo deployment | **LOW** for this demo, MEDIUM for default-config deployments |

---

## A. Multi-query LLM node + interpretation (vague_term) review

### Verdict: CONFIRMED — the accepted value reaches only queries *without* their own template; when every query has its own template it reaches **no** prompt, and nothing rejects that wiring.

**Mechanism (code):**
- Resolver writes only the node-level template: `src/elspeth/web/sessions/pending_interpretation.py:804-822`
  (`patched_options["prompt_template"] = new_template`); `queries` is copied through untouched.
  The legacy placeholder path is the same (`:997-1001`).
- Runtime template selection: `src/elspeth/plugins/transforms/llm/transform.py:644-649` — a query with
  `template` uses its own pre-compiled override (`:548-550`); only a query with `template=None` falls back
  to the node-level `self.template`. A per-query template fully replaces the node-level one.
- Per-query tokens are rejected (`src/elspeth/plugins/transforms/llm/multi_query.py:236-250`, docstring:
  "there is NO delivery mechanism into per-query templates").
- The staging gate `vague_term_wiring_count` (`src/elspeth/web/interpretation_state.py:2947-3018`) returns
  1 whenever `prompt_template_parts` holds an `interpretation_ref` for the requirement — it never asks
  whether any query actually renders the node-level template. Every gate goes through it
  (`tools/sessions.py:2819`, `composer/interpretation_surfacing.py:145,373`, `interpretation_state.py:674`).
  The resolver's own backstop (`pending_interpretation.py:790-802`) checks only that a ref exists in the parts.
- `tests/unit/web/composer/test_request_interpretation_review_tool.py:1718-1731` (test_05g) calls the
  "resolver succeeds while silently dropping the accepted value" case "an audit divergence worse than a 422".
  The all-override multi-query shape is that same divergence, and it is uncovered.

**Reproduction (real catalog, `execute_tool("set_pipeline")` → production `_patch_llm_transform_prompt` →
real `LLMTransform`, each query rendered with the executor's own selection):**
```
MODE: all_override
set_pipeline success: True
stage1 errors: []
stored interpretation_requirements: [('vague_term', 'eligibility', 'pending'), ('llm_prompt_template', ...), ('llm_model_choice', ...)]
patched node-level prompt_template contains ACCEPTED: True
query 'eligibility': own_template=True accepted_in_prompt=False rendered='Judge eligibility of: ROW-DESC'
query 'risk': own_template=True accepted_in_prompt=False rendered='Judge risk of: ROW-DESC'
--- llm_prompt_template review draft ---
...
Node-level prompt_template: not used (every query supplies its own template).

MODE: mixed   (risk has no template)
query 'eligibility': own_template=True accepted_in_prompt=False rendered='Judge eligibility of: ROW-DESC'
query 'risk': own_template=False accepted_in_prompt=True rendered='Assess ROW-DESC. Criterion: ACCEPTED-RUBRIC-SENTINEL: ...'
```
So the composer admits the node. The user is shown a vague_term card for a slot inside a template that the
auto-staged prompt card on the same node calls "not used". Resolving it changes nothing the model sees.

**Is the limitation taught?** Only by implication. Nothing states it directly.
- `planner_authoring_aids.py:867-876` says: "NEVER put {{interpretation:...}} tokens inside a queries.*.template … Reviewed
  slots belong in the node-level template."
- `planner_authoring_aids.py:837-840` and `:442-448` say the node-level template is a fallback for queries without one, and
  "optional when every query supplies its own".
- `skills/pipeline_composer.md:797-801` and `skills/pipeline_capabilities.md:413-414` say per-query templates cannot carry
  interpretation tokens.
- The llm `get_plugin_assistance` hints (`transform.py:2157-2163`) require a vague_term whenever the model writes a
  rubric. They say nothing about multi-query.

No text tells the model that a reviewed slot reaches only the fallback queries, so it has to put the judged
criterion in a query that has no template of its own, or use separate nodes. DeepSeek worked this out itself
(s08 turn01 call 0/1 reasoning: "if every query supplies its own template, then node-level prompt_template
is not used… the interpretation tokens don't appear in the queries that actually run") and fell back to one
single-prompt structured node (s08 `state.json`). That is why the live run was fine.

**Severity: HIGH.** This is an integrity defect: a user-approved rubric is silently absent from the prompts that
run, which goes against the auditability contract. It did not show up in the live demo, and an attentive
operator could spot the "not used" line on the prompt card. Advisor coverage is only probabilistic: it gets a
`prompt_template_in_use: not used` fact (`composer/advisor_context.py:764-800`) but has no deterministic rule.

**Suggested fix:** In `vague_term_wiring_count` and the resolver backstop, also require that at least one query
renders the node-level template (`MultiQueryPromptSurface.node_template_users` is non-empty,
`interpretation_state.py:2418-2420`). Otherwise reject at staging with a clear code. Also add one rule line:
"a reviewed slot reaches only queries without their own `template`; put the judged criterion in such a query or use separate nodes."

---

## B. `report_assemble` → `document` sink: HIGH "declares no semantic facts" warning

### Verdict: CONFIRMED. This is a plugin-code gap; no composer option clears it. It is advisory only: it blocks neither execution nor auto-commit, and the advisor passed it as CLEAN.

**Reproduction** (csv source → `report_assemble` aggregation, the live s08 node shape → `document` sink;
real catalog, `set_pipeline`):
```
[baseline markdown] success=True ... semantic_warnings=1
    WARN severity=high code=semantic_contract_violation: Semantic contract: 'document' sink 'doc' requires field
    'report_body' (document.field.verbatim_text) but upstream producer 'assemble' (report_assemble) declares no
    semantic facts for that field. Producers semantically feeding this consumer must declare output_semantics().
    contract ... producer_facts=None ... severity='high', unknown_policy=WARN ... outcome=UNKNOWN
[format=plain_text]    ... semantic_warnings=1   (same)
[format=html_fragment] ... semantic_warnings=1   (same)
[ra schema fixed]      ... semantic_warnings=1   (same)
[sink schema observed] success=True is_valid=True errors=0 semantic_warnings=1
ReportAssemble defines output_semantics itself: False
ReportAssemble.output_semantics() -> OutputSemanticDeclaration(fields=())
BaseTransform.output_semantics is inherited: True
document requirement: document.field.verbatim_text unknown_policy= warn severity= high
ReportAssemble config fields: ['format', 'join_with', 'output_field', 'required_input_fields', 'schema_config', 'text_field', 'title']
```
- **Root cause:** `ReportAssemble` (`src/elspeth/plugins/transforms/report_assemble.py:97-325`) does not override
  `output_semantics()`, so it inherits the empty base (`plugins/infrastructure/base.py:1914`). It already knows
  the facts: `created_output_fields` declares the output `str` (`:247-249`), and every renderer returns text
  (`:216-245`). The document requirement is `unknown_policy=WARN`, and `FieldSemanticRequirement.severity`
  defaults to `"high"` (`contracts/plugin_semantics.py:211`; requirement at `plugins/sinks/document_sink.py:414-428`).
  The message comes from `web/composer/_semantic_validator.py:461-492`.
- **Can the composer clear it?** No. None of report_assemble's config fields (listed above) touches semantics, and none
  of the format or schema variants changed the result. No transform in the default core can sit between the two to
  supply semantic facts for a field it does not produce. The planner's repair catalogue
  (`composer/tools/generation.py:908-914`) still says "swap the producer, or route through a transform that produces
  the required content kind". The document sink's issue assistance (`document_sink.py:433-442`) says "point field at a
  string-valued field, or convert the value to a string upstream", but the value is already a string. Both send the
  model after a fix that does not exist.
- **Blocks execution? No.** The runtime phase reports it as `passed=True`, "N unproven (advisory)"
  (`web/execution/_validation_authoring.py:965-995`). Live s08 `validation.json` shows
  `"semantic_contracts" passed: true, "1 semantic contract(s) checked; 1 unproven (advisory): document.field.verbatim_text"`.
  Execution raises only on `semantic_errors` (`web/execution/service.py:1371-1373, 2300-2322`).
- **Blocks auto-commit? No.** Auto-commit is gated on `runtime_result.is_valid` (`composer/planning_application.py:719, 856`).
  The `[sink schema observed]` run is `is_valid=True` even with the warning, and live s08 `validation_is_valid: true`.
- **Advisor:** in live s08 turn02 the advisor call (`z-ai/glm-5.3`, call 2) had the warning in its input
  (`warning_in_messages= 1`) and returned `{"verdict": "CLEAN", ... "publication_verdict": "ADMIT"}`. It did not flag it.
- **Approved pairing for "many rows → one briefing":** yes, and it is exactly this pairing. The document sink's own
  digest text (`document_sink.py:131`: "Use report_assemble first to produce one aggregate value") and hint
  (`:453-455`: "put report_assemble upstream to combine rows into one value first") recommend it. The skill pack does
  not mention report_assemble at all (grep of both skill files: no hits). So the pairing ELSPETH teaches always
  carries a HIGH ⚠, and the chat summary shows the first warning (`frontend/src/components/chat/PipelineValidationSummary.tsx:111-118`).
  In s08 that first warning is this one, in plugin-developer wording ("must declare output_semantics()").
- Side finding: the document sink's `example_use` (`document_sink.py:134-145`) uses `schema.mode: fixed` with only
  the document field. Behind report_assemble that is a hard `sink_locked_extras` error, because report_assemble also
  emits 8 metadata fields (baseline run above: `ERR sink_locked_extras`). The live model avoided it by choosing
  `flexible`.

**Severity: HIGH (demo optics), not a blocker.** A clean, recommended "briefing" pipeline shows a HIGH-severity
⚠ that the user and the model cannot clear, and the advisor ignores it.

**Suggested fix:** Add `ReportAssemble.output_semantics()` declaring `output_field` as `value_type=STR` and
`text_framing=UNCONSTRAINED`. A `title` / `join_with` can carry newlines, so the framing is not otherwise statically
bounded. Set `content_kind` from `format`: PLAIN_TEXT/MARKDOWN, and UNKNOWN for html_fragment. Then
report_assemble → document is SATISFIED. Add a pinned test for that edge.

---

## C. Planner `not_for` / hint cross-references to unavailable plugins

### Verdict: CONFIRMED for the default (empty-allowlist) policy. Not present in the live demo deployment, which has a wide allowlist.

**Visible set (live objects):** `compile_web_plugin_policy` was run over the real registry with
`WebSettings(plugin_allowlist=())`, then `build_plugin_snapshot` and `PolicyCatalogView` with a real
`OperatorProfileRegistry`. One run had an openrouter `llm_profiles` entry (`core`) and one had no LLM profile
(`core_no_llm_profile`):
```
plugin_allowlist setting: ()
visible: {'source': ['csv','json','llm','text'], 'transform': ['field_mapper','line_explode','llm','reference_join','report_assemble','web_scrape'], 'sink': ['csv','document','json','text']}
```
Side finding: the code's required core (`src/elspeth/web/plugin_policy/compiler.py:17-40`, 14 ids) is wider than
`docs/reference/environment-variables.md:589-592`, which lists 9 and omits `source:llm`, `sink:document`,
`transform:line_explode`, `transform:reference_join` and `transform:report_assemble`. The doc is stale.

**Matcher controls:** whole-token regex over all 52 installed plugin names.
```
CONTROL+ field_mapper.not_for: 'Not for expression evaluation or type coercion: use value_transform ... and type_coerce ...'
CONTROL+ matched: ['type_coerce', 'value_transform']
CONTROL- matched (expect only none or common words): []      # "my_value_transform", "json_explode_x", "transform"
```
Names that are also ordinary words (`null`, `passthrough`, `truncate`, `llm`, `text`, …) were split out and
checked by eye. All hits were the JSON literal `null`, `output_mode: passthrough`, or the verb "truncate", so none
are references.

**Findings (`core` variant; distinct unavailable names taught = 8):**

Digest (the planner system prompt, `discovery_digest`, `planner_authoring_aids.py:1725-1768`; `not_for` is carried verbatim, with no policy filter):
- `transform:field_mapper.not_for` → `value_transform`, `type_coerce`
- `transform:line_explode.not_for` → `blob_csv_expand`
- `transform:reference_join.not_for` → `blob_csv_expand`
- `transform:web_scrape.not_for` → `blob_fetch`

Other authoring_aids sections (same prompt):
- `llm_output_contract.rules[5]` (`planner_authoring_aids.py:825-829`) → `value_transform`, `json_explode`, `blob_json_expand`, `type_coerce` ("narrow it by inserting a type_coerce transform"). It is shown only when llm is visible, but it does not check whether those plugins are.
- `expression_grammar` → `value_transform`. This is only context ("applies to gate conditions, value_transform expressions"), so it is benign.

get_plugin_schema / get_plugin_assistance hints (on demand):
- `field_mapper[22]` → `type_coerce`. `reference_join[5]` → `type_coerce` ("insert type_coerce after the join before a numeric consumer").
- `line_explode[3]` → `value_transform` ("use field_mapper + value_transform first").
- `llm[41]`/`web_scrape[16]` → `azure_prompt_shield`, and `llm[48]`/`web_scrape[17]` → `azure_content_safety`. These are already conditioned ("only when discovery lists it" / "do not substitute").

Static skill pack (`pipeline_composer.md`, policy-independent): `type_coerce` ×8, `value_transform` ×2, `batch_top_k` ×1. Most are in the repair table at `:1424-1428`.

**Consequence when followed:** a default-policy planner that follows field_mapper's `not_for` and authors `type_coerce` gets:
```
plugin_not_enabled | Node 'coerce': transform plugin selection is unavailable (plugin_not_enabled): the plugin is installed but not turned on in this deployment's plugin policy; an operator must enable it
```
That costs a wasted repair turn. The digest guidance calls `not_for` "binding on selection: … choose a different
plugin" (`planner_authoring_aids.py:1921-1924`), so for a calculated field or a type change a default deployment
offers a binding prohibition and no legal alternative.

**Live demo deployment:** the s08 captured digest (`turn01/llm_calls.json` call 0, message 1) shows 30 transforms
visible, including `type_coerce`, `value_transform`, `blob_*` and `json_explode`. The scan found 0 unavailable names
in the live digest, 0 in the other authoring_aids sections and 0 in the live system prompt. The live run does not
show C.

**Severity: LOW** for this demo (it does not show up). **MEDIUM** for any default-config deployment: wasted
turns, and dead-end advice that reads as an error in front of a user.

**Suggested fix:** Project `not_for` / composer_hints / the authoring_aids rules through the request snapshot.
Either drop clauses that name a plugin outside `snapshot.available`, or have the plugins state their remedies by
capability and let `discovery_digest` resolve them to available names (with a generic "ask the operator to enable
…" fallback). Also update the docs list at `environment-variables.md:589-592` to match `REQUIRED_WEB_PLUGIN_IDS`.
