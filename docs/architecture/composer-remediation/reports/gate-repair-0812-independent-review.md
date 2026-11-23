# Independent review: gate-evaluation kind-truthful repair, ported to 0.8.12

Reviewed 2026-11-11 on `work/gate-0812` (cut from 0.8.12 tip `5237494d5`). Reviewer: Claude
(Sonnet 5.5) with an independent read-only pass by Astra (`gpt-6-astra`); every Astra finding
below was re-checked against the code or by a probe before being recorded.

**Verdict: LAND WITH FIXES.** The port was clean; the review found three truthfulness defects
in the repair texts (two the author's own review missed) and fixed them.

## Port

`git cherry-pick -x` of 797ecbe6c, 5a42603b3, aac0e2467, 45682eb62 onto 0.8.12. No conflicts
(git auto-merged `pipeline_composer.md` and `test_prompts.py`). The fix does not touch
`NEXT-SESSION.md` or `CHANGELOG.md`, so nothing was dropped. The planner prompt-size pin in
`test_pipeline_planner.py` still passes (375 passed); it was run, not assumed.

## What was checked

**Kinds.** `ExpressionEvaluationKind` has 8 members (`expression_parser.py:41`). The `match` in
`_gate_evaluation_failure_text` covers all 8 with `assert_never`, and a test pins the set size.
Over rows that are `dict[str, str]` the reachable causes per kind were enumerated:

- `missing_key`, `incompatible_types`: see findings 1 to 3.
- `index_out_of_range` (string index), `division_by_zero` (`len(a)/len(b)`), `non_finite_result`,
  `arithmetic_overflow`, `unexpected_error`: text is true; nothing found that contradicts it.
- `invalid_value`: only `decimal_within` (and wrapped ValueError) reach it; text is true.

**Composer invariants.** Every text is feedback to the planner: it says what failed and what to
check or change. Nothing in `generation.py` rewrites a gate condition, a schema or a node; the
`row.get('<field>') is not None and <condition>` text is an example in prose. No
tutorial-specific branch. `provider="server"` is not involved.

**Way-out.** The diagnostics are consumed by `_attempt_proof_repair`
(`composition_completion.py:1022`): at most `_MAX_REPAIR_TURNS` = 2 injections, then `blocked`,
which is preserved as an explicit non-runnable result. The injected message tells the model it
may explain the blocker without changing the draft (`:1086`). So no loop is unbounded and the
model can return to the user. Two texts, however, offered no satisfiable move (findings 1, 2);
fixed.

**CSV sampler.** `_sample_csv_rows` probed directly: empty file, header only, header cut,
one row longer than the cap, multi-byte character split at the cut (with and without a
terminated earlier row), quoted field with newline (open and closed), unterminated closed quote,
CRLF cut between CR and LF, cut exactly on a record boundary, TSV, 100-row cap. No input
sampled a truncated record or dropped a complete one. One unchanged behaviour: a file whose
first line is blank gives an empty header and empty-dict rows (the old `DictReader` did the
same); not a regression.

**Leakage.** No row value, raw header or observed key set reaches a message, evidence
locator or audit record. Two small disclosures remain by design: the names the model itself
wrote in the condition that the failing row lacks, and `sample_row_index`. The model can read the
same header facts through `inspect_source`, so no custody rule is crossed.

**Trust-tier.** No new `getattr`/soft mapping, no `# noqa`, no `# type: ignore` in the diff.

## Findings

| # | Severity | Finding | Disposition |
|---|----------|---------|-------------|
| 1 | High | Preview keys sampled rows by the RAW CSV header; runtime keys are normalized (`Order Amount` becomes `order_amount`). A gate using the runtime name is blocked, and the `missing_key` text said the name was "not a column" or the row short, then told the model to rename: renaming to the raw spelling passes preview and breaks runtime. Probed live: `row['order_amount']` blocked, `row['Order Amount']` passes. Present before the fix (old text sent the model to declare a type), now made worse by a confident wrong cause. Sites: `_sample_csv_rows` and `_gate_evaluation_failure_text` in `generation.py`. | Text fixed: names the third cause, forbids renaming to the raw spelling, and tells the model to tell the user. **Root cause owed**: sample rows should be keyed by the runtime row names (see Owed). |
| 2 | High | `field=fields[0]` in a multi-field condition: `row['customer'] == 'Alice' and row['price'] >= 100` told the model to declare `customer: int`. | Fixed at the call site (generic `<field>: int` unless the condition reads one field). Test added; red without the fix. |
| 3 | Medium | `incompatible_types` also arises from `starts_with(row['x'], 1)` and `row['x']['bad']` (probed); the unconditional "declare a numeric type" cures neither. A short row with `row.get('price')` was offered a spelling check and a type but not the guard. | Both texts now point inside the condition and offer the guard. |
| 4 | Low | `absent_fields` is collected across the whole expression, so `row.get('absent') is None and starts_with(row['x'], 1)` blames the safely handled absence. Texts are conditional ("First compare"), so not untrue; left as is. | Accepted. |
| 5 | Low | `gate_expression_preview_memory_exhaustion` is reachable only if the evaluator's constructor fails (already NEXT-SESSION B13). | Unchanged. |
| 6 | Low | Several of the author's CSV tests are preservation controls that pass without the fix (Astra: uncut keeps final row, boundary cut, header longer than cut, CRLF, ragged non-cut row, row cap). They guard against over-dropping, which is a legitimate second direction. | Accepted. |

## Tests proved red without the fix (scratch mutation of `generation.py`, restored from a backup copy; `git diff --stat` confirmed only the intended diff remains)

- `cut = False` in `_sample_csv_rows`: 55 failures in `test_gate_sample_csv_rows.py` and
  `test_observed_csv_gate_on_a_correct_field_is_not_blocked_by_the_byte_cut`.
- Ignore `exc.kind` (always `incompatible_types`): the missing-key, division-by-zero and
  wrapped-MemoryError preview tests fail.
- `absent_fields = ()`: the misspelt-`get` and short-row preview tests fail.
- `field` without the single-field guard: `..._does_not_name_a_string_compared_field` fails.
- The new text assertions in `test_gate_evaluation_kind_repair.py` (raw spelling, way out,
  not-the-only-cause) are string assertions on the changed text; red by construction.

Not mutation-tested: the skill-row assertion in `test_prompts.py` (reasoned from the assertion:
it requires the changed row text) and the preservation controls in finding 6.

## Verification (all `-n 0`, one file at a time, load under 6)

`test_gate_evaluation_kind_repair.py` 104 passed; `test_gate_sample_csv_rows.py` 74;
`test_tools_preview.py` 149; `test_prompts.py` 75; `test_pipeline_planner.py` 375 (2 deselected);
`test_skills_loader.py` 21; `test_capability_skill_identity.py` 33;
`test_tall_dialog_fixture_prompt_hash_pin.py` 3; `test_planner_teaching_gate.py` 22;
`test_prompt_cache_layout.py` 10. ruff check and format clean; mypy on `generation.py` clean.

## Owed

- Root cause of finding 1: key the preview sample by the runtime row names (the source's
  normalization and `field_mapping`) so a correct gate is not blocked. A separate change.
- Not run: the whole-tree AST gates (CONTRIBUTING § Whole-tree gates), the keyless lint set-diff,
  `pytest tests/` and the testcontainer job, and the other tests that read `pipeline_composer.md`
  (integration `web/composer` and `pipeline` files). The diff adds no dynamic-attribute site,
  suppression or masquerade, but that is reasoning, not a measurement.

## Changelog line

"Composer: the gate-preview mismatch diagnostic now names the actual evaluation failure (misspelt
field, short row, zero divisor, bad function argument) instead of always advising a numeric
source-schema type, and the preview no longer samples a CSV row cut short by its 8 KiB byte
limit."
