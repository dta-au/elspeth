---
title: Composer planner authors schema options that validation rejects; cause not discriminated
labels: [area/composer, type/bug]
---

The composer's planner repeatedly authored `schema` option blocks that the schema
validators reject, which blocked pipeline construction on a staging deployment. Three
sessions produced three different schema errors. Whether this is a regression or
long-standing planner variance has not been established, and establishing that is the
first task here.

The planner is the component that turns a user's prompt into pipeline structure, at
`src/elspeth/web/composer/pipeline_planner.py`. The rules it has to satisfy are in
`src/elspeth/contracts/schema.py`. Every node carries a `schema` option declaring the
fields it guarantees; as the errors below show, `mode: flexible` requires an explicit
`fields` list, and `guaranteed_fields` must be a subset of the declared fields.

## What happens

Attempts to build a pipeline failed, each rejected on the same `schema` option with a
different message:

- `'fields' key is required for mode 'flexible'`
- `'guaranteed_fields' contains fields not declared in schema: colour_pair_llm_response, hex_code_llm_response. Declared fields are: colour.`

A further session failed with `profile_unavailable` on `['schema/fields']`; that is a
separate defect in an option-profile gate and is out of scope here. The
`guaranteed_fields` rejection occurred on the 0.8.1 build (`9a02bac8f`), after that gate
defect was addressed, so it is genuine semantic validation rather than the gate.

## Why

The cause is not established. What has been measured:

- **The validators did not change.** AST hashes of the two functions carrying the
  `guaranteed_fields` rule are identical across the 0.8.0 build (`947a3b2`) and the 0.8.1
  build (`9a02bac8f`): `_validate_contract_fields_subset` (`dc9fe5f7738d6f99`) and
  `declare_missing_guaranteed_fields` (`52007cf72412f1b7`), both in
  `src/elspeth/contracts/schema.py`. The only other delta in that file between the builds
  is a docstring. The rule dates to `6394b11b0` (6 August 2026).
- **The planner-facing guidance did not change.**
  `src/elspeth/plugins/infrastructure/config_base.py` is blob-identical across both builds
  (`aab18b99c3f8899c8ed9c369773da9bd6f7cda56`), so `COMPOSER_SCHEMA_EXAMPLE` and
  `COMPOSER_SCHEMA_DESCRIPTION` — the text that instructs the model how to write a `schema`
  block — are unchanged.

One explanation has been measured and disproven, and should not be pursued again. An
earlier write-up claimed `src/elspeth/web/composer/tools/schema_contract.py` lost roughly
two thirds of its content between the builds; measured, it went from 787 to 626 lines
(−161, −20%; the 215 in the diffstat counts changed lines on both sides). Every deletion
belongs to one family of six name-constraint disclosure helpers covering node names, sink
names and route destinations — nothing touching `schema`, `fields`, `mode` or
`guaranteed_fields`.

Hypotheses still open, none discriminated:

1. **Planner feedback shape changed.** The planner moved schema-failure responses from
   inline dicts to closed envelopes: 0.8.0 returned
   `{"error": ..., "error_code": "schema_projection_unavailable"}`, 0.8.1 returns
   `closed_provider_envelope(result, success=False, data=schema_projection_failure())`
   (both helpers in `src/elspeth/web/composer/provider_discovery_response.py`). That is a
   different recovery signal to the model after a failure, with the same guidance text.
2. **Model or provider parameters differ** between the two deployed builds.
3. **The 0.8.1 mid-turn advisor checkpoint** interferes with the authoring turn.
4. **Nothing changed and the planner is stochastic.** Three sessions producing three
   different errors is itself consistent with sampling variance rather than a regression.

## Impact

Pipeline authoring is blocked on the affected deployment for prompts that lead the planner
into this shape.

There is no baseline to compare against. The deployment ran the 0.8.0 build from
2 September 2026 until 14 September 2026, and the roll to 0.8.1 recreated its session
store, destroying every earlier session. That 0.8.0's planner would have authored
`{"mode": "observed"}` for the same prompt and passed is inference, not proof.

## Fix

The first deliverable is a verdict, not a patch. Run the same prompt against both builds —
a prompt about colours, which drives the planner towards a node producing `colour`,
`colour_pair_llm_response` and `hex_code_llm_response`. That single comparison separates
all four hypotheses:

- 0.8.0 succeeds → the regression is real; bisect between `947a3b2` and `9a02bac8f`
  (hypotheses 1–3).
- 0.8.0 fails the same way → this is not a regression; the planner has never reliably
  authored this shape (hypothesis 4), and the work is improving the guidance the planner
  is given, not a bisect.

Done, for this issue, is that verdict recorded with the two transcripts behind it. The
follow-on fix is then scoped by which branch it lands on, and should be a separate issue.

Size and readiness: this is not a code task yet and it cannot be started from the
repository alone — it needs both builds runnable against a provider, so the cost is
environment setup rather than lines changed. Do not change code before the comparison; the
open hypotheses include one in which there is no defect in the code at all.
