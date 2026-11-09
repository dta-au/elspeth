# Task 2 — GS-02 diagnostic match codes

Implementation is frozen, uncommitted, and ready for parent/Astra review in `/workspace/elspeth/.claude/worktrees/composer-remediation-discovery`, HEAD `6825994b482bc274df2ae85d8738b9078f0117b4`.

Review patch: `task-2-review.diff` in this lane. SHA256: `f572f10fc652d2cabf7ee03e3be13736c4acabd9c854b8bccede23cd67579f99`.

## Behavior and boundary

The exact-code branch now publishes its canonical code. The first matching legacy regex retains precedence, explanation and Expected-hint augmentation. Its code comes exclusively from the existing guidance index entries whose record is the matched tuple by identity. An explicit code among those owners wins in index order. A unique-owner prose record identifies its code without an explicit spelling. An unowned or ambiguous prose record omits `error_code`; other codes elsewhere in the input cannot supply ownership. Existing fuzzy behavior remains unchanged.

Live index measurement: 168 closed codes; the new matrix exercises all 504 exact/lowercase-noise/uppercase-noise calls. There are three shared regex records: coalesce on_success (2 owners), coalesce policy/merge (5 owners), dangling on_success (3 owners). Every alias is covered by the matrix and each record has a competing-explicit-code precedence test. The first and third have ambiguous prose witnesses that explicitly retain omission. The second only matches explicit code alternatives. This intentionally retains an optional field for ambiguous prose rather than guessing which of several codes was emitted. Runtime-only `union_field_collision`, unknown input, an unowned first regex followed by unrelated code, equal explanation/fix prose on a different record, competing regexes and Expected hints have controls.

No new arguments, catalog coverage, redaction policy, reflection, mock sites or Task 1 edits. Compatibility is additive within the existing optional `error_code` response contract.

## Red/green evidence

All invocations ran with explicit workdir above. Common environment and pytest arguments below were literal:

```bash
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
.venv/bin/python -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)'
```

Both printed roots resolve inside the worktree (production `src/elspeth/__init__.py` and `elspeth-lints/src/elspeth_lints/__init__.py`). Each test command wrote the indicated lane-private log with `> LOG 2>&1`; its result was captured immediately as `result=$?`, printed as `exit=$result`, and read only after completion.

Affected selection, used for baseline, pre-fixture check and final green:

```bash
.venv/bin/python -m pytest -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" tests/unit/web/composer/test_tools.py::TestExplainValidationError tests/unit/web/composer/test_validation_guidance_catalogue.py tests/unit/web/composer/test_generation_response_contracts.py tests/unit/web/composer/test_discovery_response_contracts.py
```

- `task-2-baseline.log`: exit 0, 124 passed before edits.
- Red command: `.venv/bin/python -m pytest -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" tests/unit/web/composer/test_validation_guidance_catalogue.py`; `task-2-red.log`: exit 1, **290 failed, 258 passed** against unchanged producer. Example first failure: `test_closed_code_is_canonical_on_every_match_branch[exact-unknown_node_type]` raises `KeyError: 'error_code'`. Identifiable prose, lowercase noisy codes, direct exact shapes and competing regex code assertions also fail. Unknown/runtime/uppercase existing behavior controls pass.
- `task-2-green-pre-fixture.log`: exit 1, 638 passed and exactly one failure, frozen baseline `explain_exact`. Producer behavior tests passed before updating expected bytes.
- `task-2-green.log`: exit 0, **642 passed** after producer fixture regeneration and expanded response-contract assertions.

## Fixture producer evidence

Actual fixture: `tests/unit/web/composer/fixtures/generation_response_wire_4f206253.json`. Regenerated through existing test `collect()` and `_contract(name).admit(result.data).to_wire()` plus the same `json.dumps` serializer used by its byte gate. `task-2-fixture.log`: exit 0, `Producer-regenerated fixture cases: ['explain_exact', 'explain_hint']`.

The exact successful regeneration command body (executed through `.venv/bin/python -` with both roots exported):

```python
import json
from pathlib import Path
from elspeth.core.litellm_policy import configure_litellm_pricing
configure_litellm_pricing()
from tests.unit.web.composer.test_generation_response_contracts import collect, _contract
fixture = Path('tests/unit/web/composer/fixtures/generation_response_wire_4f206253.json')
baseline = json.loads(fixture.read_text())
results = collect()
assert set(results) == set(baseline['cases'])
changed = []
for name, result in results.items():
    case = {'success': result.success, 'data': json.dumps(_contract(name).admit(result.data).to_wire())}
    if case != baseline['cases'][name]:
        old = json.loads(baseline['cases'][name]['data'])
        new = json.loads(case['data'])
        assert name in {'explain_exact', 'explain_hint'}
        assert new == {**old, 'error_code': 'unknown_node_type'}
        changed.append(name)
    baseline['cases'][name] = case
assert changed == ['explain_exact', 'explain_hint'], changed
fixture.write_text(json.dumps(baseline, indent=2) + '\n')
print('Producer-regenerated fixture cases:', changed)
```

An initial regeneration attempt imported `configure_litellm_pricing` from the wrong module (`elspeth.core.config`), exited 1 before any fixture write, then was corrected to the actual import above. No expected bytes were hand-edited. Inspected fixture diff: only the two cases gain `error_code: unknown_node_type`; explanation, fix, hint and all other bytes remain unchanged.

## Final checks

Commands below used both exported source roots where relevant, wrote named logs in this lane and had measured exit 0:

- `.venv/bin/python -m ruff check src/elspeth/web/composer/tools/generation.py tests/unit/web/composer/test_validation_guidance_catalogue.py tests/unit/web/composer/test_generation_response_contracts.py` → `task-2-ruff-check.log`, all checks passed.
- `.venv/bin/python -m ruff format src/elspeth/web/composer/tools/generation.py tests/unit/web/composer/test_validation_guidance_catalogue.py tests/unit/web/composer/test_generation_response_contracts.py` → `task-2-ruff-format.log`, 1 reformatted/2 unchanged; final `ruff format --check` with same paths → `task-2-ruff-format-check.log`, 3 already formatted.
- `.venv/bin/python -m mypy src/elspeth/web/composer/tools/generation.py` → `task-2-mypy.log`, success, no issues in 1 source file (existing unused-config-section note only).
- `.venv/bin/python -m scripts.check_contracts` → `task-2-contracts.log`, all checks passed; soft census remains 2615 soft occurrences/416 files/60 boundary-parsed.
- `.venv/bin/python -m pytest -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" tests/unit/web/test_sessions_composer_attribute_contracts.py` → `task-2-attribute.log`, 5 passed.
- `git diff --check` → exit 0.

Self-review covered the complete four-file patch. Final changed paths are only `generation.py`, `test_validation_guidance_catalogue.py`, `test_generation_response_contracts.py`, and the producer-regenerated fixture. Parent owns final consolidated all-rule lint comparison, expensive whole-tree masquerade/mock gates, broad suite, independent Astra review and integration. No provider calls, subagents or commits were made.
