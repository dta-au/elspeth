# Queue for the related-bugs workflow (after engine-r2 + E2 integrate)
- AC-R1 missing schema-required field at a batch node (raw KeyError abort)
- AC-R2 ContractMergeError on row-to-row observed type variance (transform.py:684 -> union_merge.py:274)
- AC-R3 value_transform non-scalar result TypeError (value_transform.py:487/:63) — contract typing
- AC-R4 collector exception arm leaves member holds OPEN; batch plugin under transforms: / report_assemble as collector pass validate then abort
- B9 json_explode stale docstrings (+ manifest re-pin if its hash is node material)
- PLUGINS-F1 paired_preference overflow value scrub unpinned (review-PLUGINS-r1.md Finding 1)
- PLUGINS-F2 schema_validation.py + contract surfaces left false (review-PLUGINS-r1.md Finding 2)
- RAG-F1 shared template-error renderer: Jinja exception text leaks row values (rag/query.py:148-155 and llm/templates.py)
- 1E propagation docs (plugin-protocol.md, data-trust guide, PLUGIN.md, transforms/AGENTS.md, tier-model-deep-dive skill, README doctrine) + G8 regression gate pytest
- mis-attributed elspeth-35d03f1f28 comment in test_aggregation_recovery.py (impl-engine-r2 E1 note)
- NEW EXAMPLE (John, 2026-09-23): after the engine upgrades land, add an examples/<name>/ that demonstrates the behaviour
  (aggregation on_error to a named quarantine sink receiving the whole failed batch with a value-free reason; discard
  counted as quarantined; the good batches still producing output). Follow examples/AGENTS.md conventions; must be
  runnable and verified by a real `elspeth run --execute`; add to the examples partition in
  tests/e2e/examples/test_shipped_examples.py if that is the convention; README explains what to look at in the audit.
- MINORS SWEEP from round 3 reviews: C4 docstring names nonexistent BatchRepository.recorded_failure_verdict; C4 mutation
  survivors (batch_lineage.py:144 transform_id==aggregation_node_id clause; restore_read_model.py:1400 two untested
  corruption raises; execution_repository.py:1330 _require_live_unterminated_members_on; processor.py:4671 redundant
  exact-BLOCKED raise — delete if truly redundant, else pin); C5 non_canonical_output.py docstring (3 callers); C2: 3 sources
  (azure_blob_source:1246, llm/source:430, text_source:283) renderer call unpinned, no-fields-schema survivor at 5 sites,
  collector-seam redaction test not a full Landscape scan; C3: MCP 'attempt evidence' label only in docstrings.
- C2 lints: +4 R5 isinstance in contracts/safe_validation_errors.py (alias handling) — adjudicate: honest Tier-3 parse of
  pydantic error objects → stage key-free for signing, or restructure; record decision.
- CODEX-R2 (codex/codex-review-engine-r2.md, evidence codex/scratch2/verdict + counts) — owned by R4 if it is running the
  collector-holds work, else a following unit R9. RIGHT FIX (lane owner): a collector group failure writes its verdict
  through the SAME one-transaction, leader-fenced verdict writer as aggregations (ExecutionRepository.complete_aggregation_failure
  generalised, not copied): per-member transform_errors (fixes the counts minor), node_state FAILED + scrubbed reason, group
  failure, member holds closed — atomically; resume completes a RECORDED collector failure verdict WITHOUT re-invoking the
  plugin (principle of the C4 ruling: crash timing must not change the outcome). Acceptance: Codex's
  test_collector_shipped_overflow.py (crash after first failed hold → both resumes deliver terminals) and the
  nondeterministic collector-challenge matrix (6 failing windows) all pass; collector failures appear in error totals and
  failure categories via terminal outcomes.
  + MINOR state_guard.py:508: after a committed verdict whose call raised before returning, cleanup must not report a
  permanent OPEN state — re-read the durable state and report truthfully.
- CODEX-S1a (codex/codex-s1a.log ~line 8494; the Codex session was cut off by OpenAI's cyber-safety filter before writing
  its report): CONFIRMED by the lane owner at a760fa19e with a real CLI run
  (scratch/confirm-codex-hdr-src/): field_mapper mapping {Name: given}, schema flexible fields ['Name: int?'] (declaration
  spelled by the SOURCE's ORIGINAL HEADER), CSV Name=Ann/Bob → exit 0, delivered {"given":"Ann"}, and the node output
  contract records given: int nullable source=declared. A Tier-1 record making a false claim — ruled in-lane class
  (declared-type-not-enforced). S1a's residual "header-spelled source split — no contract at construction time" is the gap:
  resolve source spellings at RUNTIME against the arriving row's contract (original_name↔normalized_name) so the input
  check / output check enforces the declaration whatever its spelling. Then re-run Codex on S1a with a REWORDED brief
  (no "attack"/"disprove"/"adversarial" wording; keep the substance) — the previous brief tripped the safety filter.
- SPELLING-RULE unit (RULINGS 2026-09-25 "field-name spelling rule"; evidence systems-spelling-sweep.md §2.1-2.8 and
  scratch/systems-spelling/, plus the CODEX-S1a repro): implement the canonical-name rule at every declaration surface,
  delete the field_mapper spelling patches it makes dead, fix corpus/examples/composer teaching, CLI regression for every
  confirmed site (field_mapper false claim, value_transform inert, json sink inert (+ all 9 sinks), required_input_fields
  false rejection, rag/web_scrape/blob siblings) and the PLAUSIBLE ones (rag build() raw dict; group_by raw key) verified
  or refuted. Then Codex re-review of S1a + this unit with a REWORDED brief.
- EXTRACTOR-ALIGN (from S0-sandbox, 2026-09-25): after S0 the template `row` is a field-only TemplateRow, so
  `row.contract` / `row.to_dict` / `row.to_checkpoint_format` read FIELDS at render time, while
  `core/templates._PIPELINE_ROW_API_NAMES` still classifies them as dynamic "row-api" access (config refuses them
  unless `required_input_fields: []`). Config-time is strictly MORE conservative than runtime (nothing the composer
  admits does the runtime refuse). Sizing: setting the set to {"get"} turns 78 tests red — the whole alias/macro/
  carrier corpus in tests/unit/plugins/llm/test_llm_config.py uses `to_dict()` as its canary, and the row-api kind
  machinery in core/templates.py (`_row_api_alias_expression_kind`, `_merge_row_api_kinds`, container aliases) loses
  its purpose (evidence: logs/round5b/S0-sandbox/extractor-constants-only.log). Needs its own unit + operator call:
  retire the reserved names (and which extractor paths become dead) or keep them reserved by policy.
  (multi_query_source_row_columns was aligned in S0 itself: its exclusion was fail-OPEN once those names read columns.)
- CLI-RELATIVE-SETTINGS (found by S0-sandbox, measured): `elspeth run --settings examples/X/settings.yaml` from a cwd
  inside <repo>/.claude/... loaded the MAIN checkout's examples/X/settings.yaml (Dynaconf upward search),
  not the cwd copy — the run recorded the main copy's base_url 8199 while the cwd copy said 8219. Absolute path fixed
  it. Agents running examples from worktrees with relative --settings may be exercising the main checkout's config.
- S3 input from S0 fix round 1 (2026-09-25): an opted-out (`required_input_fields: []`) template doing `row == row`,
  `row | items` or `row | dictsort` on a FIXED row whose data carries an extra key ends as "Template worker stopped before
  completing" (TemplateRow iterates data keys, resolves through the name index — PipelineRow's own asymmetry, parity-pinned).
  After fix round 1 every trigger needs the `[]` opt-out; the KeyError/AttributeError routing is S3's render-phase catch-all.
  Repro: scratch/S0-sandbox/fr1/probe_forms.py (FIXED+extra section), logs/round5b/S0-sandbox/fr1/probe_forms_HEAD.out.
- Noted, pre-existing, not chased (S0 fix round 1): `extract_jinja2_field_usage("{{ {'a': row}['a'] | ... }}")` records `a`
  as a row field (the broad receiver fallback treats the dict literal as a row). Over-approximation (fail-closed); a node that
  does this must declare `a`. Parent a05cfb2a9 identical.
- CARRIER-PRECISION (found by S0 fix round 2, 2026-09-26; measured on release/0.8.1 b97549096, fr2 HEAD identical): two
  whole-row gate misses in the carrier machinery of core/templates.py, both LEAKING on the S0 tree at render (TemplateRow is a
  Mapping, so dictsort emits every column; on the pre-S0 parent dictsort failed at render):
  (a) `{% set c = [[row]] %}{% for v in c %}{% for w in v %}{{ w | dictsort }}{% endfor %}{% endfor %}` → `()` with
      `required_input_fields: [note]` ADMITTED and leaking: `_carrier_path_has_pattern_child` accepts any deeper int path,
      so the loop target `v` is marked a ROW and the inner carrier is lost (a for target is never bound to the element's
      relative carrier paths).
  (b) `{% set c = [row, {'r': row}] %}{{ c[1].r | dictsort }}` → no whole-row: `_record_context_binding` returns at the
      row-collection branch, so a literal that is both a collection and a carrier never records its carrier paths. Refused
      today only because field `r` is undeclared; with `[note, r]` it would be admitted.
  Evidence: logs/round5b/S0-sandbox/fr2/preexisting-carrier-gaps-rel.out, probe_carrier_WIP.out. Same class as r1 Finding A
  (a whole row reaching a consumer the extractor never classifies) — the natural next S0 fix round.
- MQ-ALIAS-COLUMNS (found by S0 fix round 2; OPERATOR QUESTION): in multi-query, a column read through ANY alias of
  `row.source_row` is admitted with `required_input_fields: [body]`: `{% set s = row.source_row %}{{ s.secret }}`,
  `{% macro m(s) %}{{ s.secret }}{% endmacro %}{{ m(row.source_row) }}`, `m()`+`varargs[0].secret`. Identical on
  a05cfb2a9, release/0.8.1 and fr2 (logs/round5b/S0-sandbox/fr2/probe_mq_alias_BASE_REL.out, probe_mq_WIP.out).
  Cause: `multi_query_source_row_columns` is literal-only by design (pinned by test_llm_config_multi_query_contract.py
  "reads every literal source_row spelling and nothing else", shared verbatim with the composer at state.py:4296).
  Fix shape: derive the columns from `extract_jinja2_field_usage(template, row_attribute="source_row").fields` on BOTH
  seams (runtime + composer), which moves that pin and the composer's column list together. Needs an operator call.
- S7a-TYPED-SOURCE (found by S6, 2026-09-26, head 22a12cd8e): S7(a)'s passthrough abort also fires on a TYPED source.
  csv `x: int` (fixed) -> passthrough `schema: {mode: flexible|fixed, fields: ["x: float"]}` was a build refusal (exit 1)
  until S6 (f) applied ruling C3 at build time; it now builds and aborts at runtime with SchemaConfigModeViolation "field
  metadata mismatches for ['x']" (exit 4). field_mapper (which stamps) delivers the same shape (exit 0). Repro:
  scratch/S6-flags-minors/c3/t-passthrough/settings.yaml and c3/int/ (run-before.log exit 1, run-after.log exit 4).
  22a12cd8e must not merge to release/0.8.1 ahead of S7(a), or it is held back on its own.
- CLOSED by P2-S0-projection (43fe778d0 / 93e407cfb / a551acb41, ADR-051): CARRIER-PRECISION and MQ-ALIAS-COLUMNS close by
  construction (the template row and row.source_row hold only required_input_fields; r3's forms and the mq alias forms render
  0 sentinel hits, tests/unit/plugins/infrastructure/test_template_projection.py). EXTRACTOR-ALIGN decided by RULINGS Q5: the
  reserved row-API names stay reserved (no work). The S3 FIXED+extras residual remains only under the `[]` opt-out.
- P2 left for P3 (RAG-projection): rag/core.py:361 QueryBuilder.build(row.to_dict()) is the one unprojected row render;
  add RAG to the W10 projection inventories in test_template_call_sites.py.
- P2 found in the worktree an uncommitted P1 refactor (field_spelling.py, schema_validation.py, state.py; saved as
  scratch/P2-S0-projection/p1-leftover.patch) — lane owner to commit as P1's or drop.
- P3 (RAG-projection) found, for P4-minors (P2 hangover, not widened into P3): the LLM dual
  `LLMConfig._validate_required_input_fields_appear_in_template` fires on `extract_jinja2_fields(...)` being empty, so
  `prompt_template: "{{ row | dictsort }}"` with `required_input_fields: [a]` is REFUSED with the false claim "does not
  interpolate any row.* fields" (under ADR-051 that template renders `a`), and its remedy (b) recommends
  `required_input_fields: []` — the whole-row opt-out, the one repair that reopens the leak. Measured:
  logs/round6/P3-RAG-projection/llm-dual-probe.log. The RAG dual (P3) counts any load of `row` and never suggests `[]`;
  align the LLM one (and its composer twin, if any) the same way.
  CLOSED by P4-minors f60338a2b (LLM dual counts only reads of the context row; remedy never suggests []; P3 F1
  scope-aware template_loads_name). The P1 leftover diff (field_spelling.py,
  schema_validation.py, state.py) is STILL uncommitted and was excluded from P3's commit by pathspec (byte-identical to
  scratch/P3-RAG-projection/foreign-at-start.patch / foreign-now.patch).
- P3 closed: rag/core.py `QueryBuilder.build(row.to_dict())` is gone; RAG is in the W10 projection inventories.
- PASSTHROUGH-CAPABILITY (from P4-minors, S4 r2 F2, 2026-09-27): any batch plugin whose flush is not one row per buffered
  row validates under `output_mode: passthrough` and then aborts the run (exit 4, tokens with no terminal outcome):
  batch_replicate with copies (scratch/P4-minors/replicate_pt) AND batch_stats (scratch/P4-minors/stats_pt). The composer
  refuses only batch_replicate, by NAME. A narrow declaration fix (bd09a4a20) broke 24 lane tests that use identity-replicate
  as a passthrough vehicle and was reverted (5c6917577). Needs a ruling on options A/B/C in impl-P4-minors.md §2.5, then
  its own unit with a composer/runtime parity sweep over the batch plugins.

## 2026-09-27 — from GUIDANCE-REVIEW.md (read-only review at 4bb77fbd5; guidance NOT updated per John)
- Guidance update unit (on John's go-ahead): 18 P1 / 31 P2 / 9 P3 items across skills, tool schemas, refusal/explain text,
  plugin hints + 21 gates to regenerate. Do AFTER T1, G3, C1/C2/C3, X1 and merge-2 land (5 P1s are conditional on them).
- CODE defects surfaced by the review (not guidance — fix in lane):
  - P-03: type_coerce with a missing conversion field now ABORTS the run (exit 4, measured) — S7(b) required the runtime
    residual to ROUTE. Regression to fix.
  - S-02: a template reading `row['Price USD']` (header spelling) under a declared list is REFUSED by config while the
    runtime resolves it. Lane-owner lean: declarations AND template reads use canonical names (spelling rule); keep the
    refusal, make its message name the canonical spelling, fix the teaching — confirm in the T1/G3 follow-up.
  - Catalogue exposes no is_batch_aware / flush_emits_one_row_per_buffered_row / created-output types (T-04, P-14): the
    planner cannot SEE passthrough capability or output types — a catalogue payload gap, fix with the guidance unit.
- behaviour-diff REPORT-4bb77fbd5.md reportedly has every entry MISSING — check the harness (H agent still running).

## 2026-09-27 ~19:00 — SLOTTING of the guidance findings after Astra's independent verification (codex/codex-review-guidance.md)
Rule applied: Astra's verdict + corrected draft WINS over GUIDANCE-REVIEW.md wherever it says CONFIRMED-AMEND; priority = Astra's
column; nothing REFUTED by Astra except the two overturned/kept below. SUPERSEDES the two "CODE defects" bullets above.

### Code defects (lane code, NOT guidance)
| id | verdict | slot | note |
|---|---|---|---|
| P-03 | LANE REGRESSION (Astra's "not a code defect" OVERTURNED by measurement) | NEW unit R2 in round 10b, after specialist review of DESIGN-R2-declared-input-miss.md | release 4cfabda20: exit 1, row routed to q; lane 7976d84a7: exit 4 abort (scratchpad p03/). Root: ADR-013 per-row check treats a miss the build could NOT prove as Tier 1. Fix = two-case split + ADR-013 amendment, whole class (census CENSUS-R2-*.md). DROP Astra's P-03 draft ("Missing required input stops the run") — it teaches the regression. |
| M-04 | PRE-EXISTING (release also exit 4) | R2 (class) + separate field_mapper `strict:false` semantics decision after R2 | docstring field_mapper.py:310 vs option text vs runtime disagree; decide semantics, then align all three. Astra's draft NOT adopted yet. |
| S-02 | CONFIRMED code defect (narrower: identifier-shaped header e.g. `row['Name']` under `[name]` refused; `Price USD` admitted) | round 10b, S02 unit (after G3 lands — same static template analysis) | RULINGS lines 124/186 + ADR-051(b): both spellings of a declared field resolve. My earlier "keep the refusal" lean CONTRADICTED the ruling — withdrawn. Static check must resolve aliases with the upstream alias info. |
| C2 / S-03 | CONFIRMED (`row \| tojson` validate 0 / run 2) | round 10b C2 unless G3 fixed it — measure first | whole-row view must support builtin filters; do NOT narrow the documented API. |
| G3 row.keys() / R-03 | CONFIRMED gap | G3 (round9, running) — verify at 10b | unsupported calls refused at config unconditionally. |
| C3 leak | CONFIRMED at 4bb77fbd5 (`'oops' is not a valid integer string`) | round10a C3 (running) | Astra confirms the example only; the C3 review must show the CLASS sweep. |

### Guidance → the ONE merge-1 guidance unit (after T1, G3, C1–C3, R2, S02, X1, X2 land; Astra's drafts win)
- P1: S-01, S-02(text half, after S02 code), S-03 (after C2/G3), S-04 ("`get` is the only row method…"), S-05, S-10↑, S-13↑,
  T-01 (upsert_node + transforms.py:278–281,1860 ONLY — set_pipeline/terminal do not carry it), T-02 (keep image-input
  exception), R-01, R-02, R-03, R-07↑, P-13↑, P-17↑ (first sentence only; runtime-contract criticism REFUTED),
  M-01, M-02, M-03, M-06, B-16.
- P2: S-08, S-09, S-11, S-12↑, T-03, T-04 (needs the catalogue capability published first — CODE, same unit), T-05, T-06,
  T-07, T-08, T-09, T-10↑, T-11↑, R-06, R-08 (Astra's draft; the review's "never declare the post-conversion type" is WRONG),
  R-09, R-10, R-11, R-12, R-13, R-14 (reason subtype, not an invented code), P-01↓, P-02↓, P-03 (REWRITE after R2 to the
  routed behaviour), P-05, P-07 (no ban on new capitalised targets), P-08, P-09, P-10, P-11, P-12 (`fields: all` exception),
  P-14 (configured-instance facts, CODE in catalogue), P-16↑, P-18 (scope corrected; not "91 enum defects"), M-05, M-07,
  B-19.
- P3: S-14 (note only; policy knob unauthorable), B-17, B-18, B-21, B-22.
- Conditional on T1 (same unit, written after T1): S-07, R-05 (split: type_coerce runtime contract already right), P-06,
  M-08, and T-03's post-T1 sentence.
- Conditional on codex-final cherry-pick: R-04 (Astra's draft — the refusal's canonical name is withheld from the planner;
  either teach resolution from options or publish a structured canonical-name fact).
- BRIEF RULES for the unit (from Astra's surface map): (1) JSON-schema field/knob descriptions are STRIPPED before the proposal
  planner (planner_authoring_aids.py:1363,1432) — a description-only fix does not reach it; fix hints/skills/aids and check
  the planner projection; (2) include the omitted surfaces: selected-schema evidence rehydration, terminal instructions,
  notices, direct tool-loop context messages; (3) MCP roster is 32 tools vs 42 — no inspect_source/emit_pipeline_proposal
  on MCP, and MCP carries no skill instructions; (4) repair feedback keeps detail for 3 codes (pipeline_planner.py:2696–2710);
  (5) composer invariants: teach and refuse, never author. Regenerate its 21 gates.

### Guidance → merge 2 (six batch plugins)
- S-06 / P-04: replace every inventory claim ("only batch_rank…", counts) with capability wording ("emits one row per
  buffered row; supports passthrough and transform") — do it at the X1 cherry-pick in merge 1 so merge 2 changes no prose.
- P-15: integration checklist for the merge-2 integration unit (verify real options when implemented).
- B-23: correlation's x_field/y_field are not read by GEN:3244–3246 (reads only value_field) — derive the numeric-input
  capability properly in the merge-2 integration unit.

## 2026-09-27 ~20:30 — from X2 (impl-X2-unbounded-binds.md "Finding for the lane owner") — NOT FIXED, needs a design
- SINK-EFFECT EVIDENCE CAP: LocalFileEffectPlanEvidence (json/csv/text/document) lists every accepted/diverted ordinal in
  safe_evidence (64 KiB cap, contracts/sink_effects.py:278) → one sink write of > ~12,641 rows stops the run. release/0.8.1
  (rel-pl15000): exit 4, 15000/15000 tokens OUTCOMELESS (pre-existing; violates "every row one terminal outcome").
  chroma / azure_blob / dataverse evidence also list ordinals. X2's options: derive ordinals from
  sink_effect_members.prepared_disposition + bind a digest; bound each engine sink write to ≤ N rows; range-encode (not a
  class fix). Lane owner: architect + systems design pass after X2 commits; propose slotting with merge 1b.

## 2026-09-28 12:15 — from the batch_rank P1 fix (impl-rankfix.md); NOT in the landing branch (frozen scope)
- batch_replicate.py ~385 and batch_outlier_annotator.py ~558 do the SAME first-row-wins output-contract merge (pre-existing
  on release). Fix = join_batch_contracts over the distinct contract instances, as f481c3d05 did for batch_rank. Slot: merge 2
  (six batch plugins) or a small release fix — John to choose when the landing is done.
- A downstream per-row transform rebuilding its contract makes merge_for_node_evolution raise when rows carry object then
  int — pre-existing for any multi-producer input. Merge 1b.
