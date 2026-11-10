# Gate-evaluation diagnostic: kind-truthful repair (report)

Branch `fix/composer-gate-eval-kind-repair`, cut from 846bc4b0e.

## Defect

`gate_expression_type_mismatch_against_source_schema` (preview of gate conditions against
sampled observed-CSV rows, `generation.py`) was emitted for every `ExpressionEvaluationError`
and always told the model to declare a numeric field type. A misspelt field (`missing_key`),
a zero divisor, etc. sent the model on the wrong repair.

## Change (one producer site)

`_gate_evaluation_failure_text(kind, *, node_id, condition, field)` in
`src/elspeth/web/composer/tools/generation.py` returns `(message, suggested_repair)` by
`match` over the closed `ExpressionEvaluationKind`, with `case _: assert_never(kind)` (no soft
lookup, no silent fallback; an unknown kind raises AssertionError). The `except
ExpressionEvaluationError as exc` arm passes `exc.kind`. Same error code, same blocking
severity, same evidence_locator keys. Only `kind` is used, never `str(exc)`, so the existing
"raw evaluation error withheld" concern (row values / observed key set) still holds; every
message keeps the withheld note.

Per-kind text (repair gist):

- incompatible_types: unchanged numeric-typing repair (declare `<field>: int` in the source schema).
- missing_key: field-name problem; compare names the condition reads with observed field names via `inspect_source`, fix the spelling or column mapping.
- index_out_of_range: guard the index / use one present in every row.
- division_by_zero: guard the divisor.
- arithmetic_overflow: simplify arithmetic (avoid large powers/repeated multiplication).
- invalid_value: restructure so the operation only receives values it accepts (avoid int()/float() over free text).
- non_finite_result: guard operands against NaN/infinity.
- unexpected_error: states it is not a field-typing mismatch and includes resource failures such as memory exhaustion; repair is to simplify the condition.

evidence_locator: NOT extended with the kind. `ProofEvidence` in `_generation_responses.py`
is a closed pydantic record (`extra="forbid"`, strict, frozen), so a new key would be a wire
shape change needing the record, wire pins and frontend consumers; out of scope. Follow-up if wanted.

## Consumer / pin compatibility

Grepped `gate_expression_type_mismatch_against_source_schema` across src/tests/docs.

- error_codes.py:277 registry entry: code unchanged.
- execution/service.py:351 maps the code to node kind "gate": code-keyed, unaffected; no admission/blocking change.
- tests: test_gate_type_proof_admission (integration), test_service_admission, test_validation_diagnostics, test_source_proof_readiness, test_tools_preview, convergence-scenarios comment: all assert code/severity/locator, none pins message text. All green.
- skills/pipeline_composer.md:1468 (repair-table row): says "Declare numeric fields in source schema, or insert a type_coerce ...". After this change that row is only right for incompatible_types; for other kinds the diagnostic's own suggested_repair is now authoritative. NOT edited (out of scope). Follow-up: reword the row to "for incompatible types; otherwise follow the diagnostic's suggested_repair".
- docs/agents/sweeps/tier-burndown/B39.rationales.json mentions the code (historical).
- The validation guidance catalogue (`_VALIDATION_GUIDANCE_BY_CODE`) has no entry for this code (grep of the code string finds it only in the files above).

## MemoryError finding (report only)

`MemoryError` is NOT in `contract_errors.TIER_1_ERRORS` (measured: `MemoryError in TIER_1_ERRORS` -> False, no member is a superclass). In `ExpressionParser.evaluate` it is not in the re-raise lists, and since `MemoryError` subclasses `Exception` it falls to `except Exception` and is wrapped as `ExpressionEvaluationError(kind="unexpected_error")` (measured by patching `_ExpressionEvaluator.visit` to raise MemoryError: result `ExpressionEvaluationError unexpected_error ... : MemoryError`). So the `except MemoryError` arm in generation.py can fire only when the MemoryError escapes outside that try block: `_ExpressionEvaluator(...)` construction, building the wrapping exception inside the handler, or when `evaluate` itself is replaced (the existing test monkeypatches `evaluate`, which is why it passes). In production the arm is effectively shadowed; the `gate_expression_preview_memory_exhaustion` code is rarely reachable. With this change a wrapped MemoryError gets the unexpected_error text: explicitly "not a field-typing mismatch", mentions resource failures, repair = simplify the condition; no numeric-typing repair (pinned by a test that drives the real wrapping path). Follow-up option: have the evaluate wrapper re-raise MemoryError (expression_parser.py, out of scope here).

## Red -> green

Tests added: `tests/unit/web/composer/tools/test_gate_evaluation_kind_repair.py` (helper: every member of the kind Literal via `get_args`, distinctness, incompatible_types control, no numeric-typing text for the rest, unknown kind refused) and four tests in `test_tools_preview.py` (missing_key natural end to end; division_by_zero and wrapped-MemoryError end to end through the real `evaluate` wrapper; incompatible_types control).

- Before the fix: unit module fails at collection (ImportError, helper absent) -- `.claude/lanes/gatekind/red.log`; the three e2e kind tests FAIL, the incompatible_types control passes (`red-e2e.log`: 3 failed, 1 passed). This pre-fix run is the mutation check (fix absent -> red); I did not separately re-revert afterwards.
- After: test_gate_evaluation_kind_repair + test_tools_preview: 166 passed, exit 0 (`green.log`).

## Verification (logs in .claude/lanes/gatekind/, run with -n 4)

| Scope | exit | result |
| --- | --- | --- |
| tests/unit/web/composer | 0 | 13559 passed |
| tests/unit/web/execution | 0 | 2257 passed |
| tests/unit/architecture | 0 | 2012 passed, 1 xfailed (maintainer-approved xfail; not touched) |
| tests/unit/evals | 0 | 475 passed, 18 skipped |
| tests/integration/web/test_gate_type_proof_admission.py | 0 | 3 passed |
| ruff check src tests/unit/web/composer/tools | 0 | clean |
| ruff format --check (same) | 0 | clean (generation.py reformatted once after the suites; whitespace-only line joins) |
| mypy generation.py + new test | 0 | no issues (2 files; not whole-repo) |
| python -m scripts.check_contracts | 0 | soft-mapping census matches; no re-pin needed (a `match` was used, not a mapping) |

The suites ran before the final `ruff format` of generation.py (string-literal line joins only, no AST change); format and ruff were re-run clean afterwards, but the suites were not re-run.

Keyless trust-tier scan (`elspeth_lints.core.cli check --rules all`, shape-only mode), generation.py findings before (base file in a scratch copy) vs after: identical set (R5/R1 sites, R_TB_SUPPRESSED); only line numbers shifted (e.g. 3242 -> 3324). No new finding. (The scratch copy also lacked tests, so it shows extra R_TB_TESTS_FILE_MISSING rows -- a harness artefact.) Whole-run exit 1 both times as expected.

## Residual follow-ups

1. Reword skill table row (pipeline_composer.md:1468) to scope the numeric-typing advice to incompatible_types.
2. Optionally surface the kind in evidence_locator (requires ProofEvidence + wire pins + frontend).
3. Decide whether `ExpressionParser.evaluate` should let MemoryError propagate so the memory arm is live.
4. A kind-specific code split (e.g. missing_key) would be a registry change; not done by design.
