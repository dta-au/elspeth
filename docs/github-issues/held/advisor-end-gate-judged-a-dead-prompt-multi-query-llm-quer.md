---
title: Advisor end-of-turn gate judged a prompt that never runs on multi-query LLM nodes
labels: [area/composer, area/audit, type/bug]
---

On an `llm` node configured with multiple queries, the evidence assembled for the
end-of-turn advisor gate omitted every per-query `template` and the node's `system_prompt`.
The advisor therefore judged the node-level `prompt_template`, which on that node never
rendered, and could not see the repairs that were made.

## Background

The advisor is a second model that reviews a composer turn before it may complete; it is
given a summary of the pipeline's node options as evidence, with some option values withheld.
An `llm` node may carry one prompt, or several named queries that each override the node's
prompt with their own `template`.

## What happened

Observed in one reviewed session on 2026-09-13. The advisor evidence summary admitted only
node-level prompt keys (`prompt_template`, `template`) and reported the rest as
"values withheld: queries, system_prompt". Where every query carries its own `template`
override, the node-level `prompt_template` does not render for that node — the rule is
enforced by `LLMConfig._validate_template_variable_bindings` in
`src/elspeth/plugins/transforms/llm/base.py`.

The advisor flagged the user's explicit constraint against that unused prompt. The repairs
were then made in the query's own `template` and in `system_prompt`; both were invisible to
the gate, so the second pass flagged the same thing again, and with a two-pass limit the
session was left blocked on a one-line prompt edit it could not see.

## The same omission affected four other surfaces

- **Injection pre-scan.** The walk that collects prompt option values for the advisor
  (`_advisor_prompt_option_values` in `src/elspeth/web/composer/service.py`) did not reach
  query templates, so a payload placed in a query template was neither displayed nor
  scanned.
- **Degeneracy signal.** `_render_interpolated_row_fields` computed over the node-level slot
  rather than the templates that actually render.
- **Human review.** The `llm_prompt_template` review card and its attestation anchor covered
  the node-level prompt; per-query templates were not part of what a reviewer attested.
- **Planner teaching.** The LLM output-contract rules in
  `src/elspeth/web/composer/planner_authoring_aids.py` described plain query variables, while
  the validator requires `{{ row.<variable> }}`.

## Impact

Two distinct costs. The gate can block a session over a constraint the author has already
satisfied, with no way to show it the repair. Separately, prompt text that reaches a provider
can bypass the injection pre-scan and the human review attestation, so a review record can
attest a prompt that does not run while the one that does runs unreviewed.

## Where the work starts

`src/elspeth/web/composer/service.py` holds the advisor evidence walk and the key set that
decides which option values are shown. The other surfaces are
`src/elspeth/web/composer/planner_authoring_aids.py` (what the planner is taught),
`src/elspeth/web/interpretation_state.py` (the review card and its attestation anchor) and
`src/elspeth/plugins/transforms/llm/base.py` (the rule that makes a node-level template
dead). Tests live under `tests/unit/web/composer/`.

**Check the current state before starting.** This report is a dated observation, and the
evidence walk has been actively worked on since; compare the key set and the per-query walk in
`service.py` against the description above before assuming any of it still holds.

## Fix

Evidence, injection scanning, the degeneracy signal, the review card and the attestation
anchor must all be computed over the node's *effective* prompt text — each query's rendered
template plus the node `system_prompt` — expanded per query rather than concatenated into one
blob, with an explicit marker for which queries still fall back to the node-level template.
The planner teaching must state the variable form the validator enforces.

One consequence needs care rather than invention: when a review anchor starts covering a
different surface than before, it must be treated as a new anchor, so previously attested
reviews reopen instead of silently carrying over an attestation of something else.

## Done looks like

- For a multi-query node, the advisor evidence contains each query's template and the node
  `system_prompt`, and names which queries use the node-level template.
- A payload placed only in a query template is scanned by the injection pre-scan.
- A review attested before the surface changed does not remain attested afterwards.

## Size

Medium and cross-cutting: five surfaces must move together, or the gate stays blind in
whichever one is missed. A design covering them is written up in
`docs/plans/2026-09-13-advisor-multiquery-evidence-fix-plan.md`; read it before estimating.
