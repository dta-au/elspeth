# SME Review — Advisor Evidence Fix for Multi-Query LLM Nodes (session 94f6f00c defect)

> This document is a durable copy of an SME (subject-matter-expert) design review produced by an
> LLM-as-judge subagent in session `084a8817` on 2026-09-13, originally written to that session's
> ephemeral scratchpad (`scratchpad/llm_evidence_design_review.md`) and copied here verbatim so it
> survives past that session. It is the source document cited by
> `docs/plans/2026-09-13-advisor-multiquery-evidence-fix-plan.md`'s `**Spec:**` line — read this file
> first for the full evidence citations behind every task in that plan.

Reviewed as an LLM-as-judge design SME. Read-only review of a proposed design (not yet coded); all
citations were re-verified directly against the live tree this session, not carried over from the prior
diagnostician report.

## Fact-finding summary (evidence base for this review)

- `src/elspeth/web/composer/service.py:9415-9436` (`_ADVISOR_SUMMARY_VALUE_KEYS`, `_ADVISOR_SUMMARY_PROMPT_VALUE_KEYS`,
  `_ADVISOR_SUMMARY_PROMPT_VALUE_MAX_CHARS=1000`): confirmed `queries` and `system_prompt` are absent from
  the allowlist; only `prompt_template`/`template` get the 1000-char prompt budget.
- `service.py` `_render_options_for_advisor` (~9700): confirmed the exact render shape —
  `{key}_untrusted_json={json.dumps(rendered)}` for prompt-shaped keys, `values withheld: k1, k2, …` for
  everything else, sorted by key name.
- `service.py` `_advisor_prompt_option_values` (~9253-9280) and `_advisor_summary_renders_option_value`
  (~9475): confirmed the deterministic injection pre-scan walks the **same** `options` mapping through the
  **same** admission predicate as the renderer — `queries` and `system_prompt` are skipped by both, not
  just under-rendered.
- `service.py` `_render_schema_for_advisor` (~9485-9560): confirmed the existing precedent for a bounded,
  budget-aware render with an explicit `additional_fields_withheld`-style counter — this is the pattern the
  proposed `additional_queries_withheld: N` would extend, not invent.
- `service.py` rubric text (~7910-7935, the `problem_summary` built for `ADVISOR_TRIGGER_DETERMINISTIC_END`):
  confirmed the degeneracy-check sentence **literally names** `prompt_template`: "Use each LLM node's
  visible prompt_template excerpt and its listed, length-independent interpolated row fields to check one
  concrete degeneracy…". Confirmed separately the general mismatch-check sentence does **not** name any
  specific key ("quote each explicit configuration constraint visible in the user's request excerpt … and
  compare it only when the pipeline excerpt exposes the corresponding fact") and the withheld-value
  carve-out ("never FLAG an option, field, or contract merely because its value or entry is withheld").
- `src/elspeth/web/composer/state.py:3831` (`_well_formed_query_entries`) and `~3714-3760`
  (`_parse_template_names`, `PromptTemplateNames`): confirmed the mapping/list dual-shape parser used
  elsewhere in this same module for query entries — the natural helper to reuse for the render/scan surface.
- `src/elspeth/plugins/transforms/llm/base.py:495-500` and `:685-760`
  (`_validate_required_input_fields_declared`, `_validate_template_variable_bindings`): confirmed the
  authoritative effective-template rule ("each query's effective template is its `template` override when
  present, else the node-level `prompt_template`; a node-level template no query falls back to never
  renders") and confirmed the plugin **already computes** exactly the "which queries fall back to the
  node-level template" list (`node_template_specs` at ~line 700) that a dead/partially-used label needs —
  the composer-side helper does not need to invent new logic, it needs to mirror this existing check.
- `src/elspeth/plugins/transforms/llm/transform.py:337-341`: confirmed `system_prompt`, when present, is
  sent as `ChatMessage(role="system", …)` on **every** provider call for the node — single-query mode. (Not
  re-confirmed for multi-query mode in this pass; see Information Gaps.)
- `tests/unit/web/composer/test_advisor_checkpoint.py:4579-4598`: confirmed the "single source of truth
  derived list + disagreement test" pattern this codebase already uses for
  `_advisor_control_flow_fields` — the precedent your Point 1/2 helper should follow for its own test.
- Live incident data, `versions.json` v6 (`colour_questions` node), read directly this session:
  ```
  prompt_template: "Answer the question about the colour {{ row.colour }} in one short reply."
  queries.good_pair.template: "What is a good colour pair for {{ row.colour }}? Reply with the single
    colour name only, with no other words, no explanation, and no punctuation."
  queries.hex_code.template: "What is the approximate hex code of the colour {{ row.colour }}? Reply
    with just the hex code."
  system_prompt: "Reply with only the value asked for and nothing else — no explanations, no
    commentary, no extra words, and no punctuation."
  ```
  This is the exact payload to use for Question E's before/after render diff.
- `src/elspeth/web/config.py:397` (`composer_advisor_max_prompt_tokens: int = Field(default=4000)`) and
  `service.py:7299` (`char_cap = composer_advisor_max_prompt_tokens * 4`): confirmed 16,000 chars, matching
  the proposal's stated cap — but see Q C below on scope.

---

## Answers

### A. Is rendering `system_prompt` beside per-query templates the right evidence shape?

**Yes, with a labelling requirement, not a shape change.** `system_prompt` is not optional context — per
`transform.py:338` it is sent on every call, so it is part of the effective prompt for every query, more
authoritative in scope than any single query's template (it is the union of "applies to all"; a query
template is "applies to one"). Withholding it, as today, is strictly worse than any rendering risk from
showing it. The real design question is ordering and labelling, not inclusion.

Two concrete bias risks, both groundable in the rubric text you're not proposing to change:

1. **Scope-conflation risk.** If `system_prompt` is rendered as just another same-shaped bullet next to
   `queries.good_pair.template`, the judge has no signal that it must be read as a *modifier* of every
   query below it rather than a peer prompt. The live incident is the worst case for this: the actual
   defect that occurred later in the same session (F7 in the prior diagnostician report — a node-wide
   `system_prompt` saying "no punctuation" silently governs `hex_code`, whose correct answers are
   `#RRGGBB` strings) is a scope-conflation defect a good judge *should* catch — but only if the render
   makes the "applies to every query below" scope explicit rather than implicit. Label it literally as
   scope-qualified, e.g. `system_prompt (applies to every query on this node): "…"`, not bare
   `system_prompt: "…"`.
2. **Length/verbosity bias (Zheng 2023).** Adding system_prompt + N query templates to a node's evidence
   block measurably lengthens it relative to today's single-`prompt_template` render. LLM judges have a
   documented tendency to associate longer, more elaborated evidence with higher quality regardless of
   content. This is a real but secondary risk here because the judge is scoring the *pipeline*, not the
   response text — the more urgent asymmetry is the one in (1), not verbosity. Mitigate the same way
   `_render_schema_for_advisor` already does: fixed per-item truncation (you already propose 1000 chars),
   not proportional-to-importance elaboration.

I would **not** frame `system_prompt` as "authoritative" in the sense of overriding a query template on
conflict — that is not how they compose (they are concatenated as separate messages, not merged/overridden)
— frame it only as "in scope for every query," to avoid teaching the judge a resolution rule that doesn't
exist in the plugin.

**Confidence:** Moderate — the scope-conflation risk is grounded in the actual F7 finding from this same
incident and the confirmed `transform.py:338` call site; the verbosity-bias claim is generic LLM-judge
literature (Zheng 2023), not something this review measured in this repo.
**Risk of not addressing:** Medium. A judge that can now see `system_prompt` but reads it as a peer to a
per-query template, rather than as a governing constraint over all of them, would fail to catch exactly the
cross-query contamination class of defect this incident's own repair sequence produced.

### B. How should the dead node-level `prompt_template` be labelled?

Reuse the computation the plugin **already performs**, don't invent a parallel one. `base.py`'s
`_validate_template_variable_bindings` (~line 700) already builds `node_template_specs`: the list of query
names whose `template` key is absent, i.e. which queries actually fall back to the node-level template. The
composer-side helper's "used by queries X" label is this same computation restated over
`_well_formed_query_entries` (`state.py:3831`), which already exists in the composer module and accepts the
same mapping/list dual-shape queries option `base.py` accepts.

Proposed exact wording, three cases:

- **All queries override (dead slot):**
  `prompt_template: not used — every query below supplies its own template`
  This is the incident's exact case (v6: both `good_pair` and `hex_code` carry `template`).
- **Some queries fall back:**
  `prompt_template: used by quer{y|ies} without their own template: [name1, name2]` followed by the
  rendered prompt_template text — i.e. still render the *text*, because for those queries it is live.
- **No `queries` option at all (single-prompt mode):** unchanged from today — render as effective, no label
  needed, since there is nothing to disambiguate against.

The failure mode to avoid, stated precisely: do not phrase the dead-slot label as an editorial judgement
("dead", "unused code", "leftover") — that primes the judge to treat a structurally-inert-but-honestly-
present field as a code-quality defect in itself, which is out of the rubric's scope (the rubric owns
pipeline/prompt correctness, not composer hygiene). "not used" / "used by" is a fact statement about
render-time behavior, matching the register of your existing `additional_fields_withheld` counters.

**Confidence:** High for the underlying mechanism (both `base.py:685-706` and `state.py:3831` were read
directly and the fallback logic matches). Moderate for the exact wording, which is a design choice with no
single objectively-correct phrasing.
**Risk:** Low. Wording is cheap to iterate and does not affect what the judge can see, only how it's framed.

### C. Is a per-query cap of 8 plus an explicit withheld count sound?

**Sound as the least-bad choice, but it reproduces F1 in miniature above the cap, and you should say so
in the label rather than let the fix look complete.** The precedent is real and load-bearing:
`_render_schema_for_advisor` (~9500-9560) already does exactly this — cap at `_ADVISOR_SUMMARY_SCHEMA_MAX_FIELDS
= 8`, an `additional_{key}_withheld` counter, and a budget-aware backoff loop that never emits a
half-truncated field. Reusing "8" for query count is defensible as consistency with an existing,
presumably-tuned constant, not because 8 has independent significance for queries specifically.

**Failure mode above the cap:** a node with more than 8 queries has queries 9+ genuinely invisible to the
judge — the render will show `additional_queries_withheld: N`, and per the rubric's own carve-out ("never
FLAG … merely because its value or entry is withheld"), the judge is instructed not to penalize this. That
is correct behavior for the *rubric*, but it means the fix's guarantee is "the judge sees this defect class
for pipelines with ≤8 queries per node," not "the judge sees it, period." That's a materially narrower claim
than what your Point 1 helper's docstring should assert. I'd recommend the withheld-count message name the
limitation explicitly for the judge, not just as a code comment: something like
`additional_queries_withheld: N (evidence-capped; use request_advisor_hint tool if visibility into a
specific withheld query is needed)` — assuming such an escape hatch tool exists and can take an
arbitrary key path (I did not verify this in this pass — see Information Gaps).

**One interaction risk worth checking before landing, not yet confirmed:** the per-call cap referenced in
your Point 2 (`composer_advisor_max_prompt_tokens*4` = 16,000 chars, confirmed at `config.py:397` /
`service.py:7299`) is enforced in `_validate_advisor_arguments` for the **`request_advisor_hint`** tool
call path. I did not find, in this pass, an equivalent enforced cap on the END checkpoint's own
`schema_excerpt` (the `pipeline_summary` built by `_summarize_pipeline_for_advisor`) — the code comment at
`service.py:9472` cites the 16,000-char figure as a design target ("kept well under"), which reads as an
informal budget rather than a runtime-enforced one for this specific path. A pipeline with several
multi-query LLM nodes each near the per-node cap (8 queries × 1000 chars + system_prompt × 1000 chars ≈
9000 chars from ONE node) could plausibly make the total `schema_excerpt` for a multi-node pipeline exceed
whatever the actual enforced ceiling is for the END-gate call. Verify what the real ceiling is for
`schema_excerpt` specifically (not `request_advisor_hint`'s arguments) before assuming the 16,000-char
figure applies here as a hard backstop.

**Confidence:** High on the precedent match (`_render_schema_for_advisor` is read directly and is
structurally identical to what you're proposing). Moderate on the failure-mode framing (rubric text is
explicit; the practical rate of >8-query nodes in the wild is not something I measured). Low/Insufficient
Data on the char-cap interaction — flagged as a verification item, not a finding.
**Risk:** Low for the cap mechanism itself (it degrades gracefully and honestly). Medium for the unverified
char-cap interaction if it turns out `schema_excerpt` has no enforced ceiling at all — a large pipeline with
several near-cap multi-query nodes could then produce evidence the advisor provider itself truncates
server-side, silently reproducing a different flavor of F1 (mid-render truncation instead of a withheld
key).

### D. Why did the judge FLAG here, given the rubric already says "don't infer/verify on withheld"?

**Because the dead `prompt_template` was never withheld — it was rendered, in full, as if it were live.**
The withheld-value carve-out only protects keys that are actually absent from the render (`values withheld:
…`). `prompt_template` is in `_ADVISOR_SUMMARY_PROMPT_VALUE_KEYS`, so it was fully rendered with its 1000-char
budget; from the judge's point of view it received exactly what the rubric tells it to use: "quote each
explicit configuration constraint visible in the user's request excerpt … and compare it only when the
pipeline excerpt exposes the corresponding fact; FLAG any visible mismatch." The user's excerpt stated a
constraint ("I just want the colour they recommend and nothing else"); the pipeline excerpt exposed a fact
(the dead `prompt_template` text, rendered as *the* prompt); the two didn't match; the judge FLAGged. **The
rubric executed correctly on incomplete evidence** — this is not a rubric defect in the general
mismatch-check sentence, it is exactly the evidence-completeness bug your fix targets.

**However, the rubric does need one specific wording change, not zero, and evidence completeness alone is
not sufficient.** The degeneracy-check sentence hardcodes the key name: "Use each LLM node's visible
**prompt_template** excerpt and its listed … interpolated row fields to check one concrete degeneracy."
Even after your Point 1/3 fix makes `_render_interpolated_row_fields` union the effective templates, this
sentence still tells the judge, by name, to look at "prompt_template" for the degeneracy check specifically
— not "queries.*.template." A judge following the literal instruction on a multi-query node where
`prompt_template` is now correctly labelled "not used" could reasonably conclude the degeneracy check
doesn't apply to this node at all, rather than redirecting itself to the query templates. Reword to
something scope-neutral: "Use each LLM node's visible effective prompt text (the node-level
`prompt_template`, or in multi-query mode each query's `template`) and its listed … interpolated row
fields …". The general mismatch-check sentence, by contrast, does not need a wording change — it already
says "compare it only when the pipeline excerpt exposes the corresponding fact," which becomes correct by
construction once the fact (the query template) is exposed.

**Confidence:** High — both rubric sentences were read directly this session and the distinction (one
names a key, one doesn't) is unambiguous text, not inference.
**Risk of skipping the rubric edit:** Medium. Evidence completeness fixes the mismatch-check path (Q's
constraint-vs-fact comparison) but leaves the degeneracy-check path pointing at a key your own fix is about
to relabel "not used," which could produce a new, narrower blind spot specific to the degeneracy check on
multi-query nodes — the same defect class as F1, smaller radius.

### E. What measurement proves the fix works, deterministic and offline?

Use the real v6 `colour_questions` options captured above as fixture data (already extracted this session,
not needing a live provider) and assert against `_render_options_for_advisor` / the new helper directly —
no advisor call needed, since the defect and the fix both live entirely in string-building code.

1. **Render diff (before/after), no LLM call.** Call today's `_render_options_for_advisor(v6_options)` and
   confirm it reproduces the incident's already-measured shape (`prompt_template_untrusted_json="Answer the
   question about the colour {{ row.colour }} in one short reply."`, `values withheld: …, queries, …,
   system_prompt`, per the prior diagnostician report's independently-verified control). Then call the
   fixed renderer on the identical `v6_options` and assert: (a) `system_prompt` text appears and is
   labelled scope-qualified per Q A; (b) both `queries.good_pair.template` and `queries.hex_code.template`
   appear verbatim, each under a `queries.<name>.template` label; (c) `prompt_template` is labelled "not
   used" per Q B, case 1. This is a pure unit test — three string assertions against one fixture, no judge
   involved.
2. **Injection-scan parity, no LLM call — and this is the sharper test, run it first.** Before touching the
   renderer, confirm the **security** half of the same gap: construct a mutant of the v6 fixture where
   `queries.good_pair.template` contains an injection payload string (reuse whatever fixture
   `_looks_like_advisor_prompt_injection` already has a positive control for in
   `test_advisor_checkpoint.py`) and assert that **today's** `_advisor_prompt_option_values(mutant_options)`
   does *not* flag it — i.e. prove the pre-scan is currently blind here too, not just the renderer. This
   matters because `_advisor_prompt_option_values` walks the identical `_advisor_summary_renders_option_value`
   admission set as the renderer (confirmed, `service.py:9253-9280`), so today an injection payload smuggled
   into a query template is neither shown to the judge NOR caught by the deterministic pre-scan — a second,
   more serious defect than F1's judge-blindness, since this one is a security control gap, not just an
   evidence-quality gap. Then run the same mutant through your Point 2 unified helper and assert the scan
   now fires. This is the repo's own doctrine applied directly: AGENTS.md's evidence-doctrine section says
   an instrument must be proven against a known-positive case before it's trusted, and this codebase already
   has a "disagreement test" convention (`test_advisor_checkpoint.py:4579-4598`, for
   `_advisor_control_flow_fields`) that is the exact template to copy for the new `queries`/`system_prompt`
   surface — write ONE test asserting render-set == scan-set for the new helper's output, mirroring that
   existing test.
3. **Row-field union check.** With the same v6 fixture (both query templates interpolate `row.colour`, and
   `input_fields` maps `colour` -> `colour`), assert the fixed `_render_interpolated_row_fields`-equivalent
   returns `[colour]` rather than today's behavior, which (per `_node_prompt_template`, `service.py:9560-9575`)
   reads only the dead `prompt_template` and would report the SAME `[colour]` field by coincidence in this
   exact fixture (both the dead node-level template and the live query templates interpolate `row.colour`).
   **This fixture alone will not distinguish correct-union-of-effective-templates from
   still-reading-the-dead-slot**, because they agree by accident here. Add a second synthetic fixture where
   the node-level `prompt_template` interpolates a field the query templates do NOT (e.g. node-level reads
   `{{ row.notes }}`, both queries read only `{{ row.colour }}`) and assert the fixed signal reports
   `[colour]`, not `[colour, notes]` or `[notes]` — this is the actual regression-catching case, not the
   incident replay.
4. **No provider call needed for any of the above** — all four are string/dict-in, string/dict-out unit
   tests against the already-captured fixture plus one synthetic fixture for point 3. A live-judge
   confirmation (does the judge actually stop FLAGging on this evidence) is a separate, non-deterministic
   question this review does not consider decidable offline — see Information Gaps.

**Confidence:** High for the fixture data itself and for the mechanism gaps in points 1-3 (all read
directly from the incident's own `versions.json` and the current source). Moderate for point 2's specific
security-gap claim — I confirmed the code path structurally but did not run the actual mutation test in
this pass (no source-file edits permitted for this review).
**Risk:** Low — these are all pre-merge unit-test recommendations, not runtime changes.

---

## Confidence Assessment

| Finding | Confidence | Basis |
|---|---|---|
| `queries`/`system_prompt` are absent from both the render allowlist and the injection pre-scan admission set | High | `service.py:9415-9436`, `:9253-9280` read directly |
| The rubric's degeneracy-check sentence hardcodes `prompt_template` by name | High | `service.py` rubric text ~7920, read directly this session |
| The rubric's general mismatch-check sentence does not need a wording change | High | Same read; sentence is already key-agnostic |
| The dead-slot computation already exists in `base.py` and should be reused, not reinvented | High | `base.py:685-706` (`node_template_specs`) read directly |
| The FLAG was a correct rubric execution on incomplete evidence, not a rubric logic bug | High | Direct comparison of rubric text against the render allowlist |
| `_render_schema_for_advisor`'s cap+counter pattern is the right precedent for query-cap | High | `service.py:9485-9560` read directly, structurally matches Point 2 |
| The pre-scan has the identical `queries`/`system_prompt` blind spot as the renderer (security gap, not just visibility gap) | Moderate | Code path confirmed; the actual mutation was not executed in this review |
| `schema_excerpt` may lack an enforced 16,000-char ceiling distinct from `request_advisor_hint`'s | Low/Insufficient Data | Only one enforcement site found (`_validate_advisor_arguments`), scoped to a different tool; did not locate an equivalent for the END checkpoint path |
| `system_prompt` behaves identically (system-role message on every call) in multi-query mode, not just single-query mode | Moderate | Confirmed for single-query (`transform.py:338`); multi-query call site not re-checked this pass |

## Risk Assessment

**Implementation Risk (of the proposed fix, as scoped by Points 1-3):** Low-Medium
**Reversibility:** Easy — this is string-rendering code with unit-testable fixtures; no runtime data model changes implied.

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| Scope-conflation: judge reads `system_prompt` as a peer prompt, not a governing constraint over every query | Medium | Medium | Label explicitly per Q A ("applies to every query on this node") |
| Degeneracy-check rubric sentence still points at the now-relabelled dead `prompt_template` | Medium | Medium-High if unaddressed | Reword per Q D to name "effective prompt text" generically |
| >8-query nodes remain a smaller-radius repeat of F1 | Low-Medium | Low (most nodes likely have few queries) | Say so explicitly in the withheld-count message, not just in a code comment |
| Injection pre-scan gap on `queries`/`system_prompt` is a live security hole independent of this fix's visibility goal | High if exploited | Unknown (not measured) | Point 2's "scanner scans the same texts as the renderer" design is the correct fix; verify with the mutation test in Q E point 2 before merging |
| `schema_excerpt` char-cap enforcement scope is unverified | Low-Medium | Unknown | Verify before assuming the 16,000-char figure backstops the END-gate path specifically |

## Information Gaps

1. [ ] **Multi-query `system_prompt` call site**: I confirmed `system_prompt` → system-role message in
   single-query mode (`transform.py:338`) but did not re-locate and confirm the equivalent site in
   multi-query execution (likely `multi_query.py`, not opened this pass). If multi-query mode sends
   `system_prompt` differently (e.g. per-query vs once), the "applies to every query" label in Q A needs to
   match the actual runtime semantics, not an assumption carried over from single-query mode.
2. [ ] **Enforced char ceiling for `schema_excerpt`/`pipeline_summary` specifically**: only one enforcement
   site was found (`_validate_advisor_arguments`, scoped to the `request_advisor_hint` tool). Whether the
   END checkpoint's `schema_excerpt` has any enforced (not just commented-as-target) ceiling was not
   determined in this pass — relevant to Q C's interaction risk.
3. [ ] **Whether an escape-hatch tool exists for a withheld query beyond the cap**: Q C proposes the
   withheld-count message point the judge at `request_advisor_hint` for a specific withheld query; I did
   not verify that tool accepts an arbitrary options key path for a specific node.
4. [ ] **Live judge behavior on the fixed evidence**: this review is entirely deterministic/offline per the
   question's own framing (Q E). Whether the judge's actual FLAG/CLEAN behavior changes as expected on the
   fixed render is a live-provider question outside this review's scope.

## Caveats & Required Follow-ups

### Before Relying on This Analysis
- [ ] Confirm multi-query `system_prompt` semantics (Gap 1) before finalizing the Q A label wording.
- [ ] Confirm `schema_excerpt`'s actual enforced (or unenforced) char ceiling (Gap 2) before treating 16,000
      chars as a hard backstop for Point 2's budget math.
- [ ] Run the injection-scan mutation test in Q E point 2 before claiming Point 2 closes the security gap,
      not just the visibility gap — this review inferred the gap structurally but did not execute the
      mutant.

### Assumptions Made
- That `_well_formed_query_entries` (state.py:3831) is safe and appropriate to reuse from
  `service.py` for the new helper (both are in the composer package; no cross-module boundary issue
  expected, but the review did not check for an existing import or a reason it was duplicated rather than
  shared, if it is).
- That the rubric's fixed system-instruction text (`problem_summary`) is the only place the degeneracy
  check is worded — did not search for a second copy (e.g. a YAML-side or skill-side restatement) that
  might also need the same edit.

### Limitations
- This review did not execute any test, did not call a live judge, and made no source edits (as
  instructed). All "before/after" claims in Q E are design recommendations, not measured results.
- Did not review whether the fix changes the injection-scan's runtime cost meaningfully (more values
  scanned per node) — likely negligible given the existing 1000-char-per-value budget, but not measured.

### Recommended Next Steps
1. Close Information Gaps 1-3 (two are single-file reads; one is a tool-schema check).
2. Implement Points 1-2 with the mutation test from Q E point 2 as a required test, not optional — this
   codebase's own evidence doctrine (AGENTS.md) treats an unproven instrument as equivalent to no
   instrument.
3. Add the rubric wording change from Q D alongside the evidence fix, in the same change — landing the
   evidence fix without it leaves the degeneracy check pointed at a key you are simultaneously relabelling
   "not used."
4. Add the two-fixture test from Q E point 3 (incident replay + a fixture where node-level and query-level
   templates diverge) so the row-field union logic is actually exercised, not coincidentally passed.

## Summary (machine-readable)

```json
{
  "overall_confidence": "Moderate",
  "implementation_risk": "Medium",
  "reversibility": "Easy",
  "top_findings": [
    {"claim": "queries/system_prompt are absent from both the render allowlist AND the injection pre-scan admission set (security gap, not just visibility gap)", "confidence": "High", "evidence": "service.py:9253-9280, 9415-9436"},
    {"claim": "The FLAG was correct rubric execution on incomplete evidence; the general mismatch-check sentence needs no wording change", "confidence": "High", "evidence": "service.py rubric text ~7920"},
    {"claim": "The degeneracy-check sentence hardcodes 'prompt_template' by name and needs a wording change even after the evidence fix lands", "confidence": "High", "evidence": "service.py rubric text ~7920"},
    {"claim": "The dead-slot 'used by queries X' computation already exists in base.py and should be mirrored, not reinvented", "confidence": "High", "evidence": "base.py:685-706 node_template_specs"},
    {"claim": "The 8-query cap is sound but reproduces F1 in miniature above the cap; say so in the withheld-count message", "confidence": "Moderate", "evidence": "service.py:9485-9560 precedent"}
  ],
  "blocking_gaps": [
    "Multi-query system_prompt call site not re-verified",
    "schema_excerpt char-cap enforcement scope not located",
    "Injection-scan mutation test not executed (structural gap inferred, not proven)"
  ],
  "recommended_next_steps": [
    "Land the rubric wording change (Q D) in the same change as the evidence fix, not separately",
    "Add the render-set == scan-set disagreement test for the new helper, mirroring test_advisor_checkpoint.py:4579-4598",
    "Add a synthetic fixture where node-level and query-level templates interpolate different fields, to actually exercise the row-field union logic"
  ]
}
```
