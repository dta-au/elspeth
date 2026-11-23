# Gate-evaluation kind-truthful repair: independent review

Reviewed `git diff 846bc4b0e..797ecbe6c` on `fix/composer-gate-eval-kind-repair`
(HEAD 5a42603b3; `797ecbe6c..HEAD` is the author's report only, so runs at HEAD
cover the reviewed, post-format tree).

## Verdict: merge after fixes

One must-fix (M1), a wording change in one `case` arm. The mechanics of the change
are sound: same code, same blocking severity, same locator, exhaustive `match`,
value-free. If the coordinator rules M1 an accepted known defect, the rest is
mergeable as it stands.

## Must-fix

### M1. `missing_key` text is false for a correctly spelt field, and removes the only exit the model had

`src/elspeth/web/composer/tools/generation.py:3185-3192` says "This is a field-name
problem ... correct the spelling in the gate condition or the source column mapping".

`_sample_csv_rows` (`generation.py:2978-2987`) cuts the blob at 8 KB and drops
`None` values, so a row cut mid-line (or a ragged short row) lacks its trailing keys.
A string-safe gate passes every full row and then raises `missing_key` on the cut row.

Measured end to end (scratch test `.claude/lanes/gatekind/test_review_trunc.py`, log
`review-trunc.log`): a 400-row CSV with columns `order_id,customer,price`, observed
schema, gate `row['price'] == '9'` (correct name) returns the blocking diagnostic
with the new missing_key text at `sample_row_index: 51`. A direct probe
(`review_probe2.py`) hit this on 103 of 110 row widths tried. Preconditions: file
over 8 KB, fewer than 100 rows in the first 8 KB, gate reads a non-leading column,
earlier rows evaluate cleanly (string equality gates do).

The false blocker itself predates this change. What the change alters:

- Old text (wrong cause) told the model to declare a schema type. That leaves
  observed mode, and `generation.py:3035` then returns no diagnostics, so the model
  converged by accident.
- New text tells the model to fix a spelling that is already right. Nothing it
  names clears the blocker. I did not run a live model against it; the claim rests
  on the code path above.

Minimum fix in this change: the `missing_key` arm must not assert a naming problem
as the only cause. Name both causes (a field that is not an observed column, or a
sampled row that is short or cut off) and give a repair that works for the second.
The root defect (sampling a truncated last row) is separate and should be reported,
not fixed here.

## Should-fix

### S1. Two kinds' texts can be swapped with no test going red

Mutation via a lane-private source overlay (import path confirmed to be the overlay
before each run; logs `review-mut-M*.log`):

| Mutant | Result |
| --- | --- |
| M1 call site passes constant `"incompatible_types"` | red, 3 failed |
| M2 swap `index_out_of_range` and `arithmetic_overflow` arms | **survived**, 166 passed |
| M3 swap `non_finite_result` and `unexpected_error` arms | **survived**, 166 passed |
| M4 swap `missing_key` and `division_by_zero` arms | red, 4 failed |

Only four kinds have a positive assertion
(`tests/unit/web/composer/tools/test_gate_evaluation_kind_repair.py:37-68`);
distinctness (`:33-35`) is a set-size check. `test_unexpected_error_does_not_claim_a_typing_mismatch`
passes on the non-finite text too. Add one distinguishing phrase assertion per kind.

### S2. `invalid_value` and `arithmetic_overflow` repairs name constructs the gate language forbids

- `generation.py:3215-3217` advises avoiding `int()` / `float()`. Both are rejected
  by `_validate_gate_expression` ("Forbidden function call"), so a gate containing
  them never reaches this evaluation. The reachable source I found is
  `decimal_within` (negative tolerance, non-finite operand).
- `generation.py:3207-3208` advises avoiding "large powers". `Pow` is a forbidden
  binary operator.

Not harmful, but the examples point at causes that cannot be the cause.

### S3. `incompatible_types` still mis-repairs a misspelt field read with `.get`

`row.get('prcie') >= 100` and `lower(row.get('x'))` evaluate `None` and raise
`incompatible_types`, so the model is still told to declare `prcie: int`. Same as
before this change, but it contradicts the report's claim that only
incompatible_types is the string-typing problem. Also `row['price']['x']` (a str
subscripted by a str key) lands here. Follow-up, or soften the report.

### S4. Skill row `src/elspeth/web/composer/skills/pipeline_composer.md:1468`

The table is framed as "common one-shot mappings" from code to repair, so it
competes with the diagnostic rather than deferring to it; line 1385 does say to
apply `suggested_repair`. For non-typing kinds the model now gets two different
instructions. It was equally wrong before, so this is not a merge blocker. Do it as
a separate follow-up (a skill edit moves the skill hash).

## Notes

- N1. `division_by_zero` wording says "divides"; modulo by zero takes the same arm. Close enough.
- N2. Dict-literal lookup keyed by a row value is rejected by the validator
  ("Subscript access is only allowed on allowed names"), so that is not a second
  false `missing_key` path.
- N3. Value-freedom holds. The helper takes only `kind`; `node_id`, `condition!r`
  and `field` were all in the old text, and `field` still appears only in the
  incompatible_types arm. No `str(exc)`. The missing_key message does disclose one
  new bit (a read field is absent from a sampled row); it is derived from `kind`.
- N4. Behaviour unchanged: code, `_blocking_diagnostic`, locator keys, the `break`,
  and arm order before `except MemoryError` are identical. No consumer string-matches
  the old message or repair text (grep over src, tests, frontend, docs; only the code
  string is matched, at `execution/service.py:351` and in tests).
- N5. `match` with `assert_never`, no mapping, no new dynamic attribute access
  (`exc.kind` is a typed attribute). `ruff check` and `ruff format --check` on the
  three files: exit 0.
- N6. The suites running before the final `ruff format` does not matter: I re-ran
  the affected tests on the formatted tree (below).
- N7. Two module-level insertions shift `body[N]` AST paths for later allowlisted
  sites in `generation.py`, and the edited handler changes its scope fingerprint.
  This is ordinary binding churn for the operator to sign later.

## Runs (worktree HEAD, `-n 4`, logs in `.claude/lanes/gatekind/`)

| Scope | exit | result |
| --- | --- | --- |
| `tests/unit/web/composer/tools` | 0 | 587 passed (`review-tools.log`) |
| the two touched test files | 0 | 166 passed (`review-touched.log`) |
| pins the author did not run: `elspeth_lints/test_composer_rules.py`, `test_tier_model_scope_fallback_match.py`, `scripts/cicd/test_runtime_rejection_parity_gate.py`, `scripts/test_composer_wire_scorecard.py`, `contracts/test_freeze_regression.py` | 0 | 209 passed (`review-pins.log`) |
| `integration/web/test_gate_type_proof_admission.py`, `composer/test_csv_blob_header_mismatch.py`, `composer/parity/test_repair_and_deferral.py` | 0 | 29 passed (`review-integ.log`) |

## Not verified

- The author's keyless trust-tier before/after comparison (not re-run).
- Full `tests/unit/web/composer`, `architecture`, `evals`, mypy, `check_contracts`
  (author's results taken as claimed; I re-ran only the subsets above).
- Any live-model behaviour, including whether a model actually loops on M1.
- Frontend rendering of the longer messages (no frontend text match found; not run).

Scratch left in `.claude/lanes/gatekind/` (gitignored): `review_probe.py`,
`review_probe2.py`, `review_mutate.py`, `test_review_trunc.py`, `review-*.log`. The
35 MB source overlay was deleted after the mutation runs. No tracked file changed.
