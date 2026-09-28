# Composer guidance landing: live work log

## Custody

- Target: `release/0.8.1`; branch: `docs/composer-guidance-landing`; worktree: `<repo>/.claude/worktrees/composer-guidance-landing`.
- Cut from release commit `9c2e8b17c984264be618a06de33d8d7d8956be69` on 2026-09-29.
- Main checkout is dirty with unrelated AGENTS/docs changes; do not edit it before the final fast-forward.
- Both `elspeth` and `elspeth_lints` imports resolve to this worktree with the two-root `PYTHONPATH`.
- Source inputs: tracked `prompt.md`, `guidance-review.md`, `astra-verification.md`, `slotting.md` § SLOTTING, and `rulings.md` (all beside this log). Astra's corrected drafts and newer rulings control.
- Confirmed release ancestry: T1/S-02 merge `ce3dcc6b3`, S-02 carrier correction `ac30f001f`, field_mapper strict retirement `286be0b92`, and row-items teaching `5b70950f6` are all ancestors of this branch.

## Triage method

Read current text and record its file:line for every in-scope item. DONE means current release already teaches the measured rule; APPLY means a still-wrong surface takes the corrected draft; REWRITE means the review draft conflicts with since-landed behavior; DROP means the named surface no longer exists or the finding is outside this unit. Behavioral claims require an absolute-settings `elspeth validate` and, where runtime matters, `elspeth run --execute` before editing. No guidance source has been edited yet.

## Triage table

| ID | Current release surface and text | Verdict | Evidence / intended wording |
|---|---|---|---|
| S-01 | `pipeline_composer.md:525-531` still says to use the literal observed header | APPLY | Say inspection shows raw headers; declarations use carried names. |
| S-02 | `pipeline_composer.md:555-563` already qualifies aliases by a source header that can reach the node | DONE | CSV header positive validates and runs; headerless negative refuses. See measurements. |
| S-03 | `pipeline_capabilities.md:300-317` teaches whole-row filters and rejects row calls | DONE | Current text covers `row | tojson`; CLI runtime control still needed. |
| S-04 | `pipeline_capabilities.md:305-315` says `get` is the only method and lists safe enumeration | DONE | G3 text landed. |
| S-05 | `pipeline_composer.md:1247` separates type_coerce arrival from value_transform target output | DONE | T1 text landed. |
| S-06 | `pipeline_capabilities.md:136-141` says batch_rank is the one eligible plugin | APPLY | Replace inventory/name count with declared 1:1 capability and catalogue fact. |
| S-07 | `pipeline_capabilities.md:333-340` says an undeclared computed expression is `any` | REWRITE | T1 derives provable result types; retain `any` only for unresolved results. |
| S-08 | `pipeline_capabilities.md:291-317` names RAG query field and refuses omitted whole-row LLM reads | DONE | Current text incorporates G3. |
| S-09 | `pipeline_capabilities.md:300-302` covers `[]` but not omitted declaration's shield effect | APPLY | Add none/`[]` unprovable rule and all-fields exception on planner-visible surfaces. |
| S-10 | `pipeline_composer.md:533-544` says every source normalizes headers, then separately qualifies headerless columns | REWRITE | State carried name directly, including mapping targets and headerless names as written. |
| S-11 | `pipeline_composer.md:1240-1268` has no `union_field_collision` row | APPLY | Scope here is the union reason only: externally authored runtime policy; Composer cannot author the knob. R-08 owns edge repair. |
| S-12 | `pipeline_capabilities.md:132-141` omits failed-batch disposition | APPLY | Teach whole-batch on_error and discard outcomes. |
| S-13 | `src/elspeth/web/composer/guided/skills/step_3_transforms.md` absent | DROP | Guided skill surface was retired from current release. |
| S-14 | `pipeline_capabilities.md:145-159` exposes only authorable coalesce fields; no collision-policy knob | DROP | Note only; Composer cannot author the policy. R-14/S-11 cover the runtime reason. |
| T-01 | `tools/transforms.py:278-281,1859-1861` says schema never types created results | APPLY | Distinguish consumed-field input types from created-target output types. |
| T-02 | `planner_authoring_aids.py:886-889`; `llm/base.py:200-202` lacks projection rule | APPLY | Teach projected source_row and image-input exception. |
| T-03 | `planner_authoring_aids.py:846-851` groups value_transform with any-typed outputs | REWRITE | T1-derived types and authored target type alternative; conversion for actual value change. |
| T-04 | `tools/transforms.py:337-340` has minimal output_mode text; catalogue summary lacks mode fact | APPLY | Publish class-derived capability and include in all planner projections. |
| T-05 | `tools/sources.py` inspect_source and sink tool descriptions lack carried-name distinction | APPLY | Raw observed header vs carried declaration, including mapping/headerless. |
| T-06 | `tools/generation.py` expression grammar and `reference_join` output description lack complete set/type rule | APPLY | Teach stored-set refusal and T1 result typing. |
| T-07 | MCP has no skill instructions; shared descriptions/hints lack several rules | APPLY | Put concise teaching on actual MCP tool/hint surfaces; do not mention unavailable tools. |
| T-08 | `planner_authoring_aids.py:416-474` lacks union type compatibility | APPLY | Explain compatible actual outputs; authored declarations do not convert values. |
| T-09 | `planner_authoring_aids.py:457-458,902-908` omits carried-name rule | APPLY | Fix mapper input/output distinction and sink names. |
| T-10 | `tools/transforms.py:266-273` omits whole-batch on_error | APPLY | Add same aggregation rule as S-12. |
| T-11 | `planner_authoring_aids.py:892-894` omits usage type | APPLY | Automatic usage field, when declared, is `any`. |
| R-01 | `tools/generation.py:1243-1253` still names batch_replicate as the third placement | APPLY | Explain class-declared 1:1 passthrough rule; message is withheld in repair feedback. |
| R-02 | `llm/base.py:627-636` separates misuse from computed keys; `core/templates.py:175-189` gives safe replacements | DONE | Row-API misuse no longer recommends `[]`; computed key may. |
| R-03 | `core/templates.py:175-189`; LLM/RAG call misuse validators run before undeclared checks | DONE | `row.keys()` is explained as a row-field call; source parity tests still needed. |
| R-04 | `tools/generation.py:1599-1613` says rewrite missing names to normalized form | APPLY | Correct for field_mapping target; use Astra's source-carried-name draft, not withheld message. |
| R-05 | `tools/generation.py:923-949` says untyped value_transform output is any; `type_coerce.py:595-600` now publishes conversion output | REWRITE | T1-derived expression typing; distinguish converter input from output. |
| R-06 | `tools/generation.py:916-920` offers all-observed without qualification | APPLY | Retain mode remedy, add that certain shared-field conflicts need actual compatible output. |
| R-07 | `tools/generation.py:923-949` gives long union repair that still says untyped target always any | REWRITE | Align with T1 and actual rewritten-output semantics; no declaration-only conversion claim. |
| R-08 | `state.py` emits `edge_field_type_incompatible`; `tools/generation.py` has no exact guidance record | APPLY | Add direct record: converter input arrives with old type, downstream consumes converted type. |
| R-09 | `tools/generation.py:1067-1071` still says `[]` merely opts out | APPLY | Explain whole-row use and shield coverage limit. |
| R-10 | `llm/base.py:710-720` offers `[]` as generic runtime risk and asks planner to call Python extractor | APPLY | Offer list first; `[]` for intended whole row/computed key, with shield qualification; remove Python-API advice. |
| R-11 | `rag/core.py:267-277` offers `[]` for a static undeclared read | APPLY | Prefer explicit declaration; preserve real whole-row/computed-key exception elsewhere. |
| R-12 | `llm/base.py:899-911` and `state.py:4243-4253` suggest source_row column reads without saying they need declaration | APPLY | Say direct source_row reads belong in required_input_fields. |
| R-13 | `tools/generation.py:1238-1240` offers passthrough without the capability condition | APPLY | Describe 1:1 admission and transform default. |
| R-14 | `tools/generation.py` has no `union_field_collision` explanation | APPLY | Scope is this runtime reason only; do not invent a Composer validation code or authorable policy. |
| P-01 | `config_base.py:755-762` points to Python extractor and omits template projection | APPLY | Add planner-visible hint as well as schema description, since descriptions are stripped. |
| P-02 | `rag/core.py:89-93`, `rag/transform.py:211-215`, `azure/ai_search.py:182-187` omit query-field projection | APPLY | RAG row contains query field plus declared fields; use live hints. |
| P-03 | `type_coerce.py:588-599` says on_error catches uncoercible rows but omits missing-input routing | REWRITE | Live validate exit 0 and run exit 1: one row delivered, one routed to q as missing_field. Teach R2's route, not Astra's stale abort draft. |
| P-04 | `batch_rank.py:142-145,178-181` calls itself the only eligible batch plugin | APPLY | Describe this class's 1:1 output capability, no inventory claim. |
| P-05 | `batch_outlier_annotator.py:186-192,221-225` omits transform-only mode | APPLY | Tell planner skipped rows break passthrough guarantee. |
| P-06 | `value_transform.py:699-707` has T1 typing but omits union compatibility | APPLY | Add any-is-a-type qualification; retain authored type and derived result split. |
| P-07 | `value_transform.py:691-708` has no target spelling hint | APPLY | Existing field uses carried name; new valid target may have its own name. |
| P-08 | `field_mapper.py:167-173` describes mapping without target declaration/type detail | APPLY | Keys locate inputs; values create output names; typed target overrides nested any. |
| P-09 | `batch_stats.py` value_field and analogous row-column options lack carried-name advice | APPLY | Apply only to verified row declarations, not Azure index field options. |
| P-10 | `llm/transform.py:2056-2060` teaches bound types but omits compatible authored override and usage typing | APPLY | Number is float; authored type must admit bound type; usage is any. |
| P-11 | `llm/transform.py:2042` teaches only row interpolation | APPLY | Add row fields/get, enumeration and carrier-qualified aliases in visible hint. |
| P-12 | `azure/prompt_shield.py:150-155` and `aws/bedrock_prompt_shield.py:72-76` omit provable coverage | APPLY | Field-scoped coverage needs declarations; all-fields exception. |
| P-13 | `config_base.py:558-565` says columns normalized and mapping keys are raw observed names | APPLY | Columns as written; mapping key is normalized/headerless name, value is carried name. |
| P-14 | `web/catalog/service.py:376-418` emits no configured created-output facts | APPLY | Small class-derived fact if feasible; project effective names/types/presence/nullability and overrides. |
| P-16 | Batch reducer assistance lacks transform-only / failed-batch rule | APPLY | Add shared accurate hint to affected reducers. |
| P-17 | `type_coerce.py:588-600` says use only after upstream transform; T1 output text is now true | APPLY | Fix first sentence; retain conversion-output rule. |
| P-18 | `web/catalog/service.py:559-583` reduces named Literal ref to object in summaries | REWRITE | Scope is named enum metadata, not every object field; determine planner-visible guidance within this package's code limit. |
| M-01 | `pipeline_capabilities.md:294-302` says computed keys refused before noting `[]` whole-row view | APPLY | Make the deliberate `[]` exception explicit and its shield cost. |
| M-02 | `pipeline_capabilities.md:281-289` says LLM declaration must be exactly bound type | APPLY | Say declaration must admit bound type; int can satisfy float, any accepts. |
| M-03 | `planner_authoring_aids.py:457-458` says mapper schema names input only | APPLY | Inherited input and explicit created target output declarations, no conversion. |
| M-04 | `field_mapper.py:176-192,323-324` refuses retired strict and says missing mapping source routes | DONE | Strict option gone; live P-03 route confirms class rule generally, mapper-specific test still needed. |
| M-05 | `llm/base.py:849-856`, `rag/core.py:239-245` bare-name remedy omits declaration | APPLY | Add declaration requirement; RAG query field is already available. |
| M-06 | `tools/generation.py:1067-1071` says observed schema infers types generically | APPLY | Scope that inference to sources; created fields follow declarations/T1. |
| M-07 | `value_transform.py:699-703` offers any for changing declared type without union qualification | APPLY | Any is not a union wildcard; new target/conversion may be needed. |
| M-08 | `value_transform.py:699-703` says all type mismatches route at runtime | REWRITE | T1 rejects a provably incompatible authored output at build; residual value-dependent mismatch routes. |
| B-16 | `llm/base.py:134-140` conflates missing query binding with source_row render miss | REWRITE | Split context-construction miss from projected source_row undeclared read; account for R2 routing. |
| B-17 | `llm/base.py:58-65` now avoids accidental alias wording | DONE | Carrier rule is already in skill S-02. |
| B-18 | `state.py` and `union_merge.py` retain two union conflict texts | APPLY | Align the two remedies without inventing unavailable facts. |
| B-19 | `core/dag/schema_validation.py:595-609` suggests type_coerce without every-row input prerequisite | APPLY | Add prerequisite without claiming this edge proves sparsity. |
| B-21 | `core/templates.py:175-189` refuses row-API calls before render | DONE | Internal TemplateRow runtime error no longer guides an admitted row call. |
| B-22 | `tools/generation.py:3521-3525` points to nonexistent rule 10 | APPLY | Self-contained headerless CSV repair. |

Out of scope: P-15 and B-23 (merge 2), B-20 (no supplied finding), and all other IDs not selected by SLOTTING.

## Measurements and gate results

- P-03, current branch `ab0f1ded5`: `elspeth.cli validate --settings /tmp/composer-guidance-landing-probes/p03-missing/settings.yaml` exit 0. `elspeth.cli run --settings /tmp/composer-guidance-landing-probes/p03-missing/settings.yaml --execute` exit 1 (partial run); output says `2 rows processed | 1 succeeded | 1 failed | 1 routed (q:1)`; `q.jsonl` and `out.jsonl` each have one row. Config uses observed JSONL rows `{"a":"1","b":"2"}` and `{"a":"3"}` with type_coerce converting `b` to int. Paths are absolute; both source roots were on PYTHONPATH.
- S-02, current branch `ab0f1ded5`: CSV header `Name` with declared `name` and template `row['Name']` validates exit 0 at `/tmp/composer-guidance-landing-probes/s02-carrier/positive.yaml`. `run --execute` exit 0 against a localhost fixed-response OpenRouter-compatible endpoint; one row succeeded and captured provider request user content `Alice`. Headerless CSV `columns: [name]` with the same template at `/tmp/composer-guidance-landing-probes/s02-carrier/negative.yaml` validates exit 1 with `Unreachable header spelling` and says to read `row['name']`. An initial JSON control also validated; it was not a no-carrier case and was superseded by the headerless control. The first positive runtime attempt under sandbox exited 4 at provider preflight because local sockets were blocked; the scoped localhost run completed.
- Gate results: pending.
