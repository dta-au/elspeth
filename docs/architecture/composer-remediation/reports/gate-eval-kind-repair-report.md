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

## Review fixes

Response to `gate-eval-kind-repair-review.md` (verdict: merge after fixes). Work on the same branch,
base tip 5a42603b3. Logs are in `.claude/lanes/gatekind/` (gitignored). All suites ran with
`-n 4` on the final, formatted tree; the tree hash (`git diff` + the new untracked test file) was
identical before and after the run.

### M1. False blocker behind `missing_key` (fixed at the producer)

Reproduced first: a 400-row CSV (`order_id,customer,price`, 150-char customer, 8.6 KiB) with
`row['price'] == '9'` returned the blocking diagnostic (red: `red-cut-e2e.log`, 1 failed).

Root cause: `_sample_csv_rows` decoded `content[:8 KiB]` and yielded the last record even when the
byte cut had landed in it. Fix in `_sample_csv_rows` (`generation.py`): it now takes
`total_size_bytes`; when `total_size_bytes > len(sample)` the final record is dropped unless it is
provably terminated. Terminated means it ends in a newline AND re-parsing it with one more newline
appended yields the same record, which keeps a newline inside an open quoted field from passing as a
terminator (the same test `source_inspection._inspect_csv` already applies to its tail). A blob that
was not cut keeps an unterminated final record. A cut inside a multi-byte character, a CRLF cut
between CR and LF (the record is terminated and kept), a header longer than the cut (no rows), and a
blob with exactly one complete row are each pinned in
`tests/unit/web/composer/tools/test_gate_sample_csv_rows.py` (74 cases, including every cut offset
over a six-row file). Still value-free and bounded (8 KiB, 100 rows).

Consumers of `_sample_csv_rows`: one, `_gate_expression_type_diagnostics_for_observed_csv`, which now
receives `total_size_bytes` (already in scope in `compute_proof_diagnostics`). Siblings with the same
8 KiB cut, measured not assumed:

- `source_inspection._inspect_csv`: already handles the cut (`sample_truncated`, uncertain tail excluded). No defect.
- `source_inspection.inspect_json_source_content`: pops an incomplete last JSONL line. No defect.
- `source_inspection._inspect_jsonl`: has no cut flag. Measured: 200 wide JSONL lines give 49 rows and the warnings `jsonl parse error: Unterminated string...` plus `1 jsonl line(s) failed to parse as objects`. A spurious warning, not a wrong header or row count claim. NOT fixed (different function).
- `source_inspection._inspect_text`: a cut final line counts as a sampled row and can feed a truncated URL candidate. Counts only. NOT fixed, report only.
- `tools/sources.py` `_INSPECT_SOURCE_MAX_BYTES`: bounds the prefix handed to `inspect_blob_content`, which carries the total size. Not read further.

`missing_key` text is now true for what remains. With the cut removed, a sampled row lacks a read
field only if the name is not a column or the row is shorter than the header, so the message names
both causes and says the check covers only the first rows of the file; the repair keeps the
`inspect_source` name check and adds an exit for correct names (fix the data or guard with
`row.get('<field>') is not None and ...`).

Evidence: unit module red before the fix (74 failed, signature), e2e red (`red-cut-e2e.log`);
green after (`green3.log`: 323 passed). The e2e test also compares the whole blocking-code set of
the small and the large CSV (equal). Sampler mutants, all killed (`sampler-mutation.log`): cut
detection off, cut always on, quote-aware terminator test removed, terminator never true,
`absent_fields` always empty / always all.

### S1. Kind-to-text pins

`test_gate_evaluation_kind_repair.py` now has one distinguishing phrase per kind (a table checked
to cover every kind) that must appear in its own kind's text and in no other kind's text. Swap
mutation over every one of the 28 kind pairs by wrapping the helper (`swap-mutation.log`): 28 of 28
killed, 0 survivors (the author's mutants M2 and M3 are among them). Control: the unmutated module is
green (`green3.log`). The swap run predates a later `absent_fields` signature tweak; the tweak does not
touch the per-kind phrases and the module is green on the final tree.

### S2. Repairs cite only what the grammar allows

Read `expression_parser.py`: `int()`, `float()`, `**` are not in the grammar; the allowed calls are
`len abs lower upper strip casefold starts_with word_count decimal_within` and `row.get`.
`invalid_value` is reachable through `decimal_within` (negative tolerance, non-finite operand), so its
repair now names exactly that. `arithmetic_overflow` is reachable only from very large literals
(strings are strings, no `**`), so the repair says to shrink literals or compare directly. A test
asserts none of `int()`, `float()`, `power`, `**` appear in those kinds.

### S3. `row.get('typo')` (made truthful, value-free)

Trace: `row.get('prcie')` is `None`; `None >= 100` raises `TypeError` in `visit_Compare`, wrapped as
`incompatible_types`. The same arm fires for `len(row.get('price'))` on a short row. It is
distinguishable without any value: the call site now passes `absent_fields`, the names the condition
reads that the FAILING row does not carry (names only, operator-authored text). When non-empty, the
incompatible_types text says the field is not carried by the row, that `row.get()` of an absent field
yields None, and the repair checks the name with `inspect_source` first, and offers the numeric typing
only conditionally ("Only if the names are right ..."). When empty (a real string-vs-number mismatch)
the unconditional typing repair is unchanged. Tests: unit (both variants, ordering), e2e for a
misspelt name, a short row and a present field. Still open: `row['price']['x']` (a str subscripted
by a str key) also lands in `incompatible_types` with no absent field, so it still gets the typing
repair; it cannot be distinguished from a type mismatch without values.

### Skill row (`pipeline_composer.md`, repair table)

Reworded minimally: "Apply the diagnostic's own `suggested_repair`; it names the cause (a misspelt
field, a zero divisor, a string compared with a number). For the string-vs-number case: declare
numeric fields ...", rest of the row unchanged. Size 107,291 -> 107,457 bytes (+166); the skill hash
moves with the content. Pins that the edit could move, checked rather than assumed: the planner
fixed-scaffold ratchet measured 139,601 B (ceiling 139,608) both before and after the edit (the
planner request does not embed this file), the skill-inventory drift hook
(`generate_skill_inventory.py --check` exit 0), no test carries the old skill sha256 or byte count
(`git grep` of both; the sha is computed from the file at runtime), and the whole composer suite
plus the sessions skill tests are green. New test
`test_core_skill_gate_mismatch_row_defers_to_the_diagnostic_by_kind` (red against the old row).
No ceiling was raised.

### Verification (final tree, formatted; `-n 4`)

| Scope | exit | result |
| --- | --- | --- |
| tests/unit/web/composer | 1 then pass | 13716 passed, 1 failed (`test_pipeline_planner.py::test_real_inline_custody_returns_only_blob_id_and_ready_row`, "planner wall-clock budget exhausted" on a host at load average 16-21). Re-run serially `-n 0`: 1 passed. Whole `test_pipeline_planner.py` re-run `-n 4`: 377 passed, exit 0 |
| tests/unit/web/execution | 0 | 2257 passed |
| tests/unit/architecture | 0 | 2012 passed, 1 xfailed (maintainer-approved xfail, untouched) |
| tests/unit/evals | 0 | 475 passed, 18 skipped |
| tests/integration/web/composer + test_gate_type_proof_admission.py | 0 | 760 passed |
| sessions skill tests + test_skill_rgr_harness + attribute-contracts gate | 0 | 108 passed |
| ruff check src tests/unit/web/composer; ruff format --check src tests/unit/web/composer tests/unit/web/execution | 0 | clean |
| mypy src/ elspeth-lints/src/ | 0 | no issues in 1130 files |
| python -m scripts.check_contracts | 0 | soft-mapping census matches (2617 across 418 files; `match`/helper adds no mapping) |
| keyless trust-tier (`elspeth_lints.core.cli check --rules all --root src/elspeth`, shape-only) | 1 (expected corpus) | base vs final `generation.py`: whole 3721-line finding set identical after normalising line:col (15 generation.py findings both sides) |

Note: `python -m elspeth_lints check` does not run (no `__main__`); the module used is
`elspeth_lints.core.cli`, as in the original report.

### Left open

- `_inspect_jsonl` spurious parse warning on a cut line and `_inspect_text` counting a cut line / truncated URL (see M1 siblings): different functions, reported.
- `row['a']['b']` on a str still gets the typing repair (S3).
- MemoryError wrapping (`ExpressionParser.evaluate` wraps it as `unexpected_error`): unchanged, report only, as before.
- The one composer-suite failure above is load-dependent (wall-clock budget); passes serially and as a file.
- The review's frontend-rendering and live-model items were not run.
