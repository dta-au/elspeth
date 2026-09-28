Verified against **`4bb77fbd51fe215e0a280688d265c17ff160eaea`**, with the two side branches inspected separately. The review identifies substantial real drift, but **its replacement text should not be applied verbatim**. Several recommendations overstate restrictions, confuse runtime contracts with build-time type publication, or target text that the proposal planner never receives.

No tracked files changed; no commits. Final check:

```text
$ git rev-parse HEAD
4bb77fbd51fe215e0a280688d265c17ff160eaea
EXIT_CODE=0
$ git status --porcelain
EXIT_CODE=0
PORCELAIN_BYTES=0
```

Evidence is in [scratch6](<repo>/.claude/lanes/5887-batch-row/codex/scratch6). Detailed independent evidence files: [skills]( <repo>/.claude/lanes/5887-batch-row/codex/scratch6/skills/report.md), [tools]( <repo>/.claude/lanes/5887-batch-row/codex/scratch6/tools/report.md), [refusals]( <repo>/.claude/lanes/5887-batch-row/codex/scratch6/refusals/report.md), [plugins]( <repo>/.claude/lanes/5887-batch-row/codex/scratch6/plugins/report.md).

**1. Summary table**

Legend: **C** = CONFIRMED; **A** = CONFIRMED-AMEND; **D** = DEPENDENCY-CHECK. Conditional priorities apply when the identified work lands, not to current HEAD.

| ID | Reviewer | Verdict | My priority | Finding |
|---|---:|---|---:|---|
| S-01 | P1 | C | P1 | Literal-header declaration advice contradicts canonical-name enforcement. |
| S-02 | P1 | A | P1 | Identifier-shaped header aliases are wrongly refused; `Price USD` itself works. |
| S-03 | P1 | A | P1 | Whole-row visibility works; broken rendering operations are implementation defects. |
| S-04 | P1 | A | P1 | Callable row-field syntax is misdescribed; replacement must preserve `get`. |
| S-05 | P1 | A | P1 | Created-target schemas declare output types; qualify input/output overlap and T1. |
| S-06 | P1 conditional | D | P1 conditional | Inventory statement is true at HEAD; X1/merge 2 changes it. |
| S-07 | P1 conditional | D | P1 conditional | Current fallback is `any`; T1 changes unauthored provable results. |
| S-08 | P2 | A | P2 | Omitted LLM declaration exposes nothing; distinguish RAG and future G3 refusal. |
| S-09 | P2 | C | P2 | Omitted declarations also make field-scoped shield coverage unprovable. |
| S-10 | P2 | A | P1 | Universal normalization wording is already false for mappings/headerless columns. |
| S-11 | P2 | A | P2 | Targeted repair rows are missing; some runtime reasons are already named elsewhere. |
| S-12 | P3 | C | P2 | Missing failed-batch disposition is substantive teaching. |
| S-13 | P3 | A | P1 | “Every declared field must be interpolated” is false; guided inherits the capability core. |
| S-14 | P3 | A | P3 | Collision policy is unauthorable; ordinary Composer graphs cannot select its fail behavior. |
| T-01 | P1 | A | P1 | Input-only claim is false, but absent from `set_pipeline` and terminal schemas. |
| T-02 | P1 | A | P1 | `source_row` is projected; query input bindings have a separate image-input exception. |
| T-03 | P2 → P1 at T1 | A | P2 | Missing direct-declaration alternative; quoted conditional statement does not automatically become false. |
| T-04 | P2 | C | P2 | Discovery lacks structured passthrough capability. |
| T-05 | P2 | A | P2 | Teach actual carried names, including mapping targets, rather than universal normalization. |
| T-06 | P2 | C | P2 | Stored-set restriction and result-type teaching are incomplete. |
| T-07 | P2 | C | P2 | MCP supplies no skill instructions. |
| T-08 | P2 | A | P2 | Union rule is missing; declarations cannot repair incompatible values by themselves. |
| T-09 | P2 | A | P2 | Canonical-name teaching is missing; adjacent mapper input-only advice is also false. |
| T-10 | P3 | C | P2 | Failed-batch routing omission is substantive, like S-12. |
| T-11 | P3 | C | P2 | Missing `_usage: any` guidance helps prevent incompatible declarations. |
| R-01 | P1 | A | P1 | Explain text retains by-name placement rule and points at a suppressed message. |
| R-02 | P1 | A | P1 | Row-API refusal recommends an opt-out that cannot enable that API. |
| R-03 | P1 | C | P1 | Method calls are misdiagnosed as missing field declarations. |
| R-04 | P1 conditional | A | P1 conditional | Mapping-target correction is needed; proposed remedy references withheld details. |
| R-05 | P1 conditional | A | P1 conditional | T1 dependency is real; runtime type-coerce derivation is already correct. |
| R-06 | P2 | A | P2 | Homogeneous observed modes remain valid; they do not cure independent type conflicts. |
| R-07 | P2 | A | P1 | Excluding computed fields from output declarations is false advice now. |
| R-08 | P2 | A | P2 | Explain entry is missing; proposed downstream-type prohibition is wrong. |
| R-09 | P2 | A | P2 | Opt-out needs qualification; it is not exclusively for computed keys. |
| R-10 | P2 | A | P2 | Remove unavailable Python API advice; preserve intentional whole-row use. |
| R-11 | P2 | A | P2 | Prefer explicit RAG declarations, but `[]` is not inherently an invalid repair. |
| R-12 | P2 | C | P2 | `source_row` repair omits its declaration requirement. |
| R-13 | P2 | C | P2 | Passthrough suggestion omits its capability constraint. |
| R-14 | P2 | A | P2 | Runtime explanations are missing; distinguish reason text from validation codes. |
| R-15 | P3 | A | Mixed | Bundle contains substantive errors, dependency work, and wording issues. |
| P-01 | P1 | A | P2 | Description is incomplete, not wholly false; replacement mishandles RAG. |
| P-02 | P1 | A | P2 | Missing RAG declaration teaching; `query_field` remains visible automatically. |
| P-03 | P1 | A | P2 | Missing-input abort reproduced; code-defect classification refuted below. |
| P-04 | P1 conditional | D | P1 conditional | Rank’s “only” claim becomes false at merge 2, not on its side tip. |
| P-05 | P2 | A | P2 | Explicit mode help is missing; existing text does not literally invite passthrough. |
| P-06 | P2 → P1 at T1 | A | Same | Add union qualifications now; update unauthored result typing with T1. |
| P-07 | P2 | A | P2 | Do not turn the spelling rule into a ban on new capitalized names. |
| P-08 | P2 | A | P2 | Mapper targets need accurate declaration teaching; explicit types override nested `any`. |
| P-09 | P2 | A | P2 | Apply canonical-name advice only to actual row declarations. |
| P-10 | P2 | A | P2 | Type mapping is already taught; compatibility and usage details are missing. |
| P-11 | P2 | A | P2 | Final API wording depends on G3 and must preserve alias lookup semantics. |
| P-12 | P2 | A | P2 | Proposed absolute requirement omits the `fields: all` shield exception. |
| P-13 | P2 | A | P1 | Source option descriptions misstate existing name semantics. |
| P-14 | P2 | A | P2 | Structured output facts are missing; derive configured-instance facts. |
| P-15 | P2 | D | P2 | Correct future integration checklist, not current missing-plugin defects. |
| P-16 | P3 | A | P2 | Mode restriction prevents refusals; distinguish whole-batch failure from skips. |
| P-17 | P3 | A | P1 | Source-only restriction is false; runtime output-contract criticism is refuted. |
| P-18 | P3 | A | P2 | Named-alias summary fallback is real; “91 enum fields at HEAD” is unsupported. |

**2. Corrections, evidence, and replacement drafts**

Code paths below are relative to `src/elspeth/`. `PC` means `web/composer/skills/pipeline_composer.md`; `CAP` means the adjacent `pipeline_capabilities.md`; `AIDS` means `web/composer/planner_authoring_aids.py`; `GEN` means `web/composer/tools/generation.py`.

**S findings**

- **S-02:** PC:549–552 promises either lookup spelling. The actual comparison at `plugins/sources/field_normalization.py:484–549` normalizes non-identifier literals but preserves valid identifiers. Consequently:

  ```text
  row['Name']      / required_input_fields:[name]       validate=1
  row.name        / required_input_fields:[name]       validate=0
  row['Price USD']/ required_input_fields:[price_usd]  validate=0
  ```

  `templates.py:251–286` deliberately preserves aliases in the projected view. A production `QueryBuilder` render resolves `Name` under `[name]`; CLI execution under `[]` sends `alice`. **The binding decision already exists:** ADR-051(b) and RULINGS Q5 require both spellings of a declared field to resolve. Repair validation using actual upstream alias information; do not adopt normalized-only template teaching or request a new ruling.

- **S-03:** CAP:297–299’s visibility claim is true: `templates.py:191–195,263–264` exposes every field under `[]`. The defects concern operations on that view. CLI results were `row | tojson`: **validate 0/run 2**; `dict(row) | tojson`: **0/0**. Bare `{{ row }}` produces an object representation. **Correct disposition:** repair C2/G3, then document supported whole-row rendering. A permanent restricted-filter workaround would contradict the rulings.

- **S-04:** CAP:302–304 conflates accessing a field with calling it. The runtime grants `get` specially at `templates.py:322–335`; `row.keys()` under `[]` validates but fails during rendering. Correct draft: **“`get` is the only row method. Use filters or `dict(row)` to enumerate the view; do not call row fields or retired row APIs.”** This permits `row.get(...)` and does not accidentally ban methods on field values, such as `row.name.upper()`. G3 adds unconditional misuse refusal.

- **S-05:** PC:1236’s “never the transformed result” is contradicted by `value_transform.py:405–439,560–587`. Independent CLI results:

  ```text
  label:int, expression 1     validate=0 run=0
  label:int, expression 'x'   validate=0 run=2
  audit: reason=type_mismatch expected=int actual=str
  ```

  Correct draft: **“A created target’s schema type declares and checks its output. A field consumed and rewritten may constrain both input and output; declaring a type does not convert it.”** After T1, add the construction-time refusal for *provably* incompatible results.

- **S-06/S-07 — dependencies correctly identified:** The live registry reports no passthrough-capable batch plugin at this HEAD. Rank’s side branch adds one; merge 2 adds more. `value_transform.py:431–439` presently supplies `any` for untyped targets. Update these statements at their integrations. T1 must retain explicit authored `any`; it does not infer types from runtime values.

- **S-08:** Omitted LLM declarations produce an empty projection, but RAG adds `query_field` (`rag/core.py:168–177`). CLI `row | dictsort` with an omitted LLM declaration completed and sent `Score []`. Correct draft now distinguishes those cases; after G3, explicitly teach that omitted single-query LLM declarations plus whole-row reads are refused.

- **S-10 — P1, not P2:** PC:533–538’s universal normalization statement is already false. `field_normalization.py:223–226,374–402` uses headerless columns as written and applies mappings. Correct draft: **“Declare the canonical name rows carry: the normalized header, its mapping target, or the configured headerless column/mapping target.”** Codex-final repairs downstream alias enforcement; it does not introduce these source semantics.

- **S-11:** The table at PC:1231–1255 lacks dedicated repairs, but PC:545–547 already names the two spelling runtime reasons. Add targeted repairs without claiming total absence. Treat “Undeclared field” as a template failure subtype; E7’s `union_field_collision` remains conditional.

- **S-12/T-10 — P2, not P3:** This is missing semantic teaching, not cosmetic wording. `engine/executors/aggregation.py:646–692` records the failed batch; `engine/processor.py:1311–1349` routes its members or assigns failure/quarantine outcomes. Draft: **“A whole-batch failure applies the aggregation’s `on_error` policy to every buffered input row; discard records failure/quarantine outcomes.”**

- **S-13 — P1, not P3:** The guided step’s “every declared field must be interpolated” contradicts `llm/base.py:725–749`. The control with one unread declared field validates; a template reading no context row is refused. Correct draft: **“A nonempty declaration requires the template to read the context row; it need not interpolate every declared field separately.”** Also, guided prompts inherit CAP (`guided/prompts.py:93–95`), so they do receive its spelling/projection teaching. Retirement affects scheduling, not the truth of this finding.

- **S-14:** `web/composer/yaml_importer.py:35–40` rejects the policy knob; runtime defaults to `last_wins` (`core/config.py:1049–1053`). Thus **neither** E7 fail-policy behavior is ordinarily selectable by Composer. Explain its runtime reason for externally authored runs if useful; do not imply it is a current Composer authoring failure.

**T findings**

- **T-01:** The substantive contradiction is confirmed, but the claimed schema propagation is false. Live registry output:

  ```text
  upsert input-only phrase=True
  set_pipeline input-only phrase=False
  terminal input-only phrase=False
  ```

  `tools/transforms.py:278–281,1860` carries the bad sentence. `tools/sessions.py:2079` instead says “Plugin-specific node config”; the canonical terminal copies that schema. Use S-05’s input/output distinction. Do not report nonexistent duplicate text.

- **T-02:** AIDS:887–888 says “the whole source row.” Correct draft: **“`source_row` follows the node’s None/list/[] projection. Under a list, direct `source_row` reads must be declared there.”** Do not require every query binding to be duplicated in `required_input_fields`: `llm/base.py:932–959` also accepts `input_fields` values covered by image-input declarations, read from the full row in the parent.

- **T-03:** AIDS:846–851 starts **“A producer’s any-typed field…”** It omits an alternative; it does not literally assert that every value-transform output is `any`. P2 remains appropriate after T1. Draft: **“An explicit created-output declaration can enforce the intended type. Use conversion when values require conversion; retain compatible input declarations for consumed fields.”** Explicit output typing also applies beyond value-transform; do not categorically label all exploded values `any`.

- **T-05/T-09:** Use the carried-name draft under S-10. Mapping-target semantics already exist; codex-final fixes their enforcement. Source inspection returns raw evidence, while declarations name carried fields. Also, MCP does **not** advertise `inspect_source`, so that tool’s description cannot be treated as MCP’s available remedy.

- **T-08:** AIDS:416–474 lacks the union rule. `contracts/union_merge.py:220–272` treats `any` as a distinct type. Draft: **“Shared fields in a union need compatible output declarations. Convert values where necessary or write a type-changing result under a new name; changing a declaration alone does not convert data.”** “Declare the type on every last node” is unsafe when that declaration also constrains an incompatible input.

- **T-11 — P2, not P3:** AIDS:892–894 omits how to declare usage metadata. `llm/transform.py:1745` publishes its mapping-shaped value as `any`. Draft: **“If selecting or declaring the automatic usage field, use `any`.”**

Other T confirmations have direct supporting evidence: passthrough admission at `state.py:1799–1808` versus the minimal tool description at `tools/transforms.py:340` (**T-04**); set-result refusal at `value_transform.py:96–108` versus GEN:234–246 (**T-06**); and MCP’s instruction-free server at `composer_mcp/server.py:209–221,684` (**T-07**). Independent set/list/membership CLI controls returned **1/1**, **0/0**, and **0/0** respectively.

**R findings**

- **R-01:** GEN:1433–1443 retains the `batch_replicate` rule and says “Read the message,” although normal planner feedback removes that message. Draft: **“Passthrough requires a batch plugin declaring exactly one output per buffered row; otherwise use transform.”** Do not point to a catalogue capability until T-04 actually publishes it.

- **R-02:** `llm/base.py:632–637` incorrectly offers `[]` for row-API misuse. Separate that misuse from legitimate computed-key/whole-row opt-out. G3 owns unconditional checks; C2 separately owns `tojson`. Plain `row.contract` can currently read an actual data field, so “every such form always fails” is too broad.

- **R-04:** Codex-final resolves names through mappings, making normalization-only GEN:1818–1831 insufficient. However, **“declare the name the refusal gives” references text withheld from this planner path**; `missing_fields` contains rejected literals, not resolved replacements. Draft: **“Resolve the source’s carried name using its normalization/columns and field_mapping options, then patch the declaration.”** Alternatively publish an approved structured canonical-name fact.

- **R-05:** Split the claims. Untyped value-transform results becoming derived types is correctly a T1 dependency. But type-coerce already derives its **runtime emitted contract** at `type_coerce.py:498–535`:

  ```text
  CONVERTER_RUNTIME_TYPE int
  CONVERTER_BUILD_STAMP {}
  ```

  The pending work concerns build-time publication. Draft: **“Declare the converter’s input using the arriving type; `conversions[].to` determines its output type; downstream consumers declare that resulting type.”**

- **R-06:** “All observed” repairs schema-mode homogeneity; it does not promise to resolve every independent conflict. Keep that valid option and add: **“A certain shared-field type conflict still requires compatible actual outputs, conversion, or distinct names.”** Controlled int/int union contracts accepted; int/any conflicted.

- **R-07 — P1:** GEN:950–952 excludes fields the producer computes from output declarations. That directly contradicts the enforced target declaration above. Draft: **“Align actual output types on participating branches. Created targets may be operator-typed; use conversion or a separate target when an input/output rewrite cannot satisfy one declaration.”**

- **R-08:** The missing entry is confirmed: `state.py:6672–6685` emits the code, but exact explain lookup returns no record. The proposed **“Never declare the post-conversion type on the consumer” is wrong**. Draft: **“Declare the converted type on the downstream consumer; the converter’s own input schema describes the arriving type.”** Exact conflicting field/type facts are also lost by current feedback projection; a static explanation alone does not recover them.

- **R-09:** “`[]` is needed only for a computed key” contradicts legitimate intentional whole-row use. Draft: **“`[]` exposes the whole row and permits computed keys; field-scoped controls cannot prove coverage, and unsupported row APIs remain unsupported.”**

- **R-10:** Remove `extract_jinja2_fields()` as planner advice. Preserve intentional whole-row use and the `fields: all` shield exception (`plugin_policy/validation.py:237–242`). This correction needs no unlanded work.

- **R-11:** Explicit declaration is the better RAG repair, but `[]` genuinely permits an ordinary static field read. Do not attribute the separate `tojson` defect to this refusal branch. Draft: **“Declare additional query-template fields, provided upstream supplies them; query_field is already visible.”**

- **R-14:** Exact lookups confirmed the missing explanations. However, **“Undeclared field” is human text inside `template_rendering_failed`**, not an emitted validation code. Teach the existing runtime reason/subtype rather than inventing a code with spaces. Add E7’s explanation only with its implementation and authoring-scope qualification.

The unchanged **R-03** finding is supported by `llm/base.py:849–865`, `rag/core.py:234–250`, and the CLI `keys()` failure. **R-12** is supported by the proposed escape at `llm/base.py:895–902` immediately meeting the declaration check at :932–944. **R-13** contrasts GEN:1428–1430 with capability admission at `state.py:1799–1808`.

R-15 needs individual dispositions:

| Subitem | Verdict / priority | Correction |
|---|---|---|
| B-16 | A / P1 for source-row claim | `llm/base.py:155–160` conflates full-row input binding with projected `source_row`. `multi_query.py:256–259` implements different paths. Missing input bindings fail during context construction; undeclared source-row reads fail during rendering even when the original row carries the field. |
| B-17 | A / P3 | Header alias resolution is deliberate, not accidental (`templates.py:251–271`), but coordinate wording with S-02’s static-check repair. |
| B-18 | C / P3 | Two messages exist at `state.py:5403–5419` and `union_merge.py:276–289`. Align advice without fabricating unavailable provenance facts. |
| B-19 | A / P2 | Conversion advice at `schema_validation.py:598–599` needs the every-row input prerequisite; do not imply that this particular edge proves sparse data exists. |
| B-20 | No supplied finding | The review contains no B-20 entry. |
| B-21 | C / P3 | Internal `TemplateRow` name reproduced in failure text; final remedy depends on G3. |
| B-22 | C / P3 | GEN:3736 references nonexistent “rule 10.” Replace with a self-contained remedy or actual section name. |
| B-23 | A / P2 conditional | Adding correlation to the name list is insufficient: GEN:3244–3246 reads only `value_field`; planned correlation uses `x_field` and `y_field`. Inspect both numeric inputs or derive the capability properly. |

**P findings**

- **P-01/P-02 — P2, not P1:** Existing descriptions omit projection teaching but do not expressly promise undeclared visibility. Their proposed replacements are inaccurate for RAG. `rag/core.py:168–177` always adds `query_field`; :253–260 rejects extra declarations only when the template never reads context `row`, not whenever one declared field is unused. Correct draft: **“RAG row contains query_field plus declared fields; [] exposes the whole row. Declare additional fields read by the template.”** For LLMs, omission gives an empty source-row view.

- **P-03 — P2:** The hint accurately describes conversion failures, but omits the required-input prerequisite. See the separate defect verdict below. Draft: **“Every conversion field is required. Missing required input stops the run; on_error handles conversion failures after those inputs are present.”**

- **P-04 — dependency correct:** Rank is absent at HEAD. Its side-tip “only” statement is then true; merge 2 invalidates it. Replace inventory claims with **“emits one row per buffered row; supports passthrough and transform.”**

- **P-05:** `batch_outlier_annotator.py:187–188` says it preserves **valid** rows; :225 explicitly permits drops, and its example uses transform. This is missing mode teaching, not a literal passthrough recommendation. Draft: **“Use transform; skipped invalid rows prevent the required 1:1 passthrough guarantee.”**

- **P-06:** Current untyped-`any` teaching matches `value_transform.py:431–440`. Add the union restriction with its actual guaranteed-presence/known-type conditions. After T1, distinguish derived unauthored types from explicit authored `any`.

- **P-07/P-08:** Blanket normalized-only target rules exceed the ruling. A new `target: Total` independently **validated and ran with exit 0**. Normalization-fixed-point requirements for new operator-created names were explicitly deferred. Draft: **“Use the canonical name when addressing an existing field; do not create a second spelling of it. New targets may use valid new names.”** For nested mapper extraction, say **untyped** results are `any`; an explicit target declaration takes precedence.

- **P-09:** Apply the suffix to proven row declaration options, not every field-looking option. For example, Azure search index-field options name the index, not pipeline columns. Draft: **“Use the name carried by upstream rows—normally the normalized header, or its field_mapping target.”**

- **P-10:** `llm/transform.py:2043` already teaches the complete bound-type mapping and numeric coercions. Missing additions are override compatibility and usage typing. Draft: **“An authored declaration must admit the bound output type; number cannot narrow to int. Declare usage mappings as any.”** Exact equality is not required.

- **P-11:** Final row-API refusal language depends on G3. Teach fields plus `get`, filter/builtin enumeration, and bracket access for reserved-name columns. Do not turn canonical declaration advice into a prohibition of the header lookups required by S-02’s ruling.

- **P-12:** The absolute “must list fields” replacement omits `fields: all`, accepted at `coverage.py:180–195`. Draft: **“A field-scoped shield requires provable declared coverage; a dominating all-fields shield can cover an otherwise unprovable scope.”**

- **P-13 — P1:** `config_base.py:560,564` misstates existing behavior. Draft: **“columns supplies headerless names as written. field_mapping maps normalized source names—or headerless columns as written—to carried output names.”** CSV’s `{normalized: Original}` example is consistent with implementation, not itself reversed.

- **P-14:** The systematic structured metadata gap is confirmed by live catalogue payloads. But some types already appear in prose, and `created_output_fields()` describes a **configured instance**, including prefixes and selected outputs. Publish effective names/types/presence/nullability and operator overrides; ensure the planner projection carries them.

- **P-15 — dependency correct:** The six plugins are absent at HEAD and explicitly belong to merge 2. Treat this as an integration checklist. Verify actual options and semantics when implementations land.

- **P-16 — P2:** Transform-only admission prevents actual configuration refusals. Draft: **“Use transform. Whole-batch failure applies on_error to buffered inputs; discard records failure/quarantine outcomes.”** Do not equate intentional member skips with whole-batch failure.

- **P-17 — P1 for the first sentence:** `type_coerce.py:545` says to use it **only** after an upstream transform. Observed sources can require conversion too; the source-only CLI control converted `"2"` to `2` successfully. The second criticism is **refuted**: :498–535 already derives runtime output contracts. Build-time publication is the T1 dependency.

- **P-18:** The reported **91** counts object-typed summaries, not 91 enum defects. The HEAD scan found no corresponding top-level enum references; rank is on the side branch. A controlled named-Literal-alias/inline-Literal/dict probe produced **object/string/object**, confirming the fallback at `catalog/service.py:568–583`. Rank’s named aliases exercise it after X1. Correct the scope and preserve enum metadata; do not classify every object field as broken.

**3. Missed findings**

| ID | Surface / current text | Priority | Problem and recommended wording | Dependency / evidence |
|---|---|---:|---|---|
| M-01 | CAP:292–295: computed keys “are refused” | P1 | Add the intentional `[]` exception; distinguish it from unsupported row APIs. | None. `llm/base.py:609–610`; computed-key `[]` CLI **0/0**, list control validation **1**. G3 preserves this exception. |
| M-02 | CAP:283–287: LLM schema type “must be that type” | P1 | Say **“must admit the bound type”**; integer under float and explicit any are valid. | None. `llm/base.py:532–578`; integer→float/int/any validate **0**, number→int **1**, number→float **0**. |
| M-03 | AIDS:457–458: mapper schema names inputs, “not the renamed output names” | P1 | Explain inherited source declarations and explicit target-only output declarations; neither converts values. | None. `field_mapper.py:526–544,619–633`; target `given:str` CLI **0/0**, `given:int` **0/2**, audited operator-declared output violation. |
| M-04 | `field_mapper.py:310` raw class docstring: strict=False tolerates missing sources | P1 | Align with the updated option description: normalized configured sources are required during pipeline execution regardless of strict. | None; raw schema/MCP surface. Missing mapped input with strict=False validates **0**, runs **4** with `DeclaredRequiredInputFieldsViolation`. |
| M-05 | GEN:1488–1493; `llm/base.py:842–847`; `rag/core.py:212–215`: rewrite bare variable to row field | P2 | Add the declaration requirement; RAG’s query field is already available. | None. Following the remedy changes an unbound-name refusal into a missing-declaration refusal. |
| M-06 | GEN:1076: observed schema “infer[s] types” in generic plugin repair | P1 | Scope inference to sources. Transform-created fields use declarations, with unknown results represented as any. | None; ADR-050 and `value_transform.py:431–439,585–587`. R-09 only addresses another clause on this line. |
| M-07 | `value_transform.py:574–581`: remedy says declare target any | P2 | Qualify against downstream and union contracts; any is not a unifying wildcard. | None. `union_merge.py:265–272`; controlled int/any conflict. T1 also requires correct operator/plugin attribution. |
| M-08 | Current and proposed output-type guidance says mismatches route at runtime | P2 conditional | Teach construction/bind refusal when all provable successful result types contradict the authored type; residual value-dependent violations still route. | T1, `specialist2-arbiter.md:322–327` D5. Not implemented at this HEAD; no future error code invented. |

**4. Code defects and real-run evidence**

Commands used the requested worktree imports and absolute settings paths:

```bash
cd <repo>/.claude/worktrees/codex-review-s1a
export PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src
.venv/bin/python -m elspeth.cli validate --settings <absolute-scratch-settings>
.venv/bin/python -m elspeth.cli run --settings <absolute-scratch-settings> --execute
```

Both import roots were printed and resolved inside this checkout. Individual logs contain complete commands and explicit exit codes.

- **S-02 — CONFIRMED code defect, narrower than reported.** `Name` under `[name]` is refused while canonical `name` and actual spaced-header `Price USD` controls are admitted. Production projected rendering resolves the `Name` alias; CLI under `[]` completes and captures user content `"alice"`. This contradicts the already-decided lookup contract. [Execution evidence]( <repo>/.claude/lanes/5887-batch-row/codex/scratch6/skills/execution-evidence.txt).

- **P-03 — abort CONFIRMED; code-defect classification REFUTED.** The missing canonical conversion field validates **0** and runs **4**:

  ```text
  DeclaredRequiredInputFieldsViolation:
  declared required input fields ['b'] ... only exposed ['a']; missing ['b']
  ```

  Present-field control runs **0**; uncoercible present value routes through `on_error`, run **2**. `type_coerce.py:245–261` deliberately declares conversion inputs; `declared_required_fields.py:62–88` enforces them before conversion. Q4’s routed residual concerns **header spelling**, as specified in `specialist-q4-doctrine.md:112–126`, not an absent canonical prerequisite. [Missing-input run]( <repo>/.claude/lanes/5887-batch-row/codex/scratch6/plugins/missing/run.log).

- **C2/S-03 — CONFIRMED implementation defect.** Direct `row | tojson` validates **0**, runs **2**, and routes both rows with `template_rendering_failed`/TypeError. `dict(row) | tojson` validates/runs **0/0**. The binding C2 decision requires repairing this, not narrowing the documented whole-row API.

- **G3 row-method handling — CONFIRMED implementation gap.** `row.keys()` under `[]` validates **0**, runs **2**. Configuration should refuse unsupported calls unconditionally as decided.

- **Known C3 value leakage — CONFIRMED, still pending.** The real failed-conversion run records the supplied value inside the error message:

  ```text
  "'oops' is not a valid integer string"
  ```

  This is `error_details_json.message`, not legitimate row payload storage. The generator is `type_coerce.py:85`; routing is :415–424. This confirms the known C3 example, **not completion of its required class-wide sweep**.

For LLM CLI execution, local sockets were unavailable. A disclosed scratch-only stub replaced **only `httpx.Client.send`**, captured rendered requests, and supplied a fixed provider response. ELSPETH validation, projection, rendering, parsing, execution, and auditing remained unchanged. These runs prove engine behavior, not live-provider availability.

The focused existing guidance-catalogue test module completed with **29 passed, exit 0**. No full suite ran.

**5. Surface-map corrections**

1. **Plugin-schema prose does not universally reach the proposal planner.** `pipeline_planner.py:3323–3336` projects `get_plugin_schema` into `PlannerPluginContract`. AIDS:1230–1300 retains schemas and composer hints, but :1363 strips JSON-schema prose and :1432 strips knob descriptions. Live measurements:

   ```text
   type_coerce: raw descriptions 1 → projected 0; hints retained 5
   llm:         raw descriptions 4 → projected 0; hints retained 30
   rag:         raw descriptions 1 → projected 0; hints retained 4
   field_mapper:raw descriptions 1 → projected 0; hints retained 21
   ```

   Therefore **Field-description-only fixes do not repair this planner path**. Raw MCP/list responses are separate surfaces.

2. **Selected-schema evidence is a separate guidance carrier.** `pipeline_planner.py:3833–3839,3889` rehydrates `schema_contract_evidence`; rejection-triggered selected contracts also enter repair feedback. The map should include both, their projection, and budgets.

3. **The discovery digest is not the full catalogue.** AIDS:1754–1768 carries name, purpose, required-option names, prohibition text, and tags. Detailed hints travel with selected contracts; examples and full option descriptions do not automatically enter that digest.

4. **Tool-schema identity was overstated.** Live registry: **42 tools**; MCP roster: **32**. `set_pipeline` and terminal options text differ from `upsert_node`. MCP exposes neither `emit_pipeline_proposal` nor `inspect_source`.

5. **Repair feedback retains more than the review states.** `pipeline_planner.py:2696–2710` can retain detail for:
   - `plugin_options_invalid`;
   - `gate_condition_ignores_stated_threshold`;
   - `passthrough_cannot_produce_declared_fields`.

   Config-owned errors can suppress even those details. Approved structural facts also survive separately at :2740–2804. Thus “the explain record is the only remedy for every other code” is too broad.

6. **Plugin refusal transport is qualified, not universally verbatim.** `_prevalidate_plugin_options` wraps and sometimes filters/revalidates deferred values; ownership masking can subsequently remove details. R-01’s particular lost-message problem remains confirmed.

7. **Additional instruction surfaces were omitted:** canonical and reduced terminal instructions (`pipeline_planner.py:1894–1904,3894`), prose/decline/escape notices (:1907–1945), discovery/repair guards, and the direct tool loop’s catalogue/state context messages (`prompts.py:204–261,607–628`).

8. **Tutorial profile does more than restrict the palette.** It also affects unavailable-information declarations and provider state/result projections. These shared restricted-surface mechanisms are not evidence of tutorial-special pipeline authoring, but the map’s “only” claim is inaccurate.

9. **MCP’s missing skill instructions is confirmed.** `Server("elspeth-composer")` supplies no instructions; shared tool/hint teaching is therefore important. Any remedy should publish facts and constraints while preserving planner authorship.

10. **Deployment overlays remain unverified.** The shipped prompt loading path is confirmed; no external overlay actually served by a deployment was inspected.
